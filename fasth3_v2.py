"""FastH3 V2 sampling contract for one DGX Spark.

Pure request mapping. This module does not download weights and does not
import FastVideo. The checkpoint ladder (eight DiT forwards, nine sigma
points, video/audio shifts 10/3, VSA-H3 sparsity 0.8, 64-token tiles) is the
published ``fastvideo_inference.json`` contract, not the base H3 shift of 12
and not the 4-forward preview.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

MODEL_NAME = "FastH3 V2"
MODEL_ID = "FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer"
VIDEO_LABEL = "FastH3 V2 (8 steps, synced audio)"
LAYER_PROFILE = "h3_dit_vsa"
ATTENTION_BACKEND = "VIDEO_SPARSE_ATTN_H3"
TRANSFORMER_FORWARDS = 8
SIGMA_POINTS = 9
DMD_STEPS = (999, 874, 749, 624, 500, 375, 250, 125)
VIDEO_SHIFT = 10.0
AUDIO_SHIFT = 3.0
VSA_SPARSITY = 0.8
VSA_TILE = 64
FPS = 24
# A 345-frame clip (~14.4 s) can OOM one GB10. Legal counts stay below that.
MAX_FRAMES = 345
GUIDANCE = 1.0
DEFAULT_SEED = 1234
# The one-Spark NVFP4 consumer recipe is text-to-audio-video. It does not
# accept a still as the first frame.
ACCEPTS_FIRST_FRAME = False
DEFAULT_PROMPT = (
    "Natural subtle motion with synchronized ambient sound. "
    "Keep the subject, composition, and lighting steady."
)

# Named canvases. fast/480p is the one-Spark 832×480 recipe; 768p is 1344×768.
_CANVASES = {
    "fast": (832, 480),
    "480p": (832, 480),
    "480": (832, 480),
    "832x480": (832, 480),
    "768p": (1344, 768),
    "768": (1344, 768),
    "1344x768": (1344, 768),
}

_GENERATION_ENV = {
    "FASTVIDEO_MINIMAX_H3_FUSIONS": "all",
    "FASTVIDEO_NVFP4_MM_BACKEND": "cutlass",
    "FASTVIDEO_H3_VAE_TILE_BATCH": "1",
    "FASTVIDEO_VSA_TRITON": "1",
    "FASTVIDEO_VSA_SM100A": "0",
    "FASTVIDEO_FA4": "0",
    "FASTVIDEO_ATTENTION_BACKEND": ATTENTION_BACKEND,
    "FASTVIDEO_STAGE_LOGGING": "1",
}


class FastH3V2Error(ValueError):
    """The request cannot run on the one-Spark FastH3 V2 recipe."""


def legal_frame_count(frames: int) -> bool:
    """H3 frame counts are 17n+5 at 24 fps, shorter than a 345-frame clip."""
    return (
        isinstance(frames, int)
        and not isinstance(frames, bool)
        and frames >= 5
        and frames < MAX_FRAMES
        and (frames - 5) % 17 == 0
    )


def frames_for_seconds(seconds: float) -> int:
    """Nearest legal frame count for a requested duration.

    Five seconds lands on 124 frames and ten seconds on 243. A request whose
    raw 24 fps length is already 345 frames or more is rejected.
    """
    amount = _seconds(seconds)
    target = amount * FPS
    if target >= MAX_FRAMES:
        raise FastH3V2Error(
            f"duration {_format_seconds(amount)}s would run {int(target)} frames; "
            f"{VIDEO_LABEL} on one Spark rejects clips of {MAX_FRAMES} frames or longer"
        )
    best: tuple[float, int] | None = None
    index = 0
    while True:
        frames = 17 * index + 5
        if frames >= MAX_FRAMES:
            break
        err = abs(frames - target)
        if best is None or err < best[0] or (err == best[0] and frames > best[1]):
            best = (err, frames)
        index += 1
    if best is None:
        raise FastH3V2Error(f"no legal frame count below {MAX_FRAMES}")
    return best[1]


def _format_seconds(seconds: float) -> str:
    if float(seconds).is_integer():
        return str(int(seconds))
    return str(seconds)


def _seconds(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise FastH3V2Error("duration must be a positive number of seconds")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        try:
            seconds = float(value.strip())
        except ValueError as e:
            raise FastH3V2Error("duration must be a positive number of seconds") from e
    else:
        raise FastH3V2Error("duration must be a positive number of seconds")
    if seconds <= 0:
        raise FastH3V2Error("duration must be a positive number of seconds")
    return seconds


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise FastH3V2Error("expected an integer frame or pixel count")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise FastH3V2Error("expected an integer frame or pixel count")


def _compact_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _is_v2_name(name: str) -> bool:
    compact = _compact_name(name)
    if any(token in compact for token in ("trim", "preview", "4step", "h3local", "h3c", "minimaxh3")):
        return False
    return "fasth3v2" in compact or ("8stepv2" in compact and "fasth3" in compact)


def _resolution_label(requested: str, width: int, height: int) -> str:
    key = (requested or "").strip().lower()
    if (width, height) == (832, 480) and key in ("", "fast", "480p", "480", "832x480"):
        return key or "fast"
    if (width, height) == (1344, 768) and key in ("", "768p", "768", "1344x768"):
        return key or "768p"
    return f"{width}x{height}"


def canvas_for(
    resolution: Optional[str],
    width: Optional[int],
    height: Optional[int],
) -> tuple[str, int, int]:
    """Snap a request onto a 32-pixel canvas whose short edge is at most 768.

    Named modes ``fast`` and ``480p`` are 832×480. ``768p`` is 1344×768.
    Any other explicit size must already lie on that grid, with the long edge
    at most 1344. Preview, trim, and off-grid sizes are rejected.
    """
    if width is not None or height is not None:
        if width is None or height is None:
            raise FastH3V2Error("width and height must be set together")
        if width % 32 or height % 32 or width < 32 or height < 32:
            raise FastH3V2Error(
                f"{width}×{height} is not a legal FastH3 V2 canvas (32-pixel grid)"
            )
        if min(width, height) > 768:
            raise FastH3V2Error(
                f"{width}×{height} is not a legal FastH3 V2 canvas (short edge must be 768 or less)"
            )
        if max(width, height) > 1344:
            raise FastH3V2Error(
                f"{width}×{height} is not a legal FastH3 V2 canvas (long edge must be 1344 or less)"
            )
        label = _resolution_label(resolution or "", width, height)
        return label, width, height
    key = (resolution or "fast").strip().lower().replace("×", "x").replace(" ", "")
    if not key:
        key = "fast"
    if key not in _CANVASES:
        raise FastH3V2Error("resolution must be fast (832×480) or 768p (1344×768)")
    canvas_w, canvas_h = _CANVASES[key]
    return _resolution_label(key, canvas_w, canvas_h), canvas_w, canvas_h


def normalize_video_request(body: dict) -> dict:
    """Map one portal video request onto the FastH3 V2 contract.

    Caller step counts, shifts, and sparsity are ignored. The 4-forward
    preview, FastH3 Trim, and the base ~49-forward MiniMax H3 schedule cannot
    be selected. A still does not become first-frame conditioning: this
    checkpoint's one-Spark recipe is text-to-audio-video.
    """
    raw_model = body.get("model")
    if raw_model is not None and str(raw_model).strip():
        if not _is_v2_name(str(raw_model)):
            raise FastH3V2Error(
                f"video is {VIDEO_LABEL}; preview, trim, h3-local, and the base "
                "MiniMax H3 schedule cannot be selected"
            )
    try:
        width = _optional_int(body.get("width"))
        height = _optional_int(body.get("height"))
        explicit_frames = _optional_int(body.get("num_frames"))
    except FastH3V2Error:
        raise
    resolution, width, height = canvas_for(body.get("resolution"), width, height)
    seconds = _seconds(body.get("duration", 5))
    if seconds * FPS >= MAX_FRAMES:
        raise FastH3V2Error(
            f"duration {_format_seconds(seconds)}s would run {int(seconds * FPS)} frames; "
            f"{VIDEO_LABEL} on one Spark rejects clips of {MAX_FRAMES} frames or longer"
        )
    if explicit_frames is not None:
        if explicit_frames >= MAX_FRAMES:
            raise FastH3V2Error(
                f"{explicit_frames} frames is not allowed; one Spark rejects "
                f"{MAX_FRAMES} frames or longer"
            )
        if not legal_frame_count(explicit_frames):
            raise FastH3V2Error(
                f"{explicit_frames} frames is not a legal FastH3 count "
                f"(17n+5, shorter than {MAX_FRAMES})"
            )
        frames = explicit_frames
    else:
        frames = frames_for_seconds(seconds)
    if isinstance(seconds, float) and seconds.is_integer():
        duration: int | float = int(seconds)
    else:
        duration = seconds
    seed_raw = body.get("seed", DEFAULT_SEED)
    if seed_raw is None or seed_raw == "":
        seed = DEFAULT_SEED
    else:
        seed_int = _optional_int(seed_raw)
        if seed_int is None:
            seed = DEFAULT_SEED
        else:
            seed = seed_int
    return {
        "model": MODEL_NAME,
        "model_id": MODEL_ID,
        "layer_profile": LAYER_PROFILE,
        "attention_backend": ATTENTION_BACKEND,
        "transformer_forwards": TRANSFORMER_FORWARDS,
        "steps": TRANSFORMER_FORWARDS,
        "sigma_points": SIGMA_POINTS,
        "num_inference_steps": SIGMA_POINTS,
        "dmd_denoising_steps": list(DMD_STEPS),
        "video_scheduler_shift": VIDEO_SHIFT,
        "audio_scheduler_shift": AUDIO_SHIFT,
        "vsa_sparsity": VSA_SPARSITY,
        "vsa_tile_size": VSA_TILE,
        "vsa_kernel": "triton",
        "fa4": False,
        "sm100a": False,
        "vae_tiling": True,
        "synced_audio": True,
        "audio": True,
        "task": "t2av",
        "conditioning": "t2av",
        "first_frame": False,
        "accepts_first_frame": ACCEPTS_FIRST_FRAME,
        "fps": FPS,
        "num_frames": frames,
        "width": width,
        "height": height,
        "resolution": resolution,
        "duration": duration,
        "guidance_scale": GUIDANCE,
        "seed": seed,
        "prompt": str(body.get("prompt") or "").strip(),
    }


def generation_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Subprocess environment for one GB10. Triton VSA, FA4 and sm100a off."""
    env = dict(base if base is not None else os.environ)
    env.update(_GENERATION_ENV)
    return env


