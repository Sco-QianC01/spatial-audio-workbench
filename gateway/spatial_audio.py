"""Local object scene and first-order AmbiX rendering; no model or network calls."""
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from spatial_surround import STEREO_GEOMETRY, layout_config, speaker_gains, write_surround

IDS = ("drums", "bass", "other", "vocals")
NATIVE_SCHEMA = "ace-native-stems-v1"
NATIVE_IDS = ("vocals", "backing_vocals", "drums", "bass", "guitar", "keyboard",
              "percussion", "strings", "synth", "fx", "brass", "woodwinds")
LABELS = ("鼓", "低音", "其他声部", "人声")
RATE = 48000
RENDER_TAIL_SECONDS = .25


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}须在{low}至{high}之间。")
    return float(value)


def project_stem_ids(project):
    native = project.get("nativeStems")
    if native is None:
        return IDS
    if not isinstance(native, dict) or native.get("schema") != NATIVE_SCHEMA:
        raise ValueError("原生声部元数据schema无效。")
    tracks = native.get("tracks")
    if not isinstance(tracks, list) or not 2 <= len(tracks) <= 12:
        raise ValueError("原生项目须包含2至12个声部。")
    ids = []
    for track in tracks:
        sid = track.get("id") if isinstance(track, dict) else None
        if not isinstance(sid, str) or sid not in NATIVE_IDS or sid in ids:
            raise ValueError("原生声部ID无效或重复。")
        ids.append(sid)
    return tuple(ids)


def default_scene(project):
    ids = project_stem_ids(project)
    neutral = project.get("nativeStems") is not None or project.get("source", {}).get("workflow") == "separated-stereo-v1"
    positions = ([(f"轨道{i + 1:02d}", -30 + 60*i/(len(ids)-1), 0, 30)
                  for i in range(len(ids))] if neutral else
                 list(zip(LABELS, (-35, 0, 100, 25), (0, -15, 30, 10), (35, 30, 70, 15))))
    scene = {"schema": "spatial-scene-v1", "sampleRate": RATE,
             "durationSeconds": project["input"]["durationSeconds"],
             "renderTailSeconds": RENDER_TAIL_SECONDS,
             "objects": [{"id": sid, "label": label,
                          "audioUrl": f"/api/spatial/projects/{project['id']}/files/stems/{sid}.wav",
                          "azimuthDeg": float(az), "elevationDeg": float(el), "widthDeg": float(width),
                          "gainDb": 0.0, "muted": False, "keyframes": []}
                         for sid, (label, az, el, width) in zip(ids, positions)]}
    if neutral:
        scene["speakerLayout"] = "7.1.4"
    return scene


def validate_scene(value, project):
    ids = project_stem_ids(project)
    if not isinstance(value, dict) or value.get("schema") != "spatial-scene-v1" \
            or not isinstance(value.get("objects"), list) or len(value["objects"]) != len(ids):
        raise ValueError("场景须包含项目的全部声部。")
    scene = default_scene(project)
    if "speakerLayout" in value:
        layout_config(value["speakerLayout"])
        scene["speakerLayout"] = value["speakerLayout"]
    by_id = {}
    for obj in value["objects"]:
        if not isinstance(obj, dict) or obj.get("id") not in ids or obj["id"] in by_id:
            raise ValueError("声部ID无效或重复。")
        by_id[obj["id"]] = obj
    duration = scene["durationSeconds"]
    for obj in scene["objects"]:
        source = by_id[obj["id"]]
        for field, low, high in (("azimuthDeg", -180, 180), ("elevationDeg", -90, 90),
                                 ("widthDeg", 0, 120), ("gainDb", -24, 6)):
            obj[field] = number(source.get(field), low, high, field)
        if type(source.get("muted")) is not bool:
            raise ValueError("静音状态无效。")
        obj["muted"] = source["muted"]
        keys = source.get("keyframes", [])
        if not isinstance(keys, list) or len(keys) > 12002 or len(keys) == 1:
            raise ValueError("轨迹为空或包含2至12002个关键帧。")
        for frame in keys:
            if not isinstance(frame, dict):
                raise ValueError("关键帧无效。")
            obj["keyframes"].append({
                "timeSeconds": number(frame.get("timeSeconds"), 0, duration, "关键帧时间"),
                "azimuthDeg": number(frame.get("azimuthDeg"), -180, 180, "方位角"),
                "elevationDeg": number(frame.get("elevationDeg"), -90, 90, "仰角")})
        if keys and (obj["keyframes"][0]["timeSeconds"] != 0
                     or abs(obj["keyframes"][-1]["timeSeconds"] - duration) > 1e-6
                     or any(b["timeSeconds"] <= a["timeSeconds"]
                            for a, b in zip(obj["keyframes"], obj["keyframes"][1:]))):
            raise ValueError("轨迹须从0秒到音频结束，关键帧时间严格递增。")
    mix_id = value.get("mixId")
    if mix_id is not None:
        if not isinstance(mix_id, str) or not re.fullmatch("[a-f0-9]{64}", mix_id) or not any(
                run.get("id") == mix_id and run.get("status") == "awaiting-listening"
                for run in project.get("mixRuns", [])):
            raise ValueError("混音版本不存在或尚未完成。")
        scene["mixId"] = mix_id
        for obj in scene["objects"]:
            obj["audioUrl"] = (f"/api/spatial/projects/{project['id']}/files/mixes/"
                               f"{mix_id}/processed_stems/{obj['id']}.wav")
    return scene


