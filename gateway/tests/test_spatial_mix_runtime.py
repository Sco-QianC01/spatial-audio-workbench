"""Real local mix API: immutable sources, bounded plans, restore and playback files."""
import asyncio
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_audio import NATIVE_SCHEMA, canonical_hash, default_scene
from gateway.tests.test_spatial_native_workflow import fixture_bundle
from spatial_runtime import create_app

spec = importlib.util.spec_from_file_location("mix_agent", Path(__file__).resolve().parents[2] /
                                             "tools/spatial-mix-agent.py")
mix_agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mix_agent)


class MixRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pid = "a" * 32
        self.folder = self.root / self.pid
        (self.folder / "stems").mkdir(parents=True)
        rate, frames = 44100, 44100 * 3
        t = np.arange(frames) / rate
        stem = np.column_stack((.08 * np.sin(2 * np.pi * 440 * t),
                                .07 * np.sin(2 * np.pi * 660 * t))).astype("float32")
        ids = ('drums', 'bass', 'guitar', 'vocals')
        native = {'schema': NATIVE_SCHEMA, 'fixture': True,
                  'context': {'file': 'context.wav'},
                  'tracks': [{'id': sid, 'file': f'stems/{sid}.wav',
                              'task': {'responseFile': f'receipts/{sid}.json'}} for sid in ids]}
        summed = np.zeros_like(stem, dtype=np.float64)
        for index, sid in enumerate(ids):
            data = (stem if sid == 'guitar' else np.column_stack(
                (np.sin(2*np.pi*(100+index*130)*t),)*2).astype('float32')*.002)
            sf.write(self.folder / 'stems' / f'{sid}.wav', data, rate, subtype='FLOAT')
            summed += data
        sf.write(self.folder / 'original.wav', summed, rate, subtype='FLOAT')
        (self.folder / 'stems/separation.json').write_text(json.dumps(native))
        project = {"id": self.pid, "state": "ready", "createdAt": 1, "error": None,
                   "input": {"name": "local-test.wav", "durationSeconds": 3,
                             "sampleRate": rate, "channels": 2, "frames": frames,
                             "audioUrl": f"/api/spatial/projects/{self.pid}/files/original.wav"},
                   "separation": native, "nativeStems": native,
                   "source": {"kind": "ace-native-stems", "manifestHash": canonical_hash(native)},
                   "progress": {"stage": "separated", "fraction": 1}}
        project["scene"] = default_scene(project)
        project["sceneHash"] = canonical_hash(project["scene"])
        (self.folder / "project.json").write_text(json.dumps(project))
        self.original_hash = hashlib.sha256((self.folder / "original.wav").read_bytes()).hexdigest()
        self.env = patch.dict(os.environ, {
            "PATH": str(Path.home() / ".local/bin") + os.pathsep + os.environ.get("PATH", "")})
        self.env.start()
        self.client = TestClient(TestServer(create_app(self.root, sys.executable,
                                                     self.root / "no-weights")))
        await self.client.start_server()
        self.base = f"/api/spatial/projects/{self.pid}"

    async def asyncTearDown(self):
        await self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    async def completed(self):
        for _ in range(150):
            response = await self.client.get(self.base)
            value = await response.json()
            if value["state"] not in ("analyzing", "mixing"):
                self.assertNotEqual(value["state"], "error", value.get("error"))
                return value
            await asyncio.sleep(.1)
        self.fail("local mix task did not finish within bounded test wait")

    async def test_plan_requires_real_analysis(self):
        response = await self.client.post(self.base + "/mix/plan", json={"intent": "preserve"})
        self.assertEqual(response.status, 400)

    async def test_stale_scene_save_and_mix_apply_cannot_overwrite_newer_edit(self):
        before = await (await self.client.get(self.base)).json()
        scene = json.loads(json.dumps(before["scene"]))
        scene["objects"][2]["azimuthDeg"] = 47
        response = await self.client.post(self.base + "/scene", json={
            "scene": scene, "expectedSceneHash": before["sceneHash"]})
        self.assertEqual(response.status, 200)
        winner = await response.json()
        for endpoint, payload in (("/scene", {"scene": before["scene"]}),
                                  ("/mix/apply", {"mixId": None})):
            response = await self.client.post(self.base + endpoint, json={
                **payload, "expectedSceneHash": before["sceneHash"]})
            self.assertEqual(response.status, 409)
            after = await (await self.client.get(self.base)).json()
            self.assertEqual(after["scene"], winner["scene"])
            self.assertEqual(after["sceneHash"], winner["sceneHash"])

    async def test_agent_executes_real_tools_and_reuses_candidate_without_selecting_it(self):
        before = await (await self.client.get(self.base)).json()
        tools = mix_agent.LocalTools(str(self.client.make_url("/")).rstrip("/"))
        args = argparse.Namespace(project=self.pid, input=None, intent="preserve", focus=[("0", "2", "主体")],
                                  reference_project=None, preview_3d=False)
        records = []
        for index in range(2):
            runner = mix_agent.Coordinator(tools, self.root / f"agent-{index}", 30)
            record = await asyncio.to_thread(runner.execute, args)
            records.append(record)
            self.assertEqual(record["state"], "awaiting-listening")
            self.assertIsNone(record["selectedMix"])
            self.assertTrue(record["technicalChecks"]["sourceHashUnchanged"])
            self.assertTrue(record["technicalChecks"]["loudnessMatched"])
            self.assertFalse(record["technicalChecks"]["humanListeningVerified"])
            for item in record["files"]:
                content = (runner.output / item["file"]).read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
            self.assertEqual(sf.info(runner.output / "candidate_matched.wav").frames, 44100 * 3)
        after = await (await self.client.get(self.base)).json()
        self.assertFalse(records[0]["reusedCandidate"])
        self.assertTrue(records[1]["reusedCandidate"])
        self.assertEqual(records[0]["mixId"], records[1]["mixId"])
        self.assertEqual(after["scene"], before["scene"])
        self.assertEqual(len(after["mixRuns"]), 1)

    async def test_agent_import_keeps_native_bundle_identity_separate_from_working_sum(self):
        tools = mix_agent.LocalTools(str(self.client.make_url("/")).rstrip("/"))
        path = self.root / "synthetic-native-fixture.zip"
        path.write_bytes(fixture_bundle()[0])
        bundle_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        imported = await asyncio.to_thread(tools.import_file, path)
        self.assertEqual(imported["state"], "ready")
        self.assertEqual(imported["importedFile"]["sha256"], bundle_hash)
        self.assertEqual(imported["importedFile"]["bytes"], path.stat().st_size)
        self.assertEqual(imported["input"]["frames"], 8820)
        self.assertNotEqual(imported["id"], self.pid)

    async def test_mix_request_size_and_unknown_reference_are_rejected(self):
        response = await self.client.post(self.base + "/mix/analyze",
                                         data=b'{"unused":"' + b"x" * 65536 + b'"}')
        self.assertEqual(response.status, 400)
        self.assertIn("64KiB", (await response.json())["error"])
        response = await self.client.post(self.base + "/mix/analyze",
                                         json={"referenceProjectId": "../outside"})
        self.assertEqual(response.status, 400)

    async def test_slow_apply_body_cannot_change_scene_after_analysis_starts(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_body():
            yield b'{"mixId":'
            started.set()
            await release.wait()
            yield b'null}'

        pending = asyncio.create_task(self.client.post(self.base + "/mix/apply", data=slow_body(),
                                                      headers={"Content-Type": "application/json"}))
        await started.wait()
        await asyncio.sleep(.05)
        response = await self.client.post(self.base + "/mix/analyze", json={})
        self.assertEqual(response.status, 202)
        release.set()
        response = await pending
        self.assertEqual(response.status, 409)
        await self.completed()

    async def test_analyze_process_playback_apply_and_revert(self):
        response = await self.client.post(self.base + "/mix/analyze", json={})
        self.assertEqual(response.status, 202)
        analyzed = await self.completed()
        self.assertEqual(analyzed["analysis"]["schema"], "spatial-analysis-v1")
        self.assertIn("truePeakDbTP", analyzed["analysis"]["original"])
        response = await self.client.post(self.base + "/mix/plan", json={"intent": "preserve"})
        self.assertEqual(response.status, 200)
        proposed = await response.json()
        plan = proposed["mixPlan"]
        self.assertEqual(plan["schema"], "spatial-mix-plan-v1")
        plan["stems"][2]["gainDb"] = -1.5
        injected = {**plan, "shell": "unexpected"}
        response = await self.client.post(self.base + "/mix/process", json={"plan": injected})
        self.assertEqual(response.status, 400)
        response = await self.client.post(self.base + "/mix/process", json={"plan": plan})
        self.assertEqual(response.status, 202)
        processed = await self.completed()
        self.assertEqual(processed["mixPlan"], plan)
        run = processed["mixRuns"][-1]
        self.assertEqual(processed["mixResultId"], run["id"])
        self.assertEqual(run["status"], "awaiting-listening")
        self.assertNotIn("mixId", processed["scene"])
        response = await self.client.post(self.base + "/mix/process", json={"plan": plan})
        self.assertEqual(response.status, 200)
        self.assertEqual(len((await response.json())["mixRuns"]), 1)
        second = json.loads(json.dumps(plan))
        second["stems"][2]["gainDb"] = -2
        response = await self.client.post(self.base + "/mix/process", json={"plan": second})
        self.assertEqual(response.status, 202)
        newer = await self.completed()
        self.assertNotEqual(newer["mixResultId"], run["id"])
        response = await self.client.post(self.base + "/mix/process", json={"plan": plan})
        reused = await response.json()
        self.assertEqual(reused["mixResultId"], run["id"])
        self.assertEqual(len(reused["mixRuns"]), 2)
        self.assertIn("baselineMono", run["urls"])
        for name in ("baseline_matched.wav", "candidate_matched.wav", "baseline_mono.wav",
                     "candidate_mono.wav", "mix.json"):
            response = await self.client.get(f"{self.base}/files/mixes/{run['id']}/{name}")
            self.assertEqual(response.status, 200, name)
            self.assertGreater(len(await response.read()), 100)
        response = await self.client.get(f"{self.base}/files/mixes/{run['id']}/plan.py")
        self.assertEqual(response.status, 404)
        response = await self.client.post(self.base + "/mix/apply",
                                         json={"mixId": run["id"], "layout": "preserve"})
        self.assertEqual(response.status, 200)
        applied = await response.json()
        self.assertEqual(applied["scene"]["mixId"], run["id"])
        self.assertIn("processed_stems", applied["scene"]["objects"][2]["audioUrl"])
        self.assertNotEqual(applied["sceneHash"], processed["sceneHash"])
        analysis_file = self.folder / "mixes" / run["id"] / "analysis.json"
        analysis_bytes = analysis_file.read_bytes()
        analysis_file.write_text('{"tampered":true}')
        response = await self.client.post(self.base + "/mix/apply",
                                         json={"mixId": run["id"], "layout": "content"})
        self.assertEqual(response.status, 400)
        unchanged = await (await self.client.get(self.base)).json()
        self.assertEqual(unchanged["sceneHash"], applied["sceneHash"])
        analysis_file.write_bytes(analysis_bytes)
        response = await self.client.post(self.base + "/mix/apply",
                                         json={"mixId": run["id"], "layout": "content"})
        self.assertEqual(response.status, 200)
        content = await response.json()
        self.assertEqual(content["contentPlan"]["analysisHash"], run["analysisHash"])
        self.assertEqual(content["contentPlan"]["sceneHash"], content["sceneHash"])
        self.assertEqual(content["contentPlan"]["mixId"], run["id"])
        self.assertTrue(any("处理前" in item for item in content["contentPlan"]["warnings"]))
        self.assertEqual(next(item for item in content["contentPlan"]["objects"]
                              if item["id"] == "guitar")["role"], "front-primary")
        response = await self.client.post(self.base + "/mix/apply",
                                         json={"mixId": run["id"], "layout": "spacious"})
        self.assertEqual(response.status, 200)
        spacious = await response.json()
        self.assertEqual(spacious["contentPlan"]["layout"], "spacious")
        self.assertEqual(spacious["contentPlan"]["analysisHash"], run["analysisHash"])
        self.assertEqual(spacious["contentPlan"]["sceneHash"], spacious["sceneHash"])
        self.assertIn("expandedObjectCount", spacious["contentPlan"])
        for before, after in zip(content["scene"]["objects"], spacious["scene"]["objects"]):
            for key in ("id", "audioUrl", "gainDb", "muted"):
                self.assertEqual(before[key], after[key])
        response = await self.client.post(self.base + "/mix/apply", json={"mixId": None})
        self.assertEqual(response.status, 200)
        reverted = await response.json()
        self.assertNotIn("mixId", reverted["scene"])
        self.assertEqual(reverted["scene"], processed["scene"])
        self.assertNotIn("contentPlan", reverted)
        self.assertEqual(hashlib.sha256((self.folder / "original.wav").read_bytes()).hexdigest(),
                         self.original_hash)


if __name__ == "__main__":
    unittest.main()
