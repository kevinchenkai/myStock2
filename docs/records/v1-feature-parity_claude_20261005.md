# V1 功能对照表（parity matrix，WP 0a.5 / D16）

| 项 | 内容 |
| --- | --- |
| 作者 | Claude（子任务，M0a） |
| 日期 | 2026-10-05 |
| V1 基线 | `ca16e31ad8f78b23e2ceabbd58dba0a01b55e35e`（只读；读取的是工作树文件，未核验是否含未提交改动） |
| V2 基线 | 实施方案 v1.0（`main` @ `504d3fc` 为方案头部记载，未核验）；本表依据方案 §3.7、§4.2、§5 的里程碑编号 |
| 状态 | **建议稿，待负责人确认（D16）；未确认前不得宣布可切换（方案 §3.7）** |
| 边界 | 只读 V1 源码与已入库文档；**未**运行 V1 任何代码、**未**读取 V1 数据库与 `config.yaml`、**未**连接券商、**未**改 V1 或 V2 其他文件、**未**执行 git 写操作（V1 只执行过 `rev-parse HEAD`、`ls-files`）。总报告见 [v1-audit_claude_20261005.md](v1-audit_claude_20261005.md) §0 |
| 读法 | 「迁移」＝沿用设计/代码（拷贝＋重写测试）；「V2 替代」＝按 V2 契约新实现，V1 口径不沿用；「放弃」＝V2 不提供。里程碑列为**验收所在**里程碑 |

## 1 用户可见页面与前端面板

V1 共 2 个页面：`/`（`templates/index.html`，6 个 Tab＋下钻浮窗）与 `/ml-next`（`templates/ml_next.html`）。

