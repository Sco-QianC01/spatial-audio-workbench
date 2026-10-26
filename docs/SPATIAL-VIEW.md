# 三维对象视图 · 0.6.1

入口：`http://127.0.0.1:8773/spatial-audio/`。本视图接入本地空间混音工作台；工作路线仍为立体声下载、本地分轨、混音比较、场景设计和渲染。

## 设计与交互

- 中央听者采用连续的哑光头部网格，耳部、鼻梁、下颌自然衔接；裁去肩部与多余颈部并封底，去除皮肤贴图和高光。来源为 Lee Perry-Smith / Infinite-Realities 的 CC BY 3.0 头部扫描，转换、缩放及前向重定向后随项目保存在 `spatial-audio/assets/listener-head.json`（约633 KiB）；原始授权、来源与修改说明见同目录 `LISTENER-LICENSE.txt`。运行时无需联网或新增建模软件。它只提供朝向参照，不用于个体声学校准。
- 标签避让头部投影区域，声源与扬声器标签保留定位引线；避让不改变对象坐标。头部资源加载失败时保留前向标记与场景编辑入口。
- 空白拖动旋转观察角度，滚轮缩放；透视、俯视、正视和侧视可复位相机。点击对象或其标签选择声部，保留播放位置与播放状态；静音对象仍保留选择入口。
- 对象球位置、立体声宽度弧、关键帧路径来自同一音频场景。选中状态与静音状态不被表现成信号电平。播放期间按照音频 `currentTime` 刷新，暂停/隐藏页面不运行持续动画。
- 三维选中轨道显示2.2 CSS像素实线和关键帧节点，未选轨迹减弱并采用虚线。点击节点选择精确时间，点击路径只移动播放头；悬停显示分秒与毫秒。头部遮挡的路径不抢占可见候选，正视/侧视中投影重合的方向文字优先显示靠近相机的一项。轨迹采样包含所有真实关键帧，包括相隔1ms的节点。
- 四轨时间轴显示方位实线与仰角虚线；每轨2至32个关键帧，选点后用“选中关键帧 / 秒”精修时间。中间点按毫秒取整、相隔至少1ms，首尾时间固定，删除仅移除中间点。跨±180°图线分段，不画跨全图假跳跃。
- 精确修改使用右侧角度/宽度/增益和展开后的二维定位；方向键可在二维图调整位置。相机旋转不会改变音频或使已有渲染失效。
- 无WebGL时自动展开二维编辑并显示实际故障；没有空白的假三维图。中文字库、Three.js、OrbitControls均使用仓库既有离线资源；本轮新增同版本r180官方Line2系列宽线模块，MIT许可、来源与适配见[开源记录](SPATIAL-OSS-INTERACTION.md)。

## 0.6.1 选择、调度与导出状态

普通声部名称与时间线轨道名称现在和三维对象选择一致：只改变选轨，保留当前播放位置和播放状态。点击真实关键帧节点仍会定位声音；轨迹空白处按点击时间定位。

浏览器HRTF位置调度合并50ms网格、全部真实关键帧和终点，避免短关键帧运动被固定网格遗漏。节点之间仍使用原有WebAudio坐标线性插值，不声称与扬声器渲染逐采样一致。

布局修改会清除旧导出，未保存场景的保存请求失败也不会重新挂回旧布局链接。后端不将损坏或不完整缓存视为完成，保留旧目录后重建；坏双耳发布只隔离双耳链接，健康扬声器文件仍可下载。恢复和渲染完成时显示`artifactWarnings`及重新渲染提示。文件完整性和界面行为检查不等同物理棚内定位或听感验收。

## 0.6.0 7.1.4扩展

布局选择器现在同步音频渲染与三维扬声器标记。7.1.4显示11个有方向的标记：7个平面与4个上方；LFE仍不虚构位置。切换清除过期导出，纯扬声器导出不经过HRTF。详见[棚内逐通道交付](SPATIAL-STUDIO-HANDOFF.md)。下文5.1.2几何与旧验收记录继续适用于旧布局。

## 坐标与格式

