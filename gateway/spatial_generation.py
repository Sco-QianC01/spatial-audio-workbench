"""Fixed school generation API; session token stays in memory, jobs survive restarts."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar
from spatial_online import ORIGIN, METADATA_BYTES, validate_audio_url
from spatial_native import WORKFLOW

MODELS = [
    dict(id='ace', label='ACE XL-SFT', remote='acestep-v15-xl-sft', durationMode='target', minDuration=10, maxDuration=300, requiresScore=False),
    dict(id='shao', label='Shao · 韶元', remote='liujiafeng/Shao-MusicGeneration-v1.0', durationMode='model', minDuration=60, maxDuration=60, requiresScore=False),
    dict(id='minimax', label='MiniMax Music3', remote='MiniMaxAI/MiniMax-Music3', durationMode='target', minDuration=10, maxDuration=300, requiresScore=False),
    dict(id='yue', label='YuE2 · ABC 乐谱', remote='m-a-p/YuE2-3B', durationMode='model', minDuration=10, maxDuration=300, requiresScore=True),
]
ACTIVE = {'submitting', 'queued', 'running', 'submission_unknown'}


class RemoteRejected(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'科大接口返回HTTP {status}；请检查账号或服务状态。')


def generation_request(value):
    if not isinstance(value, dict) or set(value) - {'requestId', 'model', 'prompt', 'duration', 'seed', 'abcScore', 'prepareStudio'}:
        raise ValueError('生成参数包含不支持的字段。')
    if 'prepareStudio' in value and type(value['prepareStudio']) is not bool:
        raise ValueError('棚内准备选项须为布尔值。')
    if value.get('prepareStudio') is True:
        raise ValueError('生成阶段不自动分离或制作7.1.4；请在结果中明确选择本地分离路线，或导入模型原生分轨。')
    if not isinstance(value.get('requestId'), str) or not re.fullmatch('[a-f0-9]{32}', value['requestId']):
        raise ValueError('需要有效的本地请求编号。')
    model = next((item for item in MODELS if item['id'] == value.get('model')), None)
    if not model:
        raise ValueError('仅支持当前指定的四个科大模型。')
    prompt = value.get('prompt')
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000 or '\x00' in prompt:
        raise ValueError('提示词须为1至4000个字符。')
    duration, seed = value.get('duration'), value.get('seed')
    if type(duration) not in (int, float) or not model['minDuration'] <= duration <= model['maxDuration']:
        raise ValueError('目标时长超出该模型允许范围。')
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError('Seed须为0至2147483647的整数。')
    score = value.get('abcScore', '')
    if not isinstance(score, str) or len(score) > 60000 or '\x00' in score:
        raise ValueError('ABC乐谱格式或长度无效。')
    if model['requiresScore'] and (not re.search(r'^X:\s*\d+', score, re.M) or not re.search(r'^K:\s*\S+', score, re.M)):
        raise ValueError('YuE2需要用户提供含X:与K:头部的ABC乐谱。')
    if score and not model['requiresScore']:
        raise ValueError('当前仅YuE2接受ABC乐谱。')
    normalized = dict(requestId=value['requestId'], model=model['id'], prompt=prompt.strip(), duration=duration, seed=seed)
    if value.get('prepareStudio') is True:
        normalized['prepareStudio'] = True
    if model['requiresScore']:
        normalized['abcScore'] = score.strip()
    payload = dict(ditModel=model['remote'], customMode=False, instrumental=True,
                   title='空间音频_' + model['id'], songDescription=prompt.strip(), style=prompt.strip(), lyrics='',
                   batchSize=1, inferenceSteps=50, thinking=False, seed=seed, randomSeed=False,
                   audioFormat='mp3', taskType='text2music')
    if model['id'] != 'yue':
        payload['duration'] = duration
    else:
        payload.update(abcScore=score.strip(), scoreFormat='ABC', scoreCot='melody')
    return model, normalized, payload


class GenerationBridge:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.token = None
        self.username = None
        self.jobs = {}
        self.lock = asyncio.Lock()
        self.poll_locks = {}
        for path in self.folder.glob('*.json'):
            try:
                job = json.loads(path.read_text(encoding='utf-8'))
                if not re.fullmatch('[a-f0-9]{32}', job['id']) or path.stem != job['id']:
                    continue
                if job['status'] == 'submitting':
                    job.update(status='submission_unknown', error='服务在提交期间中断；不会自动重复生成。')
                self.jobs[job['id']] = job
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def save(self, job):
        self.folder.mkdir(parents=True, exist_ok=True)
        pending = self.folder / (job['id'] + '.tmp')
        pending.write_text(json.dumps(job, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        pending.replace(self.folder / (job['id'] + '.json'))

    def state(self):
        ordered = sorted(deepcopy(list(self.jobs.values())), key=lambda item: item['createdAt'], reverse=True)
        return dict(connected=bool(self.token), username=self.username, workflow=WORKFLOW,
                    models=[{key: value for key, value in item.items() if key != 'remote'} for item in MODELS],
                    jobs=[job for index, job in enumerate(ordered) if index < 100 or job['status'] in ACTIVE])

    async def request(self, method, path, payload=None, authenticated=True):
        if authenticated and not self.token:
            raise ValueError('请先连接研究账号；服务重启后需重新连接。')
        headers = {'Authorization': 'Bearer ' + self.token} if authenticated else {}
        async with ClientSession(timeout=ClientTimeout(total=45, connect=10, sock_read=40), trust_env=False,
                                 cookie_jar=DummyCookieJar(), auto_decompress=False) as client:
            async with client.request(method, ORIGIN + path, json=payload, headers=headers, allow_redirects=False) as response:
                if response.status == 401:
                    self.token = None
                if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise RemoteRejected(response.status)
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > METADATA_BYTES:
                        raise ValueError('科大接口响应过大；未接受。')
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError('科大接口响应格式无效。')
                return result

    async def connect(self, username):
        if not isinstance(username, str) or not 1 <= len(username.strip()) <= 120 or any(ord(c) < 32 for c in username):
            raise ValueError('请提供1至120字符的研究用户名。')
        if any(job['status'] in ACTIVE and job.get('username') != username.strip() for job in self.jobs.values()):
            raise ValueError('尚有待确认任务，请继续使用原研究用户名。')
        try:
            result = await self.request('POST', '/api/auth/setup', {'username': username.strip()}, authenticated=False)
        except (ClientError, TimeoutError) as exc:
            raise ValueError('科大研究账号连接超时或不可达。') from exc
        token = result.get('token')
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise ValueError('科大接口未提供有效登录会话。')
        self.token, self.username = token, username.strip()
        return self.state()

    async def submit(self, value):
        model, normalized, payload = generation_request(value)
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with self.lock:
            previous = self.jobs.get(normalized['requestId'])
            if previous:
                if previous['requestHash'] != digest:
                    raise ValueError('相同请求编号不能提交不同参数。')
                return deepcopy(previous)
            if not self.token:
                raise ValueError('请先连接研究账号。')
            if any(job['status'] in ACTIVE for job in self.jobs.values()):
                raise ValueError('已有排队、运行或待确认的生成任务，请先检查原任务。')
            job = dict(id=normalized['requestId'], **{k:v for k,v in normalized.items() if k!='requestId'},
                       label=model['label'], requestHash=digest, username=self.username, createdAt=time.time(),
                       status='submitting', audioUrls=[], remoteJobId=None)
            self.jobs[job['id']] = job
            self.save(job)
            try:
                result = await self.request('POST', '/api/generate', payload)
                remote_id = result.get('jobId')
                if not isinstance(remote_id, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', remote_id):
                    raise ValueError('科大接口未返回有效任务编号。')
                job.update(remoteJobId=remote_id, status='queued', error=None)
            except (ClientError, TimeoutError, ValueError, asyncio.CancelledError) as exc:
                job.update(status='failed' if isinstance(exc, RemoteRejected) and 400 <= exc.status < 500 and exc.status != 408 else 'submission_unknown',
                           error=str(exc) if isinstance(exc, RemoteRejected) else '提交未得到可确认回执；保留请求编号，不自动重试。')
                self.save(job)
                if isinstance(exc, asyncio.CancelledError):
                    raise
            self.save(job)
            return deepcopy(job)

    async def poll(self, job_id):
        if job_id not in self.jobs:
            raise ValueError('没有找到该本地生成任务。')
        async with self.poll_locks.setdefault(job_id, asyncio.Lock()):
            return await self._poll(job_id)

    async def _poll(self, job_id):
        job = self.jobs[job_id]
        if job['status'] in {'succeeded', 'failed'} or not job.get('remoteJobId'):
            return deepcopy(job)
        if self.username != job['username']:
            raise ValueError('请重新连接提交该任务的研究用户名。')
        try:
            result = await self.request('GET', '/api/generate/status/' + job['remoteJobId'])
        except (ClientError, TimeoutError) as exc:
            raise ValueError('任务查询暂时中断；保留原任务，请稍后刷新。') from exc
        state = result.get('status')
        if state not in {'queued', 'pending', 'running', 'processing', 'succeeded', 'failed'}:
            raise ValueError('科大返回未知任务状态；未将其判为完成。')
        update = dict(status={'pending':'queued', 'processing':'running'}.get(state, state), checkedAt=time.time())
        if state == 'succeeded':
            urls = (result.get('result') or {}).get('audioUrls')
            if not isinstance(urls, list) or not 1 <= len(urls) <= 16:
                raise ValueError('成功回执缺少音频结果；未标记完成。')
            update['audioUrls'] = list(dict.fromkeys(validate_audio_url(url) for url in urls))
            update['error'] = None
        elif state == 'failed':
            error = result.get('error')
            update['error'] = error[:400] if isinstance(error, str) else '科大模型生成失败，需检查服务日志。'
        job.update(update)
        self.save(job)
        return deepcopy(job)

    def source(self, job_id, url):
        job = self.jobs.get(job_id)
        if not job or job['status'] != 'succeeded' or validate_audio_url(url) not in job['audioUrls']:
            raise ValueError('生成任务与所选音频不匹配。')
        return {key: job[key] for key in ('id', 'remoteJobId', 'model', 'label', 'prompt', 'seed', 'requestHash')}

    def imported(self, job_id, project_id):
        job = self.jobs[job_id]
        job['projectId'] = project_id
        self.save(job)
