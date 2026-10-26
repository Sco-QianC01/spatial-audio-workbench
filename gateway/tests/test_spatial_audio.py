import json
import asyncio
import io
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_audio import (IDS, RATE, angles, canonical_hash, default_scene,
                           render_foa, sn3d_coefficients, validate_scene)
from spatial_surround import STEREO_GEOMETRY
from spatial_runtime import artifacts, create_app, package, verify_binaural
from aiohttp.test_utils import AioHTTPTestCase

FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    raise RuntimeError("Spatial audio tests require ffmpeg on PATH; install ffmpeg before running this suite.")


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.project = {"id": "a" * 32, "input": {"durationSeconds": 0.1}}
        self.scene = default_scene(self.project)

    def test_foa_null_does_not_reject_nonzero_discrete_speakers(self):
        self.scene['speakerLayout'] = '7.1.4'
        wave = .1*np.sin(2*np.pi*440*np.arange(4410)/44100)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for obj, (angle, polarity) in zip(self.scene['objects'], ((0, 1), (90, -1), (180, 1), (-90, -1))):
                obj.update(azimuthDeg=angle, elevationDeg=0, widthDeg=0)
                sf.write(root/(obj['id']+'.wav'), np.column_stack((wave, wave))*polarity, 44100, subtype='FLOAT')
            receipt = render_foa(self.scene, root, root/'render')
            feeds, _ = sf.read(root/'render/surround_7_1_4.wav', always_2d=True)
            self.assertLess(receipt['audio']['peak'], 1e-10)
            self.assertGreater(np.max(np.abs(feeds)), .1)
            self.assertTrue(np.isfinite(feeds).all())

    def test_default_bass_preserves_antiphase_and_manual_zero_reports_fold_loss(self):
        self.scene['speakerLayout'] = '7.1.4'
        bass = self.scene['objects'][1]
        self.assertEqual(bass['widthDeg'], 30)
        wave = .1*np.sin(2*np.pi*440*np.arange(4410)/44100)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for obj in self.scene['objects']:
                obj['muted'] = obj['id'] != 'bass'
                sf.write(root/(obj['id']+'.wav'), np.column_stack((wave, -wave if obj['id']=='bass' else wave)),
                         44100, subtype='FLOAT')
            receipt = render_foa(self.scene, root, root/'default')
            feeds, _ = sf.read(root/'default/surround_7_1_4.wav', always_2d=True)
            self.assertGreater(np.max(np.abs(feeds)), .01)
            diagnostic = next(d for d in receipt['stereoFoldDown'] if d['id']=='bass')
            self.assertFalse(diagnostic['applied'])
            self.assertTrue(diagnostic['fullyCancelled'])
            self.assertFalse(diagnostic['warning'])
            bass['widthDeg'] = 0
            self.scene['objects'][0]['muted'] = False
            receipt = render_foa(self.scene, root, root/'manual-zero')
            diagnostic = next(d for d in receipt['stereoFoldDown'] if d['id']=='bass')
            self.assertTrue(diagnostic['applied'])
            self.assertTrue(diagnostic['fullyCancelled'])
            self.assertIsNone(diagnostic['foldDownLossDb'])
            self.assertTrue(diagnostic['warning'])
            self.assertGreater(diagnostic['sideMeanSquare'], 0)
            self.assertEqual(diagnostic['midMeanSquare'], 0)
            centered = next(d for d in receipt['stereoFoldDown'] if d['id']=='drums')
            self.assertEqual(centered['foldDownEnergyRatio'], 1)
            self.assertEqual(centered['foldDownLossDb'], 0)
            self.assertFalse(centered['warning'])
            self.assertEqual(bass['widthDeg'], 0)
            json.dumps(receipt, allow_nan=False)

    def test_great_circle_stereo_width_is_constant_at_poles_and_matches_horizontal_mapping(self):
        obj = self.scene['objects'][0]
        obj.update(keyframes=[], widthDeg=60)
        for elevation in (-90, -89, -45, 0, 45, 89, 90):
            obj.update(azimuthDeg=25, elevationDeg=elevation)
            left = sn3d_coefficients(*angles(obj, np.array([0.]), 30))[0, 1:]
            right = sn3d_coefficients(*angles(obj, np.array([0.]), -30))[0, 1:]
            np.testing.assert_allclose([np.linalg.norm(left), np.linalg.norm(right)], 1, atol=1e-7)
            self.assertAlmostEqual(float(np.dot(left, right)), .5, places=6)
            if elevation == 0:
                az, el = angles(obj, np.array([0.]), 30)
                np.testing.assert_allclose(np.degrees(az), [55], atol=1e-7)
                np.testing.assert_allclose(el, [0], atol=1e-7)
        obj['keyframes'] = [
            {'timeSeconds': 0, 'azimuthDeg': 170, 'elevationDeg': 80},
            {'timeSeconds': .05, 'azimuthDeg': -170, 'elevationDeg': 90},
            {'timeSeconds': .1, 'azimuthDeg': 170, 'elevationDeg': 80}]
        times = np.linspace(0, .1, 1001)
        left = sn3d_coefficients(*angles(obj, times, 30))[:, 1:]
        right = sn3d_coefficients(*angles(obj, times, -30))[:, 1:]
        np.testing.assert_allclose(np.sum(left*right, axis=1), .5, atol=1e-7)
        self.assertLess(float(np.max(np.abs(np.diff(left, axis=0)))), .002)

    def test_nonzero_width_preserves_antiphase_at_both_poles_in_foa_and_speakers(self):
        wave = .1*np.sin(2*np.pi*440*np.arange(4410)/44100)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for obj in self.scene['objects']:
                obj.update(muted=obj['id'] != 'bass', widthDeg=60, azimuthDeg=0)
                sf.write(root/(obj['id']+'.wav'), np.column_stack((wave, -wave)), 44100, subtype='FLOAT')
            for layout in ('5.1.2', '7.1.4'):
                self.scene['speakerLayout'] = layout
                for elevation in (-90, 90):
                    self.scene['objects'][1]['elevationDeg'] = elevation
                    dest = root/f'{layout}-{elevation}'
                    receipt = render_foa(self.scene, root, dest)
                    feeds, _ = sf.read(dest/('surround_'+layout.replace('.', '_')+'.wav'), always_2d=True)
                    self.assertGreater(receipt['audio']['peak'], .03)
                    self.assertGreater(float(np.max(np.abs(feeds))), .03)
                    self.assertEqual(receipt['stereoGeometry'], STEREO_GEOMETRY)
                    self.assertEqual(receipt['renderSources'][0]['greatCircleOffsetDeg'], 30)

    def test_untrusted_scene_and_source_urls(self):
        self.scene["objects"][0]["audioUrl"] = "file:///private"
        self.assertNotIn("private", json.dumps(validate_scene(self.scene, self.project)))
        for invalid in (float("nan"), float("inf"), True, "0", 181):
            self.scene["objects"][0]["azimuthDeg"] = invalid
            with self.assertRaises(ValueError):
                validate_scene(self.scene, self.project)

    def test_trajectory_order_and_shortest_arc(self):
        obj = self.scene["objects"][0]
        obj["keyframes"] = [{"timeSeconds": 0, "azimuthDeg": 179, "elevationDeg": 0},
                            {"timeSeconds": 0.1, "azimuthDeg": -179, "elevationDeg": 30}]
        validate_scene(self.scene, self.project)
        az, el = angles(obj, np.array([0.05]))
        np.testing.assert_allclose(np.cos(az), [-1], atol=1e-7)
        np.testing.assert_allclose(np.degrees(el), [15])
        obj["keyframes"][1]["timeSeconds"] = 0
        with self.assertRaises(ValueError):
            validate_scene(self.scene, self.project)

    def test_acn_sn3d_axes(self):
        result = sn3d_coefficients(np.radians([0, 90, 0, 180]), np.radians([0, 0, 90, 0]))
        np.testing.assert_allclose(result, [[1, 0, 0, 1], [1, 1, 0, 0],
                                            [1, 0, 1, 0], [1, 0, 0, -1]], atol=1e-6)

    def test_half_turn_matches_browser_negative_tie(self):
        obj = self.scene["objects"][0]
        for start, end, midpoint in ((0, 180, -90), (0, -180, -90),
                                     (179, -179, 180), (-179, 179, -180)):
            obj["keyframes"] = [
                {"timeSeconds": 0, "azimuthDeg": start, "elevationDeg": 0},
                {"timeSeconds": .1, "azimuthDeg": end, "elevationDeg": 0}]
            az, _ = angles(obj, np.array([.05]))
            np.testing.assert_allclose(np.degrees(az), [midpoint])

    def test_real_four_channel_output_and_group_gain(self):
        for o in self.scene["objects"]:
            o.update(muted=o["id"] != "bass", azimuthDeg=90, elevationDeg=0, widthDeg=0)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for sid in IDS:
                sf.write(root / f"{sid}.wav", np.full((4410, 2), 0.1, np.float32),
                         44100, subtype="FLOAT")
            receipt = render_foa(self.scene, root, root / "render")
            audio, rate = sf.read(root / "render/foa.wav", always_2d=True)
            self.assertEqual((rate, audio.shape), (RATE, (16800, 4)))
            np.testing.assert_allclose(audio[2400, :2], [0.1 * 2 ** 0.5] * 2, atol=1e-4)
            np.testing.assert_allclose(audio[2400, 2:], [0, 0], atol=1e-6)
            self.assertEqual(receipt["normalization"], "SN3D")
            self.assertEqual(receipt["mixHeadroomGain"], 1)
            self.assertEqual(receipt["sceneHash"], canonical_hash(self.scene))
            self.assertEqual(receipt["timeline"]["renderTailSeconds"], .25)
            np.testing.assert_array_equal(audio[-2400:], 0)
            self.assertEqual(len(list((root / "render/objects").glob("*.wav"))), 8)
            with self.assertRaises(FileExistsError):
                render_foa(self.scene, root, root / "render")

    def test_foa_retains_end_impulse_resampler_response(self):
        for obj in self.scene["objects"]:
            obj.update(muted=obj["id"] != "bass", widthDeg=0)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = np.zeros((4410, 2), np.float32)
            audio[-1] = .2
            for sid in IDS:
                sf.write(root / (sid + ".wav"), audio, 44100, subtype="FLOAT")
            render_foa(self.scene, root, root / "render")
            result = sf.read(root / "render/foa.wav")[0]
            self.assertGreater(float(np.sum(result[4800:] ** 2)), 0)
            np.testing.assert_array_equal(result[-2400:], 0)


class ApiTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        return create_app(Path(self.temp.name), "/missing/python", "/missing/weights")

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp.cleanup()

    async def test_health_and_real_model_readiness(self):
        response = await self.client.get("/api/spatial/status")
        data = await response.json()
        self.assertEqual(data["app"], "neural-spatial-audio")
        self.assertFalse(data["separation"]["ready"])
        self.assertEqual(data["projects"], [])

    async def test_host_origin_and_paths(self):
        r = await self.client.get("/api/spatial/status", headers={"Host": "attacker.test"})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/spatial/demo", headers={"Origin": "https://attacker.test"})
        self.assertEqual(r.status, 403)
        r = await self.client.get("/api/spatial/projects/bad/files/original.wav")
        self.assertEqual(r.status, 404)
        r = await self.client.get("/gateway/spatial_runtime.py")
        self.assertEqual(r.status, 404)
        r = await self.client.get("/api/spatial/projects/" + "a" * 32)
        self.assertEqual(r.status, 404)

    async def test_local_viewer_assets_are_explicitly_served(self):
        for name in ("spatial-audio/scene-view.mjs", "spatial-audio/coordinates.mjs", "spatial-audio/trajectory.mjs",
                     "public/vendor/three/lines/Line2.js", "public/vendor/three/lines/LineGeometry.js", "public/vendor/three/lines/LineMaterial.js",
                     "public/vendor/three/lines/LineSegments2.js", "public/vendor/three/lines/LineSegmentsGeometry.js",
                     "public/vendor/three/three.module.min.js", "public/vendor/three/three.core.min.js",
                     "public/vendor/three/OrbitControls.js"):
            response = await self.client.get("/" + name)
            self.assertEqual(response.status, 200, name)
            self.assertIn("javascript", response.content_type, name)
        for name in ("gateway/spatial_runtime.py", "public/vendor/NOTICE.md", "spatial-audio/private.json"):
            self.assertEqual((await self.client.get("/" + name)).status, 404, name)
        head = await self.client.get("/spatial-audio/assets/listener-head.json")
        self.assertEqual(head.status, 200)
        self.assertEqual((await head.json())["metadata"]["type"], "BufferGeometry")
        credit = await self.client.get("/spatial-audio/assets/LISTENER-LICENSE.txt")
        self.assertEqual(credit.status, 200)
        self.assertIn("Attribution 3.0", await credit.text())
        self.assertEqual((await self.client.get("/spatial-audio/assets/private.json")).status, 404)


class BinauralRaceTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = {"id": "b" * 32, "state": "ready", "input": {"durationSeconds": .1},
                        "separation": {"complete": True}, "artifacts": {}, "error": None}
        self.project["scene"] = default_scene(self.project)
        self.project["sceneHash"] = canonical_hash(self.project["scene"])
        folder = self.root / self.project["id"]
        folder.mkdir()
        (folder / "project.json").write_text(json.dumps(self.project))
        stems = folder / "stems"
        stems.mkdir()
        for sid in IDS:
            sf.write(stems / (sid + ".wav"), np.full((4410, 2), .01), 44100)
        (stems / "separation.json").write_text("{}")
        self.rendered = folder / "renders" / self.project["sceneHash"]
        render_foa(self.project["scene"], stems, self.rendered)
        package(self.project, self.root)
        return create_app(self.root, "/missing/python", "/missing/weights")

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp.cleanup()

    async def test_fold_warning_reaches_valid_project_and_clears_for_unrendered_scene(self):
        folder = self.root / self.project["id"]
        bass = next(o for o in self.project["scene"]["objects"] if o["id"] == "bass")
        bass["widthDeg"] = 0
        self.project["scene"] = validate_scene(self.project["scene"], self.project)
        wave = .05*np.sin(2*np.pi*100*np.arange(4410)/44100)
        sf.write(folder/"stems/bass.wav", np.column_stack((wave, -wave)), 44100, subtype="FLOAT")
        self.project["sceneHash"] = canonical_hash(self.project["scene"])
        dest = folder/"renders"/self.project["sceneHash"]
        render_foa(self.project["scene"], folder/"stems", dest)
        package(self.project, self.root)
        self.assertIn("packageUrl", artifacts(self.project, self.root))
        self.assertFalse(self.project["artifactWarnings"])
        self.assertEqual(len(self.project["audioWarnings"]), 1)
        self.assertIn("bass", self.project["audioWarnings"][0])
        updated = await self.client.post("/api/spatial/projects/"+self.project["id"]+"/scene",
            json={"scene": self.project["scene"]})
        self.assertEqual(updated.status, 200)
        response = await self.client.get("/api/spatial/projects/"+self.project["id"])
        self.assertEqual((await response.json())["audioWarnings"], self.project["audioWarnings"])
        next(o for o in self.project["scene"]["objects"] if o["id"] == "bass")["widthDeg"] = 30
        self.project["sceneHash"] = canonical_hash(self.project["scene"])
        self.assertEqual(artifacts(self.project, self.root), {})
        self.assertEqual(self.project["audioWarnings"], [])

    async def test_scene_cannot_change_during_partial_headphone_upload(self):
        payload = io.BytesIO()
        audio = np.zeros((16800, 2))
        audio[:4800] = .01
        sf.write(payload, audio, RATE, format="WAV", subtype="PCM_16")
        first, release = asyncio.Event(), asyncio.Event()

        async def body():
            yield payload.getvalue()[:44]
            first.set()
            await release.wait()
            yield payload.getvalue()[44:]

        base = "/api/spatial/projects/" + self.project["id"]
        upload = asyncio.create_task(self.client.post(base + "/binaural", data=body(),
            headers={"X-Spatial-Geometry": STEREO_GEOMETRY, "X-Scene-Hash": self.project["sceneHash"], "Content-Type": "audio/wav"}))
        await first.wait()
        await asyncio.sleep(.05)
        scene = self.project["scene"]
        scene["objects"][0]["azimuthDeg"] = 12
        update = await self.client.post(base + "/scene", json={"scene": scene})
        release.set()
        result = await upload
        self.assertEqual(update.status, 409)
        self.assertEqual(result.status, 200, await result.text())
        published = artifacts(self.project, self.root)
        receipt = json.loads((self.root / self.project["id"] /
                              published["binauralReportUrl"].split("/files/", 1)[1]).read_text())
        self.assertEqual(receipt["sceneHash"], self.project["sceneHash"])
        self.assertTrue(receipt["quality"]["passed"])
        self.assertLessEqual(receipt["quality"]["final"]["truePeakDbTP"], -1)
        self.assertEqual(receipt["timeline"]["renderTailSeconds"], .25)

    async def test_render_tail_is_required_and_final_guard_must_decay(self):
        base = "/api/spatial/projects/" + self.project["id"]
        for audio in (np.full((4800, 2), .01), np.full((16800, 2), .01)):
            payload = io.BytesIO()
            sf.write(payload, audio, RATE, format="WAV", subtype="PCM_16")
            response = await self.client.post(base + "/binaural", data=payload.getvalue(),
                headers={"X-Spatial-Geometry": STEREO_GEOMETRY, "X-Scene-Hash": self.project["sceneHash"]})
            self.assertEqual(response.status, 400, await response.text())
        self.assertFalse(artifacts(self.project, self.root).get("binauralUrl"))

    async def test_failed_package_keeps_entire_previous_publication(self):
        base = "/api/spatial/projects/" + self.project["id"]
        async def upload(level):
            payload = io.BytesIO()
            audio = np.zeros((16800, 2))
            audio[:4800] = level
            sf.write(payload, audio, RATE, format="WAV", subtype="PCM_16")
            return await self.client.post(base + "/binaural", data=payload.getvalue(),
                headers={"X-Spatial-Geometry": STEREO_GEOMETRY, "X-Scene-Hash": self.project["sceneHash"]})

        first = await upload(.01)
        self.assertEqual(first.status, 200, await first.text())
        links = (await first.json())["artifacts"]
        old = {key: await (await self.client.get(links[key])).read()
               for key in ("binauralUrl", "binauralReportUrl", "packageUrl")}
        with patch("spatial_runtime.package", side_effect=OSError("injected package failure")):
            failed = await upload(.02)
        self.assertEqual(failed.status, 500)
        self.assertIn("此前已发布版本保留", (await failed.json())["error"])
        after = await (await self.client.get(base)).json()
        self.assertEqual(after["artifacts"], links)
        for key, data in old.items():
            self.assertEqual(await (await self.client.get(links[key])).read(), data)
        self.assertFalse(list(self.rendered.glob("binaural/tmp-*")))
        replace = Path.replace
        def fail_pointer(path, dest):
            if Path(dest).name == "binaural-current.json":
                raise OSError("injected publication switch failure")
            return replace(path, dest)
        with patch.object(Path, "replace", fail_pointer):
            self.assertEqual((await upload(.025)).status, 500)
        self.assertEqual((await (await self.client.get(base)).json())["artifacts"], links)
        self.assertFalse(list(self.rendered.glob(".binaural-current-*.tmp")))
        next_result = await upload(.03)
        self.assertEqual(next_result.status, 200, await next_result.text())
        next_links = (await next_result.json())["artifacts"]
        self.assertNotEqual(next_links["binauralUrl"], links["binauralUrl"])
        for key, data in old.items():
            self.assertEqual(await (await self.client.get(links[key])).read(), data)

    async def test_intersample_peak_is_remeasured_and_channels_share_attenuation(self):
        path = self.root / "intersample.wav"
        n = np.arange(RATE)
        signal = .98*np.sin(np.pi/2*n+np.pi/4)
        audio = np.column_stack((signal, signal*.5))
        sf.write(path, audio, RATE, subtype="PCM_16")
        before = sf.read(path)[0]
        self.assertLess(np.max(np.abs(before)), .951)
        value = await verify_binaural(path, FFMPEG)
        self.assertGreater(value["input"]["truePeakDbTP"], -1)
        self.assertLess(value["commonGainDb"], 0)
        self.assertLessEqual(value["final"]["truePeakDbTP"], -1)
        after = sf.read(path)[0]
        np.testing.assert_allclose(after, before*10**(value["commonGainDb"]/20), atol=1/32768)
        self.assertFalse(value["loudnessRangeSuitable"])
        self.assertEqual(len(value["audioSha256"]), 64)
        self.assertTrue(value["ffmpegVersion"].startswith("ffmpeg version"))

    async def test_changed_binaural_content_is_hidden_and_denied_without_losing_healthy_exports(self):
        base = "/api/spatial/projects/" + self.project["id"]
        speaker_links = (await (await self.client.get(base)).json())["artifacts"]

        async def upload(level):
            payload = io.BytesIO()
            audio = np.zeros((16800, 2))
            audio[:4800] = level
            sf.write(payload, audio, RATE, format="WAV", subtype="PCM_16")
            response = await self.client.post(base + "/binaural", data=payload.getvalue(),
                headers={"X-Spatial-Geometry": STEREO_GEOMETRY,
                         "X-Scene-Hash": self.project["sceneHash"]})
            self.assertEqual(response.status, 200, await response.text())
            return (await response.json())["artifacts"]

        old_links = await upload(.01)
        publication_keys = ("binauralUrl", "binauralReportUrl", "packageUrl")
        old_data = {key: await (await self.client.get(old_links[key])).read()
                    for key in publication_keys}
        current_links = await upload(.02)
        self.assertNotEqual(current_links["binauralUrl"], old_links["binauralUrl"])
        path = self.root / self.project["id"] / current_links["binauralUrl"].split("/files/", 1)[1]
        before = sf.info(path)
        original_bytes = path.read_bytes()
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        audio[:4800] *= .5
        sf.write(path, audio, rate, format="WAV", subtype=before.subtype)
        after = sf.info(path)
        self.assertEqual((after.format, after.subtype, after.samplerate, after.channels, after.frames),
                         (before.format, before.subtype, before.samplerate, before.channels, before.frames))
        self.assertNotEqual(path.read_bytes(), original_bytes)

        state = await (await self.client.get(base)).json()
        for key in ("binauralUrl", "binauralReportUrl"):
            with self.subTest(hidden_link=key):
                self.assertNotIn(key, state["artifacts"])
        with self.subTest(speaker_package_restored=True):
            self.assertEqual(state["artifacts"]["packageUrl"], speaker_links["packageUrl"])
        with self.subTest(corruption_warning=True):
            self.assertTrue(state["artifactWarnings"])
        for key in publication_keys:
            with self.subTest(corrupted_publication=key):
                response = await self.client.get(current_links[key])
                self.assertEqual(response.status, 404)
            with self.subTest(previous_healthy_revision=key):
                response = await self.client.get(old_links[key])
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.read(), old_data[key])
        for key, url in speaker_links.items():
            with self.subTest(healthy_speaker_export=key):
                self.assertEqual(state["artifacts"][key], url)
                self.assertEqual((await self.client.get(url)).status, 200)

    async def test_upload_rejects_float_wav_and_preserves_published_audio(self):
        previous = self.rendered / "binaural.wav"
        previous.write_bytes(b"existing published output")
        payload = io.BytesIO()
        sf.write(payload, np.full((4800, 2), .01), RATE, format="WAV", subtype="FLOAT")
        response = await self.client.post(
            "/api/spatial/projects/"+self.project["id"]+"/binaural", data=payload.getvalue(),
            headers={"X-Spatial-Geometry": STEREO_GEOMETRY, "X-Scene-Hash": self.project["sceneHash"], "Content-Type": "audio/wav"})
        self.assertEqual(response.status, 400)
        self.assertIn("PCM16", (await response.json())["error"])
        self.assertEqual(previous.read_bytes(), b"existing published output")
        self.assertFalse(list(self.rendered.glob("headphone-*.wav")))


if __name__ == "__main__":
    unittest.main()
