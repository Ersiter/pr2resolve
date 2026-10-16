# GitHub → Gitee 同步配置 / Mirror setup

GitHub 是开发和构建源，Gitee 接收全部分支、标签和正式发行包。安装包不提交到 Git 历史。首次部署必须完成下列配置和在线验收；仅添加工作流不代表镜像已启用。

GitHub remains the development and build source. Gitee receives branches, tags and stable release assets. Packages are not committed to Git history. Adding the workflow alone does not activate the mirror: complete setup and live verification below.

工作流使用 GitHub Ubuntu runner 已提供的 Git、GitHub CLI 和 curl，不新增 Python 包依赖。附件并行上传，单文件最多等待 30 分钟，任务最多运行 60 分钟；失败后按同一标签重跑会复用校验一致的附件。

The workflow uses Git, GitHub CLI and curl provided by GitHub Ubuntu runners, with no additional Python packages. Assets upload in parallel with a 30-minute per-file limit and a 60-minute job limit. Retry the same tag after a failure to reuse verified matching assets.

## 1. 创建目标仓库 / Create the destination

登录自己的 Gitee 账号，确认路径为 `Ersiter`，创建公开的空仓库 `pr2resolve`。不要初始化 README、许可证或 `.gitignore`。首次同步 `main` 后将其设为默认分支。若该路径已存在且包含独立提交，先解决归属和历史冲突，不能强制覆盖。

Sign in to the Gitee account with path `Ersiter`. Create an empty public `pr2resolve` repository without an initial README, license or `.gitignore`. Set `main` as the default branch after the first sync. Do not overwrite an existing repository with independent history.

## 2. 凭据 / Credentials

生成专用于此镜像的无口令 SSH 密钥，将公钥加入有目标仓库写权限的 Gitee 账号。Gitee 的只读部署公钥不能用于推送。私钥只录入 GitHub Actions Secret `GITEE_SSH_PRIVATE_KEY`。

Generate a dedicated SSH key without a passphrase. Add the public key to a Gitee account with write access to the destination; a read-only deployment key cannot push. Store the private key only as the GitHub Actions secret `GITEE_SSH_PRIVATE_KEY`.

在 Gitee 的[私人令牌设置](https://gitee.com/profile/personal_access_tokens)中创建有项目 API 权限的令牌，将值直接录入 GitHub Actions Secret `GITEE_TOKEN`。不要把私钥或令牌放进聊天、命令参数、仓库文件或日志。

Create a token with project API permissions in [Gitee token settings](https://gitee.com/profile/personal_access_tokens) and enter it directly as the GitHub Actions secret `GITEE_TOKEN`. Do not put credentials in chat, command arguments, tracked files or logs.

GitHub 配置入口 / GitHub configuration:

- [Repository secrets](https://github.com/Ersiter/pr2resolve/settings/secrets/actions)
- [Repository variables](https://github.com/Ersiter/pr2resolve/settings/variables/actions)

| 类型 / Kind | 名称 / Name | 内容 / Value |
|---|---|---|
| Variable | `GITEE_OWNER` | `Ersiter` |
| Variable | `GITEE_REPO` | `pr2resolve` |
| Variable | `GITEE_SSH_KNOWN_HOSTS` | 已核实的 `gitee.com` OpenSSH known_hosts 行 / verified OpenSSH host-key lines |
| Secret | `GITEE_SSH_PRIVATE_KEY` | 专用 SSH 私钥全文 / complete dedicated private key |
| Secret | `GITEE_TOKEN` | Gitee 项目 API 令牌 / Gitee project API token |

可用 `ssh-keyscan gitee.com` 收集候选主机公钥，但必须先通过 [Gitee 官方指纹说明](https://help.gitee.com/account/gitees-ssh-key-fingerprints)核实其指纹，再保存到 `GITEE_SSH_KNOWN_HOSTS`。工作流启用严格主机校验，不在运行时自动信任扫描结果。

Use `ssh-keyscan gitee.com` to collect candidate host keys, then independently verify their fingerprints through an official Gitee channel before saving them in `GITEE_SSH_KNOWN_HOSTS`. The workflow strictly checks the host key; it does not automatically trust runtime scans.

## 3. 部署与首次验收 / Deployment and initial verification

审阅并推送工作流后，在 GitHub Actions 的 **Sync Gitee → Run workflow** 中输入 `release_tag=latest`。首次同步读取当时最新的正式 GitHub Release，无需新建版本或重新构建。也可指定 `v1.6.0` 等已发布标签；留空只同步代码。

After reviewing and pushing the workflow, open **Sync Gitee → Run workflow** in GitHub Actions and enter `release_tag=latest`. This copies the latest published stable release without a new version or rebuild. Supply a specific published tag such as `v1.6.0` to backfill it, or leave the field empty to sync code only.

验收 / Acceptance:

1. 工作流成功，日志显示分支和标签 SHA 校验通过。/ The workflow succeeds and verifies branch and tag SHAs.
2. Gitee 默认分支为 `main`，公开页面可访问。/ The Gitee default branch is `main` and the repository is publicly accessible.
3. Gitee 同版本发行版包含三个平台安装包和 `SHA256SUMS.txt`，下载校验通过。脚本会对上传后的所有附件重新下载并计算 SHA-256。/ The Gitee release contains three platform packages plus `SHA256SUMS.txt`; the script downloads every uploaded asset again and verifies SHA-256.
4. 对同一标签重跑后无重复附件。/ Re-running the same tag creates no duplicate assets.
5. 下一次正常 GitHub 推送后，确认 Gitee 对应引用自动更新。/ Confirm the next normal GitHub push automatically updates the matching Gitee references.

## 4. 日常维护与恢复 / Operation and recovery

- 全部分支和标签推送触发同步。删除、强推和改写标签不会删除或覆盖 Gitee 的独立历史；非快进更新会报错，需要人工确认历史处理。/ Branch and tag pushes trigger sync. Deletions, force pushes and rewritten tags do not delete or overwrite Gitee history; non-fast-forward updates fail for manual resolution.
- 发布流程在 GitHub Release 发布成功后直接调用同步；不能只依赖 `release.published`，因为 `GITHUB_TOKEN` 创建的事件通常不触发新工作流。另有 Release 发布／编辑事件入口，用于手动发布或修改说明。/ The release workflow directly calls sync after publishing. A `GITHUB_TOKEN` release event alone does not trigger another workflow. Release publish/edit events also support manual publication and note updates.
- 同步串行排队（最多 100 个待运行任务），每次重新获取 GitHub 最新引用。/ Sync runs serialize with up to 100 queued jobs and fetch fresh GitHub references each time.
- Gitee 失败不撤销 GitHub Release。手动按标签补同步即可；已上传且内容一致的附件会复用，缺失附件继续上传。/ A Gitee failure leaves the GitHub release intact. Retry the tag manually; matching attachments are reused and missing ones uploaded.
- 同名附件内容不一致时停止，不自动删除或替换。令牌失效、权限不足和附件大小限制均明确报错；修复配置后重试。/ Conflicting attachments stop sync without deletion or replacement. Expired tokens, permission errors and upload limits fail visibly; fix the configuration and retry.
- Issues、PR、Wiki 和历史发行版不批量迁移。/ Issues, PRs, Wikis and historical releases are not migrated in bulk.

接口参考 / References: [Gitee API](https://gitee.com/api/v5/swagger), [GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow), [GitHub concurrency queues](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
