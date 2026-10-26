import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np
import soundfile as sf
from aiohttp.test_utils import AioHTTPTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_online import ORIGIN, download_audio, online_metadata, validate_audio_url
from spatial_runtime import create_app


class RemoteResponse:
    def __init__(self, body=b"", status=200, headers=None, chunks=None, entered=None, release=None):
        self.status, self.headers = status, headers or {}
        self.chunks = chunks if chunks is not None else [body]
        self.content = self
        self.entered, self.release = entered, release

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        if self.entered:
            self.entered.set()
            await self.release.wait()
        for chunk in self.chunks:
            yield chunk


class RemoteClient:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url not in self.responses:
            raise AssertionError("Unexpected upstream request: " + url)
        return self.responses[url]


class OnlineTests(unittest.IsolatedAsyncioTestCase):
    def test_url_validation_fixed_origin_and_paths(self):
        for url in ("/audio/track.wav", ORIGIN + "/audio/2026/take_01.mp3"):
            self.assertEqual(validate_audio_url(url), ORIGIN + url if url.startswith("/") else url)
        for url in (None, {}, "file:///audio/a.wav", "http://localhost:8189/audio/a.wav",
                    "http://10.100.16.56/audio/a.wav", "http://10.100.16.56:08189/audio/a.wav",
                    "http://u:p@10.100.16.56:8189/audio/a.wav", "https://10.100.16.56:8189/audio/a.wav",
                    "http://[::1]:8189/audio/a.wav", "//10.100.16.56:8189/audio/a.wav",
                    "/audio/../private", "/audio/a/../../x.wav", "/audio/%2e%2e/x.wav",
                    "/audio/%252e%252e/x.wav", "/audio/a%2fb.wav", "/audio/a\\b.wav",
                    "/audio/a.wav?key=secret", "/audio/a.wav#secret", "/audio/a.wav?",
                    " /audio/a.wav", "/audio/a\r\nb.wav", "/audio//x.wav", "/api/generate/models"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_audio_url(url)

    async def test_safe_metadata_only_two_fixed_gets_without_credentials(self):
        client = RemoteClient({
            ORIGIN + "/api/generate/models": RemoteResponse(json.dumps({"models": [
                {"name": "model/name", "is_active": True, "is_preloaded": False,
                 "api_key": "do-not-copy", "token": "do-not-copy", "endpoint": "private"}],
                "auth": "do-not-copy"}).encode()),
            ORIGIN + "/health": RemoteResponse(b'{"status":"ok","service":"music-studio","auth":"do-not-copy"}')})
        with patch("spatial_online.ClientSession", return_value=client) as factory:
            metadata = await online_metadata()
        self.assertTrue(metadata["reachable"])
        self.assertEqual(metadata["models"], [{"name": "model/name", "is_active": True, "is_preloaded": False}])
        self.assertNotIn("do-not-copy", json.dumps(metadata))
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(options == {"allow_redirects": False} for _, options in client.calls))
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertLessEqual(factory.call_args.kwargs["timeout"].total, 120)

    async def test_metadata_redirect_and_oversize_are_unavailable(self):
        for response in (RemoteResponse(status=302, headers={"Location": "http://127.0.0.1/secret"}),
                         RemoteResponse(chunks=[b" "*65536, b" "])):
            client = RemoteClient({ORIGIN + "/api/generate/models": response,
                                   ORIGIN + "/health": RemoteResponse(b'{"status":"ok"}')})
            with patch("spatial_online.ClientSession", return_value=client):
                self.assertFalse((await online_metadata())["reachable"])
            self.assertTrue(all(url.startswith(ORIGIN) for url, _ in client.calls))

    async def test_redirect_size_encoding_and_partial_fail_closed(self):
        responses = (RemoteResponse(status=302, headers={"Location": "http://169.254.169.254/latest"}),
                     RemoteResponse(headers={"Content-Length": "9"}),
                     RemoteResponse(chunks=[b"1234", b"56789"]),
                     RemoteResponse(b"x", headers={"Content-Encoding": "gzip"}),
                     RemoteResponse(b"123", headers={"Content-Length": "4"}), RemoteResponse())
        for response in responses:
            with tempfile.TemporaryDirectory() as temp:
                client = RemoteClient({ORIGIN + "/audio/x.wav": response})
                with patch("spatial_online.ClientSession", return_value=client), self.assertRaises(ValueError):
                    await download_audio("/audio/x.wav", temp, max_bytes=8)
                self.assertEqual(list(Path(temp).iterdir()), [])
                self.assertEqual(client.calls, [(ORIGIN + "/audio/x.wav", {"allow_redirects": False})])
        with patch("spatial_online.ClientSession") as factory, self.assertRaises(ValueError):
            await download_audio("http://localhost/audio/x.wav", "/unused")
        factory.assert_not_called()

    async def test_original_receipt_sha_and_exclusive_noncollision_files(self):
        payload = b"original downloaded bytes"
        client = RemoteClient({ORIGIN + "/audio/x.wav": RemoteResponse(payload)})
        with tempfile.TemporaryDirectory() as temp, patch("spatial_online.ClientSession", return_value=client):
            first, source = await download_audio("/audio/x.wav", temp)
            second, _ = await download_audio("/audio/x.wav", temp)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), payload)
            self.assertEqual(source["sha256"], hashlib.sha256(payload).hexdigest())
            receipt = json.loads(first.with_name(first.name + ".source.json").read_text())
            self.assertEqual(receipt["bytes"], len(payload))
            self.assertEqual(receipt["url"], ORIGIN + "/audio/x.wav")
            self.assertNotIn(temp, json.dumps(receipt))


