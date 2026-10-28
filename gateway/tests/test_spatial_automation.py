import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_audio import angles, default_scene, render_foa, validate_scene


class AutomationTests(unittest.TestCase):
    def test_dense_write_curve_roundtrips_and_matches_shortest_arc(self):
        project = {"id": "a"*32, "input": {"durationSeconds": 600}}
        scene = default_scene(project)
        obj = scene["objects"][0]
        obj["keyframes"] = [
            {"timeSeconds": i/20, "azimuthDeg": (i/20*2+170+180) % 360-180,
             "elevationDeg": 20*np.sin(i/200)}
            for i in range(12001)]
        before = copy.deepcopy(scene["objects"][1:])
        restored = validate_scene(json.loads(json.dumps(scene)), project)
        self.assertEqual(restored["objects"][0]["keyframes"], obj["keyframes"])
        self.assertEqual(restored["objects"][1:], before)
        az, el = angles(restored["objects"][0], np.array([0, 1, 60, 599]))
        np.testing.assert_allclose(np.degrees(az), [170, 172, 290, 1368], atol=1e-6)
        self.assertTrue(np.isfinite(el).all())
        obj["keyframes"].extend([dict(obj["keyframes"][-1])]*2)
        with self.assertRaisesRegex(ValueError, "12002"):
            validate_scene(scene, project)

    def test_moving_curve_changes_actual_channel_feeds(self):
        project = {"id": "a"*32, "input": {"durationSeconds": .3}}
        scene = default_scene(project)
        scene["speakerLayout"] = "7.1.4"
        for obj in scene["objects"]:
            obj.update(muted=obj["id"] != "other", widthDeg=0)
        scene["objects"][2]["keyframes"] = [
            {"timeSeconds": 0, "azimuthDeg": 0, "elevationDeg": 0},
            {"timeSeconds": .1, "azimuthDeg": 0, "elevationDeg": 0},
            {"timeSeconds": .2, "azimuthDeg": 180, "elevationDeg": 0},
            {"timeSeconds": .3, "azimuthDeg": 180, "elevationDeg": 0}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wave = .05*np.sin(2*np.pi*440*np.arange(13230)/44100)
            for obj in scene["objects"]:
                sf.write(root/(obj["id"]+".wav"), np.column_stack((wave, wave)), 44100, subtype="FLOAT")
            receipt = render_foa(validate_scene(scene, project), root, root/"render")
            feeds, rate = sf.read(root/"render/surround_7_1_4.wav", always_2d=True)
            front = np.mean(feeds[1000:4000]**2, axis=0)
            rear = np.mean(feeds[11000:14000]**2, axis=0)
            self.assertEqual(rate, 48000)
            self.assertGreater(front[2], front[6]+front[7])
            self.assertGreater(rear[6]+rear[7], rear[2])
            self.assertEqual(receipt["sceneHash"], json.loads((root/"render/render.json").read_text())["sceneHash"])

    def test_truncated_stem_cannot_silently_become_a_valid_timeline(self):
        project = {"id": "a"*32, "input": {"durationSeconds": .1}}
        scene = default_scene(project)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i, obj in enumerate(scene["objects"]):
                sf.write(root/(obj["id"]+".wav"), np.full((2205 if i == 0 else 4410, 2), .01),
                         44100, subtype="FLOAT")
            with self.assertRaisesRegex(ValueError, "时间线"):
                render_foa(scene, root, root/"render")
