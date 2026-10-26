import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase
from spatial_runtime import create_app

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("spatial_launcher", ROOT / "run.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.weights = self.root / "weights.pt"
        self.weights.touch()
        self.ffmpeg = self.root / "ffmpeg"
        self.ffmpeg.touch()
        self.ffmpeg.chmod(0o755)
        self.args = argparse.Namespace(weights=str(self.weights), ffmpeg=str(self.ffmpeg),
                                       output=None, downloads=None, port=8773)

    def test_config_validates_paths_and_reuses_only_local_path_keys(self):
        with patch.object(launcher, "ROOT", self.root), \
                patch.object(launcher, "CONFIG", self.root / "config.json"):
            paths = launcher.configuration(self.args)
            self.assertEqual(paths["output"], self.root / ".runtime" / "projects")
            self.args.weights = str(self.root / "missing.pt")
            with self.assertRaisesRegex(ValueError, "weights"):
                launcher.configuration(self.args)
            self.args.weights = str(self.weights)
            self.args.output = str(self.weights)
            with self.assertRaisesRegex(ValueError, "directory"):
                launcher.configuration(self.args)
            self.args.output = None
            config = self.root / "config.json"
            config.write_text(json.dumps({"weights": str(self.weights), "token": "not-allowed"}))
            with self.assertRaisesRegex(ValueError, "configuration"):
                launcher.configuration(self.args)
            config.write_text(json.dumps({"weights": str(self.weights), "ffmpeg": str(self.ffmpeg)}))
            self.args.weights = self.args.ffmpeg = None
            self.assertEqual(launcher.configuration(self.args)["weights"], self.weights.resolve())

    def test_requires_own_private_venv(self):
        with patch.object(launcher, "ROOT", self.root):
            with self.assertRaisesRegex(ValueError, ".venv"):
                launcher.require_environment()
            venv = self.root / ".venv"
            venv.mkdir()
            cfg = venv / "pyvenv.cfg"
            cfg.write_text("include-system-site-packages = true\n")
            with patch.object(sys, "prefix", str(venv)):
                with self.assertRaisesRegex(ValueError, ".venv"):
                    launcher.require_environment()
                cfg.write_text("include-system-site-packages = false\n")
                launcher.require_environment()

    def test_child_environment_drops_python_injection(self):
        with patch.dict(os.environ, {"PYTHONPATH": "/other/repo", "PYTHONHOME": "/other/python",
                                     "PythonPath": "/case-insensitive", "KEEP_ME": "yes"}):
            env = launcher.clean_environment()
        self.assertFalse(any(key.upper() in ("PYTHONPATH", "PYTHONHOME") for key in env))
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["KEEP_ME"], "yes")

    def test_background_refuses_foreign_app_without_spawning_or_killing(self):
        with patch.object(launcher, "port_occupied", return_value=True), \
                patch.object(launcher, "status", return_value={"app": "other"}), \
                patch.object(launcher.subprocess, "Popen") as process:
            with self.assertRaisesRegex(ValueError, "another application"):
                launcher.background(self.args, {})
            process.assert_not_called()
        with self.assertRaisesRegex(ValueError, "different checkout"):
            launcher.check_instance({"app": launcher.APP, "environment": {"sourceRoot": "/old/repo"}})

    def test_readiness_requires_environment_and_both_engines(self):
        data = {"app": launcher.APP, "environment": {"sourceRoot": str(ROOT), "isolated": True},
                "ready": True, "separation": {"ready": True}, "mixing": {"ready": False}}
        self.assertFalse(launcher.check_instance(data))
        data["mixing"]["ready"] = True
        self.assertTrue(launcher.check_instance(data))
        data["environment"]["isolated"] = False
        self.assertFalse(launcher.check_instance(data))


class EnvironmentContractTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        return create_app(Path(self.temp.name), "/missing/python", "/missing/weights")

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp.cleanup()

    async def test_status_reports_actual_environment_and_static_boundary(self):
        data = await (await self.client.get("/api/spatial/status")).json()
        self.assertEqual(data["version"], "0.8.3")
        self.assertEqual(data["environment"]["sourceRoot"], str(ROOT))
        self.assertEqual(data["environment"]["python"], sys.executable)
        self.assertEqual(data["environment"]["prefix"], sys.prefix)
        self.assertEqual(data["environment"]["separationPython"], "/missing/python")
        self.assertFalse(data["environment"]["isolated"])
        self.assertEqual((await self.client.get("/public/spatial-icon.svg")).status, 200)
        for path in ("music-prompt/style.css", "music-prompt/icon.svg", "run.py", ".runtime/config.json"):
            self.assertEqual((await self.client.get("/" + path)).status, 404)


if __name__ == "__main__":
    unittest.main()
