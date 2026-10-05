账本的只追加、成交幂等冲突、严格穿越撮合和否决 JSON 的越权拒绝是真做了的，`coach run` 的标准输出也没有直接打印动作或限价。但开账更正、点时行情和密封时间戳这几处会改写和式或比较资格，现在还不能认为实施方案 §6、§6A 和两份 ADR 已经落地。最严重的是：期初事件的冲销被 `pre_opening` 过滤器丢掉，更正或取消开账后重建结果与有效版本不一致。模拟侧的权益没有应收/应付，`simulate_line` 读的是库里的最新行情而不是决策当时能看到的版本。本评审未看 `mystock2/web/`。

## 问题表

| 严重度 | 位置 | 问题 | 触发场景 | 建议修复 |
| --- | --- | --- | --- | --- |
| critical | `mystock2/ledger/projection.py:79` | 冲销开账不进入和式。`event_at ≤ t0` 且类型不是 `OPENING_*` 的事件被整行跳过；`correct_event` 生成的 `REVERSAL` 沿用原 `event_at`（`events.py:311-320`），因此开账冲销被丢掉，原期初行仍计入。取消开账后持仓/现金不变；把期初 10 股更正为 12 股会得到 22。 | 对 `OPENING_POSITION` / `OPENING_CASH` 调用 `correct_event`（含 `new_draft=None` 的取消），再 `project()`。 | 冲销按被冲销事件的类型判断是否属于开账，期初更正必须抵消当前有效期初；加一条「更正两次 / 取消一次」后的和式断言。 |
| high | `mystock2/ledger/opening.py:46` | 同一 `t0` 的再次 `record_opening` 可以追加新标的或新币种。只拒绝不同的 `opening_at`；已有键走 `post_event` 幂等，新键直接插入。开账集合只能变大。 | 第一次只开 NVDA，第二次用同一 `t0` 再传入 `HK.00700`。 | 第二次调用必须与已入库的期初集合完全一致，否则拒绝；不要只做逐键幂等。 |
| high | `mystock2/ledger/settlement.py:42` | 未结算卖出回款把所有 `FILL` 且数量为负的行加总，不看有效版本、不减 `REVERSAL`、不排除 `pre_opening`。更正或取消后，可交易现金被多扣已失效的回款。 | 卖出成交入账后 `correct_event` 改数量或取消，再算 `unsettled_sell_proceeds`。 | 只累加每个 `business_key` 的当前有效版本，并套用与 `project()` 相同的开账边界。 |
| high | `mystock2/ledger/projection.py:88` | 缺腿的 `FX` 仍计入现金。`post_fx` 写的时候是原子的，但 `post_event` 可以只写一条腿；`incomplete_fx_groups`（`projection.py:114`）只报告，和式不排除。直接 `post_event` 一条 `FX` 腿后，该币种现金已变，对账虽会失败，账本和式已经生效。 | 不经 `post_fx`，只 `post_event` 一条 `FX` 腿；或只冲销两腿中的一条。 | 和式中缺腿组的现金效果为零（或拒绝提交不完整组），不要只在对账报告里列出来。 |
| high | `mystock2/ledger/projection.py:97` | `DIVIDEND_SHORTFALL` 的 `attrib_amount` 只在原类型上累加。冲销把 `attrib_amount` 写成 `None`（`events.py:317`），且冲销行的类型是 `REVERSAL`，差额不会被扣回。更正一笔 shortfall 后，旧差额和新差额叠在一起。 | 对 `:shortfall` 事件做 `correct_event`，再看 `attributed_shortfall`。 | 冲销时取反 `attrib_amount`，投影按冲销链净额计入。 |
| high | `mystock2/market/bars.py:73` | 日线内容哈希不含 `quality`。先写入 `quality='partial'` 后，收盘后 OHLC 相同的终值被当成 duplicate 丢掉，行永远停在 `partial`。`generate` 只接受 `ok`，该日预测和操作单不再产生；持仓估值会缺收盘价并进入 `UNKNOWN`。 | 收盘前 `collect quotes` 一次，收盘后再采集同一价格。 | 把 `quality` 纳入版本判定，或允许同内容把 `partial` 升级为 `ok` 并保留原版本。 |
| high | `mystock2/scoreboard/marketdata.py:24` | `hourly` / `close` 没有 `received_at` 截止，也不绑定证据快照。`simulate_line`（`ops.py:144`）和 `state_at_open`（`ops.py:165`）因此用「现在库里的最新 bar」重算历史成交；`coach run` 取预测收盘价的查询同样不按截止过滤（`ops.py:281`）。`scoreboard run` 写入的 `evidence` 恒为 `[]`（`ops.py:415`）。事后修订行情会改变早已冻结的操作单的模拟库存。 | 用 `--now` 重放较早的 `coach run`，或在评分后修订某日小时线再 `scoreboard run`。 | 模拟和决策只读 `received_at ≤` 该日决策截止（或该 run 绑定的快照版本）；run 记下快照 id。 |
| high | `mystock2/scoreboard/lines.py:92` | 买入持有缺 bar 时直接跳过该标的，当日仍可记 `OK`。市价单只要有任意一根 bar 就按第一根的开盘价全部成交，且不看 `complete`（`matcher.py:47-51`）。开盘缺口时会用稍后的 bar 开盘价建仓，并当成已知成交。 | 首个建仓日没有小时线，或第一根 bar 晚于开盘超过 1 分钟。 | 没有覆盖开盘的完整 bar 时该日 `UNKNOWN` 并暂停；禁止用缺失数据换成「不买」。 |
| high | `mystock2/cli/ops.py:209` | 批次权益 `E0 = 现金 + 数量 × D0 收盘价`。`LineState`（`types.py:45`）没有应收、应付、成本批次以外的字段；初始库存的单位成本用 D0 收盘价估算。方案里的例子（现金 1000、库存 1000、应收 100 → `E0=2100`、开账 `R=0`）这条路径做不到；`min_gain` / 时间止损会用错成本。 | `batch create --positions` 建批次，且开账快照含应收或真实成本 ≠ 收盘价。 | 状态包纳入逐币种应收应付和真实成本批次；`E0` 用未复权价按 §6A.5 计算，缺价格就拒绝建批。 |
| high | `mystock2/coach/intents.py:72` | `seen_ai` / `late_record` 由调用方传入的 `now` 决定，选择时再拿 `recorded_at` 和 `revealed_at` 比（`intents.py:149`）。`intent add --now`、`coach show --now` 都把这个时间写入账（`ops.py:363`、`ops.py:321`）。看过 AI 之后把记录时间改到揭示之前，计划会以 `seen_ai=0` 进入 `human_plan`；第一次揭示若写成很晚的时间，之后的真实记录都会落在「揭示前」。 | `coach show` 之后 `intent add --now <revealed_at`；或第一次 `coach show --now` 设到未来。 | 正式命令只用本机时钟，拒绝倒填；选择时用写入事务里重新读取的首次揭示时间，不要信任客户端时间戳。 |
| high | `mystock2/cli/ops.py:134` | 证券规则未知或 `lot_size` 为空时，模拟手数默认为 1。撮合因此接受任意整数股。`decide` 本身会因规则未知而 SKIP，但这条默认值把「未知」变成了可执行手数。 | 规则过期或未录入后跑 `scoreboard run` / `buyhold`。 | 规则未知则该日 `UNKNOWN` 或拒绝模拟，不要默认 1。 |
| high | `mystock2/coach/decide.py:135` | 时间止损卖出数量是 `int(持仓)`，没有按手数向下取整。持仓不是整手时，引擎以 `bad_lot` 拒绝整单（`engine.py:95`），到期退出没有发生，次日也不会自动改成可卖的整手。 | 合股或碎股后持仓 50、手数 100，且持有天数已超过 `max_hold_days`。 | 与普通卖单一样 `floor_to_lots`；一手都不到就记明确的 `odd_lot_only`，不要生成会被撮合整单拒绝的数量。 |
| high | `mystock2/cli/ops.py:458` | 否决包用 `cash_pct = cash/equity`、`exposure_pct = 数量×收盘价/equity`，同时给出 `position_qty`。持仓非零且收盘价可知时，权益和现金可以反解出来，外发的是绝对金额。CLI 文案写的是不含绝对金额。 | 导出时该线持有 100 股、收盘价已知、`exposure_pct=0.5`。 | 外发前去掉可反解的组合（例如只留档位或分桶），并加测试：由包内字段不能还原现金和权益。 |
| medium | `mystock2/assistant/veto.py:277` | 导入否决时用的是「导入当下」`state_at_open` 的哈希，不是基础单冻结时的 `state_ref`。基础单因持仓变化本应作废时，新版本却绑上新状态，旧决策重新变成有效单。另外先 `freeze_tickets` 再写 `llm_call`（`veto.py:274-283`），日志没写上时重放检查不成立，另一份不同响应还能再冻一版。 | `coach run` 之后行情修订改变了线内状态，再 `veto import`；或冻结成功、写 `llm_call` 失败后换一份 JSON 再导入。 | 新版本沿用基础单的 `state_ref`，对不上当前状态就拒绝导入；`applied` 与冻结放在同一失败语义下，或先占位再冻结。 |
| medium | `mystock2/ledger/events.py:200` | 内容哈希不含 `ref_deal_id` / `ref_event_key`。同一 `business_key`、同一金额、不同归属的费用或税，后到的被当成 duplicate，不报 `LedgerConflict`。先写入的错误归属会留下。同一 `correction_request_id` 换一份不同的 `new_draft` 也直接返回第一次的结果（`events.py:299`），不核对经济内容。 | 两路费用金额相同但 `ref_deal_id` 不同；或更正请求 id 被复用且数量不同。 | 归属字段进入冲突比较；幂等命中时校验新内容与已写入版本一致，不一致则报冲突。 |
| medium | `mystock2/scoreboard/engine.py:146` | 买单只扣 `fee`，不扣 `tax`；现金预留也只用费用（`engine.py:112`）。`side=ANY` 且 `tax_pct>0` 的档案（港股印花税若写在这一栏）会使买入成本偏小、可买数量偏大。 | 费用档案 `tax_pct` 非 0，且方向是 `BUY` 或 `ANY`。 | 预留和成交都扣 `fee+tax`；卖出已经这么做了。 |
| medium | `mystock2/ledger/events.py:347` | 除息计提基数没有实现。`post_dividend` 按调用方给的总额入账，不看除息日前最后 cum 收盘持仓。除息日买入仍可被记应收，除息日卖出也可以被漏记。`DIVIDEND_PAYMENT` 也不检查 `|recv_delta|` 是否结清同组应收。 | 直接 `post_event` 一笔与持仓无关的 `DIVIDEND_ACCRUAL`，或支付只冲掉部分应收。 | 计提必须由投影持仓算出 G；支付必须一次结清该组剩余应收，否则拒绝。 |
| medium | `mystock2/coach/intents.py:153` | 揭示后若从未补录，正式计划只有 `plan_missing`，没有 `exposed_before_record`。该标志只在「已经有一条 `late_record`」时才出现。另外 `select_human_plan` 不比较意图上的 `state_hash` 与当日线内状态，持仓已变的计划仍会下单。 | 只 `coach show` / 导出否决包，不再 `intent add`；或记录计划后线内库存因修订而变化。 | 只要首次揭示早于任何合格记录就标 `exposed_before_record`；状态哈希不一致则该日无订单并记原因。 |
| medium | `mystock2/market/bars.py:103` | `get_daily` 按 `version` 取每个交易日最后一行，版本号按来源各自从 1 起。备用源的 v1 可以盖过主源更高版本；`quality='partial'` 的更高版本也会把同日更早的 `ok` 挤掉，预测侧再滤掉 `partial` 后这一天就变成没有行情。 | 同一 `code+session` 先后写入两个 `source`，或终值之后又写入一条 partial。 | 点时选择规则改为「截止前、指定来源、`ok` 中的最高版本」，不要跨来源比 version。 |
| low | `mystock2/cli/ops.py:569` | `collect futu` 把完整报告打到标准输出。`LedgerConflict` 文本含 `fill:{account_id}:...`，`failed_scopes` 还会进入 `run_log`。账号标识会出现在回执里。 | 重复采集导致内容冲突，或 OpenD 返回错误。 | 回执只留冲突计数和 deal 身份的哈希；账号号不要进入 `run_log` 和标准输出。 |

