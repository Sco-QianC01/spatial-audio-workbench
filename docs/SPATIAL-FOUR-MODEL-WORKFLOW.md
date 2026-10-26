# 四模型生成与本地空间音频工作流 · 0.8.0

当前入口`http://127.0.0.1:8773/spatial-audio/`；科大上游固定`http://10.100.16.56:8189`。先生成立体声，再明确选择本地自动分离；另一条ACE Base原生生成声部路线见[分轨来源与制作流程](SPATIAL-STEM-ROUTES.md)。两条路线不混用来源，不默认续接。

## 当前使用顺序

1. 展开线上四模型，按正常研究用户名流程连接；后端Token仅在内存中，不交给前端或任务记录。
2. 选择ACE XL-SFT、Shao、MiniMax或YuE并填写提示词。YuE需自己的ABC，Shao/YuE时长由模型决定。明确点击生成，等待真实成功状态及音频URL。
3. 点击结果的“下载并本地分离”。这一步才下载原件并用本地HDemucs处理；仅生成、刷新、重连和历史prepareStudio记录均不自动启动。下载保留原件、任务来源与SHA256。
4. 分离完成后逐轨试听，检查串音、瞬态、低频及相位，再做混音候选与等响度比较。中性轨道编号不保证对应某件乐器。
5. 编辑对象位置、宽度、增益与Automation，再渲染7.1.4。只有ready且具有有效导出链接才可下载12路mono PCM24/48kHz包；LFE节目默认静音，另附路由识别音。
6. HRTF仅为可选耳机试听。棚内逐路路由与物理听音独立验收；FOA/多声道WAV不能按普通立体声播放。

`POST /online/prepare`及生成参数`prepareStudio=true`现在拒绝；`POST /projects/{id}/separate`必须显式JSON `{"workflow":"separated-stereo-v1"}`。原生导入端点为`POST /native/import`，不会再分离原生声部。`GET /status`公布当前双路线。

## 历史记录说明

以下0.7.0端点表、自动续接字段及旧测试报告保留作追溯；其中/online/prepare、自动prepareStudio相关行为已被上述0.8.0策略取代，不是当前操作说明。既有项目保留可回看和导出，不会自动迁移成新路线。

## 本地端点与字段

以下路径均接在 `/api/spatial` 后；POST 使用 `Content-Type: application/json`。

| 方法与路径 | 请求 / 返回要点 |
| --- | --- |
| `GET /online/generation` | 返回 `connected`、`username`、`models`、`jobs`。模型字段为 `id`、`label`、`durationMode`、`minDuration`、`maxDuration`、`requiresScore`。 |
| `POST /online/connect` | 请求 `{username}`；返回同上状态。用户名为 1–120 字符；若有待处理任务，必须使用该任务原用户名。 |
| `POST /online/generate` | 请求 `{requestId,model,prompt,duration,seed,abcScore?,prepareStudio?}`；返回本地任务。`requestId` 为 32 位小写十六进制；`model` 为 `ace/shao/minimax/yue`；提示词 1–4000 字符；seed 为 0–2147483647 整数。YuE2 的 ABC 最长 60000 字符，须含 `X:` 与 `K:` 头部。 |
| `GET /online/jobs/{id}` | 查询同一个本地任务；需要时只向上游查询其 `remoteJobId`，不会重新提交生成。 |
| `POST /online/import` | 请求 `{audioUrl,jobId?}`。生成结果导入必须携带本地任务 `jobId`；后端校验该任务已成功且 URL 属于它的结果。手动 URL 导入可不传 jobId。返回本地项目。 |
| `POST /online/prepare` | 请求 `{jobId,audioUrl}`；复用来源下载与已有本地制作进度，返回制作项目，继续查询项目直到分轨与7.1.4渲染完成。不重新向上游提交生成。 |
| `POST /projects/{pid}/separate` | 启动本地分轨；生成结果的“导入并自动分轨”由前端接续调用。 |
| `GET /projects/{pid}` | 查询导入、分轨、场景和产物状态。 |
| `POST /projects/{pid}/render` | 按场景布局渲染本地5.1.2或7.1.4及FOA；浏览器双耳结果经 `POST /projects/{pid}/binaural` 保存。 |

