"""FastH3 V2 video contract through the shipped portal entry. No weights loaded."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["IMAGINE_DATA"] = tempfile.mkdtemp(prefix="imagine-fasth3-")
os.environ["IMAGINE_PIN"] = "haku"
os.environ["IMAGINE_THUMB_WARM"] = "0"
os.environ.pop("IMAGINE_MODEL", None)

import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

_STACK = Path(os.environ["IMAGINE_DATA"]) / "stack"
_SCRIPT = _STACK / "fastvideo"
_MODEL = _STACK / "FastVideo-FastH3-8-Step-V2-NVFP4-Consumer"
_CAPTURE = _SCRIPT.with_suffix(".capture.json")

_CONTRACT = {
    "schema_version": "fasth3-inference-contract-v1",
    "model_id": "FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer",
    "task": "t2av",
    "dmd_denoising_steps": [999, 874, 749, 624, 500, 375, 250, 125],
    "num_inference_steps": 9,
    "transformer_forwards": 8,
    "video_scheduler_shift": 10.0,
    "audio_scheduler_shift": 3.0,
    "vsa_sparsity": 0.8,
    "vsa_tile_size": 64,
    "attention_backend": "VIDEO_SPARSE_ATTN_H3",
}


def _install_stack() -> None:
    _STACK.mkdir(parents=True, exist_ok=True)
    _MODEL.mkdir(parents=True, exist_ok=True)
    (_MODEL / "fastvideo_inference.json").write_text(json.dumps(_CONTRACT))
    (_MODEL / "weights.safetensors").write_bytes(b"not-a-real-tensor")
    _SCRIPT.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
cfg = None
if "--config" in args:
    cfg = Path(args[args.index("--config") + 1])
text = cfg.read_text() if cfg is not None and cfg.is_file() else ""
out = None
for line in text.splitlines():
    stripped = line.strip()
    if not stripped.startswith("output_path:"):
        continue
    raw = stripped.split(":", 1)[1].strip()
    out = Path(json.loads(raw) if raw[:1] in ('"', "'") else raw)
    break
capture = Path({str(_CAPTURE)!r})
env = {{k: v for k, v in os.environ.items() if k.startswith("FASTVIDEO_")}}
capture.write_text(json.dumps({{"argv": sys.argv, "env": env, "config": text}}))
if out is not None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "clip.mp4").write_bytes(bytes.fromhex("000000186674797069736f6d0000000069736f6d69736f326d703431"))
"""
    )
    _SCRIPT.chmod(0o755)


_install_stack()


