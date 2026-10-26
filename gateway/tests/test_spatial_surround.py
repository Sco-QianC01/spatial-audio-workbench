import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import soundfile as sf
from aiohttp.test_utils import AioHTTPTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_audio import IDS, RATE, angles, canonical_hash, default_scene, render_foa, validate_scene
from spatial_runtime import create_app, render_complete, zip_has
from spatial_mix import file_hash
from spatial_surround import ACTIVE, CHANNEL_ORDER, SPEAKERS, STEREO_GEOMETRY, layout_config, speaker_gains


class PanningTests(unittest.TestCase):
    def test_zip_payload_crc_is_checked_after_file_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package.zip"
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("scene.json", '{"scene":"verified"}')
            self.assertTrue(zip_has(path, ["scene.json"]))
            original = path.read_bytes()
            damaged = bytearray(original)
            damaged[30+len("scene.json")+2] ^= 1
            path.write_bytes(damaged)
            self.assertFalse(zip_has(path, ["scene.json"]))
            path.write_bytes(original)
            self.assertTrue(zip_has(path, ["scene.json"]))

    def test_714_anchors_coverage_continuity_and_scene_validation(self):
        order, positions = layout_config("7.1.4")
        active = [i for i, pos in enumerate(positions) if pos is not None]
        self.assertEqual(order, ("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs", "Ltf", "Rtf", "Ltr", "Rtr"))
        gains = speaker_gains(*np.radians(np.array([positions[i] for i in active])).T, "7.1.4")
        np.testing.assert_allclose(gains, np.eye(12)[active], atol=1e-6)
        for elevation in (-30, 0, 30, 45, 70, 90):
            gains = speaker_gains(np.radians(np.linspace(-180, 180, 10001)),
                                  np.full(10001, np.radians(elevation)), "7.1.4")
            self.assertTrue(np.isfinite(gains).all())
            np.testing.assert_allclose(np.sum(gains**2, axis=1), 1, atol=2e-7)
            np.testing.assert_array_equal(gains[:, 3], 0)
            self.assertLess(np.max(np.abs(np.diff(gains, axis=0))), .01)
        project = {"id": "a"*32, "input": {"durationSeconds": 1}}
        scene = default_scene(project)
        legacy_hash = canonical_hash(validate_scene(scene, project))
        scene["speakerLayout"] = "7.1.4"
        self.assertEqual(validate_scene(scene, project)["speakerLayout"], "7.1.4")
        self.assertNotEqual(canonical_hash(validate_scene(scene, project)), legacy_hash)
        for invalid in ("9.1.6", "../../", None, [], True):
            scene["speakerLayout"] = invalid
            with self.assertRaises(ValueError):
                validate_scene(scene, project)

    def test_714_pcm24_prints_match_interleaved_and_silent_lfe(self):
        project = {"id": "a"*32, "input": {"durationSeconds": .1}}
        scene = default_scene(project)
        scene["speakerLayout"] = "7.1.4"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for sid in IDS:
                sf.write(root / f"{sid}.wav", np.full((4410, 2), .1), 44100, subtype="FLOAT")
            render_foa(scene, root, root / "out")
            audio, rate = sf.read(root / "out/surround_7_1_4.wav", always_2d=True)
            self.assertEqual((rate, audio.shape), (RATE, (16800, 12)))
            receipt = json.loads((root / "out/surround.json").read_text())
            self.assertEqual(receipt["speakerLayout"], "7.1.4")
            self.assertEqual(receipt["subtype"], "PCM_24")
            for i, speaker in enumerate(receipt["speakers"]):
                path = root / "out" / speaker["file"]
                self.assertEqual(sf.info(path).subtype, "PCM_24")
                mono, mono_rate = sf.read(path)
                self.assertEqual(mono_rate, RATE)
                np.testing.assert_array_equal(mono, audio[:, i])
            np.testing.assert_array_equal(audio[:, 3], 0)

    def test_speaker_anchors_unit_power_and_lfe_zero(self):
        positions = np.radians(np.array([SPEAKERS[i] for i in ACTIVE]))
        gains = speaker_gains(*positions.T)
        np.testing.assert_allclose(gains, np.eye(8)[ACTIVE], atol=1e-6)
        az, el = np.meshgrid(np.radians(np.linspace(-180, 180, 181)),
                             np.radians(np.linspace(-90, 90, 91)))
        gains = speaker_gains(az.ravel(), el.ravel())
        self.assertTrue(np.isfinite(gains).all())
        self.assertGreaterEqual(gains.min(), 0)
        np.testing.assert_allclose(np.sum(gains**2, axis=1), 1, atol=2e-7)
        np.testing.assert_array_equal(gains[:, 3], 0)

    def test_shortest_arc_wrap_horizon_and_mesh_edges_are_continuous(self):
        obj = default_scene({"id": "a"*32, "input": {"durationSeconds": 1}})["objects"][0]
        obj["keyframes"] = [{"timeSeconds": 0, "azimuthDeg": 179, "elevationDeg": -10},
                            {"timeSeconds": 1, "azimuthDeg": -179, "elevationDeg": 10}]
        az, el = angles(obj, np.linspace(0, 1, 1001))
        gains = speaker_gains(az, el)
        self.assertLess(np.max(np.abs(np.diff(gains, axis=0))), .002)
        np.testing.assert_allclose(speaker_gains(np.radians([-180, 180]), np.zeros(2))[0],
                                   speaker_gains(np.radians([-180, 180]), np.zeros(2))[1], atol=1e-6)
        for elevation in (0, 30, 80, 90):
            gains = speaker_gains(np.radians(np.linspace(-180, 180, 10001)),
                                   np.full(10001, np.radians(elevation)))
            self.assertLess(np.max(np.abs(np.diff(gains, axis=0))), .01)
        np.testing.assert_allclose(speaker_gains(np.array([.3, .3]), np.array([-1, 0]))[0],
                                   speaker_gains(np.array([.3, .3]), np.array([-1, 0]))[1])

    def test_render_format_gain_width_muting_and_mono_manifest(self):
        project = {"id": "a"*32, "input": {"durationSeconds": .1}}
        scene = default_scene(project)
        for obj in scene["objects"]:
            obj.update(muted=obj["id"] != "bass", azimuthDeg=0, elevationDeg=0, widthDeg=60, gainDb=-6)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for sid in IDS:
                sf.write(root / f"{sid}.wav", np.full((4410, 2), .1), 44100, subtype="FLOAT")
            report = render_foa(scene, root, root / "out")
            audio, rate = sf.read(root / "out/surround_5_1_2.wav", always_2d=True)
            self.assertEqual((rate, audio.shape), (RATE, (16800, 8)))
            self.assertEqual(sf.info(root / "out/surround_5_1_2.wav").subtype, "FLOAT")
            source = sf.read(root / "out/objects/bass_L.wav")[0][2400]
            np.testing.assert_allclose(audio[2400, :2], [source * 10**(-6/20) / np.sqrt(2)]*2, atol=1e-7)
            np.testing.assert_array_equal(audio[:, 2:], 0)
            receipt = json.loads((root / "out/surround.json").read_text())
            self.assertEqual(receipt["channelOrder"], list(CHANNEL_ORDER))
            self.assertEqual(receipt["sceneHash"], canonical_hash(scene))
            self.assertEqual(receipt["mixHeadroomGain"], report["mixHeadroomGain"])
            for i, speaker in enumerate(receipt["speakers"]):
                mono, mono_rate = sf.read(root / "out" / speaker["file"])
                self.assertEqual(mono_rate, RATE)
                np.testing.assert_array_equal(mono, audio[:, i])

    def test_one_headroom_gain_for_all_feeds_and_foa(self):
        project = {"id": "a"*32, "input": {"durationSeconds": .1}}
        scene = default_scene(project)
        for obj in scene["objects"]:
            obj.update(azimuthDeg=0, elevationDeg=0, widthDeg=0, gainDb=6, muted=False)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for sid in IDS:
                sf.write(root / f"{sid}.wav", np.full((4410, 2), .7), 44100, subtype="FLOAT")
            report = render_foa(scene, root, root / "out")
            foa = sf.read(root / "out/foa.wav")[0]
            feeds = sf.read(root / "out/surround_5_1_2.wav")[0]
            self.assertLess(report["mixHeadroomGain"], 1)
            self.assertLessEqual(np.max(np.abs(foa)), .950001)
            self.assertLessEqual(np.max(np.abs(feeds)), .950001)
            np.testing.assert_allclose(foa[:, 0], feeds[:, 2], atol=1e-7)
            np.testing.assert_array_equal(feeds[:, 3], 0)