def angles(obj, times, offset=0):
    if obj["keyframes"]:
        keys = obj["keyframes"]
        degrees = np.array([k["azimuthDeg"] for k in keys])
        unwrapped = np.r_[degrees[0], degrees[0] + np.cumsum((np.diff(degrees) + 180) % 360 - 180)]
        az = np.interp(times, [k["timeSeconds"] for k in keys],
                       np.radians(unwrapped))
        el = np.radians(np.interp(times, [k["timeSeconds"] for k in keys],
                                 [k["elevationDeg"] for k in keys]))
    else:
        az = np.full(times.shape, math.radians(obj["azimuthDeg"]))
        el = np.full(times.shape, math.radians(obj["elevationDeg"]))
    if offset:
        half = math.radians(offset)
        # A great-circle offset keeps stereo width finite at the poles.
        x = np.cos(half)*np.cos(az)*np.cos(el) - np.sin(half)*np.sin(az)
        y = np.cos(half)*np.sin(az)*np.cos(el) + np.sin(half)*np.cos(az)
        z = np.cos(half)*np.sin(el)
        az, el = np.arctan2(y, x), np.arcsin(np.clip(z, -1, 1))
    return az, el


def sn3d_coefficients(az, el):
    # ACN 0,1,2,3 = W,Y,Z,X; positive azimuth is listener-left.
    return np.column_stack((np.ones_like(az), np.sin(az) * np.cos(el),
                            np.sin(el), np.cos(az) * np.cos(el))).astype(np.float32)


def stats(data):
    if not data.size or not np.isfinite(data).all():
        raise ValueError("音频包含无效采样。")
    return {"frames": len(data), "channels": data.shape[1] if data.ndim == 2 else 1,
            "peak": float(np.max(np.abs(data))),
            "rms": float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))}


