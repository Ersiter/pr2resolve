# 参赛复现与验收清单

## 环境

- Windows 10/11；Python 3.10+（本次记录：Python 3.12.14）。
- 不需要第三方运行时依赖即可执行 XML/prproj 主路径；Resolve 输出路径按下表另有条件。
- 使用仓库版本：`1.1.0`。版本来源为 `pr2_constants.py`，批处理入口和 shell 入口横幅同步为 `1.1.0`。

## 最小复现

```powershell
python pr2resolve.py --version
python pr2resolve.py "test\序列 01_pr_direct.xml" --diagnose-only
python pr2resolve.py "test\荷花.prproj" -o .\contest-output --sequence "序列 01" --native-drp --report
```

预期：版本输出 `pr2resolve 1.1.0`；XML 诊断退出码为 0 并报告 22 项问题；prproj 命令退出码为 0，生成 native `.drp`、修正 `.xml` 和报告，并输出 `All 23 validation checks passed`。输出目录可换成任意工作目录，不依赖固定盘符。

## 输出路径条件

| 输出 | 条件 | 本次可复现边界 |
| --- | --- | --- |
| FCP7 XML | 无 Resolve | XML/prproj 均可 |
| native DRP | 无 Resolve、无媒体扫描 | 仓库 Resolve 21 模板 |
| DRT | Resolve Studio 运行中 | Resolve API |
| 传统 DRP | Resolve Studio 与对应后台/GUI模式 | Resolve 进程生命周期 |

## 已记录事实

- `test/荷花.prproj` → “序列 01”：native DRP、XML、报告成功，23 项验证通过；诊断时有 1 项 N5 提示。
- `test/序列 01_pr_direct.xml`：诊断成功，22 项问题。
- `tests/run_local_tests.py --mode fast`：1 passed、4 failed。失败是旧测试调用已移除的 ffprobe/合并私有接口；本清单保留该结果，待测试随代码迁移后再更新。

## 复核项

- [ ] 输入样本来自 `test/`，未引入本机媒体或用户路径。
- [ ] 未把 Resolve、媒体扫描、性能、用户量或 AI 能力写成无证据承诺。
- [ ] 演示和提交说明链接均为仓库内相对链接。
- [ ] 参赛材料与 MIT License、`AGENTS.md`、`CLAUDE.md` 和安全策略一致。
