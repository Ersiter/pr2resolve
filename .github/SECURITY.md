# Security Policy / 安全策略

## Supported Versions / 支持版本

| Version | Supported |
| --- | --- |
| 1.0.x | Yes |
| < 1.0 | No |

## Reporting a Vulnerability / 报告安全问题

Please do not report security vulnerabilities through public GitHub issues.

请不要通过公开 GitHub Issue 报告安全漏洞。

Use GitHub Private Vulnerability Reporting for:

请通过 GitHub Private Vulnerability Reporting 报告以下问题：

- Arbitrary file access / 任意文件访问
- Path traversal / 路径穿越
- Maliciously crafted `.prproj` or XML files / 恶意构造的 `.prproj` 或 XML 文件
- Command injection / 命令注入
- Unexpected execution of external programs / 非预期外部程序执行
- Exposure of local paths, project names, or media metadata / 本地路径、项目名或素材元数据泄露

This project intentionally does not list an email fallback for vulnerability
reports. Maintainers must keep GitHub Private Vulnerability Reporting enabled
before directing users to this policy.

本项目暂不提供邮箱 fallback。维护者必须先启用 GitHub Private Vulnerability
Reporting，再将用户引导到本安全策略。

Maintainers should enable this in:

维护者应在以下位置启用私密漏洞报告：

```text
Repository Settings -> Security -> Private vulnerability reporting
```

If private vulnerability reporting is not available, please wait until it is
enabled instead of opening a public issue with exploit details.

如果私密漏洞报告暂不可用，请等待维护者启用后再提交，不要在公开 Issue
中发布漏洞细节。
