# .prproj 实测解析结果

> 样本: 荷花.prproj (gzip 432KB, 解压 1.37MB)
> Premiere Version: Project ObjectID Version="45" (PR 25.x)
> 解析日期: 2026-06-04

---

## 1. 文件结构确认

```xml
<PremiereData Version="3">
  ├── 1742 个顶层元素
  ├── Sequence[3] (含 2 个空/临时 + 1 个主序列)
  ├── MasterClip[21] (全部为 ObjectURef 引用)
  ├── ClipProjectItem[21]
  ├── ClipLoggingInfo[21]
  ├── VideoClip[29], AudioClip[22]
  ├── VideoFilterComponent[14]  ← Lumetri 数据
  ├── VideoComponentChain[10]
  ├── VideoClipTrackItem[9]
  ├── ArbVideoComponentParam[216]
  ├── VideoComponentParam[999]  ← 调色参数详情
  ├── AudioComponentParam[14]
  ├── AudioFader[5]
  └── Media[20] (仅 ObjectURef, 无子元素/无 FilePath)
```

---

## 2. 素材清单 (从 ClipLoggingInfo 提取)

| 类型 | 名称 | 内部帧率 |
|------|------|---------|
| Video (序) | 序列 01 | 8475667200 (29.97fps) |
| Audio | DJI_...0217_D.MP4 | 4237833600 (59.94fps) |
| Audio | DJI_...0219_D.MP4 | 4237833600 |
| Audio | DJI_...0223_D.MP4 | 4237833600 |
| Audio | DJI_...0226_D.MP4 | 4237833600 |
| Audio | DJI_...0227_D.MP4 | 4237833600 |
| Audio | DJI_...0229_D.MP4 | 4237833600 |
| Audio | DJI_...0231_D.MP4 | 4237833600 |
| Audio | DJI_...0235_D.MP4 | 4237833600 |
| Audio | DJI_...0237_D.MP4 | 4237833600 |
| Audio | DJI_...0241_D.MP4 | 4237833600 |
| Audio | MVI_1725.MOV | 8475667200 |
| Audio | MVI_1732.MOV | 8475667200 |
| Audio | MVI_1736.MOV | 8475667200 |
| Audio | MVI_1740.MOV | 8475667200 |
| Audio | MVI_1742.MOV | 8475667200 |
| Audio | MVI_1743.MOV | 8475667200 |
| Audio | MVI_1746.MOV | 8475667200 |
| Audio | MVI_1747.MOV | 8475667200 |
| Audio | MVI_1748.MOV | 8475667200 |
| Audio | 蛙仔听音乐.aiff | 5760000 |

**注意**: Media 元素只有 ObjectURef 引用，无 FilePath 子元素——素材路径在 .prproj 中不是直接嵌入的。

---

## 3. 时间线片段 (视频轨道 V1)

| 序号 | 素材 | 时间范围 (sec) | InPoint (ticks) | OutPoint (ticks) |
|------|------|--------------|----------------|-----------------|
| 1 | DJI_...0237_D.MP4 | 0 → 3596 | 593296704000 | 1610376768000 |
| 2 | DJI_...0217_D.MP4 | 3596 → 7522 | 0 | 1110312403200 |
| 3 | MVI_1725.MOV | 7522 → 11928 | 932323392000 | 2178246470400 |
| 4 | MVI_1747.MOV | 11928 → 15884 | 0 | 1118788070400 |
| 5 | MVI_1736.MOV | 15884 → 20470 | 0 | 1296777081600 |
| 6 | MVI_1732.MOV | 20470 → 24635 | 0 | 1178117740800 |
| 7 | DJI_...0229_D.MP4 | 24635 → 29670 | 0 | 1423912089600 |
| 8 | DJI_...0226_D.MP4 | 29670 → 34376 | 0 | 1330679750400 |
| 9 | MVI_1746.MOV | 34376 → 38601 | 0 | 1195069075200 |

**音频轨道**: 4 个轨道 (V1 + A1-A3)

---

## 4. Transform / Scale 数据

**结论: 这些 clip 的 Transform 都是默认值。**

VideoComponentChain 377（第一个 VideoClipTrackItem 的组件链）:
```xml
<DefaultMotion>true</DefaultMotion>
<DefaultOpacity>true</DefaultOpacity>
```

`DefaultMotion=true` = Scale 100%, Position center, Rotation 0。
没有任何 `VideoComponentParam` 包含 Scale/Position/Rotation 数据。

**这证实**: .prproj 和 PR FCP7 XML 给出的 Scale 信息**完全一致**——都显示这些 clip 是默认 Scale 100%。

### .prproj 无法提供比 XML 更多的 Scale 信息的原因

"Scale to Frame Size" 是 PR 的**显示策略**，不存储在工程文件的 Transform 参数中。它只在 PR 的显示引擎中生效，export FCP7 XML 时丢失，.prproj 的 Transform 存储中也不存在。

**唯一能推算正确 Scale 的方法**: 比较源素材分辨率 vs 时间线分辨率（已记录在 XML 的 `<file>/<media>/<video>/<samplecharacteristics>` 中）。

---

## 5. Lumetri 调色数据（.prproj 独有结构）

每个 VideoFilterComponent 包含完整的 Lumetri 面板参数（~130 个参数/组件）。

