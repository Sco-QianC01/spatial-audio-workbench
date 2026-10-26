"""Loopback-only local stereo -> stems -> scene -> AmbiX/binaural workbench."""
import argparse
import asyncio
from collections import deque
from functools import lru_cache
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import time
import uuid
import zipfile

import numpy as np
import soundfile as sf
from aiohttp import web

from spatial_audio import IDS, NATIVE_IDS, RATE, canonical_hash, default_scene, project_stem_ids, render_foa, stats, validate_scene
from spatial_native import MAX_BUNDLE_BYTES, WORKFLOW, import_native_bundle, require_mix_source
from spatial_online import ORIGIN as ONLINE_ORIGIN, download_audio, online_metadata, validate_audio_url
from spatial_generation import GenerationBridge
from spatial_mix import LOUDNESS_FILTER, build_plan, file_hash, parse_loudness, validate_plan
from spatial_content import build_content_scene
from spatial_studio import studio_package
from spatial_surround import LAYOUTS, STEREO_GEOMETRY, layout_config, surround_filename

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 200 * 1024 * 1024
MAX_SECONDS = 600


def save(project, root):
    target = root / project["id"] / "project.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(project, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(target)


def render_folder(project, root):
    folder = root / project["id"] / "renders" / project["sceneHash"]
    return folder / "v2" if (folder / "v2").exists() else folder


def json_object(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("导出记录不是有效对象。")
    return value


def wav_matches(path, channels, frames, subtype, rate=RATE):
    info = sf.info(path)
    return (info.samplerate, info.channels, info.frames, info.subtype) == (rate, channels, frames, subtype) \
        and info.format in ("WAV", "WAVEX") \
        and path.stat().st_size >= 44 + frames*channels*{"PCM_16": 2, "PCM_24": 3, "FLOAT": 4}[subtype]


@lru_cache(maxsize=256)
def checked_file_hash(path, signature):
    return file_hash(path)


def current_file_hash(path):
    stamp = path.stat()
    return checked_file_hash(path, (stamp.st_mtime_ns, stamp.st_ctime_ns, stamp.st_size))


@lru_cache(maxsize=128)
def checked_zip(path, signature, names):
    with zipfile.ZipFile(path) as bundle:
        members = bundle.namelist()
        return len(members) == len(set(members)) and all(bundle.getinfo(name).file_size > 0 for name in names) \
            and bundle.testzip() is None


def zip_has(path, names):
    stamp = path.stat()
    return checked_zip(path, (stamp.st_mtime_ns, stamp.st_ctime_ns, stamp.st_size), tuple(sorted(names)))


def render_complete(folder, expected_hash=None):
    try:
        scene = json_object(folder / "scene.json")
        layout = scene.get("speakerLayout", "5.1.2")
        order, _ = layout_config(layout)
        scene_hash = canonical_hash(scene)
        render = json_object(folder / "render.json")
        surround = json_object(folder / "surround.json")
        frames = round(scene["durationSeconds"]*RATE) + round(scene.get("renderTailSeconds", 0)*RATE)
        if scene.get("schema") != "spatial-scene-v1" or frames <= 0 \
                or (expected_hash is not None and expected_hash != scene_hash) \
                or render.get("schema") != "spatial-render-v2" or render.get("sceneHash") != scene_hash \
                or render.get("stereoGeometry") != STEREO_GEOMETRY \
                or surround.get("stereoGeometry") != STEREO_GEOMETRY \
                or not isinstance(render.get("audio"), dict) or render["audio"].get("frames") != frames \
                or surround.get("sceneHash") != scene_hash or surround.get("channelOrder") != list(order) \
                or surround.get("speakerLayout", "5.1.2") != layout:
            return False
        channels = [f"surround_channels/{i+1:02d}_{name}.wav" for i, name in enumerate(order)]
        objects = [f"objects/{sid}_{side}.wav" for sid in [obj["id"] for obj in scene["objects"]] for side in ("L", "R")]
        subtype = "PCM_24" if layout == "7.1.4" else "FLOAT"
        if not wav_matches(folder / "foa.wav", 4, frames, "FLOAT") \
                or not wav_matches(folder / surround_filename(layout), len(order), frames, subtype) \
                or not all(wav_matches(folder/name, 1, frames, subtype) for name in channels) \
                or not all(wav_matches(folder/name, 1, frames, "FLOAT") for name in objects):
            return False
        required = ["foa.wav", "scene.json", "render.json", surround_filename(layout), "surround.json", *channels, *objects,
                    *[f"stems/{obj['id']}.wav" for obj in scene["objects"]], "stems/separation.json"]
        if not zip_has(folder / "package.zip", required):
            return False
        if layout == "7.1.4":
            studio = json_object(folder / "studio.json")
            tests = [f"routing_test/{i+1:02d}_{name}.wav" for i, name in enumerate(order)]
            documents = {"scene.json", "surround.json", "render.json", "routing.csv", "STUDIO_README.md"}
            if studio.get("sceneHash") != scene_hash or studio.get("channelOrder") != list(order) \
                    or studio.get("speakerLayout") != layout \
                    or {item["file"] for item in studio["documents"]} != documents \
                    or not all(file_hash(folder/item["file"]) == item["sha256"] for item in studio["documents"]) \
                    or not all(wav_matches(folder/name, 1, RATE*2*len(order), "PCM_24") for name in tests) \
                    or not zip_has(folder/"studio.zip", ["studio.json", *documents, *channels, *tests]):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, zipfile.BadZipFile):
        return False


def outdated_render_geometry(folder):
    try:
        report = json_object(folder / "render.json")
        return report.get("schema") == "spatial-render-v2" and report.get("stereoGeometry") != STEREO_GEOMETRY
    except (OSError, ValueError):
        return False


def binaural_complete(folder, scene_hash, frames, expected_audio_hash=None):
    try:
        receipt = json_object(folder / "binaural.json")
        quality = receipt.get("quality")
        if receipt.get("sceneHash") != scene_hash or receipt.get("stereoGeometry") != STEREO_GEOMETRY \
                or not isinstance(quality, dict) or quality.get("passed") is not True:
            return False
        digest = quality.get("audioSha256")
        return isinstance(digest, str) and re.fullmatch("[a-f0-9]{64}", digest) is not None \
            and (expected_audio_hash is None or digest == expected_audio_hash) \
            and wav_matches(folder / "binaural.wav", 2, frames, "PCM_16") \
            and current_file_hash(folder / "binaural.wav") == digest \
            and zip_has(folder / "package.zip", ["binaural.wav", "binaural.json", "scene.json"])
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, zipfile.BadZipFile):
        return False


