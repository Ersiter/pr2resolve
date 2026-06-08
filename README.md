<div align="center">

# prxml2fcp7xml

将 Premiere Pro 工程文件转换为 DaVinci Resolve 可直接导入的 FCP7 XML 格式，支持可选 DRT 输出以保留完整调色数据。

[**English Version**](README_EN.md)

</div>

---

## 你是不是也遇到过

Premiere Pro 导出的 FCP7 XML 导入达芬奇后，素材比例全乱、调色全部消失、素材离线找不到——这不是你的操作问题，是 PR 导出 XML 本身就有缺陷。

**Scale to Frame Size 凭空消失。** 你在 PR 里缩放适配了素材，XML 里 Scale 却写着 100%。导入达芬奇后画面比预期大好几倍，必须手动计算修正值。

**Lumetri 调色数据被扔掉。** 你花了大量时间做的调色，在 XML 里是一大坨 base64 编码的 Lumetri blob。达芬奇不认识这个插件，导入时静默忽略——你的调色工作等于白做，必须在达芬奇里从头再来。

**pathurl 格式让达芬奇找不到素材。** Windows 上的 PR 导出 `file://localhost/C%3a/Users/...`，达芬奇不认识这个格式，素材全部离线，你只能一个个手动 relink。

这个工具自动化解决以上所有问题。

---

## 干了什么

- **修正 Scale 缩放** — 自动检测源素材分辨率与时间线分辨率的差异，计算正确的 fit scale 值
- **清理 Lumetri 噪声** — FCP7 XML 路径自动移除无意义的 Lumetri 块，减小文件体积；DRT 路径映射 Lumetri 参数到达芬奇原生 Color 节点
- **修复路径格式** — pathurl 统一转换为 `file:///` 标准格式
- **补全缺失元素** — 自动生成缺失的 `<format>`、`<ntsc>`、`<sourcetrack>`、`<masterclipid>` 等达芬奇要求的元素
- **双入口支持** — 支持 PR 导出的 FCP7 XML，更推荐直接导入 PR 原生 `.prproj` 工程文件（数据更完整）
- **双出口输出** — FCP7 XML 零依赖始终可用；DRT 通过达芬奇 Scripting API 保留最完整数据

---

## 为什么推荐导入 .prproj 工程

PR 自带的 FCP7 XML 导出是「二手数据」——PR 先生成一份简化版 XML，数据已经损失了一批。而 `.prproj` 是 PR 自己的原生工程格式（gzip 压缩的 XML），包含最完整的 Lumetri 调色参数、变速曲线、Transform 关键帧等数据。

本工具直接解析 `.prproj`，提取完整时间线数据，相当于从源头获取最高质量的信息。**如果你的工程是 `.prproj`，直接用 .prproj 导入本工具，不要先从 PR 导出 XML。**

---

## DRT 输出的意义

FCP7 XML 是一种交换格式，表达能力有硬上限——FCP7 规范定义了什么就是什么，不能多。达芬奇的 Color 节点树、完整 Transform 关键帧、光流变速算法等，XML 根本表达不了。

DRT (DaVinci Resolve Timeline) 是达芬奇的原生格式，能做 XML 做不到的事：Lumetri 参数可以直接映射到达芬奇 Color Corrector 节点，Scale/Fit 策略可以精确保留，变速算法可以完整传递。

**DRT 输出需要 DaVinci Resolve Studio 运行中。** 使用流程：
1. 打开 DaVinci Resolve Studio
2. 在本工具中开启 DRT 选项
3. 工具自动通过 Scripting API 导入修正后的 XML
4. 自动补全 Lumetri Color 节点数据
5. 导出 .drt 文件

如果没有达芬奇运行，DRT 会优雅降级，XML 依然正常生成。

---

## 快速开始

### Windows

双击 `converter.bat` 启动 TUI 交互界面。

```
1. 双击 converter.bat
2. 输入 1 选择输入文件 (.xml 或 .prproj)
3. 输入 2 设置输出目录（或回车跳过，默认同输入目录）
4. 输入 3 配置导出选项（XML / DRT / Report）
5. 输入 4 开始转换
```

### macOS / Linux

```bash
chmod +x converter.sh
./converter.sh
```

### 命令行（全平台）

```bash
# PR XML -- 修正后 XML
python prxml_to_fcp7xml.py "input.xml"

# .prproj -- 直接导出 XML（推荐）
python prxml_to_fcp7xml.py "project.prproj" -o ./output

# .prproj 指定序列名
python prxml_to_fcp7xml.py "project.prproj" --sequence "序列 01"

# DRT 输出（需要达芬奇 Studio 运行中）
python prxml_to_fcp7xml.py "input.xml" --drt

# 生成修正报告
python prxml_to_fcp7xml.py "input.xml" --report

# 仅诊断不修正
python prxml_to_fcp7xml.py "input.xml" --diagnose-only
```

---

## 工作原理