class FastH3V2ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["FASTH3_V2_BIN"] = str(_SCRIPT)
        os.environ["FASTH3_V2_MODEL_DIR"] = str(_MODEL)
        os.environ.pop("FASTVIDEO_FA4", None)
        os.environ.pop("FASTVIDEO_VSA_SM100A", None)
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.headers = {"X-Imagine-Pin": "haku"}

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def _job_names(self) -> set[str]:
        return {path.name for path in app.JOBS.glob("*.json")}

    def _wait(self, job_id: str) -> dict:
        last = None
        for _ in range(80):
            response = self.client.get(f"/api/jobs/{job_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            last = response.json()
            if last["status"] in ("done", "error", "cancelled"):
                return last
            time.sleep(0.05)
        self.fail(f"job stayed {None if last is None else last.get('status')}: {last}")

    def _animate(self, payload: dict) -> tuple[dict, dict]:
        before = self._job_names()
        response = self.client.post("/api/animate", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        started = response.json()
        self.assertNotEqual(self._job_names(), before)
        self.assertTrue((app.JOBS / f"{started['id']}.json").is_file())
        finished = self._wait(started["id"])
        self.assertEqual(finished["status"], "done", finished.get("error"))
        return started, finished

    def _reject(self, payload: dict) -> dict:
        before = self._job_names()
        response = self.client.post("/api/animate", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self._job_names(), before)
        body = response.json()
        self.assertIsInstance(body["detail"], str)
        self.assertTrue(body["detail"])
        return body

    def _assert_contract(self, job: dict, frames: int, width: int, height: int) -> None:
        self.assertEqual(job["model"], "FastH3 V2")
        self.assertNotEqual(job["model"], "h3-local")
        self.assertEqual(job["model_id"], "FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer")
        self.assertEqual(job["layer_profile"], "h3_dit_vsa")
        self.assertEqual(job["attention_backend"], "VIDEO_SPARSE_ATTN_H3")
        self.assertEqual(job["transformer_forwards"], 8)
        self.assertEqual(job["steps"], 8)
        self.assertEqual(job["sigma_points"], 9)
        self.assertEqual(job["num_inference_steps"], 9)
        self.assertEqual(job["dmd_denoising_steps"], [999, 874, 749, 624, 500, 375, 250, 125])
        self.assertEqual(job["video_scheduler_shift"], 10.0)
        self.assertEqual(job["audio_scheduler_shift"], 3.0)
        self.assertEqual(job["vsa_sparsity"], 0.8)
        self.assertEqual(job["vsa_tile_size"], 64)
        self.assertEqual(job["vsa_kernel"], "triton")
        self.assertFalse(job["fa4"])
        self.assertFalse(job["sm100a"])
        self.assertTrue(job["vae_tiling"])
        self.assertTrue(job["synced_audio"])
        self.assertTrue(job["audio"])
        self.assertEqual(job["task"], "t2av")
        self.assertEqual(job["conditioning"], "t2av")
        self.assertFalse(job["first_frame"])
        self.assertFalse(job["accepts_first_frame"])
        self.assertIsNone(job.get("image_id"))
        self.assertEqual(job["fps"], 24)
        self.assertEqual(job["num_frames"], frames)
        self.assertEqual(job["width"], width)
        self.assertEqual(job["height"], height)
        self.assertEqual(job["guidance_scale"], 1.0)
        blob = json.dumps(job)
        self.assertNotIn("h3-local", blob)
        self.assertNotIn("Trim", blob)
        self.assertNotIn("Preview", blob)
        self.assertNotIn("4-step", blob)

    def _assert_launch(self, job: dict, frames: int, width: int, height: int, prompt: str) -> None:
        command = job["command"]
        self.assertEqual(command[:3], ["nice", "-n", "19"])
        self.assertEqual(command[3], str(_SCRIPT))
        self.assertEqual(command[4:6], ["generate", "--config"])
        launched = " ".join(command)
        self.assertNotIn("h3.c", launched)
        self.assertNotIn("--first-frame", launched)
        self.assertNotIn("--layers", launched)
        self.assertNotIn("--ssd-streaming", launched)
        capture = json.loads(_CAPTURE.read_text())
        env = capture["env"]
        self.assertEqual(env["FASTVIDEO_VSA_TRITON"], "1")
        self.assertEqual(env["FASTVIDEO_VSA_SM100A"], "0")
        self.assertEqual(env["FASTVIDEO_FA4"], "0")
        self.assertEqual(env["FASTVIDEO_ATTENTION_BACKEND"], "VIDEO_SPARSE_ATTN_H3")
        self.assertEqual(env["FASTVIDEO_H3_VAE_TILE_BATCH"], "1")
        self.assertEqual(env["FASTVIDEO_MINIMAX_H3_FUSIONS"], "all")
        config = capture["config"]
        self.assertIn("num_inference_steps: 9", config)
        self.assertIn("VSA_sparsity: 0.8", config)
        self.assertIn("VSA_tile_size: 64", config)
        self.assertIn("layer_profile: h3_dit_vsa", config)
        self.assertIn("vae_tiling: true", config)
        self.assertIn("attention_backend: VIDEO_SPARSE_ATTN_H3", config)
        self.assertIn("video_decode_backend: h3-vae", config)
        self.assertIn(f"num_frames: {frames}", config)
        self.assertIn(f"width: {width}", config)
        self.assertIn(f"height: {height}", config)
        self.assertIn(prompt, config)
        self.assertIn(str(_MODEL), config)
        self.assertNotIn("0.9", config)
        self.assertNotIn("Trim", config)
        self.assertNotIn("Preview", config)
        self.assertNotIn("4-step", config)
        self.assertNotIn("h3-local", config)
        self.assertNotIn("num_inference_steps: 4", config)
        self.assertNotIn("num_inference_steps: 5", config)
        self.assertNotIn("num_inference_steps: 49", config)
        self.assertNotIn("first_frame", config)
        self.assertNotIn("image_id", config)

    def test_api_normalizes_five_and_ten_second_canvases(self) -> None:
        os.environ["FASTVIDEO_FA4"] = "1"
        os.environ["FASTVIDEO_VSA_SM100A"] = "1"
        cases = (
            ({"prompt": "A red fox leaps into deep snow.", "duration": 5, "resolution": "fast",
              "steps": 4, "num_inference_steps": 49}, 124, 832, 480),
            ({"prompt": "Gulls call over a quiet harbor.", "duration": 5, "resolution": "480p"},
             124, 832, 480),
            ({"prompt": "A potter finishes a bowl at the wheel.", "duration": 5, "resolution": "768p"},
             124, 1344, 768),
            ({"prompt": "A slow drone glides over a coastal town.", "duration": 10, "resolution": "fast"},
             243, 832, 480),
            ({"prompt": "Lanterns sway above a night market.", "duration": 10, "resolution": "768p",
              "model": "FastVideo/FastVideo-FastH3-8-Step-V2-NVFP4-Consumer"},
             243, 1344, 768),
            ({"prompt": "Leaves turn over in a light breeze.", "duration": 6, "resolution": "fast"},
             141, 832, 480),
        )
        for payload, frames, width, height in cases:
            started, finished = self._animate(payload)
            self._assert_contract(started, frames, width, height)
            self._assert_contract(finished, frames, width, height)
            self._assert_launch(finished, frames, width, height, payload["prompt"])
            self.assertFalse(finished["first_frame"])
            self.assertIsNone(finished.get("reference_image_id"))
            video = self.client.get(finished["video"], headers=self.headers)
            self.assertEqual(video.status_code, 200, video.text)
            self.assertGreater(len(video.content), 0)
            self.assertIn("video/mp4", video.headers["content-type"])

    def test_text_only_does_not_record_first_frame(self) -> None:
        started, finished = self._animate(
            {"prompt": "Rain ticks on a tin roof.", "duration": 5, "resolution": "fast"}
        )
        for job in (started, finished):
            self._assert_contract(job, 124, 832, 480)
            self.assertFalse(job["first_frame"])
            self.assertEqual(job["conditioning"], "t2av")
            self.assertIsNone(job.get("image_id"))
            self.assertIsNone(job.get("reference_image_id"))
            self.assertNotIn("first_frame_path", job)
        self.assertNotIn("first_frame", json.loads(_CAPTURE.read_text())["config"])

    def test_supplied_still_is_not_first_frame_conditioning(self) -> None:
        image_id = "stillframe01"
        Image.new("RGB", (64, 40), (20, 30, 40)).save(app.UPLOADS / f"{image_id}.png")
        started, finished = self._animate(
            {
                "prompt": "Steam rises off a teacup.",
                "image_id": image_id,
                "duration": 5,
                "resolution": "768p",
                "steps": 49,
            }
        )
        for job in (started, finished):
            self._assert_contract(job, 124, 1344, 768)
            self.assertFalse(job["first_frame"])
            self.assertEqual(job["conditioning"], "t2av")
            self.assertIsNone(job.get("image_id"))
            self.assertEqual(job["reference_image_id"], image_id)
        config = json.loads(_CAPTURE.read_text())["config"]
        self.assertNotIn(image_id, config)
        self.assertNotIn("first_frame", config)
        self.assertNotIn(".png", config)

    def test_image_worker_progress_does_not_rewrite_a_video_job(self) -> None:
        started, _finished = self._animate(
            {"prompt": "A cat watches dust in a sunbeam.", "duration": 10, "resolution": "fast"}
        )
        poison = app.PROGRESS / f"{started['id']}.json"
        poison.write_text(json.dumps({"step": 49, "steps": 49, "pct": 40, "status": "running"}))
        seen = self.client.get(f"/api/jobs/{started['id']}", headers=self.headers)
        self.assertEqual(seen.status_code, 200, seen.text)
        job = seen.json()
        self.assertEqual(job["model"], "FastH3 V2")
        self.assertEqual(job["steps"], 8)
        self.assertEqual(job["transformer_forwards"], 8)
        self.assertEqual(job["num_inference_steps"], 9)
        self.assertNotEqual(job.get("step"), 49)
        self.assertEqual(job["status"], "done")

    def test_illegal_requests_leave_no_job(self) -> None:
        illegal = (
            {"prompt": "too long", "duration": 15, "resolution": "fast"},
            {"prompt": "too long", "duration": 20, "resolution": "768p"},
            {"prompt": "too many frames", "duration": 5, "resolution": "fast", "num_frames": 345},
            {"prompt": "base schedule", "duration": 5, "resolution": "fast", "num_frames": 49},
            {"prompt": "preview frames", "duration": 5, "resolution": "fast", "num_frames": 4},
            {"prompt": "bad size", "duration": 5, "resolution": "preview"},
            {"prompt": "bad size", "duration": 5, "resolution": "trim"},
            {"prompt": "bad size", "duration": 5, "resolution": "4k"},
            {"prompt": "off grid", "duration": 5, "width": 100, "height": 100},
            {"prompt": "too wide", "duration": 5, "width": 1920, "height": 1080},
            {"prompt": "off grid", "duration": 5, "width": 800, "height": 450},
            {"prompt": "wrong model", "duration": 5, "resolution": "fast", "model": "FastH3 Trim"},
            {"prompt": "wrong model", "duration": 5, "resolution": "fast", "model": "h3-local"},
            {
                "prompt": "wrong model",
                "duration": 5,
                "resolution": "fast",
                "model": "FastVideo/FastVideo-FastH3-4-step-Preview-v1-VSA-DataFree",
            },
        )
        for payload in illegal:
            body = self._reject(payload)
            self.assertNotIn("job", body)

    def test_callers_cannot_select_another_schedule(self) -> None:
        contract = app.normalize_fasth3_v2(
            {
                "prompt": "A red fox",
                "duration": 5,
                "resolution": "fast",
                "steps": 4,
                "num_inference_steps": 49,
                "vsa_sparsity": 0.9,
                "video_scheduler_shift": 12,
                "audio_scheduler_shift": 12,
                "transformer_forwards": 4,
                "model": "FastH3 V2",
            }
        )
        self.assertEqual(contract["transformer_forwards"], 8)
        self.assertEqual(contract["num_inference_steps"], 9)
        self.assertEqual(contract["vsa_sparsity"], 0.8)
        self.assertEqual(contract["video_scheduler_shift"], 10.0)
        self.assertEqual(contract["audio_scheduler_shift"], 3.0)
        self.assertFalse(contract["first_frame"])
        with self.assertRaises(app.fasth3_v2.FastH3V2Error):
            app.normalize_fasth3_v2(
                {"prompt": "nope", "duration": 5, "resolution": "fast", "model": "FastH3 Trim"}
            )

    def test_not_ready_and_preview_checkpoint_do_not_start_jobs(self) -> None:
        os.environ["FASTH3_V2_BIN"] = "/no/such/fastvideo-bin"
        os.environ["FASTH3_V2_MODEL_DIR"] = "/no/such/fasth3-weights"
        first = self.client.get("/api/status").json()
        second = self.client.get("/api/status").json()
        self.assertEqual(first["video_label"], "FastH3 V2 (8 steps, synced audio)")
        self.assertEqual(second["video_label"], first["video_label"])
        self.assertEqual(second["video_detail"], first["video_detail"])
        self.assertFalse(first["video_ready"])
        self.assertFalse(first["h3_video"])
        self.assertEqual(first["h3_detail"], first["video_detail"])
        self.assertIn("FastH3 V2 (8 steps, synced audio)", first["video_detail"])
        self.assertIn("not ready", first["video_detail"])
        self.assertEqual(first["video_steps"], 8)
        self.assertEqual(first["video_audio"], "synced")
        missing = self._reject({"prompt": "A red fox leaps.", "duration": 5, "resolution": "fast"})
        again = self._reject({"prompt": "A red fox leaps.", "duration": 5, "resolution": "fast"})
        self.assertEqual(again["detail"], missing["detail"])

        preview = Path(os.environ["IMAGINE_DATA"]) / "preview-stack"
        preview.mkdir(parents=True, exist_ok=True)
        (preview / "fastvideo_inference.json").write_text(
            json.dumps(
                {
                    "model_id": "FastVideo/FastVideo-FastH3-4-step-Preview-v1-VSA-DataFree",
                    "transformer_forwards": 4,
                    "num_inference_steps": 5,
                    "video_scheduler_shift": 12,
                    "audio_scheduler_shift": 3,
                    "vsa_sparsity": 0.9,
                }
            )
        )
        (preview / "weights.safetensors").write_bytes(b"preview")
        os.environ["FASTH3_V2_BIN"] = str(_SCRIPT)
        os.environ["FASTH3_V2_MODEL_DIR"] = str(preview)
        status = self.client.get("/api/status").json()
        self.assertFalse(status["video_ready"])
        self.assertIn("FastH3 V2 (8 steps, synced audio)", status["video_detail"])
        self.assertIn("not ready", status["video_detail"])
        self._reject({"prompt": "Do not run the preview.", "duration": 5, "resolution": "768p"})

    def test_shipped_animate_route_spawns_fasth3_v2_not_h3c(self) -> None:
        import inspect

        route = inspect.getsource(app.api_animate)
        runner = inspect.getsource(app._run_fasth3_v2)
        self.assertIn("_run_fasth3_v2", route)
        self.assertNotIn("_run_animate", route)
        self.assertNotIn("_h3_command", route)
        self.assertNotIn("_h3_command", runner)
        self.assertNotIn("--first-frame", runner)
        self.assertIn("generation_command", runner)
        self.assertIn("create_subprocess_exec", runner)

    def test_served_animate_copy_names_fasth3_v2(self) -> None:
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("FastH3 V2 (8 steps, synced audio)", html)
        self.assertIn("synced audio", html)
        self.assertIn('id="animateDrop"', html)
        self.assertIn('id="animateBrowse"', html)
        self.assertIn('id="animateFile"', html)
        self.assertIn('id="animatePreview"', html)
        self.assertIn('id="steps"', html)
        self.assertIn('value="8"', html)
        self.assertIn("Qwen-Image-2.1-Turbo", html)
        self.assertNotIn("uses this image as the first frame", html)
        script = self.client.get("/static/app.js")
        self.assertEqual(script.status_code, 200)
        js = script.text
        self.assertIn("FastH3 V2 (8 steps, synced audio)", js)
        self.assertIn("const DURATIONS = [5, 6, 10];", js)
        self.assertIn('{ id: "fast", label: "Fast" }', js)
        self.assertIn('{ id: "768p", label: "768p" }', js)
        self.assertNotIn("Local H3 uses this image as the first frame", js)
        self.assertEqual(app.H3_RESOLUTIONS, ("fast", "768p"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
