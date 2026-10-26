"""Validate producer receipts and lossless native stems before creating a local scene."""
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import zipfile
from urllib.parse import urljoin, urlsplit

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from spatial_audio import NATIVE_IDS, NATIVE_SCHEMA, canonical_hash, default_scene

MAX_BUNDLE_BYTES = 1024 * 1024 ** 2
BASE_MODEL = "acestep-v15-base"
WORKFLOW = {"id": "explicit-stems-routes-v1", "locked": True,
            "routes": {"native-stems-first-v1": "ACE Base lego原生生成声部",
                       "separated-stereo-v1": "已有立体声在本地自动分离"},
            "steps": ["明确选择分轨来源", "声部与时间线校验", "本地混音与Automation", "7.1.4逐通道导出"],
            "separationFallback": False,
            "reason": "可选择原生生成分轨或本地自动分离；两条路线分别记录来源，不自动回退。学校当前公开接口尚未验证Base/lego。"}


def require_native(project):
    native = project.get("nativeStems")
    if not isinstance(native, dict) or native.get("schema") != NATIVE_SCHEMA:
        raise ValueError("固定工作流要求ACE原生生成分轨；请先导入带生成回执的无损分轨包。不会回退成曲分离。")


def require_mix_source(project):
    if project.get("nativeStems"):
        require_native(project)
    elif project.get("source", {}).get("workflow") != "separated-stereo-v1" \
            or project.get("source", {}).get("kind") != "estimated-stems" \
            or not project.get("separation", {}).get("model", {}).get("strictLoad"):
        raise ValueError("请导入已校验的原生分轨，或明确选择本地自动分离；历史来源不自动转入新混音。")


