# M0a 只读预审：V1 源码/文档与官方文档核验

| 项 | 内容 |
| --- | --- |
| 作者 | Claude（子任务，执行实施方案 §5「M0a」WP 0a.1–0a.5） |
| 日期 | 2026-10-05（America/Los_Angeles；官方文档读取日同为 2026-10-05） |
| V1 基线 | `ca16e31ad8f78b23e2ceabbd58dba0a01b55e35e`（`git -C <V1> rev-parse HEAD`）。**读取的是工作树文件**；未运行 `git status/diff`，故未核验工作树是否含未提交改动 |
| V2 基线 | 方案 v1.0 头部记载 `main` @ `504d3fc`（v0.7.1）。V2 目录当前不是 git 仓库（环境信息），本任务未执行任何 git 命令，**未核验**该 SHA |
| 状态 | 完成，待负责人/执笔方采纳；结论不改动方案正文，修订建议见 §7 |
| 同批产出 | [v1-feature-parity_claude_20261005.md](v1-feature-parity_claude_20261005.md)（WP 0a.5 / D16）、[module-sources_claude_20261005.md](module-sources_claude_20261005.md)（借用 vs 重写） |

## 0 边界：做了什么、明确没做什么（COLLABORATION §5 逐条表态）

| 事项 | 表态 |
| --- | --- |
| 读过 V1 数据库或 `data/` 下任何文件 | **否**。未打开、未查询、未列出 `data/` 内容。注意：读了 V1 已入库的文档 `docs/guides/data-dictionary_claude_20260623.md`，其中含 2026-06-22 的历史统计（文档文字，不是本次读库）；引用处均标明「文档称」 |
| 读过 V1 `config.yaml` | **否**。只读 `config.example.yaml` 与 `mystock/config.py`（`ls -la` 在仓库根目录列出过该文件的存在与大小，未读内容） |
| 运行过 V1 代码/脚本 | **否**。只用读文件与 grep；未执行 `*.sh`、`python -m mystock…`、测试 |
| 连接富途 OpenD 或任何券商接口 | **否** |
| 修改过 V1 任何文件 | **否** |
| 对 V1 执行的 git 命令 | 仅 `git rev-parse HEAD` 与 `git ls-files`（只读，列出已跟踪文件名）。无 add/commit/push/status/log/diff |
| 对 V2 执行 git 命令 | **无** |
| V2 文件改动 | 仅新增上表三个文件；未改动其他文件（`docs/records/` 下已有的 `m1-foundation_claude_20261005.md` 不是本任务产出） |
| 联网与外发 | 只读公开页面（openapi.futunn.com、hkex.com.hk、sec.gov、nyse.com、ranaroussi.github.io/yfinance、raw.githubusercontent.com 的 yfinance 源码，及少量二手来源：charltonslaw.com、thestandard.com.hk、搜索引擎摘要）。未发送任何本机私有信息 |
| **方法限制（影响可信度）** | 网页经 `WebFetch` 由小模型摘要后返回，**不是原文逐字阅读**；表格中「已核实」＝官方页面摘要与多处来源一致，仍建议引用进 ADR 前对官方原文复核。个别页面 404/超时或只拿到二手来源，已标「未核实/部分核实」 |
| 本文是否含私有信息 | 不含账户数据、金额、主机名与 IP（V1 脚本里的远端主机/路径默认值**未转录**，只写「有」） |

## 1 E1–E12 逐项复核（WP 0a.1）

结论图例：**成立 / 部分成立 / 不成立 / 需补证据**。证据一律为 V1 `文件:行号`（相对 V1 仓库根）。

