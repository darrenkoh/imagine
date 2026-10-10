"""Imagine portal — public HTTPS UI+API on :7860, proxies to warm worker :7861."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("IMAGINE_DATA", ROOT / "data"))
IMAGES = DATA / "images"
JOBS = DATA / "jobs"
PROGRESS = DATA / "progress"
UPLOADS = DATA / "uploads"
VIDEOS = DATA / "videos"
THUMBS = DATA / "thumbs"
STATIC = ROOT / "static"
IMAGES.mkdir(parents=True, exist_ok=True)
JOBS.mkdir(parents=True, exist_ok=True)
PROGRESS.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(parents=True, exist_ok=True)
VIDEOS.mkdir(parents=True, exist_ok=True)
THUMBS.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = DATA / "prompt_history.json"
# Grid cells are ~200–400 CSS px. 512px JPEG covers a phone retina without
# shipping the full 1280px PNG for every tile.
THUMB_PX = 512
_MEDIA_CACHE = {"Cache-Control": "private, max-age=31536000, immutable"}


def _load_dotenv(path: Path) -> None:
    """Fill missing env vars from a local .env. Never overrides the process env."""
    if not path.exists():
        return
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")

PIN = os.environ.get("IMAGINE_PIN", "haku")
# Nudge the model to keep subjects fully in frame (Qwen often crops heads/limbs).
FRAME_SUFFIX = (
    "full subject visible in frame, head to toe if person, "
    "generous margin from all edges, nothing cut off, "
    "slightly wide shot, complete composition"
)
FRAME_NEGATIVE = (
    "cropped, cut off, out of frame, truncated, "
    "head cut off, feet cut off, tight crop, partial body"
)
# Grok Imagine–like Spicy (+18) bias — adults only
SPICY_SUFFIX = (
    "explicit adult sexual content, NSFW, uncensored, erotic, "
    "sexually explicit, detailed intimate anatomy, sensual, passionate"
)
RGBA_PREFIX = (
    "This is an RGBA image with transparency. "
    "The image has alpha channel and the background is transparent. "
)
SPICY_NEGATIVE = (
    "censored, clothed, modest, safe for work, prudish, "
    "child, children, underage, teen, teenager, minor, loli, young girl, young boy"
)
# Extra negatives when a style needs to overpower conflicting mediums
STYLE_NEGATIVES = {
    "3d": (
        "2d, 2D, flat illustration, anime, cel shading, drawing, sketch, pencil, "
        "graphite, watercolor, ink, comic, manga, painting, matte painting, "
        "concept art plate, paper texture, hand-drawn"
    ),
    "anime": (
        "photorealistic, photo, 3d render, cgi, western cartoon, watercolor, "
        "oil painting, blurry photo, live action"
    ),
    "cinematic": (
        "flat lighting, overexposed, underexposed, amateur snapshot, "
        "cartoon, anime, clipart, low detail"
    ),
    "photorealistic": (
        "anime, cartoon, illustration, painting, drawing, sketch, cgi, "
        "3d render, plastic skin, doll-like"
    ),
    "watercolor": (
        "photorealistic, photo, 3d, cgi, hard digital lines, vector flat, "
        "anime cel, oily photo"
    ),
    "sketch": (
        "photorealistic, full color photo, 3d render, watercolor wash, "
        "oil painting, anime cel"
    ),
}
# Qwen-Image-2.1-Turbo's saved sample_sigmas grid. The worker reports this
# count unless the client asks for a different length via explicit sigmas.
STILL_STEPS = 8
WORKER_URL = os.environ.get("IMAGINE_WORKER_URL", "http://127.0.0.1:7861")
COOKIE = "imagine_pin"
# Local MiniMax H3 via antirez/h3.c (Apple Silicon). Not the cloud API.
H3_RESOLUTIONS = ("fast", "768p")
H3_DURATION_MIN = 1
H3_DURATION_MAX = 15
H3_MAX_PIXELS = 768 * 1344
H3_DEFAULT_MOTION = (
    "The still image comes alive with natural, subtle motion. "
    "Keep the subject, composition, lighting, and any on-screen text unchanged. "
    "Gentle ambient movement only."
)

app = FastAPI(title="Imagine")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class GenerateBody(BaseModel):
    prompt: str = Field(..., min_length=1)
    negative_prompt: Optional[str] = ""
    width: int = 1024
    height: int = 1024
    steps: int = STILL_STEPS
    seed: Optional[int] = None
    style: Optional[str] = None
    spicy: bool = False
    rgba: bool = False
    framing: bool = True  # auto crop-nudge suffix; off for tight crops
    image_id: Optional[str] = None  # uploaded file id under data/uploads
    strength: float = 0.65  # edit intensity 0.15–1.0


class AnimateBody(BaseModel):
    prompt: Optional[str] = ""
    image_id: str = Field(..., min_length=1)
    duration: int = 5
    resolution: str = "fast"


def _check_pin(
    request: Request,
    x_imagine_pin: Optional[str] = Header(default=None, alias="X-Imagine-Pin"),
) -> None:
    # Public: /, /static/*, /trust, /api/status (status is soft-gated below)
    cookie = request.cookies.get(COOKIE)
    provided = x_imagine_pin or cookie or request.query_params.get("pin")
    if provided != PIN:
        raise HTTPException(status_code=401, detail="bad pin")


def _job_path(job_id: str) -> Path:
    return JOBS / f"{job_id}.json"


def _safe_media_id(job_id: str) -> str:
    return "".join(c for c in str(job_id) if c.isalnum())[:24]


_gallery_lock = threading.Lock()
_gallery_cache: list[dict] | None = None
_gallery_token: tuple | None = None
_gallery_gen = 0
_thumb_locks: dict[str, threading.Lock] = {}
_thumb_locks_guard = threading.Lock()


def _dir_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _gallery_token_now() -> tuple:
    return (
        _gallery_gen,
        _dir_mtime_ns(JOBS),
        _dir_mtime_ns(IMAGES),
        _dir_mtime_ns(VIDEOS),
    )


def _bump_gallery() -> None:
    """Drop the in-memory gallery list. The next read rebuilds it from disk."""
    global _gallery_gen, _gallery_cache, _gallery_token
    with _gallery_lock:
        _gallery_gen += 1
        _gallery_cache = None
        _gallery_token = None


def _write_job(job_id: str, data: dict) -> None:
    p = _job_path(job_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)
    _bump_gallery()



def _job_kind(job: dict) -> str:
    return job.get("kind") or "image"


def _queue_stats(job_id: str, job: dict) -> dict:
    """How many active jobs of the same kind are ahead, plus a rough ETA in seconds."""
    my_t = float(job.get("created_at") or job.get("updated_at") or 0)
    kind = _job_kind(job)
    ahead = 0
    # average elapsed from recent done jobs for ETA
    dones: list[float] = []
    for p in JOBS.glob("*.json"):
        try:
            o = json.loads(p.read_text())
        except Exception:
            continue
        if _job_kind(o) != kind:
            continue
        st = o.get("status")
        if st in ("queued", "running") and o.get("id") != job_id:
            ot = float(o.get("created_at") or o.get("updated_at") or 0)
            if ot < my_t or (ot == my_t and str(o.get("id") or "") < job_id):
                ahead += 1
        elif st == "done":
            el = o.get("elapsed_s")
            if isinstance(el, (int, float)) and el > 0:
                dones.append(float(el))
    out: dict = {"queue_ahead": ahead}
    if dones:
        dones.sort()
        # median of last up to 8
        sample = dones[-8:]
        mid = sample[len(sample) // 2]
        out["eta_s"] = round(mid * (ahead + 1), 1)
    return out


def _read_job(job_id: str) -> dict:
    p = JOBS / f"{job_id}.json"
    if not p.exists():
        raise HTTPException(404, "job not found")
    job = json.loads(p.read_text())
    # Video jobs report their own provider status. The image worker's
    # progress file is unrelated and must not flip them back to queued.
    if _job_kind(job) == "video":
        if job.get("status") in ("queued", "running"):
            job.update(_queue_stats(job_id, job))
        elif job.get("status") == "done":
            job["pct"] = 100
        return job
    # Merge live denoising progress from shared volume (worker writes this)
    if job.get("status") in ("queued", "running"):
        pp = PROGRESS / f"{job_id}.json"
        if pp.exists():
            try:
                prog = json.loads(pp.read_text())
                job["step"] = prog.get("step", job.get("step", 0))
                job["steps"] = prog.get("steps", job.get("steps"))
                job["pct"] = prog.get("pct", job.get("pct", 0))
                # Waiting on GPU lock: progress file missing or still at step 0
                # before the worker actually started denoising.
                if job.get("status") == "running" and int(job.get("step") or 0) == 0 and prog.get("status") != "done":
                    job["status"] = "queued"
            except Exception:
                pass
        else:
            job.setdefault("pct", 0)
            job.setdefault("step", 0)
            # No progress file yet → still waiting for the worker to start
            if job.get("status") == "running":
                job["status"] = "queued"
    elif job.get("status") == "done":
        job["pct"] = 100
        job["step"] = job.get("steps") or job.get("step")
    if job.get("status") in ("queued", "running"):
        job.update(_queue_stats(job_id, job))
    return job


def _strip_frame(prompt: str) -> str:
    p = (prompt or "").strip()
    # Remove auto framing suffix if present
    for sep in (f", {FRAME_SUFFIX}", f",{FRAME_SUFFIX}"):
        if sep.lower() in p.lower():
            idx = p.lower().rfind(sep.lower())
            if idx != -1:
                p = p[:idx].rstrip(", ").strip()
    return p


def _read_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_history(items: list[dict]) -> None:
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(HISTORY_FILE)


def _history_add(user_prompt: str, negative_prompt: str = "", *, width: int | None = None, height: int | None = None) -> dict:
    """Prepend a prompt history entry. Dedupes identical prompt text (moves to top)."""
    text = (user_prompt or "").strip()
    if not text:
        return {}
    items = _read_history()
    items = [it for it in items if (it.get("prompt") or "").strip() != text]
    entry = {
        "id": uuid.uuid4().hex[:12],
        "prompt": text,
        "negative_prompt": (negative_prompt or "").strip(),
        "width": width,
        "height": height,
        "created_at": time.time(),
    }
    items.insert(0, entry)
    items = items[:200]
    _write_history(items)
    return entry


def _seed_history_from_jobs() -> None:
    """One-time seed from existing jobs if history file is missing."""
    if HISTORY_FILE.exists():
        return
    items = []
    seen = set()
    for p in sorted(JOBS.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            j = json.loads(p.read_text())
        except Exception:
            continue
        raw = _strip_frame(j.get("user_prompt") or j.get("prompt") or "")
        if not raw or raw.lower() in seen:
            continue
        # skip smoke tests
        if raw.lower().startswith("test framing"):
            continue
        seen.add(raw.lower())
        neg = (j.get("negative_prompt") or "")
        for bad in (FRAME_NEGATIVE,):
            if bad in neg:
                neg = neg.replace(bad, "").strip(", ").strip()
        items.append(
            {
                "id": uuid.uuid4().hex[:12],
                "prompt": raw,
                "negative_prompt": neg,
                "width": j.get("width"),
                "height": j.get("height"),
                "created_at": j.get("created_at") or p.stat().st_mtime,
                "job_id": j.get("id"),
            }
        )
    _write_history(items[:200])




_cpu_prev: tuple[float, float] | None = None  # (idle, total)


def _cpu_percent() -> Optional[float]:
    """Non-blocking CPU % from /proc/stat deltas between calls."""
    global _cpu_prev
    try:
        line = Path("/proc/stat").read_text().splitlines()[0]
        parts = line.split()
        # cpu user nice system idle iowait irq softirq steal ...
        nums = [float(x) for x in parts[1:8]]
        idle = nums[3] + nums[4]  # idle + iowait
        total = sum(nums)
        if _cpu_prev is None:
            _cpu_prev = (idle, total)
            return None
        idle0, total0 = _cpu_prev
        _cpu_prev = (idle, total)
        dt = total - total0
        if dt <= 0:
            return None
        return round(max(0.0, min(100.0, 100.0 * (1.0 - (idle - idle0) / dt))), 1)
    except Exception:
        return None


def _gpu_percent() -> Optional[float]:
    """GPU util % via nvidia-smi (GB10 reports utilization.gpu)."""
    util, _temp = _gpu_stats()
    return util


def _gpu_temp_c() -> Optional[float]:
    """GPU temperature °C via nvidia-smi."""
    _util, temp = _gpu_stats()
    return temp


def _gpu_stats() -> tuple[Optional[float], Optional[float]]:
    """Return (util %, temp °C) from one nvidia-smi call."""
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode != 0:
            return None, None
        line = (r.stdout or "").strip().splitlines()[0].strip()
        parts = [p.strip() for p in line.split(",")]
        util = temp = None
        if parts and parts[0] and "N/A" not in parts[0].upper():
            util = round(float(parts[0]), 1)
        if len(parts) > 1 and parts[1] and "N/A" not in parts[1].upper():
            temp = round(float(parts[1]), 1)
        return util, temp
    except Exception:
        return None, None


def _cpu_temp_c() -> Optional[float]:
    """Best-effort CPU/package temp °C from thermal zones."""
    try:
        temps: list[float] = []
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            try:
                raw = (zone / "temp").read_text().strip()
                t = float(raw) / 1000.0  # millidegree C
                if 0 < t < 150:
                    temps.append(t)
            except Exception:
                continue
        if not temps:
            return None
        return round(max(temps), 1)
    except Exception:
        return None


def _meminfo() -> dict:
    out: dict[str, Any] = {"mem_total_gb": None, "mem_available_gb": None, "mem_free_gb": None}
    try:
        info: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":"):
                info[parts[0][:-1]] = int(parts[1])  # kB
        out["mem_total_gb"] = round(info.get("MemTotal", 0) / 1024 / 1024, 1)
        out["mem_available_gb"] = round(info.get("MemAvailable", 0) / 1024 / 1024, 1)
        out["mem_free_gb"] = round(info.get("MemFree", 0) / 1024 / 1024, 1)
    except Exception as e:
        out["error"] = str(e)
    return out


# (monotonic time, rx bytes, tx bytes) from the previous status sample.
_net_prev: tuple[float, int, int] | None = None


def _physical_iface(name: str) -> bool:
    """Real NICs only, so docker veth and loopback are not counted twice."""
    n = name.strip().rstrip("*")
    return n.startswith(("en", "eth", "wl", "wlan", "bond", "ib"))


def _parse_proc_net_dev(text: str) -> list[tuple[str, int, int]]:
    """(iface, rx bytes, tx bytes) from /proc/net/dev."""
    rows: list[tuple[str, int, int]] = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        cols = rest.split()
        if len(cols) < 9:
            continue
        try:
            rows.append((name.strip(), int(cols[0]), int(cols[8])))
        except ValueError:
            continue
    return rows


def _parse_netstat_ib(text: str) -> list[tuple[str, int, int]]:
    """Link-layer rows from `netstat -ibn` (macOS). Ibytes/Obytes counted from the right."""
    rows: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if "<Link" not in line:
            continue
        cols = line.split()
        if len(cols) < 8:
            continue
        name = cols[0].rstrip("*")
        if name in seen:
            continue
        try:
            rx = int(cols[-5])
            tx = int(cols[-2])
        except ValueError:
            continue
        seen.add(name)
        rows.append((name, rx, tx))
    return rows


def _select_net_rows(rows: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    phys = [row for row in rows if _physical_iface(row[0])]
    if phys:
        return phys
    return [row for row in rows if row[0] not in ("lo", "lo0")]


def _read_net_rows() -> list[tuple[str, int, int]] | None:
    proc = Path("/proc/net/dev")
    try:
        if proc.exists():
            return _parse_proc_net_dev(proc.read_text())
    except Exception:
        return None
    try:
        result = subprocess.run(
            ["netstat", "-ibn"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        return _parse_netstat_ib(result.stdout or "")
    except Exception:
        return None


def _net_bandwidth() -> dict:
    """Receive/transmit bytes per second since the previous /api/status sample.

    The first call returns null rates; the footer fills in on the next poll.
    """
    empty: dict[str, Any] = {"rx_bps": None, "tx_bps": None, "ifaces": []}
    global _net_prev
    try:
        rows = _read_net_rows()
        if not rows:
            return empty
        chosen = _select_net_rows(rows)
        if not chosen:
            return empty
        rx = sum(r for _, r, _ in chosen)
        tx = sum(t for _, _, t in chosen)
        active = [name for name, r, t in chosen if r + t > 0]
        ifaces = active or [name for name, _, _ in chosen]
        now = time.monotonic()
        prev = _net_prev
        _net_prev = (now, rx, tx)
        if prev is None:
            return {"rx_bps": None, "tx_bps": None, "ifaces": ifaces}
        t0, rx0, tx0 = prev
        dt = now - t0
        if dt <= 0:
            return {"rx_bps": None, "tx_bps": None, "ifaces": ifaces}
        drx = rx - rx0 if rx >= rx0 else rx
        dtx = tx - tx0 if tx >= tx0 else tx
        return {
            "rx_bps": round(drx / dt, 1),
            "tx_bps": round(dtx / dt, 1),
            "ifaces": ifaces,
        }
    except Exception:
        return empty


def _docker_running(name: str) -> Optional[bool]:
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return False
        return r.stdout.strip().lower() == "true"
    except Exception:
        return None


async def _worker_health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{WORKER_URL}/health")
            return r.json()
    except Exception as e:
        return {"ok": False, "ready": False, "loading": False, "error": str(e)}


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/trust")
def trust() -> Response:
    """Serve the self-signed cert PEM for iPhone Safari install."""
    from tls import CERT_FILE, ensure_certs

    ensure_certs()
    pem = CERT_FILE.read_text()
    html = f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trust Imagine cert</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#0b0b0f;color:#eee;
padding:24px;padding-bottom:env(safe-area-inset-bottom);line-height:1.5;max-width:40rem;margin:auto}}
a.btn{{display:inline-block;background:#7c5cff;color:#fff;padding:14px 20px;border-radius:12px;
text-decoration:none;font-weight:600;margin:12px 0}}
code,pre{{background:#1a1a24;padding:2px 6px;border-radius:6px;font-size:13px}}
pre{{padding:12px;overflow:auto;white-space:pre-wrap}}
</style></head><body>
<h1>Trust this cert (iPhone)</h1>
<ol>
<li>Tap <a class="btn" href="/trust/cert.pem">Download cert.pem</a></li>
<li>Settings → Profile Downloaded → Install</li>
<li>Settings → General → About → Certificate Trust Settings → enable “Imagine Spark”</li>
<li>Return to <a href="/">Imagine</a></li>
</ol>
<p>Or open over Tailscale HTTP if the Tailscale app is on (no cert needed).</p>
<details><summary>PEM preview</summary><pre>{pem}</pre></details>
</body></html>"""
    return HTMLResponse(html)