| # | V1 项 | 位置 | 功能说明 | 建议 | 理由 | V2 里程碑 |
| --- | --- | --- | --- | --- | --- | --- |
| P-01 | 「我的持仓」Tab | `index.html:16,26-39`；`app.js:153-331` | 最新快照持仓表；市场筛选（全部/美/港）；数值列表头排序 | **V2 替代** | 需带账户维度、新鲜度头部；成本三种并列（券商/摊薄/本地移动平均，方案 WP3.3）；金额注币种 | M3 |
| P-02 | 组合概览卡（按币种汇总市值/浮盈/占比） | `app.js:221-286` | USD/HKD 各一卡，不跨币种相加 | **迁移（设计）** | 「逐币种、不相加」与 V2 §6A 一致；数据来源改为账本/快照 | M3 |
| P-03 | 账户总览卡（总资产/市值/现金/仓位/购买力，HKD 记账） | `app.js:289-331` | 来自 `account_funds` 最新快照 | **V2 替代** | V1 只取 HKD 合并值；V2 逐币种、区分经济现金与可交易现金（WP2.6） | M3 |
| P-04 | 「我的交易」Tab：按订单/按成交子 Tab；市场与年份筛选 | `index.html:17,42-64`；`app.js:368-530` | 全状态订单、成交表 | **V2 替代** | 需费用列、账户、币种（V1 成交无币种，N1）；订单只是意图、成交是事实 | M3（交易流水视图） |
| P-05 | 「交易盈亏」Tab（移动平均已实现盈亏、兜底成本提示） | `index.html:18,67-77`；`app.js:562-710`；`pnl.py:37-141` | 每股已实现盈亏、合计按币种 | **V2 替代（口径不迁移）** | E6：三套口径并存、兜底成本为事后成本、无费用、不处理拆股。V2 用指标字典＋「估算」标识 | M3（盈亏视图），指标字典 M5 |
| P-06 | 「财务统计」（年度现金流＝卖出额−买入额） | `index.html:80-90`；`app.js:579-646`；`pnl.py:148-221` | 按年、按美/港市场汇总 | **放弃** | 口径易被误读（只买未卖显示大额负数）；由账本现金流与资产趋势视图取代 | — |
| P-07 | 「资产趋势」Tab（市值/浮盈折线、净资产卡、区间环比表、30/90/360/全部） | `index.html:19,94-118`；`app.js:711-966` | 来自历史快照 | **V2 替代** | E4/E5：入金显示为上涨、缺口无标注；V2 区分持仓市值≠权益≠剔除资金流收益，缺口显式（WP3.3） | M3 |
| P-08 | 「美元汇率」Tab（USDCNY 趋势与统计） | `index.html:20,121-124`；`app.js:967-1063` | 来自 `fx_rates`，中性配色 | **迁移（设计）** | V2 要 USD/HKD/CNY 路径；配色约定（汇率中性）沿用 | M3（外汇视图），数据 M4 |
| P-09 | 「ML 挂单回溯」Tab（legacy） | `index.html:21,127-145`；`app.js:1558-1713`；`web/app.py:239-303`；`ml/strategy.py` | 按预测区间次日挂买/卖各一手，三种收益率分母，裸空 | **放弃** | 旧协议：假设资金库存充足、无费用、固定 10/100 股、净持仓可为负（E9；V1 自己的 R-01 已标其不可当验收）。其研究目的由 M5 记分牌取代 | — |
| P-10 | `/ml-next`「ML 预测与库存回溯」研究页（下一目标日卡、复盘场景、逐 session 复盘、真实委托事实） | `templates/ml_next.html`；`static/ml_next.js`；`web/ml_api.py` | 20/60/120 session 回放；本金、库存、lot/tick、费用参数；`live` vs `recomputed` 来源标注 | **V2 替代** | 其「预测来源分层、不可覆盖版本、受约束库存回放」的思想与 V2 M4/M5 一致，但 V2 要同协议、线内状态、冻结操作单；引擎只参考 | M4（预测留档视图）、M5（记分牌视图）、M6（操作单视图） |
| P-11 | 个股下钻浮窗：通用信息 | `app.js:1346-1398`；`/api/stock/<code>/profile` | yfinance 公司/估值（每日覆盖，无历史） | **V2 替代** | 方案 LN-07：资料须带发布时间，当前资料不得回填历史特征 | M3 后续 WP 3.6 |
| P-12 | 个股下钻：K 线＋成交量（lightweight-charts）＋历史日线表 | `app.js:1400-1520`；`static/vendor/…v4.2.3` | 蜡烛图，红涨绿跌 | **迁移（库与约定）** | 方案 §3.2 已定沿用 lightweight-charts 本地 vendor；数据口径（原始价/复权价）按 M4 | M3 后续 WP 3.6 |
| P-13 | 个股下钻：主力资金流向（近 60 日柱图） | `app.js:1249-1345`；`capital_flow` 表 | 富途个股日频资金流向 | **V2 替代（低优先）** | 与账户资金流水无关；富途只给近约 1 年；是否需要由负责人定 | M3 后续 WP 3.6（LN-07） |
| P-14 | 个股下钻：订单/成交明细 | `app.js:1521-1556` | 该股订单与成交 | **V2 替代** | 并入交易流水视图的筛选 | M3 |
| P-15 | 交易复盘浮窗（FIFO 回合、胜率、盈亏比、持有时长、客观观察） | `app.js:1097-1204`；`pnl.py:242-448` | 单股复盘 | **V2 替代** | 方案 WP7.4：保留 FIFO 为「诊断回合」并改名；成交费用、拆股须入账；事实/推测分开 | M7 |
| P-16 | 数据状态折叠面板与个股缓存快照卡 | `static/data-status.js`；`web/data_status.py`；`/api/data-status`、`/api/stock/<code>/snapshot` | 来源/个股的采集状态（ok/partial/empty/error/unsupported/unknown）、业务时间 vs 采集时间、「时区未知」 | **迁移（设计）** | 与方案「每个视图自带新鲜度头部」（LN-02）同源；状态枚举与「未知显示未知」值得沿用 | M3 |
| P-17 | 主题切换（跟随系统/浅/深，`localStorage`，首屏防闪烁） | `static/theme.js`；`_theme_head.html` | 三态主题 | **迁移** | 47 行、无业务耦合 | M3（WP3.4） |
| P-18 | 红涨绿跌与金额注币种约定 | `app.js:5-35`；AGENTS.md | 全站配色 | **迁移（约定）** | 方案已采纳 | M3 |
| P-19 | 表头排序、市场/年份筛选、前端缓存后重渲染 | `app.js:333-366,433-447` | 纯前端交互 | **V2 替代** | 放入视图框架 | M3 |

