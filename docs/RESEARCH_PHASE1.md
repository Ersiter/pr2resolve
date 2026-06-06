# Phase 1 研究：PR FCP7 XML ↔ DaVinci Resolve 兼容性问题

> 研究日期: 2026-06-04
> 数据来源: Adobe 社区论坛、Creative COW 论坛、Blackmagic 论坛、GitHub 开源项目

---

## 1. 核心发现：Scale/Fit 问题

### 1.1 "Scale to Frame Size" — 数据彻底丢失

这是最重要的发现。Premiere Pro 的 **"Scale to Frame Size"**（右键→缩放为帧大小）**不生成任何 FCP7 XML 变换数据**。

在 Premiere 内部，"Scale to Frame Size"是独立的内部缩放策略，不走 Motion/Transform 管线。PR 导出 FCP7 XML 时：

- 用 Motion 面板缩放 → XML 含 `<filter><effect effectid="basic"><parameter name="Scale">` 数据
- 用 "Scale to Frame Size" → XML **完全没有任何缩放信息**，clip 显示 scale=100%

**这解释了 100%→56.3% 的问题**：源素材 3840×2160 在 2160×3840 竖屏时间线中，PR 内部通过"Scale to Frame Size"自动缩小到适配宽度，但 XML 导出时这个信息完全丢失。DaVinci 按 100% 渲染，素材比预期大。

**修正策略**：
- 无法从 XML 恢复"Scale to Frame Size"的缩放值（数据不存在）
- 可以通过源素材分辨率 × 时间线分辨率推算正确的 fit scale
- 仅当当前 scale=100% 且分辨率不匹配时触发，`fit_scale = tl_w / src_w × 100`

### 1.2 Motion 面板缩放 — "2x 缩小"问题

即使使用 Motion 面板手动缩放（有 transform 数据），DaVinci 导入时的缩放值也会偏大约 2 倍。

社区实践的修复方法：在 DaVinci 中将 **Input Zoom 设为 2.0**，可以完美恢复所有在 Premiere 中通过 Motion 面板做的缩放/位移。

**根因**：Premiere 使用的是**绝对像素位置**缩放模型，FCP7 XML 和 DaVinci 使用**相对百分比模型**。两者之间的坐标系转换存在系统性偏移。

**修正策略**：检测到 Motion 面板缩放值时，考虑应用 2.0 修正因子。需要实际 PR XML 样本验证。

### 1.3 达芬奇双重缩放陷阱

DaVinci 可能在导入时**施加两次缩放**：
1. 从 XML 的 sizing 数据 — 一次
2. 从项目的 Image Scaling 预设（"Scale to fit"等）— 第二次

建议修正后的 XML 在导入时建议用户不勾选 "Use sizing information"，或设置 Image Scaling 为 "Center crop with no resizing"。

---

## 2. Lumetri 滤镜 — XML 路径必须删除

### 2.1 已是确认的崩溃原因

Adobe 官方确认的 Bug（jamieclarke，Adobe 员工已复现）：Premiere Pro 25.4.1/25.5 版本中，含 Lumetri 的 FCP7 XML 回导时**导致 Premiere 崩溃**。

### 2.2 XML 路径无法传达 Lumetri

FCP7 XML 中 Lumetri 以 `<filter><effect><effectid>Lumetri</effectid>` 出现：
- DaVinci 没有 Lumetri 插件，导入时**静默忽略整个 filter 块**
- 参数名（如 "Lumetri Color Preset"）和 base64 LUT blob 对 DaVinci 无意义
- 只会增加 XML 体积，无功能收益

**结论**：FCP7 XML 路径 → 必须删除 Lumetri 块。

### 2.3 Lumetri 参数 → DaVinci Color 映射（DRT 潜力）

PR Lumetri 与 DaVinci Color Corrector 的参数对应：

