#!/usr/bin/env python3
"""Bounded local coordinator. Reuses workbench tools; never rates musical quality."""
import argparse
import asyncio
import hashlib
import html
import json
import math
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ValueError("本地工具不跟随重定向。")


class LocalTools:
    def __init__(self, base):
        value = urllib.parse.urlsplit(base)
        if value.scheme != "http" or value.hostname not in ("127.0.0.1", "localhost", "::1") \
                or value.username or value.password or value.path not in ("", "/") \
                or value.query or value.fragment or not value.port:
            raise ValueError("仅接受带端口的本机HTTP工作台地址。")
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, body=None, headers=None):
        if not path.startswith("/api/spatial/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("工具路径超出本地空间音频API。")
        if isinstance(body, dict):
            body = json.dumps(body, allow_nan=False).encode()
            headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(self.base + path, data=body, headers=headers or {})
        try:
            return self.opener.open(req, timeout=120)
        except urllib.error.HTTPError as error:
            detail = error.read(2048).decode("utf-8", "replace")
            raise ValueError(f"本地工具失败 HTTP {error.code}: {detail}") from error

    def json(self, path, body=None, headers=None):
        with self.request(path, body, headers) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("工具JSON超过8MiB。")
            return json.loads(raw)

    def download(self, path, dest):
        digest, count = hashlib.sha256(), 0
        with self.request(path) as response, dest.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > 2 * 1024 ** 3:
                    raise ValueError("单个产物超过2GiB，停止收集。")
                output.write(chunk)
                digest.update(chunk)
        return {"file": dest.name, "bytes": count, "sha256": digest.hexdigest(), "source": path}

    def import_file(self, path, route="native"):
        path = Path(path)
        native = route == "native"
        if not path.is_file() or not 0 < path.stat().st_size <= (1024 ** 3 if native else 200 * 1024 ** 2) \
                or (native and path.suffix.lower() != ".zip"):
            raise ValueError("原生入口须为1GiB以内的回执ZIP；显式separate入口接受200MiB以内的立体声音频。")
        boundary = uuid.uuid4().hex
        raw = path.read_bytes()
        # Only a generated safe filename enters the multipart header.
        filename = "native-stems.zip" if native else "stereo-source.audio"
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        body += raw + f"\r\n--{boundary}--\r\n".encode()
        project = self.json("/api/spatial/native/import" if native else "/api/spatial/import", body,
                            {"Content-Type": "multipart/form-data; boundary=" + boundary})
        project["importedFile"] = {"name": path.name, "bytes": len(raw),
                                   "sha256": hashlib.sha256(raw).hexdigest()}
        return project


def require_id(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{32}", value):
        raise ValueError("项目ID须为32位小写十六进制。")
    return value


def validate_focus(items, duration):
    result = []
    for start, end, note in items:
        start, end = float(start), float(end)
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration \
                or not note.strip() or len(note) > 200 or len(result) >= 16:
            raise ValueError("试听关注段须在曲长内，最多16段，每段说明1至200字。")
        result.append({"startSeconds": start, "endSeconds": end, "note": note})
    return result


def technical_checks(report, source_hash):
    metrics, match = report["metrics"], report["loudnessMatch"]
    lufs_a = metrics["baselineMatched"]["integratedLufs"]
    lufs_b = metrics["candidateMatched"]["integratedLufs"]
    peak = metrics["candidate"]["truePeakDbTP"]
    valid = lambda x: type(x) in (float, int) and math.isfinite(x)
    return {"sourceHashUnchanged": report["hashes"]["original"] == source_hash,
            "candidateTruePeakWithinProjectLimit": valid(peak) and peak <= -1,
            "loudnessMatched": match["status"] == "matched" and valid(lufs_a) and valid(lufs_b)
                and abs(lufs_a - lufs_b) <= .2,
            "humanListeningVerified": False, "physicalSpeakerSystemVerified": False}


class Coordinator:
    def __init__(self, tools, output, timeout):
        self.tools, self.output, self.timeout = tools, Path(output), timeout
        self.output.mkdir(parents=True, exist_ok=False)
        self.record = {"schema": "spatial-mix-agent-run-v1", "planner": "bounded-rules-v1",
                       "startedAt": time.time(), "state": "validating", "events": [],
                       "files": [], "selectedMix": None,
                       "limits": {"newCandidatesPerRun": 1, "automaticRetries": 0,
                                  "pollingBudgetSeconds": timeout, "httpRequestTimeoutSeconds": 120},
                       "limitations": ["规则协调器；未部署学习型音乐审美模型",
                           "估计分轨不等于原始乐器多轨；重建误差不是串音评分",
                           "关注段只组织试听，不自动推断曲式、声部角色或小节",
                           "FOA及通用HRTF试听不代表Atmos ADM或IOSONO现场验收"]}

    def event(self, stage, **details):
        self.record["state"] = stage
        self.record["events"].append({"stage": stage, "at": time.time(), **details})
        self.save()
        print(stage, flush=True)

    def save(self):
        dest = self.output / "run.json"
        temp = dest.with_suffix(".tmp")
        temp.write_text(json.dumps(self.record, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temp.replace(dest)

    def wait(self, pid):
        deadline = time.monotonic() + self.timeout
        last = None
        while time.monotonic() < deadline:
            project = self.tools.json("/api/spatial/projects/" + pid)
            state = project["state"]
            if state == "error":
                raise ValueError(project.get("error") or "本地任务失败；未重试。")
            if state not in ("separating", "analyzing", "mixing", "rendering"):
                return project
            if state != last:
                self.event("waiting", toolState=state)
                last = state
            time.sleep(.5)
        raise TimeoutError("等待预算已用完；未重提任务。服务端任务可能仍在运行，请先检查项目状态。")

    def collect(self, path, name):
        pid = self.record["projectId"]
        if not path.startswith(f"/api/spatial/projects/{pid}/files/"):
            raise ValueError("下载链接不属于本次项目。")
        item = self.tools.download(path, self.output / name)
        self.record["files"].append(item)
        self.save()
        return item

    def execute(self, args):
        route = getattr(args, "source_route", "native")
        status = self.tools.json("/api/spatial/status")
        if status.get("app") != "neural-spatial-audio" or not status.get("mixing", {}).get("ready"):
            raise ValueError("本地工作台或混音工具未就绪。")
        if args.project:
            pid = require_id(args.project)
            project = self.wait(pid)
        else:
            project = self.tools.import_file(args.input, route)
            pid = require_id(project["id"])
        base = "/api/spatial/projects/" + pid
        self.record.update(projectId=pid, runtimeVersion=status["version"], base=self.tools.base,
                           sourceName=project["input"]["name"],
                           importedFile=project.get("importedFile"),
                           focus=validate_focus(args.focus, project["input"]["durationSeconds"]),
                           intent=args.intent, sceneBefore=project.get("scene"))
        self.event("validated")
        if route == "separate" and project.get("state") == "imported":
            self.event("separating")
            self.tools.json(base + "/separate", {"workflow": "separated-stereo-v1"})
            project = self.wait(pid)
        if route == "native":
            if project.get("nativeStems", {}).get("schema") != "ace-native-stems-v1":
                raise ValueError("原生路线需要已校验的ACE声部包；不会回退成曲分离。")
            self.record["nativeStemManifestHash"] = project["source"]["manifestHash"]
        elif project.get("source", {}).get("workflow") != "separated-stereo-v1":
            raise ValueError("分离路线需要明确完成本地分离的项目；不自动迁移历史项目。")
        self.record["workflow"] = "native-stems-first-v1" if route == "native" else "separated-stereo-v1"
        self.record["separation"] = project["separation"]
        source = self.collect(project["input"]["audioUrl"], "source.wav")
        stems = {sid: self.collect(base + f"/files/stems/{sid}.wav", f"source_stem_{sid}.wav")
                 for sid in (obj["id"] for obj in project["scene"]["objects"])}
        reference_id = require_id(args.reference_project) if args.reference_project else None
        if args.intent == "reference" and reference_id is None:
            raise ValueError("reference意图需要明确的本地参考项目。")
        reference = None
        if reference_id:
            ref = self.tools.json("/api/spatial/projects/" + reference_id)
            ref_path = ref["input"]["audioUrl"]
            if not ref_path.startswith(f"/api/spatial/projects/{reference_id}/files/") or reference_id == pid:
                raise ValueError("参考项目无效。")
            reference = self.tools.download(ref_path, self.output / "reference.wav")
            self.record["files"].append(reference)
            self.record["referenceProjectId"] = reference_id
        analysis = project.get("analysis")
        expected_reference = reference["sha256"] if reference else None
        if not analysis or analysis["original"]["sha256"] != source["sha256"] \
                or (analysis.get("reference") or {}).get("sha256") != expected_reference \
                or any(item["sha256"] != stems[item["id"]]["sha256"] for item in analysis["stems"]):
            self.event("analyzing")
            self.tools.json(base + "/mix/analyze", {"referenceProjectId": reference_id})
            project = self.wait(pid)
            analysis = project["analysis"]
        if analysis["original"]["sha256"] != source["sha256"]:
            raise ValueError("分析与当前源文件哈希不一致。")
        if any(item["sha256"] != stems[item["id"]]["sha256"] for item in analysis["stems"]):
            raise ValueError("分析与收集的声部哈希不一致。")
        self.record["analysis"] = analysis
        self.record["analysisHash"] = project["analysisHash"]
        self.event("proposing")
        proposed = self.tools.json(base + "/mix/plan", {"intent": args.intent})
        plan = proposed["mixPlan"]
        self.record["plan"] = plan
        before = {run["id"] for run in proposed.get("mixRuns", [])}
        self.event("processing")
        self.tools.json(base + "/mix/process", {"plan": plan})
        project = self.wait(pid)
        mix_id = project["mixResultId"]
        run = next(item for item in project["mixRuns"] if item["id"] == mix_id)
        if run["analysisHash"] != self.record["analysisHash"]:
            raise ValueError("候选与本轮分析不一致。")
        self.record.update(mixId=mix_id, reusedCandidate=mix_id in before)
        self.event("collecting")
        for key, name in (("report", "mix.json"), ("baseline", "baseline_matched.wav"),
                          ("candidate", "candidate_matched.wav"), ("baselineMono", "baseline_mono.wav"),
                          ("candidateMono", "candidate_mono.wav"), ("package", "comparison.zip")):
            self.collect(run["urls"][key], name)
        report = json.loads((self.output / "mix.json").read_text(encoding="utf-8"))
        if report["plan"] != plan:
            raise ValueError("处理记录的计划与提交参数不同。")
        self.record["technicalChecks"] = technical_checks(report, source["sha256"])
        if not self.record["technicalChecks"]["sourceHashUnchanged"]:
            raise ValueError("处理记录源哈希不符。")
        if args.preview_3d:
            if not all(self.record["technicalChecks"][key] for key in (
                    "candidateTruePeakWithinProjectLimit", "loudnessMatched")):
                raise ValueError("候选未满足技术比较条件，停止空间预览。")
            self.event("rendering-preview")
            asyncio.run(render_preview(self, args, project))
        self.event("awaiting-listening")
        self.record["listeningTasks"] = report["hearingRequired"] + [
            "分别试听主体清晰度、瞬态、串音、低频、包围感与高度感；不用总分替代具体问题",
            "耳机与目标音箱分别记录；不能把未校准的物理系统标为通过"]
        write_audition(self.output, self.record)
        self.save()
        return self.record


async def render_preview(agent, args, project):
    from playwright.async_api import async_playwright
    pid, mix_id = project["id"], agent.record["mixId"]
    base = "/api/spatial/projects/" + pid
    original_scene = project["scene"]
    original_hash = project["sceneHash"]
    agent.record["sceneBefore"] = original_scene
    agent.save()
    applied_hash = None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--enable-unsafe-swiftshader", "--autoplay-policy=no-user-gesture-required"], **(
            {"executable_path": args.chromium} if args.chromium else {}))
        try:
            context = await browser.new_context(viewport={"width": 1920, "height": 1080})
            await context.add_init_script("localStorage.setItem('nr-spatial-last-project-v1'," + json.dumps(pid) + ")")
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(agent.tools.base + "/spatial-audio/")
            await page.wait_for_function("!document.querySelector('#mix-apply').disabled")
            panel = page.locator("details.advanced-mix")
            if await panel.get_attribute("open") is None:
                await panel.locator(":scope > summary").click()
            if agent.tools.json(base)["sceneHash"] != original_hash:
                raise ValueError("声场被其他操作修改，未应用预览。")
            await page.select_option("#mix-versions", mix_id)
            await page.select_option("#mix-layout", getattr(args, "spatial_layout", "preserve"))
            # Observe actual publication, rather than the button's transient busy state.
            async with page.expect_response(lambda r: r.url.endswith(base + "/binaural"),
                                            timeout=args.timeout * 1000) as finished:
                async with page.expect_response(lambda r: r.url.endswith(base + "/mix/apply")) as applied:
                    await page.click("#mix-apply")
                response = await applied.value
                if not response.ok:
                    raise ValueError("空间预览应用失败。")
                value = await response.json()
                applied_hash = value["sceneHash"]
                agent.record["previewSceneHash"] = applied_hash
                agent.record["previewScene"] = value["scene"]
                if value.get("contentPlan"):
                    agent.record["contentPlan"] = value["contentPlan"]
                    (agent.output / "content-plan.json").write_text(
                        json.dumps(value["contentPlan"], ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                    plan_file = agent.output / "content-plan.json"
                    agent.record["files"].append({"file": plan_file.name, "bytes": plan_file.stat().st_size,
                        "sha256": hashlib.sha256(plan_file.read_bytes()).hexdigest(), "source": "bound mix/apply receipt"})
                agent.save()
            if not (await finished.value).ok:
                raise ValueError("双耳渲染未通过发布检查。")
            value = agent.tools.json(base)
            if value["sceneHash"] != applied_hash or value["scene"].get("mixId") != mix_id:
                raise ValueError("空间预览场景已变化。")
            paths = value.get("artifacts", {})
            if not all(key in paths for key in ("binauralReportUrl", "binauralUrl", "foaUrl", "packageUrl")):
                raise ValueError("空间渲染未发布完整产物，请检查工作台错误状态。")
            for key, name in (("binauralReportUrl", "binaural.json"), ("binauralUrl", "binaural.wav"),
                              ("foaUrl", "foa.wav"), ("packageUrl", "spatial.zip")):
                agent.collect(paths[key], name)
            if paths.get("studioPackageUrl"):
                agent.collect(paths["studioPackageUrl"], "7.1.4逐通道.zip")
            receipt = json.loads((agent.output / "binaural.json").read_text(encoding="utf-8"))
            audio_hash = next(item["sha256"] for item in agent.record["files"] if item["file"] == "binaural.wav")
            if receipt["sceneHash"] != applied_hash or receipt["quality"]["passed"] is not True \
                    or receipt["quality"]["audioSha256"] != audio_hash:
                raise ValueError("双耳产物与场景/计量哈希不一致。")
            agent.record["preview"] = {"status": "technical-pass-awaiting-listening",
                                       "browser": browser.version, "receipt": receipt,
                                       "pageErrors": errors}
            if errors:
                raise ValueError("渲染页面出现脚本异常。")
            await page.evaluate("window.scrollTo(0,0)")
            await page.screenshot(path=str(agent.output / "声场候选.png"))
        finally:
            try:
                if applied_hash:
                    current = agent.tools.json(base)
                    if current["state"] == "ready" and current["sceneHash"] == applied_hash:
                        if "mixBeforeScene" not in project:
                            current = agent.tools.json(base + "/mix/apply", {
                                "mixId": None, "expectedSceneHash": applied_hash})
                        restored = agent.tools.json(base + "/scene", {
                            "scene": original_scene, "expectedSceneHash": current["sceneHash"]})
                        migrated = dict(original_scene)
                        if "renderTailSeconds" not in migrated:
                            migrated["renderTailSeconds"] = restored["scene"].get("renderTailSeconds")
                            agent.record["legacySceneTailMigration"] = migrated["renderTailSeconds"]
                        agent.record["sceneRestored"] = restored["scene"] == migrated
                        agent.record["restoredSceneHash"] = restored["sceneHash"]
                    else:
                        agent.record["sceneRestored"] = False
                        agent.record["restoreNotice"] = "项目忙碌或声场被修改；未覆盖其他操作，原场景保存在run.json。"
            finally:
                agent.save()
                await browser.close()
    if applied_hash and not agent.record.get("sceneRestored"):
        raise ValueError("空间试听已收集但原场景未恢复，请按run.json检查；未覆盖并发修改。")


def write_audition(folder, record):
    fonts = Path(__file__).resolve().parents[1] / "public/vendor/fonts"
    for name in ("NotoSansSC.ttf", "OFL-NotoSansSC.txt"):
        source, dest = fonts / name, folder / name
        if source.is_file():
            if not dest.exists():
                with source.open("rb") as src, dest.open("xb") as out:
                    shutil.copyfileobj(src, out)
            if not any(item["file"] == name for item in record["files"]):
                record["files"].append({"file": name, "bytes": dest.stat().st_size,
                    "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(), "source": "bundled public font/license"})
    tracks = [("原混音 · 等响度", "baseline_matched.wav"), ("候选 · 等响度", "candidate_matched.wav"),
              ("原混音 · 单声道兼容", "baseline_mono.wav"), ("候选 · 单声道兼容", "candidate_mono.wav")]
    if record.get("preview"):
        tracks.append(("候选空间预览 · 通用HRTF，未与二维版等响度配对", "binaural.wav"))
    content = "".join(f'<p>{html.escape(label)}</p><audio controls preload="metadata" src="{path}"></audio>'
                      for label, path in tracks)
    focuses = "".join(f'<li>{item["startSeconds"]:g}–{item["endSeconds"]:g} 秒：{html.escape(item["note"])}</li>'
                      for item in record["focus"])
    text = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>混音候选试听</title><style>@font-face{font-family:"NR CJK";src:url("NotoSansSC.ttf")}body{max-width:820px;margin:40px auto;padding:0 20px;'
            'font:16px/1.7 "NR CJK",system-ui;background:#f8f8f5;color:#202825}audio{width:100%}</style>'
            '<h1>混音候选试听</h1><p>技术检查完成；音乐质量待听辨，尚未选定最终版本。</p>'
            '<p>A/B为0.2 LU以内的电气响度匹配（状态见run.json）；耳机音量和房间声压仍需独立确认。'
            '单声道保留原监听增益；双耳预览单独评价，不是Dolby认证输出。</p>'
            + content + '<h2>关注段</h2><ul>' + focuses + '</ul><h2>试听记录</h2><ul>'
            + ''.join('<li>' + html.escape(item) + '</li>' for item in record['listeningTasks'])
            + '</ul><p><a href="run.json">运行记录</a> · <a href="comparison.zip">比较包</a></p>'
            '<script>for(const audio of document.querySelectorAll("audio"))audio.addEventListener("play",()=>{'
            'for(const other of document.querySelectorAll("audio"))if(other!==audio)other.pause();});</script></html>')
    if not record["technicalChecks"]["loudnessMatched"]:
        text = text.replace('A/B为0.2 LU以内的电气响度匹配（状态见run.json）', 'A/B响度匹配不可用，不作等响度比较')
    (folder / "试听.html").write_text(text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="原生ZIP；--source-route separate时为立体声音频")
    source.add_argument("--project")
    parser.add_argument("--source-route", choices=("native", "separate"), default="native",
                        help="明确选择分轨来源；失败不自动切换路线")
    parser.add_argument("--base", default="http://127.0.0.1:8773")
    parser.add_argument("--output", type=Path, required=True, help="新的运行目录；不覆盖已有输出")
    parser.add_argument("--intent", choices=("preserve", "clarity", "reference"), default="preserve")
    parser.add_argument("--reference-project")
    parser.add_argument("--focus", nargs=3, action="append", default=[], metavar=("START", "END", "NOTE"))
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--preview-3d", action="store_true", help="为候选渲染空间试听，再恢复原场景；不是最终选择")
    parser.add_argument("--spatial-layout", choices=("preserve", "content", "spacious"), default="preserve",
                        help="空间试听保留现有场景，或生成内容适配／宽广保留主体候选")
    parser.add_argument("--chromium", help="可选：现有Playwright兼容Chromium可执行文件；不会自动安装")
    args = parser.parse_args()
    agent = None
    try:
        if not 1 <= args.timeout <= 3600:
            raise ValueError("单项等待预算须为1至3600秒。")
        if args.spatial_layout != "preserve" and not args.preview_3d:
            raise ValueError("声场候选须同时启用 --preview-3d，才会生成并收集空间试听。")
        tools = LocalTools(args.base)
        agent = Coordinator(tools, args.output, args.timeout)
        agent.execute(args)
        print(str(args.output / "试听.html"))
    except (Exception, KeyboardInterrupt) as exc:
        if agent:
            agent.record["error"] = str(exc) or type(exc).__name__
            agent.event("failed")
        print(str(exc) or type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
