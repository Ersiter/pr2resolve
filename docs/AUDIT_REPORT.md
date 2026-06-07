# 审计报告：PRJCT_PLAN.md vs 实现

> 审计日期: 2026-06-07

---

## 1. .bat / .sh 功能不同步 ❌

| 功能 | .bat | .sh | 计划要求 |
|------|:----:|:---:|:--------:|
| Select input | ✅ | ✅ | ✅ |
| Set output | ✅ | ✅ | ✅ |
| Output options (DRT/Report) | ❌ | ✅ | ✅ |
| SEQ_NAME | ❌ | ✅ | ✅ |
| Auto scan | ❌ | ❌ | ✅ |
| START | ✅ | ✅ | ✅ |

**结论**: .bat 缺少 Output options 和 SEQ_NAME 功能，与 .sh 不同步。

---

## 2. 计划 §9 TUI 菜单 vs 实际

计划要求:
```
[1] Select input file (.xml / .prproj)
[2] Auto scan for PR exports
[3] Set output directory
[4] Output format
[5] START
[0] Quit
```

.bat 实际: `[1] input, [2] output, [3] START, [0] Quit` — 缺 #2 auto scan, #4 output format
.sh 实际: `[1] input, [2] output, [3] options, [4] START, [0] Quit` — 缺 #2 auto scan

---

## 3. VERSION 同步 ✅

- prxml_to_fcp7xml.py: `VERSION = "1.0.0"`
- converter.bat: `VERSION=1.0.0`
- converter.sh: `VERSION="1.0.0"`
- 三处一致。

---

## 4. 未实现的修正规则

| 规则 | 检测 | 修正 | 计划要求 |
|------|:----:|:----:|:--------:|
| C7 DOCTYPE | N/A (ET 剥离) | ✅ 输出时总加 | ✅ |
| M3 duration 语义 | ❌ | ❌ | 有 |
| M4 缺少 link | ❌ | ❌ | 有 |
| M5 file 缺详情 | ✅ 检测 | ❌ 未修正 | 有 |
| M6 clipitem 顺序 | ❌ | ❌ | 有 |
| N2 timecode 缺 displayformat | ✅ 检测 | ❌ 未修正 | 有 |
| D1-D4 DRT 规则 | — | 框架已有 | 有 |

---

## 5. README.md 问题

- 缺少 README_EN.md（计划 §7 要求中英双语文档）
- PRJCT_PLAN.md 在 docs/ 但 README 写 "PRJCT_PLAN.md" 在根目录
- 项目结构树与实际不符（PRJCT_PLAN.md 位置）

---

## 6. 过时文档

| 文件 | 过时内容 | 应更新为 |
|------|---------|---------|
| docs/SAMPLE_ANALYSIS.md §5 | "无法实际验证" | .prproj 已实现 |
| docs/PRPROJ_FORMAT.md §7 | "Lumetri 数据（待研究）" | 已实现提取 |
| docs/RESEARCH_PHASE1.md §5.3 | "需要进一步获取的数据" checklist | 部分已完成 |
| README.md 项目结构 | PRJCT_PLAN.md 位置错误 | 在 docs/ 下 |

---

## 7. 计划 §2 代码规范检查

- ✅ Python 命名: snake_case + _ 前缀
- ✅ 类型注解: 所有函数有
- ✅ 段落分隔符: `═══` 使用正确
- ✅ .bat 变量: UPPER_SNAKE_CASE
- ✅ .bat set /p: 前有 `set "VAR="`
- ✅ .sh 变量: UPPER_SNAKE_CASE
- ✅ 开关值: [ON]/[OFF]（.sh 使用）
- ⚠️ .bat 缺少 [ON]/[OFF] 开关值（简化版无选项菜单）

---

## 8. 计划 §4 反硬编码检查

- ✅ 无硬编码盘符/用户路径
- ✅ DEFAULT_FPS 常量
- ✅ VERSION 三处一致
- ✅ FCP7 version="5" 常量
