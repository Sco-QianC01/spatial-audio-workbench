"""Synthetic fixture contract tests, not evidence of actual ACE model generation."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

import aiohttp
from aiohttp.test_utils import AioHTTPTestCase
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_audio import IDS, NATIVE_IDS, NATIVE_SCHEMA, canonical_hash, default_scene
from spatial_native import BASE_MODEL, import_native_bundle
from spatial_runtime import create_app


def wav(samples, rate=48000, subtype='FLOAT'):
    stream = io.BytesIO()
    sf.write(stream, samples, rate, format='WAV', subtype=subtype)
    return stream.getvalue()


def fixture_bundle(case=None, ids=('guitar', 'keyboard'), rate=48000):
    """Deliberately synthetic producer-supplied receipts, never remote attestations."""
    frames = rate // 5
    t = np.arange(frames) / rate
    context = wav(np.column_stack((.04*np.sin(2*np.pi*110*t),)*2), rate)
    context_hash = hashlib.sha256(context).hexdigest()
    manifest = {'schema': NATIVE_SCHEMA,
                'producer': {'model': BASE_MODEL, 'api': 'http://127.0.0.1:18000', 'fixture': True},
                'context': {'file': 'context.wav', 'sha256': context_hash},
                'timeline': {'sampleRate': rate, 'frames': frames, 'startFrame': 0}, 'tracks': []}
    files = {'context.wav': context}
    for index, sid in enumerate(ids):
        samples = np.column_stack((.02*np.sin(2*np.pi*(330+index*220)*t),)*2).astype('float32')
        if case in ('duplicate', 'duplicate-pcm') and index == 1:
            samples = np.column_stack((.02*np.sin(2*np.pi*330*t),)*2).astype('float32')
        audio = wav(samples, rate, 'DOUBLE' if case == 'duplicate-pcm' and index == 1 else 'FLOAT')
        digest = hashlib.sha256(audio).hexdigest()
        request = {'task_type': 'lego', 'model': BASE_MODEL, 'seed': index,
                   'instruction': f'Generate the {sid} track based on the audio context:',
                   'prompt': f'SYNTHETIC TEST FIXTURE {sid}', 'contextSha256': context_hash,
                   'audio_format': 'wav'}
        if case == 'instruction' and index == 0:
            request['instruction'] = 'Extract a finished song'
        task_id = f'fixture-{index}'
        track = {'id': sid, 'file': f'stems/{sid}.wav', 'sha256': digest,
                 'task': {'taskType': 'lego', 'model': BASE_MODEL, 'seed': index,
                          'prompt': request['prompt'], 'contextSha256': context_hash,
                          'id': task_id, 'requestHash': canonical_hash(request),
                          'responseFile': f'receipts/{sid}.json'}}
        receipt = {'request': request, 'submit': {'task_id': task_id},
                   'result': {'task_id': task_id, 'status': 0 if case == 'failed' else 1,
                              'result': json.dumps([{'status': 1, 'dit_model':
                                  'acestep-v15-sft' if case == 'result-model' else BASE_MODEL,
                                  'file': f'/v1/audio?path=fixture-{sid}.wav'}])},
                   'audioSha256': digest}
        files[track['file']] = audio
        files[track['task']['responseFile']] = json.dumps(receipt).encode()
        manifest['tracks'].append(track)
    if case == 'sha':
        # Keep the claimed receipt/manifest hashes consistent but alter downloaded bytes.
        first = manifest['tracks'][0]['file']
        files[first] = files[first][:-1] + bytes([files[first][-1] ^ 1])
    elif case == 'frames':
        manifest['timeline']['frames'] += 1
    elif case == 'model':
        manifest['producer']['model'] = 'acestep-v15-sft'
    elif case == 'traversal':
        files['../escaped.wav'] = b'cannot escape'
    files['manifest.json'] = json.dumps(manifest).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_STORED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue(), files


class NativeFileContractTests(unittest.TestCase):
    def test_two_and_twelve_tracks_resample_only_working_copies_and_exclude_context(self):
        for ids, rate in ((('guitar', 'keyboard'), 48000), (NATIVE_IDS, 44100)):
            with self.subTest(count=len(ids)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bundle, files = fixture_bundle(ids=ids, rate=rate)
                (root/'fixture.zip').write_bytes(bundle)
                folder = root/'project'
                folder.mkdir()
                project = import_native_bundle(root/'fixture.zip', folder, 'a'*32)
                self.assertEqual(project['state'], 'ready')
                self.assertEqual([item['id'] for item in project['scene']['objects']], list(ids))
                self.assertEqual(project['scene']['speakerLayout'], '7.1.4')
                self.assertEqual(project['input']['frames'], 8820)
                self.assertEqual(project['sceneHash'], canonical_hash(project['scene']))
                summed = np.zeros((8820, 2))
                for sid in ids:
                    self.assertEqual((folder/'native-source/stems'/f'{sid}.wav').read_bytes(), files[f'stems/{sid}.wav'])
                    working = folder/'stems'/f'{sid}.wav'
                    info = sf.info(working)
                    self.assertEqual((info.samplerate, info.channels, info.subtype), (44100, 2, 'FLOAT'))
                    summed += sf.read(working)[0]
                self.assertEqual((folder/'native-source/context.wav').read_bytes(), files['context.wav'])
                np.testing.assert_allclose(sf.read(folder/'original.wav')[0], summed, atol=1e-7)
                metadata = json.loads((folder/'stems/separation.json').read_text())
                self.assertTrue(metadata['validation']['sourcePreserved'])
                self.assertIn('context is excluded', metadata['validation']['baseline'])


class NativeWorkflowApiTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'PATH': '/home/codex/.local/bin'+os.pathsep+os.environ.get('PATH', '')})
        self.env.start()
        self.legacy = {'id': 'c'*32, 'state': 'ready', 'error': None,
                       'input': {'durationSeconds': .2, 'frames': 8820, 'sampleRate': 44100, 'channels': 2},
                       'separation': {'model': {'name': 'legacy fixture'}}, 'artifacts': {}}
        self.legacy['scene'] = default_scene(self.legacy)
        self.legacy['sceneHash'] = canonical_hash(self.legacy['scene'])
        folder = self.root/self.legacy['id']
        folder.mkdir()
        (folder/'project.json').write_text(json.dumps(self.legacy))
        self.weights = self.root/'fixture-weights.pt'
        self.weights.write_bytes(b'synthetic-test-weights')
        return create_app(self.root, sys.executable, self.weights)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.env.stop()
        self.temp.cleanup()

    async def upload(self, bundle):
        form = aiohttp.FormData()
        form.add_field('file', bundle, filename='synthetic-fixture.zip', content_type='application/zip')
        return await self.client.post('/api/spatial/native/import', data=form)

    async def completed(self, base):
        for _ in range(200):
            response = await self.client.get(base)
            value = await response.json()
            if value['state'] not in ('rendering', 'analyzing', 'mixing'):
                self.assertEqual(value['state'], 'ready', value)
                return value
            await asyncio.sleep(.05)
        self.fail('native fixture task exceeded bounded ten-second wait')

    async def test_import_render_download_packages_and_analyze_real_worker(self):
        bundle, files = fixture_bundle()
        response = await self.upload(bundle)
        self.assertEqual(response.status, 201, await response.text())
        project = await response.json()
        base = '/api/spatial/projects/'+project['id']
        source_url = base+'/files/native-source/stems/guitar.wav'
        response = await self.client.get(source_url)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), files['stems/guitar.wav'])
        response = await self.client.post(base+'/render', json={'expectedSceneHash': project['sceneHash']})
        self.assertEqual(response.status, 202, await response.text())
        project = await self.completed(base)
        links = project['artifacts']
        self.assertTrue(links, project)
        self.assertIn('surround_7_1_4.wav', links['surroundUrl'])
        for name in ('surroundUrl', 'surroundReportUrl', 'packageUrl', 'studioPackageUrl'):
            response = await self.client.get(links[name])
            self.assertEqual(response.status, 200, name)
            data = await response.read()
            if name == 'surroundUrl':
                self.assertEqual(sf.info(io.BytesIO(data)).channels, 12)
            elif name == 'packageUrl':
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertIn('stems/guitar.wav', archive.namelist())
                    self.assertIn('stems/keyboard.wav', archive.namelist())
                    self.assertIn('native-source/receipts/guitar.json', archive.namelist())
                    self.assertNotIn('stems/other.wav', archive.namelist())
            elif name == 'studioPackageUrl':
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(len([name for name in archive.namelist() if name.startswith('surround_channels/')]), 12)
        response = await self.client.post(base+'/mix/analyze', json={})
        self.assertEqual(response.status, 202, await response.text())
        analyzed = await self.completed(base)
        self.assertEqual([item['id'] for item in analyzed['analysis']['stems']], ['guitar', 'keyboard'])
        self.assertLess(analyzed['analysis']['reconstructionResidual']['samplePeak'], 2e-6)
        response = await self.client.post(base+'/mix/plan', json={'intent': 'preserve'})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(len((await response.json())['mixPlan']['stems']), 2)

    async def test_rejected_bundles_never_publish_partial_projects(self):
        before = {path.name for path in self.root.iterdir()}
        for case in ('sha', 'frames', 'duplicate', 'duplicate-pcm', 'failed', 'model', 'result-model', 'instruction', 'traversal'):
            with self.subTest(case=case):
                response = await self.upload(fixture_bundle(case)[0])
                self.assertEqual(response.status, 400, await response.text())
                self.assertEqual({path.name for path in self.root.iterdir()}, before)
                self.assertFalse((self.root/'escaped.wav').exists())
        for ids in (('guitar',), NATIVE_IDS+('guitar',), ('guitar', 'other')):
            with self.subTest(ids=ids):
                response = await self.upload(fixture_bundle(ids=ids)[0])
                self.assertEqual(response.status, 400, await response.text())
                self.assertEqual({path.name for path in self.root.iterdir()}, before)

    async def imported_stereo(self):
        form = aiohttp.FormData()
        form.add_field('file', fixture_bundle()[1]['context.wav'], filename='synthetic-stereo.wav', content_type='audio/wav')
        response = await self.client.post('/api/spatial/import', data=form)
        self.assertEqual(response.status, 200, await response.text())
        project = await response.json()
        self.assertEqual(project['state'], 'imported')
        self.assertIsNone(project['workflow'])
        return project

    def fixture_separator(self, outcome='success'):
        async def spawn(*args, **kwargs):
            stage = Path(args[3])
            stage.mkdir()
            audio, _ = sf.read(args[2], dtype='float32', always_2d=True)
            report = {'sources': list(IDS), 'model': {'name': 'SYNTHETIC TEST FIXTURE', 'strictLoad': True},
                      'input': {'frames': len(audio)}, 'outputs': {sid: {'fixture': True} for sid in IDS}}
            for index, sid in enumerate(IDS):
                data = audio[:-1] if outcome == 'frames' and index == 0 else audio
                sf.write(stage/(sid+'.wav'), data/4, 44100, subtype='FLOAT')
            (stage/'separation.json').write_text(json.dumps(report))
            kwargs['stdout'].write(b'synthetic separation fixture, no model execution\n')
            proc = Mock(returncode=1 if outcome == 'process-failed' else 0)
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc
        return spawn

    async def test_explicit_stereo_route_requires_declaration_then_publishes_neutral_estimates(self):
        project = await self.imported_stereo()
        base = '/api/spatial/projects/'+project['id']
        folder = self.root/project['id']
        original_hash = hashlib.sha256((folder/'original.wav').read_bytes()).hexdigest()
        with patch('spatial_runtime.asyncio.create_subprocess_exec', new_callable=AsyncMock) as process:
            for payload in ({}, {'workflow': 'native-stems-first-v1'},
                            {'workflow': 'separated-stereo-v1', 'fallback': True}):
                response = await self.client.post(base+'/separate', json=payload)
                self.assertEqual(response.status, 400, await response.text())
            process.assert_not_awaited()
        with patch('spatial_runtime.asyncio.create_subprocess_exec', side_effect=self.fixture_separator()) as process:
            response = await self.client.post(base+'/separate', json={'workflow': 'separated-stereo-v1'})
            self.assertEqual(response.status, 202, await response.text())
            for _ in range(100):
                result = await (await self.client.get(base)).json()
                if result['state'] != 'separating':
                    break
                await asyncio.sleep(.01)
            self.assertEqual(result['state'], 'ready', result)
            process.assert_awaited_once()
            self.assertIn('spatial_separation.py', process.call_args.args[1])
        self.assertEqual(result['workflow'], 'separated-stereo-v1')
        self.assertEqual(result['source']['kind'], 'estimated-stems')
        self.assertEqual(result['source']['workflow'], 'separated-stereo-v1')
        self.assertNotIn('nativeStems', result)
        self.assertEqual(result['separation']['model']['name'], 'SYNTHETIC TEST FIXTURE')
        self.assertEqual(result['scene']['speakerLayout'], '7.1.4')
        for index, obj in enumerate(result['scene']['objects']):
            self.assertEqual(obj['label'], f'轨道{index+1:02d}')
            self.assertEqual((obj['elevationDeg'], obj['widthDeg'], obj['keyframes']), (0, 30, []))
        np.testing.assert_allclose([o['azimuthDeg'] for o in result['scene']['objects']], np.linspace(-30, 30, 4))
        response = await self.client.post(base+'/scene', json={'scene': result['scene'], 'expectedSceneHash': result['sceneHash']})
        self.assertEqual(response.status, 200)
        saved = await response.json()
        self.assertEqual(saved['scene'], result['scene'])
        self.assertEqual(saved['sceneHash'], result['sceneHash'])
        self.assertEqual(hashlib.sha256((folder/'original.wav').read_bytes()).hexdigest(), original_hash)
        self.assertTrue((folder/'separation.log').is_file())
        self.assertFalse(list(folder.glob('.separation-*')))
        # Selection enables estimated-stem mixing, but does not silently start it.
        response = await self.client.post(base+'/mix/plan', json={'intent': 'preserve'})
        self.assertEqual(response.status, 400)
        self.assertIn('分析', (await response.json())['error'])

    async def test_explicit_stereo_preserves_online_provenance_and_never_runs_without_weights(self):
        imported = await self.imported_stereo()
        original = self.root/imported['id']/'original.wav'
        project = json.loads(json.dumps(imported))
        project['id'] = 'd'*32
        project['input']['audioUrl'] = '/api/spatial/projects/'+project['id']+'/files/original.wav'
        provenance = {'url': 'https://fixture.invalid/audio.wav',
                      'generation': {'id': 'fixture-online-job', 'model': 'ace', 'seed': 1283}}
        project['source'] = provenance
        folder = self.root/project['id']
        folder.mkdir()
        (folder/'original.wav').write_bytes(original.read_bytes())
        (folder/'project.json').write_text(json.dumps(project))
        base = '/api/spatial/projects/'+project['id']
        self.weights.unlink()
        with patch('spatial_runtime.asyncio.create_subprocess_exec', new_callable=AsyncMock) as process:
            response = await self.client.post(base+'/separate', json={'workflow': 'separated-stereo-v1'})
            self.assertEqual(response.status, 400)
            process.assert_not_awaited()
        self.assertEqual((await (await self.client.get(base)).json())['state'], 'imported')
        self.weights.write_bytes(b'synthetic-test-weights')
        with patch('spatial_runtime.asyncio.create_subprocess_exec', side_effect=self.fixture_separator()):
            response = await self.client.post(base+'/separate', json={'workflow': 'separated-stereo-v1'})
            self.assertEqual(response.status, 202, await response.text())
            for _ in range(100):
                result = await (await self.client.get(base)).json()
                if result['state'] != 'separating':
                    break
                await asyncio.sleep(.01)
        self.assertEqual(result['state'], 'ready', result)
        for key, value in provenance.items():
            self.assertEqual(result['source'][key], value)
        self.assertEqual(result['source']['kind'], 'estimated-stems')
        self.assertEqual(result['source']['workflow'], 'separated-stereo-v1')

    async def test_explicit_stereo_failure_never_publishes_partial_stems_and_releases_lock(self):
        for outcome in ('process-failed', 'frames'):
            with self.subTest(outcome=outcome):
                project = await self.imported_stereo()
                base = '/api/spatial/projects/'+project['id']
                folder = self.root/project['id']
                with patch('spatial_runtime.asyncio.create_subprocess_exec', side_effect=self.fixture_separator(outcome)):
                    response = await self.client.post(base+'/separate', json={'workflow': 'separated-stereo-v1'})
                    self.assertEqual(response.status, 202, await response.text())
                    for _ in range(100):
                        state = await (await self.client.get(base)).json()
                        if state['state'] != 'separating':
                            break
                        await asyncio.sleep(.01)
                self.assertEqual(state['state'], 'error')
                self.assertIsNone(state['scene'])
                self.assertIsNone(state['separation'])
                self.assertFalse((folder/'stems').exists())
                self.assertFalse(list(folder.glob('.separation-*')))
                self.assertTrue((folder/'separation.log').is_file())
                status = await (await self.client.get('/api/spatial/status')).json()
                self.assertFalse(status['working'])

    async def test_legacy_read_preserved_and_forbidden_paths_do_no_remote_or_process_work(self):
        base = '/api/spatial/projects/'+self.legacy['id']
        with patch('spatial_runtime.download_audio', new_callable=AsyncMock) as download, \
                patch('spatial_runtime.GenerationBridge.request', new_callable=AsyncMock) as remote, \
                patch('spatial_runtime.asyncio.create_subprocess_exec', new_callable=AsyncMock) as process:
            for route, payload in ((base+'/separate', {}), ('/api/spatial/online/prepare', {}),
                                   (base+'/mix/analyze', {}), (base+'/mix/plan', {'intent': 'preserve'})):
                response = await self.client.post(route, json=payload)
                self.assertEqual(response.status, 400, await response.text())
            download.assert_not_awaited()
            remote.assert_not_awaited()
            process.assert_not_awaited()
        response = await self.client.get(base)
        self.assertEqual(response.status, 200)
        project = await response.json()
        self.assertEqual(project['scene'], self.legacy['scene'])
        self.assertEqual([item['id'] for item in project['scene']['objects']], list(IDS))
        self.assertNotIn('nativeStems', project)
        saved = json.loads((self.root/self.legacy['id']/'project.json').read_text())
        self.assertEqual(saved, self.legacy)


if __name__ == '__main__':
    unittest.main()