def render_foa(scene, stems_dir, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    objects_dir = output_dir / "objects"
    objects_dir.mkdir()
    tail = scene.get("renderTailSeconds", 0)
    frames = round(scene["durationSeconds"] * RATE) + round(tail * RATE)
    mixed = np.zeros((frames, 4), dtype=np.float32)
    layout = scene.get("speakerLayout", "5.1.2")
    surround = np.zeros((frames, len(layout_config(layout)[0])), dtype=np.float32)
    sources = []
    fold_down = []
    for obj in scene["objects"]:
        audio, rate = sf.read(Path(stems_dir) / f"{obj['id']}.wav",
                              dtype="float32", always_2d=True)
        if rate != 44100 or audio.shape[1] != 2 or not np.isfinite(audio).all() \
                or abs(len(audio) - round(scene["durationSeconds"]*rate)) > 1:
            raise ValueError("声部音频格式或完整时间线无效。")
        stereo = audio.astype(np.float64)
        mid = (stereo[:, 0] + stereo[:, 1]) / math.sqrt(2)
        side = (stereo[:, 0] - stereo[:, 1]) / math.sqrt(2)
        mid_power, side_power = float(np.mean(mid**2)), float(np.mean(side**2))
        total_power = mid_power + side_power
        ratio = mid_power / total_power if total_power > 0 else None
        applied = obj["widthDeg"] == 0 and not obj["muted"]
        fold_down.append({"id": obj["id"], "widthDeg": obj["widthDeg"], "applied": applied,
                          "midMeanSquare": mid_power, "sideMeanSquare": side_power,
                          "stereoTotalMeanSquare": total_power, "foldDownEnergyRatio": ratio,
                          "foldDownLossDb": -10*math.log10(ratio) if ratio is not None and ratio > 0 else None,
                          "fullyCancelled": total_power > 0 and mid_power == 0,
                          "warning": "宽度0会丢失超过一半的数字信号能量；请检查反相或Side成分。"
                                     if applied and ratio is not None and ratio < .5-1e-9 else None,
                          "meaning": "Source PCM before resampling/gain; orthonormal M=(L+R)/sqrt(2), "
                                     "S=(L-R)/sqrt(2). Width0 retains M only. Energy is not loudness or listening acceptance; "
                                     "null loss dB means silence or complete cancellation, see fullyCancelled."})
        del stereo, mid, side
        audio = np.pad(audio, ((0, round(tail * rate)), (0, 0)))
        audio = resample_poly(audio, 160, 147, axis=0).astype(np.float32)
        audio = audio[:frames]
        if len(audio) < frames:
            audio = np.pad(audio, ((0, frames - len(audio)), (0, 0)))
        for channel, side, offset in ((0, "L", obj["widthDeg"] / 2),
                                      (1, "R", -obj["widthDeg"] / 2)):
            name = f"{obj['id']}_{side}.wav"
            sf.write(objects_dir / name, audio[:, channel], RATE, subtype="FLOAT")
            gain = 0 if obj["muted"] else 10 ** (obj["gainDb"] / 20) / math.sqrt(2)
            for start in range(0, frames, 65536):
                end = min(frames, start + 65536)
                times = np.arange(start, end, dtype=np.float64) / RATE
                az, el = angles(obj, times, offset)
                mixed[start:end] += audio[start:end, channel, None] * gain * sn3d_coefficients(az, el)
                surround[start:end] += audio[start:end, channel, None] * gain * speaker_gains(az, el, layout)
            sources.append({"id": f"{obj['id']}_{side}", "group": obj["id"],
                            "audio": f"objects/{name}", "gainDb": obj["gainDb"] - 10 * math.log10(2),
                            "muted": obj["muted"], "greatCircleOffsetDeg": offset,
                            "azimuthDeg": obj["azimuthDeg"], "elevationDeg": obj["elevationDeg"],
                            "keyframes": obj["keyframes"]})
    before = stats(mixed)
    surround_before = stats(surround)
    if max(before["peak"], surround_before["peak"]) <= 1e-10:
        raise ValueError("场景没有有效声音；请检查静音与声部。")
    headroom = min(1.0, 0.95 / max(before["peak"], surround_before["peak"]))
    mixed *= headroom
    surround *= headroom
    sf.write(output_dir / "foa.wav", mixed, RATE, subtype="FLOAT")
    report = {"schema": "spatial-render-v2", "sceneHash": canonical_hash(scene),
              "stereoGeometry": STEREO_GEOMETRY,
              "format": "AmbiX first-order", "channelOrder": ["W", "Y", "Z", "X"],
              "normalization": "SN3D", "ordering": "ACN", "sampleRate": RATE,
              "coordinates": {"azimuth": "degrees; 0 front, +90 left, -90 right, +/-180 back",
                              "elevation": "degrees; +90 up, -90 down",
                              "cartesian": "X front, Y left, Z up"},
              "stereoMapping": "L/R virtual sources at great-circle offsets +/- width/2 along the center's "
                               "horizontal tangent (oriented by azimuth at poles), each gain / sqrt(2)",
              "trajectory": "piecewise linear; shortest azimuth arc; no inferred room reverb",
              "timeline": {"sourceDurationSeconds": scene["durationSeconds"],
                           "renderTailSeconds": tail, "outputDurationSeconds": frames / RATE,
                           "meaning": "Shared tail interval; FOA retains resampling response, not room reverb."},
              "mixHeadroomGain": headroom, "audio": stats(mixed), "renderSources": sources,
              "stereoFoldDown": fold_down,
              "limitation": "Not Dolby Atmos ADM or an IOSONO-validated input stream."}
    write_surround(output_dir, surround, RATE, report["sceneHash"], headroom,
                   report["timeline"], stats(surround), layout)
    (output_dir / "scene.json").write_text(json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "render.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