def generation_command(binary: Path, config_path: Path) -> list[str]:
    return ["nice", "-n", "19", str(binary), "generate", "--config", str(config_path)]


def render_config(contract: dict, model_path: Path, output_dir: Path) -> str:
    """YAML for ``fastvideo generate``. Matches the one-Spark V2 NVFP4 recipe.

    No still is attached. ``num_inference_steps`` is nine sigma points
    (eight DiT forwards). Sparsity stays 0.8.
    """
    prompt = json.dumps(str(contract.get("prompt") or DEFAULT_PROMPT), ensure_ascii=False)
    model = json.dumps(str(model_path))
    output = json.dumps(str(output_dir))
    seed = int(contract.get("seed") or DEFAULT_SEED)
    height = int(contract["height"])
    width = int(contract["width"])
    frames = int(contract["num_frames"])
    return (
        "generator:\n"
        f"  model_path: {model}\n"
        "  engine:\n"
        "    num_gpus: 1\n"
        "    use_fsdp_inference: false\n"
        "    quantization:\n"
        "      transformer_quant: NVFP4\n"
        f"      layer_profile: {LAYER_PROFILE}\n"
        "    parallelism:\n"
        "      tp_size: 1\n"
        "      sp_size: 1\n"
        "    offload:\n"
        "      dit: false\n"
        "      dit_layerwise: false\n"
        "      text_encoder: false\n"
        "      image_encoder: false\n"
        "      vae: false\n"
        "      pin_cpu_memory: false\n"
        "      lazy_module_load: false\n"
        "    compile:\n"
        "      enabled: false\n"
        "      vae_enabled: false\n"
        "  pipeline:\n"
        "    workload_type: t2v\n"
        "    vae_tiling: true\n"
        "    experimental:\n"
        f"      attention_backend: {ATTENTION_BACKEND}\n"
        f"      VSA_sparsity: {VSA_SPARSITY}\n"
        f"      VSA_tile_size: {VSA_TILE}\n"
        "      h3_sequential_load: false\n"
        "      inference_torch_compile: false\n"
        "      vae_parallel_decode: false\n"
        "      video_decode_backend: h3-vae\n"
        "request:\n"
        f"  prompt: {prompt}\n"
        "  negative_prompt: \"\"\n"
        "  sampling:\n"
        f"    seed: {seed}\n"
        f"    height: {height}\n"
        f"    width: {width}\n"
        f"    num_frames: {frames}\n"
        f"    fps: {FPS}\n"
        f"    num_inference_steps: {SIGMA_POINTS}\n"
        f"    guidance_scale: {GUIDANCE}\n"
        "    batch_cfg: false\n"
        "  output:\n"
        f"    output_path: {output}\n"
        "    save_video: true\n"
        "    return_frames: false\n"
    )


