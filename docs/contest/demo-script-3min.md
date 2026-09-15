# 三分钟演示脚本

## 0:00–0:30：问题

“剪辑师在 Premiere Pro 完成后交给 Resolve 调色，直接导出的 XML 常见路径、缩放、结构和 Lumetri 问题。逐条修复既慢又不可审计。pr2resolve 把这段交接变成可重复的命令行流程。”

## 0:30–1:10：输入与诊断

在仓库根目录执行公开 fixture 诊断：

```powershell
python pr2resolve.py "examples\minimal_fcp7.xml" --diagnose-only
```

展示诊断日志，说明工具先报告问题，不修改输入文件。再指出 `.prproj` 是推荐入口，因为它保留的工程上下文多于 PR 二次导出的 XML；本次公开 fixture 只验证 XML 输入和输出链路。

## 1:10–2:20：纯数据转换

```powershell
python pr2resolve.py "examples\minimal_fcp7.xml" -o .\contest-output --native-drp --report
```

强调这条路径不启动 Resolve、不扫描媒体；工程中的媒体路径仍会写入输出，素材离线时可以先完成结构转换，之后在 Resolve 中 relink。展示生成的 `.drp`、`.xml` 和 `_fix_report.md`，以及 `All 23 validation checks passed`。

## 2:20–3:00：边界与开源治理

说明 native DRP 当前以仓库内 Resolve 21 模板为验证边界；DRT 和传统 DRP 需要 Resolve Studio。展示 `docs/contest/reproducibility-checklist.md`、MIT License 与 `.github/SECURITY.md`，说明结果可复跑、限制有记录、漏洞走 GitHub Private Vulnerability Reporting。不要声称性能、用户量或 AI 能力，现场只展示公开 fixture 和日志。