```
输入: PR FCP7 XML (.xml) 或 PR 工程 (.prproj)
    │
    ├─ 入口 A: FCP7 XML 解析 (xml.etree.ElementTree)
    │   └─ 不再逐行正则！结构化语义理解
    │
    ├─ 入口 B: .prproj 解析 (gzip → ObjectID 图遍历)
    │   └─ Sequence → TrackGroup → TrackItem → SubClip → MasterClip
    │
    ▼
统一诊断引擎 — 扫描 23 项已知问题，生成 Issue[]
    │
    ▼
修正引擎 — 按 C(ritical) → M(ajor) → N(ormal) 优先级自动修复
    │
    ▼
验证器 — 23 项 FCP7 规范合规检查
    │
    ▼
输出:
    ├─ output.xml   ← 修正后的 FCP7 XML（始终输出）
    ├─ output.md    ← 修正报告（可选）
    └─ output.drt   ← DaVinci 原生时间线（可选，需达芬奇运行）
```

---

## 修正规则一览

| 级别 | 规则 | 说明 |
|------|------|------|
| **C0** | version | `xmeml version="4"` → `"5"` |
| **C1-C2** | format | 补全缺失的 video/audio `<format>` |
| **C3-C4** | rate | 补全缺失的 `<ntsc>` / `<timebase>` |
| **C5** | pathurl | `file://localhost/...` → `file:///...` |
| **C6** | media 顺序 | video 移到 audio 前面 |
| **M0** | Lumetri | XML 路径删除，DRT 路径映射到 Color 节点 |
| **M1-M2** | clipid/track | 补全缺失的 `<masterclipid>` / `<sourcetrack>` |
| **M7** | Scale | 源分辨率 ÷ 时间线分辨率，自动计算 fit scale |
| **N1-N7** | 细节 | timecode / 浮点精度 / 帧率一致性等 |

---

## 系统要求

| 项目 | 要求 |
|------|------|
| Python | 3.8 或更高版本 |
| 操作系统 | Windows 10+, macOS 10.15+, Linux |
| 外部依赖 | 零（仅 Python 标准库） |
| DRT 输出 | DaVinci Resolve Studio（免费版无 Scripting API） |

---

## 已知限制

1. **文字 / 生成器素材** — PR 的 generatoritem (文字标题) 导入达芬奇后常显示为空。FCP7 XML 格式本身的限制，无法在 XML 层修复
2. **嵌套序列** — PR 的嵌套序列在 FCP7 XML 导出时经常被展平或失败
3. **素材路径** — XML 引用原始绝对路径。素材移动后需在达芬奇中手动 Relink
4. **达芬奇双重缩放** — 导入时建议不勾选 "Use sizing information"，避免达芬奇额外施加缩放
5. **DRT 需要达芬奇 Studio** — 免费版 Resolve 无 Scripting API，DRT 功能不可用。XML 不受影响

---

## 常见问题

### Q: 提示 "Python not found"

安装 Python 3.8+ 并确保添加到系统 PATH。
- Windows: https://www.python.org/downloads/ → 安装时勾选 "Add Python to PATH"
- macOS: `brew install python3`
- Linux: `sudo apt install python3`

### Q: 导入达芬奇后素材离线

XML 引用绝对路径。如果素材移动了位置，在达芬奇中使用 Relink 功能定位素材。

### Q: 导入达芬奇后画面比例不对

运行本工具修正后的 XML 应该已经自动修复 Scale 值。如果仍然不对，检查达芬奇导入设置 —— 建议将 Image Scaling 设为 "Center crop with no resizing"。

### Q: .prproj 和 PR 导出的 XML 应该选哪个

**推荐 .prproj。** 除非你的 PR 版本不支持直接导出工程（你肯定有 .prproj，它是 PR 的保存格式），否则不需要先导出 XML 再修正——直接给 .prproj 就好。

### Q: DRT 有什么用，什么时候用

DRT 在做 XML 做不到的事情：Lumetri 调色映射到达芬奇 Color 节点。如果你在 PR 中做了大量调色，用 .prproj + DRT 可以最大程度保留这些数据。但 DRT 需要达芬奇 Studio 正在运行。

### Q: Lumetri 调色能完美还原吗

FCP7 XML 路径：不能——Lumetri 是 PR 专有滤镜，XML 中会被删除。DRT 路径：基本参数（曝光/对比度/高光/阴影/色温等）可以映射到达芬奇 Color 节点。Creative LUT 可以提取 .cube 文件。Vignette、Sharpen 等复合效果只能近似。

---

## 参考项目

- [PRPROJ-READER](https://github.com/sergeiventurinov/PRPROJ-READER) — .prproj 格式逆向工程
- [prproj_downgrade](https://github.com/snorkem/prproj_downgrade) — .prproj 版本降级工具
- [ppro-scripting.docsforadobe.dev](https://ppro-scripting.docsforadobe.dev) — Adobe 官方对象模型文档
- [DaVinci Resolve Scripting API](https://resolvedevdoc.readthedocs.io/) — 达芬奇 Python API 文档

---

## 开源协议

[MIT License](LICENSE)

---

## 项目结构

```
pr2drt/
├── prxml_to_fcp7xml.py    # 核心 CLI 工具（解析 / 诊断 / 修正 / 验证 / DRT）
├── converter.bat           # Windows TUI
├── converter.sh            # macOS/Linux TUI
├── tests/
│   └── test_validator.py   # 18 项单元测试
├── README.md
└── LICENSE
```
