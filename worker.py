"""Warm Qwen-Image-2.1-Turbo worker — FastAPI inside the GPU Docker container.

Loads QwenImage21Pipeline once at startup. Portal proxies POST /generate here.
Writes per-job progress under /data/progress/{id}.json for the portal to poll.

Turbo ships an 8-step `sample_sigmas` grid. A bare `num_inference_steps` does
not replace that grid, so the default call omits it and reports 8. Any other
count is sent as an explicit `sigmas` list whose length is the count that runs.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

CHECKPOINT_ID = "Qwen/Qwen-Image-2.1-Turbo"
# Length of the checkpoint's saved sample_sigmas grid.
TURBO_STEPS = 8
MODEL_PATH = os.environ.get("IMAGINE_MODEL", "/models/Qwen-Image-2.1-Turbo")
DATA_DIR = Path(os.environ.get("IMAGINE_DATA", "/data"))
IMAGES_DIR = DATA_DIR / "images"
PROGRESS_DIR = DATA_DIR / "progress"
UPLOADS_DIR = DATA_DIR / "uploads"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="imagine-qwen-worker")

_state: dict[str, Any] = {
    "loading": True,
    "ready": False,
    "error": None,
    "model": MODEL_PATH,
    "started_at": time.time(),
    "ready_at": None,
    "compiled": False,
}
_pipe = None
_lock = threading.Lock()
_progress_lock = threading.Lock()
_progress: dict[str, dict[str, Any]] = {}


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    negative_prompt: Optional[str] = ""
    width: int = Field(1024, ge=256, le=2048)
    height: int = Field(1024, ge=256, le=2048)
    steps: int = Field(TURBO_STEPS, ge=1, le=100)
    seed: Optional[int] = None
    true_cfg_scale: float = Field(1.0, ge=1.0, le=20.0)
    id: Optional[str] = None
    # Relative to /data, e.g. "uploads/abc.png" or "images/xyz.png"
    image_path: Optional[str] = None
    rgba: bool = False
    # Edit intensity 0.15–1.0 (prompt-mapped; pipeline has no strength arg)
    strength: float = Field(0.65, ge=0.0, le=1.0)
    # A negative at CFG 1 does not guide, so the worker raises CFG when this
    # is set. The portal turns it off for a plain Turbo still (automatic
    # framing text only) and leaves it on when the user, a style, or spicy
    # mode actually supplied a negative. Direct API clients keep the bump.
    apply_negative_cfg: bool = True


class CancelledError(Exception):
    pass


def _set_progress(job_id: str, *, step: int, steps: int, status: str) -> None:
    pct = 100 if status in ("done", "error", "cancelled") else min(
        99, int(round(100.0 * step / max(steps, 1)))
    )
    if status == "running" and step <= 0:
        pct = 0
    data = {
        "id": job_id,
        "step": step,
        "steps": steps,
        "pct": pct,
        "status": status,
        "updated_at": time.time(),
    }
    with _progress_lock:
        _progress[job_id] = data
    try:
        (PROGRESS_DIR / f"{job_id}.json").write_text(json.dumps(data))
    except Exception as e:
        print(f"[worker] progress write failed: {e}", flush=True)


def _cancel_path(job_id: str) -> Path:
    return PROGRESS_DIR / f"{job_id}.cancel"


def _is_cancelled(job_id: str) -> bool:
    return _cancel_path(job_id).exists()


def _strength_phrase(strength: float) -> str:
    if strength < 0.35:
        return "subtle edit, keep composition and identity, small change only"
    if strength < 0.55:
        return "moderate edit, keep main subject recognizable"
    if strength < 0.8:
        return "strong edit, reshape scene freely while keeping subject"
    return "full redesign from the reference, bold transformation"


def checkpoint_label(path: Optional[str] = None) -> str:
    """Health name for the weights directory. Turbo paths use the HF id."""
    text = MODEL_PATH if path is None else path
    if "Qwen-Image-2.1-Turbo" in str(text).replace("\\", "/"):
        return CHECKPOINT_ID
    return str(text)


_state["model"] = checkpoint_label()


def explicit_sigmas(steps: int) -> list[float]:
    """Inclusive linspace the pipeline uses when no saved grid applies.

    Matches QwenImage21Pipeline: np.linspace(1.0, 1/n, n). The terminal sigma
    is not included; the scheduler appends it.
    """
    n = int(steps)
    if n < 1:
        raise ValueError("steps must be >= 1")
    if n == 1:
        return [1.0]
    stop = 1.0 / n
    span = stop - 1.0
    return [1.0 + span * i / (n - 1) for i in range(n)]


def schedule_for_steps(steps: int) -> tuple[dict[str, Any], int]:
    """Pipeline schedule kwargs, and how many denoising steps that schedule runs.

    The default (8) omits both `sigmas` and `num_inference_steps` so the
    checkpoint's saved grid runs. `num_inference_steps` alone does not override
    that grid. Any other count is an explicit sigma list of that length.
    """
    n = int(steps)
    if n == TURBO_STEPS:
        return {}, TURBO_STEPS
    sigmas = explicit_sigmas(n)
    return {"sigmas": sigmas}, len(sigmas)


def running_steps(kwargs: dict[str, Any]) -> int:
    """Steps that will actually denoise, given the kwargs we hand the pipeline.

    An explicit `sigmas` list wins. Otherwise the saved 8-step grid runs.
    A bare `num_inference_steps` is not an override.
    """
    sigmas = kwargs.get("sigmas")
    if isinstance(sigmas, (list, tuple)) and len(sigmas) > 0:
        return len(list(sigmas))
    return TURBO_STEPS


class StillCall:
    """One pipeline call, built with no weights loaded."""

    def __init__(
        self,
        *,
        kwargs: dict[str, Any],
        steps: int,
        rgba: bool,
        edited: bool,
    ) -> None:
        self.kwargs = kwargs
        self.steps = steps
        self.rgba = rgba
        self.edited = edited

    @property
    def output_mode(self) -> str:
        return "RGBA" if self.rgba else "RGB"


def _open_edit_image(req: GenerateRequest, width: int, height: int):
    from PIL import Image

    rel = (req.image_path or "").lstrip("/")
    if ".." in rel or rel.startswith("/"):
        raise HTTPException(400, "bad image_path")
    src = DATA_DIR / rel
    if not src.exists() or not src.is_file():
        raise HTTPException(404, f"image not found: {rel}")
    pil_image = Image.open(src).convert("RGBA" if req.rgba else "RGB")
    if not req.width or not req.height:
        width = max(256, (pil_image.width // 16) * 16)
        height = max(256, (pil_image.height // 16) * 16)
    else:
        pil_image = pil_image.resize((width, height), Image.Resampling.LANCZOS)
    return pil_image, width, height


def prepare_still_call(
    req: GenerateRequest,
    *,
    generator: Any = None,
    on_step_end: Any = None,
) -> StillCall:
    """Map a still request onto QwenImage21Pipeline kwargs. Does not load weights."""
    width = max(256, (int(req.width) // 16) * 16)
    height = max(256, (int(req.height) // 16) * 16)
    prompt = req.prompt
    if req.rgba:
        prefix = (
            "This is an RGBA image with transparency. "
            "The image has alpha channel and the background is transparent. "
        )
        if "rgba" not in prompt.lower() and "transparent" not in prompt.lower():
            prompt = prefix + prompt

    pil_image = None
    if req.image_path:
        pil_image, width, height = _open_edit_image(req, width, height)
        prompt = f"{prompt}. {_strength_phrase(float(req.strength))}"

    schedule, _schedule_steps = schedule_for_steps(int(req.steps))
    cfg = float(req.true_cfg_scale)
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "true_cfg_scale": cfg,
        "use_kv_cache": True,
    }
    kwargs.update(schedule)
    if generator is not None:
        kwargs["generator"] = generator
    if pil_image is not None:
        kwargs["image"] = pil_image
    if req.negative_prompt:
        kwargs["negative_prompt"] = req.negative_prompt
        if req.apply_negative_cfg and cfg <= 1.0:
            kwargs["true_cfg_scale"] = 4.0
    if on_step_end is not None:
        kwargs["callback_on_step_end"] = on_step_end
    steps = running_steps(kwargs)
    return StillCall(
        kwargs=kwargs,
        steps=steps,
        rgba=bool(req.rgba),
        edited=pil_image is not None,
    )


def _require_turbo_runtime() -> None:
    """Saved Turbo sigmas need pipeline `sample_sigmas` and transformers>=5.17.0."""
    import inspect

    import transformers
    from diffusers import QwenImage21Pipeline

    params = inspect.signature(QwenImage21Pipeline.__init__).parameters
    if "sample_sigmas" not in params:
        raise RuntimeError(
            "QwenImage21Pipeline has no sample_sigmas; install Diffusers with "
            "pipeline-configured sampling sigmas so Qwen-Image-2.1-Turbo loads "
            "its saved 8-step schedule"
        )
    nums: list[int] = []
    for part in transformers.__version__.split("+", 1)[0].split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        nums.append(int(digits))
    while len(nums) < 3:
        nums.append(0)
    if tuple(nums[:3]) < (5, 17, 0):
        raise RuntimeError(
            f"transformers {transformers.__version__} is below 5.17.0, which "
            "Qwen-Image-2.1-Turbo needs for its text encoder"
        )


def _load_pipeline() -> None:
    global _pipe
    try:
        _require_turbo_runtime()
        import torch
        from diffusers import QwenImage21Pipeline

        print(f"[worker] loading {checkpoint_label()} via QwenImage21Pipeline from {MODEL_PATH}", flush=True)
        t0 = time.time()
        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
        pipe = QwenImage21Pipeline.from_pretrained(
            MODEL_PATH, torch_dtype=dtype, local_files_only=True
        )
        if torch.cuda.is_available():
            pipe = pipe.to("cuda")

        compiled = False
        compile_on = os.environ.get("IMAGINE_TORCH_COMPILE", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "",
        )
        if compile_on and torch.cuda.is_available():
            try:
                target = getattr(pipe, "transformer", None)
                if target is not None:
                    print("[worker] torch.compile transformer…", flush=True)
                    pipe.transformer = torch.compile(target, mode="reduce-overhead")
                    compiled = True
                else:
                    print("[worker] no transformer attr; skip compile", flush=True)
            except Exception as ce:
                print(f"[worker] torch.compile failed (continuing): {ce}", flush=True)
                compiled = False

        _pipe = pipe
        _state["compiled"] = compiled
        _state["loading"] = False
        _state["ready"] = True
        _state["ready_at"] = time.time()
        print(
            f"[worker] ready in {time.time() - t0:.1f}s compiled={compiled}",
            flush=True,
        )
    except Exception as e:
        _state["loading"] = False
        _state["ready"] = False
        _state["error"] = f"{type(e).__name__}: {e}"
        print(f"[worker] load failed: {_state['error']}", flush=True)


@app.on_event("startup")
def on_startup() -> None:
    threading.Thread(target=_load_pipeline, daemon=True, name="load-pipe").start()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "loading": _state["loading"],
        "ready": _state["ready"],
        "error": _state["error"],
        "model": checkpoint_label(),
        "uptime_s": round(time.time() - _state["started_at"], 1),
        "ready_at": _state["ready_at"],
        "compiled": bool(_state.get("compiled")),
    }


@app.get("/progress/{job_id}")
def get_progress(job_id: str) -> dict:
    with _progress_lock:
        data = _progress.get(job_id)
    if data:
        return data
    path = PROGRESS_DIR / f"{job_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    raise HTTPException(404, "no progress")


@app.post("/generate")
def generate(req: GenerateRequest) -> dict:
    if not _state["ready"] or _pipe is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "pipeline_not_ready",
                "loading": _state["loading"],
                "ready": _state["ready"],
                "load_error": _state["error"],
            },
        )

    import torch

    job_id = req.id or uuid.uuid4().hex[:12]
    steps = schedule_for_steps(int(req.steps))[1]
    if _is_cancelled(job_id):
        _set_progress(job_id, step=0, steps=steps, status="cancelled")
        raise HTTPException(409, "cancelled")

    seed = (
        req.seed
        if req.seed is not None
        else int(torch.randint(0, 2**31 - 1, (1,)).item())
    )
    out_path = IMAGES_DIR / f"{job_id}.png"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    generator = torch.Generator(device=device).manual_seed(int(seed))

    def on_step_end(pipe, step, timestep, callback_kwargs):
        if _is_cancelled(job_id):
            raise CancelledError(job_id)
        _set_progress(job_id, step=int(step) + 1, steps=steps, status="running")
        return callback_kwargs

    call = prepare_still_call(req, generator=generator, on_step_end=on_step_end)
    steps = call.steps
    width = int(call.kwargs["width"])
    height = int(call.kwargs["height"])

    t0 = time.time()
    try:
        with _lock:
            if _is_cancelled(job_id):
                _set_progress(job_id, step=0, steps=steps, status="cancelled")
                raise HTTPException(409, "cancelled")
            _set_progress(job_id, step=0, steps=steps, status="running")
            result = _pipe(**call.kwargs)
        image = result.images[0]
        if call.rgba and getattr(image, "mode", None) != call.output_mode:
            image = image.convert(call.output_mode)
        image.save(out_path)
        elapsed = round(time.time() - t0, 2)
        _set_progress(job_id, step=steps, steps=steps, status="done")
        try:
            _cancel_path(job_id).unlink(missing_ok=True)
        except Exception:
            pass
        return {
            "id": job_id,
            "path": str(out_path),
            "width": width,
            "height": height,
            "steps": steps,
            "seed": seed,
            "elapsed_s": elapsed,
            "rgba": call.rgba,
            "edited": call.edited,
            "strength": float(req.strength) if call.edited else None,
        }
    except CancelledError:
        _set_progress(job_id, step=0, steps=steps, status="cancelled")
        raise HTTPException(409, "cancelled")
    except HTTPException:
        raise
    except Exception:
        _set_progress(job_id, step=0, steps=steps, status="error")
        raise
