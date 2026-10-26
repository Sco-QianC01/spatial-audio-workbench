# 7.1.4 棚内逐通道交付 · 0.6.0

已确认目标：7.1.4，12路节目通道。立体声生成、下载和本地分离沿用现有四模型链路；本次复用既有音频，不向学校模型重复提交。最终空间定位验收在棚内按离散通道回放完成。

## 文件合同

- `scene.speakerLayout` 接受 `5.1.2` 或 `7.1.4`。旧场景省略此字段仍按5.1.2解释，场景哈希不因渲染算法更新而改变；新布局参与场景哈希，避免混用导出。
- 7.1.4顺序为 `L R C LFE Ls Rs Lrs Rrs Ltf Rtf Ltr Rtr`。这是本工程明确声明的交换顺序，不是对任意声卡或DAW端口顺序的假设。
- 12个mono WAV：48kHz、PCM24、同一工程零点、同采样数；保留全部起始静音和共同0.25秒尾段。没有嵌入BWF绝对时间码，导入时需同时对齐同一个工程零点。
- 方位0°前、+90°左、−90°右、±180°后；正仰角朝上。名义水平角L/R ±30°、C 0°、Ls/Rs ±100°、Lrs/Rrs ±145°；上方为方位±45°/±135°、仰角45°。实际棚内几何和接线待现场确认，未按照片推算。
- 使用既有凸包VBAP，非负三通道增益以平方和归一化；低于水平面的对象投至水平面。立体声宽度按 `great-circle-width-v1` 沿中心的水平切向作正负半宽大圆偏移，高仰角和天顶不再因仅偏移方位角而收成同一点；FOA、扬声器馈送、HRTF及宽度显示使用同一几何。中心轨迹仍按最短方位弧插值。FOA与全部扬声器馈送共用一个余量衰减，不对每轨独立归一化。
- 渲染报告和HRTF记录包含几何版本。缺少当前版本的旧导出需重新渲染，旧文件保留，重建时沿现有机制隔离保存；旧页面须刷新后重新渲染HRTF，不能继续发布旧几何结果。
- `surround_7_1_4.wav` 与 `surround_channels/*.wav` 的PCM采样逐通道一致。交错WAV没有speaker mask，棚内优先使用已命名mono文件。
- 节目LFE留空，主通道保留低频；未进行低频管理或文件+10dB提升。未经过的扬声器方向可以整轨静音，必须保留全长静音文件。

## 界面和下载

在本地工作台导入/恢复项目，选择7.1.4，编辑声源与轨迹，点击“渲染棚内直出包”。此操作只需本地后端，不依赖浏览器HRTF。

`GET /api/spatial/projects/{id}` 的 `artifacts.studioPackageUrl` 指向独立 `studio.zip`，包含：

- `surround_channels/`：12个最终扬声器馈送mono文件。
- `routing_test/`：12个等长的逐路识别音文件，需同零点同时播放；每2秒轮到下一路，活动0.5秒、20ms淡入淡出。普通通道500Hz/峰值−30dBFS；LFE50Hz/峰值−40dBFS。这是数字路由识别，不是SPL/极性/房间校准。
- `routing.csv`：含文件名、方向、识别音时间；DAW轨道、硬件输出、监听端口、实际音箱及现场验收列留空待填。
- `scene.json`、`render.json`、`surround.json`、`studio.json`：可编辑场景、渲染合同、格式、来源、文件哈希和PCM检查。
- `STUDIO_README.md`：棚内导入、直通路由、增益与验收说明。

此包不含分离stem、对象源或耳机双耳文件。原有“完整工程包”和HRTF试听功能仍独立保留；5.1.2继续输出旧8路文件，不提供7.1.4专属棚内包。

三维视图随布局切换：7.1.4显示11个有方向的扬声器，LFE不虚构方向；5.1.2仍为7个标记。改变布局会清除旧下载链接，必须重新渲染。

## 棚内操作

将12个mono作为12条mono轨对齐同零点。一轨直送一个经确认的单声道硬件输出，轨道0dB，不再追加声像、上混、空间化、双耳或立体声下混。轨迹已烘焙在文件中，无需再叠加自动化。

先降低监听音量，播放识别音，依次填写routing.csv。若开启低频管理，主通道的低频可能同时使低音扬声器发声，不能据此直接判为数字串路。识别完成后停掉识别音轨，再播放音乐轨。房间电平、极性、时延、低频管理和听音位置由棚内工程师使用其规定的校准方法确认。

## 实际交付与核验

2026-10-25四个既有模型样例各复制成独立7.1.4项目，原项目JSON、原始WAV及分离stem哈希保持不变。没有重新生成、分离或修改原轨迹。

| 样例 | 新项目ID | 节目采样数（48k） | 输出秒数（含0.25秒尾段） |
|---|---|---:|---:|
| ACE XL-SFT | f736020b197942ea97e96f32198a32fd | 572640 | 11.930000 |
| Shao | 9c9c0db559354668bb7b9ec884d88b2f | 4144780 | 86.349583 |
| MiniMax Music3 | d1ad13a1de294d29bcc064050e38b772 | 491817 | 10.246188 |
| YuE2 | 68fb1eff30d648ef9c4090fd1ca4d172 | 6833697 | 142.368688 |

本轮沿用的轨迹没有经过Rrs和Rtr，故这两路节目文件全静音；LFE亦按约定静音。识别音覆盖全部12路。YuE为早前两小节ABC技术样例，其实际长时输出不证明符合谱面、风格或音乐质量。

交付目录：`Q:\常用数据\文档\chatgpt\音疗系统\交付\20261025_7.1.4棚内逐通道验收`。数字检查与截图：`artifacts/spatial-studio-714-20261025/`。

独立检查已导出的ZIP：

```sh
.venv/bin/python tools/verify-spatial-studio.py /path/to/7.1.4.zip --output verification.json
```

检查实际PCM格式/采样数/峰值/静音约定、文件及场景SHA256、逐路识别音时段/频率/电平和任意时刻至多一个活动数字通道。后端发布前另检查12个mono与交错文件逐采样相同。

`physicalAcceptance=pending`：文件检查、HTTP下载和浏览器画面不能证明物理声场已验收。此包没有ADM对象元数据，不是ADM BWF或Dolby Atmos编码/认证母版；不宣称IOSONO系统已对接。

## 依据

- [Dolby 7.1.4扬声器布局](https://www.dolby.com/siteassets/about/support/guide/setup-guides/7.1.4-overhead-speaker-placement/7_1_4_overhead_speaker_setup.pdf)：用于声道名称与空间层级参考，不等于棚内实测。
- [Dolby Atmos Home Theater Installation Guidelines](https://www.dolby.com/siteassets/technologies/dolby-atmos/atmos-installation-guidelines-121318_r3.1.pdf)：名义几何参考；家庭影院指南不替代制作棚工程规范。
- [ITU-R BS.2076-2 Audio Definition Model](https://www.itu.int/dms_pubrec/itu-r/rec/bs/R-REC-BS.2076-2-201910-S!!PDF-E.pdf)：ADM定义依据；本包未实现ADM/BWF关联元数据。
