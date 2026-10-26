"""Conservative, observation-based static scene proposals (stdlib only)."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math


def _number(value, low=0, high=math.inf):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and low <= value <= high)


def build_content_scene(scene, analysis, residual_routing=None, layout='content'):
    """Explicitly propose a static layout; never infer instrument identities or gain.

    Confidently classified active objects and explicit significant residual carriers
    lose motion. Other unknown/quiet objects retain layout. Thresholds are heuristics.
    """
    if layout not in ('content', 'spacious'):
        raise ValueError('Unknown content layout.')
    spacious = layout == 'spacious'
    candidate = deepcopy(scene)
    objects = candidate['objects']
    ids = [obj['id'] for obj in objects]
    if len(set(ids)) != len(ids):
        raise ValueError('Scene object IDs must be unique.')
    for obj in objects:
        for field, low, high in (('azimuthDeg', -180, 180),
                                 ('elevationDeg', -90, 90), ('widthDeg', 0, 120),
                                 ('gainDb', -24, 6)):
            if not _number(obj.get(field), low, high):
                raise ValueError('Invalid scene coordinate: ' + field)
    warnings = []
    try:
        encoded = json.dumps(analysis, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()
        analysis_hash = hashlib.sha256(encoded).hexdigest()
    except (TypeError, ValueError):
        analysis_hash = None
        warnings.append('Analysis is not canonical finite JSON; this proposal cannot be bound for apply.')
    stems = analysis.get('stems', [])
    counts = Counter(s.get('id') for s in stems if isinstance(s, dict))
    by_id = {s.get('id'): s for s in stems if isinstance(s, dict)
             and counts[s.get('id')] == 1}
    observations = {}
    for obj in objects:
        sid = obj['id']
        obs = by_id.get(sid, {})
        low = obs.get('energyRatios', {}).get('low') if isinstance(obs.get('energyRatios'), dict) else None
        rms, activity, crest = (obs.get(k) for k in ('rms', 'activityRatio', 'crestDb'))
        cancellation = obs.get('monoCancellationDb')
        fully_cancelled = obs.get('monoFullyCancelled') is True
        valid = (_number(rms) and _number(low, 0, 1) and _number(activity, 0, 1)
                 and _number(crest) and (_number(cancellation, -1000, 0) or fully_cancelled))
        side = (1.0 if fully_cancelled else
                max(0.0, min(1.0, 1 - 10 ** (cancellation / 10)))
                if _number(cancellation, -1000, 0) else None)
        audible_rms = (0.0 if obj.get('muted') is True else rms * 10 ** (obj['gainDb'] / 20)) if _number(rms) else None
        if not _number(audible_rms):
            audible_rms, valid = None, False
        observations[sid] = {'rms': rms if _number(rms) else None,
                             'audibleRms': audible_rms,
                             'lowEnergyRatio': low if _number(low, 0, 1) else None,
                             'activityRatio': activity if _number(activity, 0, 1) else None,
                             'crestDb': crest if _number(crest) else None,
                             'sideEnergyRatio': side, 'valid': valid,
                             'active': valid and audible_rms > .001 and obs.get('active') is not False}
    maximum = max((o['audibleRms'] for o in observations.values() if o['active']), default=0)
    original_rms = analysis.get('original', {}).get('rms')
    residual_rms = analysis.get('reconstructionResidual', {}).get('rms')
    residual_ratio = (residual_rms / original_rms
                      if _number(original_rms) and original_rms > 0 and _number(residual_rms) else None)
    if residual_ratio is not None and not math.isfinite(residual_ratio):
        residual_ratio = None
        warnings.append('Residual/original RMS ratio is not finite; residual placement is unknown.')
    significant_residual = residual_ratio is not None and residual_ratio >= .1
    if significant_residual and residual_routing not in ids:
        warnings.append('Significant reconstruction residual has no identified scene carrier.')
    reports = []
    for obj in objects:
        sid = obj['id']
        evidence = observations[sid]
        role, reasons, track_warnings = 'unchanged', [], []
        if obj.get('muted') is True:
            reasons.append('Scene object is muted; retain all layout and motion.')
        elif significant_residual and sid == residual_routing:
            role = 'residual-carrier'
            obj.update(azimuthDeg=0.0, elevationDeg=0.0,
                       widthDeg=max(obj['widthDeg'], 30.0))
            reasons.append('Residual RMS is at least 0.1 of original RMS; keep composite carrier in front.')
            track_warnings.append('Raw stem observations do not measure the added residual; retain carrier width.')
        elif not evidence['valid']:
            reasons.append('Missing, duplicate, or invalid observations; retain layout and motion.')
        elif not evidence['active'] or (spacious and evidence['activityRatio'] < .25):
            reasons.append('Quiet or inactive observation; retain layout and motion.')
        elif evidence['lowEnergyRatio'] >= .5:
            role = 'front-low'
            obj.update(azimuthDeg=0.0, elevationDeg=0.0)
            reasons.append('At least half of measured spectral energy is below 200 Hz.')
        elif evidence['audibleRms'] >= .7 * maximum:
            role = 'front-primary'
            obj.update(azimuthDeg=max(-30.0, min(30.0, obj['azimuthDeg'])), elevationDeg=0.0)
            reasons.append('Audible RMS after scene gain is at least 0.7 of the strongest unmuted active object.')
        elif (spacious and evidence['audibleRms'] <= .65 * maximum
              and evidence['activityRatio'] >= .5 and evidence['crestDb'] <= 20):
            role = 'surround-secondary'
            obj.update(azimuthDeg=-115.0 if obj['azimuthDeg'] < 0 else 115.0,
                       elevationDeg=30.0, widthDeg=max(obj['widthDeg'], 75.0))
            reasons.append('Explicit spacious candidate: secondary RMS <=0.65 of reference, activity >=0.5, '
                           'crest <=20 dB, and low-frequency share <0.5; no Side requirement for mono companions.')
        elif (not spacious and evidence['audibleRms'] <= .5 * maximum and evidence['activityRatio'] >= .75
              and evidence['crestDb'] <= 15 and evidence['sideEnergyRatio'] >= .15):
            role = 'surround-secondary'
            obj.update(azimuthDeg=-120.0 if obj['azimuthDeg'] < 0 else 120.0,
                       elevationDeg=15.0 if evidence['sideEnergyRatio'] >= .5 else 0.0)
            reasons.append('Secondary RMS, sustained activity, moderate crest, and Side evidence support a surround candidate.')
        else:
            reasons.append('Evidence does not meet conservative placement thresholds; retain layout and motion.')
        if spacious and evidence['active'] and evidence['activityRatio'] >= .25 \
                and evidence['valid'] and role not in ('front-low', 'residual-carrier') \
                and not obj.get('muted'):
            if role == 'unchanged':
                role = 'front-secondary'
                obj.update(azimuthDeg=max(-45.0, min(45.0, obj['azimuthDeg'])), elevationDeg=0.0)
                reasons = ['Valid active non-low object retained in front; insufficient evidence for rear placement.']
            if role != 'surround-secondary':
                obj['widthDeg'] = max(obj['widthDeg'], 60.0)
                reasons.append('Explicit spacious candidate widens the front stereo pair to at least 60 degrees; gain unchanged.')
        if role != 'unchanged':
            obj['keyframes'] = []
        if evidence['active'] and evidence['sideEnergyRatio'] >= .15 \
                and (not spacious or evidence['activityRatio'] >= .25):
            if role == 'unchanged' and obj['widthDeg'] < 30:
                reasons.append('Protect stereo width only; retain position and motion.')
            obj['widthDeg'] = max(obj['widthDeg'], 30.0)
            track_warnings.append('Measured Side energy: retain stereo width; width zero would discard Side.')
        reports.append({'id': sid, 'role': role, 'evidence': evidence,
                        'reasons': reasons, 'warnings': track_warnings})
    if spacious:
        expanded = sum(item['role'] == 'surround-secondary' for item in reports)
        if not expanded:
            warnings.append('No safe active secondary object qualified for rear/height expansion; no channels were artificially filled.')
        warnings.append('Explicit static spacious candidate replaces classified object motion; use restore to recover the prior scene.')
    return candidate, {'schema': 'spatial-content-v1', 'analysisHash': analysis_hash,
                       **({'layout': layout, 'expandedObjectCount': expanded,
                           'thresholds': {'activeRmsAbove': .001, 'minimumActivity': .25,
                                          'secondaryRelativeRmsMaximum': .65, 'secondaryActivityMinimum': .5,
                                          'secondaryCrestMaximumDb': 20, 'lowEnergyProtectionMinimum': .5},
                           'motionPolicy': 'Classified objects become static; unknown, quiet and muted objects retain motion.'}
                          if spacious else {}),
                       'measurementScope': 'Original estimated stems, scene gain/mute; processed composite spectra not remeasured.',
                       'method': 'Conservative acoustic heuristics; no instrument recognition or optimality claim.',
                       'residualRmsRatio': residual_ratio, 'residualRouting': residual_routing,
                       'objects': reports, 'warnings': warnings,
                       'limitations': ['Thresholds are project heuristics, not a paper reproduction.',
                                       'Placement proposals require listening and physical playback verification.']}
