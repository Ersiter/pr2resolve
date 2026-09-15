# 2026 上海开源软件应用创新大赛：pr2resolve 参赛说明

## 项目定位

pr2resolve 是一个开源的 Premiere Pro → DaVinci Resolve 时间线转换与修复工具。它处理剪辑师实际回批中的格式问题：PR 导出的 FCP7 XML 可能缺少结构字段、路径格式不兼容、缩放比例失真，Lumetri 数据也无法被 Resolve 直接读取。工具先诊断，再按规则修复，并输出 FCP7 XML、DRT 或 DRP。

它最适合申报“创意软件/数字内容生产工具”方向；若赛道更偏通用 AI 或互联网服务，适配风险在于本项目是离线桌面工程工具，没有在线用户量、云服务或 AI 核心能力。材料只陈述已实现的转换、诊断、验证和开源治理，不把 AI 作为装饰性卖点。

## 可复现的技术特点

- 双入口：读取 PR 导出的 `.xml`，或直接读取 gzip 压缩的 `.prproj` 对象图。
- 数据优先：`.prproj` 转换读取工程中的路径、帧率、时码、时长和分辨率；主转换链路不要求源媒体存在，也不调用 Resolve 或媒体扫描。
- 三种输出：FCP7 XML 为零依赖路径；DRT 与传统 DRP 需要运行中的 Resolve Studio；`--native-drp` 使用仓库内 Resolve 21 模板生成原生 DRP，无需启动 Resolve。
- 可审计：诊断、修复和 23 项结构验证在 CLI 日志及 `--report` 报告中留下结果；MIT License，贡献与安全问题均以仓库规则为准。

## 生产痛点与边界

工具针对“剪辑完成后换调色/交付工具”的真实断点，减少逐条 relink、缩放重算和 XML 清理。它不读取媒体内容来判断剪辑效果，也不承诺完整还原 Premiere 效果。Resolve 21 native DRP 模板是当前验证边界；DRT/后台 DRP 仍受 Resolve Studio 与本机安装方式影响。

已知限制包括嵌套序列、PR 文字标题、移动素材后的 relink、非标准像素宽高比，以及 Lumetri 的近似映射。免费版 Resolve 不作为 DRT/DRP 支持目标；FCP7 XML 路径不受该限制。

## 当前真实验证

在 Windows 工作区使用仓库绑定的 Python 3.12.14 运行：

- `test/荷花.prproj` 的“序列 01”执行 `--native-drp --report` 成功；输出 native DRP、修正 XML 和报告，日志显示 “All 23 validation checks passed”。诊断阶段有 1 个 N5 多时间基提示。
- `test/序列 01_pr_direct.xml` 执行 `--diagnose-only` 成功，发现 22 项待处理问题。
- `tests/run_local_tests.py --mode fast` 当前为 1 passed、4 failed；4 个失败都来自测试仍引用已从主链路移除的 `_ffprobe_*` / `_merge_source_tc_info` 私有接口，不能记作通过。

复现命令、样本边界和验收记录见 [reproducibility-checklist.md](reproducibility-checklist.md)。三分钟现场讲解见 [demo-script-3min.md](demo-script-3min.md)。
