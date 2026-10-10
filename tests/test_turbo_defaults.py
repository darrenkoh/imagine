"""Portal still defaults through the worker pipeline kwargs. No weights loaded."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolate gallery writes before either module reads IMAGINE_DATA at import.
# If the portal module is already loaded, keep its directory so the worker
# opens the same uploads the portal just wrote.
_loaded = sys.modules.get("app")
if _loaded is not None:
    os.environ["IMAGINE_DATA"] = str(_loaded.DATA)
else:
    os.environ["IMAGINE_DATA"] = tempfile.mkdtemp(prefix="imagine-turbo-")
os.environ["IMAGINE_PIN"] = "haku"
os.environ.pop("IMAGINE_MODEL", None)

import app  # noqa: E402
import worker  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402


def _still(body: app.GenerateBody, image_path: str | None = None):
    """Shipped portal payload → shipped worker kwargs. Not a copy of that logic."""
    payload = app.still_worker_payload(body, "job", image_path)
    req = worker.GenerateRequest(**payload)
    return payload, worker.prepare_still_call(req)


class TurboDefaultsTest(unittest.TestCase):
    def test_portal_default_uses_saved_eight_step_schedule(self) -> None:
        body = app.GenerateBody(prompt="a red apple on a table")
        self.assertEqual(body.steps, 8)
        self.assertTrue(body.framing)
        self.assertFalse(body.rgba)
        self.assertFalse(body.spicy)

        payload, call = _still(body)
        self.assertEqual(payload["steps"], 8)
        self.assertEqual(call.steps, 8)
        self.assertEqual(worker.running_steps(call.kwargs), call.steps)
        self.assertTrue(call.kwargs["use_kv_cache"])
        self.assertEqual(call.kwargs["true_cfg_scale"], 1.0)
        self.assertNotIn("num_inference_steps", call.kwargs)
        self.assertNotIn("sigmas", call.kwargs)
        self.assertNotIn("image", call.kwargs)
        self.assertFalse(call.edited)
        self.assertFalse(call.rgba)
        self.assertEqual(call.output_mode, "RGB")
        # Framing still submits. It must not pretend to be a 28-step run,
        # and it must not lift the Turbo CFG default on its own.
        self.assertIn(app.FRAME_SUFFIX.split(",")[0], payload["prompt"])
        self.assertIn(app.FRAME_NEGATIVE, payload["negative_prompt"])
        self.assertFalse(payload["apply_negative_cfg"])
        # A bare step count is not an override of the saved grid.
        self.assertEqual(worker.running_steps({"num_inference_steps": 28}), 8)
        self.assertEqual(worker.TURBO_STEPS, 8)
        self.assertIn("Qwen-Image-2.1-Turbo", worker.health()["model"])

    def test_custom_step_count_is_an_explicit_sigma_list(self) -> None:
        payload, call = _still(app.GenerateBody(prompt="a cat", steps=12, framing=False))
        self.assertEqual(payload["steps"], 12)
        self.assertNotIn("num_inference_steps", call.kwargs)
        sigmas = call.kwargs["sigmas"]
        self.assertEqual(len(sigmas), call.steps)
        self.assertEqual(call.steps, worker.running_steps(call.kwargs))
        self.assertEqual(sigmas[0], 1.0)
        self.assertAlmostEqual(sigmas[-1], 1.0 / 12)
        self.assertTrue(all(sigmas[i] > sigmas[i + 1] for i in range(len(sigmas) - 1)))

        _, forced = _still(app.GenerateBody(prompt="a cat", steps=28, framing=False))
        self.assertNotIn("num_inference_steps", forced.kwargs)
        self.assertEqual(len(forced.kwargs["sigmas"]), forced.steps)
        self.assertEqual(forced.steps, 28)

    def test_edit_attaches_one_source_image(self) -> None:
        src = app.UPLOADS / "src.png"
        Image.new("RGBA", (32, 48), (10, 20, 30, 255)).save(src)
        image_path = app.resolve_edit_path("src")
        self.assertEqual(image_path, "uploads/src.png")
        body = app.GenerateBody(
            prompt="add a red hat",
            image_id="src",
            framing=False,
            strength=0.2,
        )
        payload, call = _still(body, image_path)
        self.assertEqual(payload["image_path"], "uploads/src.png")
        self.assertEqual(payload["strength"], 0.2)
        self.assertTrue(call.edited)
        self.assertEqual(call.steps, 8)
        self.assertNotIn("sigmas", call.kwargs)
        self.assertTrue(call.kwargs["use_kv_cache"])
        self.assertEqual(call.kwargs["true_cfg_scale"], 1.0)
        image = call.kwargs["image"]
        self.assertNotIsInstance(image, (list, tuple))
        self.assertEqual(image.size, (call.kwargs["width"], call.kwargs["height"]))
        self.assertIn("subtle edit", call.kwargs["prompt"])

    def test_rgba_marks_a_transparent_output(self) -> None:
        payload, call = _still(
            app.GenerateBody(prompt="a cartoon dragon sticker", rgba=True, framing=False)
        )
        self.assertTrue(payload["rgba"])
        self.assertTrue(call.rgba)
        self.assertEqual(call.output_mode, "RGBA")
        prompt = call.kwargs["prompt"].lower()
        self.assertIn("transparent", prompt)
        self.assertIn("alpha", prompt)
        self.assertEqual(call.steps, 8)
        self.assertTrue(call.kwargs["use_kv_cache"])
        self.assertEqual(call.kwargs["true_cfg_scale"], 1.0)
        self.assertNotIn("sigmas", call.kwargs)

    def test_user_negative_still_bumps_cfg(self) -> None:
        _, call = _still(
            app.GenerateBody(prompt="a fox", negative_prompt="blurry watermark")
        )
        self.assertEqual(call.kwargs["true_cfg_scale"], 4.0)
        self.assertIn("blurry watermark", call.kwargs["negative_prompt"])
        self.assertEqual(call.steps, 8)

        _, styled = _still(
            app.GenerateBody(prompt="a fox", style="watercolor painting", framing=False)
        )
        self.assertEqual(styled.kwargs["true_cfg_scale"], 4.0)
        self.assertIn("watercolor painting", styled.kwargs["prompt"])
        self.assertIn("photorealistic", styled.kwargs["negative_prompt"])

        _, spicy = _still(app.GenerateBody(prompt="a portrait", spicy=True, framing=False))
        self.assertEqual(spicy.kwargs["true_cfg_scale"], 4.0)
        self.assertIn(app.SPICY_NEGATIVE.split(",")[0], spicy.kwargs["negative_prompt"])

    def test_still_controls_still_submit(self) -> None:
        payload, call = _still(
            app.GenerateBody(
                prompt="a fox in snow",
                negative_prompt="blurry",
                width=1344,
                height=768,
                steps=8,
                seed=7,
                style="cinematic film still, dramatic lighting",
                framing=True,
                rgba=False,
                strength=0.4,
            )
        )
        self.assertEqual(payload["width"], 1344)
        self.assertEqual(payload["height"], 768)
        self.assertEqual(payload["seed"], 7)
        self.assertEqual(payload["strength"], 0.4)
        self.assertEqual(payload["steps"], 8)
        self.assertIn("cinematic film still", payload["prompt"])
        self.assertIn(app.FRAME_SUFFIX.split(",")[0], payload["prompt"])
        self.assertIn("blurry", payload["negative_prompt"])
        self.assertEqual(call.kwargs["true_cfg_scale"], 4.0)
        self.assertEqual(call.steps, 8)
        self.assertTrue(call.kwargs["use_kv_cache"])

    def test_generate_entry_uses_the_mapping(self) -> None:
        import inspect

        source = inspect.getsource(worker.generate)
        self.assertIn("prepare_still_call", source)
        self.assertNotIn("num_inference_steps", source)
        self.assertIn("output_mode", source)

    def test_served_still_page_and_h3_controls(self) -> None:
        client = TestClient(app.app)
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        html = page.text
        steps = re.search(r'<input id="steps"[^>]*>', html)
        self.assertIsNotNone(steps)
        self.assertIn('value="8"', steps.group(0))
        self.assertIn("Qwen-Image-2.1-Turbo", html)
        self.assertIn("8-step", html)
        self.assertIn("compose-card collapsed", html)
        self.assertIn("compose-header", html)
        self.assertIn("appbarComposeBtn", html)
        self.assertIn("compose-collapsible", html)

        script = client.get("/static/app.js")
        self.assertEqual(script.status_code, 200)
        js = script.text
        self.assertIn('Number($("steps").value) || 8', js)
        self.assertNotIn('Number($("steps").value) || 28', js)
        self.assertIn("const DURATIONS = [5, 6, 10];", js)
        self.assertIn('{ id: "fast", label: "Fast" }', js)
        self.assertIn('{ id: "768p", label: "768p" }', js)
        self.assertIn("setComposeExpanded", js)
        self.assertIn("toggleComposePanel", js)
        self.assertIn("composeExpanded = false", js)
        self.assertEqual(app.H3_RESOLUTIONS, ("fast", "768p"))
        animate = app.AnimateBody(image_id="frame")
        self.assertEqual(animate.duration, 5)
        self.assertEqual(animate.resolution, "fast")

        readme = (ROOT / "README.md").read_text()
        launch = (ROOT / "run_worker.sh").read_text()
        self.assertIn("Qwen-Image-2.1-Turbo", readme)
        self.assertIn("8-step", readme)
        self.assertNotIn("28 steps", readme)
        self.assertIn('IMAGINE_MODEL:-/home/dkoh/models/Qwen-Image-2.1-Turbo', launch)
        self.assertIn("/models/Qwen-Image-2.1-Turbo", launch)
        self.assertIn("Qwen/Qwen-Image-2.1-Turbo", launch)
        self.assertIn("transformers>=5.17.0", launch)
        self.assertIn("sample_sigmas", launch)
        self.assertIn('if (it.kind !== "video" && it.image) return it.image;', js)
        self.assertIn("app.js?v=105", html)

    def test_still_gallery_thumb_uses_the_original_image(self) -> None:
        client = TestClient(app.app)
        still_id = "stillorig01"
        video_id = "videothumb1"
        still_png = app.IMAGES / f"{still_id}.png"
        poster_png = app.IMAGES / f"{video_id}.png"
        video_mp4 = app.VIDEOS / f"{video_id}.mp4"
        Image.new("RGB", (896, 1152), (20, 40, 60)).save(still_png)
        Image.new("RGB", (768, 1344), (60, 40, 20)).save(poster_png)
        video_mp4.write_bytes(b"not-a-real-mp4")
        app._write_job(still_id, {
            "id": still_id,
            "status": "done",
            "kind": "image",
            "prompt": "a red apple",
            "width": 896,
            "height": 1152,
            "created_at": 10,
        })
        app._write_job(video_id, {
            "id": video_id,
            "status": "done",
            "kind": "video",
            "prompt": "the apple turns",
            "created_at": 11,
        })
        try:
            items = {
                item["id"]: item
                for item in client.get("/api/gallery", headers={"X-Imagine-Pin": "haku"}).json()["items"]
            }
            self.assertEqual(items[still_id]["thumb"], f"/api/images/{still_id}.png")
            self.assertEqual(items[still_id]["image"], f"/api/images/{still_id}.png")
            self.assertEqual(items[video_id]["thumb"], f"/api/thumbs/{video_id}.jpg")
        finally:
            for path in (
                still_png,
                poster_png,
                video_mp4,
                app.JOBS / f"{still_id}.json",
                app.JOBS / f"{video_id}.json",
            ):
                path.unlink(missing_ok=True)
            app._bump_gallery()


if __name__ == "__main__":
    unittest.main(verbosity=2)