def _find_named(root: Path, name: str) -> Optional[Path]:
    direct = root / name
    if direct.is_file():
        return direct
    for pattern in (f"*/{name}", f"*/*/{name}", f"*/*/*/{name}"):
        found = sorted(path for path in root.glob(pattern) if path.is_file())
        if found:
            return found[0]
    return None


def _find_weights(root: Path) -> Optional[Path]:
    for pattern in ("*.safetensors", "*/*.safetensors", "*/*/*.safetensors", "*/*/*/*.safetensors"):
        found = sorted(path for path in root.glob(pattern) if path.is_file())
        if found:
            return found[0]
    return None


def _numbers_equal(left: Any, right: float) -> bool:
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False


def checkpoint_problem(data: dict) -> Optional[str]:
    """Why this fastvideo_inference.json is not the V2 contract, or None."""
    model_id = str(data.get("model_id") or "")
    folded = model_id.lower()
    if "trim" in folded or "preview" in folded or "4-step" in folded:
        return f"checkpoint {model_id or 'unknown'} is not {MODEL_ID}"
    forwards = data.get("transformer_forwards")
    if forwards != TRANSFORMER_FORWARDS:
        return (
            f"checkpoint transformer_forwards is {forwards}, not {TRANSFORMER_FORWARDS} "
            "(refusing the 4-forward preview and the base schedule)"
        )
    steps = data.get("num_inference_steps")
    if steps != SIGMA_POINTS:
        return f"checkpoint num_inference_steps is {steps}, not {SIGMA_POINTS} sigma points"
    if not _numbers_equal(data.get("video_scheduler_shift"), VIDEO_SHIFT):
        return (
            f"checkpoint video_scheduler_shift is {data.get('video_scheduler_shift')}, "
            f"not {VIDEO_SHIFT:g}"
        )
    if not _numbers_equal(data.get("audio_scheduler_shift"), AUDIO_SHIFT):
        return (
            f"checkpoint audio_scheduler_shift is {data.get('audio_scheduler_shift')}, "
            f"not {AUDIO_SHIFT:g}"
        )
    if "vsa_sparsity" in data and not _numbers_equal(data.get("vsa_sparsity"), VSA_SPARSITY):
        return f"checkpoint vsa_sparsity is {data.get('vsa_sparsity')}, not {VSA_SPARSITY}"
    ladder = data.get("dmd_denoising_steps")
    if ladder is not None and list(ladder) != list(DMD_STEPS):
        return "checkpoint DMD ladder is not the FastH3 V2 eight-step schedule"
    return None


