# 未尽事项与待决事项

> **跨轮次的唯一待办来源。** 开新一轮前先读这里；一轮结束时把没做完的登记进来。约定见 [COLLABORATION.md](COLLABORATION.md) §4。
> 关闭一项时标记完成并写明关闭它的提交／文档，**不要删行**。
> 最后更新：2026-10-05（方案 v1.2；全仓代码审核与 Web UI 升级见 [审核报告](records/code-review-20261005_claude_20261005.md)）。

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
| D1 | `NV` 是否指 NVDA | ✅ 已确认（负责人 2026-10-05：研究名单写明「英伟达」＝US.NVDA） |
| D2 | 研究宇宙 | ✅ 已确认（负责人 2026-10-05）：美股 英伟达 NVDA、特斯拉 TSLA、拼多多 PDD；港股 腾讯 00700、阿里 09988、康方生物 09926 |
| D3 | 基准币种、预算 `B`、开账日 `D0`（决定共同初始权益 `E0`） | 待答复 |
| D4 | 核心仓／交易仓边界；`max_weight`、`max_lots`；是否允许加仓 | 待答复 |
| D5 | 费用来源：券商对账单／接口，还是自填费用档案 | 待答复 |
| D6 | 时间止损 `max_hold_days` 与亏损承受；到期退出是否覆盖最低获利（默认覆盖） | 待答复 |
| D7 | LLM 通道：仅人工，还是同时开 API；预算上限、脱敏范围 | 待答复 |
| D8 | 公网报告：是否继续发布；是否改为私有＋白名单公开 | 待答复 |
| D9 | Python 版本与环境名（默认 3.11、`mk2`）；是否引入 hypothesis/ruff | ✅ 已关闭（负责人 2026-10-05：与 V1 共用 `mk`、Python 3.10；ruff/hypothesis 目前只在 `mk2`，是否装进 `mk` 见审核报告 Q6） |
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
| RV-01 | ✅ 已关闭（方案已获负责人批准定稿；下述为历史）gpt-6.1 评审（七轮；末轮余 1 项机械性 must-fix，v0.7.1 已修，未再送审，[原文](plans/plan-review_gpt_r7_20261005.md)）：第一轮（[原文](plans/plan-review_gpt_20261005.md)）、第二轮（[原文](plans/plan-review_gpt_r2_20261005.md)）、第三、四轮（[原文](plans/plan-review_gpt_r3_20261005.md)、[原文](plans/plan-review_gpt_r4_20261005.md)）仍「不通过」但 must-fix 逐轮收敛 16→11→1→1→0，v0.2–v0.5.1 已逐条处置（[评审记录](plans/plan-review-log_claude_20261005.md)）；第五轮（[原文](plans/plan-review_gpt_r5_20261005.md)，限定范围）结论「通过」、无新增 must-fix | ✅ 已关闭 |
| RV-02 | grok-4.7 评审（经 `cursor-agent`）：首轮（[原文](plans/plan-review_grok_20261005.md)）「有条件通过」→ 确认轮（[原文](plans/plan-review_grok_r2_20261005.md)）「**通过**」，无阻塞 must-fix | ✅ 已关闭（对象为 v0.6；v0.7/v0.7.1 的改动未经 grok 复核，若负责人要求可补一轮） |

## 实施进度

