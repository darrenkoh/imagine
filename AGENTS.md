# AGENTS.md

Instructions for coding agents in this repository. Read this before changing the portal, the image worker, or video.

This tree is the public Imagine app: a pin-gated web studio, a warm Qwen-Image-2.1-Turbo worker, and local MiniMax H3 video through the Apple Silicon `h3.c` binary. A still is the first frame. The MiniMax cloud API is not wired in. The live Spark portal's opening clips use the MiniMax H3 Turbo LoRA on the H3 server. That queue is not in this tree.

## Two trees

| | This checkout | Live Spark portal |
|---|---|---|
| Path | `/Users/darrenkoh/github/inference/imagine` | `/home/dkoh/github/inference/imagine` |
| Git | `https://github.com/darrenkoh/imagine.git` | `https://github.com/darrenkoh/inference.git` |
| Store | JSON files under `data/jobs` | SQLite (`IMAGINE_STORE=sqlite`) |
| Extra product | Gallery of stills and clips | Series, still batches, video queue, playback proxies |

Do not rsync or overwrite the Spark tree with this one. Do not add series, still-batch, or the video queue here unless the task asks for them. Spark-only notes in memory are about the other tree.

`data/`, `certs/`, `.env`, and `.venv/` are gitignored. The gallery people use lives on the Spark. This Mac checkout's `data/` is local scratch.

## Layout

| Path | Role |
|---|---|
| `app.py` | Portal. HTTPS UI and JSON API on `:7860`. Proxies stills to the worker. Spawns local `h3.c` for video. |
| `static/index.html`, `static/app.js`, `static/styles.css` | The UI. Vanilla JS. No build step. |
| `worker.py` | Qwen-Image-2.1-Turbo worker. Loaded once inside Docker. Listens on `127.0.0.1:7861`. |
| `tls.py` | Self-signed cert for iPhone Safari. Trust page is `/trust`. |
| `run_worker.sh` | Starts container `imagine-qwen-worker`. |
| `run_portal.sh` | Uvicorn on `0.0.0.0:7860`. HTTPS unless `IMAGINE_HTTP=1`. |
| `run_portal_bg.sh` | Portal in the background. Writes `data/portal.pid` and `data/portal.log`. |
| `launch_tmux.sh` | Stops container `vllm-minimax-h3`, then worker, then portal, then an optional smoke still. |
| `stop_all.sh` | Removes the worker container and stops the portal. |
| `tests/test_turbo_defaults.py` | Still defaults through the real portal payload and worker kwargs. No weights. |
| `tests/test_h3_local.py` | `POST /api/animate` stays on local `h3.c`. No weights and no binary. |

