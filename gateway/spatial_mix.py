"""Bounded local mixing: measured evidence, reversible DSP, no model/network calls."""
import hashlib
import json
import math
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import welch

if __package__:
    from .spatial_audio import canonical_hash, project_stem_ids, NATIVE_SCHEMA
    from .spatial_diagnostics import analyze_relationships, stereo_windows
else:
    from spatial_audio import canonical_hash, project_stem_ids, NATIVE_SCHEMA
    from spatial_diagnostics import analyze_relationships, stereo_windows

IDS = ('drums', 'bass', 'other', 'vocals')
RATE = 44100
LOUDNESS_FILTER = 'loudnorm=I=-20:TP=-1:LRA=11:print_format=json'


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_audio(path):
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != '.wav':
        raise ValueError('需要存在的本地WAV文件。')
    info = sf.info(path)
    if info.format not in ('WAV', 'WAVEX') or info.channels != 2 or info.samplerate != RATE \
            or info.frames <= 0 or info.frames > RATE*600:
        raise ValueError('混音仅接受44.1kHz双声道、非空且不超过600秒的WAV；FOA不能按立体声计量。')
    data, _ = sf.read(path, dtype='float32', always_2d=True)
    if not np.isfinite(data).all():
        raise ValueError('音频含非finite采样。')
    return data


def run_ffmpeg(ffmpeg, args):
    if not Path(ffmpeg).is_file():
        raise ValueError('本地FFmpeg入口不存在。')
    result = subprocess.run([str(ffmpeg), '-nostdin', '-hide_banner', '-threads', '1',
                             '-filter_threads', '1', *args], capture_output=True,
                            timeout=180, check=False)
    if result.returncode:
        raise ValueError('FFmpeg处理失败：'+result.stderr.decode(errors='replace')[-2000:])
    return result


def finite_or_none(value):
    value = float(value)
    return value if math.isfinite(value) else None


def db(value):
    return 20*math.log10(value) if value > 0 else None