| 编号 | 事项 | 状态 |
| --- | --- | --- |
| M0a | 只读预审（V1 源码与官方文档）、V1 功能对照表（D16 输入）：[审计](records/v1-audit_claude_20261005.md)、[功能对照](records/v1-feature-parity_claude_20261005.md)、[模块来源](records/module-sources_claude_20261005.md) | ✅ 已完成（方案 v1.1 §14 已吸收；官方文档部分经摘要读取，引用前须复核） |
| M1 | 地基（包骨架、core、迁移器、边界测试） | ✅ 已完成（回执 [m1-foundation](records/m1-foundation_claude_20261005.md)） |
| M0b | V1 库只读核对与一次性导入 | ✅ 已完成（负责人 2026-10-05 授权；回执 [first-real-run](records/first-real-run_claude_20261005.md)） |
| M2a | 账本（最小前向包）：核心逻辑与合成测试完成，回执 [m2a-ledger-core](records/m2a-ledger-core_claude_20261005.md)；Futu 采集与真实对账已在首跑完成（[首跑回执](records/first-real-run_claude_20261005.md)） | ✅ 已完成 |
| M4 | 行情与预测基线：回执 [m4-market-baseline](records/m4-market-baseline_claude_20261005.md) | ✅ 已完成（合成数据＋公开行情冒烟） |
| M5 | 记分牌：引擎/指标/统计/持久化完成，回执 [m5-scoreboard](records/m5-scoreboard_claude_20261005.md)；页面（M3b）与 CLI（`scoreboard run`）已有 | 核心完成（等比较批次） |
| M6 | 教练：核心/密封/暴露/冻结/选择规则与记分牌接入完成（合成端到端），回执 [m6-coach](records/m6-coach_claude_20261005.md)；**合格前向计时待 D1–D6/D12–D15 与真实账户授权** | 核心完成，待启动 |
| M7 | 复盘：卡片/回合/行为指标完成，回执 [m7-replay](records/m7-replay_claude_20261005.md)；「不操作」反事实待真实数据 | ✅ 已完成（合成数据） |
| M9 | LLM 否决（人工通道）：回执 [m9-llm-veto](records/m9-llm-veto_claude_20261005.md)；API 通道（D7）未做 | ✅ 已完成（合成数据） |
| M2b | V1 历史导入器：回执 [m2b-v1-import](records/m2b-v1-import_claude_20261005.md)；真实导入已在 M0b 授权下完成 | ✅ 已完成 |
| M3 | 透视（Web）：框架＋六个视图完成，回执 [m3-web-views](records/m3-web-views_claude_20261005.md)；LN-07 中资金流向与公司资料已在股票详情展示存量（M3d），K 线与持续采集未做 | ✅ 已完成 |
| M3c/M3d/M3e | 预测效果视图、标的中文名与股票详情、全表翻页/券商订单/财务统计：回执 [m3c](records/m3c-forecast-view_claude_20261005.md)、[m3d](records/m3d-names-stock-detail_claude_20261005.md)、[m3e](records/m3e-pagination-orders-finance_claude_20261005.md) | ✅ 已完成 |
| UPD | 例行更新 `mystock2 update` + launchd 多时间点：[日常更新指南](guides/daily-update_claude_20261005.md) | ✅ 已上线（2026-10-05） |
| M3-a | `snapshot_position` 成本口径 | ✅ 已核实并修正（真实首跑 2026-10-05）：富途 `cost_price` 是**摊薄成本**（可为负），`average_cost` 才是平均成本；迁移 0008 新增 `average_cost`/`diluted_cost` 两列，`cost_basis` 保留为历史列；开账成本证据只用平均成本，且要求开账后该标的无成交 |
| M3-b | 盈亏成本口径（移动平均；无证据/估算/精确三类按比例消耗；见 `ledger/pnl.py` 头注释）需负责人确认 | 待确认 |
| M8 | LightGBM+CQR 候选线：预测器与评估框架完成；探索性评估未过门槛（+0.57%，3/6），**不晋级**；回执 [m8-lgbm-cqr](records/m8-lgbm-cqr_claude_20261005.md)。2026-10-06 起为 `lgbm-cqr-v2`（审核 F-02），历史样本需 `forecast run --model lgbm` 按区间重跑才有 | 完成（负结果）；v2 历史重跑待执行 |
| M10 | 交接文档：[真实数据启动指南](guides/real-data-startup_claude_20261005.md)、[切换运行手册](guides/cutover-runbook_claude_20261005.md)；**`quoted` 近实时、公开导出白名单（D8）、切换执行均未做** | 文档完成，执行待决定/授权 |
| M3b | 操作单（密封）/记分牌/复盘/数据状态视图：回执 [m3b-web-ops-views](records/m3b-web-ops-views_claude_20261005.md)；复盘「对照」栏、记分牌下钻/敏感性并列/经济增量表未做 | ✅ 已完成（合成数据） |
| CR-1 | 代码评审第 1 轮（gpt 26 条＋grok 独有项）：[处置记录](records/code-review-disposition_claude_r1_20261005.md) | ✅ 已处置（局限已登记） |
| CR-2 | 全仓审核（2026-10-05/06）：[审核报告](records/code-review-20261005_claude_20261005.md)；P0×4、P1×15 全部修复并有回归测试；Q1–Q8 已由负责人决定（Q6、Q8 不做）；迁移 0010–0012 已备份后应用到真实库 | ✅ 已处置（遗留见下表 CR2-*） |
| UI-1 | Web UI 整体升级：[盘点与回执](records/webui-upgrade-20261005_claude_20261005.md)（token 化双主题、分组导航、页头、KPI 卡、表格与窄屏两列卡片、SVG 图表重绘） | ✅ 完成；K 线图、页面时区（U2，仍 UTC）未做 |
| CLI | 新增：`collect quotes/futu`、`v1 import`、`ledger open/reconcile/status`、`batch`、`protocol`、`coach`、`intent`、`veto`、`scoreboard`、`replay`、`forecast run`、`update` | ✅ |

## 全仓审核遗留（来自 [CR-2 审核报告](records/code-review-20261005_claude_20261005.md) §9.3）

