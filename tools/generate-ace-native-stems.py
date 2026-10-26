#!/usr/bin/env python3
"""ACE Base lego producer. Receipts record API claims, not model attestation.

Requires aiohttp, numpy and soundfile. Never initializes models or reads credentials.
Each plan contains 2..12 tracks: {"tracks":[{"id":"bass","prompt":"...","seed":1}, ...]}.
Run again with identical arguments to resume known task IDs. An uncertain submission
requires operator reconciliation; it is deliberately never submitted again.
"""
import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import urljoin, urlsplit
import zipfile

import aiohttp
import numpy as np
import soundfile as sf

MODEL = 'acestep-v15-base'
SCHOOL = 'http://10.100.16.56:8189'
TRACKS = {'vocals', 'backing_vocals', 'drums', 'bass', 'guitar', 'keyboard',
          'percussion', 'strings', 'synth', 'fx', 'brass', 'woodwinds'}
MAX_AUDIO = 200 * 1024 * 1024
TRACK_SECONDS = 45 * 60


class Blocked(ValueError):
    pass


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    if os.name != 'nt':
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


@contextmanager
def output_lock(path):
    with path.open('a+b') as lock:
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b'0')
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Blocked('another producer holds the output lock')
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def endpoint_url(value):
    url = urlsplit(value)
    try:
        local = url.hostname == 'localhost' or ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        local = False
    if (url.scheme not in ('http', 'https') or url.username or url.password or
            url.path not in ('', '/') or url.query or url.fragment or
            not (local or value.rstrip('/') == SCHOOL)):
        raise Blocked('endpoint must be loopback or the existing school origin')
    return value.rstrip('/')