| 编号 | 结论 | 证据 | 对 V2 的影响与补充 |
| --- | --- | --- | --- |
| **E1** 无账户字段；`account_funds` 主键仅日期 | **成立** | `mystock/schema.sql:3-18`（positions 主键 `snapshot_date,market,code`）、`:21-38`（orders）、`:41-54`（deals）均无账户/环境列；`:143-158` `account_funds` 主键 `snapshot_date`。采集端不传 `acc_id`/`acc_index`：`collectors/futu_client.py:153-158`（`OpenSecTradeContext` 只给 `filter_trdmarket`，`security_firm=FUTUSECURITIES` **硬编码**）、`:183-186`、`:244`；`trd_env` 来自配置（`config.py:45-47`）但不入库 | V2 `account` 表与业务键成立。补充：①V1 历史行没有账户标识，M2b 只能给全部行赋一个「遗留账户」占位，其 `acc_id/券商主体` 无法从 V1 库还原，须负责人确认；②官方建议用 `acc_id`，`acc_index` 会随开/销户变化（见 §3） |
| **E2** 无手续费；盈亏不扣费 | **成立**（有两点补充） | `schema.sql` 全文无 fee/commission 列；`futu_client.py:337-384`（`order_rows/deal_rows`）不取费用；`pnl.py` 全文无费用；`ml/simulator.py:65-84` 买卖无费用；`ml/strategy.py:15` 自述「未扣佣金/印花税/平台费/融券成本/滑点」；`web/app.py:301-302` 文案同。补充：①`ml/execution.py:40`、`:148` 的 `/ml-next` 研究页**有**可选的 `fee_bps+fee_flat`（缺省显示 `gross_fees_missing`），但只是单一比例＋固定费，无最低费/封顶/分方向/税/聚合层级，且不进入 `backtest/strategy/pnl`；②数据字典称成交 `raw_json` 也无费用明细（`docs/guides/data-dictionary_claude_20260623.md` 的 deals「坑点」）——来自文档，**需 M0b 核实** | 费用档案（WP2.4）成立。V1 历史成交无法回填费用；官方有 `order_fee_query` 可补（≤400 单/次，10 次/30 秒；见 §3），**本机是否可用需验证** |
| **E3** 数量/价格/金额为 `REAL` | **成立** | `schema.sql:8-14,29-34,46-51,61-69,147-156`（ML 库 `ml/schema.sql:9-16,27-31,46-47,59-61,73-77,102-107`）；Python 侧一律 `float`（`pnl.py:28-34`、`simulator.py:56-88`、`execution.py`），容差硬编码 `1e-9`（`simulator.py:67,82`） | 补充：`raw_json` 是 `pandas` 行转 JSON（`futu_client.py:305-310`），数值在此前已是 float64，**不能还原十进制原文**。M2b 导入须带「量化规则」（按 tick/小数位 ROUND）并记录舍入差（LG-08 已允许有记录的舍入差） |
| **E4** 无资金流水 | **成立** | `schema.sql` 表清单（positions/orders/deals/daily_quotes/collection_status/stock_profiles/fx_rates/account_funds/capital_flow/quote_skiplist/sync_log）无入出金/换汇/股息/利息/税费；`mystock/` 与 `scripts/` 无 `get_acc_cash_flow`、`order_fee_query` 调用（grep 无结果）；`web/static/app.js:883-908` 的「区间净资产变化」直接取首末 `total_assets` 之差，入金会显示为上涨且无提示 | WP2.3 成立。**命名歧义**：V1 的 `capital_flow` 表是**个股主力资金流向**（行情接口 `get_capital_flow`，`schema.sql:160-175`），不是账户资金流水；V2 命名须区分 |
| **E5** 趋势仅来自快照且有缺口 | **成立** | 快照只在 `update/init` 流程采集（`pipelines/update_load.py:53-56`、`init_load.py:36-83`），无调度（`scripts/ml.sh:2` 写明 No scheduler；README「手动按需执行」）；`db.py:174-182` 同日先删后插；`schema.sql:141-142` 注释「历史不可从富途回补」；趋势图对缺口无标记（`app.js:916-955` 折线直连，`grep gap/缺口` 无命中），页脚文案写「周末/休市日快照沿用前值」（`app.js:829`） | 补充（新发现）：`snapshot_date` 取本机本地日期且不带时区（`db.py:89-90` `datetime.now()`），在美西机器上采集港股收盘数据时「日期」与市场交易日未必对齐（机器时区**需核实**）。`accinfo_query` 未传 `refresh_cache`（`futu_client.py:244`），可能读到 OpenD 缓存（**需核实**，见 §3） |
| **E6** 三套盈亏口径；兜底成本为事后成本 | **成立** | 移动平均：`pnl.py:66-109`；兜底=`positions` **最新**快照 `cost_price`（`web/app.py:158-168`，`pnl.py:74-76,104-109`），`≤0` 则不计入并累计 `uncovered_sell_qty`；年度「卖出额−买入额」`pnl.py:203,214`；单股 FIFO `pnl.py:283-326`（含自己的兜底 `:314-326`） | 补充（新发现）：①兜底值来自 `futu_client.py:325` 的列链 `cost_price / diluted_cost / average_cost`，摊薄成本与均价**混用**（取决于 OpenD 返回哪列，**需核实**）；②`pnl.py` **不处理拆股/并股**：成交 qty/price 为原始值，`net_qty=buy−sell`（`:133`），全仓无 corporate_action 概念；③仍无费用 |
| **E7** 人类基线被重新撮合且静默丢弃 | **成立** | `ml/backtest.py:123-127` 按日索引真实成交；`:190-198` 把真实成交价当限价交给 `match_limit_order`，`if f.filled` 才记账（未成交即**静默丢弃**）；记账价用撮合价 `f.fill_price`（`simulator.py:44-50`，买取 `min(限价, 开盘)`）而非真实成交价；人类账户从零库存起算（`backtest.py:118`），卖出超过持仓被 `Account.sell` 截断（`simulator.py:75`）、买入现金不足被忽略（`:67`），同样静默。README 自述旧回放匹配率 88–93%（§2.1），即 7–12% 被丢弃 | 方案 §2.2 #1 的「`human_actual` 原样记账、不撮合」成立。V1 的人类基线不可借用 |
| **E8** 三基线独立账户/独立初始资金；现金可行性检查只在 bandit | **部分成立（表述需修正，结论不变）** | 三条策略账户初值相同：`BTConfig.init_cash=20000.0`（`backtest.py:33`），`:118` 为 rule/bandit/human 各建 `Account(cash=init_cash)`；单标的各自独立账户（`run_backtest(code)`，不共享现金池）。**不是**「各用独立初始资金」，也**不是**「检查只在 bandit」：`Account.buy` 对三条线都拒绝 `cost>cash`（`simulator.py:67`）；bandit 另有 `can_buy` 动作过滤（`backtest.py:176`、`policy.py:56-67`）。**真正的不一致**：①同一个 20000 不分币种地用于美股(USD)与港股(HKD)；②human 数量=真实成交数量且从零库存起算，rule/bandit 数量=`unit_shares(5)×档`（`backtest.py:34,236`）；③`buy_hold` 用小数股、无手数无费用（`backtest.py:161`）；④无费用 | §2.2 #8 与 PRD E8 的论据措辞需改，**统一协议的结论不变** |
| **E9** 固定 US 10/HK 100 股；假设资金库存充足；无费用；占款按各股峰值求和 | **成立** | `ml/strategy.py:3-5`（「假设现金与持仓充足」）、`:53-60`（`LOT_BY_MARKET={"US":10,"HK":100}`，`:9` 自述「仅为旧版模拟参数，非证券实际交易单位」）、`:15` 无费用；`:160` 允许净持仓为负（裸空）；`:281-284` 组合分母=Σ 各股峰值（自述偏保守）。同一假设写进了 Web 文案（`web/app.py:244-245`、`templates/index.html:141`） | 补充：V1 已在采集行情快照时存了 `lot_size`、`price_spread`（`futu_client.py:474-475`、`db.py:50-51`），但 V1 UI 明说 `price_spread` 是「本次观察值，不用于历史交易规则」（`data-status.js` 的快照卡）；且官方定义它只是**当前向上报价间隔**（见 §3），不是 tick 价位表 |
| **E10** 配置模板无标的名单；ML 标的不在用户可配层 | **成立（实际位置已查明）** | 名单**硬编码在代码**：`ml/config.py:16-19` `TARGETS`（US.NVDA、US.TSLA、US.PDD、HK.00700、HK.09988、HK.01810）。重复硬编码：`ml/shadow.py:26-27`、`ml/preopen.py:29`、`ml/external.py:19-26`（HK→ADR 映射与 KWEB 对照）、`web/app.py:235`（默认 4 只）、`templates/index.html:131`、`templates/ml_next.html:16`；校验入口 `ml/service.py:19-20`、`web/app.py:282`。`config.example.yaml`（全文 32 行）无名单。另：**生产库采集宇宙不是名单**，而是「positions/orders/deals 中出现过的全部代码」（`db.py:213-221`、`init_load.py:122,179,221,259`），数据字典称 34–38 只 | 开放问题 1 的答案。V2 应把「采集宇宙（全部出现过的代码）」与「研究/操作单宇宙（universe.yaml）」分开。`NV` 字样在 V1 源码/文档与 `docs/V2项目 idea.md` 中均**未出现**（grep），D1 的来源需负责人确认 |
| **E11** Web 只读是约定；顶层 import ML | **前半不成立 / 后半成立** | **只读已有技术保证**：Prod 库 `web/app.py:73-74`（`?mode=ro` + `PRAGMA query_only=ON`）；ML 库 `ml/db.py:28-33`，全部 Web 路径走它（`ml/service.py:26,38,121`、`ml/strategy.py:74`、`ml/data.py:23`）；`web/` 与 `ml/service.py` 无任何 INSERT/UPDATE/DELETE/commit（grep）；测试 `tests/test_ml_api.py:11-16`（`get_db()` 建表必败）、`tests/test_web_data_status.py:137-`（GET 前后文件字节不变、GET 不触发迁移/抓取）。**顶层 import 成立**：`web/app.py:38-39` → `web/ml_api.py:4`（`from ..ml import service…`）→ `ml/service.py:8`（`import numpy`）与 `:9-12`（`data`→pandas、`features`→numpy/pandas）；`web/data_status.py:5` 亦顶层导入 `ml.sessions`。`app.py:253-255` 的 docstring 声称不顶层导入 ML 以免 Web 起不来，与 `:38` 自相矛盾（AGENTS.md:8 承认） | 开放问题 2 的答案：**V1 Web 已用只读连接**。V2 §3.2/§3.4 的「`mode=ro`」应写成**沿用 V1 做法**，不是新增价值；V2 仍需要的是 import 边界测试与写表授权器（V1 没有）。`get_db()` 模式可直接借用 |
| **E12** 待办关联 | **成立** | `docs/OPEN_ITEMS.md:29`（P2-3 证券规则无版本历史）、`:30`（P2-4）、`:31`（P2-5）、`:62`（R-01）。佐证：`rules_effective_from` 每日被覆盖（`futu_client.py:476`、`db.py:147-148` UPSERT）；P2-4 即 `ml/runs.py:18` 每次整库 backup 且硬依赖 git（`:19-20`）；P2-5 即 `scripts/ml.sh:4`（`set -e`）+ `ml/fetch.py:312` 失败即整体非零 | 如方案所述承接。另：`OPEN_ITEMS.md:115` 显示 WEB-DEPLOY（生产库 additive 迁移）**待部署**——生产库可能尚无 `lot_size` 等新列，M0b 须核实 |