def inspect_runtime(binary: Optional[Path], model_dir: Optional[Path]) -> dict[str, Any]:
    """Is the local FastVideo stack the V2 consumer checkpoint? Does not download.

    ``model_path`` is the directory that holds ``fastvideo_inference.json``
    when the stack is ready. Generation must point there so FastVideo does
    not fetch weights.
    """
    label = VIDEO_LABEL
    if binary is None or not binary.is_file() or not os.access(binary, os.X_OK):
        where = "on PATH" if binary is None else f"at {binary}"
        return {
            "ready": False,
            "detail": (
                f"{label} is not ready: fastvideo CLI was not found {where}. "
                "This check does not download weights."
            ),
            "binary": binary,
            "model_path": None,
        }
    if model_dir is None or not model_dir.is_dir():
        where = model_dir if model_dir is not None else "~/models/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer"
        return {
            "ready": False,
            "detail": (
                f"{label} is not ready: weights were not found at {where}. "
                "This check does not download weights."
            ),
            "binary": binary,
            "model_path": None,
        }
    try:
        contract_path = _find_named(model_dir, "fastvideo_inference.json")
        weights = _find_weights(model_dir)
    except OSError as e:
        return {
            "ready": False,
            "detail": f"{label} is not ready: could not read {model_dir}: {e}",
            "binary": binary,
            "model_path": None,
        }
    if contract_path is None or weights is None:
        return {
            "ready": False,
            "detail": (
                f"{label} is not ready: {model_dir} is missing fastvideo_inference.json "
                "or safetensors. This check does not download weights."
            ),
            "binary": binary,
            "model_path": None,
        }
    try:
        data = json.loads(contract_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {
            "ready": False,
            "detail": f"{label} is not ready: could not read {contract_path}: {e}",
            "binary": binary,
            "model_path": None,
        }
    if not isinstance(data, dict):
        return {
            "ready": False,
            "detail": f"{label} is not ready: {contract_path} is not a JSON object",
            "binary": binary,
            "model_path": None,
        }
    problem = checkpoint_problem(data)
    if problem:
        return {
            "ready": False,
            "detail": f"{label} is not ready: {problem}.",
            "binary": binary,
            "model_path": contract_path.parent,
        }
    return {
        "ready": True,
        "detail": f"{label} is ready.",
        "binary": binary,
        "model_path": contract_path.parent,
    }
