# 来源与授权

本项目由来源项目的空间音频相关代码按明确文件清单抽取，起始提交为 `c0a2213`。独立项目名为 `Sco-QianC01/spatial-audio-workbench`；创建新的 Git 历史，不复制原混合仓库的完整历史。

抽取范围为本地空间音频后端、混音与分离工具、空间前端、对应 Node/Python 测试、详细合同和必要静态资产。不含原设备采集系统、音乐素材、模型权重、用户工程、凭据或运行环境。独立入口、环境配置、构建与 CI 在抽取后维护。

| 组成 | 本地位置 | 来源与许可 |
| --- | --- | --- |
| 项目代码与文档 | 根目录、`gateway/`、`spatial-audio/`、`tools/`、`tests/` | 来源提交 `c0a2213`；MIT，完整声明见根目录 `LICENSE`，Copyright (c) 2026 Sco-QianC01 |
| Three.js 与 OrbitControls、线条组件 | `public/vendor/three/` | Three.js r180；MIT，完整声明见该目录 `LICENSE`；组件来源记录见 `SPATIAL-OSS-SOURCES.json` |
| 听者头部网格 | `spatial-audio/assets/listener-head.json` | Lee Perry-Smith 的 *Infinite, 3D Head Scan*，来自 Three.js r180 的 LeePerrySmith 示例；CC BY 3.0，完整归属见 `spatial-audio/assets/LISTENER-LICENSE.txt` |
| 中文字体 | `public/vendor/fonts/NotoSansSC.ttf` | Noto Sans SC；SIL Open Font License 1.1，保留完整声明于 `public/vendor/fonts/OFL-NotoSansSC.txt` |

头部网格已移除纹理、裁切并闭合下颈部、重新居中、缩放并旋转至 -Z 朝前。它只提供方向视觉参照，不是个体化声学头模。原始 GLB SHA256 与上游地址保存在随附归属文件。

第三方资产适用各自许可，项目 MIT 不重新许可它们。分发时须保留相应声明。HDemucs、PyTorch/torchaudio 和可选外部生成服务不作为权重或服务副本分发；用户应另行核对其模型、依赖及生成素材的许可。

保留的详细文档含来源项目历史验收记录，不意味着新仓库、目标操作系统或用户硬件已经完成同样验收。独立项目的本轮结果以实际测试及后续发布记录为准。