@app.get("/trust/cert.pem")
def trust_cert() -> Response:
    from tls import CERT_FILE, ensure_certs

    ensure_certs()
    return Response(
        content=CERT_FILE.read_bytes(),
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="imagine-spark.pem"'},
    )


@app.post("/api/auth")
async def auth(request: Request) -> JSONResponse:
    body = await request.json()
    pin = str(body.get("pin", ""))
    if pin != PIN:
        raise HTTPException(401, "bad pin")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE,
        PIN,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        secure=request.url.scheme == "https",
    )
    return resp


def _image_model_name(model: Optional[str]) -> str:
    text = model or ""
    if "Qwen-Image-2.1" in text:
        return "Qwen Image 2.1"
    if "qwen" in text.lower():
        return "Qwen Image"
    leaf = text.strip("/").split("/")[-1]
    if not leaf:
        return "Qwen Image 2.1"
    return leaf.replace("-", " ").replace("_", " ")


def _footer_model(worker: dict, h3_running: Optional[bool]) -> tuple[str, str]:
    """Pill label for the model that is loaded, or the one still loading."""
    image = _image_model_name((worker or {}).get("model"))
    if (worker or {}).get("loading"):
        return f"Loading Model {image}...", "loading"
    if (worker or {}).get("ready"):
        return image, "ready"
    if h3_running:
        return "MiniMax H3", "ready"
    return "No model", "down"


