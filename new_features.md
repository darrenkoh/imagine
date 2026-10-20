# Imagine — UI & Backend Improvement Plan (`new_features.md`)

This document outlines prioritized improvements across the **UI (`static/`)**, **Portal API (`app.py`)**, and **Image Worker (`worker.py`)**. It is structured as an implementation spec that a coding agent can execute incrementally while preserving all constraints in `AGENTS.md`.

---

## Guardrails for the Implementing Agent (Read First)

Before modifying any code, keep these repository invariants intact:
1. **Do not break existing contracts tested by `unittest`**:
   ```bash
   .venv/bin/python -m unittest tests.test_turbo_defaults tests.test_h3_local
   ```
   - `tests/test_turbo_defaults.py` checks specific strings in `index.html`, `app.js`, `README.md`, and `run_worker.sh` (including `value="8"` on `#steps`, `styles.css?v=`, `app.js?v=`, and `self.assertNotIn("galleryPageSize", html)`). Whenever you bump `?v=` in `static/index.html`, update the corresponding assertion in `tests/test_turbo_defaults.py`.
2. **Respect tree boundaries**: Do not add Spark-only features (series, still-batch, cloud MiniMax API, or FastH3 V2) unless explicitly requested. Keep video generation on local `h3.c` with `--first-frame`.
3. **Keep desktop layout ratio**: `.layout` at `min-width: 720px` must remain `grid-template-columns: 1.1fr .9fr`.
4. **Keep Turbo defaults**: Default steps must stay `8` (omitting `sigmas` and `num_inference_steps` at `true_cfg_scale=1.0`, `use_kv_cache=True`), and non-8 step counts must use `explicit_sigmas`.

---

## Part 1: Backend Service & Performance Improvements

### 1.1 Serialize Local H3 Video Execution with an Async Queue Lock
- **Problem**: In `worker.py`, `_lock` serializes still generation so concurrent requests wait their turn. In `app.py` (`api_animate` → `_run_animate`, lines 1043–1233), every `POST /api/animate` immediately spawns an `h3` subprocess via `asyncio.create_task(_run_animate(...))` with no lock. Launching two clips back-to-back runs two `h3.c` processes simultaneously, thrashing unified memory / SSD streaming even though `_queue_stats` already computes `queue_ahead` for video jobs.
- **Solution**:
  - Add a module-level `_h3_lock = asyncio.Lock()` in `app.py`.
  - In `_run_animate`, keep status as `"queued"` (`provider_status="queued"`) while waiting to acquire `async with _h3_lock:`.
  - Check `_cancelled()` both before acquiring `_h3_lock` and immediately after acquiring it (before spawning `h3`).
  - Once acquired, transition status to `"running"` (`provider_status="starting"`) and launch the subprocess.

### 1.2 Non-Blocking Telemetry & macOS Host Metrics in `GET /api/status`
- **Problem**: `GET /api/status` (`app.py`, lines 718–743) is an `async def` route polled every **500ms** by default, but it executes multiple blocking calls on the main event loop:
  - `_gpu_percent()` and `_gpu_temp_c()` each call `_gpu_stats()`, running `nvidia-smi` **twice** per poll (`timeout=2`).
  - `_docker_running()` runs `docker inspect` **twice** per poll (`timeout=5`).
  - `_net_bandwidth()` spawns `netstat -ibn` synchronously on macOS (`timeout=2`).
  - `_h3_ready()` runs `any(model.rglob("*.safetensors"))` on every poll.
  - On macOS (Apple Silicon), `_cpu_percent()`, `_cpu_temp_c()`, and `_meminfo()` only read Linux `/proc/*` and `/sys/*` paths, returning `None` for CPU and memory free in the status pill.
- **Solution**:
  - Call `_gpu_stats()` once per status check and unpack `gpu_percent, gpu_temp_c`.
  - Cache slow system probes (`_docker_running`, `_gpu_stats`, `_h3_ready`) with a short TTL (e.g., `2.0s` for Docker/H3 readiness, `0.8s` for GPU stats) and run subprocess probes off the event loop (`await asyncio.to_thread(...)`).
  - Add a macOS fallback in `_meminfo()` (using `vm_stat` + `_RAM_GB` via `sysctl`) and `_cpu_percent()` (or `ps`/`sysctl` load average fallback) so Apple Silicon hosts display available memory and CPU utilization in the footer pill.