def wav(channels=2):
    data = io.BytesIO()
    sf.write(data, np.full((4410, channels), .1, np.float32), 44100, format="WAV", subtype="FLOAT")
    return data.getvalue()


class OnlineApiTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.downloads = self.root / "Downloads"
        return create_app(self.root / "projects", "/missing/python", "/missing/weights", downloads=self.downloads)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp.cleanup()

    async def test_status_proxy_exposes_only_safe_catalog_and_configuration(self):
        client = RemoteClient({
            ORIGIN + "/api/generate/models": RemoteResponse(b'{"models":[{"name":"model-x","is_active":true,"token":"private"}]}'),
            ORIGIN + "/health": RemoteResponse(b'{"status":"ok","service":"studio","secret":"private"}')})
        with patch("spatial_online.ClientSession", return_value=client):
            response = await self.client.get("/api/spatial/online/status")
        self.assertEqual(response.status, 200)
        metadata = await response.json()
        self.assertTrue(metadata["reachable"])
        self.assertTrue(metadata["downloadEnabled"])
        self.assertFalse(metadata["busy"])
        self.assertNotIn("private", json.dumps(metadata))
        self.assertNotIn(str(self.downloads), json.dumps(metadata))

    async def test_download_is_preserved_and_real_ffmpeg_creates_stereo_project(self):
        payload = wav()
        client = RemoteClient({ORIGIN + "/audio/fixture.wav": RemoteResponse(payload)})
        with patch("spatial_online.ClientSession", return_value=client):
            response = await self.client.post("/api/spatial/online/import", json={"audioUrl": "/audio/fixture.wav"})
        self.assertEqual(response.status, 200, await response.text())
        project = await response.json()
        self.assertEqual(project["state"], "imported")
        self.assertEqual(project["input"]["channels"], 2)
        self.assertIsNone(project["separation"])
        source = project["source"]
        self.assertEqual(source["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual((self.downloads / source["filename"]).read_bytes(), payload)
        self.assertTrue((self.downloads / (source["filename"] + ".source.json")).is_file())
        saved = json.loads((self.root / "projects" / project["id"] / "project.json").read_text())
        self.assertEqual(saved["source"], source)
        original = await self.client.get(project["input"]["audioUrl"])
        self.assertEqual(original.status, 200)
        self.assertEqual(sf.info(io.BytesIO(await original.read())).channels, 2)
        self.assertEqual(len(client.calls), 1)

    async def test_mono_is_rejected_after_download_but_original_and_receipt_remain(self):
        client = RemoteClient({ORIGIN + "/audio/mono.wav": RemoteResponse(wav(1))})
        with patch("spatial_online.ClientSession", return_value=client):
            response = await self.client.post("/api/spatial/online/import", json={"audioUrl": "/audio/mono.wav"})
        self.assertEqual(response.status, 400, await response.text())
        self.assertIn("双声道", (await response.json())["error"])
        self.assertEqual(len(list(self.downloads.glob("*.wav"))), 1)
        self.assertEqual(len(list(self.downloads.glob("*.source.json"))), 1)
        self.assertEqual(list((self.root / "projects").iterdir()), [])

    async def test_generated_source_binds_job_and_reuses_relative_or_absolute_url(self):
        with patch('spatial_generation.GenerationBridge.request',AsyncMock(return_value={'token':'memory-session'})):
            self.assertEqual((await self.client.post('/api/spatial/online/connect',json={'username':'ResearchQA'})).status,200)
        with patch('spatial_generation.GenerationBridge.request',AsyncMock(return_value={'jobId':'remote-1'})):
            self.assertEqual((await self.client.post('/api/spatial/online/generate',json=dict(requestId='a'*32,model='ace',prompt='Piano',duration=10,seed=1))).status,200)
        with patch('spatial_generation.GenerationBridge.request',AsyncMock(return_value={'status':'succeeded','result':{'audioUrls':['/audio/fixture.wav']}})):
            self.assertEqual((await self.client.get('/api/spatial/online/jobs/'+'a'*32)).status,200)
        client=RemoteClient({ORIGIN+'/audio/fixture.wav':RemoteResponse(wav())})
        with patch('spatial_online.ClientSession',return_value=client):
            first=await self.client.post('/api/spatial/online/import',json={'audioUrl':'/audio/fixture.wav','jobId':'a'*32})
            self.assertEqual(first.status,200,await first.text())
            project=await first.json()
            second=await self.client.post('/api/spatial/online/import',json={'audioUrl':ORIGIN+'/audio/fixture.wav','jobId':'a'*32})
            self.assertEqual(second.status,200,await second.text())
            self.assertEqual((await second.json())['id'],project['id'])
        self.assertEqual(project['source']['generation']['remoteJobId'],'remote-1')
        self.assertEqual(len(client.calls),1)

    async def test_json_origin_size_and_url_boundaries_do_not_contact_upstream(self):
        with patch("spatial_online.ClientSession") as factory:
            for headers in ({}, {"Content-Type": "text/plain"}):
                response = await self.client.post("/api/spatial/online/import", data='{"audioUrl":"/audio/x.wav"}', headers=headers)
                self.assertEqual(response.status, 415)
            response = await self.client.post("/api/spatial/online/import", json={"audioUrl": "/audio/x.wav"}, headers={"Origin": "https://attacker.test"})
            self.assertEqual(response.status, 403)
            for body in ({"audioUrl": "http://127.0.0.1/secret"}, {"audioUrl": "/audio/x.wav", "token": "x"}, {"audioUrl": "x"*70000}):
                response = await self.client.post("/api/spatial/online/import", json=body)
                self.assertEqual(response.status, 400)
            factory.assert_not_called()
        self.assertFalse(self.downloads.exists())

    async def test_download_holds_work_lock_until_local_import_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        client = RemoteClient({ORIGIN + "/audio/x.wav": RemoteResponse(wav(), entered=entered, release=release)})
        with patch("spatial_online.ClientSession", return_value=client):
            first = asyncio.create_task(self.client.post("/api/spatial/online/import", json={"audioUrl": "/audio/x.wav"}))
            await asyncio.wait_for(entered.wait(), 2)
            status = await (await self.client.get("/api/spatial/status")).json()
            self.assertTrue(status["working"])
            second = await self.client.post("/api/spatial/online/import", json={"audioUrl": "/audio/x.wav"})
            self.assertEqual(second.status, 409)
            release.set()
            response = await first
        self.assertEqual(response.status, 200, await response.text())
        self.assertFalse((await (await self.client.get("/api/spatial/status")).json())["working"])


class OnlineDisabledTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        return create_app(Path(self.temp.name), "/missing/python", "/missing/weights")

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp.cleanup()

    async def test_unconfigured_download_is_disabled_before_network_access(self):
        with patch("spatial_online.ClientSession") as factory:
            response = await self.client.post("/api/spatial/online/import", json={"audioUrl": "/audio/x.wav"})
            self.assertEqual(response.status, 400)
            factory.assert_not_called()
        status = await (await self.client.get("/api/spatial/status")).json()
        self.assertFalse(status["online"]["downloadEnabled"])


if __name__ == "__main__":
    unittest.main()
