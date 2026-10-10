"""Local h3 animate contract. No weights and no h3 binary required."""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_loaded = sys.modules.get("app")
if _loaded is not None:
    os.environ["IMAGINE_DATA"] = str(_loaded.DATA)
else:
    os.environ["IMAGINE_DATA"] = tempfile.mkdtemp(prefix="imagine-h3-")
os.environ["IMAGINE_PIN"] = "haku"

import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class LocalH3AnimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app.app)
        self.client.__enter__()
        self.headers = {"X-Imagine-Pin": "haku"}

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_animate_requires_a_still_and_writes_no_job_when_h3_is_down(self) -> None:
        before = {path.name for path in app.JOBS.glob("*.json")}
        missing = self.client.post(
            "/api/animate",
            headers=self.headers,
            json={"prompt": "a breeze", "duration": 5, "resolution": "fast"},
        )
        self.assertEqual(missing.status_code, 422, missing.text)
        self.assertEqual({path.name for path in app.JOBS.glob("*.json")}, before)

        refused = self.client.post(
            "/api/animate",
            headers=self.headers,
            json={
                "prompt": "a breeze",
                "image_id": "missingstill",
                "duration": 5,
                "resolution": "fast",
            },
        )
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("h3", refused.json()["detail"].lower())
        self.assertEqual({path.name for path in app.JOBS.glob("*.json")}, before)

    def test_route_uses_local_h3_first_frame(self) -> None:
        route = inspect.getsource(app.api_animate)
        runner = inspect.getsource(app._run_animate)
        command = inspect.getsource(app._h3_command)
        self.assertIn("_run_animate", route)
        self.assertIn('model": "h3-local"', route)
        self.assertNotIn("fasth3", route)
        self.assertNotIn("fastvideo", runner)
        self.assertIn("--first-frame", command)
        self.assertIn("_h3_command", runner)

    def test_status_keeps_the_model_label_and_local_h3_readiness(self) -> None:
        status = self.client.get("/api/status").json()
        self.assertIn(status["model_state"], ("ready", "loading", "down"))
        self.assertTrue(status["model_label"])
        self.assertIn("h3_video", status)
        self.assertIn("h3_detail", status)
        self.assertNotIn("video_label", status)
        self.assertFalse(status["h3_video"])

    def test_served_copy_names_local_h3(self) -> None:
        html = (ROOT / "static" / "index.html").read_text()
        js = (ROOT / "static" / "app.js").read_text()
        self.assertIn("Local H3 uses this image as the first frame", html)
        self.assertIn("Drop to animate with local H3", html)
        self.assertIn("Local H3 uses this image as the first frame", js)
        self.assertNotIn("FastH3", html)
        self.assertNotIn("FastH3", js)
        self.assertIn("function footerModel", js)
        self.assertIn("imagine_page_size", js)
        self.assertIn("statusRate", html)