| PR Lumetri Basic Correction | DaVinci Color Wheels | 映射难度 |
|-----------------------------|---------------------|:--------:|
| Exposure | Offset / Gain | 直接 |
| Contrast | Contrast | 直接 |
| Highlights | Highlights | 直接 |
| Shadows | Shadows / Lift | 直接 |
| Whites | Gain | 直接 |
| Blacks | Lift | 直接 |
| Saturation | Saturation | 直接 |
| Temperature | Temperature | 直接 |
| Tint | Tint | 直接 |
| Vibrance | — | 无等效 |
| Sharpness | Midtone Detail | 近似 |
| Faded Film | — | 无法 |
| Vignette | Power Window | 近似 |

PR Lumetri Creative / Curves:
| PR Lumetri Creative | DaVinci | 可行性 |
|--------------------|---------|:------:|
| Look / LUT (.cube) | LUT 节点 | base64 解码→.cube ✅ |
| RGB Curves | Custom Curves | 参数映射 ✅ |
| HSL Secondaries | Qualifier + Hue curves | 理论上可行 |
| Vignette | Power Window + Vignette effect | 近似 |

**结论**：DRT 路径可以映射大部分 Lumetri 参数，但需要准确理解达芬奇 Color 节点在 DRT project.xml 中的表示格式。

---

## 3. DRT/DRP 格式研究

### 3.1 容器格式

| 属性 | DRP (工程归档) | DRT (时间线归档) |
|------|:------------:|:------------:|
| 容器 | ZIP | ZIP |
| 根 XML 标签 | `<SM_Project>` | `<SM_Project>` |
| 时间线数据 | `SeqContainer/{UUID}.xml` | `SeqContainer/{UUID}.xml` |
| Gallery 数据 | `Gallery.xml` | 不含 |
| 媒体池结构 | `MediaPool/Master/*/MpFolder.xml` | 不含 |

### 3.2 关键限制

1. **刻意混淆**：Blackmagic 官方承认 "Our project files purposely obfuscate our IP"。`<FieldsBlob>`、`<Buffer>` 等字段含故意不可读的 hex blob。
2. **无公开 Schema**：没有官方文档，没有第三方规范
3. **版本锁定**：DRT 从 Resolve 18 和 17 **互不兼容**，格式随版本变化
4. **官方建议**：使用 **Scripting API** 而非逆向工程 DRT 格式

### 3.3 Scripting API 替代路径

DaVinci Resolve Studio 提供 Python API：
```python
# 导入 FCP7 XML
mediaPool.ImportTimelineFromFile("sequence.xml", {
    "timelineName": "Imported",
    "importSourceClips": True,
})

# 导出 DRT
timeline.Export("sequence.drt", resolve.EXPORT_DRT)
```

**限制**：需要 Resolve Studio 运行中。不适合作为独立命令行工具，但可作为高级用户选项。

### 3.4 clean_drt.py 的 DRT 处理方式

clean_drt.py 通过 **解包 ZIP → 修改现有 project.xml → 重新打包 ZIP** 来清洗 DRT。这个方式可行的前提是**已经有合法的 DRT**（从 DaVinci 导出的原始文件）。

prxml2fcp7xml 的输入是 **PR 导出的 FCP7 XML**，不是 DRT。从 FCP7 XML 生成 DRT 需要：
- 构造 `SM_Project` 根节点（含正确的版本注释头 `<!--DbAppVer=... DbPrjVer=...-->`）
- 生成 `SeqContainer/{UUID}.xml` 格式的时间线
- 填充 `FieldsBlob` 等混淆字段

这比 clean_drt.py 的"修改现有 DRT"复杂得多。

### 3.5 对 prxml2fcp7xml 的 DRT 策略建议

| 路径 | 风险 | 可行性 | 建议 |
|------|:----:|:------:|------|
| **从零生成 DRT** | 🔴 高 | 低（无公开 schema，混淆字段） | ❌ Phase 1 不做 |
| **Scripting API 桥接** | 🟡 中 | 需 Resolve Studio 运行 | ⚠️ Phase 2 实验 |
| **生成干净 FCP7 XML → 用户手动导入后导出 DRT** | 🟢 低 | 100% | ✅ Phase 1 首选 |
| **修改现有 DRT 模板** | 🟡 中 | 需合法模板 DRT | ⚠️ 需用户提供模板 |