@app.get("/api/status")
async def status(request: Request) -> dict:
    # Soft gate: allow unauthenticated status but omit pin confirmation
    wh = await _worker_health()
    h3_ok, h3_detail = _h3_ready()
    h3_running = _docker_running("vllm-minimax-h3")
    model_label, model_state = _footer_model(wh, h3_running)
    return {
        "ok": True,
        "time": time.time(),
        "worker": wh,
        "h3_running": h3_running,
        "model_label": model_label,
        "model_state": model_state,
        "h3_video": h3_ok,
        "h3_detail": h3_detail,
        "worker_container": _docker_running("imagine-qwen-worker"),
        "memory": _meminfo(),
        "cpu_percent": _cpu_percent(),
        "gpu_percent": _gpu_percent(),
        "gpu_temp_c": _gpu_temp_c(),
        "cpu_temp_c": _cpu_temp_c(),
        "net": _net_bandwidth(),
        "pin_ok": request.cookies.get(COOKIE) == PIN
        or request.headers.get("X-Imagine-Pin") == PIN,
    }



@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), _: None = Depends(_check_pin)) -> dict:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "expected an image upload")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "image too large (max 25MB)")
    uid = uuid.uuid4().hex[:12]
    # Always store as PNG for the worker
    from io import BytesIO
    from PIL import Image

    try:
        im = Image.open(BytesIO(data))
        im.load()
    except Exception as e:
        raise HTTPException(400, f"invalid image: {e}") from e
    # Keep alpha if present
    dest = UPLOADS / f"{uid}.png"
    im.save(dest, format="PNG")
    return {
        "id": uid,
        "path": f"uploads/{uid}.png",
        "url": f"/api/uploads/{uid}.png",
        "width": im.width,
        "height": im.height,
        "mode": im.mode,
    }


