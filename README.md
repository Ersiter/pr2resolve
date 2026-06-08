<div align="center">

# pr2resolve

Premiere Pro 时间线转 DaVinci Resolve 兼容格式。支持 FCP7 XML 和 DRT 双输出。

[**English Version**](README_EN.md)

</div>

---

## 功能特性

- **双入口** — PR 导出的 FCP7 XML (.xml) 和 PR 原生工程 (.prproj) 均可输入，推荐直接丢 .prproj（原始数据最完整）
- **双出口** — FCP7 XML 零依赖始终可用；DRT 通过达芬奇 Scripting API 保留 Lumetri 调色、变速曲线等 XML 无法承载的数据
- **Scale 自动修正** — 源素材分辨率与时间线分辨率不匹配时，自动计算并写入正确的 fit scale 值
- **Lumetri 双策略** — XML 路径删除达芬奇不认的 Lumetri 块；DRT 路径将调色参数映射到达芬奇原生 Color Corrector 节点
- **路径标准化** — `file://localhost/...` 统一转为 `file:///...` 标准格式
- **缺失元素补全** — 自动生成 `<format>`、`<ntsc>`、`<sourcetrack>`、`<masterclipid>`、`<link>` 等达芬奇要求的元素
- **多轨支持** — 视频多轨、音频多轨、素材修剪 (in/out)、变速 (PlaybackSpeed)
- **交互 TUI** — Windows (.bat)、macOS / Linux (.sh) 双平台菜单界面

---

## 系统要求

| 项目 | 要求 |
|------|------|
| Python | **3.8** 或更高版本 |
| 操作系统 | Windows 10+、macOS 10.15+、Linux |
| 外部依赖 | 无（仅 Python 标准库） |
| DRT 输出 | DaVinci Resolve Studio（免费版无 Scripting API） |

---

## 快速开始

### Windows

双击 `converter.bat` 启动 TUI 交互界面。

```
1. 双击 converter.bat
2. 输入 1 选择输入文件 (.xml 或 .prproj)
3. 输入 2 设置输出目录（可选四种模式）
4. 输入 3 配置导出选项 (XML / DRT / Report)
5. 输入 4 开始转换
```

### macOS / Linux

```bash
chmod +x converter.sh
./converter.sh
```

### 命令行 (全平台)

```bash
# PR XML — 修正后输出
python pr2resolve.py "input.xml"

# .prproj — 直接解析输出（推荐）
python pr2resolve.py "project.prproj" -o ./output

# .prproj 指定序列名
python pr2resolve.py "project.prproj" --sequence "序列 01"

# DRT 输出（需达芬奇 Studio 运行中）
python pr2resolve.py "input.xml" --drt

# 生成修正报告
python pr2resolve.py "input.xml" --report

# 仅诊断不修正
python pr2resolve.py "input.xml" --diagnose-only
```

### 达芬奇导入

```
DaVinci Resolve -> File -> Import Timeline -> Import AAF, EDL, XML...
-> 选择生成的 .xml 文件

DRT: File -> Import Timeline -> Import DRT...
```

---

## 工作原理

```
输入 (.xml 或 .prproj)
    |
    +-- .xml -> ElementTree 结构化解析
    +-- .prproj -> gzip 解压 -> ObjectID 图遍历
    |
    v
诊断引擎 — 扫描 21 项已知问题 (C0-C6, M0-M7, N1-N7)
    |
    v
修正引擎 — 按 Critical -> Major -> Normal 优先级自动修复
    |
    v
验证器 — 23 项 FCP7 规范合规检查
    |
    v
输出:
    +-- output.xml  -- 修正后的 FCP7 XML (始终输出)
    +-- output.md   -- 修正报告 (可选)
    +-- output.drt  -- 达芬奇原生时间线 (可选，需达芬奇运行)
```

---

## 修正规则一览

| 级别 | 规则 | 说明 |
|------|------|------|
| C0 | version | `xmeml version="4"` -> `"5"` |
| C1-C2 | format | 补全缺失的 video/audio `<format>` |
| C3-C4 | rate | 补全缺失的 `<ntsc>` / `<timebase>` |
| C5 | pathurl | `file://localhost/...` -> `file:///...` |
| C6 | media 顺序 | video 移到 audio 前面 |
| M0 | Lumetri | XML 路径删除 lumetri 块；DRT 路径映射到 Color 节点 |
| M1-M2 | clipid/track | 补全 `<masterclipid>` / `<sourcetrack>` |
| M4 | link | 同源素材自动生成 `<link>` 关联 |
| M5 | file details | 补全 `<file>` 缺少的 samplecharacteristics |
| M6 | 元素顺序 | clipitem 子元素按 FCP7 规范排序 |
| M7 | Scale | 源分辨率 / 时间线分辨率 = fit scale |
| N1-N7 | 细节 | timecode / 浮点精度 / 帧率一致性 / displayformat 等 |

---

## 已知限制

1. **文字标题** — PR generatoritem 导入达芬奇后常显示为空（FCP7 XML 格式限制，不可修复）
2. **嵌套序列** — PR 嵌套序列在 FCP7 XML 导入时经常展平或失败
3. **素材路径** — XML 引用原始绝对路径。素材移动后需在达芬奇中手动 Relink
4. **达芬奇缩放** — 导入时建议取消勾选 "Use sizing information"，避免达芬奇额外施加缩放
5. **免费版达芬奇** — Scripting API 仅 Studio 版提供，DRT 输出不可用。XML 不受影响
6. **Lumetri 映射** — DRT 路径下基本参数（曝光/对比度/高光/阴影/色温等）可映射到达芬奇 Color 节点；Vignette / Sharpen 等复合效果仅近似

---

## 常见问题

### Q: 提示 "Python not found"

安装 Python 3.8+ 并确保添加到系统 PATH：
- Windows: https://www.python.org/downloads/ -> 安装时勾选 "Add Python to PATH"
- macOS: `brew install python3`
- Linux: `sudo apt install python3`

### Q: .prproj 和 PR 导出的 XML 选哪个

**推荐 .prproj。** .prproj 是 PR 的原生保存格式，包含完整的 Lumetri 参数、变速曲线、关键帧数据。PR 自带的 FCP7 XML 导出是简化版，数据已经损失。有 .prproj 就直接用，不要多此一举先导出 XML。

### Q: DRT 有什么用

DRT 能做 XML 做不到的事——Lumetri 参数直接写入达芬奇 Color 节点。在 PR 中做了大量调色的项目，.prproj + DRT 保留最多数据。需要 DaVinci Resolve Studio 正在运行。

### Q: 导入达芬奇后素材离线

XML 引用绝对路径。素材移动后使用达芬奇的 Relink 功能重新定位。

### Q: 导入达芬奇后画面比例不对

修正后的 XML 已自动修复 Scale 值。如仍然不对，将达芬奇 Image Scaling 设为 "Center crop with no resizing"。

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
pr2resolve/
├── pr2resolve.py          # 核心 CLI 工具
├── converter.bat           # Windows TUI
├── converter.sh            # macOS / Linux TUI
├── tests/
│   └── test_validator.py   # 18 项单元测试
├── README.md               # 中文文档
├── README_EN.md            # 英文文档
└── LICENSE
```
