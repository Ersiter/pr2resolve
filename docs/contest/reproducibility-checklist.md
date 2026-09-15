# 参赛复现与验收清单

## 环境

- Windows 10/11；Python 3.10+（本次记录：Python 3.12.14）。
- 不需要第三方运行时依赖即可执行 XML/prproj 主路径；Resolve 输出路径按下表另有条件。
- 使用仓库版本：`1.1.0`。版本来源为 `pr2_constants.py`，批处理入口和 shell 入口横幅同步为 `1.1.0`。

## 最小复现

```powershell
python pr2resolve.py --version
python pr2resolve.py "examples\minimal_fcp7.xml" --diagnose-only
python pr2resolve.py "examples\minimal_fcp7.xml" -o .\contest-output --native-drp --report
```

预期：版本输出 `pr2resolve 1.1.0`；XML 诊断退出码为 0；native DRP 命令退出码为 0，生成 native `.drp`、修正 `.xml` 和报告，并输出 `All 23 validation checks passed`。输出目录可换成任意工作目录，不依赖固定盘符。

## 输出路径条件

| 输出 | 条件 | 本次可复现边界 |
| --- | --- | --- |
| FCP7 XML | 无 Resolve | XML/prproj 均可 |
| native DRP | 无 Resolve、无媒体扫描 | 仓库 Resolve 21 模板 |
| DRT | Resolve Studio 运行中 | Resolve API |
| 传统 DRP | Resolve Studio 与对应后台/GUI模式 | Resolve 进程生命周期 |

## 已记录事实

- `.github/scripts/ci_smoke_tests.py`：2 项通过，覆盖 prproj 元数据解析和最小 native DRP 导出。
- `examples/minimal_fcp7.xml`：公开合成 fixture，可生成 native DRP；其中 `neutral.mov` 只是路径占位，不需要媒体文件。
- 开发机真实项目样本另有验证，但未随仓库分发，不能作为公开复现依据。

## 复核项

- [ ] 输入样本来自 `test/`，未引入本机媒体或用户路径。
- [ ] 未把 Resolve、媒体扫描、性能、用户量或 AI 能力写成无证据承诺。
- [ ] 演示和提交说明链接均为仓库内相对链接。
- [ ] 参赛材料与 MIT License、`AGENTS.md`、`CLAUDE.md` 和安全策略一致。