| 编号 | 事项 | 状态 |
| --- | --- | --- |
| CR2-1 | C-07 离线资金流水 JSONL 不检查覆盖区间（只在重建时用） | 待办 |
| CR2-2 | F-03 复盘不应用拆股；F-04 手数取起点；F-05 预测输入缺口检查 | 待办（涉及口径） |
| CR2-3 | 数据状态页：已停止采集的标的长期显示「落后」，页头随之「陈旧」；是否单列「停止跟踪」状态 | 已撤回，不做（负责人 2026-10-06 不关心「陈旧」：页头不再因行情落后判陈旧，见 [Web 细节调整回执](records/webui-polish_claude_20261006.md)） |
| AI-1 | 复盘卡 AI 评价：①无「上一笔/下一笔」；②自动请求没有开关；③提示词 `trade-review-v1` 效果待使用后评估；④`test_names_are_display_only…` 偶发失败待查（见 [回执 §6](records/trade-review-ai_claude_20261006.md)） | 待办（低优先级） |
| FC-1 | HK 半日市（12-24、12-31、农历新年前夕）yfinance 日线成交量为 0，LGBM 把 0 当缺失，其后约 60 个交易日拒绝预测 | ✅ 已处置（负责人 2026-10-06「富途有量就用富途，取不到就跳过」）：富途日 K 有成交量（单位与 yfinance 一致，OpenD 实测）。`collect quotes --futu-volume`（例行更新日线步骤已带）对终值日线的 0/缺失成交量用富途补，补不到原样入库；`collect volume` 修补历史（已对名单 19 行追加新版本，不改旧行）；LGBM 升 `lgbm-cqr-v3`：量比窗口跳过缺失/为 0 的日子（至少一半有效），T 日自身缺失则当日不预测；v3 历史已重跑（rebuilt 2475 条，HK 缺口补上）。富途补不到的非名单标的（如 HK.00981 等持仓）未修补 |
| FC-2 | 上述 19 行（名单内 HK 三支的半日市日线）yfinance 的 OHLC 是**平的**（开=高=低=收，且收盘价与富途不一致，如 00700 2025-12-24 yfinance 602.5、富途收 603、开 598），成交量已用富途补上但价格没动。后果：这 19 天本身不能出 LGBM 预测（区间为 0）、其前一日的标签与这些日的评估会有轻微失真；基线预测同样用了这些价 | 待决定（可用富途同日 OHLC 修这 19 行，需决定是否让富途价覆盖 yfinance 价） |
| CR2-4 | U-11：进程被 SIGKILL 时运行回执停在 running（数据状态页超过 1 小时标问题） | 已知局限 |
| CR2-5 | 报告 §9.3 所列 P3 其余项（采集、CLI、预测、Web、测试） | 待办（低优先级） |
| CR2-6 | 若真实库曾以 `…:N/A` 为键入账过资金流水，C-02 修复后同样流水会进待匹配：`ledger status` 看待匹配数 | 待核实 |

## 代码评审遗留（来自 [CR-1 处置记录](records/code-review-disposition_claude_r1_20261005.md)）

| 编号 | 事项 | 状态 |
| --- | --- | --- |
| PT-01 | 点时重建：引擎读取行情/汇率按 `received_by`，`eval_run` 记录证据快照 id（ADR 0002 局限 5）；**正式评分前必须完成** | 待办 |
| PT-02 | `human_plan` 冻结 ticket 路径的 `state_ref` 对齐；`ai_lgbm` 接入 `coach run` 的显式开关 | 待办 |
| PT-03 | 否决外发包里 ticket 的 qty×价格仍可粗略反推账户规模：人工外发前确认（关联 D7） | 待办 |
| PT-04 | M3b 决策点：①揭示是否只显示 `version_hashes` 覆盖到的版本；②密封期是否显示「哪只标的缺单」；③pilot 判定规则固化进协议登记 | 待负责人答复 |
| FR-1 | 真实首跑（OpenD 只读采集、V1 导入、行情/预测回填）：[回执](records/first-real-run_claude_20261005.md)；首跑发现并修复的问题见回执 | ✅ 完成（历史回填；前向计时未启动） |
| FR-2 | 开账成本 | ✅ 已处置（负责人 2026-10-05「按推荐处理」）：用券商**平均成本**作开账成本证据（标「估算」）；摊薄成本单列展示、不当买入成本；开账后有成交的标的不用后到快照的成本（不猜） |
| FR-3 | 港股资金流水/股息的备注格式未核实（港股股息暂进待匹配队列）；接口无除息日，股息应收与到账同日 | 待核实（等首次港股派息出现） |
| FR-4 | M3c 预测效果视图：[回执](records/m3c-forecast-view_claude_20261005.md) | ✅ 已处置（负责人 2026-10-05）：事后重建（rebuilt）的预测不密封；前向（forward）预测仍按 §6A.2 密封。rebuilt 的「新鲜」只代表刚重建过 |
| FR-5 | 资金流水 `其他`（港币 4 笔转出＝提现；美元 2024-11-06 转入 100000、2024-12-31 转出 10000）| ✅ 已确认（负责人 2026-10-05）；美元期初现金残差 −13.85（账户升级前后小额未入账项）待查，不影响现金水平判断；2026-10-05 起设为对账基线（`reconcile.known_cash_diffs`，现差额 −13.84），偏离才报警 |
| FR-6 | 倒推开账（2024-10-20）：富途账户于 2024-10-21 升级（`资产迁移` 现金转入、持仓以实物迁入）。期初现金≈0（HKD 0、USD −13.85），现金曲线的水平可信；期初持仓来自迁入的股票，**无成本证据** ⇒ 来自它们的卖出盈亏「不可用」。若有旧账户的成本记录可导入改善 | 部分关闭（现金已可信；期初成本待决定） |