### 1.3 Orphaned Image Job Cleanup & Queue Accuracy on Startup
- **Problem**: `_fail_orphaned_video_jobs()` (`app.py`, lines 1784–1799) marks interrupted `"video"` jobs as `"error"` on startup, but orphaned `"image"` jobs left in `"queued"` or `"running"` state after a portal restart remain stuck forever in `data/jobs/*.json`. Because `_queue_stats()` scans all `JOBS.glob("*.json")` for `status in ("queued", "running")`, stale image jobs permanently inflate `queue_ahead` and `eta_s`.
- **Solution**:
  - Add `_fail_orphaned_image_jobs()` on startup (or unify into `_fail_orphaned_jobs()`) that marks any image job stuck in `"queued"` or `"running"` whose `updated_at` is older than the portal startup time (and has no active worker progress update) as `status="error"`, `error="interrupted — the portal restarted before generation finished"`.
  - In `_queue_stats()` (`app.py`, line 227), sort `dones` by `created_at` (timestamp) before taking `dones[-8:]`, rather than sorting by `elapsed_s` value (`dones.sort()` currently sorts durations numerically before slicing `[-8:]`, which biases the median toward the 8 slowest historical jobs instead of the 8 most recent jobs!).

### 1.4 Zero-Copy Server-Side Source Reuse for Edit & Animate
- **Problem**: When a user clicks **Edit** (`#sheetUseSource`) or **Animate** (`useJobAsAnimateSource`) on an existing gallery still or result, `static/app.js` fetches the full PNG over the network (`/api/images/{id}.png`) into a browser `Blob` and re-uploads it via `POST /api/upload`. Over mobile Wi-Fi or Tailscale, this transfers several megabytes down and back up for a file already sitting in `data/images/{id}.png`.
- **Solution**:
  - Add `POST /api/uploads/from-image/{job_id}` (or allow `resolve_edit_path` and `api_animate` to resolve `IMAGES / f"{safe}.png"` by copying/hardlinking it into `UPLOADS / f"{safe}.png"` on demand when `UPLOADS / f"{safe}.png"` does not exist).
  - Note: Keep `api_animate`'s validation that a missing `image_id` in both `UPLOADS` and `IMAGES` returns HTTP `400` and writes no job file (preserving `tests/test_h3_local.py`).
  - Update `useJobAsAnimateSource(job)` and `sheetUseSource` in `static/app.js` to call the lightweight endpoint (or pass the existing `job.id`) instead of round-tripping the PNG blob through the browser.

### 1.5 Storage Hygiene & Atomic File Locking
- **Problem**:
  - `DELETE /api/clear-all` (`app.py`, lines 1460–1506) deletes `images`, `videos`, `jobs`, `progress`, and `thumbs`, but never cleans up `data/uploads/*.png` or `data/progress/*.cancel`.
  - `_read_history()` / `_write_history()` and per-job updates (`_touch` in `_run_animate` vs `api_job_cancel` / `api_gallery_star`) lack a lock around read-modify-write cycles.
- **Solution**:
  - Include `UPLOADS.glob("*.png")` and `PROGRESS.glob("*.cancel")` in `DELETE /api/clear-all` (and return `cleared["uploads"]`).
  - Guard history read/write with a `_history_lock = threading.Lock()` and per-job JSON mutations with a lock helper.

---

## Part 2: UI & UX Improvements

### 2.1 Wire Up the Prompt History Drawer / Picker
- **Problem**: The backend already implements `GET /api/prompt-history`, `DELETE /api/prompt-history/{item_id}`, and `DELETE /api/prompt-history` (`app.py`, lines 1434–1456), and `static/styles.css` (lines 409–428) already defines `.prompt-history`, `.hitem`, `.hitem-body`, `.hitem-prompt`, `.hitem-meta`, and `.hitem .del`. However, there is no button or container in `static/index.html` or `static/app.js` to view or pick from prompt history.
- **Solution**:
  - Add a subtle **History** button next to the `#promptLabel` header inside `.compose-inner-body` in `static/index.html`.
  - Clicking **History** toggles a collapsible `.prompt-history` list (or sheet/popover) populated from `GET /api/prompt-history`.
  - Clicking an `.hitem-body` fills `#prompt` (and `#negative` if present), snaps the aspect chip if `width`×`height` matches a known aspect, and auto-grows the prompt textarea.
  - Clicking `.hitem .del` calls `DELETE /api/prompt-history/{id}` and removes the row; include a "Clear history" action wired to `DELETE /api/prompt-history`.

### 2.2 Edit Mode Dropzone, Clipboard Paste, Aspect Matching & Result "Edit" Button
- **Problem**:
  - `animatePanel` has a rich `.dropzone` (`#animateDrop`), drag-and-drop, and paste handling, while `editPanel` only has a bare `<input id="editFile" type="file">`.
  - Global `drop` and `paste` listeners in `static/app.js` (lines 2079–2102) always route dropped/pasted images to `useAnimateFile(file)`, even when the user has selected the **Edit** task tab!
  - In `static/app.js` line 1978 (`// Snap aspect to upload if close`), uploading an edit image leaves `aspect` unchanged, which can stretch a landscape source image into a `3:4` portrait canvas unless the user manually changes the aspect chip.
  - `#resultWrap` has Download, Regenerate, and Animate buttons, but no **Edit** button to iterate on the newly generated still.