## 2 API 路由

`mystock/web/app.py` 与 `web/ml_api.py`。V2 路由统一为 `GET /api/v/<view_id>`（方案 WP3.1），下表为功能映射，不要求路径兼容。

| # | V1 路由 | 位置 | 建议 | 理由/说明 | V2 里程碑 |
| --- | --- | --- | --- | --- | --- |
| A-01 | `GET /api/positions` | `app.py:100-116` | V2 替代 | 账本重建持仓＋券商快照对账 | M3 |
| A-02 | `GET /api/orders?code=` | `app.py:119-132` | V2 替代 | 带账户/币种/费用 | M3 |
| A-03 | `GET /api/deals?code=` | `app.py:135-148` | V2 替代 | 同上 | M3 |
| A-04 | `GET /api/pnl` | `app.py:151-171` | V2 替代 | 口径换指标字典（E6） | M3/M5 |
| A-05 | `GET /api/finance?year=` | `app.py:174-185` | 放弃 | 见 P-06 | — |
| A-06 | `GET /api/stock/<code>/analysis` | `app.py:188-212` | V2 替代 | 见 P-15 | M7 |
| A-07 | `GET /api/fx?pair=` | `app.py:215-231` | V2 替代 | 外汇视图 | M3/M4 |
| A-08 | `GET /api/ml/strategy`（legacy 与 `mode=inventory`） | `app.py:239-303` | 放弃（legacy）/ V2 替代（inventory） | 见 P-09、P-10 | M5 |
| A-09 | `GET /api/asset-trend` | `app.py:306-328` | V2 替代 | 见 P-07 | M3 |
| A-10 | `GET /api/account-funds` | `app.py:331-353` | V2 替代 | 见 P-03 | M3 |
| A-11 | `GET /api/quotes?code=&start=&end=` | `app.py:356-377` | V2 替代 | 原始/复权价分列 | M3/M4 |
| A-12 | `GET /api/stock/<code>` | `app.py:380-422` | V2 替代 | 聚合接口拆成视图查询 | M3 |
| A-13 | `GET /api/stock/<code>/capital-flow?days=` | `app.py:425-450` | V2 替代（低优先） | 见 P-13 | M3 后续 |
| A-14 | `GET /api/stock/<code>/profile` | `app.py:453-467` | V2 替代 | 见 P-11 | M3 后续 |
| A-15 | `GET /api/data-status` | `app.py:482-490` | 迁移（设计） | 见 P-16 | M3 |
| A-16 | `GET /api/stock/<code>/snapshot` | `app.py:493-503` | 迁移（设计） | 见 P-16；`lot_size/price_spread` 仅作观察值 | M3/M4 |
| A-17 | `GET /api/ml/v2/latest` | `ml_api.py:41-43` | V2 替代 | 预测版本视图 | M4 |
| A-18 | `GET /api/ml/v2/review` | `ml_api.py:45-53` | V2 替代 | 预测复盘/校准过程指标 | M4/M5 |
| A-19 | `GET /api/ml/v2/compare` | `ml_api.py:55-57` | V2 替代 | 库存回放→记分牌 | M5 |
| A-20 | 页面路由 `/`、`/ml-next` | `app.py:89-96` | V2 替代 | 视图框架页面 | M3 |

## 3 脚本与命令入口

