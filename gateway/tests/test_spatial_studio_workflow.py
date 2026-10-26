import asyncio
import json
import io
import zipfile
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np
import soundfile as sf
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_audio import IDS, canonical_hash, default_scene, render_foa, validate_scene
from spatial_generation import GenerationBridge
from spatial_mix import file_hash
from spatial_online import validate_audio_url
from spatial_runtime import create_app, package


class StudioWorkflowTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.weights = self.root / 'weights.pt'
        self.weights.touch()
        self.sources, self.jobs = {}, {}
        bridge = GenerationBridge(self.root / '.online-jobs')
        for index, model in enumerate(('ace', 'shao', 'minimax', 'yue'), 1):
            pid, jid = f'{index:032x}', f'{index+16:032x}'
            folder = self.root / pid
            (folder / 'stems').mkdir(parents=True)
            audio = np.full((4410, 2), .01, dtype=np.float32)
            sf.write(folder / 'original.wav', audio, 44100, subtype='FLOAT')
            for sid in IDS:
                sf.write(folder / 'stems' / (sid+'.wav'), audio, 44100, subtype='FLOAT')
            (folder / 'stems/separation.json').write_text('{}')
            project = {'id': pid, 'state': 'ready', 'input': {'name': model+'.wav', 'durationSeconds': .1,
                'sampleRate': 44100, 'frames': 4410, 'channels': 2}, 'separation': {'complete': True},
                'source': {'url': validate_audio_url('/audio/'+model+'.wav'),
                    'generation': {'id': jid, 'remoteJobId': 'remote-'+model, 'model': model,
                                   'label': model, 'prompt': 'piano', 'seed': 1283, 'requestHash': 'a'*64}}, 'error': None}
            scene = default_scene(project)
            if model == 'shao':
                scene['objects'][2]['keyframes'] = [
                    {'timeSeconds': 0, 'azimuthDeg': -50, 'elevationDeg': 10},
                    {'timeSeconds': .1, 'azimuthDeg': 70, 'elevationDeg': 40}]
            project['scene'] = validate_scene(scene, project)
            project['sceneHash'] = canonical_hash(project['scene'])
            (folder / 'project.json').write_text(json.dumps(project))
            bridge.save({'id': jid, 'model': model, 'status': 'succeeded', 'createdAt': index,
                'username': 'ResearchQA', 'remoteJobId': 'remote-'+model, 'projectId': pid,
                'audioUrls': [validate_audio_url('/audio/'+model+'.wav')], 'prompt': 'piano', 'seed': 1283, 'requestHash': 'a'*64,
                'label': model})
            self.sources[model], self.jobs[model] = project, jid
        bridge.save({'id': 'f'*32, 'status': 'running', 'createdAt': 5, 'audioUrls': []})
        bridge.save({'id': 'e'*32, 'status': 'succeeded', 'createdAt': 6,
                     'audioUrls': [validate_audio_url('/audio/unimported.wav')],
                     'remoteJobId': 'remote-unimported', 'model': 'ace', 'label': 'ace',
                     'prompt': 'piano', 'seed': 1283, 'requestHash': 'a'*64})
        return self.application()

    def application(self):
        return create_app(self.root, sys.executable, self.weights)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp.cleanup()

    def protected(self):
        return {str(path): file_hash(path) for p in self.sources.values()
                for path in (self.root/p['id']).rglob('*') if path.is_file()}

    async def prepare(self, model, client=None):
        response = await (client or self.client).post('/api/spatial/online/prepare', json={
            'jobId': self.jobs[model], 'audioUrl': '/audio/'+model+'.wav'})
        self.assertEqual(response.status, 400, await response.text())
        self.assertIn('原生分轨', (await response.json())['error'])

    async def rendered(self, source, client=None):
        client = client or self.client
        base = '/api/spatial/projects/'+source['id']
        response = await client.post(base+'/render', json={'expectedSceneHash': source['sceneHash']})
        self.assertEqual(response.status, 202, await response.text())
        for _ in range(200):
            project = await (await client.get(base)).json()
            if project['state'] != 'rendering':
                return project
            await asyncio.sleep(.01)
        self.fail('bounded legacy render timeout')

    async def test_four_stereo_models_cannot_prepare_or_mutate_sources_or_jobs(self):
        before = self.protected()
        jobs = {p.name: p.read_bytes() for p in (self.root/'.online-jobs').glob('*.json')}
        with patch('spatial_runtime.download_audio', new_callable=AsyncMock) as download, \
                patch('spatial_generation.GenerationBridge.request', new_callable=AsyncMock) as remote, \
                patch('spatial_runtime.asyncio.create_subprocess_exec', new_callable=AsyncMock) as worker:
            for model in self.sources:
                with self.subTest(model=model):
                    await self.prepare(model)
            download.assert_not_awaited()
            remote.assert_not_awaited()
            worker.assert_not_awaited()
        self.assertEqual(self.protected(), before)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root/'.online-jobs').glob('*.json')}, jobs)
        self.assertEqual(len(list(self.root.glob('*/project.json'))), len(self.sources))

    async def test_rejects_unfinished_job_foreign_result_and_unavailable_import(self):
        before = self.protected()
        for payload in ({'jobId': 'f'*32, 'audioUrl': '/audio/ace.wav'},
                        {'jobId': self.jobs['ace'], 'audioUrl': '/audio/foreign.wav'},
                        {'jobId': 'e'*32, 'audioUrl': '/audio/unimported.wav'},
                        {'jobId': '0'*32, 'audioUrl': '/audio/ace.wav'}):
            response = await self.client.post('/api/spatial/online/prepare', json=payload)
            self.assertEqual(response.status, 400, await response.text())
        self.assertEqual(self.protected(), before)

    async def test_legacy_render_failure_can_retry_without_separation_or_source_loss(self):
        source = self.sources['ace']
        folder = self.root/source['id']
        before = {str(p): file_hash(p) for p in [folder/'original.wav', *list((folder/'stems').iterdir())]}
        with patch('spatial_runtime.render_foa', side_effect=ValueError('injected render failure')):
            failed = await self.rendered(source)
        self.assertEqual(failed['state'], 'error')
        self.assertIn('injected render failure', failed['error'])
        with patch('spatial_runtime.download_audio', new_callable=AsyncMock) as download, \
                patch('spatial_runtime.asyncio.create_subprocess_exec', new_callable=AsyncMock) as worker:
            retried = await self.rendered(source)
            download.assert_not_awaited()
            worker.assert_not_awaited()
        self.assertEqual(retried['state'], 'ready', retried)
        self.assertIn('packageUrl', retried['artifacts'])
        self.assertEqual({path: file_hash(path) for path in before}, before)

    async def test_restart_keeps_legacy_project_readable_and_render_downloadable(self):
        source = self.sources['minimax']
        first = await self.rendered(source)
        self.assertEqual(first['state'], 'ready', first)
        project_path = self.root/source['id']/'project.json'
        before = self.protected()
        saved_hash = file_hash(project_path)
        client = TestClient(TestServer(self.application()))
        await client.start_server()
        try:
            restored = await (await client.get('/api/spatial/projects/'+source['id'])).json()
            self.assertEqual(restored['scene'], source['scene'])
            self.assertEqual(restored['artifacts'], first['artifacts'])
            for name in ('foaUrl', 'surroundUrl', 'packageUrl'):
                response = await client.get(restored['artifacts'][name])
                self.assertEqual(response.status, 200, name)
                await response.read()
            await self.prepare('minimax', client)
            self.assertEqual(file_hash(project_path), saved_hash)
            self.assertEqual(self.protected(), before)
        finally:
            await client.close()

    async def test_missing_weights_never_enables_fallback_or_creates_retry_project(self):
        self.weights.unlink()
        before = self.protected()
        client = TestClient(TestServer(self.application()))
        await client.start_server()
        try:
            with patch('spatial_runtime.asyncio.create_subprocess_exec', new_callable=AsyncMock) as worker:
                await self.prepare('ace', client)
                self.weights.touch()
                await self.prepare('ace', client)
                worker.assert_not_awaited()
            job = json.loads((self.root/'.online-jobs'/(self.jobs['ace']+'.json')).read_text())
            self.assertNotIn('studioProjectId', job)
            self.assertEqual(len(list(self.root.glob('*/project.json'))), len(self.sources))
            self.assertEqual(self.protected(), before)
        finally:
            await client.close()

    async def test_legacy_results_export_independently_with_original_trajectories(self):
        for model in ('ace', 'shao'):
            source = self.sources[model]
            with self.subTest(model=model):
                project = await self.rendered(source)
                self.assertEqual(project['state'], 'ready', project)
                self.assertEqual(project['scene'], source['scene'])
                self.assertEqual([obj['id'] for obj in project['scene']['objects']], list(IDS))
                response = await self.client.get(project['artifacts']['packageUrl'])
                self.assertEqual(response.status, 200)
                with zipfile.ZipFile(io.BytesIO(await response.read())) as bundle:
                    self.assertIsNone(bundle.testzip())
                    self.assertTrue(all('stems/'+sid+'.wav' in bundle.namelist() for sid in IDS))
                    self.assertEqual(json.loads(bundle.read('scene.json')), source['scene'])
                await self.prepare(model)
        self.assertEqual(len(list(self.root.glob('*/project.json'))), len(self.sources))
