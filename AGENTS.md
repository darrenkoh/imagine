# AGENTS.md

Instructions for coding agents in this repository. Read this before changing the portal, the image worker, or video.

This tree is the public Imagine app: a pin-gated web studio, a warm Qwen-Image-2.1-Turbo worker, and a FastH3 V2 video backend. FastH3 V2 is the MiniMax-H3 video stack this app runs (8 DiT forwards, synced audio, text-to-audio-video). It is the video API. The MiniMax cloud API is not wired in.

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
| `app.py` | Portal. HTTPS UI and JSON API on `:7860`. Proxies stills to the worker. Spawns FastH3 V2 for video. |
| `static/index.html`, `static/app.js`, `static/styles.css` | The UI. Vanilla JS. No build step. |
| `worker.py` | Qwen-Image-2.1-Turbo worker. Loaded once inside Docker. Listens on `127.0.0.1:7861`. |
| `fasth3_v2.py` | Pure video request contract. No weights import, no download. |
| `tls.py` | Self-signed cert for iPhone Safari. Trust page is `/trust`. |
| `run_worker.sh` | Starts container `imagine-qwen-worker`. |
| `run_portal.sh` | Uvicorn on `0.0.0.0:7860`. HTTPS unless `IMAGINE_HTTP=1`. |
| `run_portal_bg.sh` | Portal in the background. Writes `data/portal.pid` and `data/portal.log`. |
| `launch_tmux.sh` | Stops container `vllm-minimax-h3`, then worker, then portal, then an optional smoke still. |
| `stop_all.sh` | Removes the worker container and stops the portal. |
| `tests/test_turbo_defaults.py` | Still defaults through the real portal payload and worker kwargs. No weights. |
| `tests/test_fasth3_v2.py` | `POST /api/animate` against a fake `fastvideo` binary. No weights. |

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

Static files are read from disk. A CSS or JS edit shows up without a restart, after the cache-bust query changes. `index.html` loads `app.js?v=101` and `styles.css?v=102`. Bump the query on the file you edit. Python route or status-field changes need a portal restart. `worker.py` is bind-mounted read-only; a worker change needs `./run_worker.sh`, which recreates the container and reloads the model. That takes minutes. Do not restart the worker while a still is running.

`python-multipart` is required. Uploads fail without it.

Tests, from the repo root:

```bash
.venv/bin/python -m unittest tests.test_turbo_defaults tests.test_fasth3_v2
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
| `GET` | `/api/status` | Worker health, FastH3 readiness, `h3_running`, CPU/GPU/temp, memory, network. Open. |
| `POST` | `/api/upload` | Image, max 25MB. Stored as `data/uploads/{id}.png`. Returns `{ id, path, url, width, height, mode }`. |
| `GET` | `/api/uploads/{id}.png` | The upload. |
| `POST` | `/api/generate` | Start a still. Returns the job JSON immediately. |
| `POST` | `/api/animate` | Start one FastH3 V2 clip. See the video section. |
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

`_read_job` for a video job returns the job file as written. It does not merge `data/progress/{id}.json`. That file belongs to the image worker. A video poll must keep `steps` at 8 and must not pick up a 49-step still progress file.

For an image job, `_read_job` merges the progress file while status is `queued` or `running`. Status `running` with step 0, or with no progress file yet, is reported back as `queued` so the UI can show the wait state. Done image jobs report `pct` 100.

`_queue_stats` counts earlier queued/running jobs of the same kind and sets `eta_s` from the median `elapsed_s` of up to 8 recent done jobs of that kind.

Cancel is a file, `data/progress/{id}.cancel`, plus `status: cancelled` on the job. The worker checks that file between denoising steps. FastH3 checks it while the subprocess runs. A portal restart marks leftover queued/running video jobs as error: `interrupted — the portal restarted before FastH3 V2 finished`.

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

The state word is `ready`, `loading model`, `H3 up (stop first)`, `worker down`, or `offline`. `H3 up (stop first)` means the worker is down and Docker container `vllm-minimax-h3` is running. That container is a separate LLM. `launch_tmux.sh` stops it before starting the Qwen worker. It is not the video backend. `h3_video` / `video_ready` are FastH3 V2 readiness.

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

Qwen-Image and a 70B LLM do not share a 120GB box. FastH3 V2 weights are the same order of size as the Qwen worker. This portal does not stop the Qwen container before a clip. On one Spark, stop the worker before a long FastH3 run and start it again after.

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

The shipped video API is FastH3 V2. The checkpoint is `FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer`. The architecture is MiniMax-H3 (`layer_profile` `h3_dit_vsa`, attention `VIDEO_SPARSE_ATTN_H3`, env `FASTVIDEO_MINIMAX_H3_FUSIONS=all`). The job model string is `FastH3 V2`. The status label is `FastH3 V2 (8 steps, synced audio)`.

`POST /api/animate` calls `fasth3_v2.normalize_video_request` and then `_run_fasth3_v2`. It spawns `fastvideo generate`. It does not call `_run_animate`, `_h3_command`, or the `h3.c` binary.

There is no `MINIMAX_API_KEY` and no `https://api.minimax.io` client in this repo.

