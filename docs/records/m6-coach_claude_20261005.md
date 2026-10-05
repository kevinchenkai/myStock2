# M6 教练 阶段回执

| 项 | 内容 |
| --- | --- |
| 里程碑 | M6（实施方案 §5 M6）——**教练核心、密封与暴露、记分牌接入已完成（合成数据端到端）；真实数据启动与协议冻结待负责人答复** |
| 基线 | `main` @ `4b5e8e3`（M5）→ 本回执所在提交见 Git |
| 状态 | 迁移 `0005_coach.sql`；全链路集成测试通过；**未开始任何合格前向计时**（未冻结真实协议、未授权真实账户） |
| 验证 | `decide`/票据/意图/CLI 相关测试通过；集成测试覆盖「批次→协议→教练密封→意图/暴露→冻结→记分牌（CLI 数值＝库内数值）」 |

## 做了什么

| WP | 产出 | 位置 |
| --- | --- | --- |
| 6.1 | universe 校验（M1）＋决策层按名单 tier/`max_weight`/`max_lots`/`pending_confirmation` 约束；缺参数不出可执行数量 | `instruments/universe.py`、`coach/decide.py` |
| 6.2 | `decide()` 纯函数：边际门槛（区间宽度 vs 往返成本×k）、买单限价与数量（预算切片、可交易现金、`max_weight`、`max_lots`、整手、合法 tick、预估费用入预算）、卖单（`min_gain` 地板、不可达则 HOLD）、**时间止损优先于 min_gain**、共享预算按优先顺序预留；缺预测/缺规则 → SKIP＋原因码 | `coach/decide.py` |
| 6.3 | 操作单冻结：内容哈希、重跑 no-op、变化＝新版本并 `supersedes`、`state_ref`（线内状态哈希）、`visible_at`；触发器禁止改/删 | `coach/tickets.py`、迁移 0005 |
| 6.4 | **唯一选择规则**：单元＝(批次, 线, kind, 市场, 目标日, 标的)；采用「截止前最后一个成功冻结且 visible_at ≤ 截止」；`state_ref` 与当前状态不符→失效（无订单、不冒充新单）；盘前刷新 unavailable 时回退到仍有效的较早版本 | `coach/tickets.py`、`scoreboard/providers.py` |
| 6.5 | 截止：冻结晚于项目截止→`missed_deadline` 的 SKIP，不回填；缺关键数据→`unavailable`；覆盖率统计（CO-02） | `coach/tickets.py` |
| 6.6 | **人类计划/暴露/密封**：结构化意图（约束处理：截断或拒绝，由协议选择）、暴露日志、首次揭示即锁定此前最后一个合格计划、揭示后修改 `seen_ai=1`/`late_record=1` 且不进入 `human_plan`、缺失＝无订单（`plan_missing`），揭示前无记录→`exposed_before_record` | `coach/intents.py` |
| 6.7 | CLI：`batch create`、`protocol freeze`（缺必填项拒绝；`--pilot` 登记）、`coach run`（**只输出回执与计数，不打印动作/限价/数量**）、`coach show`（受控揭示并写暴露日志）、`coach status`（不含动作）、`intent state/add/freeze`、`scoreboard run`（新 run 不覆盖，数值与库一致） | `cli/ops.py` |
| 6.8 | 协议冻结登记表 `protocol_freeze`（真实协议只在本地私有；库里只存哈希与白名单摘要） | 迁移 0005、`cli/ops.py` |
| 接入 | `DbMarketData`（小时线、会话完整性含港股午休间隙、未复权收盘价）；`TicketProvider`/`HumanPlanProvider` 把冻结单/人类计划喂给 M5 引擎 | `scoreboard/marketdata.py`、`providers.py` |

## 验证（T-ID）

T-02（共享预算按优先顺序）、T-09（截止后完成的单不进入正式记录）、T-13/T-39（首次揭示锁定、揭示后修改不进入 human_plan、`seen_ai`）、T-14（重跑/刷新不覆盖已冻结单）、T-26（时间止损不受最低获利约束）、T-27（唯一选择、截止后版本被拒、状态变化失效）、T-28（预算越界被截断）、T-35（超卖/超预算按协议截断或拒绝）、CO-02（覆盖率含 SKIP 与缺失）、SB-01（CLI 与库内同一数值）。

## 偏差与说明（如实）

1. **`live_guidance`（真实账户状态的指导单）未实现**：需要把账本投影转成线内状态（持有天数、成本等需要真实成交/快照），依赖真实账户访问授权与 M2a 采集，留待授权后实现；CLI 的 `coach show --live` 参数已预留。
2. **`ai_veto`/`ai_lgbm` 线**未接入 `coach run`（M8/M9）；`coach run` 目前只为 `ai` 线出单。
3. `coach run` 的预测用基线预测器；其 α、窗口等参数默认值是占位（M4 回执已说明），冻结协议时须由负责人确认。
4. 买入持有权重默认为交易仓标的等权，可由协议 `buyhold.weights` 预注册覆盖。
5. 批次初始库存 `--positions` 的单位成本在无快照成本时**估计为 D0 未复权收盘价**，并在批次元数据里标 `cost_estimated:<code>`（影响 `min_gain` 地板与持有天数的起点）。
6. 本机 `config/local/` 没有真实协议/名单/费用档案；所有端到端测试使用临时目录里的合成配置。

## 没做什么（边界）

- **未启动任何合格前向计时**：D1–D6、D12–D15 未答复，真实协议未冻结，未获得真实账户访问授权。
- 未连接富途 OpenD 或任何券商接口；未读取真实账户数据；未读写 V1 数据库/运行目录；未改 V1 文件；未重启 V1；未碰 8888；未联网；未向外部模型发送内容；无调度；无交易。
