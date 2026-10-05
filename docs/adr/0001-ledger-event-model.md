# ADR 0001：账本事件模型（M2a）

- 状态：已采纳（2026-10-05，随 M2a 核心落地）
- 依据：实施方案 v1.0 §5 M2a、§6 账本不变量、§8.2 T-01/03/04/06/17–22
- 范围：事件、来源证据、更正、多腿事件、开账、拆股与股息、对账、费用、结算。采集器（Futu、V1 导入）不在本 ADR。

## 决定

1. **两层结构**：`source_record`（来源证据，不可变）与 `ledger_event`（规范业务事件，只追加），经 `source_link` 多对多。规范键 `business_key`＝`类型:账户:成交/流水身份`，**不含来源渠道**，故 V1/Futu/CSV 三路到达同一成交自然归并为一个事件（T-17）。身份不足（缺 deal_id 等）抛 `IdentityInsufficient`，由调用方 `queue_pending`，不入账。
2. **追加语义由库层保证**：账本与证据类表有 `BEFORE UPDATE/DELETE` 触发器；写入权限由 `TABLE_OWNERS` 授权器限定（仅 `ledger` 写账本表）。
3. **三种身份**（内容哈希只覆盖「经济字段」，辅助元数据差异不算冲突；2026-10-05 M2b 补充）：`business_key`（跨版本稳定）、`event_version`（事件版本，`event_id=key#version`）、`correction_request_id`（更正的幂等键）。同键同版本同内容的重复到达＝`duplicate`；同键同版本内容不同＝`LedgerConflict`。
4. **更正**＝同一事务内追加 `REVERSAL`（取反**当前有效版本**，`note` 记录 `reverses TYPE#version`）＋新版本事件；取消＝只追加 REVERSAL。同一旧版本不得被冲销两次（部分唯一索引 `uq_reversal_once`）。有效版本＝最后版本且非 REVERSAL；有效排序＝`(event_at, business_key, event_version)`。
5. **开账**：`account_opening.opening_at=t0`；期初以显式 `OPENING_POSITION/OPENING_CASH` 事件进入和式；`event_at ≤ t0` 的其他事件为 `pre_opening`，只计数不参与和式。开账点一经写入不可改，相同内容重复调用幂等。
6. **拆股**不是数量增量：`corporate_action` 保存比例与生效时点；持仓＝Σ（原始数量 × Π 生效时点在该事件之后的拆股因子）。同一时刻先应用公司行动：因子只作用于 `effective_at > event_at` 的事件。合股产生的非整数持仓在投影中以 `fractional_position:<code>` 告警，**不静默取整**。
7. **股息**：除息日 `DIVIDEND_ACCRUAL` 形成应收总额 G；支付日 `DIVIDEND_PAYMENT` 结清全部 G。预扣税是独立 `TAX`（唯一的现金扣减）；仅有净额 N 时：现金＋N、应收−G、差额记 `DIVIDEND_SHORTFALL`（非现金，`cash_delta=0`），不再扣现金。三情形合成例（G=100、W=10）期末应收均 0、现金净增 90、支付日权益变化 −10（测试逐一断言）。
8. **FX 多腿**以 `group_id`+`leg_id` 原子写入；`incomplete_fx_groups` 检出缺腿（有效腿须恰好两条、币种不同、方向相反）。
9. **ADJUST**必须带 `adjust_class`（EXTERNAL_FLOW/INVESTMENT/OTHER）与原因；仅 EXTERNAL_FLOW 计入外部资金流。DEPOSIT/WITHDRAW 恒计外部流（含被更正后的冲销）。
10. **FILL 校验**：必须带正价；`cash_delta = −qty×price`（容差 0.01，成交额按币种最小单位舍入）；币种须与标的币种一致；费用不含在内。
11. **费用估算与实际分列**：`fees.estimate` 只用于共同的模型化口径，不写账本；聚合层级 `order`（最低费只收一次）/`fill`（逐笔）；无适用档案时报错而不是 0。
12. **结算**：`SettlementRule.lag_sessions=None` 表示未核实，计算时报 `SettlementUnknown`（失败关闭）；T+N 由 M0a 核实后配置，按交易所日历推进。可交易现金＝经济现金 − 未结算卖出回款 − 预留；不把融资买力当自有现金。

## 实施方案要求的「ADR 必答」问题的回答

| 问题 | 回答 |
| --- | --- |
| 业务键由什么构成 | `类型:账户:成交/流水身份`，不含来源 |
| 事件有效排序 | `(event_at, business_key, event_version)`；和式与顺序无关（属性测试覆盖插入顺序无关） |
| 开账快照边界 | `event_at ≤ t0` 为 pre_opening；快照 `captured_at` 与 `opening_at` 分开存放 |
| 碎股/合股 | 告警不取整；现金替代留待 M0a 核实券商处理方式 |

## 未决 / 后续

- Futu 采集器与字段映射（需真实账户访问授权与 M0a 的接口核验）；资金流水接口的「单一记账来源」实现（WP2.3 的采集侧）。
- V1 历史导入（M2b）。
- `settlement_state` 目前是**派生计算**（`ledger/settlement.py`），未建表；若 M5 需要持久化再加迁移。
- 对账阈值（D5/M0）与「差异可解释」的人工回执格式。
