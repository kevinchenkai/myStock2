# 代码评审第 1 轮：处置记录（gpt-6.1 与 grok-4.7）

| 项 | 内容 |
| --- | --- |
| 对象 | M1–M9、M2b、Futu 采集器、M3（不含 M3b 视图）的合成数据实现，基线 `9d969a7` |
| 评审者 | gpt-6.1（`codex exec` 只读）：[原文](code-review_gpt_r1_20261005.md)；grok-4.7（`cursor-agent` ask 模式）：[原文](code-review_grok_r1_20261005.md) |
| 结论 | gpt 26 条（F01–F26）全部处置；grok 的独有问题逐条处置，见下。全量测试与 `ruff` 通过 |
| 范围声明 | 评审只针对合成数据下的代码；未涉及真实账户、OpenD、V1 库 |

## 处置汇总

| 类别 | 处置 |
| --- | --- |
| 已修复并带回归测试 | 冲销开账进入和式（开账边界按被冲销事件类型判断）；同一 `t0` 再次开账整包冻结校验；FX 缺腿整组不生效；`correct_event` 请求内容校验；`unsettled_sell_proceeds` 只看有效事件与开账边界；Futu 手续费键含账户与订单；未知币种进待处理；跨渠道去重只哈希经济字段；`iso_utc` 固定微秒；`--now` 需环境变量放行；协议/费用/结算/名单与批次创建时不一致→拒绝（调试用 `--allow-drift`，结果不得入正式记录）；批次 E0 含库存与其他权益；`select_ticket` 整组原子选择；冻结校验（kind/state_ref_type/model_ref）；`veto` 单一复合写入者；0007 触发器冻结 `eval_run`；等（gpt 条目逐条对应测试，见各 `tests/`） |
| 本轮追加修复（grok 独有） | ① 时间止损卖出数量取整手（`floor_to_lots`），只有零股则不出单并标 `odd_lot_only`；② 证券规则未知/未核实时 `lot_sizes()` **失败关闭**（不再默认 1 股）；③ `get_daily` 终值（`ok`）优先于更晚到的 `partial`；④ 揭示早于任何合格记录（含揭示后从未补录）也标 `exposed_before_record`；⑤ 冲销 FX 腿/红利归因金额，投影按被冲销事件类型计入 `DIVIDEND_SHORTFALL` 归因；⑥ 否决输入包不再外发绝对持仓股数（改为 `holding: yes/no`），比例粗化到 2 位小数；⑦ `collect futu` 终端输出遮蔽账户号 |
| 已知局限，未修（登记） | **F04 点时重建**：`DbMarketData` 当前按「最新可得」读取，不是 `received_by` 点时读取，`eval_run` 也未记录所用证据快照 id——重算历史记分牌 run 可能与当时不同。已写入 [ADR 0002](../adr/0002-execution-protocol.md) 已知局限 5；正式评分前必须补（OPEN_ITEMS PT-01） |
| 登记待办 | `human_plan` 走冻结 ticket 路径时的 `state_ref` 对齐；`ai_lgbm` 线接入 `coach run` 的显式开关与说明（候选线未过晋级门槛，默认不启用）；否决包里 ticket 的 qty×价格仍可粗略反推账户规模（人工外发前须确认，D7） |

## 方法与限制

- 两位评审者都读不到真实数据；他们的结论只对「代码与合成测试」负责，不构成对真实数据正确性的证明。
- grok 与 gpt 的重叠发现按 gpt 编号处置，只在上表列出 grok 独有项。
