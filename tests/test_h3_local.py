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
        self.assertIn("imagine_gallery_cols", js)
        self.assertIn('id="galleryCols"', html)
        self.assertIn(">10<", html)
        self.assertIn(">15<", html)
        self.assertIn(">20<", html)
        self.assertIn("statusRate", html)

    def test_h3_lock_encloses_subprocess_execution(self) -> None:
        runner = inspect.getsource(app._run_animate)
        lock_idx = runner.find("async with _h3_lock:")
        subproc_idx = runner.find("create_subprocess_exec")
        poster_idx = runner.find("_save_poster")
        self.assertGreater(lock_idx, -1)
        self.assertGreater(subproc_idx, lock_idx)
        self.assertGreater(poster_idx, subproc_idx)

    def test_active_job_tracking_and_gallery_bump(self) -> None:
        jid = "testact001"
        data = {"id": jid, "kind": "image", "status": "queued", "created_at": 100.0}
        app._write_job(jid, data, bump_gallery=True)
        self.assertIn(jid, app._active_jobs)
        self.assertEqual(app._active_job()["id"], jid)

        # Queue stats ahead calculation
        jid2 = "testact002"
        data2 = {"id": jid2, "kind": "image", "status": "running", "created_at": 200.0}
        app._write_job(jid2, data2, bump_gallery=False)
        self.assertEqual(app._queue_stats(jid2, data2)["queue_ahead"], 1)

        # Intermediate touch without bump
        gen_before = app._gallery_gen
        data2["pct"] = 50
        app._write_job(jid2, data2, bump_gallery=False)
        self.assertEqual(app._gallery_gen, gen_before)

        # Done removes from active jobs
        data2["status"] = "done"
        data2["elapsed_s"] = 5.2
        app._write_job(jid2, data2, bump_gallery=True)
        self.assertNotIn(jid2, app._active_jobs)
        self.assertGreater(app._gallery_gen, gen_before)

        # Cleanup test jobs
        (app.JOBS / f"{jid}.json").unlink(missing_ok=True)
        (app.JOBS / f"{jid2}.json").unlink(missing_ok=True)
        app._active_jobs.pop(jid, None)
        app._active_jobs.pop(jid2, None)

    def test_telemetry_module_integration(self) -> None:
        import telemetry
        for fn in ("_cpu_percent", "_cpu_temp_c", "_gpu_stats", "_meminfo", "_net_bandwidth", "_docker_running", "_ram_gb"):
            self.assertTrue(hasattr(telemetry, fn))
            self.assertTrue(hasattr(app, fn))
        self.assertFalse(hasattr(app, "_gpu_percent"))
        self.assertFalse(hasattr(app, "_gpu_temp_c"))