### What the other H3 names mean

`h3_running` on `/api/status` is Docker container `vllm-minimax-h3`. That is an LLM. The footer uses it only for `H3 up (stop first)`.

`_run_animate` and the helpers above it (`_h3_paths`, `_h3_ready`, `_h3_canvas`, `_fit_h3_still`, `_h3_command`) are the unused Apple Silicon `h3.c` path. Binary default `~/src/h3.c/h3` (`H3_BIN`). Weights default `~/src/h3.c/MiniMax-H3` (`H3_MODEL_DIR`), and only count when a `.safetensors` file is present. That helper would pass `--first-frame`, `--layers` (45 on a machine of 40GB or less, else 50, override `H3_LAYERS`), `--steps` (default 20, override `H3_STEPS`), and `--ssd-streaming` on a small machine (`H3_SSD_STREAMING`). Do not connect `api_animate` to this helper. Tests read the route source and reject `--first-frame`, `--layers`, and `--ssd-streaming` on the command that actually launches.

### `POST /api/animate`

`AnimateBody`: `prompt`, `image_id`, `duration` (default 5), `resolution` (default `fast`), optional `num_frames`, `width`, `height`, `steps`, `num_inference_steps`, `seed`, `model`.

`normalize_video_request` is the only mapping. Caller `steps`, `num_inference_steps`, shifts, and sparsity are ignored. A `model` string, when present, must name FastH3 V2 (`fasth3v2`, or `8stepv2` together with `fasth3`). Names containing trim, preview, 4-step, h3-local, h3.c, or minimaxh3 are rejected.

Fixed contract written onto the job:

| Field | Value |
|---|---|
| `model` | `FastH3 V2` |
| `model_id` | `FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer` |
| `layer_profile` | `h3_dit_vsa` |
| `attention_backend` | `VIDEO_SPARSE_ATTN_H3` |
| `transformer_forwards`, `steps` | 8 |
| `sigma_points`, `num_inference_steps` | 9 |
| `dmd_denoising_steps` | 999, 874, 749, 624, 500, 375, 250, 125 |
| `video_scheduler_shift` | 10 |
| `audio_scheduler_shift` | 3 |
| `vsa_sparsity` | 0.8 |
| `vsa_tile_size` | 64 |
| `vsa_kernel` | `triton` |
| `fa4`, `sm100a` | false |
| `vae_tiling`, `synced_audio`, `audio` | true |
| `task`, `conditioning` | `t2av` |
| `first_frame`, `accepts_first_frame` | false |
| `fps` | 24 |
| `guidance_scale` | 1 |

Frame count is `17n+5` at 24 fps, at least 5, and strictly under 345. Five seconds is 124 frames. Six seconds is 141. Ten seconds is 243. A request whose raw `seconds * 24` is 345 or more is rejected (a 15 second chip does not exist, and 15 seconds does not start a job). An explicit `num_frames` must already be a legal count under 345. An illegal body is HTTP 400 and writes no job file.

Canvases: `fast` / `480p` / `480` / `832x480` are 832×480. `768p` / `768` / `1344x768` are 1344×768. An explicit width and height must both be set, sit on a 32-pixel grid, have a short edge of at most 768 and a long edge of at most 1344. Preview, trim, and off-grid sizes are rejected.