方位0°前、+90°左、−90°右、±180°后，正仰角朝上。三维视图与 HRTF 使用同一 `coordinates.mjs`：WebAudio x向右、y向上、z向后；FOA 编码保持 ACN W,Y,Z,X / SN3D 的另一套轴约定，没有改变编码公式。

声源始终在单位方向球上，方盒提供方向参照。此版本没有物理距离、房间尺寸/反射、头部追踪、实测音箱坐标或个体耳模。7个扬声器标记采用名义5.1.2方向：L/R为±30°、C为0°、Ls/Rs为±110°（均水平）；Tml/Tmr为方位±90°、仰角80°的Top Middle。LFE默认静音，无方向坐标，不在三维视图虚构位置。不能将方盒当成房间声学仿真，不能将宽度当成物理大小。

当前导出包括浏览器通用HRTF双耳WAV、FOA AmbiX、场景JSON，以及48kHz FLOAT的5.1.2八声道WAV/八个mono声道。顺序为L/R/C/LFE/Ls/Rs/Tml/Tmr；WAV无speaker mask，必须按surround.json或mono文件明确映射，不能猜为7.1。LFE静音，未把bass轨当LFE。视觉参考不构成 Dolby Atmos Renderer、ADM/BW64 母版或 IOSONO 对接验收。

## 设计建议与验收边界

本轮实际通过已登录Edge取得Gemini的设计建议，并采纳其DAW布局、简化listener及自动化数字精修方向。
Gemini建议中的Ltf/Rtf命名和LFE方位定位不符合本项目Top Middle/静音LFE合同，未采纳；
该咨询不构成Gemini技术验收、模型生成验收或Dolby认证。实际坐标与文件以代码、映射清单和测试为准。
Mac设备与物理5.1.2系统仍未验收。

## 验收入口

```bash
node --test tests/spatial-view.test.mjs tests/spatial-automation.test.mjs tests/spatial-trajectory.test.mjs
npm test
npm run build
.venv/bin/python tools/run-gateway-tests.py --suite spatial
```

下文为来源项目的历史浏览器验收记录；其中旧机器路径、项目ID、验证脚本与证据目录不随本独立仓库分发。当前安装请先运行上述测试，再在本机实际检查播放、拖动、跳转、视角与多尺寸显示。

0.4.1重做已通过3项坐标/扬声器布局Node检查；独立无桌面Chromium加载已有四轨项目，
检查7个扬声器标签、四视角及1600px/390px显示，无页面异常、无API写入。
这与旧版完整浏览器脚本验收范围不同，不以截图替代实际播放、HRTF或实体系统校准。

## 参考

- [Apple：3D Object Viewer](https://support.apple.com/en-gb/guide/logicpro/lgcp108e7aac/mac)：虚拟听音室、头部、对象球和视角控制。
- [Apple：3D Object Panner 参数](https://support.apple.com/en-gb/guide/logicpro/lgcpa01bed87/mac)：参考对象定位的交互含义，未直接复制其XYZ约定。
- [Dolby Renderer v3.4 Release Notes](https://download.dolby.com/AtmosProdSuite/Dolby_Atmos_Renderer_v3.4_Release_Notes.pdf)：历史版本Person/Theater的高度参照差异，仅作耳平面设计依据。
- [Anthropic frontend-design](https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md)：依据领域建立界面层级，设计后实际浏览器复核。


## 2026-10-25 交互补齐验收

- 18项坐标、轨迹采样/拾取、自动化检查与本地静态路由检查通过。
- `tools/verify-spatial-interaction.py` 在已有四轨项目中验证节点精确选择、路径插值定位、选轨不跳时间且保留播放、悬停与点击一致、播放后清除过期提示和相机预设保留；未写入API，保存场景哈希与音频地址未变。
- `tools/verify-spatial-view.py` 验证1920×1080、1366×768、1280×720、768×1024、390×844、320×800、2560×1440布局，以及实际音频解码/播放进度、键盘编辑、WebGL丢失/恢复与缺少头部资源后的编辑可用性。证据目录为 `artifacts/spatial-interaction-oss-20261025/`。
- 已保存的耳机预览用于验证播放行为，本轮未重新分轨、混音或渲染，不将界面改善称为音质改善；Mac真机、实体扬声器与Dolby Renderer仍待分别验收。