def _json(bundle, name):
    if bundle.getinfo(name).file_size > 256 * 1024:
        raise ValueError("分轨JSON超过256KiB。")
    value = json.loads(bundle.read(name).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("分轨JSON必须为对象。")
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("分轨SHA256无效。")
    return value


def import_native_bundle(path, folder, pid):
    """The caller owns an unpublished new folder and removes it if validation fails."""
    folder = Path(folder)
    raw = folder / "native-source"
    with zipfile.ZipFile(path) as bundle:
        entries = bundle.infolist()
        names = [item.filename for item in entries]
        if len(entries) > 26 or len(set(names)) != len(names) or "manifest.json" not in names \
                or sum(item.file_size for item in entries) > MAX_BUNDLE_BYTES \
                or any(item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16) for item in entries):
            raise ValueError("分轨包文件重复、过多、加密、包含链接或超过1GiB。")
        manifest = _json(bundle, "manifest.json")
        producer = manifest.get("producer")
        if manifest.get("schema") != NATIVE_SCHEMA or not isinstance(producer, dict) or producer.get("model") != BASE_MODEL:
            raise ValueError("仅接受ACE Base的原生生成分轨包；extract、SFT或成曲分离不是此工作流。")
        tracks, timeline, context = manifest.get("tracks"), manifest.get("timeline", {}), manifest.get("context", {})
        if not isinstance(timeline, dict) or not isinstance(context, dict):
            raise ValueError("分轨时间线或上下文无效。")
        if not isinstance(tracks, list) or not 2 <= len(tracks) <= len(NATIVE_IDS):
            raise ValueError("需要2至12个独立生成的声部。")
        ids = [track.get("id") if isinstance(track, dict) else None for track in tracks]
        if any(sid not in NATIVE_IDS for sid in ids) or len(set(ids)) != len(ids):
            raise ValueError("原生声部ID无效或重复。")
        rate, frames = timeline.get("sampleRate"), timeline.get("frames")
        if type(rate) is not int or rate not in (44100, 48000) or type(frames) is not int \
                or not rate // 10 <= frames <= rate * 600 or type(timeline.get("startFrame")) is not int \
                or timeline["startFrame"] != 0:
            raise ValueError("原生分轨须共用零起点、44.1/48kHz和完整的0.1至600秒时间线。")
        if context.get("file") not in ("context.wav", "context.flac"):
            raise ValueError("生成上下文必须是无损context.wav或context.flac。")
        _sha(context.get("sha256"))
        expected = {"manifest.json", context["file"]}
        receipts = {}
        for track in tracks:
            sid, task = track["id"], track.get("task", {})
            if not isinstance(task, dict):
                raise ValueError("分轨任务无效。")
            if track.get("file") != f"stems/{sid}.wav" or task.get("responseFile") != f"receipts/{sid}.json":
                raise ValueError("分轨或回执文件路径无效。")
            _sha(track.get("sha256"))
            if task.get("taskType") != "lego" or task.get("model") != BASE_MODEL \
                    or type(task.get("seed")) is not int or not 0 <= task["seed"] <= 2147483647 \
                    or not isinstance(task.get("prompt"), str) or not 1 <= len(task["prompt"].strip()) <= 4000 \
                    or not isinstance(task.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task["id"]) \
                    or task.get("contextSha256") != context["sha256"]:
                raise ValueError("声部必须绑定同一上下文、Base lego任务、提示词与seed。")
            receipt = _json(bundle, task["responseFile"])
            request = receipt.get("request", {})
            if not isinstance(request, dict) or not isinstance(receipt.get("submit"), dict) or not isinstance(receipt.get("result"), dict):
                raise ValueError("生成回执结构无效。")
            if canonical_hash(request) != _sha(task.get("requestHash")) \
                    or any(request.get(key) != value for key, value in {
                        "task_type": "lego", "model": BASE_MODEL, "seed": task["seed"],
                        "instruction": f"Generate the {sid} track based on the audio context:",
                        "prompt": task["prompt"], "contextSha256": context["sha256"], "audio_format": "wav"}.items()) \
                    or receipt.get("submit", {}).get("task_id") != task["id"] \
                    or receipt.get("result", {}).get("task_id") != task["id"] \
                    or type(receipt.get("result", {}).get("status")) is not int \
                    or receipt["result"]["status"] != 1 or receipt.get("audioSha256") != track["sha256"]:
                raise ValueError("生成回执、请求摘要、结果状态或音频摘要不匹配。")
            results = json.loads(receipt["result"].get("result", "null"))
            if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict) \
                    or type(results[0].get("status")) is not int or results[0]["status"] != 1 \
                    or results[0].get("dit_model") != BASE_MODEL or not isinstance(results[0].get("file"), str):
                raise ValueError("回执必须报告单个成功的Base声部结果，不能推测缺失的模型身份。")
            api = producer.get("api")
            if not isinstance(api, str) or urlsplit(api).scheme != "http" or not urlsplit(api).hostname \
                    or urlsplit(api).username or urlsplit(api).password:
                raise ValueError("生成服务来源无效。")
            result_url = urlsplit(urljoin(api.rstrip("/") + "/", results[0]["file"]))
            if result_url.netloc != urlsplit(api).netloc or result_url.scheme != "http" \
                    or result_url.path != "/v1/audio" or not result_url.query.startswith("path=") or result_url.fragment:
                raise ValueError("回执音频地址与生成服务不一致。")
            receipts[sid] = receipt
            expected.update((track["file"], task["responseFile"]))
        if set(names) != expected:
            raise ValueError("分轨包缺少必需文件或含未声明文件；未解压。")
        if len({track["sha256"] for track in tracks}) != len(tracks) \
                or len({track["task"]["id"] for track in tracks}) != len(tracks):
            raise ValueError("独立分轨不得复制同一文件或复用同一个生成任务。")
        raw.mkdir()
        for name in sorted(expected):
            dest = raw / name
            dest.parent.mkdir(exist_ok=True)
            digest = hashlib.sha256()
            with bundle.open(name) as source, dest.open("xb") as output:
                while data := source.read(1024 * 1024):
                    digest.update(data)
                    output.write(data)
            expected_hash = context["sha256"] if name == context["file"] else next(
                (item["sha256"] for item in tracks if item["file"] == name), None)
            if expected_hash and digest.hexdigest() != expected_hash:
                raise ValueError("下载原件SHA256与分轨清单不符。")
    # Preserve exact downloaded originals. Only derived working copies are resampled.
    stem_dir = folder / "stems"
    stem_dir.mkdir()
    mixed, pcm_hashes = None, set()
    for name in [context["file"], *[item["file"] for item in tracks]]:
        info = sf.info(raw / name)
        if info.format not in ("WAV", "WAVEX", "FLAC") or info.subtype not in ("PCM_16", "PCM_24", "PCM_32", "FLOAT", "DOUBLE") \
                or info.samplerate != rate or info.frames != frames or info.channels not in (1, 2):
            raise ValueError("原生声部/上下文格式或帧数不一致；不截短、不补齐、不推测偏移。")
        audio, _ = sf.read(raw / name, dtype="float32", always_2d=True)
        if not np.isfinite(audio).all() or np.max(np.abs(audio)) <= 1e-8:
            raise ValueError("原生声部/上下文含无效采样或为静音。")
        if info.channels == 1:
            audio = np.repeat(audio, 2, axis=1)
        pcm_hash = hashlib.sha256(audio.tobytes()).hexdigest()
        if name == context["file"]:
            pcm_hashes.add(pcm_hash)
            continue
        if pcm_hash in pcm_hashes:
            raise ValueError("不同文件包含相同采样，不能作为独立声部。")
        pcm_hashes.add(pcm_hash)
        if rate != 44100:
            divisor = math.gcd(rate, 44100)
            audio = resample_poly(audio, 44100 // divisor, rate // divisor, axis=0).astype(np.float32)
        sid = Path(name).stem
        sf.write(stem_dir / (sid + ".wav"), audio, 44100, subtype="FLOAT")
        if mixed is None:
            mixed = np.zeros(audio.shape, dtype=np.float64)
        mixed += audio
    sf.write(folder / "original.wav", mixed, 44100, subtype="FLOAT")
    normalized = {**manifest, "validation": {
        "scope": "Local file hashes, producer supplied task receipts, format and timeline; not remote weight attestation",
        "sourcePreserved": True, "nativeRate": rate, "workingRate": 44100,
        "workingFrames": len(mixed), "baseline": "Unmastered unity-gain sum of native stems; context is excluded",
        "monoMapping": "Mono working copies duplicated to L/R; source unchanged",
        "musicalIsolation": "pending listening", "modelExecution": "producer receipts; not independently attested"}}
    (stem_dir / "separation.json").write_text(json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    project = {"id": pid, "state": "ready", "nativeStems": normalized,
               "separation": normalized, "source": {"kind": "ace-native-stems", "workflow": WORKFLOW["id"],
                    "producer": manifest["producer"], "manifestHash": canonical_hash(manifest)},
               "input": {"name": "ACE原生分轨", "sampleRate": 44100, "channels": 2,
                         "durationSeconds": len(mixed) / 44100, "frames": len(mixed),
                         "audioUrl": f"/api/spatial/projects/{pid}/files/original.wav"},
               "artifacts": {}, "error": None,
               "progress": {"stage": "native-stems-ready", "fraction": 1, "message": "原生分轨文件与回执已校验，等待混音与听辨"}}
    project["scene"] = default_scene(project)
    project["sceneHash"] = canonical_hash(project["scene"])
    return project