### 1.1 本次审读新发现（PRD 未列）

| 编号 | 发现 | 证据 | 建议 |
| --- | --- | --- | --- |
| N1 | 成交表无币种列，靠市场推断 | `pnl.py:25`；数据字典 deals「坑点」 | V2 `FILL` 必带币种 |
| N2 | 订单/成交时间为交易所本地时间、**无时区标记** | 数据字典；`futu_client.py` 原样透传 | V2 导入须按市场补 IANA 时区并标「推断」 |
| N3 | 预测特征用「事后复权比」：`adj_close/close` 由抓取时点的复权因子决定，回看历史时包含此后才发生的分红调整（轻微前视）；标签用未复权价（与 R-03 同源） | `ml/features.py:22-28,66-70` | M8 移植时须核实；**需核实**量级 |
| N4 | 预测器缺 LightGBM 时**静默回退 sklearn** | `ml/predictor.py:44-48,53-68`（OPEN_ITEMS R-04） | 借用时改为显式失败 |
| N5 | `split_calibrate` 为无调用方的死代码 | `ml/calibrator.py:117`（grep 仅定义处） | 不借用 |
| N6 | 默认公网发布目标（远端主机/目录）与部分 H20 主机信息写在已入库脚本里；README 域名与 `ml.sh` 中目录域名后缀不一致；README 称报告含真实交易信息 | `scripts/ml.sh:27-28`、`scripts/ml_sync_h20.sh:12-14`、README §2.3 | D8 的依据；V2 脚本不得拷贝这些默认值 |
| N7 | Python 3.10、`futu-api==10.10.7008`、`yfinance==1.7.0` 固定 | `environment.yml` | V2 用 3.11（D9）；SDK 版本应留档（WP 0a.2） |

