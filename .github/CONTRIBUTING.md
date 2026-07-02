# Contributing / 贡献指南

Thanks for helping improve pr2resolve.

感谢你帮助改进 pr2resolve。

This project converts Premiere Pro FCP7 XML / `.prproj` timelines into files
that DaVinci Resolve can import. Small changes can affect real editing projects,
so contributions should include clear reproduction steps and test evidence.

本项目会把 Premiere Pro 的 FCP7 XML / `.prproj` 时间线转换为 DaVinci
Resolve 可导入的文件。很小的改动也可能影响真实剪辑工程，因此贡献时请提供
清晰的复现步骤和测试证据。

## Development Setup / 开发环境

- Use Python 3.10+ for source mode.
- No external runtime dependency is required for the normal XML path.
- FFmpeg / `ffprobe` on `PATH` is strongly recommended for reliable `.prproj`
  source media timecode, frame rate, and duration detection.
- DaVinci Resolve Studio is only required for DRT / DRP related verification.
- Run from the repository root:

```bash
python --version
ffprobe -version
python -m py_compile pr2resolve.py pr2_engine.py pr2_constants.py tui.py
```

中文说明：

- 源码运行需要 Python 3.10+。
- 普通 XML 转换路径不需要额外运行时依赖。
- `.prproj` 可靠转换强烈建议安装 FFmpeg，并确保 `ffprobe` 在 `PATH` 中可用，
  用于读取源素材 timecode、帧率和完整时长。
- 只有验证 DRT / DRP 相关功能时才需要 DaVinci Resolve Studio。
- 命令请在仓库根目录执行。

## Privacy and Test Files / 隐私与测试文件

Do not commit:

不要提交：

- Real user `.prproj` files / 真实用户 `.prproj` 文件
- Copyrighted media or project files / 受版权保护的素材或工程文件
- Logs or reports containing user names, absolute local paths, project names,
  media paths, or client metadata / 含用户名、本地绝对路径、项目名、素材路径或客户信息的日志与报告
- Generated build artifacts / 构建产物

Use small synthetic fixtures when a regression test needs sample data.

需要样本数据时，请使用人工构造的最小复现 fixture。

## Before Submitting / 提交前检查

- Explain the original problem and the expected behavior.
- Keep one pull request focused on one change.
- Add or update regression coverage when changing parser, renderer, or export
  behavior.
- Update user-facing docs or CHANGELOG when behavior changes.
- Do not include private or copyrighted sample files.

中文说明：

- 说明原始问题和期望行为。
- 一个 PR 只处理一件事。
- 修改解析、渲染或导出行为时，补充或更新回归覆盖。
- 用户可见行为变化时，同步更新文档或 CHANGELOG。
- 不要包含私有或版权样本文件。

## High-Risk Areas / 高风险区域

Changes in these areas should include representative regression evidence:

以下区域的改动应附带有代表性的回归证据：

- `.prproj` object graph parsing / `.prproj` 对象图解析
- FCP7 XML rendering and element order / FCP7 XML 渲染与元素顺序
- Timecode, frame rate, and drop-frame logic / 时间码、帧率、drop-frame 逻辑
- `pathurl` normalization and media relinking / `pathurl` 规范化与素材重连
- Clip link generation / 片段 link 生成
- DRT / DRP export and DaVinci process handling / DRT / DRP 导出与 DaVinci 进程处理

## Pull Requests / Pull Request 要求

A useful PR should include:

一个有效 PR 应包含：

- What changed / 改了什么
- Why it changed / 为什么改
- How it was tested / 如何验证
- Compatibility impact, if any / 兼容性影响
- Whether new or changed sample files are synthetic and safe to publish /
  新增或修改的样本是否为可公开的人工构造样本

## Repository Settings / 仓库设置

Maintainers should keep GitHub Discussions and Private Vulnerability Reporting
enabled. The issue forms route usage questions to Discussions and security
reports to the Security Policy.

维护者应保持 GitHub Discussions 和 Private Vulnerability Reporting 启用。
Issue 表单会将使用问题导向 Discussions，并将安全问题导向 Security Policy。