def mix_complete(project, root, run):
    try:
        mix_id = run["id"]
        if not isinstance(mix_id, str) or not re.fullmatch("[a-f0-9]{64}", mix_id):
            return False
        folder = root / project["id"] / "mixes" / mix_id
        analysis, plan, report = [json_object(folder / name) for name in ("analysis.json", "plan.json", "mix.json")]
        if report.get("schema") != "spatial-mix-result-v1" or report.get("status") != "awaiting-listening" \
                or run.get("status") != "awaiting-listening" or report.get("plan") != plan \
                or canonical_hash(plan) != mix_id or canonical_hash(analysis) != run["analysisHash"] \
                or plan.get("analysisHash") != run["analysisHash"]:
            return False
        names = ["baseline.wav", "candidate.wav", "baseline_matched.wav", "candidate_matched.wav",
                 "baseline_mono.wav", "candidate_mono.wav", "residual.wav",
                 *[f"processed_stems/{sid}.wav" for sid in project_stem_ids(project)]]
        frames = analysis["original"]["frames"]
        return set(report["outputFiles"]) == set(names) \
            and all(wav_matches(folder / name, 2, frames, "FLOAT", rate=44100) for name in names) \
            and all(current_file_hash(path) == report["hashes"][key] for key, path in (
                ("original", root / project["id"] / "original.wav"),
                ("baseline", folder / "baseline.wav"), ("candidate", folder / "candidate.wav"))) \
            and zip_has(folder / "bundle.zip", [*names, "analysis.json", "plan.json", "mix.json"])
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, zipfile.BadZipFile):
        return False


def artifacts(project, root):
    project["artifactWarnings"] = []
    project["audioWarnings"] = []
    if not project.get("sceneHash"):
        return {}
    folder = render_folder(project, root)
    if not folder.exists():
        return {}
    if not render_complete(folder, project["sceneHash"]):
        project["artifactWarnings"].append("立体声空间几何算法已更新，请重新渲染；旧导出文件已保留。"
            if outdated_render_geometry(folder) else "扬声器导出不完整或格式损坏，原文件已保留。")
        return {}
    project["audioWarnings"] = [f"{item['id']}: {item['warning']}"
        for item in json_object(folder / "render.json").get("stereoFoldDown", []) if item.get("warning")]
    base = f"/api/spatial/projects/{project['id']}/files/renders/{project['sceneHash']}"
    if folder.name == "v2":
        base += "/v2"
    layout = project.get("scene", {}).get("speakerLayout", "5.1.2")
    result = {key: f"{base}/{name}" for key, name in (
        ("foaUrl", "foa.wav"), ("sceneUrl", "scene.json"), ("packageUrl", "package.zip"),
        ("surroundUrl", surround_filename(layout)), ("surroundReportUrl", "surround.json"),
        ("studioPackageUrl", "studio.zip")) if (folder / name).is_file()}
    pointer = folder / "binaural-current.json"
    published, suffix = folder, ""
    try:
        active = None
        if pointer.is_file():
            active = json_object(pointer)
            revision = active.get("revision")
            if not isinstance(revision, str) or not re.fullmatch("[a-f0-9]{32}", revision):
                raise ValueError("双耳版本编号无效。")
            published, suffix = folder/"binaural"/revision, "/binaural/"+revision
        elif not any((folder/name).exists() for name in ("binaural.json", "binaural.wav")):
            return result
        receipt = json_object(published/"binaural.json")
        if receipt.get("stereoGeometry") != STEREO_GEOMETRY:
            project["artifactWarnings"].append("双耳立体声空间几何算法已更新，请重新渲染；旧文件已保留。")
            return result
        frames = json_object(folder/"render.json")["audio"]["frames"]
        if not binaural_complete(published, project["sceneHash"], frames,
                                 active["audioSha256"] if active is not None else None):
            raise ValueError("双耳版本不完整。")
        for key, name in (("binauralUrl", "binaural.wav"), ("binauralReportUrl", "binaural.json"), ("packageUrl", "package.zip")):
            result[key] = f"{base}{suffix}/{name}"
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, zipfile.BadZipFile):
        if published == folder:
            result.pop("packageUrl", None)
        project["artifactWarnings"].append("双耳导出缺失或记录损坏，已保留可用的扬声器文件。")
    return result


def package(project, root, headphones=None, render_dir=None):
    folder = root / project["id"]
    rendered = render_dir or render_folder(project, root)
    layout = project["scene"].get("speakerLayout", "5.1.2")
    order, _ = layout_config(layout)
    studio_package(project, root, rendered)
    dest = (headphones or rendered) / "package.zip"
    with zipfile.ZipFile(dest.with_suffix(".tmp"), "w", zipfile.ZIP_STORED) as bundle:
        for name in ("foa.wav", "scene.json", "render.json", surround_filename(layout), "surround.json",
                     "studio.json", "routing.csv", "STUDIO_README.md", "binaural.wav", "binaural.json"):
            path = (headphones if headphones and name.startswith("binaural.") else rendered) / name
            if path.is_file():
                bundle.write(path, name)
        for path in sorted((rendered / "surround_channels").glob("*.wav")):
            bundle.write(path, "surround_channels/" + path.name)
        for path in sorted((rendered / "routing_test").glob("*.wav")):
            bundle.write(path, "routing_test/" + path.name)
        for path in sorted((rendered / "objects").glob("*.wav")):
            bundle.write(path, "objects/" + path.name)
        for name in (*[s + ".wav" for s in project_stem_ids(project)], "separation.json"):
            bundle.write(folder / "stems" / name, "stems/" + name)
        if project["scene"].get("mixId"):
            mixed = folder / "mixes" / project["scene"]["mixId"]
            for name in ("analysis.json", "plan.json", "mix.json"):
                bundle.write(mixed / name, "mix/" + name)
        if project.get("nativeStems"):
            for path in sorted((folder / "native-source").rglob("*")):
                if path.is_file():
                    bundle.write(path, "native-source/" + path.relative_to(folder / "native-source").as_posix())
        if project.get("source"):
            bundle.writestr("source.json", json.dumps(project["source"], ensure_ascii=False, indent=2))
        bundle.writestr("README.txt",
            "Local spatial audio study\n" +
            ("Native generated stems: see stems/separation.json and native-source/manifest.json; producer receipts are not remote weight attestation.\n" if project.get("nativeStems") else "Source separation: see stems/separation.json; estimated sources, not original multitrack.\n") +
            "foa.wav: 48000 Hz FLOAT, ACN W,Y,Z,X, SN3D first-order AmbiX; do not play as stereo.\n"
            f"{surround_filename(layout)}: 48k, {','.join(order)}; see surround.json for bit depth; no speaker mask.\n"
            f"surround_channels/: {len(order)} mono speaker feeds; import by name using surround.json.\n"
            "LFE silent; nominal geometry, not room/Dolby certification.\n"
            +( "studio.zip (separate download): speaker-only print package with sequential routing IDs and CSV.\n"
               if layout == "7.1.4" else "")+
            f"objects/: {len(project_stem_ids(project))*2} mono 48k sources; render.json defines gain, stereo width, position and trajectories.\n"
            "If scene.mixId is present, objects contain the selected local DSP; mix/ records its provenance.\n"
            "scene.json: editable source-object scene. Angles: 0 front, +90 left, -90 right, +elevation up.\n"
            "binaural.wav, if present: browser Web Audio HRTF, generic ear model; headphone preview only.\n"
            "Timeline: source duration and shared rendering tail are recorded separately in receipts.\n"
            "Both mixes attenuate only when needed for headroom; FOA gain is in render.json.\n"
            "No Dolby Atmos ADM or IOSONO compatibility claim; field renderer and speaker mapping still required.\n")
    dest.with_suffix(".tmp").replace(dest)