## 2 三个开放问题（WP 0a.2 前两项）

| 问题 | 答案 | 证据 |
| --- | --- | --- |
| 标的名单在 V1 的实际位置 | 代码常量 `ml/config.py:16-19`（6 只）＋多处重复硬编码；用户配置层（`config.example.yaml`）无；生产库采集宇宙＝历史出现过的全部代码 | 见 E10 |
| V1 Web 是否用只读连接 | **是**，Prod 与 ML 两库均 `mode=ro` + `query_only`，且有测试；仍顶层 import ML（numpy/pandas） | 见 E11 |
| 富途接口在本机的能力 | **无法在本机验证**（未连接 OpenD）。仅依据官方文档的能力/限频/日期边界见 §3；**不假定本机账户有权限**，须授权后在 M0b/M2a 逐接口实测并留档 SDK/OpenD 版本、券商实体、`acc_id` | §3 |

## 3 富途 OpenAPI：仅依据官方文档的能力（读取日 2026-10-05；本机均未验证）

文档根：<https://openapi.futunn.com/futu-api-doc/en/>。限频一律「按单个账户 `acc_id`」。

| 接口 | 官方文档 URL | 限频 | 日期/数量边界 | 环境与限制 | 对 V2 的含义 |
| --- | --- | --- | --- | --- | --- |
| `get_acc_cash_flow` | …/en/trade/get-acc-cash-flow.html | 20 次/30 秒（单 `acc_id`） | 证券/期货账户**必须传 `clearing_date`（YYYY-MM-DD），逐日一次查询**；`start/end` 仅加密货币账户。历史最大可回溯天数：文档摘要**未载明（需核实）** | 实盘；**不支持 moomoo US 账户**、不支持模拟盘（Q&A 页：…/en/qa/trade.html）。OpenD ≥ 9.1.5108（…/en/trade/overview.html）。返回 `cashflow_id, clearing_date, settlement_date, currency, cashflow_type, cashflow_direction, cashflow_amount(正流入/负流出), cashflow_remark` | 回补 N 天需 N 次调用；365 天≈365/20×30 秒≈9 分钟（算术）。涵盖买卖、换汇、入出金、利息（搜索摘要对官方页的转述），故只能用于对账关联（T-20）。券商主体须确认（V1 硬编码 `FUTUSECURITIES`） |
| `order_fee_query` | …/en/trade/order-fee-query.html | 10 次/30 秒 | **≤400 个订单/次**；订单自 2018-01-01 起 | 仅实盘；不支持模拟盘与 Moomoo CA 账户；OpenD ≥ 8.2.4218。返回 `order_id, fee_amount, fee_details[(项目,金额)]`；列举项偏美股（Commission、Platform Fee、ORF、OCC、Settlement Fee、SEC Fee、TAF），**港股印花税/交易费/征费是否出现在 `fee_details` 需核实** | M2a 费用来源候选（D5）；与 V1 无关（V1 不取） |
| `history_deal_list_query` | …/en/trade/get-history-order-fill-list.html | 10 次/30 秒 | 缺省 `start`=90 天前、`end`=当前；返回倒序；返回字段**无费用** | **仅实盘**；字段含 `jp_acc_type` | 官方**无「80 天窗口」说法**：V1 的 80 天是自选分段（`futu_client.py:56-57` 注释「默认 90 天，留余量取 80」）；单次最大跨度文档摘要**未载明（需核实）** |
| `history_order_list_query` | …/en/trade/get-history-order-list.html | 10 次/30 秒 | 双空→前 90 天至今；仅 `start`→`+90` 天；仅 `end`→`−90` 天；时间格式严格 `YYYY-MM-DD HH:MM:SS[.ms]` | 实盘与模拟盘均可 | 同上 |
| `accinfo_query` | …/en/trade/get-funds.html | 10 次/30 秒，**仅当 `refresh_cache=True`** | — | 有按币种拆分的 `us_cash/hk_cash/cn_cash/jp_cash/sg_cash/au_cash/ca_cash/my_cash` 等；`currency` 参数仅综合账户有效；默认用 OpenD 缓存 | V1 只取 HKD/USD 两侧且未传 `refresh_cache`（见 E5）；V2 现金按币种读全部币种 |
| `get_acc_list` | …/en/trade/get-acc-list.html | 文档未载明 | — | 返回 `acc_id, trd_env, acc_type, security_firm(如 FUTUSECURITIES/FUTUINC), trdmarket_auth, acc_status, acc_role` | 官方建议用 `acc_id`（`acc_index` 随开销户变化）；V2 须显式 `acc_id` 并留档 `security_firm` |
| `get_market_snapshot` | …/en/quote/get-market-snapshot.html；限频页 …/en/intro/authority.html | **60 次/30 秒**（authority 页），≤400 标的/次 | — | 含 `lot_size`、`price_spread`（**当前向上报价间隔**）、`suspension`、`sec_status`；**无除息日字段** | `price_spread` 不能当 tick 价位表；除息日须另找来源 |
| `get_capital_flow`（个股，非账户） | …/en/quote/get-capital-flow.html | 30 次/30 秒 | 日线「最近 1 年」，同页另称周期数据「最近 2 年」（并存）；V1 实测约 237/243 个交易日（`futu_client.py:483-485`） | — | 与账户资金流水无关 |
| 共享额度（V1+V2） | 同上各页 | 以 `acc_id` 为单位 | — | — | 同账户并行采集会共用额度（文档措辞推断；**是否跨连接/按接口分别计数需核实**）。V1 的 `RATE_LIMIT_INTERVAL=3.2s`（`futu_client.py:62`）≈9.4 次/30 秒，已接近 10 次上限：V2 采集须与 V1 `update.sh` **错峰** |