def audio_info(path, timeline=None, wav=False):
    with sf.SoundFile(path) as stream:
        info = dict(sampleRate=stream.samplerate, frames=stream.frames, startFrame=0)
        if stream.format not in (('WAV', 'WAVEX') if wav else ('WAV', 'WAVEX', 'FLAC')):
            raise Blocked('audio must be lossless WAV/FLAC (generated stems must be WAV)')
        if stream.subtype not in ('PCM_16', 'PCM_24', 'PCM_32', 'FLOAT', 'DOUBLE'):
            raise Blocked('audio must use a supported lossless PCM/float subtype')
        if (stream.channels != 2 or stream.samplerate not in (44100, 48000) or
                not stream.samplerate // 10 <= stream.frames <= stream.samplerate * 600):
            raise Blocked('audio must be stereo, 44.1/48 kHz, and 0.1..600 seconds')
        peak = 0.
        for block in stream.blocks(blocksize=65536, dtype='float64', always_2d=True):
            if not np.isfinite(block).all():
                raise Blocked('audio contains nonfinite samples')
            peak = max(peak, float(np.max(np.abs(block))))
        if peak <= 1e-7:
            raise Blocked('audio is silent')
        if timeline is not None and info != timeline:
            raise Blocked('stem timeline mismatch; no padding, cropping or resampling is allowed')
        return info, dict(container=stream.format, subtype=stream.subtype, channels=stream.channels)


def plan_tracks(path):
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    tracks = plan.get('tracks') if isinstance(plan, dict) else None
    if not isinstance(tracks, list) or not 2 <= len(tracks) <= 12:
        raise Blocked('plan requires 2..12 tracks')
    seen = set()
    for track in tracks:
        if not isinstance(track, dict) or set(track) != {'id', 'prompt', 'seed'}:
            raise Blocked('each track requires only id, prompt and seed')
        if track['id'] not in TRACKS or track['id'] in seen:
            raise Blocked('track IDs must be unique official instrument IDs')
        seen.add(track['id'])
        if (not isinstance(track['prompt'], str) or not track['prompt'].strip() or
                len(track['prompt']) > 4000 or '\x00' in track['prompt']):
            raise Blocked('each track needs a concrete prompt of 1..4000 characters')
        if type(track['seed']) is not int or not 0 <= track['seed'] <= 2147483647:
            raise Blocked('seed must be an integer in 0..2147483647')
    return tracks


def successful_audio(result):
    if not isinstance(result, dict) or type(result.get('status')) is not int or result['status'] != 1:
        raise Blocked('expected a successful task result')
    audio = json.loads(result['result'])
    if (not isinstance(audio, list) or len(audio) != 1 or not isinstance(audio[0], dict) or
            type(audio[0].get('status')) is not int or audio[0]['status'] != 1):
        raise Blocked('expected exactly one successful result audio')
    if audio[0].get('dit_model') != MODEL:
        raise Blocked('result reports a missing or different DiT model')
    return audio[0]


async def api(session, method, endpoint, route, **kwargs):
    async with session.request(method, endpoint + route, allow_redirects=False, **kwargs) as response:
        if response.status == 401:
            raise Blocked('HTTP 401: authentication required; no automatic login or credential lookup')
        if response.status != 200:
            raise Blocked(f'API HTTP {response.status}')
        raw = await response.content.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise Blocked('API response exceeds metadata limit')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise Blocked('API returned non-JSON; this endpoint does not expose the required official API') from exc
        if not isinstance(value, dict) or 'data' not in value:
            raise Blocked('API response is missing the official data envelope')
        if value.get('code', 200) != 200 or value.get('error'):
            raise Blocked('API returned an error (remote details omitted)')
        return value['data']


async def _generate(context, plan, endpoint, output, poll_seconds=5):
    endpoint = endpoint_url(endpoint)
    context = Path(context).resolve()
    tracks = plan_tracks(plan)
    timeline, context_format = audio_info(context)
    context_hash = sha256(context)
    output = Path(output)
    # A process lock prevents simultaneous resumes from both submitting a track.
    output.mkdir(parents=True, exist_ok=True)
    with output_lock(output / '.producer.lock'):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120),
                                        cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as session:
            models = await api(session, 'GET', endpoint, '/v1/models')
            if not any(item.get('name') == MODEL and item.get('is_loaded', True) is True
                       for item in models.get('models', [])):
                raise Blocked('Base model unavailable or not loaded; zero tasks submitted')
            identity = dict(endpoint=endpoint, contextSha256=context_hash, tracks=tracks)
            ledger_path = output / 'ledger.json'
            if ledger_path.exists():
                ledger = json.loads(ledger_path.read_text())
                if ledger.get('identity') != identity:
                    raise Blocked('resume arguments differ from the saved ledger')
            else:
                if set(p.name for p in output.iterdir()) - {'.producer.lock', 'blocked.json'}:
                    raise Blocked('output must be new or contain this producer ledger')
                ledger = dict(identity=identity, tracks={})
                save(ledger_path, ledger)
            extension = '.flac' if context_format['container'] == 'FLAC' else '.wav'
            context_name = 'context' + extension
            copied = output / context_name
            if copied.exists():
                if sha256(copied) != context_hash:
                    raise Blocked('saved context hash mismatch')
            else:
                shutil.copyfile(context, copied)
                if sha256(copied) != context_hash:
                    raise Blocked('context changed during copy; no task submitted')
            (output / 'stems').mkdir(exist_ok=True)
            (output / 'receipts').mkdir(exist_ok=True)
            manifest_tracks = []
            for track in tracks:
                sid = track['id']
                request = dict(task_type='lego', model=MODEL, prompt=track['prompt'], seed=track['seed'],
                               contextSha256=context_hash, instruction=f'Generate the {sid} track based on the audio context:',
                               thinking=False, batch_size=1, audio_format='wav', use_random_seed=False,
                               inference_steps=50, use_cot_caption=False, use_cot_language=False)
                state = ledger['tracks'].get(sid)
                receipt_path = output / 'receipts' / (sid + '.json')
                if state and state.get('requestHash') != canonical_hash(request):
                    raise Blocked('saved request hash mismatch')
                if state and not state.get('task_id'):
                    raise Blocked(f'{sid}: submission unknown; reconcile remotely, never automatically resubmit')
                if state is None:
                    state = dict(requestHash=canonical_hash(request), phase='submission_unknown', started=time.time())
                    ledger['tracks'][sid] = state
                    save(ledger_path, ledger)  # Durable intent precedes the network request.
                    save(receipt_path, dict(request=request, submit={}, result=None))
                    with copied.open('rb') as source:
                        form = aiohttp.FormData()
                        for key, value in request.items():
                            if key != 'contextSha256':
                                form.add_field(key, json.dumps(value) if not isinstance(value, str) else value)
                        form.add_field('src_audio', source, filename=context_name,
                                       content_type='audio/flac' if extension == '.flac' else 'audio/wav')
                        submitted = await api(session, 'POST', endpoint, '/release_task', data=form)
                    task_id = submitted.get('task_id')
                    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task_id):
                        raise Blocked('submission returned no valid task ID; submission remains unknown')
                    state.update(task_id=task_id, phase='submitted')
                    save(ledger_path, ledger)
                    save(receipt_path, dict(request=request, submit=submitted, result=None))
                receipt = json.loads(receipt_path.read_text())
                # A crash between ledger and receipt writes can recover the known task ID.
                receipt['submit']['task_id'] = state['task_id']
                remaining = TRACK_SECONDS - (time.time() - state['started'])
                if state.get('phase') != 'complete':
                    if remaining <= 0:
                        raise Blocked(f'{sid}: 45 minute deadline reached; no automatic resubmission')
                    async with asyncio.timeout(remaining):
                        while True:
                            results = await api(session, 'POST', endpoint, '/query_result',
                                                json={'task_id_list': [state['task_id']]})
                            result = next((r for r in results if r.get('task_id') == state['task_id']), None)
                            if not result or type(result.get('status')) is not int or result['status'] not in (0, 1, 2):
                                raise Blocked('invalid query_result task/status')
                            receipt['result'] = result
                            save(receipt_path, receipt)
                            if result['status'] == 2:
                                raise Blocked(f'{sid}: remote task failed; no automatic resubmission')
                            if result['status'] == 1:
                                break
                            await asyncio.sleep(poll_seconds)
                        audio = successful_audio(result)
                        url = urljoin(endpoint + '/', audio['file'])
                        parsed = urlsplit(url)
                        if (parsed.scheme, parsed.netloc) != (urlsplit(endpoint).scheme, urlsplit(endpoint).netloc) or parsed.path != '/v1/audio' or parsed.fragment or parsed.username:
                            raise Blocked('audio URL must use the same origin /v1/audio endpoint')
                        partial = output / 'stems' / (sid + '.partial')
                        async with session.get(url, allow_redirects=False) as response:
                            if response.status != 200:
                                raise Blocked(f'audio download HTTP {response.status}')
                            if response.content_length and response.content_length > MAX_AUDIO:
                                raise Blocked('audio exceeds 200 MiB limit')
                            total = 0
                            with partial.open('wb') as stream:
                                async for block in response.content.iter_chunked(65536):
                                    total += len(block)
                                    if total > MAX_AUDIO:
                                        raise Blocked('audio exceeds 200 MiB limit')
                                    stream.write(block)
                                stream.flush()
                                os.fsync(stream.fileno())
                        audio_info(partial, timeline, wav=True)
                        os.replace(partial, output / 'stems' / (sid + '.wav'))
                        receipt['audioSha256'] = sha256(output / 'stems' / (sid + '.wav'))
                        save(receipt_path, receipt)
                        state['phase'] = 'complete'
                        save(ledger_path, ledger)
                successful_audio(receipt.get('result'))
                stem = output / 'stems' / (sid + '.wav')
                _, output_format = audio_info(stem, timeline, wav=True)
                audio_hash = sha256(stem)
                if receipt.get('audioSha256') != audio_hash:
                    raise Blocked('saved stem hash mismatch')
                manifest_tracks.append(dict(id=sid, file=f'stems/{sid}.wav', sha256=audio_hash,
                                            outputFormat=output_format,
                                            task=dict(id=state['task_id'], taskType='lego', model=MODEL,
                                                      seed=track['seed'], prompt=track['prompt'],
                                                      contextSha256=context_hash, requestHash=canonical_hash(request),
                                                      responseFile=f'receipts/{sid}.json')))
            if (len({t['sha256'] for t in manifest_tracks}) != len(tracks) or
                    len({t['task']['id'] for t in manifest_tracks}) != len(tracks)):
                raise Blocked('independent stems must have distinct audio and task IDs')
            context_samples, _ = sf.read(copied, dtype='float32', always_2d=True)
            context_pcm_hash = hashlib.sha256(context_samples.tobytes()).hexdigest()
            pcm_hashes = set()
            for track in manifest_tracks:
                samples, _ = sf.read(output / track['file'], dtype='float32', always_2d=True)
                pcm_hash = hashlib.sha256(samples.tobytes()).hexdigest()
                if pcm_hash == context_pcm_hash:
                    raise Blocked('stem duplicates the context decoded samples; guide is not a generated stem')
                pcm_hashes.add(pcm_hash)
            if len(pcm_hashes) != len(tracks):
                raise Blocked('independent stems must have different decoded samples')
            manifest = dict(schema='ace-native-stems-v1', producer=dict(model=MODEL, api=endpoint),
                            context=dict(file=context_name, sha256=context_hash, outputFormat=context_format),
                            timeline=timeline, tracks=manifest_tracks,
                            quality=dict(status='pending-listening-review',
                                         note='Receipts are API records, not cryptographic remote model attestation; musical alignment and stem isolation require listening.'))
            save(output / 'manifest.json', manifest)
            with zipfile.ZipFile(output / 'bundle.zip.tmp', 'w', compression=zipfile.ZIP_STORED) as bundle:
                for name in ['manifest.json', context_name] + [name for t in manifest_tracks for name in (t['file'], t['task']['responseFile'])]:
                    bundle.write(output / name, name)
            os.replace(output / 'bundle.zip.tmp', output / 'bundle.zip')
            return output / 'bundle.zip'


async def generate(context, plan, endpoint, output, poll_seconds=5):
    try:
        return await _generate(context, plan, endpoint, output, poll_seconds)
    except (ValueError, OSError, KeyError, TypeError, aiohttp.ClientError, TimeoutError) as exc:
        # Persist a bounded local reason without remote bodies or transport details.
        reason = str(exc) if isinstance(exc, Blocked) else type(exc).__name__
        folder = Path(output)
        if folder.is_dir() and (folder / '.producer.lock').exists():
            save(folder / 'blocked.json', dict(status='blocked', reason=reason, time=time.time()))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8001')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        print(asyncio.run(generate(args.context, args.plan, args.endpoint, args.output)))
    except (ValueError, OSError, KeyError, TypeError, aiohttp.ClientError, TimeoutError) as exc:
        # Do not print remote bodies, URLs, or transport errors which might contain secrets.
        message = str(exc) if isinstance(exc, Blocked) else type(exc).__name__
        print('BLOCKED: ' + message, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
