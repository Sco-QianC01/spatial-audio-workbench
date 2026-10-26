# 空间声场编辑器：开源交互参考与实际引入记录

记录日期：2026-10-25。范围为声源方向、立体声宽度、轨迹编辑与可视线条；本记录不构成音质、物理扬声器或 Dolby 工作流验收。

## 来源与采用状态

| 项目与主源 | 本轮实际读取 | 许可 | 本地采用状态 |
| --- | --- | --- | --- |
| [IEM Plugin Suite](https://git.iem.at/audioplugins/IEMPluginSuite) | `resources/customComponents/SpherePanner.h`、`StereoEncoder/Source/PluginProcessor.cpp`、`resources/Conversions.h` | 源文件声明 GPL-3.0-or-later | 仅阅读交互和数学实现；未复制 GPL 源码、未引入插件或替换 DSP |
| [JSAmbisonics](https://github.com/polarch/JSAmbisonics) | `src/ambi-monoEncoder.js`、`src/ambi-sceneRotator.js`、`src/ambi-converters.js`、`README.md`、`LICENSE` | BSD-3-Clause | 仅阅读编码、旋转与归一化转换；未引入运行时 |
| [Resonance Audio Web SDK](https://github.com/resonance-audio/resonance-audio-web-sdk) | `src/source.js`、`src/encoder.js`、`src/utils.js`、`LICENSE` | Apache-2.0 | 仅阅读位置接口、坐标与声源扩散；未引入运行时 |
| [Three.js r180](https://github.com/mrdoob/three.js/tree/r180/examples/jsm/lines) | 下列五个 lines addon、官方线材质文档及 spline editor 示例 | MIT | 五个 addon 已实际放入本地并由 `scene-view.mjs` 使用；示例仅参考，未复制进运行时 |

本轮前三项阅读的是各主源的 `master` 文件，未锁定完整提交哈希；它们是参考阅读记录，不能作为可重建依赖锁文件。实际引入的 Three.js 文件锁定 `r180`，原文与本地 SHA-256 见[来源清单](SPATIAL-OSS-SOURCES.json)。

## 源码中可核对的交互与坐标

**IEM。** [SpherePanner.h](https://git.iem.at/audioplugins/IEMPluginSuite/-/blob/master/resources/customComponents/SpherePanner.h) 将球面方向映射到二维圆盘，区分方位与仰角操作，并包含立体声宽度手柄。[StereoEncoder 的处理器](https://git.iem.at/audioplugins/IEMPluginSuite/-/blob/master/StereoEncoder/Source/PluginProcessor.cpp) 用四元数推导左右输入方向，并在处理块之间渐变编码增益。[Conversions.h](https://git.iem.at/audioplugins/IEMPluginSuite/-/blob/master/resources/Conversions.h) 使用 `x=cos(el)cos(az), y=cos(el)sin(az), z=sin(el)`，即前、左、上坐标。可借鉴单轴约束、明确的宽度手柄和连续参数更新；这些源码并未提供本项目的 2–32 关键帧时间轴。

**JSAmbisonics。** [单声道编码器](https://github.com/polarch/JSAmbisonics/blob/master/src/ambi-monoEncoder.js) 将角度转换为球谐系数；[sceneRotator](https://github.com/polarch/JSAmbisonics/blob/master/src/ambi-sceneRotator.js) 将整个声场的 yaw/pitch/roll 与单个对象位置分开。[转换器](https://github.com/polarch/JSAmbisonics/blob/master/src/ambi-converters.js) 明确实现 SN3D 与 N3D 的阶次缩放。其[默认 ACN/N3D 约定](https://github.com/polarch/JSAmbisonics/blob/master/README.md) 与本地 FOA ACN/SN3D 不同；比较系数前必须转换归一化，不能直接以振幅差判定错误。

**Resonance Audio。** [source.js](https://github.com/resonance-audio/resonance-audio-web-sdk/blob/master/src/source.js) 分开管理位置、朝向、增益及相对听者方向；[utils.js](https://github.com/resonance-audio/resonance-audio-web-sdk/blob/master/src/utils.js) 的默认前方为 `[0,0,-1]`、上方为 `[0,1,0]`。[encoder.js](https://github.com/resonance-audio/resonance-audio-web-sdk/blob/master/src/encoder.js) 的 `sourceWidth` 调整单声源球谐扩散权重，0° 为点源、360° 为全向。这个含义与本地左右声道按中心方位分离的立体声宽度不同，不能只沿用参数名称就替换算法。

本地坐标契约继续为方位 0° 在前、+90° 在左、−90° 在右，声源位于单位球。映射到 Three.js/WebAudio 风格坐标时使用 `(-sin(az)cos(el), sin(el), -cos(az)cos(el))`；外部项目的坐标分量、角度符号与听者朝向需要显式转换。

## Three.js：已实际引入的五个 addon

本地目录为 `public/vendor/three/lines/`。入口已由 `spatial-audio/scene-view.mjs` 显式导入 `Line2`、`LineGeometry`、`LineMaterial`；其余两个文件为这些 addon 的依赖。

| 本地文件 | 固定的官方原文 |
| --- | --- |
| [Line2.js](../public/vendor/three/lines/Line2.js) | [r180/Line2.js](https://raw.githubusercontent.com/mrdoob/three.js/r180/examples/jsm/lines/Line2.js) |
| [LineSegments2.js](../public/vendor/three/lines/LineSegments2.js) | [r180/LineSegments2.js](https://raw.githubusercontent.com/mrdoob/three.js/r180/examples/jsm/lines/LineSegments2.js) |
| [LineGeometry.js](../public/vendor/three/lines/LineGeometry.js) | [r180/LineGeometry.js](https://raw.githubusercontent.com/mrdoob/three.js/r180/examples/jsm/lines/LineGeometry.js) |
| [LineSegmentsGeometry.js](../public/vendor/three/lines/LineSegmentsGeometry.js) | [r180/LineSegmentsGeometry.js](https://raw.githubusercontent.com/mrdoob/three.js/r180/examples/jsm/lines/LineSegmentsGeometry.js) |
| [LineMaterial.js](../public/vendor/three/lines/LineMaterial.js) | [r180/LineMaterial.js](https://raw.githubusercontent.com/mrdoob/three.js/r180/examples/jsm/lines/LineMaterial.js) |

唯一代码适配是将存在的 `from 'three'` 改为 `from '../three.module.min.js'`，复用本地同版本模块。`Line2.js` 和 `LineGeometry.js` 不需要此适配，与原文哈希相同；另外三个文件的原文与本地哈希不同。清单记录了每个原文 URL、HTTP 状态、字节数及两份 SHA-256。分发继续保留 Three.js 的 MIT 许可与版权声明，许可原文见 [r180 LICENSE](https://github.com/mrdoob/three.js/blob/r180/LICENSE)。

采用原因有官方依据：[LineBasicMaterial](https://threejs.org/docs/pages/LineBasicMaterial.html) 的 `linewidth` 在 WebGL/WebGPU 中被忽略，实际保持一像素；[Line2](https://threejs.org/docs/pages/Line2.html) 与 [LineMaterial](https://threejs.org/docs/pages/LineMaterial.html) 支持可控宽度。`worldUnits=false` 时宽度按 CSS 像素计；分辨率应跟随实际视口，不能以扩大原生 `LineBasicMaterial.linewidth` 代替。

[r180 spline editor 示例](https://raw.githubusercontent.com/mrdoob/three.js/r180/examples/webgl_geometry_spline_editor.html) 实际读取了点选、控制点、OrbitControls/TransformControls 配合及曲线更新方式。它未复制进本地运行时；示例中的 CatmullRom 曲线也未替换本地关键帧的最短角路径、跨 ±180° 规则或时间端点约束。

## 对本项目的三条直接建议与边界

1. 对象拖动保留明确的方位/仰角约束与数值精修，宽度使用独立可辨识控件；借鉴 IEM 的操作分工，保持本地单位球和角度契约。
2. 轨迹显示继续对应本地 2–32 关键帧与最短角插值；Three.js addon 负责可读线宽，样条示例只用于交互参考，视觉曲线不能改变实际音频轨迹。
3. 将立体声方向间距、单声源扩散和场景旋转作为不同含义处理。任何未来编码器替换先核对 ACN 顺序、SN3D/N3D、坐标与增益，再进行数值和听音验证。

本轮没有复制 IEM GPL 源码，没有替换空间音频 DSP，没有据此宣称声像或音效通过。FOA 是一阶 Ambisonics 表示，不是 ADM 文件或 Dolby Atmos 母版。本文仅核对来源、许可声明、适配记录与调用关系；物理 5.1.2、耳机听感、macOS 设备及 Dolby Renderer/ADM 工作流仍不在本次验收范围。
