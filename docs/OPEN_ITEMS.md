# 未尽事项与待决事项

> **跨轮次的唯一待办来源。** 开新一轮前先读这里；一轮结束时把没做完的登记进来。约定见 [COLLABORATION.md](COLLABORATION.md) §4。
> 关闭一项时标记完成并写明关闭它的提交／文档，**不要删行**。
> 最后更新：2026-10-05（方案 v1.0 定稿；负责人确认 D0；gpt-6.1 七轮、grok-4.7 两轮评审已处置）。

| 状态 | 含义 |
| --- | --- |
| 待办 | 已确认，尚未动手 |
| 进行中 | 有人在做，注明谁、哪个分支 |
| ✅ 已关闭 | 写明关闭它的提交或文档 |
| 已判定不做 | 写明理由 |

## 待决事项（需项目负责人答复）

详细说明、默认值与阻塞关系（编码／真实验收／正式评分三类）见 [实施方案](plans/mystock-v2-implementation-plan_claude_20261005.md) §10。另有独立的**授权动作**：真实账户访问授权（连接富途 OpenD、读取真实账户与 V1 库），由负责人逐次给出。

| 编号 | 问题 | 状态 |
| --- | --- | --- |
| D0 | V2 与 V1 的关系：新仓库替代 V1；V1 代码锁定只读，可借用或全新生成；V2 完成后 V1→8887、V2→8888 | ✅ 已确认（负责人，2026-10-05；见方案 §3.1、§3.7） |
| D1 | `NV` 是否指 NVDA（M0a：V1 与 idea 文档均无 `NV` 写法，请确认来源） | 待答复 |
| D2 | 研究宇宙：沿用 V1 的 6 只，还是收敛到 NVDA/TSLA/0700（冻结协议前必须确定） | 待答复 |
| D3 | 基准币种、预算 `B`、开账日 `D0`（决定共同初始权益 `E0`） | 待答复 |
| D4 | 核心仓／交易仓边界；`max_weight`、`max_lots`；是否允许加仓 | 待答复 |
| D5 | 费用来源：券商对账单／接口，还是自填费用档案 | 待答复 |
| D6 | 时间止损 `max_hold_days` 与亏损承受；到期退出是否覆盖最低获利（默认覆盖） | 待答复 |
| D7 | LLM 通道：仅人工，还是同时开 API；预算上限、脱敏范围 | 待答复 |
| D8 | 公网报告：是否继续发布；是否改为私有＋白名单公开 | 待答复 |
| D9 | Python 版本与环境名（默认 3.11、`mk2`）；是否引入 hypothesis/ruff | 待答复 |
| D10 | 富途账户范围：仅实盘？含模拟盘？多账户？**券商主体/账户类型**（资金流水接口不支持 moomoo US；V1 硬编码 FUTUSECURITIES） | 待答复 |
| D11 | 评审通过的标准 | ✅ 已关闭：负责人 2026-10-05 批准第一版定稿（评审收敛情况见评审记录；最后修正未再送审，已如实登记） |
| D12 | **是否愿意在正式观察期内事前记录结构化人类计划**（只阻塞 human_plan 对照的正式比较；答「否」转描述性分支） | 待答复 |
| D13 | **大模型的角色**：首期仅「数值预测＋账户约束＋LLM 否决」，独立 LLM 预测延后为 challenger——是否同意 | 待答复 |
| D14 | 首次预先固定评审时点与显著性／多重比较方法 | 待答复 |
| D15 | 风险偏好：单日最大亏损、总体回撤容忍（可明确选择「不设执行闸」，未答复不视为同意） | 待答复 |
| D16 | V1 功能迁移取舍（[功能对照表](records/v1-feature-parity_claude_20261005.md) 已给建议）：每个 V1 页面/功能「迁移/替代/放弃」；含 ML 对照回溯页、公网报告（关联 D8）、**V1 正在累积的前向 shadow 切换时如何处理**。切换前必须确认 | 待答复（输入已就绪） |

## 方案评审

| 编号 | 事项 | 状态 |
| --- | --- | --- |
| RV-01 | gpt-6.1 评审（七轮；末轮余 1 项机械性 must-fix，v0.7.1 已修，未再送审，[原文](plans/plan-review_gpt_r7_20261005.md)）：第一轮（[原文](plans/plan-review_gpt_20261005.md)）、第二轮（[原文](plans/plan-review_gpt_r2_20261005.md)）、第三、四轮（[原文](plans/plan-review_gpt_r3_20261005.md)、[原文](plans/plan-review_gpt_r4_20261005.md)）仍「不通过」但 must-fix 逐轮收敛 16→11→1→1→0，v0.2–v0.5.1 已逐条处置（[评审记录](plans/plan-review-log_claude_20261005.md)）；第五轮（[原文](plans/plan-review_gpt_r5_20261005.md)，限定范围）结论「通过」、无新增 must-fix | 进行中 |
| RV-02 | grok-4.7 评审（经 `cursor-agent`）：首轮（[原文](plans/plan-review_grok_20261005.md)）「有条件通过」→ 确认轮（[原文](plans/plan-review_grok_r2_20261005.md)）「**通过**」，无阻塞 must-fix | ✅ 已关闭（对象为 v0.6；v0.7/v0.7.1 的改动未经 grok 复核，若负责人要求可补一轮） |

