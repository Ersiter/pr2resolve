# prxml2fcp7xml

Premiere Pro FCP7 XML 修正器 — 让 PR 时间线完美导入 DaVinci Resolve。

## 解决什么问题

Premiere Pro 导出 FCP7 XML 时会丢失大量数据：

- **Scale to Frame Size** 彻底消失（100% 实际应为 ~56%）
- **Lumetri 调色** 对达芬奇无意义（静默忽略）
- **pathurl 格式** 不规范（`file://localhost/` → DaVinci 拒绝）
- **xmeml version** 错误（4 → 应为 5）
- 缺少 `<sourcetrack>`、`<masterclipid>` 等必要元素

本工具自动检测并修正这些问题。

## 双入口 × 双出口

| | 入口 A: PR FCP7 XML | 入口 B: .prproj 工程 |
|--|---|---|
| 格式 | PR 导出的 XML | PR 原生工程 (gzip XML) |
| 优势 | 通用、零依赖 | Lumetri 数据更完整 |
| 修正 | C0-C7, M0-M7, N1-N7 | 同左 + 自动转换 |

| | 出口 1: FCP7 XML | 出口 2: DRT |
|--|---|---|
| 依赖 | 无 | DaVinci Resolve Studio |
| 数据 | 中（FCP7 规范限制） | 高（Lumetri→Color 节点） |
| 始终可用 | ✅ | 需达芬奇运行 |

## 快速开始

### 最简单

```bash
# 双击 converter.bat (Windows) 或运行 converter.sh (macOS/Linux)
```

### CLI

```bash
# PR XML → 修正后 XML
python prxml_to_fcp7xml.py "input.xml"

# .prproj → XML
python prxml_to_fcp7xml.py "project.prproj" -o ./output

# 生成修正报告
python prxml_to_fcp7xml.py "input.xml" --report

# DRT 输出（需达芬奇 Studio 运行）
python prxml_to_fcp7xml.py "input.xml" --drt

# 仅诊断不修正
python prxml_to_fcp7xml.py "input.xml" --diagnose-only

# .prproj 指定序列
python prxml_to_fcp7xml.py "project.prproj" --sequence "序列 01"
```

### 扫描目录

```bash
python find_pr_xml.py D:\Projects
```

## 修正规则

### CRITICAL (C0-C7) — 缺失会导致导入失败

| ID | 问题 | 修正 |
|----|------|------|
| C0 | xmeml version ≠ 5 | 设为 5 |
| C1/C2 | 缺少 video/audio format | 插入 samplecharacteristics |
| C3 | rate 缺少 ntsc | 根据 timebase 推算 |
| C4 | rate 缺少 timebase | 默认 30 |
| C5 | pathurl 格式错误 | 转为 file:/// |
| C6 | media 子元素顺序错 | 重排 video→audio |

### MAJOR (M0-M7) — 影响画面质量

| ID | 问题 | 修正 |
|----|------|------|
| M0 | Lumetri 滤镜块 | XML 路径删除 |
| M1 | 缺少 masterclipid | 自动分配 |
| M2 | 缺少 sourcetrack | 从媒体类型推导 |
| M7 | Scale 缩放丢失 | 源/时间线分辨率比值 |

### MINOR (N1-N7) — 兼容性改善

空轨道保留不动，disabled/locked 轨道保留不动（忠实还原创作者意图）。

## 项目结构

```
pr2drt/
├── prxml_to_fcp7xml.py   # 核心 CLI 工具
├── converter.bat          # Windows TUI
├── converter.sh           # macOS/Linux TUI
├── find_pr_xml.py         # 目录扫描器
├── tests/
│   └── test_validator.py  # 18 项测试
├── test/
│   ├── 序列 01_pr_direct.xml  # 测试样本 (PR XML)
│   └── 荷花.prproj            # 测试样本 (.prproj)
├── docs/
│   ├── PRJCT_PLAN.md          # 项目规划 v2
│   ├── RESEARCH_PHASE1.md     # 兼容性研究
│   ├── PRPROJ_FORMAT.md       # .prproj 格式文档
│   ├── SAMPLE_ANALYSIS.md     # 样本分析
│   └── PRPROJ_PARSE_RESULTS.md # .prproj 实测结果
├── README.md
└── LICENSE
```

## 运行测试

```bash
python tests/test_validator.py
```

## 要求

- Python 3.8+
- 零外部依赖（仅标准库）
- DRT 输出需要 DaVinci Resolve Studio

## 设计原则

- **忠实还原** — 保留创作者意图，不替用户做编辑决定
- **只读不写** — 不改原始文件（自动 .bak 备份）
- **零依赖** — 仅 Python 标准库
- **面向非开发者** — 双击 .bat 即可运行

## License

MIT
