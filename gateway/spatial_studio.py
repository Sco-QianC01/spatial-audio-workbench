"""Channel-only studio interchange, with a separate sequential routing test."""
import csv
import json
from pathlib import Path
import zipfile

import numpy as np
import soundfile as sf

from spatial_mix import file_hash

LABELS = {"L": "左前", "R": "右前", "C": "中置", "LFE": "低频效果通道",
          "Ls": "左侧环绕", "Rs": "右侧环绕", "Lrs": "左后环绕", "Rrs": "右后环绕",
          "Ltf": "左前上方", "Rtf": "右前上方", "Ltr": "左后上方", "Rtr": "右后上方"}

README = """# 7.1.4 棚内逐通道直出

surround_channels/ 是完成空间声像与轨迹渲染的 12 路扬声器信号；不是四组分离声部，也不是耳机双耳文件。

1. 在空白工程中设为 48 kHz，将 surround_channels/ 的 12 个单声道 WAV 作为 12 条单声道轨同时导入同一工程零点。所有文件等长，含共同 0.25 秒尾段；不要按文件创建时间对齐，不要删掉前导静音。
2. 根据 routing.csv 的声道名称逐轨直接分配到棚内对应的单声道硬件输出。文件编号只是本包排序，不代表声卡端口号。请现场填写声卡、监听控制器和实际扬声器对应关系；不要凭默认 7.1.4 总线顺序猜测。
3. 保持轨道 0 dB、共同播放起点和通道间相对电平；关闭额外声像器、空间化、自动增益、上混、双耳解码和母线立体声折叠。不要给每轨独立归一化。音频中的轨迹已烘焙，无需重复导入自动化。若导入后声道不能独立送出，应先修正工程路由。
4. 先降低监听音量，再单独播放 routing_test/ 的 12 个等长文件（同时起点 0 秒），每隔 2 秒轮到下一个数字通道。普通通道 500 Hz、峰值 -30 dBFS；LFE 50 Hz、峰值 -40 dBFS；各 0.5 秒，20 ms 淡入淡出。它们只检查通道身份，不是房间声学校准信号，不对应指定声压级。
5. 数字识别测试每个时段仅一路有信号。棚内若启用低频管理，主通道测试可能同时使低音扬声器发声；这不等于数字串路。识别测试通过后停止/静音测试轨，再播放音乐轨。
6. 现场工程师继续完成扬声器身份、极性、增益、时延、低频管理与听音位置核查。用棚内规定的测量话筒、声压计和校准信号；本包没有替房间做这些校准。

本包音乐 LFE 故意留空，主通道保留低频；没有把全部低频重复送入 LFE，也没有在文件上施加 +10 dB。LFE 是独立节目通道，不能与低频管理重定向信号混为一谈。routing_test/ 的 LFE 文件单独用于识别低频路径。

名义坐标：0° 前、+90° 左、-90° 右、±180° 后，正仰角朝上。7 个平面声道 + 4 个上方声道使用名义 VBAP 几何；负仰角投至水平面。实际棚内角度和输出接线须现场确认。立体声分离声部为模型估计，不能保证还原原录音的多轨。

studio.json 记录采样数、电平、SHA256、来源和数字校验。routing.csv 的验收列留白，留给现场填写。JSON 中 physicalAcceptance=pending 表示未进行棚内物理验收。静音声道也保留全长，不表示文件缺失。

这些文件是离散声道 WAV，不是带对象元数据的 ADM BWF，也不构成 Dolby Atmos 编码/认证母版。耳机试听、文件校验和画面中声源移动，均不能代替棚内逐通道回放验收。
"""


def channel_checks(folder, report):
    """Verify the saved PCM, not just the pre-quantization float buffer."""
    paths = [folder / item["file"] for item in report["speakers"]]
    frames = report["audio"]["frames"]
    result = []
    with sf.SoundFile(folder / "surround_7_1_4.wav") as interleaved:
        if (interleaved.samplerate, interleaved.channels, len(interleaved), interleaved.subtype) != (48000, 12, frames, "PCM_24"):
            raise ValueError("交错文件格式不符合7.1.4直出约定；未发布。")
        for index, path in enumerate(paths):
            info = sf.info(path)
            if (info.samplerate, info.channels, info.frames, info.subtype) != (48000, 1, frames, "PCM_24"):
                raise ValueError("棚内通道格式或长度不一致；未发布。")
            peak, squares = 0.0, 0.0
            interleaved.seek(0)
            with sf.SoundFile(path) as mono:
                for data in mono.blocks(blocksize=65536, dtype="float64"):
                    reference = interleaved.read(len(data), dtype="float64", always_2d=True)[:, index]
                    if not np.isfinite(data).all() or not np.array_equal(data, reference):
                        raise ValueError("单声道件与渲染通道不一致；未发布。")
                    peak = max(peak, float(np.max(np.abs(data))))
                    squares += float(np.dot(data, data))
            if peak > .950001 or (report["channelOrder"][index] == "LFE" and peak != 0):
                raise ValueError("棚内通道余量或LFE约定不符；未发布。")
            result.append({"channel": report["channelOrder"][index], "file": report["speakers"][index]["file"],
                           "sha256": file_hash(path), "sampleRate": info.samplerate, "frames": info.frames,
                           "channels": 1, "subtype": info.subtype, "peak": peak, "rms": (squares/frames)**.5,
                           "equalsInterleavedChannel": True})
    return result