SDK/OpenD：V1 固定 `futu-api==10.10.7008`（`environment.yml`）；OpenD 版本、券商实体、`acc_id` 本机**未知**，授权连接后第一步留档。

## 4 市场规则核验（WP 0a.3）

状态：**已核实** / **部分核实** / **未核实**。V1 的对应实现一并标出。

| # | 项 | 结论 | 来源（URL；读取日 2026-10-05） | 状态与说明 |
| --- | --- | --- | --- | --- |
| 1 | HK 交易时段 | 开前竞价 09:00–09:30；上午 09:30–12:00；午休 12:00–13:00；下午 13:00–16:00；收市竞价（CAS）16:00 起，随机收市于 16:08–16:10 | HKEX FAQ <https://www.hkex.com.hk/Global/Exchange/FAQ/Securities-Market/Trading/Securities-Market-Operations?sc_lang=en>；时长扩展动议见 <https://www.thestandard.com.hk/finance/article/337681/HKEX-considers-extending-trading-hours-canceling-lunch-break-Bloomberg-reports>（2026-07-20 报道：取消午休、提前 30 分钟、增设晚间时段，**尚无生效日，仅探索阶段**） | **已核实**（现行）。注意：HKEX 页面把 12:00–13:00 称为「Extended Morning Session」，摘要未能确认该小时现货是否完全不交易，二手来源均称午休——**午休是否停牌需核实**。V1 日历：HK `open 09:30`、`close 16:00`、`break 12:00–13:00`、`deadline 09:00`、`final_at=close+15min`（`calendars/HK.csv`、`scripts/ml_experiments/freeze_calendar.py:20-24`），CAS 不单独建模 |
| 2 | HK 半日市 | 圣诞、新年、农历新年前夕无午休后时段与下午时段；上午收市竞价 12:00–12:10 | 同上 HKEX FAQ | **已核实**。V1 CSV 中 2026-12-24、2026-12-31、2027-02-05 的 `close` 均为 04:00 UTC（=12:00 HKT）且无午休（我核对了这些行） |
| 3 | HK 恶劣天气 | 2023 年及以前：8 号风球/黑雨/极端情况使上午取消、午间可复市或全日取消；**2024-09-23 起**报道称 8 号风球与黑雨下继续交易 | 旧规则：HKEX 公告（搜索结果摘要引用）<https://www.hkex.com.hk/news/market-communications/2023/230901news?sc_lang=en>；新规：BNN Bloomberg（搜索结果摘要）<https://bnnbloomberg.ca/business/international/2024/11/13/hk-brokers-brace-for-first-typhoon-trading-day-as-storm-nears>。HKEX 官方「恶劣天气安排」页（评审方给的 URL）读取 404/无关内容 | **未核实（官方原文未取得）**。V1 日历只手工剔除 2023-09-01、2023-09-08 两个整日停市（`calendars/README.md`），**不能**据此推断「现行台风必停市」。极端情况（Extreme Conditions）下的现行安排需核实后再写入 T-08 |
| 4 | US 交易时段 | 常规 09:30–16:00 ET；NYSE 开前时段自 06:30 ET（Arca 02:30）、早盘 07:00–09:30；晚间 16:00–20:00（American/Arca/National/Texas）。Nasdaq 盘前 04:00–09:30、盘后 16:00–20:00（搜索摘要） | NYSE <https://www.nyse.com/trade/hours-calendars>；Nasdaq <https://www.nasdaq.com/market-activity/stock-market-holiday-schedule>（直接读取超时，仅有搜索摘要） | NYSE 常规时段 **已核实**；盘前盘后随交易所/券商不同，**部分核实**。Nasdaq 另有近 24 小时交易的 SEC 备案（SR-NASDAQ-2025-106/-109，SEC 34-104563、34-105590，搜索结果列出，未读）——**生效日需核实**。券商夜盘未核实。V1 只用常规时段日历，盘前价取 08:00 ET 小时线（`ml/preopen.py` 文件头） |
| 5 | US 半日市与节假日 | 2026：11-27、12-24 为 13:00 ET 早收；7-3 为独立日补休（整日休市）。2027：11-26 早收；休市日 01-01、01-18、02-15、03-26、05-31、06-18、07-05、09-06、11-25、12-24（共 10 天） | NYSE 同上 | **已核实**（与 V1 `US.csv` 抽查一致：2026-07-03、2027-06-18、2027-07-05、2027-12-24 无行；2026-11-27、2026-12-24、2027-11-26 `close` 为 18:00 UTC）。摘要曾把 2026-07-03 同时列入早收与休市，自相矛盾，以休市为准（NYSE 原页需复核）。仅抽查，未逐年核对 |
| 6 | HK tick 档位 | 最小价差分两期下调：**2025-08-04** 第一期（10–20 港元 0.02→0.01；20–50 港元 0.05→0.02）；**2026-08-03** 第二期（0.5–10 港元 0.01→0.005）；适用股票/REIT/权证等，**不含 ETP、债券、期权、结构性产品** | HKEX <https://www.hkex.com.hk/Services/Trading/Securities/Overview/Trading-Mechanism/Reduction-of-Minimum-Spreads> | 日期**已核实**（页面标「final implementation model」）。**需核实**：①第二期是否按期落地（今日已过日期，页面摘要称通告至 2026-07）；②完整价位表（未取得）；③ETP 单独的更小 tick（HKEX infosheet，搜索结果列出，未读）。方案 WP4.4「tick 带有效期」成立 |
| 7 | HK 每手股数 | 由发行人自定，现有 40 余种；2000 最常见（约 25% 发行人），成交额/市值占比最大者多为 100 股。HKEX 2025-12 咨询拟收敛为 8 档（1/50/100/500/1000/2000/5000/10000） | HKEX 咨询文件 <https://www.hkex.com.hk/-/media/HKEX-Market/News/Market-Consultations/2016-Present/December-2025-Board-Lot-Framework-Enhancements/Consultation-Paper/cp202512.pdf>（搜索摘要引述，未通读） | 「随证券而异」**已核实**；标准化提案状态**需核实**（回应期至 2026-06）。富途 `lot_size` 字段官方存在（§3） |
| 8 | 结算周期 | US **T+1**，合规日 **2024-05-28**；HK 现行 **T+2**，HKEX 2026-04 就 T+1 咨询，**指示性实施期 2027 Q4**，未生效 | SEC <https://www.sec.gov/newsroom/press-releases/2023-29>；HK：Charltons 报道（2026-04-17）<https://www.charltonslaw.com/hkex-consults-on-t1-settlement/>，投委会教育页（搜索摘要）<https://www.ifec.org.hk/web/en/investment/investment-products/stock/stock-trading/stock-settlement.page> | US **已核实**（SEC 官方）；HK T+2 **部分核实**（二手来源，未取得 HKEX 官方页）。可用现金规则（WP2.6）按此，但须在 `security_rule`/结算规则里带生效期 |
| 9 | 除息日与记录日；「除息日及之后买入不享股息」 | **US（T+1 后）**：常规现金股息的除息日与记录日同一营业日；在除息/记录日**当日或之后**买入不享股息；首个适用新规的记录日为 2024-05-29。**HK（T+2）**：除息日通常是记录日（无停过户时）或最后过户日**前一个营业日**；除息日买入不享、除息日卖出者仍享 | US：SEC 备案（搜索摘要引用）<https://www.sec.gov/files/rules/sro/nysearca/2024/34-99881.pdf>；HK：Charltons 解读 <https://www.charltonslaw.com/hkex-publishes-consultation-paper-on-ex-entitlement-trading/>（二手）与 HKEX 咨询文件 | **部分核实**（US 经 SEC 备案摘要；HK 为二手）。「除息日及之后买入不享」两市**都成立**。方案 §6 不变量 4 的计提基数（除息前最后 cum 日收盘持仓）与此一致；但记录日与除息日的关系**两市不同**，且大额特别股息有例外规则**需核实**。富途快照无除息日字段（§3） |
| 10 | yfinance 默认复权语义 | `Ticker.history()` 与 `download()` 的 `auto_adjust` **默认 `True`**；`history` 同时 `actions=True, prepost=False, repair=False`；`end` 为**排他** | `download` 文档 <https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html>；`history` 签名与 docstring 取自源码 <https://raw.githubusercontent.com/ranaroussi/yfinance/main/yfinance/scrapers/history.py>（main 分支） | **已核实**（main 分支；V1 固定 1.7.0，**该版本默认值未单独核实**）。V1 全部显式 `auto_adjust=False`（`collectors/yf_client.py:105-107,174-176`；`ml/fetch.py:159-161`），并以 `end+1` 处理排他（`yf_client.py:54-67`） |
| 11 | yfinance 小时线可回溯范围 | 官方 docstring 与 `download` 文档只写「Intraday data cannot extend last 60 days」；V1 使用 `60m` + `period="730d"`（`ml/config.py:60`） | 同上 | **730 天未核实**：yfinance 官方文档未载明，是 Yahoo 端行为，V1 以实测（`docs/OPEN_ITEMS.md:78` P04 记录「726 个有 08:00 bar」）为据。官方只承诺 60 天 → 方案「小时线从首日起归档」更必要。V1 还记录 HK 小时桶锚定 :30 且可跨午休（`ml/data.py:88-90`），M4 须抽样核实 |
| 12 | 富途历史成交限频与「80 天窗口」 | 限频 **10 次/30 秒/`acc_id`、仅实盘**有官方依据；**80 天窗口无官方依据**，默认窗口是 90 天 | §3 | **限频已核实**；80 天**未核实/是 V1 的工程选择**，单次最大跨度需核实 |

