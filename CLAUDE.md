# Claude 项目入口

开始工作前，先阅读并遵守 [AGENTS.md](AGENTS.md)，这是 Codex 与 Claude 共用的项目约定。

本文件只放 Claude 专属补充；共用规则一律更新 AGENTS.md。运行说明、详细协作流程和阶段记录按其中的链接查阅。

## Claude 专属

- Claude 创建的 Git 提交保留用户配置的 author／committer，并附且仅附一次：`Co-Authored-By: <实际执行模型> <noreply@anthropic.com>`（对应 AGENTS.md 中 Codex 的同类规则）。署名写实际执行的模型，不写死某一版本；模型身份无法确认时写 `Claude`，不猜测版本。
- 默认使用中文沟通与写作；代码标识符、提交前缀（`docs:` `feat:` `fix:` `test:` `refactor:`）用英文。
- 当前处于方案评审期：不要写应用代码，除非用户明确宣布方案获批并指明里程碑。方案修订写入 `docs/plans/` 的现行方案并在评审记录中留痕，不要另起平行版本。
- 推送远端、读取真实账户数据、调用外部模型评审前，先确认内容不含私有信息，并按 AGENTS.md 的授权规则办理。
