"""Warm Qwen-Image-2.1 worker — FastAPI inside the GPU Docker container.

Loads QwenImage21Pipeline once at startup. Portal proxies POST /generate here.
Writes per-job progress under /data/progress/{id}.json for the portal to poll.
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

MODEL_PATH = os.environ.get("IMAGINE_MODEL", "/models/Qwen-Image-2.1")
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
    steps: int = Field(28, ge=1, le=100)
    seed: Optional[int] = None
    true_cfg_scale: float = Field(1.0, ge=1.0, le=20.0)
    id: Optional[str] = None
    # Relative to /data, e.g. "uploads/abc.png" or "images/xyz.png"
    image_path: Optional[str] = None
    rgba: bool = False
    # Edit intensity 0.15–1.0 (prompt-mapped; pipeline has no strength arg)
    strength: float = Field(0.65, ge=0.0, le=1.0)


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


def _load_pipeline() -> None:
    global _pipe
    try:
        import torch
        from diffusers import QwenImage21Pipeline

        print(f"[worker] loading QwenImage21Pipeline from {MODEL_PATH}", flush=True)
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
        "model": _state["model"],
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
    if _is_cancelled(job_id):
        _set_progress(job_id, step=0, steps=int(req.steps), status="cancelled")
        raise HTTPException(409, "cancelled")

    seed = (
        req.seed
        if req.seed is not None
        else int(torch.randint(0, 2**31 - 1, (1,)).item())
    )
    out_path = IMAGES_DIR / f"{job_id}.png"
    steps = int(req.steps)

    width = max(256, (req.width // 16) * 16)
    height = max(256, (req.height // 16) * 16)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    generator = torch.Generator(device=device).manual_seed(int(seed))

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
        from PIL import Image

        rel = req.image_path.lstrip("/")
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
        prompt = f"{prompt}. {_strength_phrase(float(req.strength))}"

    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "num_inference_steps": steps,
        "generator": generator,
        "true_cfg_scale": req.true_cfg_scale,
    }
    if pil_image is not None:
        kwargs["image"] = pil_image
    if req.negative_prompt:
        kwargs["negative_prompt"] = req.negative_prompt
        if req.true_cfg_scale <= 1.0:
            kwargs["true_cfg_scale"] = 4.0

    def on_step_end(pipe, step, timestep, callback_kwargs):
        if _is_cancelled(job_id):
            raise CancelledError(job_id)
        _set_progress(job_id, step=int(step) + 1, steps=steps, status="running")
        return callback_kwargs

    kwargs["callback_on_step_end"] = on_step_end

    t0 = time.time()
    try:
        with _lock:
            if _is_cancelled(job_id):
                _set_progress(job_id, step=0, steps=steps, status="cancelled")
                raise HTTPException(409, "cancelled")
            _set_progress(job_id, step=0, steps=steps, status="running")
            result = _pipe(**kwargs)
        image = result.images[0]
        if req.rgba and getattr(image, "mode", None) != "RGBA":
            image = image.convert("RGBA")
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
            "rgba": bool(req.rgba),
            "edited": pil_image is not None,
            "strength": float(req.strength) if pil_image is not None else None,
        }
    except CancelledError:
        _set_progress(job_id, step=0, steps=steps, status="cancelled")
        raise HTTPException(409, "cancelled")
    except HTTPException:
        raise
    except Exception:
        _set_progress(job_id, step=0, steps=steps, status="error")
        raise