def routing_test(folder, report):
    rate = report["sampleRate"]
    order = report["channelOrder"]
    dest = folder / "routing_test"
    dest.mkdir()
    frames = rate * 2 * len(order)
    count, fade = round(rate * .5), round(rate * .02)
    envelope = np.ones(count)
    envelope[:fade] = np.sin(np.linspace(0, np.pi/2, fade))**2
    envelope[-fade:] = envelope[:fade][::-1]
    records = []
    for index, channel in enumerate(order):
        frequency, db = (50, -40) if channel == "LFE" else (500, -30)
        start = round((index * 2 + .5) * rate)
        audio = np.zeros(frames)
        audio[start:start+count] = 10**(db/20) * np.sin(2*np.pi*frequency*np.arange(count)/rate) * envelope
        path = dest / f"{index+1:02d}_{channel}.wav"
        sf.write(path, audio, rate, subtype="PCM_24")
        records.append({"channel": channel, "file": "routing_test/"+path.name, "sha256": file_hash(path),
                        "startSeconds": start/rate, "endSeconds": (start+count)/rate,
                        "frequencyHz": frequency, "peakDbfsTarget": db,
                        "steadySineRmsDbfsTarget": db-10*np.log10(2), "fadeSeconds": fade/rate,
                        "frames": frames, "sampleRate": rate, "subtype": "PCM_24"})
    return records


def studio_package(project, root, folder):
    """Build before atomic render publication; never requires browser HRTF."""
    dest = folder / "studio.zip"
    if dest.is_file():
        return
    report = json.loads((folder / "surround.json").read_text(encoding="utf-8"))
    if report.get("speakerLayout") != "7.1.4":
        return
    channels = channel_checks(folder, report)
    tests = routing_test(folder, report)
    with (folder / "routing.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["文件编号", "声道", "扬声器", "音乐文件", "识别音开始秒", "识别音Hz", "名义方位角", "名义仰角",
                         "DAW轨道", "声卡单声道输出", "监听控制器端口", "实际扬声器", "身份通过", "极性通过", "电平通过", "时延通过", "备注"])
        for index, speaker in enumerate(report["speakers"]):
            writer.writerow([f"{index+1:02d}", speaker["channel"], LABELS[speaker["channel"]], speaker["file"],
                             tests[index]["startSeconds"], tests[index]["frequencyHz"], speaker["azimuthDeg"],
                             speaker["elevationDeg"], *[""]*9])
    (folder / "STUDIO_README.md").write_text(README, encoding="utf-8")
    original = Path(root) / project["id"] / "original.wav"
    metadata = {"schema": "spatial-studio-handoff-v1", "speakerLayout": "7.1.4",
                "projectId": project["id"], "sourceProjectId": project.get("sourceProjectId", project["id"]),
                "sceneHash": report["sceneHash"], "inputName": project["input"].get("name"),
                "inputSha256": file_hash(original) if original.is_file() else None,
                "separationModel": project.get("separation", {}).get("model"),
                "sourceWorkflow": project.get("source", {}).get("workflow", "legacy-estimated-stems"),
                "nativeStemManifestHash": project.get("source", {}).get("manifestHash"),
                "channelOrder": report["channelOrder"], "channels": channels, "routingTest": tests,
                "timeline": report["timeline"], "startReference": "All files imported at the same DAW project zero, no embedded BWF timecode",
                "mixHeadroomGain": report["mixHeadroomGain"], "perChannelNormalization": False,
                "digitalChecks": {"equalLength": True, "sameFormat": True, "finite": True,
                                  "monoEqualsInterleaved": True, "musicLfeSilent": True},
                "physicalAcceptance": "pending", "roomCalibration": "not performed",
                "format": "Discrete speaker PCM WAV, not ADM BWF or a Dolby Atmos master"}
    files = ["scene.json", "surround.json", "render.json", "routing.csv", "STUDIO_README.md"]
    metadata["documents"] = [{"file": name, "sha256": file_hash(folder / name)} for name in files]
    (folder / "studio.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    with zipfile.ZipFile(dest.with_suffix(".tmp"), "w", zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
        for name in [*files, "studio.json", *[c["file"] for c in channels], *[c["file"] for c in tests]]:
            bundle.write(folder / name, name)
    dest.with_suffix(".tmp").replace(dest)
