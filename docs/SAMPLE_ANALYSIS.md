# 测试样本分析：PR FCP7 XML vs .prproj

> 样本: 荷花项目 — 竖屏 (1080×1920), 29.97fps NTSC DF
> 分析日期: 2026-06-04

---

## 1. PR FCP7 XML 完整结构

```
xmeml version="4"                              ← C1: 应为 version="5"
sequence id="sequence-1"
  ├─ PreviewFrameSizeWidth="1080"              ← 竖屏: 1080×1920
  ├─ PreviewFrameSizeHeight="1920"
  ├─ duration: 1288 frames (~43 sec @29.97)
  ├─ rate: timebase=30, ntsc=TRUE
  │
  ├─ media/video/format                        ✅ 有 format (DaVinci OK)
  │   └─ samplecharacteristics
  │       └─ width=1080, height=1920           ← C0: width/height 与 PreviewFrameSize 一致
  │
  ├─ media/video/track[1] (V1, active)         9 clipitems
  ├─ media/video/track[2] (V2, empty)          仅 enabled+locked
  ├─ media/video/track[3] (V3, empty)          仅 enabled+locked
  │
  ├─ media/audio/format                        ✅ 有 format
  ├─ media/audio/track[1] (A1, stereo L)       1 clipitem (music)
  ├─ media/audio/track[2] (A2, stereo R)       1 clipitem (music, same masterclip)
  ├─ media/audio/track[3] (A3, empty)
  ├─ media/audio/track[4] (A4, empty)
  └─ media/audio/track[5] (A5, empty)

  timecode: DF, 00;00;00;00
  marker[1]: in=116
```

---

## 2. 素材分辨率与 Scale 分析

| Clip | Name | Source Res | Scale | Rotation | Frame Range | Lumetri |
|------|------|-----------|:-----:|:--------:|------------|:------:|
| 1 | DJI_...0237_D.MP4 | 1728×3072 | 100% | 270° | 0–120 | ✅ massive |
| 2 | DJI_...0217_D.MP4 | 1728×3072 | — | — | 120–251 | ✅ |
| 3 | MVI_1725.MOV | 1920×1080 | — | — | 251–398 | ✅ |
| 4 | MVI_1747.MOV | 1920×1080 | — | — | ? | ✅ |
| 5 | MVI_1736.MOV | 1920×1080 | — | — | ? | ✅ |
| 6 | MVI_1732.MOV | 1920×1080 | — | — | ? | ✅ |
| 7 | DJI_...0229_D.MP4 | 1728×3072 | — | — | ? | ✅ |
| 8 | DJI_...0226_D.MP4 | 1728×3072 | — | — | ? | ✅ |
| 9 | MVI_1746.MOV | 1920×1080 | — | — | ? | ✅ |
| 10 | 蛙仔听音乐.aiff | audio | — | — | 0–1288 | — |

### Scale 问题确认

**Clip 1**: 源 1728×3072, 时间线 1080×1920, Scale=100%, Rotation=270°

- 旋转后有效尺寸: 3072×1728 → 时间线 1080×1920
- 按宽度适配: 1080/3072 = **35.2%**
- 当前 scale=100% → 达芬奇中此 clip 会**约为预期的 2.84 倍大**
- 但有 Rotation=270° 说明用户在 PR 中转了画面，这是有意为之
- **不应自动修正** — 有 Rotation 的 clip 默认信任用户

**Clip 3/4/5/6/9 (MVI 系列)**: 源 1920×1080 (横屏), 时间线 1080×1920 (竖屏)

- 按宽度适配: 1080/1920 = **56.25%** ✨ ← 这就是 56.3% 的来源！
- 如果 Scale=100% 且无 Rotation，达芬奇中会渲染为 1920×1080 但只显示竖屏中心区域
- **应该触发 M7 Scale 修正**: fit_scale = 1080/1920 × 100 = 56.3%

**Clip 2/7/8 (DJI 系列)**: 源 1728×3072, 同 clip 1 场景
- 需要进一步确认 Rotation 值

---

## 3. 达芬奇兼容性诊断

### 已通过项 ✅

| 检查项 | 状态 |
|--------|:----:|
| `<format>` (video) | ✅ 存在 |
| `<format>` (audio) | ✅ 存在 |
| `<rate>` 含 `<ntsc>` + `<timebase>` | ✅ 全部 |
| `<sourcetrack>` 每个 clipitem | ✅ 全部 |
| `<masterclipid>` 每个 clipitem | ✅ 全部 |
| `<file>` 含 `media/video/samplecharacteristics` | ✅ 全部 |
| `media` 内 video 在 audio 前 | ✅ |
| pathurl 使用 file:// 格式 | ⚠️ `file://localhost/` (应为 `file:///`) |

### 需修正项 ❌