- **Solution**:
  - Upgrade `#editPanel` in `static/index.html` to use a `.dropzone` (`#editDrop` + `#editBrowse`) matching `#animateDrop`.
  - Extract `useEditFile(file)` in `static/app.js`. In the global `drop` and `paste` handlers, if `task === "edit"`, route the image to `useEditFile(file)` instead of `useAnimateFile(file)` (and update `#dropOverlay` text dynamically based on `task`).
  - When an image is loaded into Edit mode (`up.width`, `up.height`), automatically select the closest aspect chip in `ASPECTS` by aspect ratio (`up.width / up.height`) so edits preserve the source composition by default.
  - Add an **Edit** button (`#editResultBtn`) in `#resultWrap .actions` (hidden when the result is a video) that loads `lastJob` into Edit mode.

### 2.3 Resume Active Job Progress Across Page Reloads
- **Problem**: Generating a video with local `h3.c` or a high-step still takes time. If a user on iPhone Safari switches apps or reloads the page while a job is running, `activeJobId` in `static/app.js` is lost, the progress bar disappears, and the user has no visibility or Cancel button for the running job.
- **Solution**:
  - Store `activeJobId` and its task kind in `sessionStorage` (`imagine_active_job`), and/or include `active_job_id` in `GET /api/status` when a job is currently `"queued"` or `"running"`.
  - On page load (after PIN unlock), if an active job is still `"queued"` or `"running"`, automatically attach the progress loop (`#progressWrap`, `#cancelBtn`, elapsed timer) until it finishes.

### 2.4 Gallery Search Bar & Sheet / Lightbox Prev-Next Navigation
- **Problem**:
  - As the gallery grows across pages, users can filter by `All / Videos / Starred / Spicy / Normal`, but cannot search prompts or seeds by text.
  - Opening a card in `#sheet` or `#lightbox` requires closing the modal to view the next or previous item.
- **Solution**:
  - **Gallery Search**: Add a compact search input (`#gallerySearch`, `type="search"`, `placeholder="Search prompts…"`) in `.gallery-bar`. Filter `filteredGalleryItems()` by case-insensitive substring match against `prompt`, `negative_prompt`, or `seed`, resetting `galleryPage = 1` on input.
  - **Sheet & Lightbox Navigation**:
    - Support `ArrowLeft` / `ArrowRight` keyboard navigation (and horizontal swipe on `#sheetMedia` / `#lightbox`) when `#sheet` or `#lightbox` is open to step through `filteredGalleryItems()`.
    - Automatically advance or retreat `galleryPage` if the user navigates across a page boundary while the sheet is open.

### 2.5 Keyboard Shortcuts & Copy Feedback Polish
- **Problem**:
  - Desktop/iPad keyboard users cannot press `Cmd+Enter` / `Ctrl+Enter` inside `#prompt` or `#negative` to trigger generation.
  - Tapping `#resultPrompt` or `#sheetPrompt` reuses the prompt and scrolls to the top, but provides no visual toast/badge feedback when the prompt is copied or applied.
- **Solution**:
  - Bind `Cmd+Enter` / `Ctrl+Enter` on `#prompt` and `#negative` to trigger `$("generateBtn").click()` when not disabled.
  - Add a brief visual confirmation state on `#resultPrompt` / `#sheetPrompt` (e.g., temporarily updating `#resultPromptHint` / `#sheetPromptHint` to `"Loaded into prompt ✓"` for 1.5s) and a secondary **Copy text** icon/action so users can copy the prompt or seed to the system clipboard with one tap.

---

## Suggested Implementation Order for the Coding Agent

1. **Step 1 (Backend Fixes & Speed)**: Implement **1.1** (`_h3_lock`), **1.2** (`GET /api/status` caching + single `nvidia-smi` call + macOS memory/CPU stats), and **1.3** (recent-first ETA sort + orphaned image job cleanup).
2. **Step 2 (Zero-Copy & Storage)**: Implement **1.4** (server-side image reuse for Edit/Animate) and **1.5** (`clear-all` uploads cleanup + file locks).
3. **Step 3 (Compose & Edit UX)**: Implement **2.1** (Prompt History UI) and **2.2** (Edit dropzone, paste routing, auto-aspect match, `#editResultBtn`).
4. **Step 4 (Gallery & Resilience UX)**: Implement **2.3** (active job reload recovery), **2.4** (gallery search + sheet/lightbox prev/next navigation), and **2.5** (`Cmd/Ctrl+Enter` shortcut + prompt feedback).
5. **Step 5 (Verification)**:
   - Bump `app.js?v=` and `styles.css?v=` in `static/index.html` and update the version assertions in `tests/test_turbo_defaults.py`.
   - Run:
     ```bash
     .venv/bin/python -m unittest tests.test_turbo_defaults tests.test_h3_local
     ```
   - Verify all tests pass with zero warnings.