@app.get("/api/uploads/{name}")
def api_upload_get(name: str, _: None = Depends(_check_pin)):
    if not name.endswith(".png") or "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    path = UPLOADS / name
    if not path.exists():
        raise HTTPException(404, "missing")
    return FileResponse(path, media_type="image/png")


def _safe_upload_id(image_id: str) -> str:
    return "".join(c for c in (image_id or "") if c.isalnum())[:24]


def _h3_paths() -> tuple[Path, Path]:
    binary = Path(os.environ.get("H3_BIN", str(Path.home() / "src/h3.c/h3"))).expanduser()
    model = Path(os.environ.get("H3_MODEL_DIR", str(Path.home() / "src/h3.c/MiniMax-H3"))).expanduser()
    return binary, model


def _h3_ready() -> tuple[bool, str]:
    """Local h3.c binary plus MiniMax-H3 weights. No cloud key is involved."""
    binary, model = _h3_paths()
    if not binary.is_file():
        return False, f"Local h3 binary not found at {binary}. Build it in ~/src/h3.c."
    if not os.access(binary, os.X_OK):
        return False, f"Local h3 binary is not executable: {binary}"
    if not model.is_dir():
        return False, f"MiniMax-H3 weights not found at {model}."
    if not any(model.rglob("*.safetensors")):
        return False, f"MiniMax-H3 weights are incomplete in {model}."
    return True, ""


def _ram_gb() -> Optional[float]:
    """Physical RAM in GiB. Read once, without forking, so the event loop never blocks on sysctl."""
    try:
        if os.uname().sysname == "Darwin":
            import ctypes
            import ctypes.util

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            size = ctypes.c_uint64()
            length = ctypes.c_size_t(ctypes.sizeof(size))
            if libc.sysctlbyname(b"hw.memsize", ctypes.byref(size), ctypes.byref(length), None, 0) != 0:
                return None
            return size.value / (1024 ** 3)
        info = Path("/proc/meminfo").read_text()
        for line in info.splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / (1024 ** 2)
    except Exception:
        return None
    return None


_RAM_GB = _ram_gb()


def _h3_layers() -> int:
    raw = os.environ.get("H3_LAYERS", "").strip()
    if raw:
        return max(1, int(raw))
    # 32 GB machines need the lighter stack from the local quick-test preset.
    if _RAM_GB is not None and _RAM_GB <= 40:
        return 45
    return 50


def _h3_steps() -> int:
    raw = os.environ.get("H3_STEPS", "").strip()
    if raw:
        return max(2, int(raw))
    return 20


def _h3_ssd() -> bool:
    raw = os.environ.get("H3_SSD_STREAMING")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() not in ("0", "false", "no")
    return _RAM_GB is None or _RAM_GB <= 40


