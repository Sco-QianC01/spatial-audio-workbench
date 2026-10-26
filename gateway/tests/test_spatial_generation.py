import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_generation import GenerationBridge, MODELS, RemoteRejected, generation_request
from spatial_runtime import create_app


def request(model='ace', rid='a'*32):
    return dict(requestId=rid, model=model, prompt='Soft instrumental piano', duration=60 if model=='shao' else 20, seed=1283,
                **({'abcScore':'X:1\nM:4/4\nK:C\nC4 |'} if model=='yue' else {}))


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = GenerationBridge(self.temp.name)
        self.bridge.token, self.bridge.username = 'session-must-not-persist', 'ResearchQA'

    def tearDown(self):
        self.temp.cleanup()

    def test_four_models_request_mapping_and_score_contract(self):
        for item in MODELS:
            model, normalized, payload = generation_request(request(item['id']))
            self.assertEqual(payload['ditModel'], item['remote'])
            self.assertTrue(payload['instrumental'])
            if model['id'] == 'yue':
                self.assertNotIn('duration', payload)
                self.assertEqual(payload['scoreCot'], 'melody')
        for changes in [dict(model='other'),dict(seed=True),dict(duration=float('nan')),dict(duration=0),dict(prompt=''),dict(requestId='../x'),dict(endpoint='http://localhost')]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                generation_request({**request(),**changes})
        with self.assertRaises(ValueError):
            generation_request({**request('yue'),'abcScore':''})

    async def test_submit_is_durable_idempotent_and_has_no_session_secrets(self):
        with patch.object(self.bridge,'request',AsyncMock(return_value={'jobId':'remote-1'})) as upstream:
            first = await self.bridge.submit(request())
            second = await self.bridge.submit(request())
        self.assertEqual(first,second)
        upstream.assert_awaited_once()
        self.assertEqual(first['status'],'queued')
        raw=''.join(p.read_text() for p in Path(self.temp.name).glob('*.json'))
        self.assertNotIn('session-must-not-persist',raw)
        self.assertNotIn('token',raw)
        restored=GenerationBridge(self.temp.name)
        self.assertFalse(restored.state()['connected'])
        self.assertEqual(restored.state()['jobs'][0]['remoteJobId'],'remote-1')
        with self.assertRaises(ValueError):
            await restored.submit(request(rid='b'*32))
        with self.assertRaises(ValueError):
            await self.bridge.submit({**request(),'prompt':'different'})

    async def test_studio_option_is_rejected_without_upstream_submission(self):
        with patch.object(self.bridge, 'request', AsyncMock()) as upstream:
            for item in MODELS:
                value = request(item['id'])
                _, legacy, remote = generation_request(value)
                _, disabled, disabled_remote = generation_request({**value, 'prepareStudio': False})
                self.assertEqual(disabled, legacy)
                self.assertEqual(disabled_remote, remote)
                self.assertNotIn('prepareStudio', remote)
                with self.subTest(model=item['id']), self.assertRaisesRegex(ValueError, '模型原生分轨'):
                    generation_request({**value, 'prepareStudio': True})
                with self.subTest(submission=item['id']), self.assertRaisesRegex(ValueError, '模型原生分轨'):
                    await self.bridge.submit({**value, 'prepareStudio': True})
            upstream.assert_not_awaited()
        self.assertEqual(self.bridge.jobs, {})
        self.assertEqual(list(Path(self.temp.name).glob('*.json')), [])
        for invalid in (None, 0, 1, 'true', [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                generation_request({**request(), 'prepareStudio': invalid})

    async def test_disabled_studio_option_preserves_legacy_idempotency_hash(self):
        with patch.object(self.bridge, 'request', AsyncMock(return_value={'jobId': 'remote-legacy'})) as upstream:
            legacy = await self.bridge.submit(request())
            disabled = await self.bridge.submit({**request(), 'prepareStudio': False})
        self.assertEqual(disabled, legacy)
        self.assertNotIn('prepareStudio', disabled)
        upstream.assert_awaited_once()

    async def test_transport_unknown_never_reposts_and_blocks_new_jobs(self):
        with patch.object(self.bridge,'request',AsyncMock(side_effect=TimeoutError)) as upstream:
            first=await self.bridge.submit(request())
            second=await self.bridge.submit(request())
            with self.assertRaises(ValueError):
                await self.bridge.submit(request(rid='b'*32))
        self.assertEqual(first['status'],'submission_unknown')
        self.assertEqual(first,second)
        upstream.assert_awaited_once()

    async def test_known_rejection_allows_corrected_new_submission(self):
        with patch.object(self.bridge,'request',AsyncMock(side_effect=[RemoteRejected(400),{'jobId':'remote-2'}])):
            self.assertEqual((await self.bridge.submit(request()))['status'],'failed')
            self.assertEqual((await self.bridge.submit(request(rid='b'*32)))['status'],'queued')

    async def test_invalid_success_never_publishes_status_or_foreign_urls(self):
        with patch.object(self.bridge,'request',AsyncMock(return_value={'jobId':'remote-1'})):
            await self.bridge.submit(request())
        for urls in [[],['http://localhost/private'],['/audio/../x.wav']]:
            with patch.object(self.bridge,'request',AsyncMock(return_value={'status':'succeeded','result':{'audioUrls':urls}})),self.assertRaises(ValueError):
                await self.bridge.poll('a'*32)
            self.assertEqual(self.bridge.jobs['a'*32]['status'],'queued')
        with patch.object(self.bridge,'request',AsyncMock(return_value={'status':'succeeded','result':{'audioUrls':['/audio/result.wav']}})):
            result=await self.bridge.poll('a'*32)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(self.bridge.source('a'*32,'/audio/result.wav')['model'],'ace')
        with self.assertRaises(ValueError):
            self.bridge.source('a'*32,'/audio/other.wav')
        self.bridge.imported('a'*32,'c'*32)
        self.assertEqual(GenerationBridge(self.temp.name).jobs['a'*32]['projectId'],'c'*32)

    async def test_connect_uses_normal_setup_without_exposing_token(self):
        self.bridge.token=None
        with patch.object(self.bridge,'request',AsyncMock(return_value={'token':'new-memory-session'})) as upstream:
            value=await self.bridge.connect('ResearchQA')
        self.assertTrue(value['connected'])
        self.assertNotIn('new-memory-session',json.dumps(value))
        upstream.assert_awaited_once_with('POST','/api/auth/setup',{'username':'ResearchQA'},authenticated=False)

    async def test_interrupted_submit_restores_as_unknown(self):
        job={'id':'a'*32,'status':'submitting','createdAt':1,'username':'ResearchQA'}
        self.bridge.save(job)
        self.assertEqual(GenerationBridge(self.temp.name).jobs['a'*32]['status'],'submission_unknown')

    async def test_restart_keeps_old_active_jobs_and_all_idempotency_records(self):
        with patch.object(self.bridge,'request',AsyncMock(return_value={'jobId':'remote-1'})):
            original=await self.bridge.submit(request(rid='0'*32))
        original['createdAt']=0
        self.bridge.save(original)
        for index in range(101):
            self.bridge.save(dict(id=f'{index+1:032x}',status='succeeded',createdAt=index+1))
        restored=GenerationBridge(self.temp.name)
        self.assertEqual(len(restored.jobs),102)
        self.assertIn('0'*32,[job['id'] for job in restored.state()['jobs']])
        with patch.object(restored,'request',AsyncMock()) as upstream:
            self.assertEqual(await restored.submit(request(rid='0'*32)),original)
            restored.token='memory-only'
            with self.assertRaises(ValueError):
                await restored.submit(request(rid='b'*32))
            upstream.assert_not_awaited()

    async def test_concurrent_polls_share_terminal_result_without_another_upstream_get(self):
        with patch.object(self.bridge,'request',AsyncMock(return_value={'jobId':'remote-1'})):
            await self.bridge.submit(request())
        async def result(*args):
            await asyncio.sleep(.01)
            return {'status':'succeeded','result':{'audioUrls':['/audio/result.wav']}}
        with patch.object(self.bridge,'request',AsyncMock(side_effect=result)) as upstream:
            first,second=await asyncio.gather(self.bridge.poll('a'*32),self.bridge.poll('a'*32))
        self.assertEqual(first['status'],'succeeded')
        self.assertEqual(first,second)
        upstream.assert_awaited_once()


class GenerationApiTests(AioHTTPTestCase):
    async def get_application(self):
        self.temp=tempfile.TemporaryDirectory()
        return create_app(Path(self.temp.name)/'projects','/missing/python','/missing/weights')

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.temp.cleanup()

    async def test_same_origin_routes_validation_and_session_secrecy(self):
        response=await self.client.get('/api/spatial/online/generation')
        value=await response.json()
        self.assertEqual(len(value['models']),4)
        self.assertFalse(value['connected'])
        response=await self.client.post('/api/spatial/online/connect',json={'username':'ResearchQA'},headers={'Origin':'http://foreign.example'})
        self.assertEqual(response.status,403)
        response=await self.client.post('/api/spatial/online/generate',json=request())
        self.assertEqual(response.status,400)
        with patch('spatial_generation.GenerationBridge.request',AsyncMock(return_value={'token':'not-for-browser'})):
            response=await self.client.post('/api/spatial/online/connect',json={'username':'ResearchQA'})
            value=await response.json()
        self.assertTrue(value['connected'])
        self.assertNotIn('not-for-browser',json.dumps(value))
        with patch('spatial_generation.GenerationBridge.request',AsyncMock(return_value={'jobId':'remote-1'})):
            response=await self.client.post('/api/spatial/online/generate',json=request())
            value=await response.json()
        self.assertEqual(value['remoteJobId'],'remote-1')
        response=await self.client.post('/api/spatial/online/import',json={'audioUrl':'/audio/foreign.wav','jobId':'a'*32})
        self.assertEqual(response.status,400)
        self.assertIn('不匹配',(await response.json())['error'])

    async def test_studio_generation_route_rejects_without_upstream_post(self):
        with patch('spatial_generation.GenerationBridge.request', AsyncMock(return_value={'token': 'memory-only'})):
            response = await self.client.post('/api/spatial/online/connect', json={'username': 'ResearchQA'})
            self.assertEqual(response.status, 200)
        with patch('spatial_generation.GenerationBridge.request', AsyncMock()) as upstream:
            for item in MODELS:
                with self.subTest(model=item['id']):
                    response = await self.client.post('/api/spatial/online/generate',
                                                      json={**request(item['id']), 'prepareStudio': True})
                    self.assertEqual(response.status, 400)
                    self.assertIn('模型原生分轨', (await response.json())['error'])
            upstream.assert_not_awaited()
        response = await self.client.get('/api/spatial/online/generation')
        self.assertEqual((await response.json())['jobs'], [])

    async def test_workflow_docs_are_available_without_exposing_other_repo_files(self):
        for path in ('SPATIAL-AUDIO.md','SPATIAL-VIEW.md','SPATIAL-FOUR-MODEL-WORKFLOW.md',
                     'SPATIAL-STEM-ROUTES.md','examples/ace-native-stems-plan.json'):
            response=await self.client.get('/docs/'+path)
            self.assertEqual(response.status,200)
        self.assertEqual((await self.client.get('/gateway/spatial_generation.py')).status,404)
