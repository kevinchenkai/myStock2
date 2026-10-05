# myStock2

个人 **港股 / 美股** 的「账本 · 透视 · 教练 · 记分牌」系统（myStock 第二版）。

让 Web 随时看清自己的账户，让 AI 每天给出下一交易日的**操作单**，并在同口径下公平地检验：**AI 指导的操作，是否比我凭直觉的操作赚得更多**。

> **状态（2026-10-05）：立项与方案评审期，尚无应用代码。** 方案获批前不编码。系统**不自动下单**，不构成投资建议。

## 四个子系统

| 子系统 | 一句话 |
| --- | --- |
| **账本 Ledger** | 账户事实的唯一真相：成交、费用、资金流水，可对账，只追加 |
| **透视 Lens** | 只读 Web 视图集合：持仓、交易、盈亏、资产趋势、外汇…新增视图 = 新增一个文件夹 |
| **教练 Coach** | 每天对圈定标的（如 NVDA、TSLA、0700）生成冻结的操作单：动作、限价、数量、有效期、作废条件 |
| **记分牌 Scoreboard** | 同起点、同费用、同约束下比较「人类 / AI / 买入持有」，并对每笔真实成交复盘 |

## 与 V1 的关系

[V1 `kevinchenkai/myStock`](https://github.com/kevinchenkai/myStock) 已能采集、展示、预测次日高低区间，但账本不含费用与资金流水、人类基线被重新撮合、各策略资金口径不一致，因此还不能公平回答上面的问题。V2 是**新仓库、新数据库、账本优先**：V1 只读保留，数据一次性只读导入，代码按需拷贝复用。

## 文档导航

| 文档 | 用途 |
| --- | --- |
| [docs/V2项目 idea.md](docs/V2项目%20idea.md) | 项目负责人的原始想法 |
| [docs/PROJECT.md](docs/PROJECT.md) | 立项书（一页） |
| [docs/prd/](docs/prd/) | Codex、Claude 各自的 V2 项目书（输入材料，非现行规格） |
| [docs/plans/](docs/plans/) | **现行实施方案**（评审中） |
| [docs/README.md](docs/README.md) | 全量文档索引 |
| [AGENTS.md](AGENTS.md) / [CLAUDE.md](CLAUDE.md) | 给 Codex／Claude 的项目约定 |
| [docs/OPEN_ITEMS.md](docs/OPEN_ITEMS.md) | 跨轮次唯一待办与待决事项 |

## 隐私

仓库公开。密钥、真实账户数据、`config.yaml`、`data/`、`*.db*` 一律不提交；文档中的示例均为合成值。

## 许可证

Apache License 2.0，详见 [LICENSE](LICENSE)。