任务记录含 `id`、`model`、`label`、`prompt`、`duration`、`seed`、`requestHash`、`username`、时间戳、`status`、`remoteJobId`、`audioUrls`、`error`，导入后补充 `projectId`；制作任务持久保存按结果URL索引的 `studioProjectIds`，并保留最近使用的 `studioProjectId`。Shao等模型返回多个音频时各自复用对应制作项目。`prepareStudio`为本地连续制作意图，不传给上游模型。状态为 `submitting / queued / running / succeeded / failed / submission_unknown`。成功 URL 由后端规范化为固定科大 origin 的绝对 `/audio/...` 地址，避免误指本地8773。

来源链为：本地 `requestId` → 任务 `id` → 上游 `remoteJobId` → 成功音频 URL → 原件 `source.sha256` → 本地项目。项目 `source.generation` 保存 `id`、`remoteJobId`、`model`、`label`、`prompt`、`seed`、`requestHash`。其中 `source.generation.id` 即本地任务编号，验收记录使用字段名 `localJobId`；导入请求使用字段名 `jobId`，无需重复提交整组来源字段。

## 恢复与重复提交边界

- 后端在向上游 POST 前保存本地任务和请求摘要。同一 requestId、相同参数的重试返回已有任务，不重复 POST；同一编号更换参数会被拒绝。同时只允许一个排队、运行或待确认任务。
- 重启恢复全部幂等账本；界面展示最近 100 条历史及所有活动任务。按任务串行查询，已保存的终态不会被较早查询响应覆盖。
- 前端重载读取任务历史并恢复 GET 轮询。每个活动任务的自动查询上限为 45 分钟，重载后从当前时间重新有界计时；到期可手动刷新，后续新任务另计预算。
- 网络中断、无有效提交回执或不确定的上游错误进入 `submission_unknown`，不会自动重新生成。保留原编号并刷新确认；没有上游编号的未知提交不能靠轮询凭空恢复回执，也不会悄悄创建替代任务。明确参数拒绝标为失败后，用户可以修正再提交。
- 后端重启不恢复 Token；需用原研究用户名重新连接。即使已有 queued/running 任务，未连接状态仍允许连接。提交过程中重启的记录转为 `submission_unknown`，不自动重放。
- 本地导入与分轨可能持续数分钟；完成 promise 前持续显示处理中并阻止重复导入。分轨失败可重试，不显示为本地成功。已有同任务、同 URL 的项目会复用；原件保留与 SHA256 校验不能替代听感验收。

## 0.7.0 连续制作的恢复边界

- `job.prepareStudio`保存是否自动续接，`job.studioProjectIds`按结果URL指向本地制作项目，`job.studioProjectId`保留最近使用项；项目的`studioPreparation`保存`schema: spatial-studio-workflow-v1`、`status: working / complete / error`及`stage: separating / rendering / complete`。恢复依赖后端记录，不把页面内存或localStorage当制作账本。
- 远程生成成功前关闭窗口，不保证自动发起后续本地制作；重开工作台后读取任务记录，仅对尚无本地制作项目回执的任务自动续接。已有`studioProjectIds`结果映射（或旧版`studioProjectId`）的历史任务等待手动打开，避免覆盖正在恢复的本地素材。已启动的本地制作由服务继续运行，关闭页面不等于取消后台制作；服务不可用时不能声称仍在执行。
- 缺少分轨/渲染依赖或本地制作失败时，线上成功状态与本地未完成分别显示；修复依赖后手动刷新任务状态或点击制作按钮续接。显式刷新重新检查下载入口可用性，不重发远程生成。一次自动续接失败后不无限自动重试。
- 同一来源与制作进度可复用；普通导入与7.1.4制作分别跟踪，已经导入不妨碍后续制作。下载包仅接受本地`/api/spatial/projects/<32hex>/files/...`路径。
- 12路PCM24 / 48kHz交付、routing识别音及文件检查不代表Atmos ADM/BW64母版或Dolby认证；实体接线、校准和棚内定位听感仍需单独验收。HRTF可选预览不能替代这些检查。

## 历史5.1.2验收记录


以下为2026-10-25此前5.1.2流程的历史记录，不作为0.7.0连续制作的新生成或浏览器验收证据。该次四模型各提交一次新任务，均完成真实生成、原件下载与 SHA256 核查、本地四轨分离、Automation 渲染及 5.1.2 / FOA / 双耳文件下载与播放检查：