def _h3_canvas(src_w: int, src_h: int, mode: str) -> tuple[int, int]:
    """Snap a still to a legal h3 canvas: multiples of 32, within the pixel cap.

    Fast keeps the long side at 512. 768p uses the released 768×1344 budget.
    The nearest 32-grid size keeps the screenshot's aspect ratio.
    """
    if src_w < 1 or src_h < 1:
        raise HTTPException(400, "image is empty")
    long_cap = 512 if mode == "fast" else 1344
    max_pixels = 512 * 512 if mode == "fast" else H3_MAX_PIXELS
    aspect = src_w / src_h
    if src_w >= src_h:
        ideal_w = float(long_cap)
        ideal_h = ideal_w / aspect
    else:
        ideal_h = float(long_cap)
        ideal_w = ideal_h * aspect
    if ideal_w * ideal_h > max_pixels:
        scale = math.sqrt(max_pixels / (ideal_w * ideal_h))
        ideal_w *= scale
        ideal_h *= scale
    base_w = max(32, int(ideal_w) // 32 * 32)
    base_h = max(32, int(ideal_h) // 32 * 32)
    best: tuple[tuple[float, int], int, int] | None = None
    for w in range(max(32, base_w - 64), base_w + 97, 32):
        for h in range(max(32, base_h - 64), base_h + 97, 32):
            if w * h > max_pixels or max(w, h) > long_cap:
                continue
            err = abs((w / h) - aspect)
            key = (err, -(w * h))
            if best is None or key < best[0]:
                best = (key, w, h)
    if best is None:
        side = 512 if mode == "fast" else 768
        return side, side
    return best[1], best[2]


def _open_rgb(path: Path) -> Any:
    from PIL import Image

    im = Image.open(path)
    im.load()
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return im.convert("RGB")


def _fit_h3_still(path: Path, mode: str) -> Any:
    """Letterbox the still onto the h3 canvas so the first frame is not stretched."""
    from PIL import Image

    im = _open_rgb(path)
    canvas_w, canvas_h = _h3_canvas(im.width, im.height, mode)
    scale = min(canvas_w / im.width, canvas_h / im.height)
    resized = im.resize(
        (
            min(canvas_w, max(1, int(round(im.width * scale)))),
            min(canvas_h, max(1, int(round(im.height * scale)))),
        ),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    canvas.paste(resized, ((canvas_w - resized.width) // 2, (canvas_h - resized.height) // 2))
    return canvas


def _save_poster(im: Any, dest: Path) -> None:
    from PIL import Image

    thumb = im.copy()
    thumb.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    thumb.save(dest, format="PNG", optimize=True)


def _h3_command(frame: Path, dest: Path, motion: str, duration: int, width: int, height: int) -> list[str]:
    binary, model = _h3_paths()
    cmd = [
        str(binary),
        "-d", str(model),
        "-p", motion,
        "--width", str(width),
        "--height", str(height),
        "--seconds", str(int(duration)),
        "--steps", str(_h3_steps()),
        "--layers", str(_h3_layers()),
        "--reuse", "1",
        "--first-frame", str(frame),
        "-o", str(dest),
    ]
    if _h3_ssd():
        cmd.append("--ssd-streaming")
    return cmd


def _h3_failure_message(lines: list[str], code: int | None) -> str:
    errors = []
    for line in lines:
        text = line.strip()
        if text.lower().startswith("h3:") and "wrote " not in text.lower():
            errors.append(text)
    if errors:
        return errors[-1][:500]
    tail = " ".join(line.strip() for line in lines[-8:] if line.strip())
    return (tail or f"local h3 exited {code}")[:500]


_H3_PROGRESS_RE = re.compile(r"([A-Za-z][\w .+-]{0,40}?)\s+(\d+)\s*/\s*(\d+)\s*$")


async def _read_h3_stream(stream: Any, on_text) -> None:
    buf = ""
    while True:
        chunk = await stream.read(512)
        if not chunk:
            if buf.strip():
                on_text(buf)
            return
        buf += chunk.decode("utf-8", "replace")
        parts = re.split(r"[\r\n]", buf)
        buf = parts[-1]
        for part in parts[:-1]:
            if part.strip():
                on_text(part)


def _stop_h3(proc: asyncio.subprocess.Process, sig: int = signal.SIGTERM) -> None:
    if proc.returncode is not None:
        return
    try:
        # Only signal the child's session. killpg on our own group would stop the portal.
        if os.getpgid(proc.pid) != os.getpgrp():
            os.killpg(proc.pid, sig)
            return
    except ProcessLookupError:
        return
    except Exception:
        pass
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        return


async def _finish_pump(pump: asyncio.Future) -> None:
    """Drain h3's stdout/stderr task and always retrieve its result."""
    if not pump.done():
        try:
            await asyncio.wait_for(asyncio.shield(pump), timeout=1)
            return
        except asyncio.TimeoutError:
            pump.cancel()
        except Exception:
            return
    try:
        await pump
    except (asyncio.CancelledError, Exception):
        pass


async def _halt_h3(proc: asyncio.subprocess.Process, pump: asyncio.Future) -> None:
    _stop_h3(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        _stop_h3(proc, signal.SIGKILL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None and not stream.at_eof():
                stream.feed_eof()
        except Exception:
            pass
    await _finish_pump(pump)



@app.post("/api/animate")
async def api_animate(body: AnimateBody, _: None = Depends(_check_pin)) -> dict:
    ready, detail = _h3_ready()
    if not ready:
        raise HTTPException(400, detail)
    duration = int(body.duration)
    if duration < H3_DURATION_MIN or duration > H3_DURATION_MAX:
        raise HTTPException(400, f"duration must be {H3_DURATION_MIN}–{H3_DURATION_MAX} seconds")
    resolution = (body.resolution or "fast").strip().lower()
    if resolution not in H3_RESOLUTIONS:
        raise HTTPException(400, "resolution must be fast or 768p")
    safe = _safe_upload_id(body.image_id)
    src = UPLOADS / f"{safe}.png"
    if not safe or not src.exists():
        raise HTTPException(400, "upload not found — drop the image again")

    user_prompt = (body.prompt or "").strip()
    motion = user_prompt or H3_DEFAULT_MOTION
    if len(motion) > 7000:
        raise HTTPException(400, "motion prompt is too long (max 7000 characters)")

    if user_prompt:
        try:
            _history_add(user_prompt, "", width=None, height=None)
        except Exception:
            pass

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "kind": "video",
        "model": "h3-local",
        "status": "queued",
        "provider_status": "queued",
        "user_prompt": user_prompt,
        "prompt": motion,
        "image_id": safe,
        "duration": duration,
        "resolution": resolution,
        "created_at": time.time(),
        "updated_at": time.time(),
        "error": None,
        "elapsed_s": None,
        "image": None,
        "video": None,
        "pct": 0,
        "step": 0,
    }
    _write_job(job_id, job)
    asyncio.create_task(_run_animate(job_id, src, motion, duration, resolution))
    return job


async def _run_animate(
    job_id: str,
    src: Path,
    motion: str,
    duration: int,
    resolution: str,
) -> None:
    t0 = time.time()
    job = json.loads(_job_path(job_id).read_text())
    frame_path = PROGRESS / f"{job_id}-first.png"
    dest = VIDEOS / f"{job_id}.mp4"

    def _touch(**fields: Any) -> None:
        job.update(fields)
        job["updated_at"] = time.time()
        job["elapsed_s"] = round(time.time() - t0, 2)
        _write_job(job_id, job)

    def _cancelled() -> bool:
        if (PROGRESS / f"{job_id}.cancel").exists():
            return True
        try:
            current = json.loads(_job_path(job_id).read_text())
        except Exception:
            return False
        return current.get("status") == "cancelled"

    proc: asyncio.subprocess.Process | None = None
    pump: asyncio.Future | None = None
    try:
        if _cancelled():
            _touch(status="cancelled", error=None, pct=0)
            return
        prepared = _fit_h3_still(src, resolution)
        prepared.save(frame_path, format="PNG")
        _touch(
            width=prepared.width,
            height=prepared.height,
            status="running",
            provider_status="starting",
            pct=1,
            layers=_h3_layers(),
            steps=_h3_steps(),
        )
        cmd = _h3_command(frame_path, dest, motion, duration, prepared.width, prepared.height)
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            ),
            timeout=30,
        )
        notes: list[str] = []

        def on_text(text: str) -> None:
            line = text.strip()
            if not line:
                return
            notes.append(line)
            del notes[:-40]
            match = _H3_PROGRESS_RE.search(line)
            if not match:
                return
            phase = match.group(1).strip()
            done = int(match.group(2))
            total = max(1, int(match.group(3)))
            _touch(
                status="running",
                provider_status=f"{phase} {done}/{total}",
                pct=min(99, int(100 * done / total)),
                step=done,
                steps=total,
            )

        assert proc.stdout is not None and proc.stderr is not None
        # gather() is already a Future. create_task() rejects that on Python 3.11+.
        pump = asyncio.gather(
            _read_h3_stream(proc.stderr, on_text),
            _read_h3_stream(proc.stdout, on_text),
        )
        deadline = time.time() + 6 * 3600
        while True:
            if _cancelled():
                await _halt_h3(proc, pump)
                pump = None
                _touch(status="cancelled", error=None, pct=0)
                return
            if time.time() > deadline:
                await _halt_h3(proc, pump)
                pump = None
                raise RuntimeError("local h3 timed out after 6 hours")
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.4)
                break
            except asyncio.TimeoutError:
                continue
        await pump
        pump = None
        if _cancelled():
            _touch(status="cancelled", error=None, pct=0)
            return
        if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            raise RuntimeError(_h3_failure_message(notes, proc.returncode))
        poster = IMAGES / f"{job_id}.png"
        _save_poster(prepared, poster)
        try:
            _write_grid_thumb(poster, THUMBS / _thumb_name(job_id, False), False)
        except Exception:
            pass
        _touch(
            status="done",
            provider_status="done",
            error=None,
            pct=100,
            image=f"/api/images/{job_id}.png",
            video=f"/api/videos/{job_id}.mp4",
            elapsed_s=round(time.time() - t0, 2),
        )
    except HTTPException as e:
        _touch(status="error", error=str(e.detail), pct=0)
    except Exception as e:
        _touch(status="error", error=str(e), pct=0)
    finally:
        if proc is not None and proc.returncode is None:
            _stop_h3(proc)
        if pump is not None:
            await _finish_pump(pump)
        try:
            frame_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            (PROGRESS / f"{job_id}.cancel").unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/api/videos/{name}")
def api_video(name: str, _: None = Depends(_check_pin)) -> FileResponse:
    if not name.endswith(".mp4") or "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    path = VIDEOS / name
    if not path.exists():
        raise HTTPException(404, "missing")
    return FileResponse(path, media_type="video/mp4")


def style_still(body: GenerateBody) -> dict:
    """Prompt, negatives, and sampling fields for one still. No I/O."""
    user_prompt = body.prompt.strip()
    prompt = user_prompt
    style = (body.style or "").strip()
    if style:
        # Lead with style so it beats medium words inside the user prompt
        prompt = f"{style}. {prompt}"
    spicy = bool(getattr(body, "spicy", False))
    rgba = bool(getattr(body, "rgba", False))
    if spicy and SPICY_SUFFIX.lower() not in prompt.lower():
        prompt = f"{prompt}, {SPICY_SUFFIX}"
    if rgba and "transparent" not in prompt.lower() and "rgba" not in prompt.lower():
        prompt = RGBA_PREFIX + prompt
    framing = bool(getattr(body, "framing", True))
    user_negative = (body.negative_prompt or "").strip()
    neg = user_negative
    # Keep subjects fully framed when Framing is on (default)
    if framing:
        if FRAME_SUFFIX.lower() not in prompt.lower():
            prompt = f"{prompt}, {FRAME_SUFFIX}"
        if FRAME_NEGATIVE not in neg:
            neg = f"{neg}, {FRAME_NEGATIVE}".strip(", ").strip()
    if spicy and SPICY_NEGATIVE not in neg:
        neg = f"{neg}, {SPICY_NEGATIVE}".strip(", ").strip()
    if style:
        style_key = style.lower()
        for key, neg_extra in STYLE_NEGATIVES.items():
            if key in style_key and neg_extra not in neg:
                neg = f"{neg}, {neg_extra}".strip(", ").strip()
    # Turbo's saved schedule is CFG 1. Framing text alone must not lift that.
    # A negative the user typed, a style's negatives, or spicy mode still bump
    # CFG so those negatives actually guide.
    apply_negative_cfg = bool(user_negative) or spicy or bool(style)
    return {
        "user_prompt": user_prompt,
        "prompt": prompt,
        "negative_prompt": neg,
        "spicy": spicy,
        "rgba": rgba,
        "framing": framing,
        "image_id": getattr(body, "image_id", None),
        "strength": float(getattr(body, "strength", 0.65) or 0.65),
        "width": body.width,
        "height": body.height,
        "steps": int(body.steps),
        "seed": body.seed,
        "apply_negative_cfg": apply_negative_cfg,
    }


def resolve_edit_path(image_id: Optional[str]) -> Optional[str]:
    if not image_id:
        return None
    safe = "".join(c for c in image_id if c.isalnum())[:24]
    cand = UPLOADS / f"{safe}.png"
    if not cand.exists():
        raise HTTPException(400, "upload not found — re-upload the image")
    return f"uploads/{safe}.png"


def still_worker_payload(body: GenerateBody, job_id: str, image_path: Optional[str] = None) -> dict:
    """JSON the portal posts to the worker. Uses the same styling as /api/generate."""
    styled = style_still(body)
    return {
        "id": job_id,
        "prompt": styled["prompt"],
        "negative_prompt": styled["negative_prompt"],
        "width": styled["width"],
        "height": styled["height"],
        "steps": styled["steps"],
        "seed": styled["seed"],
        "rgba": styled["rgba"],
        "image_path": image_path,
        "strength": styled["strength"],
        "apply_negative_cfg": styled["apply_negative_cfg"],
    }


@app.post("/api/generate")
async def api_generate(body: GenerateBody, _: None = Depends(_check_pin)) -> dict:
    job_id = uuid.uuid4().hex[:12]
    styled = style_still(body)

    # Save clean user prompt to history (not the framing suffix)
    try:
        _history_add(styled["user_prompt"], body.negative_prompt or "", width=body.width, height=body.height)
    except Exception:
        pass

    job = {
        "id": job_id,
        "status": "queued",
        "user_prompt": styled["user_prompt"],
        "prompt": styled["prompt"],
        "negative_prompt": styled["negative_prompt"],
        "spicy": styled["spicy"],
        "rgba": styled["rgba"],
        "framing": styled["framing"],
        "image_id": styled["image_id"],
        "strength": styled["strength"],
        "width": styled["width"],
        "height": styled["height"],
        "steps": styled["steps"],
        "seed": styled["seed"],
        "created_at": time.time(),
        "updated_at": time.time(),
        "error": None,
        "elapsed_s": None,
        "image": None,
        "pct": 0,
        "step": 0,
    }
    _write_job(job_id, job)

    image_path = resolve_edit_path(styled["image_id"])
    payload = still_worker_payload(body, job_id, image_path)

    job["status"] = "running"
    job["updated_at"] = time.time()
    _write_job(job_id, job)

    asyncio.create_task(_run_generate(job_id, payload, body.seed))
    return job


async def _run_generate(job_id: str, payload: dict, fallback_seed: Optional[int]) -> None:
    job = _read_job(job_id)
    try:
        # Honour cancel before we take the GPU
        if job.get("status") == "cancelled" or (PROGRESS / f"{job_id}.cancel").exists():
            job["status"] = "cancelled"
            job["updated_at"] = time.time()
            _write_job(job_id, job)
            return
        async with httpx.AsyncClient(timeout=600.0) as client:
            r = await client.post(f"{WORKER_URL}/generate", json=payload)
        if r.status_code != 200:
            detail = r.text
            try:
                detail = r.json()
            except Exception:
                pass
            if r.status_code == 409 or (isinstance(detail, str) and "cancel" in detail.lower()):
                job["status"] = "cancelled"
                job["error"] = None
            else:
                job["status"] = "error"
                job["error"] = detail
            job["pct"] = 0
            job["updated_at"] = time.time()
            _write_job(job_id, job)
            return

        data = r.json()
        src = Path(data["path"])
        dest = IMAGES / f"{job_id}.png"
        if src.resolve() != dest.resolve():
            if src.exists():
                shutil.copy2(src, dest)
        try:
            rgba = bool(job.get("rgba"))
            _write_grid_thumb(dest, THUMBS / _thumb_name(job_id, rgba), rgba)
        except Exception:
            pass
        job.update(
            {
                "status": "done",
                "seed": data.get("seed", fallback_seed),
                "width": data.get("width", payload.get("width")),
                "height": data.get("height", payload.get("height")),
                "elapsed_s": data.get("elapsed_s"),
                "image": f"/api/images/{job_id}.png",
                "pct": 100,
                "step": data.get("steps", payload.get("steps")),
                "steps": data.get("steps", payload.get("steps")),
                "updated_at": time.time(),
                "error": None,
            }
        )
        _write_job(job_id, job)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["updated_at"] = time.time()
        _write_job(job_id, job)


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, _: None = Depends(_check_pin)) -> dict:
    return _read_job(job_id)



@app.get("/api/prompt-history")
def api_prompt_history(_: None = Depends(_check_pin)) -> dict:
    try:
        _seed_history_from_jobs()
    except Exception:
        pass
    return {"items": _read_history()}


@app.delete("/api/prompt-history/{item_id}")
def api_prompt_history_delete(item_id: str, _: None = Depends(_check_pin)) -> dict:
    items = _read_history()
    new_items = [it for it in items if it.get("id") != item_id]
    if len(new_items) == len(items):
        raise HTTPException(404, "not found")
    _write_history(new_items)
    return {"ok": True, "id": item_id}


@app.delete("/api/prompt-history")
def api_prompt_history_clear(_: None = Depends(_check_pin)) -> dict:
    _write_history([])
    return {"ok": True, "cleared": True}



@app.delete("/api/clear-all")
def api_clear_all(_: None = Depends(_check_pin)) -> dict:
    """Wipe prompt history, gallery images, jobs, and progress files."""
    cleared = {"history": 0, "images": 0, "jobs": 0, "progress": 0}
    # History
    n_hist = len(_read_history())
    _write_history([])
    cleared["history"] = n_hist
    # Images
    for p in IMAGES.glob("*.png"):
        try:
            p.unlink()
            cleared["images"] += 1
        except Exception:
            pass
    cleared["videos"] = 0
    for p in VIDEOS.glob("*.mp4"):
        try:
            p.unlink()
            cleared["videos"] += 1
        except Exception:
            pass
    # Jobs
    for p in JOBS.glob("*.json"):
        try:
            p.unlink()
            cleared["jobs"] += 1
        except Exception:
            pass
    # Progress
    for p in PROGRESS.glob("*.json"):
        try:
            p.unlink()
            cleared["progress"] += 1
        except Exception:
            pass
    cleared["thumbs"] = 0
    for p in THUMBS.glob("*"):
        if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        try:
            p.unlink()
            cleared["thumbs"] += 1
        except Exception:
            pass
    _bump_gallery()
    return {"ok": True, "cleared": cleared}


_NSFW_RE = re.compile(
    r"\b("
    r"nsfw|nude|naked|explicit|erotic|porn|xxx|uncensored|"
    r"sex|sexual|penetrat|intercourse|orgasm|moaning|"
    r"penis|vagina|pussy|cock|dick|cum|semen|blowjob|handjob|"
    r"genital|nipple|areola|breast(?:s)?\b.*(?:bare|exposed|naked)|"
    r"spread(?:ing)?\s+(?:her\s+)?legs|legs?\s+are\s+spread|"
    r"pubic|labia|clitoris|erection|masturbat|"
    r"spicy|hentai|ahegao"
    r")\b",
    re.I,
)


def _is_nsfw(job: dict) -> bool:
    if job.get("spicy"):
        return True
    text = " ".join(
        str(job.get(k) or "")
        for k in ("user_prompt", "prompt", "negative_prompt")
    )
    return bool(_NSFW_RE.search(text))


def _thumb_name(job_id: str, rgba: bool) -> str:
    return f"{job_id}.png" if rgba else f"{job_id}.jpg"


def _thumb_lock(name: str) -> threading.Lock:
    with _thumb_locks_guard:
        lock = _thumb_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _thumb_locks[name] = lock
        return lock


def _write_grid_thumb(src: Path, dest: Path, rgba: bool) -> None:
    """Write a small grid thumbnail. Safe to call from several requests at once."""
    from PIL import Image

    lock = _thumb_lock(dest.name)
    with lock:
        if dest.exists() and dest.stat().st_size > 0:
            return
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            with Image.open(src) as im:
                im.load()
                im.thumbnail((THUMB_PX, THUMB_PX), Image.Resampling.BILINEAR)
                if rgba:
                    frame = im.convert("RGBA")
                elif im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                    rgba_im = im.convert("RGBA")
                    frame = Image.new("RGB", rgba_im.size, (0, 0, 0))
                    frame.paste(rgba_im, mask=rgba_im.split()[-1])
                else:
                    frame = im.convert("RGB")
                dest.parent.mkdir(parents=True, exist_ok=True)
                if rgba:
                    frame.save(tmp, format="PNG", compress_level=3)
                else:
                    frame.save(tmp, format="JPEG", quality=76)
            tmp.replace(dest)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise


def _delete_gallery_id(job_id: str) -> str:
    safe = _safe_media_id(job_id)
    if not safe:
        return ""
    for path in (
        IMAGES / f"{safe}.png",
        VIDEOS / f"{safe}.mp4",
        _job_path(safe),
        THUMBS / f"{safe}.jpg",
        THUMBS / f"{safe}.png",
    ):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    return safe


def _build_gallery_items() -> list[dict]:
    rows: list[tuple[float, dict]] = []
    for p in JOBS.glob("*.json"):
        try:
            j = json.loads(p.read_text())
        except Exception:
            continue
        if j.get("status") != "done" or not j.get("id"):
            continue
        is_video = _job_kind(j) == "video"
        job_id = str(j["id"])
        poster = IMAGES / f"{job_id}.png"
        video_path = VIDEOS / f"{job_id}.mp4"
        if is_video and not video_path.exists():
            continue
        if not is_video and not poster.exists():
            continue
        rgba = bool(j.get("rgba"))
        user_prompt = _strip_frame(j.get("user_prompt") or j.get("prompt") or "")
        rows.append(
            (
                float(j.get("created_at") or 0),
                {
                    "id": job_id,
                    "kind": "video" if is_video else "image",
                    "prompt": user_prompt or j.get("prompt"),
                    "user_prompt": user_prompt or j.get("prompt"),
                    "negative_prompt": j.get("negative_prompt") or "",
                    "spicy": bool(j.get("spicy")),
                    "nsfw": _is_nsfw(j),
                    "starred": bool(j.get("starred")),
                    "rgba": rgba,
                    "image": f"/api/images/{job_id}.png" if poster.exists() else None,
                    "thumb": f"/api/thumbs/{_thumb_name(job_id, rgba)}" if poster.exists() else None,
                    "video": f"/api/videos/{job_id}.mp4" if is_video else None,
                    "duration": j.get("duration"),
                    "resolution": j.get("resolution"),
                    "model": j.get("model"),
                    "width": j.get("width"),
                    "height": j.get("height"),
                    "seed": j.get("seed"),
                    "steps": j.get("steps"),
                    "created_at": j.get("created_at"),
                    "elapsed_s": j.get("elapsed_s"),
                    "image_id": j.get("image_id"),
                },
            )
        )
    rows.sort(key=lambda row: row[0], reverse=True)
    return [item for _, item in rows]


def _gallery_items() -> list[dict]:
    global _gallery_cache, _gallery_token
    token = _gallery_token_now()
    with _gallery_lock:
        if _gallery_cache is not None and _gallery_token == token:
            return _gallery_cache
    items = _build_gallery_items()
    with _gallery_lock:
        if _gallery_token_now() == token:
            _gallery_cache = items
            _gallery_token = token
    return items


def _warm_gallery_thumbs() -> None:
    """Fill missing grid thumbs one at a time so the first scroll is not a decode storm."""
    try:
        items = _gallery_items()
    except Exception:
        return
    for it in items:
        thumb = str(it.get("thumb") or "")
        name = thumb.rsplit("/", 1)[-1]
        if not name or "/" in name or ".." in name:
            continue
        dest = THUMBS / name
        if dest.exists() and dest.stat().st_size > 0:
            continue
        src = IMAGES / f"{it['id']}.png"
        if not src.exists():
            continue
        try:
            _write_grid_thumb(src, dest, rgba=name.endswith(".png"))
        except Exception:
            continue


@app.get("/api/gallery")
def api_gallery(_: None = Depends(_check_pin)) -> dict:
    return {"items": _gallery_items()}


@app.delete("/api/gallery/{job_id}")
def api_gallery_delete(job_id: str, _: None = Depends(_check_pin)) -> dict:
    safe = _delete_gallery_id(job_id)
    _bump_gallery()
    return {"ok": True, "id": safe or job_id}


@app.get("/api/images/{name}")
def api_image(name: str, _: None = Depends(_check_pin)) -> FileResponse:
    if not name.endswith(".png") or "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    path = IMAGES / name
    if not path.exists():
        raise HTTPException(404, "missing")
    return FileResponse(path, media_type="image/png", headers=_MEDIA_CACHE)


@app.get("/api/thumbs/{name}")
def api_thumb(name: str, _: None = Depends(_check_pin)) -> FileResponse:
    if "/" in name or ".." in name or not (name.endswith(".jpg") or name.endswith(".png")):
        raise HTTPException(400, "bad name")
    job_id = name.rsplit(".", 1)[0]
    if not job_id.isalnum() or len(job_id) > 24:
        raise HTTPException(400, "bad name")
    path = THUMBS / name
    if not path.exists() or path.stat().st_size == 0:
        src = IMAGES / f"{job_id}.png"
        if not src.exists():
            raise HTTPException(404, "missing")
        try:
            _write_grid_thumb(src, path, rgba=name.endswith(".png"))
        except Exception:
            raise HTTPException(404, "missing")
    media = "image/png" if name.endswith(".png") else "image/jpeg"
    return FileResponse(path, media_type=media, headers=_MEDIA_CACHE)


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str, _: None = Depends(_check_pin)) -> dict:
    """Cancel a queued or running job."""
    job = _read_job(job_id)
    st = job.get("status")
    if st in ("done", "error", "cancelled"):
        return {"ok": True, "id": job_id, "status": st, "noop": True}
    # Signal worker (bind-mounted progress dir)
    try:
        (PROGRESS / f"{job_id}.cancel").write_text("1")
    except Exception:
        pass
    job["status"] = "cancelled"
    job["updated_at"] = time.time()
    job["error"] = None
    _write_job(job_id, job)
    return {"ok": True, "id": job_id, "status": "cancelled"}


@app.post("/api/gallery/{job_id}/star")
def api_gallery_star(job_id: str, _: None = Depends(_check_pin)) -> dict:
    job = _read_job(job_id)
    job["starred"] = not bool(job.get("starred"))
    job["updated_at"] = time.time()
    _write_job(job_id, job)
    return {"ok": True, "id": job_id, "starred": bool(job["starred"])}


@app.post("/api/gallery/delete")
async def api_gallery_delete_batch(request: Request, _: None = Depends(_check_pin)) -> dict:
    body = await request.json()
    ids = body.get("ids") or []
    deleted = []
    for job_id in ids:
        safe = _delete_gallery_id(str(job_id))
        if safe:
            deleted.append(safe)
    _bump_gallery()
    return {"ok": True, "deleted": deleted}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}

def _fail_orphaned_video_jobs() -> None:
    """A restart kills the local h3 process. Mark in-flight clips as interrupted."""
    for p in JOBS.glob("*.json"):
        try:
            job = json.loads(p.read_text())
        except Exception:
            continue
        if _job_kind(job) != "video":
            continue
        if job.get("status") not in ("queued", "running"):
            continue
        job["status"] = "error"
        job["error"] = "interrupted — the portal restarted before local h3 finished"
        job["updated_at"] = time.time()
        _write_job(str(job.get("id") or p.stem), job)


try:
    _seed_history_from_jobs()
except Exception:
    pass
try:
    _fail_orphaned_video_jobs()
except Exception:
    pass

if os.environ.get("IMAGINE_THUMB_WARM", "1") != "0":
    threading.Thread(target=_warm_gallery_thumbs, name="imagine-thumbs", daemon=True).start()