class SurroundApiTests(AioHTTPTestCase):
    async def test_old_binaural_geometry_receipt_cannot_be_published_or_downloaded(self):
        await self.rendered()
        payload = io.BytesIO()
        audio = np.zeros((16800, 2)); audio[:4800] = .01
        sf.write(payload, audio, RATE, format='WAV', subtype='PCM_16')
        async def meter(path, ffmpeg):
            return {'passed': True, 'audioSha256': file_hash(path), 'final': {'truePeakDbTP': -30}}
        with patch('spatial_runtime.verify_binaural', new=meter):
            response = await self.client.post(self.base+'/binaural', data=payload.getvalue(),
                headers={'X-Scene-Hash': self.project['sceneHash'], 'X-Spatial-Geometry': STEREO_GEOMETRY})
        self.assertEqual(response.status, 200, await response.text())
        published = (await response.json())['artifacts']
        receipt_path = self.root/self.project['id']/published['binauralReportUrl'].split('/files/', 1)[1]
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt.pop('stereoGeometry'), STEREO_GEOMETRY)
        receipt_path.write_text(json.dumps(receipt))
        hidden = await (await self.client.get(self.base)).json()
        self.assertIn('surroundUrl', hidden['artifacts'])
        self.assertNotIn('binauralUrl', hidden['artifacts'])
        self.assertIn('算法已更新', hidden['artifactWarnings'][0])
        for key in ('binauralUrl', 'binauralReportUrl', 'packageUrl'):
            self.assertEqual((await self.client.get(published[key])).status, 404)
        self.assertTrue(receipt_path.is_file())

    async def test_old_geometry_cache_and_old_binaural_client_are_hidden_and_preserved(self):
        state = await self.rendered()
        folder = self.legacy / 'v2'
        report = json.loads((folder/'render.json').read_text())
        report.pop('stereoGeometry')
        (folder/'render.json').write_text(json.dumps(report))
        old_bytes = (folder/'render.json').read_bytes()
        self.assertFalse(render_complete(folder))
        shown = await (await self.client.get(self.base)).json()
        self.assertFalse(shown['artifacts'])
        self.assertIn('算法已更新', shown['artifactWarnings'][0])
        self.assertNotIn('损坏', shown['artifactWarnings'][0])
        self.assertEqual((await self.client.get(state['artifacts']['surroundUrl'])).status, 404)
        response = await self.client.post(self.base+'/binaural', data=b'not published',
            headers={'X-Scene-Hash': self.project['sceneHash'], 'X-Spatial-Geometry': STEREO_GEOMETRY})
        self.assertEqual(response.status, 400)
        self.assertIn('重新渲染全景声', (await response.json())['error'])
        rebuilt = await self.rendered()
        preserved = self.root/self.project['id']/rebuilt['renderRecovery']['preservedDirectory']
        self.assertEqual((preserved/'render.json').read_bytes(), old_bytes)
        self.assertIn('geometry updated', rebuilt['renderRecovery']['reason'])
        self.assertTrue(render_complete(folder))
        for header in (None, 'azimuth-offset-v0'):
            headers = {'X-Scene-Hash': self.project['sceneHash']}
            if header is not None:
                headers['X-Spatial-Geometry'] = header
            response = await self.client.post(self.base+'/binaural', data=b'not published', headers=headers)
            self.assertEqual(response.status, 400)
            self.assertIn('刷新页面', (await response.json())['error'])
        self.assertFalse((folder/'binaural-current.json').exists())

    async def test_corrupt_cache_is_hidden_preserved_and_rebuilt(self):
        state = await self.rendered()
        folder = self.legacy / "v2"
        invalid_audio = json.loads((folder / "render.json").read_text()) | {"audio": []}
        for name, broken in (("surround_channels/01_L.wav", b"truncated"),
                             ("render.json", b"{}"), ("render.json", json.dumps(invalid_audio).encode()),
                             ("package.zip", b"not a zip")):
            with self.subTest(name=name):
                previous_url = state["artifacts"]["surroundUrl"]
                (folder / name).write_bytes(broken)
                self.assertFalse(render_complete(folder))
                response = await self.client.get(self.base)
                self.assertEqual(response.status, 200)
                damaged = await response.json()
                self.assertFalse(damaged["artifacts"])
                self.assertTrue(damaged["artifactWarnings"])
                self.assertEqual((await self.client.get(previous_url)).status, 404)
                state = await self.rendered()
                self.assertTrue(render_complete(folder))
                preserved = self.root / self.project["id"] / state["renderRecovery"]["preservedDirectory"]
                self.assertEqual((preserved / name).read_bytes(), broken)
                self.assertEqual((self.legacy / "foa.wav").read_bytes(), b"preserve legacy")

    async def test_broken_binaural_publication_preserves_speakers_and_can_recover(self):
        state = await self.rendered()
        folder = self.legacy / "v2"
        pointer = folder / "binaural-current.json"
        revision = folder / "binaural" / ("f"*32)
        revision.mkdir(parents=True)
        for payload, receipt in (("{broken", None),
                                 (json.dumps({"revision": "f"*32, "audioSha256": "0"*64}), None),
                                 (json.dumps({"revision": "f"*32, "audioSha256": "0"*64}), "{broken"),
                                 (json.dumps({"revision": "../../private"}), None)):
            pointer.write_text(payload)
            if receipt is None:
                (revision / "binaural.json").unlink(missing_ok=True)
            else:
                (revision / "binaural.json").write_text(receipt)
            response = await self.client.get(self.base)
            self.assertEqual(response.status, 200, await response.text())
            damaged = await response.json()
            self.assertEqual(damaged["artifacts"]["surroundUrl"], state["artifacts"]["surroundUrl"])
            self.assertNotIn("binauralUrl", damaged["artifacts"])
            self.assertTrue(damaged["artifactWarnings"])
            saved = await self.client.post(self.base+"/scene", json={"scene": state["scene"]})
            self.assertEqual(saved.status, 200, await saved.text())
        payload = io.BytesIO()
        audio = np.zeros((16800, 2)); audio[:4800] = .01
        sf.write(payload, audio, RATE, format="WAV", subtype="PCM_16")
        async def meter(path, ffmpeg):
            return {"passed": True, "audioSha256": file_hash(path), "final": {"truePeakDbTP": -30}}
        with patch("spatial_runtime.verify_binaural", new=meter):
            response = await self.client.post(self.base+"/binaural", data=payload.getvalue(),
                headers={"X-Spatial-Geometry": STEREO_GEOMETRY, "X-Scene-Hash": self.project["sceneHash"]})
        self.assertEqual(response.status, 200, await response.text())
        recovered = await response.json()
        self.assertIn("binauralUrl", recovered["artifacts"])
        self.assertFalse(recovered["artifactWarnings"])

    async def test_714_studio_package_and_exact_channel_routes(self):
        scene = self.project["scene"] | {"speakerLayout": "7.1.4"}
        response = await self.client.post(self.base + "/scene", json={"scene": scene})
        self.assertEqual(response.status, 200, await response.text())
        self.project = await response.json()
        state = await self.rendered()
        links = state["artifacts"]
        self.assertEqual((await self.client.get("/docs/SPATIAL-STUDIO-HANDOFF.md")).status, 200)
        self.assertTrue(links["surroundUrl"].endswith("surround_7_1_4.wav"))
        data = await (await self.client.get(links["studioPackageUrl"])).read()
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            names = bundle.namelist()
            self.assertEqual(len([n for n in names if n.startswith("surround_channels/")]), 12)
            self.assertEqual(len([n for n in names if n.startswith("routing_test/")]), 12)
            for name in ("routing.csv", "studio.json", "STUDIO_README.md", "scene.json", "surround.json"):
                self.assertIn(name, names)
            self.assertFalse(any(n.startswith(("stems/", "objects/")) for n in names))
            self.assertNotIn("binaural.wav", names)
            manifest = json.loads(bundle.read("studio.json"))
            self.assertEqual(manifest["physicalAcceptance"], "pending")
            self.assertEqual(manifest["speakerLayout"], "7.1.4")
            for item in manifest["channels"] + manifest["routingTest"] + manifest["documents"]:
                self.assertEqual(hashlib.sha256(bundle.read(item["file"])).hexdigest(), item["sha256"])
            occupancy = np.zeros(24*RATE, dtype=np.int8)
            for index, item in enumerate(manifest["routingTest"]):
                with sf.SoundFile(io.BytesIO(bundle.read(item["file"]))) as wav:
                    self.assertEqual((wav.samplerate, wav.channels, len(wav), wav.subtype), (RATE, 1, 24*RATE, "PCM_24"))
                    audio = wav.read()
                active = np.abs(audio) > 0
                occupancy += active
                start, end = round((index*2+.5)*RATE), round((index*2+1)*RATE)
                self.assertFalse(active[:start].any())
                self.assertFalse(active[end:].any())
                frequency, db = (50, -40) if index == 3 else (500, -30)
                self.assertAlmostEqual(20*np.log10(np.max(np.abs(audio))), db, places=3)
                steady = audio[start+round(.1*RATE):end-round(.1*RATE)]
                observed = np.argmax(np.abs(np.fft.rfft(steady))) * RATE / len(steady)
                self.assertEqual(observed, frequency)
                self.assertAlmostEqual(20*np.log10(np.sqrt(np.mean(steady**2))), db-10*np.log10(2), places=3)
            self.assertLessEqual(occupancy.max(), 1)
        prefix = links["surroundUrl"].rsplit("/", 1)[0]
        for channel in ("09_Ltf", "10_Rtf", "11_Ltr", "12_Rtr"):
            self.assertEqual((await self.client.get(prefix + "/surround_channels/" + channel + ".wav")).status, 200)
        for wrong in ("12_L", "01_Rtr", "13_Rtr", "00_L"):
            self.assertEqual((await self.client.get(prefix + "/surround_channels/" + wrong + ".wav")).status, 404)

    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = {"id": "c"*32, "state": "ready", "input": {"durationSeconds": .1},
                        "separation": {"complete": True}, "artifacts": {}, "error": None}
        self.project["scene"] = validate_scene(default_scene(self.project), self.project)
        self.project["sceneHash"] = canonical_hash(self.project["scene"])
        folder = self.root / self.project["id"]
        folder.mkdir()
        (folder / "project.json").write_text(json.dumps(self.project))
        stems = folder / "stems"
        stems.mkdir()
        for sid in IDS:
            sf.write(stems / f"{sid}.wav", np.full((4410, 2), .02), 44100, subtype="FLOAT")
        (stems / "separation.json").write_text("{}")
        self.legacy = folder / "renders" / self.project["sceneHash"]
        self.legacy.mkdir(parents=True)
        (self.legacy / "render.json").write_text('{"schema":"spatial-render-v1"}')
        (self.legacy / "foa.wav").write_bytes(b"preserve legacy")
        self.base = "/api/spatial/projects/" + self.project["id"]
        return create_app(self.root, "/missing/python", "/missing/weights")

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp.cleanup()

    async def rendered(self):
        response = await self.client.post(self.base + "/render", json={"expectedSceneHash": self.project["sceneHash"]})
        self.assertEqual(response.status, 202, await response.text())
        for _ in range(200):
            state = await (await self.client.get(self.base)).json()
            if state["state"] != "rendering":
                self.assertEqual(state["state"], "ready", state)
                return state
            await asyncio.sleep(.01)
        self.fail("bounded render timeout")

    async def test_legacy_cache_upgrade_urls_zip_and_reuse(self):
        state = await self.rendered()
        links = state["artifacts"]
        self.assertIn("/v2/", links["surroundUrl"])
        for key in ("foaUrl", "surroundUrl", "surroundReportUrl", "sceneUrl", "packageUrl"):
            self.assertEqual((await self.client.get(links[key])).status, 200, key)
        self.assertEqual((self.legacy / "foa.wav").read_bytes(), b"preserve legacy")
        data = await (await self.client.get(links["packageUrl"])).read()
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            self.assertIn("surround_5_1_2.wav", bundle.namelist())
            self.assertIn("surround.json", bundle.namelist())
            self.assertEqual(len([n for n in bundle.namelist() if n.startswith("surround_channels/")]), 8)
        with patch("spatial_runtime.render_foa", side_effect=AssertionError("must reuse complete v2")):
            self.assertEqual((await self.rendered())["artifacts"], links)

    async def test_cas_failed_publication_and_paths(self):
        wrong = await self.client.post(self.base + "/render", json={"expectedSceneHash": "0"*64})
        self.assertEqual(wrong.status, 409)
        with patch("spatial_runtime.package", side_effect=OSError("injected package failure")):
            self.assertEqual((await self.client.post(self.base + "/render")).status, 202)
            for _ in range(200):
                state = await (await self.client.get(self.base)).json()
                if state["state"] != "rendering":
                    break
                await asyncio.sleep(.01)
            self.assertEqual(state["state"], "error")
        self.assertFalse((self.legacy / "v2").exists())
        self.assertFalse(list(self.legacy.glob("tmp-*")))
        self.assertEqual((self.legacy / "foa.wav").read_bytes(), b"preserve legacy")
        state = await self.rendered()
        prefix = state["artifacts"]["surroundUrl"].rsplit("/", 1)[0]
        for name in ("../project.json", "surround_channels/01_L.wav/extra", "surround_channels/../../project.json", "unknown.wav"):
            self.assertEqual((await self.client.get(prefix + "/" + name)).status, 404, name)
        self.assertEqual((await self.client.get(prefix + "/surround_channels/01_L.wav")).status, 200)


if __name__ == "__main__":
    unittest.main()
