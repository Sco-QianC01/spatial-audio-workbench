"""Fake HTTP contract tests only: these tests do not generate audio with ACE."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_native import import_native_bundle

spec = importlib.util.spec_from_file_location('ace_native_producer', Path(__file__).resolve().parents[2] / 'tools/generate-ace-native-stems.py')
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


class NativeProducerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = self.root / 'source.wav'
        sf.write(self.context, np.full((4410, 2), .125), 44100, subtype='PCM_16')
        self.plan = self.root / 'plan.json'
        self.plan.write_text(json.dumps({'tracks': [dict(id=sid, prompt=f'Only {sid}, gentle sustained performance', seed=i) for i, sid in enumerate(('bass', 'strings'))]}))
        self.output = self.root / 'out'
        self.models = [dict(name=producer.MODEL, is_loaded=True)]
        self.posts = []
        self.submissions = []
        self.bad_submit = False
        self.wrong_frames = False
        self.copy_context = False
        self.reported_model = producer.MODEL
        app = web.Application()
        app.router.add_get('/v1/models', self.get_models)
        app.router.add_post('/release_task', self.submit)
        app.router.add_post('/query_result', self.query)
        app.router.add_get('/v1/audio', self.audio)
        self.server = TestServer(app)
        await self.server.start_server()
        self.endpoint = str(self.server.make_url('')).rstrip('/')

    async def asyncTearDown(self):
        await self.server.close()
        self.temp.cleanup()

    async def get_models(self, request):
        if getattr(self, 'html_models', False):
            return web.Response(text='<html>Studio UI</html>', content_type='text/html')
        return web.json_response({'data': {'models': self.models}, 'code': 200})

    async def submit(self, request):
        self.posts.append('/release_task')
        parts = await request.post()
        fields = {key: value for key, value in parts.items() if key != 'src_audio'}
        self.assertEqual(fields['task_type'], 'lego')
        self.assertEqual(fields['model'], producer.MODEL)
        self.assertEqual(fields['thinking'], 'false')
        self.assertEqual(fields['batch_size'], '1')
        self.assertEqual(fields['audio_format'], 'wav')
        self.assertEqual(parts['src_audio'].file.read(), self.context.read_bytes())
        self.submissions.append(fields)
        return web.json_response({'data': {} if self.bad_submit else {'task_id': f'task-{len(self.submissions)}'}, 'code': 200})

    async def query(self, request):
        self.posts.append('/query_result')
        task_id = (await request.json())['task_id_list'][0]
        audio = dict(file=f'/v1/audio?path={task_id}.wav', status=1, dit_model=self.reported_model)
        if self.reported_model is None:
            del audio['dit_model']
        return web.json_response({'data': [dict(task_id=task_id, status=1, result=json.dumps([audio]))], 'code': 200})

    async def audio(self, request):
        stream = io.BytesIO()
        if self.copy_context and request.query['path'] == 'task-1.wav':
            samples, _ = sf.read(self.context, always_2d=True)
        else:
            samples = np.full((4411 if self.wrong_frames else 4410, 2), .03 if request.query['path'] == 'task-1.wav' else .06)
        sf.write(stream, samples, 44100, format='WAV', subtype='FLOAT')
        return web.Response(body=stream.getvalue(), content_type='audio/wav')

    async def run_producer(self):
        return await producer.generate(self.context, self.plan, self.endpoint, self.output, poll_seconds=0)

    async def test_missing_or_unloaded_base_blocks_before_any_post(self):
        for models in ([dict(name='acestep-v15-xl-sft')], [dict(name=producer.MODEL, is_loaded=False)]):
            self.models = models
            with self.assertRaisesRegex(producer.Blocked, 'Base model unavailable'):
                await self.run_producer()
            self.assertEqual(self.posts, [])
            self.assertFalse((self.output / 'bundle.zip').exists())
            self.assertEqual(json.loads((self.output / 'blocked.json').read_text())['status'], 'blocked')

    async def test_html_capability_page_blocks_without_any_post(self):
        self.html_models = True
        with self.assertRaisesRegex(producer.Blocked, 'non-JSON'):
            await self.run_producer()
        self.assertEqual(self.posts, [])
        self.assertFalse((self.output / 'bundle.zip').exists())

    async def test_success_package_receipts_and_resume_without_resubmission(self):
        bundle = await self.run_producer()
        with zipfile.ZipFile(bundle) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['schema'], 'ace-native-stems-v1')
            self.assertEqual(manifest['timeline'], dict(sampleRate=44100, frames=4410, startFrame=0))
            self.assertEqual(archive.read(manifest['context']['file']), self.context.read_bytes())
            for track in manifest['tracks']:
                receipt = json.loads(archive.read(track['task']['responseFile']))
                self.assertEqual(track['task']['requestHash'], producer.canonical_hash(receipt['request']))
                self.assertEqual(receipt['submit']['task_id'], track['task']['id'])
                self.assertEqual(receipt['audioSha256'], track['sha256'])
                self.assertEqual(receipt['request']['contextSha256'], manifest['context']['sha256'])
                self.assertEqual(track['outputFormat']['container'], 'WAV')
                self.assertEqual(receipt['result']['status'], 1)
        count = len(self.posts)
        await self.run_producer()
        self.assertEqual(len(self.posts), count)
        self.assertEqual(len(self.submissions), 2)

    async def test_produced_bundle_imports_into_real_native_gateway(self):
        bundle = await self.run_producer()
        folder = self.root / 'imported'
        folder.mkdir()
        project = import_native_bundle(bundle, folder, 'a' * 32)
        self.assertEqual(project['state'], 'ready')
        self.assertEqual(project['input']['frames'], 4410)
        self.assertEqual(project['nativeStems']['validation']['nativeRate'], 44100)
        self.assertEqual(project['source']['kind'], 'ace-native-stems')
        self.assertEqual((folder / 'native-source/context.wav').read_bytes(), self.context.read_bytes())
        baseline, rate = sf.read(folder / 'original.wav', always_2d=True)
        self.assertEqual(rate, 44100)
        np.testing.assert_allclose(baseline, .09, atol=1e-7)
        self.assertNotEqual(producer.sha256(folder / 'stems/bass.wav'), producer.sha256(folder / 'stems/strings.wav'))

    async def test_unknown_submission_is_durable_and_never_repeated(self):
        self.bad_submit = True
        with self.assertRaisesRegex(producer.Blocked, 'submission returned no valid task ID'):
            await self.run_producer()
        with self.assertRaisesRegex(producer.Blocked, 'submission unknown'):
            await self.run_producer()
        self.assertEqual(len(self.submissions), 1)
        self.assertFalse((self.output / 'bundle.zip').exists())

    async def test_known_task_id_resumes_poll_without_resubmission(self):
        self.wrong_frames = True
        with self.assertRaisesRegex(producer.Blocked, 'timeline mismatch'):
            await self.run_producer()
        self.wrong_frames = False
        await self.run_producer()
        self.assertEqual(len(self.submissions), 2)
        self.assertEqual(self.posts[:3], ['/release_task', '/query_result', '/query_result'])

    async def test_wrong_reported_model_blocks(self):
        self.reported_model = 'acestep-v15-xl-sft'
        with self.assertRaisesRegex(producer.Blocked, 'different DiT model'):
            await self.run_producer()
        self.assertFalse((self.output / 'bundle.zip').exists())

    async def test_missing_reported_model_blocks(self):
        self.reported_model = None
        with self.assertRaisesRegex(producer.Blocked, 'different DiT model'):
            await self.run_producer()
        self.assertFalse((self.output / 'bundle.zip').exists())

    async def test_stem_equal_to_context_decoded_pcm_blocks(self):
        self.copy_context = True
        with self.assertRaisesRegex(producer.Blocked, 'stem duplicates the context decoded samples'):
            await self.run_producer()
        self.assertFalse((self.output / 'bundle.zip').exists())

    def test_output_lock_rejects_overlap_and_releases(self):
        path = self.root / 'lock'
        with producer.output_lock(path):
            with self.assertRaisesRegex(producer.Blocked, 'holds the output lock'):
                with producer.output_lock(path):
                    self.fail('overlapping lock acquired')
        with producer.output_lock(path):
            pass

    def test_windows_lock_branch_locks_and_releases_one_byte(self):
        # Exercises dispatch only; physical Windows file locking needs host verification.
        native = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock())
        path = self.root / 'windows-lock'
        with patch.object(producer.os, 'name', 'nt'), patch.dict(sys.modules, msvcrt=native):
            with producer.output_lock(path):
                self.assertEqual(path.read_bytes(), b'0')
                self.assertEqual(native.locking.call_args.args[1:], (native.LK_NBLCK, 1))
        self.assertEqual(native.locking.call_args.args[1:], (native.LK_UNLCK, 1))

    def test_endpoint_and_plan_boundaries(self):
        for endpoint in ('http://evil.example', 'http://user:secret@localhost:8001', 'http://localhost:8001/private'):
            with self.assertRaises(producer.Blocked):
                producer.endpoint_url(endpoint)
        self.assertEqual(producer.endpoint_url(producer.SCHOOL), producer.SCHOOL)
        self.plan.write_text(json.dumps({'tracks': [dict(id='bass', prompt='Bass', seed=1)] * 2}))
        with self.assertRaises(producer.Blocked):
            producer.plan_tracks(self.plan)


if __name__ == '__main__':
    unittest.main()