| # | V1 入口 | 位置 | 功能 | 建议 | 理由 | V2 里程碑 |
| --- | --- | --- | --- | --- | --- | --- |
| S-01 | `scripts/init.sh` | `scripts/init.sh` | 建环境、建库、全量采集 | V2 替代 | V2 为 `python -m mystock2 <子命令>`；真实账户访问须授权 | M1/M2a |
| S-02 | `scripts/update.sh`（`pipelines/update_load.py`） | `scripts/update.sh` | 增量采集（持仓/订单/成交/账户资金/行情/汇率/资料/快照/资金流向） | V2 替代 | 改为向前采集＋运行回执；错峰限频；去掉「覆盖当天」语义 | M2a/M4 |
| S-03 | `scripts/server.sh` | `scripts/server.sh` | 启动 Web（8888） | V2 替代 | V2 开发期 8889，切换见 §3.7 | M3/M10 |
| S-04 | `scripts/ml.sh data` | `ml.sh:29` | ML 行情与生产事实快照 | V2 替代 | 行情采集进 M4；V1 的「完整 bar 才入库」守卫值得借用 | M4 |
| S-05 | `scripts/ml.sh train` | `ml.sh:30-33` | 冻结输入、回测、预测、生成报告 | V2 替代 | 预测留档＋`coach run` | M4/M6 |
| S-06 | `scripts/ml.sh publish` / `all` | `ml.sh:34-39,49-54` | scp 报告到公网 | **放弃（待 D8）** | 报告含真实交易信息；默认远端目标写在脚本里（N6）。若 D8 要公开，走 M10 白名单导出 | M10 |
| S-07 | `scripts/ml.sh shadow HK/US` | `ml.sh:43-47`；`ml/shadow.py` | 开盘前 D5 前向 shadow 记录 | V2 替代 | 与 V2 前向冻结同目的；**V1 前向 shadow 正在累积（OPEN_ITEMS:54），切换时的处置须在 D16 定** | M6/M8 |
| S-08 | `scripts/ml_preview.py` | 全文 | 隔离端口预览 | 放弃 | V2 有 `config.yaml web.port` 与视图框架 | — |
| S-09 | `scripts/ml_setup_h20.sh`、`ml_sync_h20.sh`、`ml_vllm.sh` | 各文件 | GPU 机环境/同步/停启 vllm | 放弃 | V2 非目标（无 GPU/云）；脚本含私有主机默认值，**不得拷贝** | — |
| S-10 | `python -m mystock.pipelines.maintenance`（skiplist / reset-skiplist / purge） | `pipelines/maintenance.py` | 跳过名单与按代码彻底删除 | V2 替代（`purge` 放弃） | V2 账本只追加；破坏性 `purge` 与不可变证据冲突；跳过名单改为逐标的采集状态 | M4 |
| S-11 | `scripts/check_docs.py` | 125 行 | 文档目录/命名/链接检查 | 迁移（参考） | 方案 WP1.7 已列 | M1 |
| S-12 | `scripts/ml_experiments/freeze_calendar.py` | 29 行 | 生成 HK/US 交易日历 CSV | 迁移（逻辑，换来源核对） | 方案 WP1.3；须按官方公告复核（2023 手工剔除两日；HK 2026 圣诞前夕半日市靠 XHKG 补） | M1 |
| S-13 | `scripts/ml_experiments/` 其余 15 个实验/修复脚本（`exp_a/b`、`upgrade_matrix`、`model_matrix`、`overnight_*`、`fetch_external/preopen`、`rebuild_history`、`archive_development`、`import_futu_hourly`、`shadow_report`、`strategy_validation`、`summarize_upgrade`、`frozen_cv_446e657`） | `scripts/ml_experiments/README.md` | 研究、历史修复、负结果复现 | 放弃（归档于 V1） | 已出负结果且不计前向证据；M8 如要候选线另起实验 | — |
| S-14 | 模块入口：`python -m mystock.ml.{fetch,predictor,backtest,report,backfill,calibrate,offline_rl,pipeline}` | `mystock/ml/*.py` | 各自的 `__main__` | 见 §4 | — | — |

## 4 ML 报告、预测与发布功能