Python is 3.12 in `.venv`. System `python3` does not have FastAPI. Use `.venv/bin/python`.

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export IMAGINE_MODEL=/path/to/Qwen-Image-2.1-Turbo
export IMAGINE_DATA="$(pwd)/data"
export IMAGINE_PIN="choose-a-pin"          # unset default in code and scripts is haku
./run_worker.sh                            # GPU Docker, 127.0.0.1:7861
./run_portal.sh                            # HTTPS :7860, or IMAGINE_HTTP=1 for cleartext
```

Ports: portal `7860`, worker `7861`. The worker binds to localhost only. The portal is the public surface.

Static files are read from disk. A CSS or JS edit shows up without a restart, after the cache-bust query changes. `index.html` loads `app.js?v=103` and `styles.css?v=102`. Bump the query on the file you edit. Python route or status-field changes need a portal restart. `worker.py` is bind-mounted read-only; a worker change needs `./run_worker.sh`, which recreates the container and reloads the model. That takes minutes. Do not restart the worker while a still is running.

`python-multipart` is required. Uploads fail without it.

Tests, from the repo root:

```bash
.venv/bin/python -m unittest tests.test_turbo_defaults tests.test_h3_local
```

Both files set `IMAGINE_DATA` to a temp directory before import when `app` is not already loaded. They must keep passing. Several tests read the served HTML, `app.js`, `README.md`, and `run_worker.sh`, so a renamed label or a step default other than 8 fails there. Update the test in the same change when the contract changes on purpose.

## Image UI portal

`app.py` plus `static/`. Phone-first. Desktop `.layout` is `grid-template-columns: 1.1fr .9fr` at `min-width: 720px`. Leave that ratio unless the task changes layout.

### Auth

`IMAGINE_PIN` (default `haku` when unset). Do not print a live PIN, commit `.env`, or change the default in a drive-by edit.

`_check_pin` accepts `X-Imagine-Pin`, cookie `imagine_pin`, or query `pin`. `POST /api/auth` with `{ "pin": "…" }` sets the cookie (30 days, `Secure` on HTTPS).

Open without a pin: `/`, `/static/*`, `/trust`, `/trust/cert.pem`, `/healthz`. `GET /api/status` is open and adds `pin_ok`. Everything else is 401 on a bad pin.

### Tasks in the UI

`static/app.js` has three tasks: `create`, `edit`, `animate`.

Create and edit call `POST /api/generate`. Animate calls `POST /api/animate`.

Aspect chips (create/edit): `1:1` 1024², `16:9` 1344×768, `9:16` 768×1344, `3:4` 896×1152, `4:3` 1152×896, `2:3` 832×1216. Default aspect is `3:4`.

Style chips: None, Cinematic, Anime, Photo, Watercolor, Sketch, 3D. The id is the prefix string sent as `style`. Empty means no style.

Animate chips: durations `5`, `6`, `10` (default 5) and resolutions `fast`, `768p` (default `fast`). Stored in `imagine_h3_duration` and `imagine_h3_resolution`.

Other localStorage keys: `imagine_pin`, `imagine_spicy`, `imagine_nsfw_blur` (default on), `imagine_rgba`, `imagine_framing` (default on), `imagine_strength` (default 0.65), `imagine_gallery_filter`, `imagine_page_size` (default 30, clamp 1–200), `imagine_status_ms` (500, 1000, 1500, 2000, 2500, 3000; default 500).

### Portal routes

Header `X-Imagine-Pin` or the session cookie, unless noted.

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/auth` | Set the pin cookie. |
| `GET` | `/api/status` | Worker health, local H3 readiness, `h3_running`, the loaded-model label, CPU/GPU/temp, memory, network. Open. |
| `POST` | `/api/upload` | Image, max 25MB. Stored as `data/uploads/{id}.png`. Returns `{ id, path, url, width, height, mode }`. |
| `GET` | `/api/uploads/{id}.png` | The upload. |
| `POST` | `/api/generate` | Start a still. Returns the job JSON immediately. |
| `POST` | `/api/animate` | Start one local H3 clip from a still. See the video section. |
| `GET` | `/api/jobs/{id}` | Job plus live progress and queue stats. |
| `POST` | `/api/jobs/{id}/cancel` | Writes `data/progress/{id}.cancel` and marks the job cancelled. |
| `GET` | `/api/gallery` | Done stills and clips that still have their file. |
| `POST` | `/api/gallery/{id}/star` | Toggle `starred` on the job file. |
| `DELETE` | `/api/gallery/{id}` | Delete one item. |
| `POST` | `/api/gallery/delete` | `{ "ids": […] }`. |
| `GET` | `/api/images/{id}.png` | Full PNG. `Cache-Control: private, max-age=31536000, immutable`. |
| `GET` | `/api/thumbs/{id}.jpg` or `.png` | Grid thumb. Built on demand from the PNG. |
| `GET` | `/api/videos/{id}.mp4` | The clip. |
| `GET` | `/api/prompt-history` | Last 200 prompts. |
| `DELETE` | `/api/prompt-history/{id}` and `/api/prompt-history` | One entry, or all. |
| `DELETE` | `/api/clear-all` | History, PNGs, MP4s, jobs, progress JSON, thumbs. |

`GET /api/generate` body (`GenerateBody`): `prompt` (required), `negative_prompt`, `width`, `height`, `steps` (default 8), `seed`, `style`, `spicy`, `rgba`, `framing` (default true), `image_id`, `strength` (default 0.65).

`style_still` builds the worker prompt. It leads with `style`, then the user prompt, then the spicy suffix when `spicy` is set, then the RGBA prefix when `rgba` is set and the prompt does not already say rgba/transparent, then the framing suffix when `framing` is on. Negatives accumulate: the user's negative, `FRAME_NEGATIVE` when framing is on, `SPICY_NEGATIVE` when spicy, and `STYLE_NEGATIVES` when the style string contains `3d`, `anime`, `cinematic`, `photorealistic`, `watercolor`, or `sketch`. History stores the user's prompt, not the framing suffix. `_strip_frame` removes that suffix when the gallery shows a prompt.

`apply_negative_cfg` is true when the user typed a negative, a style is set, or spicy is on. Automatic framing text alone leaves it false. The worker uses that flag. See the Qwen section.

`still_worker_payload` is the JSON posted to the worker. `resolve_edit_path` turns `image_id` into `uploads/{id}.png` and 400s if that file is missing.

### Jobs and progress

Job ids are 12 hex chars. Files are `data/jobs/{id}.json`, written via a temp file and replace. `_write_job` bumps the gallery cache.

Kind is `job["kind"]` or `"image"`. Video jobs set `kind` to `video`.

`_read_job` for a video job returns the job file as written. It does not merge `data/progress/{id}.json`. That file belongs to the image worker. A video poll must not pick up a still progress file.

For an image job, `_read_job` merges the progress file while status is `queued` or `running`. Status `running` with step 0, or with no progress file yet, is reported back as `queued` so the UI can show the wait state. Done image jobs report `pct` 100.

`_queue_stats` counts earlier queued/running jobs of the same kind and sets `eta_s` from the median `elapsed_s` of up to 8 recent done jobs of that kind.

Cancel is a file, `data/progress/{id}.cancel`, plus `status: cancelled` on the job. The worker checks that file between denoising steps. Local H3 checks it while the subprocess runs. A portal restart marks leftover queued/running video jobs as error: `interrupted — the portal restarted before local h3 finished`.

### Gallery

`_build_gallery_items` lists done jobs. A still needs `data/images/{id}.png`. A video needs `data/videos/{id}.mp4`. A video may also have a poster PNG (from the reference still). Sort is newest `created_at` first. The list is cached until the jobs, images, or videos directory mtime changes, or `_bump_gallery` runs.

Thumbs: `THUMB_PX` 512. JPEG quality 76 as `{id}.jpg`, or PNG when `rgba` is set. `_write_grid_thumb` is locked per filename and skips a non-empty existing thumb. A daemon warmer starts unless `IMAGINE_THUMB_WARM=0`.

NSFW is `spicy` on the job, or `_NSFW_RE` on the prompts. The client blurs those tiles when `imagine_nsfw_blur` is on.

Filters: All, Starred, Spicy, Normal. Changing the filter returns to page 1.

Paging is client-side. One page size for the gallery. The label is `1-30 of N · page/pages`. Changing the size keeps the first item of the current page in view.

Select mode: a click toggles one id and sets `selectAnchorId`. Shift-click adds every id in `filteredGalleryItems()` between the anchor and the target, inclusive, and skips ids already deleting. That range is the full filtered list, including items on other pages. Image clicks do not open the sheet while selecting. Leaving Select, or a multi-delete, clears the anchor and the selection. While selecting, `.gallery.select-mode` uses `repeat(auto-fill, minmax(92px, 1fr))`, hides stars, and shows `.sel-check`.

Delete marks the card (`.deleting`, `.del-state`, then `.deleting-out`) and calls `DELETE /api/gallery/{id}` or `POST /api/gallery/delete`. A failure puts the card back. `removedIds` stops an in-flight `loadGallery` from restoring a card that just disappeared.

Long-press a thumb (480ms) reuses the prompt without opening the sheet. Sheet actions: download, reuse, edit, animate, star, delete.

### Footer

`#statusPill` is filled by `pollStatus()` from `GET /api/status`. The loop is `setTimeout` (`restartStatusLoop` / `runStatusPoll`). A poll still in flight is skipped. Elapsed request time is subtracted so intervals do not stack. The rate control is `#statusRate`.

The leading label is the loaded model. `/api/status` sends `model_label` and `model_state` (`ready`, `loading`, `down`). Qwen ready is `Qwen Image 2.1`. A Qwen load is `Loading Model Qwen Image 2.1...`. Docker container `vllm-minimax-h3`, when the image worker is down, is `MiniMax H3`. Nothing loaded is `No model`. The pill class is `ok`, `warn`, or `err`. That container is a separate LLM. `launch_tmux.sh` stops it before starting the Qwen worker. It is not this tree's video backend. `h3_video` / `h3_detail` are local `h3.c` readiness. The pill also appends `Local H3` or `Local H3 not ready`.

Each number keeps a fixed width with `visibility: hidden` characters (`.num-ghost`, `aria-hidden`). Percents and temperature use 3 digits. Memory uses 3 digits and one decimal, and a whole value hides `.0`. Rates use the visible precision already in `appendRate`, with unused characters on the left of the shown digits. The tooltip is the visible text (`shownText`). A missing reading is omitted. The state word is not padded. One NIC name is prefixed when `net.ifaces` has a single entry. Several names go in the tooltip.

Network bytes come from `_net_bandwidth()`. Linux uses `/proc/net/dev` (rx field 0, tx field 8). macOS uses `netstat -ibn` link rows (Ibytes/Obytes). Only names starting with `en`, `eth`, `wl`, `wlan`, `bond`, or `ib` count. The first sample stores `_net_prev` and returns null rates. A counter drop is treated as a wrap.

## Qwen Image 2.1 backend

`worker.py` is a FastAPI app titled `imagine-qwen-worker`. One process, one GPU, one pipeline. `_lock` wraps the denoise so a second still waits. The portal talks to `IMAGINE_WORKER_URL` (default `http://127.0.0.1:7861`).

### Process

`run_worker.sh` uses image `nvcr.io/nvidia/pytorch:25.11-py3` (override `IMAGINE_IMAGE`). It mounts:

- `IMAGINE_MODEL` (default `/home/dkoh/models/Qwen-Image-2.1-Turbo`) at `/models/Qwen-Image-2.1-Turbo:ro`
- `IMAGINE_DATA` at `/data`
- `worker.py` at `/app/worker.py:ro`

Inside the container the model path is `/models/Qwen-Image-2.1-Turbo`. If `model_index.json` is missing, the script downloads `Qwen/Qwen-Image-2.1-Turbo` and leaves any older Qwen-Image-2.1 tree alone. `model_index.json` by itself is not a complete weight tree. The container installs `transformers>=5.17.0` and current Diffusers from git, and uninstalls `torchao`. Startup calls `_require_turbo_runtime`: `QwenImage21Pipeline.__init__` must accept `sample_sigmas`, and `transformers` must be at least 5.17.0. Weights load with `local_files_only=True`.

`IMAGINE_TORCH_COMPILE` defaults on. The transformer is compiled with `mode="reduce-overhead"` when CUDA is available. A compile failure is logged and the worker still becomes ready.

`GET /health` returns `ok`, `loading`, `ready`, `error`, `model`, `uptime_s`, `ready_at`, `compiled`. `model` is `Qwen/Qwen-Image-2.1-Turbo` when the path contains `Qwen-Image-2.1-Turbo`. The portal's `/api/status` field `worker` is this payload. `worker_container` is whether Docker container `imagine-qwen-worker` is running.

Qwen-Image and a 70B LLM do not share a 120GB box. This portal does not stop the Qwen container before a clip. On the Spark, the video batch stops the worker before MiniMax H3 and starts it again after.

### `POST /generate`

`GenerateRequest`:

| Field | Rule |
|---|---|
| `prompt` | Required, non-empty. |
| `negative_prompt` | Optional. |
| `width`, `height` | Default 1024. Clamped to 256–2048, then snapped down to a multiple of 16. |
| `steps` | Default 8. Range 1–100. |
| `seed` | Optional. Random `int` in `0 … 2**31-2` when omitted. |
| `true_cfg_scale` | Default 1.0. Range 1–20. The portal does not send this; the worker default applies. |
| `id` | Job id. The portal sends the id it already wrote. |
| `image_path` | Optional, relative to `/data`, for example `uploads/abc.png`. `..` and absolute paths are 400. Missing file is 404. |
| `rgba` | Default false. |
| `strength` | Default 0.65. Range 0–1. Used only as prompt text. The pipeline has no strength argument. |
| `apply_negative_cfg` | Default true. Direct clients keep the CFG bump. The portal sends false for a plain Turbo still whose only negative is automatic framing. |

Not ready: 503 with `pipeline_not_ready`. Already cancelled when the request arrives: 409.

`prepare_still_call` builds the pipeline kwargs. Tests call this without loading weights. Keep it free of CUDA imports at import time.

Schedule (`schedule_for_steps` / `running_steps`):

- `steps == 8` omits both `sigmas` and `num_inference_steps`. The checkpoint's saved `sample_sigmas` grid runs. The call reports 8 steps.
- Any other count sends `sigmas` from `explicit_sigmas`: inclusive linspace of that length from 1.0 down to `1/n`, matching `np.linspace(1.0, 1/n, n)`. The scheduler appends the terminal sigma. Do not send a bare `num_inference_steps`. It does not replace the saved grid, and `running_steps` still returns 8.

Always set `use_kv_cache: true`.

CFG: a negative prompt with `apply_negative_cfg` and `true_cfg_scale <= 1` raises `true_cfg_scale` to 4.0 so the negative can guide. Framing-only portal jobs leave the scale at 1.0.

RGBA: if `rgba` is set and the prompt does not already mention rgba or transparent, prepend the official transparency sentence. Save mode is RGBA. The portal also prepends that sentence, so a portal job already contains it and the worker does not add it twice. Keep both. Direct worker clients rely on the worker prefix.

Edit: open `image_path`, convert to RGBA or RGB to match `rgba`, resize to the snapped width and height, and pass that one PIL image as `image`. Append a strength phrase: under 0.35 subtle, under 0.55 moderate, under 0.8 strong, otherwise full redesign. Response field `edited` is true when an image was attached. `strength` in the response is null when it was not.

Progress is in memory and in `/data/progress/{id}.json`: `id`, `step`, `steps`, `pct`, `status`, `updated_at`. The step callback writes `step + 1` and raises `CancelledError` when `{id}.cancel` exists. The portal polls its own job file, which merges this JSON. `GET /progress/{id}` exists for debugging.

Success writes `/data/images/{id}.png` and returns `id`, `path`, `width`, `height`, `steps`, `seed`, `elapsed_s`, `rgba`, `edited`, `strength`. The portal copies the PNG onto its images dir when the paths differ, writes the thumb, and marks the job done.

The portal HTTP timeout for a still is 600 seconds.

## MiniMax H3 video backend

This tree's video API is local MiniMax H3 through `h3.c`. The job model string is `h3-local`. The still is the first frame. There is no `MINIMAX_API_KEY` and no `https://api.minimax.io` client.

The live Spark portal is a different tree. Its opening clips use the 8-step MiniMax H3 Turbo LoRA (`--quality turbo` on `generate_minimax_h3.sh`). Storyboard clips stay on full MiniMax H3. Do not point this tree's `POST /api/animate` at FastH3 V2 or at the Spark video queue.

### `POST /api/animate`

`AnimateBody`: `prompt` (optional), `image_id` (required), `duration` (default 5), `resolution` (default `fast`).

`image_id` must already exist as `data/uploads/{id}.png`. Duration is 1–15 seconds. Resolution is `fast` or `768p`. An empty prompt uses `H3_DEFAULT_MOTION`. Prompt max is 7000 characters. An illegal body is HTTP 400, or 422 when `image_id` is missing, and writes no job file. Readiness is checked before the job file is written.

The job stores `model` `h3-local`, the user's prompt, the upload id, the duration, and the resolution.

### Launch

Readiness (`_h3_ready`) does not download weights. It looks for `H3_BIN` or `~/src/h3.c/h3`, and `H3_MODEL_DIR` or `~/src/h3.c/MiniMax-H3`. The weights directory counts only when it contains a `.safetensors` file. `/api/status` reports that on `h3_video` / `h3_detail`.

`_run_animate` letterboxes the still, then runs `h3` with `--first-frame`, `--steps` (default 20, override `H3_STEPS`), `--layers` (45 on a machine of 40GB or less, else 50, override `H3_LAYERS`), and `--ssd-streaming` on a small machine (`H3_SSD_STREAMING`). The child is its own session (`start_new_session=True`). Stop it with `killpg` on that session only. `killpg` on the portal's own group would kill the portal. Progress lines matching `Phase N/M` update `provider_status`, `pct`, and `step`. The UI polls once a second and shows `H3 · {provider_status}`. Timeout is 6 hours. Cancel and a non-zero exit go through `_halt_h3` / `_h3_failure_message`.

On success the first frame is saved as the gallery poster and the MP4 is `data/videos/{id}.mp4`.

`h3_running` on `/api/status` is Docker container `vllm-minimax-h3`. That is an LLM, not this animate path.

## Change rules

- Keep still defaults on the saved 8-step Turbo schedule at CFG 1 with `use_kv_cache`. A different step count is an explicit `sigmas` list of that length.
- Keep this tree's video on local `h3.c` with the still as `--first-frame`. Do not route `POST /api/animate` through FastH3 V2.
- Do not merge image-worker progress into a video job.
- An illegal animate request must leave no job file.
- Desktop layout stays `1.1fr .9fr` unless the task changes it.
- Bump `app.js?v=` or `styles.css?v=` in `index.html` when you edit that file.
- Do not commit `data/`, `certs/`, `.env`, or `.venv/`.
- Run the unittest command above after a portal, worker-contract, or video-contract change.
