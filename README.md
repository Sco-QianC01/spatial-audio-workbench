# Spatial Audio Workbench

独立的本地自动混音与空间音频工作台。导入原生分轨包，或明确选择从立体声进行本地 HDemucs 分离；测量、比较混音候选，编辑位置自动化，并导出双耳试听、AmbiX 与 7.1.4 的 12 条单声道扬声器文件。

本项目拥有独立代码、Python 环境、启动入口与输出目录，不依赖原采集系统运行。GitHub 项目名为 `Sco-QianC01/spatial-audio-workbench`。来源与第三方授权见 [PROVENANCE](docs/PROVENANCE.md)。

## 安装

需要 Python 3.12、FFmpeg，以及现有且兼容的 HDemucs 权重。Node.js 22 或 24 仅用于前端测试与静态构建；前端没有 npm 依赖，无需执行 `npm install`。

在本项目根目录创建 `.venv`。不要使用 `--system-site-packages`，也不要设置 `PYTHONPATH` 借用其他项目。网关、混音和分离工作进程使用同一个项目环境。

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r gateway/requirements-spatial.txt
.\.venv\Scripts\python.exe -m pip install torch==2.9.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cpu
```

Linux：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r gateway/requirements-spatial.txt
.venv/bin/python -m pip install torch==2.9.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cpu
```

macOS 使用 PyPI，不使用 Windows/Linux 的 CPU wheel 索引：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r gateway/requirements-spatial.txt
.venv/bin/python -m pip install torch==2.9.1 torchaudio==2.9.1
```

另行安装适合操作系统的 FFmpeg，放入 PATH 或启动时传入其绝对路径。项目不自动下载模型，也不包含权重、音频或个人工程。已有权重可保持在项目外，只读加载；权重来源、许可与适用性由使用者确认。

## 启动

Linux/macOS：

```bash
.venv/bin/python run.py \
  --weights /absolute/path/model.pt \
  --output /absolute/path/projects \
  --downloads /absolute/path/downloads
```

Windows：

```powershell
.\.venv\Scripts\python.exe run.py --weights "D:\Models\model.pt" --output "D:\SpatialAudio\projects" --downloads "D:\SpatialAudio\downloads"
```

可选传入 `--ffmpeg /absolute/path/to/ffmpeg`（Windows 使用 exe 绝对路径）。首次在以上命令后添加 `--configure`，只保存本机路径到忽略提交的 `.runtime/config.json`。以后在 Windows 运行 `Start-Windows.ps1`，在 macOS 双击 `Start-macOS.command` 即可复用这些路径；也可以直接运行 `.venv/bin/python run.py`。本地配置不随 GitHub 克隆分发，换电脑需要重新指定路径。

默认在前台启动服务，浏览器访问 `http://127.0.0.1:8773/`。需要后台启动时添加 `--background`，入口最多等待 45 秒确认就绪；重复启动会复用同一仓库的就绪服务，端口属于其他程序或旧仓库时会报错，不会终止它。

`--output` 保存用户工程；`--downloads` 保存显式下载的原件与来源收据。为它们选择独立的绝对路径，避免混入 Git 工作树。启动不会自动生成音乐或提交线上任务。可选线上生成依赖既有研究服务及相应访问条件；离线导入与本地制作不要求连接该服务。

## 制作流程

1. 优先导入 ACE 原生分轨 ZIP。生成器要求真实支持 Base/lego 的服务；路线标签不证明当前服务具备该能力。详见 [分轨路线](docs/SPATIAL-STEM-ROUTES.md)。
2. 已有立体声可改选“立体声 · 本地自动分离”，导入后明确点击分离。线上结果、重载或历史回执不会自行触发分离。
3. 逐轨检查后分析音频，比较原版与候选；混音候选需明确应用。编辑增益、宽度、方位、仰角及关键帧。
4. 选择 7.1.4 并渲染扬声器通道，下载 12 条 mono WAV 与路由记录。双耳试听需要浏览器通用 HRTF。

工作台以中性编号标记声部。分离输出是模型估计，原生生成也须检查同步、串音和音乐质量。输出不是 Atmos ADM/BWF 母版，不包含 Dolby 编码或认证。

详见 [工作台合同](docs/SPATIAL-AUDIO.md)、[内容适配混音](docs/SPATIAL-CONTENT-MIX.md)、[棚内通道合同](docs/SPATIAL-STUDIO-HANDOFF.md)与[三维视图](docs/SPATIAL-VIEW.md)。保留的详细文档包含来源项目历史记录；历史机器路径、样例与验收不能视为当前安装结果。

## 检查与构建

```bash
npm test
npm run build
.venv/bin/python tools/run-gateway-tests.py --suite spatial
```

Windows 的 Python 检查使用 `.\.venv\Scripts\python.exe`。CI 在 Node 22/24 上测试并构建静态前端，在 Ubuntu/Python 3.12 独立虚拟环境中运行空间音频测试。

`dist/` 包含静态前端、公共资源与文档，根页面相对跳转至 `./spatial-audio/`。静态预览需从 `dist/` 作为站点根提供服务；完整导入、混音和渲染还需要同源本地后端。静态构建不发布站点，也不配置 GitHub Pages。

## 验收边界

自动测试和文件检查不等于音质、个体 HRTF、真实音箱路由或房间校准验收。macOS 原生安装、实际声卡和棚内 7.1.4 回放仍需在目标机器验证。请保留原始音频，候选经全程听辨后再用于正式交付。