| # | V1 功能 | 位置 | 建议 | 理由 | V2 里程碑 |
| --- | --- | --- | --- | --- | --- |
| M-01 | 每日 HTML 报告（总览表、四条曲线、每股总结、预测复盘面板、状态面板；自包含零 JS） | `ml/report.py`（756 行） | 放弃（待 D8） | 报告口径仍是旧回测协议（OPEN_ITEMS R-01）；V2 的展示走视图与记分牌 | M3/M5/M10 |
| M-02 | 次日区间预测（LightGBM 分位回归＋CQR 校准；purged 切分） | `ml/predictor.py`、`calibrator.py`、`cv.py`、`features.py` | 迁移（M8 候选线） | 首条 AI 线用透明基线（方案 §1 要点 6）；LightGBM+CQR 作后续批次 | M8 |
| M-03 | 不可覆盖预测版本、来源标签（live/backfill/recomputed）、时间链校验 | `ml/versions.py`、`pipeline.py` | 迁移（设计） | 对应 `prediction_version`、证据快照（WP4.2、4.6） | M4 |
| M-04 | 预测复盘（命中/上破/下破） | `ml/review.py` | V2 替代 | 作为预测校准过程指标进入记分牌 | M5 |
| M-05 | 规则基线 S0、LinUCB bandit、离线 RL、HMM 等决策层 | `ml/policy.py`、`backtest.py`、`offline_rl.py` | 放弃 | bandit/RL 为负结果或不可靠（OPEN_ITEMS「更早的遗留」）；V2 `decide()` 为纯函数规则 | — |
| M-06 | 真实订单回放校准（撮合吻合率 88–93%） | `ml/calibrate.py` | 放弃（参考） | 缺委托生命周期，不能外推（README §2.1） | M5 执行协议 ADR 仅参考 |
| M-07 | 1h 限价撮合模拟器 | `ml/simulator.py`、`execution.py` | V2 替代 | 须按 §5 执行协议 ADR 重写（跳空价、部分成交、tick 舍入、碎股） | M5 |
| M-08 | 前向 shadow（开盘前特征：港股 ADR 隔夜、美股盘前价） | `ml/shadow.py`、`preopen.py`、`external.py` | 放弃/参考 | V2 首期不用盘前/隔夜特征；V1 已有的 D4 实验结果可作 M8 输入 | M8 |
| M-09 | 公网报告发布（`g.ismayday.*`） | `ml.sh publish`、`ml/pipeline.py` | **待 D8 决定** | 见 S-06 | M10 |

## 5 需要负责人决定的问题（关联 D8、D16）

| # | 问题 | 建议默认 | 关联 |
| --- | --- | --- | --- |
| Q1 | 公网报告（`g.ismayday.*`）是否继续发布？若发布，是否改为私有＋白名单公开 | 默认不发布；V1 报告含真实交易信息 | D8、M10 |
| Q2 | 旧「ML 挂单回溯」Tab（P-09）是否放弃 | 放弃（旧协议，不可当验收） | D16 |
| Q3 | 「财务统计」（年度现金流，P-06）是否放弃 | 放弃，由账本现金流视图取代 | D16 |
| Q4 | `/ml-next` 研究页（P-10）是并入 M5 记分牌/M6 操作单视图，还是保留独立研究页 | 并入，不单独保留 | D16 |
| Q5 | 个股下钻的「通用信息」「主力资金流向」（P-11、P-13）是否进入 M3 首版，或按方案作为 WP 3.6 后续项 | 后续项，不阻塞切换 | D16、LN-07 |
| Q6 | V1 历史成交/订单（自 2025-01）是否导入 V2（M2b）——仅作开账日前描述，无账户标识/费用/时区 | 导入，标 `pre_opening`，仅描述 | M2b |
| Q7 | V1 前向 shadow（D5，已在累积）在 V2 切换时是否继续、是否把其记录当作 V2 的 `pilot` 或另行归档 | 归档于 V1，不转为 V2 确认样本 | §3.7、§6A.7 |
| Q8 | V1 的 bandit/离线 RL/HMM 代码与实验脚本是否仅归档不迁移 | 归档不迁移 | D16 |
| Q9 | `purge`（按代码彻底删除数据）是否在 V2 不提供 | 不提供；账本只追加 | 方案不变量 |
| Q10 | 研究宇宙：沿用 V1 的 6 只，还是收敛（V1 名单硬编码在 6 处，迁移只需读 `ml/config.py:16-19`） | 6 只作研究宇宙（方案 D2 默认） | D2 |
| Q11 | 切换后 V1 在 8887 的过渡期内，是否继续手动运行 V1 的 `update.sh`/`ml.sh`（共用同一富途账户额度，V1 间隔已近 10 次/30 秒上限） | 过渡期只读参照，不再运行 V1 采集 | §3.7、WP0b.4 |
| Q12 | D1：`NV` 是否指 NVDA（V1 源码、V1 文档、`docs/V2项目 idea.md` 中均无 `NV` 字样，idea 里写的是 NVDA） | 假定 NVDA 并标「待确认」 | D1 |