| 模型 | 导入后实际时长 | 科大任务编号 | 本地项目 |
| --- | ---: | --- | --- |
| ACE XL-SFT | 11.68 秒 | abe3c660-24e5-475e-b77b-584e76fb644e | b3b715b8acf34407b0793f51716b4e65 |
| Shao | 86.10 秒 | f404d264-578c-40d8-87a7-472a09a84279 | 0eeee0161de74d20991211153638dba0 |
| MiniMax Music3 | 10.00 秒 | 137d15c8-e847-4154-b14a-5bbffce2b657 | da86bbca064c47e8a77ebcdc2877e8cf |
| YuE2 | 142.12 秒 | e96f530a-45d5-449b-8371-bfefd1e6cdc9 | 56c77d00f2994e7ba2ec22bdefc71be3 |

所有成品导出为 48 kHz，5.1.2 为 8 声道、FOA 为 4 声道、双耳为 2 声道；另保留 0.25 秒渲染尾音。YuE使用两小节ABC技术样例，实际输出142.12秒，该次记录不证明乐谱忠实度、精确时长控制或听感质量。旧Shao下载中断及旧YuE失败仍留在历史记录，不能归因为已被修复或用来替代该次证据。

上述历史验收以 `artifacts/spatial-four-model-workflow-20261025/*-workflow.json` 为准，结合任务状态、`complete`、`project`、`source`、`outputs`、文件 SHA256、声道数及浏览器记录判断；缺少记录或 `complete=false` 均不能当作通过。`listeningReview: not_performed` 表示未进行听感验收。

5.1.2 WAV 是声道音频，不是 Dolby Atmos ADM 母版。浏览器播放、文件解码及声道验证不代表物理扬声器、Mac 音频设备或 Dolby Renderer 已验收；本轮未验收物理 Mac 硬件。

## 0.6.0 棚内7.1.4交付

四模型既有样例已在独立项目中复用分离声部和原轨迹，生成12路PCM24/48kHz通道件。音频源、原项目与旧5.1.2产物保留。下载入口、文件合同、识别音及物理验收边界见[7.1.4棚内逐通道交付](SPATIAL-STUDIO-HANDOFF.md)。没有以耳机预览替代棚内扬声器验收。

## 0.7.0 当前联动验收

本轮通过当前UI复用四模型的既有7.1.4项目，四个下载入口HTTP 200。新提交一个ACE任务（本地`9ad79aa263bf458fb5af5ecc379e1cee`，上游`15bff017-ab0a-4be7-8864-71ffa3426f44`），页面中断恢复后自动接续下载、本地分轨与12路导出；原件11.68秒、导出11.93秒含尾音。Shao同一既有任务的第二个音频也经UI完成下载、本地分离和独立7.1.4导出，验证多结果映射；未重新生成Shao、MiniMax或YuE。

新产物通过12路mono PCM24/48kHz、帧数、场景/文件哈希及逐路识别音检查；保存冲突时编辑保留，重试先保存再切换。后端80项、分轨5项、前端216项与构建通过。实际记录位于`artifacts/spatial-online-studio-20261025/`，主要入口为`verification.json`、`shao-second-result.json`、`save-failure-recovery.json`及`audit-summary.json`；四个来源项目24文件哈希不变。

本轮没有专业听感、物理扬声器、IOSONO或Mac实机验收；这些数字结果不构成Atmos ADM或Dolby认证。

## 本地库回归 · 2026-10-25

本地库248条路径按SHA256得到151份不同文件内容，全部完成整曲解码；12个30秒片段及2首完整曲目完成实际分离、Automation和7.1.4导出，14包均通过独立文件校验。56个分离stem逐块读取通过，服务导入与规范化输入逐采样相同；248份原文件及12份元数据哈希未变。本轮未重新请求线上生成。

记录位于`artifacts/spatial-library-20261025/`。`tools/audit-local-audio-library.py`接受inventory/output做完整解码和确定性选择，`tools/verify-local-library-spatial.py`接受selection/output串行执行真实本地流程并保留checkpoint。后者先检查实际上传大小，失败/中断项目的error状态需诊断；不能据checkpoint存在声称能自动恢复所有错误。

本轮新增“历史制作不抢占本地项目”的回归测试，空间前端30项通过，实际1920×1080及390×844浏览器恢复/静音源播放通过。分离听感、原始对象还原及棚内声场仍未验收。