## 5 「发现不成立」的复审范围（WP 0a.4）

若某条被证伪，须重评的方案章节（§ 指实施方案 v1.0）：

| 若被证伪 | 重评 |
| --- | --- |
| E1（V1 其实有账户维度） | §3.1 导入器；§6 `account`/业务键；WP2.1；M2b 去重（T-17）；D10 |
| E2（V1 其实有费用） | §2.2 #7；WP2.4 与 `fee_profile`；T-21；§6A.5 主指标「费用后」；M2b 费用回填；D5；§7「费用与规则」 |
| E3（V1 其实是十进制） | §3.2；WP1.2；M2b LG-08 的舍入差条款 |
| E4（V1 其实有资金流水） | WP2.3；不变量 5、8；T-07/T-20；§6A.5 外部资金流；M3 资产趋势；M2b 变简单 |
| E5（V1 快照其实连续/可回补） | WP2.9；M2b 历史权益基线；§2.3 σ 估计；M3 趋势缺口 |
| E6 | WP5.4 指标字典；M3 盈亏视图；不变量 1 的 `pre_opening` 规则；T-01 |
| E7（V1 其实不重撮合） | §1 要点 2、§2.2 #1；SB-02/SB-03；`human_actual` 引擎；V1 人类基线可借用性 |
| E8 | §2.2 #8；WP5.2；§6A.5；M5（**本次已部分证伪：见 §7 修订 2**） |
| E9 | WP4.4；WP6.2 数量；T-32；D4 |
| E10（名单其实在配置层） | WP1.5、WP6.1；D2；§3.5 私有配置；M1 universe 校验器 |
| E11（**本次已前半证伪**） | §3.2/§3.4 的措辞；WP1.4、WP3.1、LN-01：只读连接不再是 V2 新增价值；import 边界测试与授权器仍需 |
| E12 | §11 风险与 OPEN_ITEMS 承接 |
| 外部事实：tick 日期 | WP4.4、T-32、M5 执行协议 ADR（碎股/tick 舍入） |
| 外部事实：结算周期 | WP2.6、T-23、M5 可用现金规则 |
| 外部事实：除息/记录日 | §6 不变量 4、WP2.5、T-22 |
| 外部事实：yfinance 默认/回溯 | WP4.3、T-40、M4 归档完整性检查 |
| 外部事实：cash flow/order fee 在本机不可用 | WP2.3/2.7、LG-06、T-20；改走授权文件导入＋对账降级；D5 改自填费用档案 |