## 实施进度

| 编号 | 事项 | 状态 |
| --- | --- | --- |
| M0a | 只读预审（V1 源码与官方文档）、V1 功能对照表（D16 输入）：[审计](records/v1-audit_claude_20261005.md)、[功能对照](records/v1-feature-parity_claude_20261005.md)、[模块来源](records/module-sources_claude_20261005.md) | ✅ 已完成（方案 v1.1 §14 已吸收；官方文档部分经摘要读取，引用前须复核） |
| M1 | 地基（包骨架、core、迁移器、边界测试） | ✅ 已完成（回执 [m1-foundation](records/m1-foundation_claude_20261005.md)） |
| M0b | V1 库 schema/行数、备份演练（需授权访问 V1 运行库） | 待授权 |
| M2a | 账本（最小前向包）：核心逻辑与合成测试完成，回执 [m2a-ledger-core](records/m2a-ledger-core_claude_20261005.md)；Futu 采集与真实对账待授权与 M0a | 进行中 |
| M4 | 行情与预测基线：回执 [m4-market-baseline](records/m4-market-baseline_claude_20261005.md) | ✅ 已完成（合成数据＋公开行情冒烟） |
| M5 | 记分牌：引擎/指标/统计/持久化完成，回执 [m5-scoreboard](records/m5-scoreboard_claude_20261005.md)；页面与 CLI 待 M3/M6 | 核心完成 |
| M6 | 教练：核心/密封/暴露/冻结/选择规则与记分牌接入完成（合成端到端），回执 [m6-coach](records/m6-coach_claude_20261005.md)；**合格前向计时待 D1–D6/D12–D15 与真实账户授权** | 核心完成，待启动 |
| M7 | 复盘：卡片/回合/行为指标完成，回执 [m7-replay](records/m7-replay_claude_20261005.md)；「不操作」反事实待真实数据 | ✅ 已完成（合成数据） |
| M9 | LLM 否决（人工通道）：回执 [m9-llm-veto](records/m9-llm-veto_claude_20261005.md)；API 通道（D7）未做 | ✅ 已完成（合成数据） |
| M2b | V1 历史导入器：回执 [m2b-v1-import](records/m2b-v1-import_claude_20261005.md)；**真实导入待 M0b 授权** | 代码完成，待授权 |
| M3 | 透视（Web）：框架＋六个视图完成，回执 [m3-web-views](records/m3-web-views_claude_20261005.md)；LN-07（K 线/资金流向/公司资料）未做 | ✅ 已完成（合成数据） |
| M3-a | `snapshot_position.cost_basis` 口径：Web 按**每股**处理；V1 导入器与 Futu 采集器按每股成本价（V1/富途 `cost_price`）写入，口径一致，**真实首跑须核对** | 待核对（真实数据） |
| M3-b | 盈亏成本口径（移动平均；无证据/估算/精确三类按比例消耗；见 `ledger/pnl.py` 头注释）需负责人确认 | 待确认 |
| M8 | LightGBM+CQR 候选线：预测器与评估框架完成；探索性评估未过门槛（+0.57%，3/6），**不晋级**；回执 [m8-lgbm-cqr](records/m8-lgbm-cqr_claude_20261005.md) | 完成（负结果） |
| M10 | 交接文档：[真实数据启动指南](guides/real-data-startup_claude_20261005.md)、[切换运行手册](guides/cutover-runbook_claude_20261005.md)；**`quoted` 近实时、公开导出白名单（D8）、切换执行均未做** | 文档完成，执行待决定/授权 |
| M3b | 操作单（密封）/记分牌/复盘/数据状态视图：回执 [m3b-web-ops-views](records/m3b-web-ops-views_claude_20261005.md)；复盘「对照」栏、记分牌下钻/敏感性并列/经济增量表未做 | ✅ 已完成（合成数据） |
| CR-1 | 代码评审第 1 轮（gpt 26 条＋grok 独有项）：[处置记录](records/code-review-disposition_claude_r1_20261005.md) | ✅ 已处置（局限已登记） |
| CLI | 新增：`collect quotes/futu`、`v1 import`、`ledger open/reconcile/status`、`batch`、`protocol`、`coach`、`intent`、`veto`、`scoreboard`、`replay` | ✅ |

## 代码评审遗留（来自 [CR-1 处置记录](records/code-review-disposition_claude_r1_20261005.md)）

| 编号 | 事项 | 状态 |
| --- | --- | --- |
| PT-01 | 点时重建：引擎读取行情/汇率按 `received_by`，`eval_run` 记录证据快照 id（ADR 0002 局限 5）；**正式评分前必须完成** | 待办 |
| PT-02 | `human_plan` 冻结 ticket 路径的 `state_ref` 对齐；`ai_lgbm` 接入 `coach run` 的显式开关 | 待办 |
| PT-03 | 否决外发包里 ticket 的 qty×价格仍可粗略反推账户规模：人工外发前确认（关联 D7） | 待办 |
| PT-04 | M3b 决策点：①揭示是否只显示 `version_hashes` 覆盖到的版本；②密封期是否显示「哪只标的缺单」；③pilot 判定规则固化进协议登记 | 待负责人答复 |
