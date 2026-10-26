# 0.8.0 分轨制作入口

两条路线都需要明确选择。原生路线失败时不会自动改用分离；线上生成结束、页面重载和历史 `prepareStudio` 回执也不会自行启动分离。普通线上生成继续产出立体声。

| 入口 | 声部来源 | 开始条件 |
| --- | --- | --- |
| ACE 原生生成分轨 · ZIP | ACE Base 根据同一个 context 分别生成的乐器声部 | 已有提供官方 API 且加载 Base 的服务 |
| 立体声 · 本地自动分离 | 从已有成曲估计出的 drums、bass、other、vocals | 本地分离运行环境及现有权重可用 |

## ACE Base：同一 context 分别生成，再导入

准备双声道无损 WAV/FLAC context，采样率 44.1 或 48 kHz，时长 0.1–600 秒；生成服务的实际时长限制仍须满足。所有声部共享它的 SHA256、完整帧数和零起点。context 是条件音频，导入后的混音不把它再作为一条声部相加。

1. 复制并编辑 [计划示例](examples/ace-native-stems-plan.json)。每轨包含 `id`、具体 `prompt`、整数 `seed`，共 2–12 轨，ID 不重复。允许的 ID 为 `vocals`、`backing_vocals`、`drums`、`bass`、`guitar`、`keyboard`、`percussion`、`strings`、`synth`、`fx`、`brass`、`woodwinds`。示例提示词是制作起点，不保证模型严格隔离乐器。
2. 在仓库根目录，用已有安装 `aiohttp`、`numpy`、`soundfile` 的 Python 执行：

   ```bash
   python tools/generate-ace-native-stems.py \
     --context /path/to/context.wav \
     --plan docs/examples/ace-native-stems-plan.json \
     --endpoint http://127.0.0.1:8001 \
     --output /path/to/new-native-run
   ```

   `--endpoint` 默认即 loopback 8001。输出目录须为新目录，或相同参数的可恢复目录。该工具只接受 loopback 或项目已有学校 origin；允许一个地址不代表该地址支持官方 API。
3. 工具先读 `/v1/models`，仅在列出 `acestep-v15-base` 且其 `is_loaded`（若提供）为 `true` 时提交。每轨上传同一 context，使用 `task_type=lego`、固定 seed、`thinking=false`、单结果 WAV，并串行轮询、完整下载。它不是模型切换器，不下载权重，不调用模型初始化、不替换为 XL-SFT，也不改用 `extract`。
4. 成功后，在工作台“制作入口”选择“ACE 原生生成分轨 · ZIP”，导入输出目录内的 `bundle.zip`。失败先看终端 `BLOCKED` 和 `blocked.json`；有 task ID 时用完全相同参数恢复。提交结果未知时不会自动重发，应先由操作者核对远端任务。401 不会触发自动登录或凭据查找。

项目当前学校能力记录仅有 ACE XL-SFT，不能据此保证 Base/lego 可用；本说明未重新探测学校服务或声称原生生成已经实测通过。

包内 `manifest.json` 使用 `ace-native-stems-v1`，保存 context、时间线、声部 SHA256、任务 ID 和请求摘要；`receipts/*.json` 保存请求、提交响应、查询结果及音频摘要。结果须明确报告 `dit_model=acestep-v15-base`。实际音频须为支持的无损 PCM/float、双声道、同采样率与帧数；工具不裁切、补齐或重采样来凑齐。重复任务、重复声部或把 context 原样充当声部会被拒绝。每轨有 45 分钟预算和 200 MiB 下载上限。

回执和哈希证明本地文件与所记录请求的对应关系，不是远端权重身份的加密证明；相同帧数也不证明拍点、串音或艺术质量合格。

## 立体声：明确选择本地分离

线上入口：展开“线上四模型 · 立体声下载与分离”，用 ACE XL-SFT、Shao、MiniMax 或 YuE 生成；任务成功后点击对应结果的 **“下载并本地分离”**。这一点击才下载、导入并启动分离，调用带 `workflow: 'separated-stereo-v1'`；成功前忙锁阻止重复提交，失败后可再次点击重试。

本地文件入口：将“制作入口”改为 **“立体声 · 本地自动分离”**，选择或拖入音频，完成导入后点击 **“本地自动分离”**。导入动作本身不表示分离已完成。也可使用现有协调工具：

```bash
python tools/spatial-mix-agent.py \
  --input /path/to/stereo.wav \
  --source-route separate \
  --output /path/to/new-mix-run \
  --intent preserve \
  --preview-3d \
  --spatial-layout content
```

本地网关须已运行；`--base` 默认 `http://127.0.0.1:8773`。输出目录须为新目录。`--preview-3d --spatial-layout content` 生成内容适配的空间试听候选，不代表最终采用；浏览器预览复用已有兼容 Chromium，不自动安装。

当前分离实现复用 CPU 上的 torchaudio HDemucs，产出四类估计声部。它不是“最新 SOTA”承诺，也不能恢复或证明录制时的原始独立轨。残差、重构相似度等电气指标不能替代听感验收；不要用“总和接近原曲”推断串音少、瞬态完整或相位稳定。

## 两条路线汇合后的制作与验收

1. 在“轨道”逐轨试听，再合听：检查串音、缺失乐器、瞬态损伤、相位/单声道兼容性及起止同步。原生生成也必须检查，不凭路线标签免验。
2. 先保留现有平衡，比较基线与候选 A/B；响度匹配是否可用以运行记录为准。候选不是自动批准的最终混音。
3. 编排可编程轨迹，核对时间、方位、高度、声像宽度及声部运动；使用空间预览检查候选，再决定采用哪一版。
4. 渲染并导出 7.1.4 的 12 条 mono 扬声器通道。通道文件与双耳预览不等于 ADM 母版或 Dolby 认证交付；实际棚内仍须校准通道映射、电平、延时与扬声器布局，并进行全程听辨。

## 实现依据、阅读深度与硬件边界

操作入口按当前 [原生生产 CLI](../tools/generate-ace-native-stems.py)、[原生导入器](../gateway/spatial_native.py)、[分离实现](../gateway/spatial_separation.py)、[协调 CLI](../tools/spatial-mix-agent.py)、[线上界面](../spatial-audio/model-link.mjs) 与 [本地入口](../spatial-audio/app.mjs) 核对。此文是操作与边界说明，未执行真实生成、整曲分离、渲染计时或棚内验收，不提供成功率、实时性或音质实测数字。

外部资料按相关章节核对，未声称逐行通读全部文档或重跑作者模型：

- [ACE 官方 HTTP API](https://ace-step.github.io/ACE-Step-1.5/en/API)：核对模型列表、multipart 提交、查询状态与结果下载契约。
- [ACE 官方 INFERENCE：Lego](https://ace-step.github.io/ACE-Step-1.5/en/INFERENCE)：核对 Base 限定、context、instruction 与乐器 ID；这是推理能力说明，不是学校部署证明。
- [torchaudio 2.9.1 HDemucs 源码](https://github.com/pytorch/audio/blob/v2.9.1/src/torchaudio/models/_hdemucs.py)：核对当前实现所用模型家族来源。官方大型教程页面本轮抓取受大小限制，未将其视为已读完的验收依据。

CPU 分离会受音频长度、线程、内存与本机负载影响；ACE 服务是否具备所需硬件、权重及任务容量需单独核查。CLI 可用、服务健康、假 HTTP 契约测试通过均不能替代真实生成与监听结果；不因本文下载或切换模型。