## 6 V1 功能对照表与模块来源表

见同批文件：[v1-feature-parity_claude_20261005.md](v1-feature-parity_claude_20261005.md)（D16）、[module-sources_claude_20261005.md](module-sources_claude_20261005.md)。

## 7 对方案的修订建议

| # | 章节 | 建议 |
| --- | --- | --- |
| 1 | §3.2、§3.4、WP1.4、WP3.1；PRD E11 | 把「Web 只读连接」改写为**沿用 V1 做法**（V1 已 `mode=ro`+`query_only`，且有测试）；V2 的增量价值是 import 边界测试与写表授权器。E11 只剩「顶层 import 耦合」 |
| 2 | §2.2 #8；PRD E8 | 改正措辞：三基线初值相同（20000，不分币种）、现金检查在 `Account.buy` 全线存在；真实不一致是**human 零库存起算且数量单位不同、`buy_hold` 小数股、币种不分、无费用**。结论（统一协议）不变 |
| 3 | WP1.5、WP6.1、D2；§3.5 | 采集宇宙（历史出现过的全部代码）与研究/操作单宇宙分开；V1 名单硬编码在 6 处，迁移只需读 `ml/config.py:16-19`；D1 的 `NV` 在 V1 与 idea 中均无出现，请负责人确认来源 |
| 4 | WP2.3、WP2.7、D10 | `get_acc_cash_flow` **逐日**查询、20 次/30 秒、不支持 moomoo US：D10 增加「券商主体/账户类型」确认；回补规模写入 M2a 估算（365 天≈9 分钟）；`history` 类接口的 80 天分段注明是工程选择 |
| 4a | WP2.4、D5 | 增补 `order_fee_query`（≤400 单/次、10 次/30 秒、2018 起）作为费用来源候选；港股费用项是否齐全待本机验证；估算费用须标注 |
| 5 | WP4.4、T-32 | 采用 2025-08-04、2026-08-03 两个 tick 生效日（已核实）；**第二期实施结果与完整价位表需 M4 取官方表**；`price_spread` 仅为当前上浮间隔，不得充当价位表；每手股数「随证券而异」已核实 |
| 6 | §6 不变量 4、WP2.5、T-22 | 按市场分别写除息/记录日关系：US（T+1）除息日=记录日；HK（T+2）除息日=记录日（或最后过户日）前一营业日；计提基数规则两市一致。除息日来源（富途快照无该字段）列入 M4 数据源核验 |
| 7 | WP1.3、T-08 | 日历「恶劣天气」改为：2024-09-23 前后规则不同（官方原文待核）；V1 CSV 仅修正 2023 两个整日停市，**不继承「台风必停市」假设**；HK 半日市与 US 早收盘已核实；CAS 与午休是否停牌需核实 |
| 8 | WP4.3、§3.1 | yfinance 小时线官方仅承诺 60 天（V1 经验 730 天未获官方背书）；归档从首日开始的决定被强化；`auto_adjust` 默认 True，须显式写参数并留 `repair/prepost` 取值 |
| 9 | §3.1 并行期、WP0b.4 | V1 采集间隔已占约 94% 的历史接口额度，V2 采集须与 V1 `update.sh` 错峰；是否「按接口分别计数」需核实 |
| 10 | §3.1 代码复用 | 借用清单与风险见 module-sources；V1 `ml/` 下多个模块代码风格极度压缩（分号/单行 if），借用须格式化并重写测试 |
| 11 | §3.7、D16 | **V1 正在累积前向 shadow（D5）**（`OPEN_ITEMS.md:54`、`scripts/ml.sh:43-47`）：切换时是否继续、与 V2 前向时钟的关系，应在 D16 中明确 |
| 12 | D8 | V1 公网发布含真实交易信息（README §2.3）且默认远端目标写在脚本里；V2 公开导出白名单（M10）不得沿用 |
| 13 | M2b | V1 历史行无账户标识、无时区、无费用、REAL 精度：导入器须有「遗留账户」占位、时区推断标记、量化规则；开账日前历史仅作描述的既定规则与此一致 |

## 8 仍标「需核实」的点（汇总）

HK 午休是否停牌；HK 恶劣天气现行官方安排；Nasdaq 近 24 小时交易生效日与券商夜盘；HK tick 第二期落地与完整价位表；HK 每手标准化提案状态；HK T+2 官方页；除息/记录日在 HK 的官方表述与美股特别股息例外；yfinance 1.7.0 的默认值；小时线 730 天；`get_acc_cash_flow` 历史最大回溯、`order_fee_query` 的港股费用项、`history_*` 单次最大跨度、额度是否按接口/连接计数；本机 OpenD/SDK 版本、券商主体、`acc_id`、账户是否有对应权限；`accinfo_query` 缓存新鲜度；V1 机器时区与 `snapshot_date` 语义；数据字典关于「无费用字段」的文档断言；`adj_close` 事后复权的前视量级；V1 工作树是否含未提交改动。