| ID | 问题 | 详细 |
|----|------|------|
| C0 | xmeml version="4" | 应为 "5" (FCP7 最终标准) |
| C0 | pathurl 格式 `file://localhost/` | 应统一为 `file:///` (Path.as_uri()) |
| M0 | **9 个 Lumetri 滤镜块** | 每个 clipitem 含 ~200 行 base64 blob + PR 调色参数，达芬奇静默忽略 |
| M7 | **Scale 100% 但需 56.3%** | 横屏 1920×1080 素材在竖屏 1080×1920 时间线，无 Rotation 时 scale 应为 56.25% |
| M0 | 2 个空 video track + 3 个空 audio track | 达芬奇会正常导入但多余 |
| N0 | `<displayformat>DF</displayformat>` | 在 `sequence/timecode` 中是 DF，clip/rate 是 NTSC=TRUE — 一致，OK |
| N0 | PR 专有属性 | `pproTicksIn`, `pproTicksOut`, `premiereChannelType`, `PannerName=平衡`, `TL.SQ*`, `MZ.*` — 达芬奇忽略 |
| N0 | 仅 1 个 marker | frame 116，无 name/comment — 正常 |

---

## 4. Lumetri 参数提取 (本次样本)

从 clip 1 的 Lumetri `<parameter>` 标签中提取的实际值：

| PR Lumetri 面板 | XML 参数名 | 原始值 |
|----------------|-----------|--------|
| 色温 | `parameterid=7 name="色温"` | -0.818 |
| 色彩 | `parameterid=8 name="色彩"` | 4.199 |
| 饱和度 | `parameterid=20 name="饱和度"` | 99.27 |
| 曝光 | `parameterid=11 name="曝光"` | 0.522 |
| 对比度 | `parameterid=12 name="对比度"` | 9.50 |
| 高光 | `parameterid=13 name="高光"` | 3.44 |
| 阴影 | `parameterid=14 name="阴影"` | -28.70 |
| 白色 | `parameterid=15 name="白色"` | -25.09 |
| 黑色 | `parameterid=16 name="黑色"` | -32.09 |
| 强度 | `parameterid=26 name="强度"` | 100 |
| 淡化胶片 | `parameterid=28 name="淡化胶片"` | 0 |
| LUT | `<parameterid=1 name="Blob">` | **base64 编码的 XML** (内含 LUT 路径 + BasicCorrection + Wheels + Curves + Secondary + Vignette + Sharpness) |

**LUT Blob 解 base64 后的内容**: 完整的 PR Lumetri 状态，包含：
- `BasicCorrection3`: Exposure/Blacks/Contrast/Shadows/Whites/Highlights/Saturation/Temp/Tint (全为 "D0" = 默认，说明该面板值全为 0)
- `Wheels`: DiffWhite/Shadows/Midtones/Specular/Highlights 的 X/Y/Z 分量
- `ColorCurves + ToneCurves`: Hue/Hue Hue/Lum Hue/Sat Lum/Sat Curves
- `Vignette`
- `Sharpen`/`Sharpness`
- 嵌入 LUT 路径: `C:\Users\viole\Downloads\DJI OSMO Pocket 4 D-Log to Rec.709 vivid V1.0.cube`

**注意**: XML 参数名是中文（`色温`/`色彩`/`曝光`...），说明用户用的是**中文版 Premiere Pro**。参数映射需要同时匹配中英文名称。

---

## 5. .prproj 入口的潜力 (无法实际验证，基于 PRPROJ-READER 源码推断)

如果 bash 可用，预期能从 .prproj 中提取以下**XML 丢失的数据**：

| 数据 | PR FCP7 XML | .prproj (预期) |
|------|:----------:|:------------:|
| Scale to Frame Size | ❌ 不存在 | ✅ 可推算 (compare src dims vs effect) |
| 完整的 Motion 参数 | ⚠️ 100% 缩放值存疑 | ✅ 原始值含关键帧 |
| Lumetri 完整层级 | ⚠️ 仅 base64 blob | ✅ SyntheticClip 结构 |
| 嵌套序列 | ❌ | ✅ SequenceSource |
| 原始 "Scale to Frame Size" 标记 | ❌ | ✅ (推测) |

**.prproj 实际验证需要 bash/gzip 可用时再执行。**

---

## 6. 对实现的影响

### 待确认

1. **MVI 系列 clips 的 Rotation 值** — 如果 Rotation=0 且 Scale=100%，M7 触发；如果有 Rotation，跳过
2. **Bash 可用后解析 .prproj** — 确认是否能提取 "Scale to Frame Size" 标记
3. **中文 Lumetri 参数名映射表** — search 已看到中文名（色温=Temperature, 色彩=Tint, 曝光=Exposure, 对比度=Contrast, 高光=Highlights, 阴影=Shadows, 白色=Whites, 黑色=Blacks, 饱和度=Saturation, 淡化胶片=Fade, 强度=Intensity）

### 修正优先级

1. **C0**: version="4" → "5" + pathurl `file://localhost/` → `file:///`  ← 极简，对所有 PR XML 都适用
2. **M0**: 移除 Lumetri 滤镜块 (FCP7 XML 路径)  ← 大幅缩文件体积
3. **M7**: Scale 自动适配 (仅当 100% + 无 Rotation)  ← 解决实际导入问题
4. 完成后可以开始编写核心转换器

---

*分析日期: 2026-06-04*
