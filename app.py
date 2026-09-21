"""Imagine portal — public HTTPS UI+API on :7860, proxies to warm worker :7861."""
from __future__ import annotations

import asyncio
import json
import re
import os
import shutil
import subprocess
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
STATIC = ROOT / "static"
IMAGES.mkdir(parents=True, exist_ok=True)
JOBS.mkdir(parents=True, exist_ok=True)
PROGRESS.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = DATA / "prompt_history.json"

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
WORKER_URL = os.environ.get("IMAGINE_WORKER_URL", "http://127.0.0.1:7861")
COOKIE = "imagine_pin"

app = FastAPI(title="Imagine")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class GenerateBody(BaseModel):
    prompt: str = Field(..., min_length=1)
    negative_prompt: Optional[str] = ""
    width: int = 1024
    height: int = 1024
    steps: int = 28
    seed: Optional[int] = None
    style: Optional[str] = None
    spicy: bool = False
    rgba: bool = False
    framing: bool = True  # auto crop-nudge suffix; off for tight crops
    image_id: Optional[str] = None  # uploaded file id under data/uploads
    strength: float = 0.65  # edit intensity 0.15–1.0


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


def _write_job(job_id: str, data: dict) -> None:
    p = _job_path(job_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)



def _queue_stats(job_id: str, job: dict) -> dict:
    """How many active jobs are ahead, plus a rough ETA in seconds."""
    my_t = float(job.get("created_at") or job.get("updated_at") or 0)
    ahead = 0
    # average elapsed from recent done jobs for ETA
    dones: list[float] = []
    for p in JOBS.glob("*.json"):
        try:
            o = json.loads(p.read_text())
        except Exception:
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


@app.get("/api/status")
async def status(request: Request) -> dict:
    # Soft gate: allow unauthenticated status but omit pin confirmation
    wh = await _worker_health()
    return {
        "ok": True,
        "time": time.time(),
        "worker": wh,
        "h3_running": _docker_running("vllm-minimax-h3"),
        "worker_container": _docker_running("imagine-qwen-worker"),
        "memory": _meminfo(),
        "cpu_percent": _cpu_percent(),
        "gpu_percent": _gpu_percent(),
        "gpu_temp_c": _gpu_temp_c(),
        "cpu_temp_c": _cpu_temp_c(),
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

@app.post("/api/generate")
async def api_generate(body: GenerateBody, _: None = Depends(_check_pin)) -> dict:
    job_id = uuid.uuid4().hex[:12]
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
    neg = (body.negative_prompt or "").strip()
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

    # Save clean user prompt to history (not the framing suffix)
    try:
        _history_add(user_prompt, body.negative_prompt or "", width=body.width, height=body.height)
    except Exception:
        pass

    job = {
        "id": job_id,
        "status": "queued",
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
        "steps": body.steps,
        "seed": body.seed,
        "created_at": time.time(),
        "updated_at": time.time(),
        "error": None,
        "elapsed_s": None,
        "image": None,
        "pct": 0,
        "step": 0,
    }
    _write_job(job_id, job)

    image_path = None
    image_id = getattr(body, "image_id", None)
    if image_id:
        safe = "".join(c for c in image_id if c.isalnum())[:24]
        cand = UPLOADS / f"{safe}.png"
        if not cand.exists():
            raise HTTPException(400, "upload not found — re-upload the image")
        image_path = f"uploads/{safe}.png"

    payload = {
        "id": job_id,
        "prompt": prompt,
        "negative_prompt": neg,
        "width": body.width,
        "height": body.height,
        "steps": body.steps,
        "seed": body.seed,
        "rgba": rgba,
        "image_path": image_path,
        "strength": float(getattr(body, "strength", 0.65) or 0.65),
    }

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

@app.get("/api/gallery")
def api_gallery(_: None = Depends(_check_pin)) -> dict:
    items = []
    for p in sorted(JOBS.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            j = json.loads(p.read_text())
        except Exception:
            continue
        if j.get("status") == "done" and (IMAGES / f"{j['id']}.png").exists():
            user_prompt = _strip_frame(j.get("user_prompt") or j.get("prompt") or "")
            items.append(
                {
                    "id": j["id"],
                    "prompt": user_prompt or j.get("prompt"),
                    "user_prompt": user_prompt or j.get("prompt"),
                    "negative_prompt": j.get("negative_prompt") or "",
                    "spicy": bool(j.get("spicy")),
                    "nsfw": _is_nsfw(j),
                    "starred": bool(j.get("starred")),
                    "rgba": bool(j.get("rgba")),
                    "image": f"/api/images/{j['id']}.png",
                    "width": j.get("width"),
                    "height": j.get("height"),
                    "seed": j.get("seed"),
                    "steps": j.get("steps"),
                    "created_at": j.get("created_at"),
                    "elapsed_s": j.get("elapsed_s"),
                }
            )
    return {"items": items}


@app.delete("/api/gallery/{job_id}")
def api_gallery_delete(job_id: str, _: None = Depends(_check_pin)) -> dict:
    img = IMAGES / f"{job_id}.png"
    job = _job_path(job_id)
    if img.exists():
        img.unlink()
    if job.exists():
        job.unlink()
    return {"ok": True, "id": job_id}


@app.get("/api/images/{name}")
def api_image(name: str, _: None = Depends(_check_pin)) -> FileResponse:
    if not name.endswith(".png") or "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    path = IMAGES / name
    if not path.exists():
        raise HTTPException(404, "missing")
    return FileResponse(path, media_type="image/png")


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
        safe = "".join(c for c in str(job_id) if c.isalnum())[:24]
        if not safe:
            continue
        img = IMAGES / f"{safe}.png"
        jobp = _job_path(safe)
        if img.exists():
            img.unlink()
        if jobp.exists():
            jobp.unlink()
        deleted.append(safe)
    return {"ok": True, "deleted": deleted}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}

try:
    _seed_history_from_jobs()
except Exception:
    pass