**结论**：Phase 1 聚焦**生成尽可能干净的 FCP7 XML**。DRT 生成作为 Phase 2，优先探索 Scripting API 路径（更可靠），从零生成 DRT 作为最后手段。

---

## 4. FCP7 XML 已知结构问题汇总

### 4.1 PR 导出的 FCP7 XML 实际缺失/错误项

| 问题 | 严重度 | 发生条件 |
|------|:------:|---------|
| 缺少 `<format>` (video → DaVinci 强制要求) | CRITICAL | PR 某些版本不输出 |
| `<rate>` 缺少 `<ntsc>` 子元素 | CRITICAL | 非标准帧率时 |
| `<pathurl>` 格式不标准 | CRITICAL | Windows 路径含反斜杠 |
| `<file>` 缺少 `media/video/samplecharacteristics` | MAJOR | 部分素材元数据缺失 |
| 缺少 `<masterclipid>` | MAJOR | PR 不输出这个 FCP7 标签 |
| `<clipitem>` 子元素顺序不规范 | MAJOR | PR 输出顺序可能与 FCP7 规范不同 |
| Scale to Frame Size 缩放丢失 | MAJOR | 用户用了 Scale to Frame Size |
| Motion 缩放值偏 ~2x | MAJOR | 用户用了 Motion 面板缩放 |
| Lumetri 滤镜块（无意义数据） | MAJOR | 用户加了调色 |
| 缺少 `<sourcetrack>` | MAJOR | 取决于 PR 版本 |
| `<in>`/`<out>` 为 -1 无意义 | MINOR | 转场附近或特殊情况 |
| NTSC 帧率不一致 | MINOR | 混合帧率项目 |
| FCPCurve 变速曲线名 | MINOR | 变速片段 |
| 浮点精度 `2.18e-10` | MINOR | PR 内部精度误差 |

### 4.2 达芬奇导入时的不支持项（无法在 XML 层修复）

| PR 元素 | XML 中有无 | 达芬奇行为 |
|---------|:--------:|-----------|
| generatoritem (文字标题) | 有 | 导入为空白/黑色 |
| 嵌套序列 | 有 | 经常展平或失败 |
| FCP7 色彩校正参数 | 有 | 不可转移 |
| 贴纸/特效 | 无（PR 不导出） | — |
| 关键帧动画 | 有 | 仅静态值导入 |

---

## 5. 对项目设计的最终影响

### 5.1 FCP7 XML 路径确认

- CRITICAL 修正 (C1-C7)：所有 PR XML 都需要
- MAJOR 修正 (M1-M6)：几乎所有 PR XML 都需要
- M7 (Scale 自动适配)：仅在分辨率不匹配 + scale=100% 时触发
- Lumetri 删除：仅在 XML 路径（DRT 路径保留以映射到 Color 节点）
- 轨道 disabled/locked：**保留不动**

### 5.2 DRT 路径降级

**Phase 1 放弃从零生成 DRT**。原因：
- 无公开 schema，官方刻意混淆格式
- 版本锁定，Resolve 18 和 17 不兼容
- 官方推荐 Scripting API

**替代方案**：
1. 生成干净 FCP7 XML → 用户导入达芬奇后自行 File→Export→Timeline 导出 DRT
2. （Phase 2 实验）Scripting API 自动化桥接
3. （Phase 2 实验）修改 DRT 模板（需用户提供）

### 5.3 需要进一步获取的数据

- [ ] 实际 PR 导出的 FCP7 XML 样本（横屏/竖屏/多轨/含变速/含 Lumetri）
- [ ] DaVinci Resolve 从同一时间线导出的 DRT 文件（对比研究）
- [ ] 含 Motion 面板手动缩放的 PR XML（验证 2x 修正因子）
- [ ] DaVinci Resolve 导出的 DRT 内 `SeqContainer/{UUID}.xml` 格式（Color 节点结构）

---

*研究日期: 2026-06-04*