The recipe is text-to-audio-video. A supplied `image_id` must already exist as `data/uploads/{id}.png`. It is stored as `reference_image_id` and is not sent to FastVideo. The job's `image_id` stays null and `first_frame` stays false. On success the portal may copy that upload into `data/images/{id}.png` as a gallery poster. Empty prompt uses `fasth3_v2.DEFAULT_PROMPT`. Prompt max is 7000 characters. Default seed is 1234 when the caller omits it.

One clip at a time. A second `POST /api/animate` while one is active is 409. Readiness is checked before the job file is written.

### Launch

Readiness (`inspect_runtime`) does not download weights. It looks for `FASTH3_V2_BIN` or `fastvideo` on `PATH`, and `FASTH3_V2_MODEL_DIR` or `~/models/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer` (Hugging Face hub cache is the second candidate). The directory must contain `fastvideo_inference.json` and a `.safetensors` file. `checkpoint_problem` refuses a trim/preview/4-step id, a forward count other than 8, a sigma count other than 9, a video shift other than 10, an audio shift other than 3, a sparsity other than 0.8, or a different DMD ladder. `model_path` passed to generate is the directory that holds `fastvideo_inference.json`.

`/api/status` reports `video_label`, `video_steps` (8), `video_audio` (`synced`), `video_ready`, `video_detail`, and the same readiness on `h3_video` / `h3_detail`. The UI copies `video_label` into `#h3Hint` and the animate progress line.

`_run_fasth3_v2` writes `data/progress/{id}-fasth3.yaml` and runs:

```text
nice -n 19 <fastvideo> generate --config <yaml>
```

`generation_env` forces `FASTVIDEO_MINIMAX_H3_FUSIONS=all`, `FASTVIDEO_NVFP4_MM_BACKEND=cutlass`, `FASTVIDEO_H3_VAE_TILE_BATCH=1`, `FASTVIDEO_VSA_TRITON=1`, `FASTVIDEO_VSA_SM100A=0`, `FASTVIDEO_FA4=0`, `FASTVIDEO_ATTENTION_BACKEND=VIDEO_SPARSE_ATTN_H3`, `FASTVIDEO_STAGE_LOGGING=1`. A caller environment that set FA4 or sm100a is overwritten.

The YAML is the one-GPU NVFP4 recipe: `workload_type: t2v`, `vae_tiling: true`, `video_decode_backend: h3-vae`, `num_inference_steps: 9`, `guidance_scale: 1`, empty negative prompt, `save_video: true`. No image path, no `first_frame`, compile off, offload off. Output dir is `data/videos/{id}-out`. The newest non-empty MP4 there is moved to `data/videos/{id}.mp4`.

The child is its own session (`start_new_session=True`). Stop it with `killpg` on that session only. `killpg` on the portal's own group would kill the portal. Progress lines matching `Phase N/M` update `provider_status`, `pct`, and `step`. The UI polls once a second and shows `FastH3 V2 · {provider_status}`. Timeout is 6 hours. Cancel and a non-zero exit go through `_halt_h3` / `_h3_failure_message`.

While the job runs, `_touch` rewrites `model` to `FastH3 V2`, `first_frame` to false, and `conditioning` to `t2av` on every update.

## Change rules

- Keep still defaults on the saved 8-step Turbo schedule at CFG 1 with `use_kv_cache`. A different step count is an explicit `sigmas` list of that length.
- Keep video on the FastH3 V2 contract above. Do not add a preview, trim, 4-forward, or ~49-forward schedule switch.
- A still attached to animate stays a reference. Do not set `first_frame` or pass the PNG into the FastVideo config.
- Do not merge image-worker progress into a video job.
- An illegal animate request must leave no job file.
- Desktop layout stays `1.1fr .9fr` unless the task changes it.
- Bump `app.js?v=` or `styles.css?v=` in `index.html` when you edit that file.
- Do not commit `data/`, `certs/`, `.env`, or `.venv/`.
- Run the unittest command above after a portal, worker-contract, or video-contract change.