未发现范围内用字符串拼接用户输入去拼 SQL 的路径（`ledger_event` 的列名来自常量）。`coach run` 的回执只写张数和 `pilot`，这一点与密封要求一致。多个写连接在当前命令里是先后提交的，没有看到同一线程里互相 `BEGIN IMMEDIATE` 的死锁；残留风险是上面的否决两段写入。

## 与方案 / ADR 不一致

- §6 不变量 1、7：开账更正后的和式不等于「期初有效版本 + 此后事件」。
- §6 不变量 4：三种支付情形在 `post_dividend` 里有对应实现，但计提基数（除息日买入不计、当日卖出仍计）没有实现。
- §6 不变量 8：缺一腿 FX 被检测，现金和式仍然计入。
- ADR 0001：内容哈希不含归属字段，和「费用必须能归到成交」冲突；这是按 ADR 写的，留下上面的静默 duplicate。
- §6A.4：批次状态包没有应收应付，初始成本用收盘价估算；未完成订单的预留在日终释放，这一点与「新批次不延续未完成订单」接近，但成本批次复制不完整。
- §6A.5：`E` 含应收减应付。引擎权益是现金（含未结算回款）加未复权市值（`engine.py:180`），与 ADR 0002 的估值句一致，与 §6A.5 的 `E0` 例子不一致。`test_t33` 用手工权益 2100 断言 `R=0`，没有走引擎或 `batch create`。
- §6A.8 写 buyhold 在 `D0` 建仓；ADR 0002 写 `D0` 之后首个交易日。代码用 `next_session(start)`（`ops.py:155`），跟的是 ADR。初始状态里已有的库存不会按权重再平衡，只会用剩余现金加仓。
- ADR 0002：股息不进模拟线，这是写明的局限；因此模拟比较相对少持仓的线偏多，代码没有另做披露字段。
- §6A.2：`exposed_before_record` 的实现窄于「揭示前没有任何记录」。
- §6A.3：`human_plan` 的正式路径是 `HumanPlanProvider` 读意图，不读已冻结票据，也不做 `state_ref` 失效判断。
- §6A.6 / ADR 0002：`UNKNOWN` 之后引擎会 `PAUSED` 且不删日；恢复没有「新证据快照版本」这一步，重算直接读最新行情。
- `ai_lgbm` 可以出现在批次里，`coach run` 只为 `ai` 和 `ai_veto` 生成基础单（`ops.py:291`），两条线用的都是 baseline。

## 最值得补的 5 个测试

1. 开账更正与取消：期初 10 股更正为 12、再取消，`project()` 分别为 12 和 0；同一 `t0` 第二次多传一个标的必须拒绝。
2. 卖出成交更正/冲销后，`unsettled_sell_proceeds` 只剩有效版本；只写入一条 FX 腿时，该币种现金和式不变，且 `incomplete_fx_groups` 非空。
3. 日线先 `partial` 再写入相同 OHLC 的终值后，`quality` 必须成为 `ok`；`simulate_line` / `close` 在 `received_by` 早于修订版本时不得使用修订价。
4. `coach show` 之后用更早的 `--now` 做 `intent add`，结果必须是 `seen_ai=1` 且 `select_human_plan` 仍为无订单；第一次揭示时间晚于真实记录时间时，不得把已看到的单算作未暴露。
5. 买入持有首日缺小时线或开盘有缺口时状态为 `UNKNOWN` 而不是 `OK` 且零成交；时间止损在持仓非整手时不得整单消失；现金 1000、库存市值 1000、应收 100 的批次 `E0=2100` 且首日 `R=0`。