async def command(*args, timeout=60):
    proc = await asyncio.create_subprocess_exec(*map(str, args), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        data, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise ValueError("本地音频进程失败；未发布输出。")
    captured = data if len(data) <= 2500 else data[:500]+b"\n[...]\n"+data[-2000:]
    return captured.decode("utf-8", "replace")


async def verify_binaural(path, ffmpeg):
    if not ffmpeg:
        raise ValueError("缺少FFmpeg，无法复测最终双耳文件；未发布输出。")

    async def meter(target):
        text = await command(ffmpeg, "-nostdin", "-hide_banner", "-threads", "1",
                             "-filter_threads", "1", "-i", target, "-map", "0:a:0",
                             "-vn", "-af", LOUDNESS_FILTER, "-f", "null", "-", timeout=180)
        value = parse_loudness(text)
        if value["truePeakDbTP"] is None:
            raise ValueError("最终双耳文件没有有效真峰读数；未发布输出。")
        return value

    before = await meter(path)
    gain = min(0, -1.1-before["truePeakDbTP"])
    corrected = path.with_suffix(".meter.wav")
    try:
        if gain:
            audio, rate = sf.read(path, dtype="float32", always_2d=True)
            sf.write(corrected, audio*10**(gain/20), rate, subtype="PCM_16")
        after = await meter(corrected) if gain else before
        if after["truePeakDbTP"] > -1:
            raise ValueError("最终双耳真峰超过−1dBTP；未发布输出。")
        if gain:
            corrected.replace(path)
    finally:
        corrected.unlink(missing_ok=True)
    info = sf.info(path)
    version = await command(ffmpeg, "-version")
    return {"schema": "spatial-binaural-quality-v1", "passed": True,
            "method": "FFmpeg loudnorm input metering on final 48 kHz stereo; output discarded",
            "filter": LOUDNESS_FILTER, "ffmpegVersion": version.splitlines()[0],
            "input": before, "final": after, "commonGainDb": gain, "ceilingDbTP": -1,
            "loudnessRangeSuitable": info.duration >= 60,
            "loudnessRangePolicy": "EBU R128-2023 p4 footnote 2",
            "audioSha256": file_hash(path),
            "meaning": "技术计量通过；同一常数仅衰减左右声道，不代表空间定位或音乐品质通过。"}


def environment_status(separation_python):
    expected = ROOT / ".venv"
    cfg = expected / "pyvenv.cfg"
    private_site = False
    if cfg.is_file():
        values = dict(line.lower().split("=", 1) for line in cfg.read_text().splitlines() if "=" in line)
        private_site = any(key.strip() == "include-system-site-packages" and value.strip() == "false"
                           for key, value in values.items())
    isolated = Path(sys.prefix).resolve() == expected.resolve() and sys.prefix != sys.base_prefix \
        and Path(separation_python).absolute() == Path(sys.executable).absolute() and private_site \
        and bool(sys.flags.no_user_site) and os.environ.get("PYTHONNOUSERSITE") == "1" \
        and not any(key.upper() in ("PYTHONPATH", "PYTHONHOME") for key in os.environ)
    return {"sourceRoot": str(ROOT), "python": sys.executable, "prefix": sys.prefix,
            "separationPython": str(separation_python), "isolated": isolated}


def create_app(output, separation_python, weights, demo=None, downloads=None, ffmpeg=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Resolving a venv's Python symlink would silently discard that environment.
    python = Path(separation_python).absolute()
    weights = Path(weights).resolve()
    demo = Path(demo).resolve() if demo else None
    downloads = Path(downloads).resolve() if downloads else None
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    projects = {}
    tasks = set()
    work_lock = asyncio.Lock()
    generation_bridge = GenerationBridge(output / ".online-jobs")
    def load_project(pid):
        if not isinstance(pid, str) or not re.fullmatch("[a-f0-9]{32}", pid):
            return None
        if pid in projects:
            return projects[pid]
        try:
            item = json_object(output / pid / "project.json")
            if item["id"] != pid:
                return None
            if item["state"] in ("separating", "rendering", "analyzing", "mixing"):
                item.update(state="error", error="上次本地任务被中断；未将部分输出视为完成。")
                if isinstance(item.get("studioPreparation"), dict):
                    item["studioPreparation"].update(status="error", error=item["error"])
                save(item, output)
            projects[pid] = item
            return item
        except (OSError, ValueError, KeyError, TypeError):
            return None

    for path in sorted(output.glob("*/project.json"))[-100:]:
        load_project(path.parent.name)

    @web.middleware
    async def boundary(request, handler):
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            raise web.HTTPForbidden(text="Local host only")
        origin = request.headers.get("Origin")
        if origin and origin not in (f"http://{request.host}", f"https://{request.host}"):
            raise web.HTTPForbidden(text="Same origin required")
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.text[:240]}, status=exc.status)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            response = web.json_response({"error": str(exc)[:240]}, status=400)
        except OSError:
            response = web.json_response({"error": "本地文件读写失败，请检查磁盘空间或文件权限。"}, status=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    app = web.Application(client_max_size=MAX_BUNDLE_BYTES, middlewares=[boundary])

    def get(request):
        project = load_project(request.match_info["pid"])
        if project is None:
            raise web.HTTPNotFound()
        return project

    def reply(project, status=200):
        project["artifacts"] = artifacts(project, output)
        runs = [run for run in project.get("mixRuns", []) if mix_complete(project, output, run)]
        if len(runs) != len(project.get("mixRuns", [])):
            project["artifactWarnings"].append("部分混音候选缺失或损坏，已隐藏试听；可重新处理相同提案恢复，原文件保留。")
        return web.json_response({**project, **({"mixRuns": runs} if "mixRuns" in project else {})}, status=status)

    def running(project):
        if project["state"] in ("separating", "rendering", "analyzing", "mixing"):
            raise web.HTTPConflict(text="本地任务正在运行。")

    def expected_scene(project, value):
        if "expectedSceneHash" in value and value["expectedSceneHash"] != project.get("sceneHash"):
            raise web.HTTPConflict(text="声场已被其他操作更新，请重新载入后再保存。")

    def require_scene_mix(project, scene):
        if not scene.get("mixId"):
            return
        run = next((run for run in project.get("mixRuns", []) if run["id"] == scene["mixId"]), None)
        if run is None or not mix_complete(project, output, run):
            raise ValueError("所选混音候选缺失或损坏，请重新处理提案，或恢复未混音的声场。")

    async def launch(project, worker, updates, *args):
        async def work():
            try:
                await worker(project, *args)
            except Exception as exc:
                project.update(state="error", error=str(exc)[:240])
            finally:
                try:
                    save(project, output)
                except OSError:
                    project.update(state="error", error="本地项目保存失败，请检查磁盘空间或文件权限后重试。")
                finally:
                    work_lock.release()

        if work_lock.locked():
            raise web.HTTPConflict(text="另一本地任务正在运行。")
        await work_lock.acquire()
        before = project.copy()
        try:
            project.update(updates)
            save(project, output)
            task = asyncio.create_task(work())
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        except BaseException:
            project.clear()
            project.update(before)
            work_lock.release()
            raise

    async def status(request):
        ready = python.is_file() and weights.is_file() and bool(ffmpeg)
        return web.json_response({
            "app": "neural-spatial-audio", "version": "0.8.4", "workflow": WORKFLOW,
            "environment": environment_status(python),
            "stereoGeometry": STEREO_GEOMETRY,
            "ready": True, "working": work_lock.locked(),
            "online": {"origin": ONLINE_ORIGIN, "downloadEnabled": downloads is not None},
            "separation": {"ready": ready, "model": "torchaudio HDemucs high",
                "device": "CPU", "reason": None if ready else "缺少本地分轨Python、模型权重或FFmpeg。"},
            "mixing": {"ready": bool(ffmpeg), "planner": "bounded-rules-v1",
                       "reason": None if ffmpeg else "缺少FFmpeg；未执行混音分析或处理。"},
            "limits": {"maxDurationSeconds": MAX_SECONDS, "maxUploadBytes": MAX_BYTES, "maxNativeBundleBytes": MAX_BUNDLE_BYTES},
            "demo": {"available": bool(demo and demo.is_file())},
            "projects": [{"id": p["id"], "state": p["state"], "input": p["input"]}
                         for p in sorted(projects.values(), key=lambda p: p.get("createdAt", 0), reverse=True)]})

    async def imported(staged, name, pid, folder, source=None):
        target = folder / "original.wav"
        await command(ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                      "-protocol_whitelist", "file,pipe",
                      "-format_whitelist", "wav,mp3,flac,ogg,mov,matroska,webm,aac,aiff",
                      "-i", staged, "-map", "0:a:0", "-vn", "-t", str(MAX_SECONDS + 1),
                      "-ar", "44100", "-c:a", "pcm_f32le", "-threads", "2", target, timeout=120)
        info = sf.info(target)
        if info.channels != 2 or not 0.1 <= info.duration <= MAX_SECONDS:
            raise ValueError("需要0.1至600秒的双声道音频；未自动转换单声道或截短长音频。")
        audio, _ = sf.read(target, dtype="float32", always_2d=True)
        values = stats(audio)
        if values["peak"] <= 1e-8:
            raise ValueError("输入音频为静音。")
        if shutil.disk_usage(output).free < target.stat().st_size * 16 + 512 * 1024 ** 2:
            raise ValueError("输出盘剩余空间不足，未启动分轨。")
        if source is None:
            staged.unlink()
        project = {"id": pid, "state": "imported", "workflow": None, "createdAt": time.time(),
                   "input": {"name": str(name)[:180], "sampleRate": info.samplerate,
                             "channels": 2, "durationSeconds": info.frames / info.samplerate,
                             "frames": info.frames,
                             "audioUrl": f"/api/spatial/projects/{pid}/files/original.wav"},
                   "progress": {"stage": "imported", "fraction": 0, "message": "已导入本地立体声"},
                   "separation": None, "scene": None, "sceneHash": None, "artifacts": {}, "error": None}
        if source is not None:
            project["source"] = source
        projects[pid] = project
        save(project, output)
        return reply(project)

    async def import_file(request):
        if not ffmpeg:
            raise ValueError("缺少FFmpeg。")
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "file" or not part.filename:
            raise ValueError("请选择音频文件。")
        pid = uuid.uuid4().hex
        folder = output / pid
        folder.mkdir()
        staged = folder / "upload.bin"
        try:
            count = 0
            with staged.open("xb") as handle:
                while chunk := await part.read_chunk(256 * 1024):
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise ValueError("音频文件超过200MiB。")
                    handle.write(chunk)
            if await reader.next() is not None:
                raise ValueError("一次只导入一个音频文件。")
            return await imported(staged, part.filename, pid, folder)
        except BaseException:
            shutil.rmtree(folder)
            raise

    async def native_import(request):
        if work_lock.locked():
            raise web.HTTPConflict(text="另一本地任务正在运行。")
        async with work_lock:
            reader = await request.multipart()
            part = await reader.next()
            if part is None or part.name != "file" or not part.filename or not part.filename.lower().endswith(".zip"):
                raise ValueError("请选择带manifest与生成回执的原生分轨ZIP包。")
            pid = uuid.uuid4().hex
            folder = output / pid
            folder.mkdir()
            staged = folder / "native-bundle.zip"
            try:
                count = 0
                with staged.open("xb") as handle:
                    while chunk := await part.read_chunk(256 * 1024):
                        count += len(chunk)
                        if count > MAX_BUNDLE_BYTES or shutil.disk_usage(output).free < count * 5 + 512 * 1024 ** 2:
                            raise ValueError("分轨包超过1GiB或本地剩余空间不足。")
                        handle.write(chunk)
                if await reader.next() is not None:
                    raise ValueError("一次只导入一个分轨包。")
                project = await asyncio.to_thread(import_native_bundle, staged, folder, pid)
                project["createdAt"] = time.time()
                project["source"]["bundleSha256"] = file_hash(staged)
                save(project, output)
                projects[pid] = project
                return reply(project, 201)
            except (zipfile.BadZipFile, RuntimeError) as exc:
                raise ValueError("原生分轨包无效或音频不能解码。") from exc
            finally:
                if pid not in projects:
                    shutil.rmtree(folder)

    async def online_status(request):
        metadata = await online_metadata()
        return web.json_response({**metadata, "downloadEnabled": downloads is not None,
                                  "busy": work_lock.locked()})

    async def online_import(request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="线上导入只接受application/json。")
        value = await mix_body(request, {"audioUrl", "jobId"})
        return reply(await import_online(value))

    async def import_online(value):
        audio_url = validate_audio_url(value.get("audioUrl"))
        provenance = generation_bridge.source(value["jobId"], audio_url) if value.get("jobId") else None
        if provenance:
            old = load_project(generation_bridge.jobs[value["jobId"]].get("projectId"))
            if old and old.get("source", {}).get("url") == audio_url:
                return old
        if downloads is None:
            raise ValueError("尚未配置线上音频下载目录。")
        if not ffmpeg:
            raise ValueError("缺少FFmpeg，未下载或导入。")
        if work_lock.locked():
            raise web.HTTPConflict(text="另一本地任务正在运行。")
        async with work_lock:
            original, source = await download_audio(audio_url, downloads)
            if provenance:
                source["generation"] = provenance
            pid = uuid.uuid4().hex
            folder = output / pid
            folder.mkdir()
            try:
                await imported(original, source["filename"], pid, folder, source)
                if provenance:
                    generation_bridge.imported(value["jobId"], pid)
                return projects[pid]
            except BaseException:
                shutil.rmtree(folder)
                raise

    async def generation_state(request):
        return web.json_response(generation_bridge.state())

    async def generation_connect(request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="研究账号连接只接受application/json。")
        value = await mix_body(request, {"username"})
        return web.json_response(await generation_bridge.connect(value.get("username")))

    async def generation_submit(request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="生成请求只接受application/json。")
        value = await mix_body(request, {"requestId", "model", "prompt", "duration", "seed", "abcScore", "prepareStudio"})
        return web.json_response(await generation_bridge.submit(value))

    async def generation_job(request):
        return web.json_response(await generation_bridge.poll(request.match_info["job"]))

    async def demo_route(request):
        if not demo or not demo.is_file():
            raise ValueError("本机没有演示音频。")
        pid = uuid.uuid4().hex
        folder = output / pid
        folder.mkdir()
        staged = folder / "upload.bin"
        try:
            shutil.copyfile(demo, staged)
            return await imported(staged, "八小节GM弦乐 · 技术样例（非分离质量评测）", pid, folder)
        except BaseException:
            shutil.rmtree(folder)
            raise

    async def separate_task(project):
        folder = output / project["id"]
        stage = folder / (".separation-" + uuid.uuid4().hex)
        proc = None
        try:
            original_hash = file_hash(folder / "original.wav")
            env = {**os.environ, "OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4"}
            with (folder / "separation.log").open("wb") as log:
                proc = await asyncio.create_subprocess_exec(
                    str(python), str(ROOT / "gateway/spatial_separation.py"),
                    str(folder / "original.wav"), str(stage), "--weights", str(weights),
                    stdout=log, stderr=asyncio.subprocess.STDOUT, env=env,
                    start_new_session=os.name != "nt")
                await asyncio.wait_for(proc.wait(), 1800)
            if proc.returncode:
                raise ValueError("本地CPU分离失败；详情已保存到separation.log，未发布部分输出。")
            report = json_object(stage / "separation.json")
            if report.get("sources") != list(IDS) or not isinstance(report.get("model"), dict) \
                    or report["model"].get("strictLoad") is not True \
                    or report.get("input", {}).get("frames") != project["input"]["frames"] \
                    or set(report.get("outputs", {})) != set(IDS):
                raise ValueError("分离回执缺少真实模型、声部或时间线信息。")
            for sid in IDS:
                path = stage / (sid + ".wav")
                info = sf.info(path)
                if (info.samplerate, info.channels, info.frames, info.subtype) != (
                        44100, 2, project["input"]["frames"], "FLOAT"):
                    raise ValueError("分离结果格式或帧数不一致；未发布部分输出。")
                audio, _ = sf.read(path, dtype="float32", always_2d=True)
                stats(audio)
            if file_hash(folder / "original.wav") != original_hash:
                raise ValueError("分离期间原版发生变化；未发布结果。")
            scene = default_scene(project)
            scene["speakerLayout"] = "7.1.4"
            for index, obj in enumerate(scene["objects"]):
                obj.update(label=f"轨道{index+1:02d}", azimuthDeg=-30+60*index/(len(IDS)-1),
                           elevationDeg=0.0, widthDeg=30.0, keyframes=[])
            stage.rename(folder / "stems")
            project["source"] = {**project.get("source", {}),
                                 "workflow": "separated-stereo-v1", "kind": "estimated-stems"}
            project.update(workflow="separated-stereo-v1", state="ready", error=None,
                           separation=report, scene=scene, sceneHash=canonical_hash(scene),
                           progress={"stage": "separated", "fraction": 1,
                                     "message": "本地CPU分离完成；估计声部须听辨确认，并非原生生成分轨。"})
        except asyncio.CancelledError:
            project.update(state="error", error="本地分离被中断；未发布部分输出。")
            raise
        except asyncio.TimeoutError:
            project.update(state="error", error="本地分离超过30分钟；未发布部分输出。")
        except Exception as exc:
            project.update(state="error", error=str(exc)[:240])
        finally:
            if proc and proc.returncode is None:
                if os.name != "nt":
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                await proc.wait()
            shutil.rmtree(stage, ignore_errors=True)

    async def separate(request):
        value = await mix_body(request, {"workflow"})
        if value != {"workflow": "separated-stereo-v1"}:
            raise ValueError("分离需明确选择workflow=separated-stereo-v1；不会自动回退分离成曲。")
        project = get(request)
        running(project)
        if project["state"] != "imported" or project.get("nativeStems"):
            raise ValueError("仅可对已导入且尚未分离的立体声显式启动分离。")
        if not python.is_file() or not weights.is_file() or not ffmpeg:
            raise ValueError("缺少本地分离Python、模型权重或FFmpeg；未启动子进程。")
        folder = output / project["id"]
        if (folder / "stems").exists():
            raise ValueError("声部目录已存在；禁止覆盖已有结果。")
        if work_lock.locked():
            raise web.HTTPConflict(text="另一本地任务正在运行。")
        if shutil.disk_usage(output).free < (folder / "original.wav").stat().st_size * 16 + 512 * 1024 ** 2:
            raise ValueError("输出盘空间不足；未启动分离。")
        await launch(project, separate_task, {"state": "separating", "error": None, "progress": {
            "stage": "separating", "fraction": 0, "message": "正在本地CPU分离；声部为模型估计。"}})
        return reply(project, 202)

    async def update_scene(request):
        project = get(request)
        running(project)
        if not project["separation"]:
            raise ValueError("请先完成本地分轨。")
        value = await request.json()
        if work_lock.locked():
            raise web.HTTPConflict(text="本地任务正在运行，暂不能修改声场。")
        expected_scene(project, value)
        scene = validate_scene(value["scene"], project)
        project.update(scene=scene, sceneHash=canonical_hash(scene), state="ready", error=None)
        save(project, output)
        return reply(project)

    async def mix_body(request, allowed):
        data = bytearray()
        async for chunk in request.content.iter_chunked(8192):
            data.extend(chunk)
            if len(data) > 65536:
                raise ValueError("混音参数超过64KiB。")
        value = json.loads(data)
        if not isinstance(value, dict) or value.keys() - allowed:
            raise ValueError("混音请求包含不支持的字段。")
        return value

    def mix_ready(project):
        running(project)
        require_mix_source(project)
        if not project.get("separation"):
            raise ValueError("请先完成本地分轨。")
        if not ffmpeg:
            raise ValueError("缺少本地FFmpeg。")
        if work_lock.locked():
            raise web.HTTPConflict(text="另一本地任务正在运行。")

    async def mix_worker(project, job):
        folder = output / project["id"]
        stage = folder / (".mix-" + uuid.uuid4().hex)
        proc = None
        try:
            stage.mkdir()
            job.update(original=str(folder / "original.wav"), stems=str(folder / "stems"),
                       ffmpeg=ffmpeg)
            config = stage / "job.json"
            config.write_text(json.dumps(job, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT / "gateway/spatial_mix_worker.py"), str(config),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=env, start_new_session=os.name != "nt")
            log, _ = await asyncio.wait_for(proc.communicate(), 1800)
            (folder / "mix.log").write_text(log[-12000:].decode("utf-8", "replace"), encoding="utf-8")
            if proc.returncode:
                detail = log.decode("utf-8", "replace").strip().splitlines()
                raise ValueError("本地混音任务失败：" + (detail[-1][:170] if detail else "进程异常退出"))
            result = json.loads((stage / "result.json").read_text(encoding="utf-8"))
            if job["mode"] == "analyze":
                analysis_hash = canonical_hash(result)
                dest = folder / "analyses"
                dest.mkdir(exist_ok=True)
                (stage / "result.json").replace(dest / f"{analysis_hash}.json")
                project.update(analysis=result, analysisHash=analysis_hash, mixPlan=None)
                message = "本地测量完成；可生成可编辑提案。"
            else:
                mix_id = canonical_hash(job["plan"])
                audio = stage / "audio"
                for name, value in (("analysis.json", job["analysis"]), ("plan.json", job["plan"]),
                                    ("mix.json", result)):
                    (audio / name).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                                        allow_nan=False), encoding="utf-8")
                with zipfile.ZipFile(audio / "bundle.zip", "w", zipfile.ZIP_STORED) as bundle:
                    for name in [*result["outputFiles"], "analysis.json", "plan.json", "mix.json"]:
                        bundle.write(audio / name, name)
                dest = folder / "mixes"
                dest.mkdir(exist_ok=True)
                if (dest / mix_id).exists():
                    (dest / mix_id).rename(dest / ("incomplete-" + uuid.uuid4().hex))
                audio.rename(dest / mix_id)
                base = f"/api/spatial/projects/{project['id']}/files/mixes/{mix_id}"
                project["mixRuns"] = [run for run in project.get("mixRuns", []) if run["id"] != mix_id]
                project["mixRuns"].append({
                    "id": mix_id, "analysisHash": job["plan"]["analysisHash"],
                    "intent": job["plan"]["intent"], "status": result["status"],
                    "createdAt": time.time(), "metrics": result["metrics"],
                    "loudnessMatch": result["loudnessMatch"],
                    "urls": {key: f"{base}/{name}" for key, name in (
                        ("baseline", "baseline_matched.wav"), ("candidate", "candidate_matched.wav"),
                        ("baselineMono", "baseline_mono.wav"), ("candidateMono", "candidate_mono.wav"),
                        ("report", "mix.json"), ("package", "bundle.zip"))}})
                project["mixResultId"] = mix_id
                message = "候选已处理，等待等响度比较试听；尚未应用到声场。"
            project.update(state="ready", error=None,
                           progress={"stage": "mix-ready", "fraction": 1, "message": message})
        except asyncio.CancelledError:
            project.update(state="error", error="本地混音任务被中断；未发布部分输出。")
            raise
        except asyncio.TimeoutError:
            project.update(state="error", error="本地混音任务超过30分钟；未发布部分输出。")
        except Exception as exc:
            project.update(state="error", error=str(exc)[:240])
        finally:
            if proc and proc.returncode is None:
                if os.name != "nt":
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                await proc.wait()
            shutil.rmtree(stage, ignore_errors=True)

    async def mix_analyze(request):
        project = get(request)
        value = await mix_body(request, {"referenceProjectId"})
        mix_ready(project)
        reference_id = value.get("referenceProjectId")
        reference = None
        if reference_id is not None:
            if not isinstance(reference_id, str) or reference_id not in projects or reference_id == project["id"]:
                raise ValueError("参考音频须为另一本机已导入项目。")
            reference = str(output / reference_id / "original.wav")
        await launch(project, mix_worker, {"state": "analyzing", "error": None, "progress": {
            "stage": "analyzing", "fraction": 0, "message": "正在测量原音频、分轨与重建残差…"}},
            {"mode": "analyze", "reference": reference})
        return reply(project, 202)

    async def mix_plan(request):
        project = get(request)
        value = await mix_body(request, {"intent"})
        mix_ready(project)
        if not project.get("analysis"):
            raise ValueError("请先分析本地音频。")
        project["mixPlan"] = build_plan(project["analysis"], value.get("intent"))
        project.update(state="ready", error=None, progress={
            "stage": "mix-plan", "fraction": 1, "message": "混音提案已生成，可编辑参数后处理候选。"})
        save(project, output)
        return reply(project)

    async def mix_process(request):
        project = get(request)
        value = await mix_body(request, {"plan"})
        mix_ready(project)
        if not project.get("analysis"):
            raise ValueError("请先分析本地音频。")
        plan = validate_plan(value.get("plan"), project["analysis"])
        mix_id = canonical_hash(plan)
        complete = [run for run in project.get("mixRuns", []) if mix_complete(project, output, run)]
        if any(run["id"] == mix_id for run in complete):
            project.update(state="ready", error=None, mixPlan=plan, mixResultId=mix_id, progress={
                "stage": "mix-ready", "fraction": 1, "message": "已复用相同提案的完成版本，可比较试听。"})
            save(project, output)
            return reply(project)
        if sum(run.get("analysisHash") == plan["analysisHash"] for run in complete) >= 2:
            raise ValueError("本轮测量已保存两个候选；请先比较现有版本。")
        if shutil.disk_usage(output).free < (output / project["id"] / "original.wav").stat().st_size * 24 + 512 * 1024 ** 2:
            raise ValueError("输出盘空间不足，未启动混音处理。")
        await launch(project, mix_worker, {"state": "mixing", "error": None, "mixPlan": plan, "progress": {
            "stage": "mixing", "fraction": 0, "message": "正在本地执行DSP、真峰值复测与等响度匹配…"}},
            {"mode": "process", "plan": plan, "analysis": project["analysis"]})
        return reply(project, 202)

    async def mix_apply(request):
        project = get(request)
        value = await mix_body(request, {"mixId", "layout", "expectedSceneHash"})
        mix_ready(project)
        expected_scene(project, value)
        if "mixId" not in value or value.get("layout", "preserve") not in ("preserve", "anchored", "content", "spacious"):
            raise ValueError("请选择完成的混音版本与有效声场布局。")
        mix_id = value["mixId"]
        if mix_id is None:
            before = project.get("mixBeforeScene")
            if before:
                project["scene"] = before
                del project["mixBeforeScene"]
                project.pop("contentPlan", None)
                if "mixBeforeContentPlan" in project:
                    project["contentPlan"] = project.pop("mixBeforeContentPlan")
        else:
            scene = json.loads(json.dumps(project["scene"]))
            scene["mixId"] = mix_id
            scene = validate_scene(scene, project)
            require_scene_mix(project, scene)
            content_plan = None
            if value.get("layout") in ("content", "spacious"):
                run = next(run for run in project["mixRuns"] if run["id"] == mix_id)
                folder = output / project["id"] / "mixes" / mix_id
                analysis = json_object(folder / "analysis.json")
                mix_report = json_object(folder / "mix.json")
                if canonical_hash(analysis) != run["analysisHash"] \
                        or mix_report["plan"]["analysisHash"] != run["analysisHash"]:
                    raise ValueError("内容规划与所选混音版本的测量不一致。")
                scene, content_plan = build_content_scene(
                    scene, analysis, mix_report.get("residualRouting"), value["layout"])
                if content_plan["analysisHash"] != run["analysisHash"]:
                    raise ValueError("内容规划缺少有效的测量绑定。")
                dsp = mix_report["plan"]
                if any(dsp["masterEq"].values()) or any(
                        stem["gainDb"] or stem["highpassHz"] or stem["eq"]["gainDb"] or stem["compressor"]
                        for stem in dsp["stems"]):
                    content_plan["warnings"].append(
                        "角色使用处理前分轨测量；本候选含逐轨DSP或主均衡，处理后的音色与能量需复测及听辨。")
                scene = validate_scene(scene, project)
                content_plan.update(mixId=mix_id, sceneHash=canonical_hash(scene))
            if value.get("layout") == "anchored":
                # Project preset, not a loudspeaker or broadcast standard.
                positions = {obj["id"]: (obj["azimuthDeg"], obj["elevationDeg"], obj["widthDeg"])
                             for obj in default_scene(project)["objects"]} if project.get("nativeStems") or project.get("source", {}).get("workflow") == "separated-stereo-v1" else {
                                 "drums": (-20, 0, 40), "bass": (0, 0, 30),
                                 "other": (60, 20, 60), "vocals": (0, 0, 15)}
                for obj in scene["objects"]:
                    obj["azimuthDeg"], obj["elevationDeg"], obj["widthDeg"] = positions[obj["id"]]
                    obj["keyframes"] = []
            if "mixBeforeScene" not in project:
                project["mixBeforeScene"] = json.loads(json.dumps(project["scene"]))
                if project.get("contentPlan"):
                    project["mixBeforeContentPlan"] = project["contentPlan"]
            project.pop("contentPlan", None)
            if content_plan:
                project["contentPlan"] = content_plan
            project["scene"] = scene
        project.update(sceneHash=canonical_hash(project["scene"]), state="ready", error=None)
        save(project, output)
        return reply(project)

    async def render_task(project):
        stage = None
        try:
            folder = output / project["id"]
            dest = folder / "renders" / project["sceneHash"] / "v2"
            if not render_complete(dest, project["sceneHash"]):
                if dest.exists():
                    outdated = outdated_render_geometry(dest)
                    preserved = dest.with_name("incomplete-"+uuid.uuid4().hex)
                    dest.rename(preserved)
                    project["renderRecovery"] = {"sceneHash": project["sceneHash"],
                        "preservedDirectory": str(preserved.relative_to(folder)),
                        "reason": "spatial geometry updated; re-render required" if outdated else "incomplete or invalid render cache"}
                dest.parent.mkdir(parents=True, exist_ok=True)
                stage = dest.with_name("tmp-" + uuid.uuid4().hex)
                sources = (folder / "mixes" / project["scene"]["mixId"] / "processed_stems"
                           if project["scene"].get("mixId") else folder / "stems")
                await asyncio.to_thread(render_foa, project["scene"], sources, stage)
                await asyncio.to_thread(package, project, output, None, stage)
                stage.rename(dest)
            project.update(state="ready", error=None,
                           progress={"stage": "rendered", "fraction": 1,
                                     "message": "AmbiX与"+project["scene"].get("speakerLayout", "5.1.2")+"声道已渲染"})
        except Exception as exc:
            project.update(state="error", error=str(exc)[:240])
        finally:
            if stage and stage.exists():
                shutil.rmtree(stage)

    async def render(request):
        project = get(request)
        running(project)
        if not project.get("scene"):
            raise ValueError("请先完成分轨并设置场景。")
        if work_lock.locked():
            raise web.HTTPConflict(text="另一本地任务正在运行。")
        expected_scene(project, await request.json() if request.can_read_body else {})
        require_scene_mix(project, project["scene"])
        await launch(project, render_task, {"state": "rendering", "error": None,
            "progress": {"stage": "rendering", "fraction": 0, "message": "正在本地编码全景声"}})
        return reply(project, 202)

    async def online_prepare(request):
        raise ValueError("请明确选择本地分离路线，或导入已校验的原生分轨；线上生成不自动续接空间混音。")

    async def binaural(request):
        project = get(request)
        running(project)
        scene_hash = project.get("sceneHash")
        if request.headers.get("X-Scene-Hash") != scene_hash:
            raise ValueError("声场已变化；未接收旧的耳机渲染。")
        if request.headers.get("X-Spatial-Geometry") != STEREO_GEOMETRY:
            raise ValueError("立体声空间几何算法已更新；请刷新页面并重新渲染双耳音频。")
        folder = render_folder(project, output)
        if not render_complete(folder, scene_hash):
            raise ValueError("请先按当前空间几何算法重新渲染全景声编码。")
        if work_lock.locked():
            raise web.HTTPConflict(text="另一本地任务正在运行。")
        async with work_lock:
            revision = uuid.uuid4().hex
            published = folder / "binaural" / revision
            stage = published.with_name("tmp-" + revision)
            stage.mkdir(parents=True)
            tmp = stage / "binaural.wav"
            pointer = folder / (".binaural-current-" + revision + ".tmp")
            try:
                count = 0
                with tmp.open("xb") as handle:
                    async for chunk in request.content.iter_chunked(256 * 1024):
                        count += len(chunk)
                        if count > MAX_BYTES:
                            raise ValueError("耳机音频超过限制。")
                        handle.write(chunk)
                info = sf.info(tmp)
                if info.format not in ("WAV", "WAVEX") or info.subtype != "PCM_16":
                    raise ValueError("耳机输出必须为PCM16 WAV；未发布其他容器或编码。")
                audio, rate = sf.read(tmp, dtype="float32", always_2d=True)
                values = stats(audio)
                if project.get("sceneHash") != scene_hash:
                    raise ValueError("声场已变化；未发布旧的耳机渲染。")
                tail = project["scene"].get("renderTailSeconds", 0)
                frames = round(project["input"]["durationSeconds"] * RATE) + round(tail * RATE)
                if rate != RATE or values["channels"] != 2 or values["frames"] != frames \
                        or not 1e-10 < values["peak"] <= 0.951:
                    raise ValueError("耳机音频格式、时长或电平无效。")
                guard_peak = float(np.max(np.abs(audio[-round(.05 * RATE):]))) if tail else None
                if guard_peak is not None and guard_peak > 1 / 32768:
                    raise ValueError("耳模尾音尚未衰减；未发布可能截尾的双耳输出。")
                quality = await verify_binaural(tmp, ffmpeg)
                values = stats(sf.read(tmp, dtype="float32", always_2d=True)[0])
                (stage / "binaural.json").write_text(json.dumps({
                    "engine": "Browser Web Audio OfflineAudioContext / PannerNode HRTF",
                    "origin": "client-rendered; waveform format verified, HRTF not identifiable from PCM",
                    "stereoGeometry": STEREO_GEOMETRY,
                    "sceneHash": scene_hash, "sampleRate": RATE, "audio": values, "quality": quality,
                    "timeline": {"sourceDurationSeconds": project["input"]["durationSeconds"],
                                 "renderTailSeconds": tail, "outputDurationSeconds": frames / RATE,
                                 "finalGuardSeconds": .05 if tail else 0,
                                 "finalGuardPeak": guard_peak, "finalGuardLimit": 1 / 32768},
                    "browser": request.headers.get("User-Agent", "")[:300],
                    "limitation": "Generic browser ear model; not individual or physical-room calibration."
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                await asyncio.to_thread(package, {**project, "sceneHash": scene_hash}, output, stage)
                # Publish immutable audio/receipt/package together, then switch the single pointer.
                stage.rename(published)
                pointer.write_text(json.dumps({"revision": revision,
                    "audioSha256": quality["audioSha256"]}), encoding="utf-8")
                pointer.replace(folder / "binaural-current.json")
            except OSError:
                raise web.HTTPInternalServerError(text="本地导出写入失败；此前已发布版本保留，请检查磁盘空间或文件权限。")
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
                pointer.unlink(missing_ok=True)
        return reply(project)

    async def project_route(request):
        return reply(get(request))

    async def files(request):
        project = get(request)
        name = request.match_info["name"]
        base = output / project["id"]
        target = (base / name).resolve()
        stem = name in {f"stems/{s}.wav" for s in project_stem_ids(project)}
        native_source = project.get("nativeStems") and name in {"native-source/manifest.json",
            "stems/separation.json", *[f"native-source/{item}" for item in [project["nativeStems"]["context"]["file"],
                *[t["file"] for t in project["nativeStems"]["tracks"]],
                *[t["task"]["responseFile"] for t in project["nativeStems"]["tracks"]]]]}
        channel_names = "|".join(sorted({f"{i+1:02d}_{channel}" for order, _ in LAYOUTS.values()
                                         for i, channel in enumerate(order)}))
        rendered = re.fullmatch(
            r"renders/([a-f0-9]{64})/(?:v2/)?(foa\.wav|scene\.json|render\.json|surround_(?:5_1_2|7_1_4)\.wav|surround\.json|binaural\.wav|binaural\.json|package\.zip|studio\.zip|studio\.json|routing\.csv|STUDIO_README\.md|"
            r"binaural/[a-f0-9]{32}/(binaural\.wav|binaural\.json|package\.zip)|"
            rf"(?:surround_channels|routing_test)/(?:{channel_names})\.wav|"
            rf"objects/({'|'.join((*NATIVE_IDS, 'other'))})_[LR]\.wav)", name)
        mixed = re.fullmatch(
            r"mixes/([a-f0-9]{64})/(baseline\.wav|candidate\.wav|baseline_matched\.wav|candidate_matched\.wav|"
            r"baseline_mono\.wav|candidate_mono\.wav|"
            r"residual\.wav|mix\.json|plan\.json|analysis\.json|bundle\.zip|"
            rf"processed_stems/({'|'.join((*NATIVE_IDS, 'other'))})\.wav)", name)
        if not target.is_relative_to(base) or not target.is_file() \
                or not (name == "original.wav" or stem or native_source or rendered or mixed):
            raise web.HTTPNotFound()
        if stem and project["separation"] is None:
            raise web.HTTPNotFound()
        if rendered:
            receipt_folder = base / "renders" / rendered[1]
            if name.startswith(f"renders/{rendered[1]}/v2/"):
                receipt_folder /= "v2"
            if not render_complete(receipt_folder, rendered[1]):
                raise web.HTTPNotFound()
            if rendered[2].startswith("binaural") or (
                    rendered[2] == "package.zip" and (receipt_folder / "binaural.json").exists()):
                frames = json_object(receipt_folder / "render.json")["audio"]["frames"]
                if not binaural_complete(target.parent, rendered[1], frames):
                    raise web.HTTPNotFound()
        if mixed:
            run = next((run for run in project.get("mixRuns", []) if run["id"] == mixed[1]), None)
            if run is None or not mix_complete(project, output, run):
                raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def static(request):
        path = request.match_info.get("path", "")
        if path in ("", "spatial-audio", "spatial-audio/"):
            target = ROOT / "spatial-audio/index.html"
        elif path in ("spatial-audio/app.mjs", "spatial-audio/mix.mjs", "spatial-audio/automation.mjs", "spatial-audio/model-link.mjs", "spatial-audio/scene-view.mjs", "spatial-audio/coordinates.mjs",
                      "spatial-audio/assets/listener-head.json", "spatial-audio/assets/LISTENER-LICENSE.txt",
                      "spatial-audio/trajectory.mjs", "public/vendor/three/lines/Line2.js", "public/vendor/three/lines/LineGeometry.js",
                      "public/vendor/three/lines/LineMaterial.js", "public/vendor/three/lines/LineSegments2.js", "public/vendor/three/lines/LineSegmentsGeometry.js",
                      "public/vendor/three/three.module.min.js", "public/vendor/three/three.core.min.js", "public/vendor/three/OrbitControls.js",
                      "spatial-audio/style.css", "public/spatial-icon.svg",
                      "docs/SPATIAL-AUDIO.md", "docs/SPATIAL-VIEW.md",
                      "docs/SPATIAL-FOUR-MODEL-WORKFLOW.md", "docs/SPATIAL-STUDIO-HANDOFF.md",
                      "docs/SPATIAL-STEM-ROUTES.md", "docs/examples/ace-native-stems-plan.json", "public/vendor/fonts/NotoSansSC.ttf"):
            target = ROOT / path
        else:
            raise web.HTTPNotFound()
        response = web.FileResponse(target)
        if target.suffix == ".mjs":
            response.content_type = "text/javascript"
        return response

    app.router.add_get("/api/spatial/status", status)
    app.router.add_post("/api/spatial/native/import", native_import)
    app.router.add_post("/api/spatial/import", import_file)
    app.router.add_get("/api/spatial/online/status", online_status)
    app.router.add_post("/api/spatial/online/import", online_import)
    app.router.add_post("/api/spatial/online/prepare", online_prepare)
    app.router.add_get("/api/spatial/online/generation", generation_state)
    app.router.add_post("/api/spatial/online/connect", generation_connect)
    app.router.add_post("/api/spatial/online/generate", generation_submit)
    app.router.add_get("/api/spatial/online/jobs/{job:[a-f0-9]{32}}", generation_job)
    app.router.add_post("/api/spatial/demo", demo_route)
    app.router.add_get("/api/spatial/projects/{pid}", project_route)
    app.router.add_post("/api/spatial/projects/{pid}/separate", separate)
    app.router.add_post("/api/spatial/projects/{pid}/scene", update_scene)
    app.router.add_post("/api/spatial/projects/{pid}/mix/analyze", mix_analyze)
    app.router.add_post("/api/spatial/projects/{pid}/mix/plan", mix_plan)
    app.router.add_post("/api/spatial/projects/{pid}/mix/process", mix_process)
    app.router.add_post("/api/spatial/projects/{pid}/mix/apply", mix_apply)
    app.router.add_post("/api/spatial/projects/{pid}/render", render)
    app.router.add_post("/api/spatial/projects/{pid}/binaural", binaural)
    app.router.add_get("/api/spatial/projects/{pid}/files/{name:.*}", files)
    app.router.add_get("/{path:.*}", static)

    async def close(app):
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)
    app.on_cleanup.append(close)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8773)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--separation-python", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--demo", type=Path)
    parser.add_argument("--downloads", type=Path, help="Explicit online-audio originals directory; disabled when absent")
    parser.add_argument("--ffmpeg", help="Explicit local FFmpeg executable")
    args = parser.parse_args()
    web.run_app(create_app(args.output, args.separation_python, args.weights, args.demo, args.downloads, args.ffmpeg),
                host="127.0.0.1", port=args.port)
