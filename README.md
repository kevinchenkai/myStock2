# myStock2

个人 **港股 / 美股** 的「账本 · 透视 · 教练 · 记分牌」系统（myStock 第二版）。

让 Web 随时看清自己的账户，让 AI 每天给出下一交易日的**操作单**，并在同口径下公平地检验：**AI 指导的操作，是否比我凭直觉的操作赚得更多**。

> **状态（2026-10-05）：实施方案 v1.2；M1–M9 在合成数据上完成，真实数据首跑、历史回填与例行更新已上线；正式前向比较尚未启动（等待 D3–D6、D12–D15）。** 系统**不自动下单**，不构成投资建议。

## 四个子系统

| 子系统 | 一句话 |
| --- | --- |
| **账本 Ledger** | 账户事实的唯一真相：成交、费用、资金流水，可对账，只追加 |
| **透视 Lens** | 只读 Web 视图集合：持仓、交易、盈亏、资产趋势、外汇…新增视图 = 新增一个文件夹 |
| **教练 Coach** | 每天对圈定标的（如 NVDA、TSLA、0700）生成冻结的操作单：动作、限价、数量、有效期、作废条件 |
| **记分牌 Scoreboard** | 同起点、同费用、同约束下比较「人类 / AI / 买入持有」，并对每笔真实成交复盘 |

## 快速开始（开发）

```bash
# 与 V1 共用默认环境 mk（Python 3.10，已含全部依赖）：在仓库根目录直接运行，无需切换环境或安装
conda activate mk                        # 若默认环境已是 mk 则不需要
cp config.example.yaml config.yaml       # 私有配置，已被 .gitignore；Web 开发期端口 8889
python -m mystock2 db migrate            # 建库（data/mystock2.db，已被忽略）
python -m mystock2 universe check --file config/universe.example.yaml
python -m pytest -q && python scripts/check_docs.py
/opt/anaconda3/envs/mk2/bin/python -m ruff check .   # ruff 不在 mk 里（mk 也没有 hypothesis，属性测试会自动跳过）
```

## 与 V1 的关系

[V1 `kevinchenkai/myStock`](https://github.com/kevinchenkai/myStock) 已能采集、展示、预测次日高低区间，但账本不含费用与资金流水、人类基线被重新撮合、各策略资金口径不一致，因此还不能公平回答上面的问题。V2 是**新仓库、新数据库、账本优先**，**将替代 V1**：V1 代码已锁定只读、在 V2 完成前继续在 8888 运行；数据一次性只读导入，代码可借用也可全新生成。开发期 V2 用 8889；完成后 V1 切到 8887 过渡数天，V2 用 8888（[切换计划](docs/plans/mystock-v2-implementation-plan_claude_20261005.md)）。

## 文档导航

| 文档 | 用途 |
| --- | --- |
| [docs/V2项目 idea.md](docs/V2项目%20idea.md) | 项目负责人的原始想法 |
| [docs/PROJECT.md](docs/PROJECT.md) | 立项书（一页） |
| [docs/prd/](docs/prd/) | Codex、Claude 各自的 V2 项目书（输入材料，非现行规格） |
| [docs/plans/](docs/plans/) | **现行实施方案**（v1.2） |
| [docs/README.md](docs/README.md) | 全量文档索引 |
| [AGENTS.md](AGENTS.md) / [CLAUDE.md](CLAUDE.md) | 给 Codex／Claude 的项目约定 |
| [docs/OPEN_ITEMS.md](docs/OPEN_ITEMS.md) | 跨轮次唯一待办与待决事项 |

## 隐私

仓库公开。密钥、真实账户数据、`config.yaml`、`data/`、`*.db*` 一律不提交；文档中的示例均为合成值。

## 许可证

Apache License 2.0，详见 [LICENSE](LICENSE)。