### Clip 1 (DJI_0237) Lumetri 参数

| 面板 | 参数名 (中文) | 值 |
|------|-------------|-----|
| 基本校正 | 色温 | -0.818 |
| 基本校正 | 色彩 | 4.20 |
| 基本校正 | 曝光 | 0.522 |
| 基本校正 | 对比度 | 9.50 |
| 基本校正 | 高光 | 3.44 |
| 基本校正 | 阴影 | -28.70 |
| 基本校正 | 白色 | -25.09 |
| 基本校正 | 黑色 | -32.09 |
| 基本校正 | 饱和度 | 99.27 |
| 创意 | 强度 | 100 |
| 创意 | 淡化胶片 | 0 |
| 曲线 | HDR 范围 | 100 |
| 优化 | 降噪 | 0 |
| 优化 | 模糊 | 0 |
| 更正 | 色温 | 0 |
| 更正 | 色彩 | 0 |
| 更正 | 对比度 | 0 |
| 更正 | 锐化 | 0 |
| 更正 | 饱和度 | 100 |
| 晕影 | 数量 | 0 |

### Clip 2 (DJI_0217) Lumetri 参数 — 不同值

| 参数 | Clip 1 | Clip 2 |
|------|:------:|:------:|
| 色温 | -0.818 | **0.269** |
| 色彩 | 4.20 | **1.12** |
| 曝光 | 0.522 | **0.166** |
| 对比度 | 9.50 | **-12.82** |
| 高光 | 3.44 | **3.81** |
| 阴影 | -28.70 | **-9.07** |
| 白色 | -25.09 | **-11.62** |
| 黑色 | -32.09 | **-1.05** |
| 饱和度 | 99.27 | **121.48** |

**每个 clip 有独立的 Lumetri 参数**。这和 PR FCP7 XML 中每个 clipitem 一个 `<filter effectid="Lumetri">` 是 1:1 对应的。

### Lumetri 参数 → 达芬奇 Color 映射（基于 .prproj 实测数据）

| PR (.prproj 中文参数名) | 达芬奇对应 | 数据源 |
|------------------------|-----------|--------|
| 色温 (Temperature) | Temperature 色轮 | StartKey 第二值 |
| 色彩 (Tint) | Tint | StartKey 第二值 |
| 曝光 (Exposure) | Exposure/Offset | StartKey 第二值 |
| 对比度 (Contrast) | Contrast | StartKey 第二值 |
| 高光 (Highlights) | Highlights 色轮 | StartKey 第二值 |
| 阴影 (Shadows) | Shadows/Lift 色轮 | StartKey 第二值 |
| 白色 (Whites) | Gain 色轮 | StartKey 第二值 |
| 黑色 (Blacks) | Lift 色轮 | StartKey 第二值 |
| 饱和度 (Saturation) | Saturation | StartKey 第二值 |

**参数值格式**: `StartKeyframe = ticks,value,...`
其中 `ticks=-91445760000000000` 是时间点（0时刻），`value` 是实际参数值。

---

## 6. StartKeyframe 格式说明

```
-91445760000000000,-0.81787109375,0,0,0,0,0,0
^                  ^             ^  ^  ^  ^  ^  ^
时间戳(pproTicks)   参数值        额外字段(常为0)
```

- 第 1 字段: pproTicks（Adobe 内部时间单位，-91445760000000000 = 时间线起始 0）
- 第 2 字段: **实际参数值**（浮点数，单位取决于参数类型）
- 第 3-8 字段: 通常为 0，用于贝塞尔曲线控制点或扩展数据

---

## 7. 对项目的结论

### .prproj 入口的优势（相比 PR FCP7 XML）

| 维度 | PR FCP7 XML | .prproj |
|------|:----------:|:------:|
| Lumetri 参数名 | **中英文混合，嵌套在 base64 blob 中** | **结构化中文参数名，值直接可读** |
| Lumetri 解析难度 | 需 base64 解码 → 解析内嵌 XML | 直接读 StartKeyframe 值 |
| 每 clip 的独立参数 | ✅ (base64 blob) | ✅ (独立 StartKeyframe) |
| Transform 数据 | ✅ (Basic Motion filter) | ⚠️ DefaultMotion=true 即 100% |
| Scale to Frame Size | ❌ 丢失 | ❌ 不存在 |
| 素材路径 | ✅ file:// pathurl | ❌ Media 无 FilePath 子元素 |
| InPoint/OutPoint | ✅ | ✅ (ticks 格式) |
| 速度 | PlaybackSpeed | PlaybackSpeed |
| 音频 | ✅ Level+Balance | ✅ AudioComponentParam |

### 建议的双入口策略

**入口 A (PR FCP7 XML)**: 用于素材路径、InPoint/OutPoint、Transform、多轨道结构、link group、时序精确数据。

**入口 B (.prproj)**: 用于提取**可读的 Lumetri 调色参数**（直接解析 StartKeyframe，无需 base64 解码）。

**混合模式**: 同时读取同一时间线的 XML + .prproj，XML 作为基础数据源，.prproj 作为 Lumetri 参数增强源。

---

*解析日期: 2026-06-04*
*解析工具: Python 3 + xml.etree.ElementTree*
