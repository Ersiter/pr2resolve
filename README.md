<div align="center">

# pr2resolve

将 Premiere Pro 时间线转成 DaVinci Resolve 能直接导入的 FCP7 XML。如果开着达芬奇 Studio，还能导出 DRT 保留调色数据。

[**English Version**](README_EN.md)

</div>

---

## 你是不是也遇到过

PR 导出 FCP7 XML 给达芬奇用，是出了名的坑——素材比例乱套、调色全丢、素材离线。问题不在你，在 PR 的 XML 导出。

**Scale to Frame Size 凭空消失。** PR 里缩放适配好的素材，出来 Scale 是 100%。达芬奇里画面大好几倍，你只能一个一个手动算修正值。

**Lumetri 调色没了。** 你的调色数据在 XML 里是一坨 base64 blob。达芬奇没有 Lumetri，导入时直接跳过——调色白做。

**pathurl 格式不对，素材全离线。** PR 导出的路径是 `file://localhost/C%3a/Users/...`，达芬奇不认，手动 relink 到手酸。

这个工具干的就是这些脏活。

---

## 搞定的事

- **修 Scale** — 拿源素材分辨率和时间线分辨率一比，算出该有的缩放值
- **摘 Lumetri** — XML 路径删掉达芬奇不认的 Lumetri 块；DRT 路径把调色参数搬到达芬奇 Color 节点
- **正 pathurl** — 全部转成 `file:///` 标准格式
- **补缺** — 自动填上达芬奇要的 `<format>`、`<ntsc>`、`<sourcetrack>`、`<masterclipid>` 之类
- **两个入口** — PR 导出的 XML 能吃，更建议直接丢 `.prproj` 工程文件进来（原始数据更全）
- **两个出口** — FCP7 XML 啥也不依赖随时出；DRT 多走一步达芬奇 API，保留最多东西

---

## 为什么用 .prproj 而不是导出 XML

PR 导出的 FCP7 XML 是二手货——PR 先生成一份精简版，数据已经丢了一波。`.prproj` 是 PR 自己保存的工程文件（gzip 压的 XML），Lumetri 参数、变速曲线、Transform 关键帧全在里头。

本工具直接读 `.prproj`，从源头拿最完整的数据。**有 .prproj 就直接给 .prproj，别多此一举先导出 XML。**

---

## DRT 能多做些什么

FCP7 XML 是交换格式，能力有天花板——规范里写什么就是什么。达芬奇 Color 节点树、完整关键帧、光流变速，XML 都管不了。

DRT（DaVinci Resolve Timeline）是达芬奇原生格式，能干 XML 干不了的事：Lumetri 参数直搬 Color Corrector 节点，Scale/Fit 精确保留，变速算法完整传递。

**前提：DRT 需要 DaVinci Resolve Studio 开着。**
1. 打开达芬奇 Studio
2. 工具里切到 DRT 选项
3. 自动走 Scripting API 导入修好的 XML
4. 补 Lumetri Color 节点数据
5. 出 .drt

达芬奇没开的话，XML 照样出，DRT 那一步跳过。

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
python pr2resolve.py "input.xml"

# .prproj -- 直接导出 XML（推荐）
python pr2resolve.py "project.prproj" -o ./output

# .prproj 指定序列名
python pr2resolve.py "project.prproj" --sequence "序列 01"

# DRT 输出（需要达芬奇 Studio 运行中）
python pr2resolve.py "input.xml" --drt

# 生成修正报告
python pr2resolve.py "input.xml" --report

# 仅诊断不修正
python pr2resolve.py "input.xml" --diagnose-only
```

---

## 流程

```
输入 (.xml 或 .prproj)
    │
    ├─ XML → ElementTree 结构化解析
    ├─ .prproj → gzip 解压 → ObjectID 图遍历
    │
    ▼
扫 23 项问题 → 按级别自动修 → 23 项合规验证
    │
    ▼
输出:
    ├─ .xml   ← 修好的 FCP7 XML（一定出）
    ├─ .md    ← 修正报告（可选）
    └─ .drt   ← 达芬奇原生时间线（可选，需达芬奇开着）
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
| 依赖 | 无，Python 标准库就够 |
| DRT | DaVinci Resolve Studio（免费版没 Scripting API） |

---

## 已知限制

1. **文字标题** — PR 的 generatoritem 到那边常变空的，XML 格式限制，修不了
2. **嵌套序列** — 经常展平或导入失败
3. **素材搬家** — XML 写的是绝对路径，搬了得手动 relink
4. **达芬奇双重缩放** — 导入时别勾 "Use sizing information"，不然又被缩一次
5. **免费版达芬奇没 DRT** — Scripting API 是 Studio 专属，XML 不受影响

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
├── pr2resolve.py    # 核心 CLI 工具（解析 / 诊断 / 修正 / 验证 / DRT）
├── converter.bat           # Windows TUI
├── converter.sh            # macOS/Linux TUI
├── tests/
│   └── test_validator.py   # 18 项单元测试
├── README.md
└── LICENSE
```