def parse_loudness(text):
    start, end = text.rfind('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('FFmpeg未返回响度计量，不能假定测量成功。')
    try:
        meter = json.loads(text[start:end+1])
        lufs, tp, lra = [finite_or_none(meter[k]) for k in ('input_i', 'input_tp', 'input_lra')]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('FFmpeg响度计量格式无效。') from exc
    return {'integratedLufs': lufs, 'truePeakDbTP': tp, 'loudnessRangeLu': lra}


def require_true_peak(measurement):
    if measurement['truePeakDbTP'] is None and measurement.get('rms') != 0:
        raise ValueError('非静音音频缺少有效真峰测量，拒绝生成或发布混音。')


def measure_audio(path, ffmpeg):
    data = read_audio(path)
    result = run_ffmpeg(ffmpeg, ['-i', str(Path(path).absolute()), '-map', '0:a:0', '-vn',
                                '-af', LOUDNESS_FILTER, '-f', 'null', '-'])
    meter = parse_loudness(result.stderr.decode(errors='replace'))
    rms = float(np.sqrt(np.mean(data.astype(np.float64)**2)))
    require_true_peak({**meter, 'rms': rms})
    peak = float(np.max(np.abs(data)))
    mono_rms = float(np.sqrt(np.mean(data.mean(axis=1, dtype=np.float64)**2)))
    std = np.std(data.astype(np.float64), axis=0)
    correlation = float(np.corrcoef(data.T)[0, 1]) if np.all(std > 1e-10) else None
    block = RATE//10
    block_rms = [float(np.sqrt(np.mean(data[i:i+block].astype(np.float64)**2)))
                 for i in range(0, len(data), block)]
    activity = sum(v > .001 for v in block_rms)/len(block_rms)
    frequencies, power = welch(data, fs=RATE, nperseg=min(16384, len(data)), axis=0)
    power = power.sum(axis=1, dtype=np.float64)
    total = float(power.sum())
    bands = {'sub35': (0, 35), 'low': (0, 200), 'mid': (200, 4000), 'high': (4000, RATE/2+1)}
    ratios = {key: float(power[(frequencies >= lo) & (frequencies < hi)].sum()/total)
              if total > 1e-20 else None for key, (lo, hi) in bands.items()}
    version = run_ffmpeg(ffmpeg, ['-version']).stdout.decode(errors='replace').splitlines()
    if not version:
        raise ValueError('FFmpeg版本溯源缺失。')
    return {'frames': len(data), 'sampleRate': RATE, 'channels': 2,
            'measurement': {'ffmpegVersion': version[0],
                'method': 'FFmpeg loudnorm input metering; normalization output discarded',
                'fields': ['input_i', 'input_tp', 'input_lra'],
                'filter': LOUDNESS_FILTER},
            'loudnessRangeInterpretation': 'LRA仅描述本片段；短时片段可能为0或不稳定，不能据此推断整曲动态或音乐品质。',
            'loudnessRangeSuitable': len(data) >= RATE*60,
            'loudnessRangePolicy': 'EBU R128-2023 p4 footnote 2: LRA not recommended for programmes shorter than 1 minute',
            'durationSeconds': len(data)/RATE, **meter,
            'rms': rms, 'rmsDbFS': db(rms), 'samplePeakDbFS': db(peak),
            'crestDb': db(peak/rms) if rms > 0 else None, 'correlation': correlation,
            'monoCancellationDb': db(mono_rms/rms) if rms > 0 else None,
            'monoFullyCancelled': bool(rms > 0 and mono_rms == 0),
            'active': rms > .001, 'activityRatio': activity,
            'activityWindowSeconds': .1, 'activityThresholdDbFS': -60,
            'activityMeaning': '分段信号活动比例，不代表乐器识别或分离置信度',
            'energyRatios': ratios, 'energyBandHz': {k: list(v) for k, v in bands.items()},
            'energyMethod': 'Welch Hann PSD, 16384-sample maximum window, stereo channel power sum',
            'stereoTime': stereo_windows(data, RATE)}


def analyze_project(original, stems_dir, ffmpeg, reference=None):
    original = Path(original)
    source = read_audio(original)
    metadata_path = Path(stems_dir)/'separation.json'
    metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.is_file() else {}
    if not isinstance(metadata, dict):
        raise ValueError('声部元数据须为JSON对象。')
    native = metadata.get('schema') == NATIVE_SCHEMA
    ids = project_stem_ids({'nativeStems': metadata}) if native else IDS
    stems = []
    residual = source.astype(np.float64) if native else source.copy()
    for sid in ids:
        path = Path(stems_dir)/(sid+'.wav')
        data = read_audio(path)
        if data.shape != source.shape:
            raise ValueError('声部必须与原版采样率、声道及帧数完全一致。')
        residual -= data
        stems.append({'id': sid, **measure_audio(path, ffmpeg), 'sha256': file_hash(path)})
    if native:
        validate_native_residual(residual, source)
    result = {'schema': 'spatial-analysis-v1', 'original': {
                  **measure_audio(original, ffmpeg), 'sha256': file_hash(original)}, 'stems': stems,
              'reconstructionResidual': {'rms': float(np.sqrt(np.mean(residual.astype(np.float64)**2))),
                  'samplePeak': float(np.max(np.abs(residual))),
                  'meaning': '原版减去四个估计声部的和；不是音质评分，处理时保留此残差'},
              'reference': None}
    if native:
        result['nativeStems'] = metadata
        result['reconstructionResidual']['meaning'] = '原生声部之和与未母带原版的浮点误差；不路由至任何声部'
    result['relationships'] = analyze_relationships(
        ((sid, read_audio(Path(stems_dir)/(sid+'.wav'))) for sid in ids), RATE)
    if reference is not None:
        result['reference'] = {**measure_audio(reference, ffmpeg), 'sha256': file_hash(reference)}
    return result


def analysis_stem_ids(analysis):
    expected = project_stem_ids(analysis)
    observed = [stem['id'] for stem in analysis['stems']]
    if len(observed) != len(expected) or set(observed) != set(expected):
        raise ValueError('分析声部与项目元数据不一致。')
    return expected


def validate_native_residual(residual, source):
    # FLOAT summation tolerates rounding only, never an omitted fullmix component.
    tolerance = 2e-6 * max(1.0, float(np.max(np.abs(source))))
    if float(np.max(np.abs(residual))) > tolerance:
        raise ValueError('原生声部求和与原版不一致，拒绝注入重建残差。')


def build_plan(analysis, intent):
    if intent not in ('preserve', 'clarity', 'reference'):
        raise ValueError('意图仅限preserve、clarity、reference。')
    ids = analysis_stem_ids(analysis)
    original = analysis['original']
    require_true_peak(original)
    tp = original['truePeakDbTP']
    plan = {'schema': 'spatial-mix-plan-v1', 'analysisHash': canonical_hash(analysis),
            'intent': intent, 'masterGainDb': max(-24, min(0, -1.1-tp)) if tp is not None else 0,
            'masterEq': {'lowShelfDb': 0, 'highShelfDb': 0},
            'stems': [{'id': sid, 'gainDb': 0, 'highpassHz': 0,
                       'eq': {'frequencyHz': 1000, 'gainDb': 0, 'q': 1}, 'compressor': None}
                      for sid in ids],
            'reasons': [{'target': 'masterGainDb', 'observationId': 'original.truePeakDbTP',
                         'explanation': '仅为真峰预留余量；声部不拉齐响度，可恢复0dB对照。'}],
            'warnings': ['声部分离为估计；保持原始残差，必须进行等响度试听，指标不能代替音乐判断。']}
    if analysis.get('nativeStems') is not None:
        plan['warnings'][0] = '原生生成声部；须进行等响度试听，指标不能代替音乐判断。'
    if intent == 'clarity':
        observations = {item['id']: item for item in analysis['stems']}
        for stem in plan['stems']:
            observation = observations[stem['id']]
            if observation['active'] \
                    and (observation['energyRatios']['sub35'] or 0) > .25:
                stem['highpassHz'] = 30
                plan['reasons'].append({'target': stem['id']+'.highpassHz',
                    'observationId': 'stems.'+stem['id']+'.energyRatios.sub35',
                    'explanation': '低于35Hz能量占比超过25%，提出30Hz高通候选；可能含有用低频，试听不佳恢复0Hz。'})
    if intent == 'reference':
        reference = analysis.get('reference')
        if not reference or not reference['active']:
            raise ValueError('reference意图需要有效本地参考音频观测。')
        for band, field in (('low', 'lowShelfDb'), ('high', 'highShelfDb')):
            a, b = original['energyRatios'][band], reference['energyRatios'][band]
            if a and b:
                plan['masterEq'][field] = round(max(-3, min(3, 10*math.log10(b/a)*.5)), 2)
                plan['reasons'].append({'target': 'masterEq.'+field,
                    'observationId': 'reference.energyRatios.'+band,
                    'explanation': '参考与原版频带能量比例差的有界候选，不代表音色优劣；恢复0dB可回退。'})
    if not original.get('loudnessRangeSuitable', False):
        plan['warnings'].append('素材不足一分钟；LRA不用于决定压缩强度或判断动态合格。')
    if original.get('stereoTime', {}).get('riskDurationSeconds', 0) > 0:
        plan['warnings'].append('存在局部单声道抵消风险；先用兼容检查定位，不通过自动宽化或补偿增益掩盖。')
    for pair in analysis.get('relationships', {}).get('overlapPairs', []):
        plan['warnings'].append(' / '.join(pair['ids'])+'在'+', '.join(pair['bands'])+
                                '同时有接近电平，仅提示相互试听，不自动判断感知掩蔽或削减频段。')
    return validate_plan(plan, analysis)


def keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(label+'字段无效或含多余字段。')


def number(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (float, int)) \
            or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError('混音参数类型或范围无效。')
    return value


def validate_plan(plan, analysis):
    ids = analysis_stem_ids(analysis)
    keys(plan, ('schema', 'analysisHash', 'intent', 'masterGainDb', 'masterEq', 'stems', 'reasons', 'warnings'), '计划')
    if plan['schema'] != 'spatial-mix-plan-v1' or plan['analysisHash'] != canonical_hash(analysis) \
            or plan['intent'] not in ('preserve', 'clarity', 'reference'):
        raise ValueError('计划schema、意图或analysisHash无效。')
    if plan['intent'] == 'reference' and not analysis.get('reference'):
        raise ValueError('reference计划缺少绑定的参考观测。')
    number(plan['masterGainDb'], -24, 0)
    keys(plan['masterEq'], ('lowShelfDb', 'highShelfDb'), '主均衡')
    for val in plan['masterEq'].values():
        number(val, -3, 3)
    if not isinstance(plan['stems'], list) or len(plan['stems']) != len(ids):
        raise ValueError('计划必须包含项目的全部声部。')
    seen = set()
    for stem in plan['stems']:
        keys(stem, ('id', 'gainDb', 'highpassHz', 'eq', 'compressor'), '声部')
        if not isinstance(stem['id'], str) or stem['id'] not in ids or stem['id'] in seen:
            raise ValueError('声部ID无效或重复。')
        seen.add(stem['id'])
        number(stem['gainDb'], -6, 3)
        number(stem['highpassHz'], 0, 60)
        keys(stem['eq'], ('frequencyHz', 'gainDb', 'q'), '均衡')
        for field, lo, hi in (('frequencyHz', 100, 10000), ('gainDb', -3, 3), ('q', .3, 3)):
            number(stem['eq'][field], lo, hi)
        if stem['compressor'] is not None:
            keys(stem['compressor'], ('thresholdDb', 'ratio', 'attackMs', 'releaseMs'), '压缩器')
            for field, lo, hi in (('thresholdDb', -36, -6), ('ratio', 1, 3),
                                  ('attackMs', 5, 100), ('releaseMs', 50, 1000)):
                number(stem['compressor'][field], lo, hi)
    if not isinstance(plan['reasons'], list) or len(plan['reasons']) > 40:
        raise ValueError('证据条目无效。')
    for reason in plan['reasons']:
        keys(reason, ('target', 'observationId', 'explanation'), '证据')
        if any(not isinstance(v, str) or not 1 <= len(v) <= 1000 for v in reason.values()):
            raise ValueError('证据文本无效。')
    if not isinstance(plan['warnings'], list) or len(plan['warnings']) > 120 \
            or any(not isinstance(v, str) or len(v) > 1000 for v in plan['warnings']):
        raise ValueError('警示文本无效。')
    return json.loads(json.dumps(plan, allow_nan=False))


def filter_audio(source, target, filters, ffmpeg):
    if not filters:
        data = read_audio(source)
        sf.write(target, data, RATE, subtype='FLOAT')
        return data
    run_ffmpeg(ffmpeg, ['-y', '-i', str(Path(source).absolute()), '-map', '0:a:0', '-vn',
                        '-af', ','.join(filters), '-ar', str(RATE), '-c:a', 'pcm_f32le', str(target)])
    data = read_audio(target)
    if sf.info(source).frames != len(data):
        raise ValueError('DSP输出帧数变化，拒绝未对齐结果。')
    return data


def process_mix(original, stems_dir, plan, output_dir, ffmpeg, analysis=None):
    actual = analyze_project(original, stems_dir, ffmpeg)
    if analysis is None:
        analysis = actual
    else:
        bound = {**actual, 'reference': analysis.get('reference')}
        if canonical_hash(bound) != canonical_hash(analysis):
            raise ValueError('原版或声部在分析后发生变化。')
    return _process_mix(original, stems_dir, plan, output_dir, ffmpeg, analysis)


def _process_mix(original, stems_dir, plan, output_dir, ffmpeg, analysis):
    plan = validate_plan(plan, analysis)
    ids = analysis_stem_ids(analysis)
    native = analysis.get('nativeStems') is not None
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError('输出目录必须为空，禁止覆盖已有音频。')
    processed = output_dir/'processed_stems'
    processed.mkdir()
    source = read_audio(original)
    residual = source.astype(np.float64)
    mixed = np.zeros_like(residual)
    for stem in plan['stems']:
        sid = stem['id']
        path = Path(stems_dir)/(sid+'.wav')
        data = read_audio(path)
        residual -= data
        filters = []
        if stem['highpassHz']:
            filters.append(f"highpass=f={stem['highpassHz']}:p=2")
        eq = stem['eq']
        if eq['gainDb']:
            filters.append(f"equalizer=f={eq['frequencyHz']}:t=q:w={eq['q']}:g={eq['gainDb']}")
        compressor = stem['compressor']
        if compressor:
            filters.append(f"acompressor=threshold={10**(compressor['thresholdDb']/20)}:ratio={compressor['ratio']}:attack={compressor['attackMs']}:release={compressor['releaseMs']}:makeup=1:link=maximum:detection=peak")
        if stem['gainDb']:
            filters.append(f"volume={stem['gainDb']}dB")
        mixed += filter_audio(path, processed/(sid+'.wav'), filters, ffmpeg)
    if native:
        validate_native_residual(residual, source)
    else:
        mixed += residual
        other_path = processed/'other.wav'
        sf.write(other_path, read_audio(other_path).astype(np.float64)+residual, RATE, subtype='FLOAT')
    sf.write(output_dir/'residual.wav', residual, RATE, subtype='FLOAT')
    sf.write(output_dir/'baseline.wav', source, RATE, subtype='FLOAT')
    candidate_path = output_dir/'candidate.wav'
    sf.write(candidate_path, mixed, RATE, subtype='FLOAT')
    master = []
    for field, name, frequency in (('lowShelfDb', 'bass', 120), ('highShelfDb', 'treble', 6000)):
        if plan['masterEq'][field]:
            master.append(f"{name}=g={plan['masterEq'][field]}:f={frequency}:t=q:w=0.707")
    if plan['masterGainDb']:
        master.append(f"volume={plan['masterGainDb']}dB")
    # Separate file avoids FFmpeg reading and overwriting the same path.
    if master:
        master_path = output_dir/'master_stage.wav'
        mixed = filter_audio(candidate_path, master_path, master, ffmpeg)
        sf.write(candidate_path, mixed, RATE, subtype='FLOAT')
        master_path.unlink()
    pre = measure_audio(candidate_path, ffmpeg)
    require_true_peak(pre)
    extra = min(0, -1.1-pre['truePeakDbTP']) if pre['truePeakDbTP'] is not None else 0
    if extra:
        mixed = read_audio(candidate_path)*10**(extra/20)
        sf.write(candidate_path, mixed, RATE, subtype='FLOAT')
    render_filters = master + ([f'volume={extra}dB'] if extra else [])
    for sid in ids:
        path = processed/(sid+'.wav')
        if render_filters:
            stage_path = output_dir/'render_stage.wav'
            data = filter_audio(path, stage_path, render_filters, ffmpeg)
            sf.write(path, data, RATE, subtype='FLOAT')
            stage_path.unlink()
    rendered_sum = np.zeros_like(residual)
    for sid in ids:
        rendered_sum += read_audio(processed/(sid+'.wav'))
    sf.write(candidate_path, rendered_sum, RATE, subtype='FLOAT')
    baseline = measure_audio(output_dir/'baseline.wav', ffmpeg)
    candidate = measure_audio(candidate_path, ffmpeg)
    require_true_peak(candidate)
    if candidate['truePeakDbTP'] is not None and candidate['truePeakDbTP'] > -1:
        raise ValueError('真峰余量验证失败。')
    valid = baseline['integratedLufs'] is not None and candidate['integratedLufs'] is not None
    target = min(baseline['integratedLufs'], candidate['integratedLufs'], -20) if valid else None
    metrics = {'baseline': baseline, 'candidate': candidate}
    match = {'status': 'matched' if valid else 'unavailable', 'targetLufs': target,
             'reason': '仅衰减至共同响度目标' if valid else '无有效综合响度，无法声称等响度', 'gainDb': {}}
    for name, measurement in (('baseline', baseline), ('candidate', candidate)):
        gain = min(0, target-measurement['integratedLufs']) if valid else 0
        match['gainDb'][name] = gain
        data = read_audio(output_dir/(name+'.wav'))*10**(gain/20)
        path = output_dir/(name+'_matched.wav')
        sf.write(path, data, RATE, subtype='FLOAT')
        metrics[name+'Matched'] = measure_audio(path, ffmpeg)
        mono = np.repeat(data.mean(axis=1, keepdims=True), 2, axis=1)
        sf.write(output_dir/(name+'_mono.wav'), mono, RATE, subtype='FLOAT')
    if valid and abs(metrics['baselineMatched']['integratedLufs']-metrics['candidateMatched']['integratedLufs']) > .2:
        match.update(status='unavailable', reason='实测响度差超过0.2LU，不能声称匹配成功')
    hashes = {'original': file_hash(original), 'baseline': file_hash(output_dir/'baseline.wav'),
              'candidate': file_hash(candidate_path)}
    return {'schema': 'spatial-mix-result-v1', 'status': 'awaiting-listening', 'plan': plan,
            'metrics': metrics, 'preHeadroomMetrics': pre, 'headroomGainDb': extra,
            'totalMasterGainDb': plan['masterGainDb']+extra, 'hashes': hashes,
            'loudnessMatch': match, 'residualRouting': None if native else 'other', 'masterAppliedToStems': True,
            'otherMeaning': None if native else '其他估计声部与原始重建残差的复合组，不代表单独真实乐器',
            'outputFiles': sorted(str(p.relative_to(output_dir)) for p in output_dir.rglob('*.wav')),
            'hearingRequired': ['分离串音与残留伪影', '低频与音色是否损失', '动态与瞬态是否自然', '等响度比较候选与原版']}
