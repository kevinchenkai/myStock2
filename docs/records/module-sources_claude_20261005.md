# V1 模块来源表（借用 vs 重写）

| 项 | 内容 |
| --- | --- |
| 作者 | Claude（子任务，M0a；对应方案 §3.1「代码复用」要求登记的模块来源表） |
| 日期 | 2026-10-05 |
| V1 基线 | `ca16e31ad8f78b23e2ceabbd58dba0a01b55e35e`（借用时的「来源提交 SHA」以此为准；读取的是工作树文件，未核验是否含未提交改动） |
| V2 基线 | 实施方案 v1.0（头部记载 `main` @ `504d3fc`，未核验） |
| 状态 | **建议稿**，各里程碑开工前由执笔方/负责人逐模块确认；本表不改变任何代码 |
| 边界 | 只读 V1 源码；**未**运行 V1 代码或测试、**未**读 V1 数据库与 `config.yaml`、**未**连接券商、**未**改任何 V1 文件、**未**执行 git 写操作。质量评估是读代码得出的判断，**不等于**运行验证：V1 测试是否通过**未验证** |
| 规则（方案 §3.1） | 借用＝**拷贝＋重写测试**，不 `import` V1，文件头注明来源提交 SHA 与 V1 路径；V1 的口径缺陷（float、无账户维度、无费用、重撮合）**不得被借用代码带入**；测试断言不按现有实现照抄（§8.1）。V1 仓库为 Apache-2.0（其 `LICENSE` 首部）；拷贝须保留来源与许可声明 |
| 行数 | `wc -l` 所得（含注释与空行）；CSV 为数据行 |

建议图例：**借用**（拷贝后重写测试，列出必改点）／**借用设计**（只借思路与接口，代码重写）／**重写**（V2 新实现，V1 仅作参考）／**仅参考**／**放弃**。

## 1 采集与代码映射（M1–M4）

| 模块 | 路径 | 行数 | 质量评估（读代码后的判断） | 已知缺陷 | 建议 | 必改点 | 里程碑 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 代码映射 `code_map` | `mystock/code_map.py` | 69 | **好**：纯函数、无依赖、有 7 个单测（`tests/test_code_map.py`）；HK 5 位↔yfinance 4 位规则清晰 | 仅识别 HK/US；`yf_to_futu` 对非 `.HK` 一律当美股；无「未知代码给候选」逻辑（方案 T-16 要求 `NV` 被拒并给候选） | **借用** | 加 universe 校验与候选建议；超 4 位港股码边界补测；重写测试 | M1（WP1.5） |
| 富途客户端 `futu_client` | `mystock/collectors/futu_client.py` | 532 | **中**：分段（80 天窗口）、固定间隔限频＋退避重试的思路可用；约三成行数是行→dict 映射 | 无 `acc_id`/`acc_index`、`SecurityFirm.FUTUSECURITIES` 硬编码（`:153-158`）；限频关键字含过宽的 `"max"`（`:67`，可能误判其他报错为限频）；每个市场、每次采集重建连接；`fund_row` 只取 `iloc[0]`（`:395`）；全程 `float`/pandas 转 `REAL`，`raw_json` 无法还原十进制原文（`:305-310`）；无资金流水与订单费用接口；`cost_price/diluted_cost/average_cost` 列链混用（`:325`） | **借用设计 → 重写** | 显式 `acc_id` 与券商实体；Decimal/字符串落地；限频按官方每接口额度配置；增加 `get_acc_cash_flow`（逐日）与 `order_fee_query`（≤400 单/次）；运行回执与错峰；**须授权才连真实账户** | M2a（WP2.7） |
| 行情快照规整 `snapshot` | `mystock/collectors/snapshot.py` | 65 | **好**：纯函数；对来源时间做时区推断并**拒绝歧义/不存在的本地时间**；停牌/状态不把 NaN 当有效值 | `lot_size/price_spread` 取值后被当「规则」写入时用 `rules_effective_from=now[:10]`（在 `futu_client.py:476`），语义是观察日而非生效日 | **借用** | 保留时区与「歧义即未知」逻辑；规则表另建带生效期的 `security_rule` | M4（WP4.4） |
| yfinance 客户端 `yf_client` | `mystock/collectors/yf_client.py` | 262 | **中**：`end` 排他修正（`:54-67`）、限频更长指数退避、显式 `auto_adjust=False` 同时保留 close/adj_close 都是对的 | 日线**不判断 bar 是否已收盘**：增量终点取今天（`pipelines/update_load.py:39-65`），盘中抓取可能写入未完成的当日 bar（ML 侧 `ml/fetch.py` 才有完成态守卫）；`float`；`fetch_profile` 用 `Ticker.info`，无发布时间；不留 `prepost/repair` 取值；日志级别全局压到 CRITICAL（`:25`）会吞告警 | **借用设计 → 重写** | 每次请求写回执（版本、参数、`auto_adjust`/`prepost`/`repair`）；bar 完成态与 `received_at`；原始价与复权价分列；失败不记零 | M4（WP4.1–4.3） |
| ML 侧采集 `fetch` / 读取 `data` | `mystock/ml/fetch.py`、`ml/data.py` | 322 / 109 | **中**：「完整 bar 才入库」「按缺口挑增量窗」「小时桶跨午休的完整性判断」（`data.py:85-109`）有价值 | 写死 6 只标的；`period="730d"` 是 Yahoo 端经验值，yfinance 官方只写 60 天；整步任一标的失败即非零（OPEN_ITEMS P2-5）；`INSERT OR REPLACE` 覆盖旧 bar（`ml/db.py:81`） | **借用设计** | 只借「完整性判断」思路；证据快照不覆盖（WP4.2） | M4 |

## 2 日历、证券规则与版本留档（M1/M4）

| 模块 | 路径 | 行数 | 质量评估 | 已知缺陷 | 建议 | 必改点 | 里程碑 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 交易日历 `sessions` | `mystock/ml/sessions.py` | 199 | **设计好、实现密**：离线、版本号、**日历外失败关闭**、决策窗口与完成态（`final_at`）、日历到期预警（<60 天）、HK 午休与半日市纳入 `hourly_final`；有 6 个测试 | 代码极度压缩（分号/单行 if），可读性差；对无时区时间戳用「−14 小时」启发式补时区（`:126,143`）；`deadline` 固定 09:00 HKT / 09:30 ET（方案为 08:30/09:00，须参数化）；CAS 不建模；日历只到 2027-12-31 | **借用（设计与接口）** | 格式化并拆函数；时区未知一律「未知」而非猜测；决策截止参数化；重写测试（含临时停市） | M1（WP1.3，T-08） |
| 日历数据与生成器 | `mystock/ml/calendars/{HK,US}.csv`（1971/2010 行）、`scripts/ml_experiments/freeze_calendar.py` | 29（脚本） | **可用但须复核**：US 与 NYSE 公告抽查一致（见总报告 §4 #5）；HK 半日市行已核对；README 记录 2023 两个台风整日停市的手工剔除 | HK 恶劣天气仅这两日手工修正，**不是**对所有历史天气事件的核证（README 自述）；生成依赖 pandas_market_calendars 5.1.3/exchange_calendars 4.11.1 的隔离环境 | **借用设计，数据重新生成并核对** | 以官方公告逐年核；2024-09-23 后恶劣天气规则、CAS、午休停牌待 M0a 遗留的「需核实」项闭合后再写 | M1 |
| 证券规则 `rules` | `mystock/ml/rules.py` | 39 | **中**：`SecurityRule` 带来源与生效期的方向对；`round_quote` 用 `Decimal`，买向下/卖向上与方案一致 | `tick_size` 只有单值（取自 `price_spread`，官方定义为**当前上浮间隔**，不是价位表）；`from_snapshot` 把观察日当 `effective_from` | **借用设计** | 价位表＋生效期＋来源；每手股数随证券；未知标「规则未知」 | M4（WP4.4） |
| 预测版本 `versions` / 回执 `pipeline` | `mystock/ml/versions.py`、`pipeline.py` | 83 / 62 | **好（设计）**：内容哈希、同 run 冲突拒绝、只追加、来源标签（live/backfill/recomputed）、live 行的时间链校验（`versions.py:32-39`）、发布前校验产物哈希 | 风格压缩；`published` 状态耦合公网发布；`legacy` 投影表并存增加复杂度 | **借用设计** | 对齐 `prediction_version`/`evidence_snapshot` 的时间链 `received_at ≤ input_cutoff_at ≤ generated_at ≤ frozen_at`；去掉发布耦合 | M4（WP4.2、4.6） |
| 运行清单 `runs` | `mystock/ml/runs.py` | 33 | **差**：每次整库 `backup` 到 `runs/<id>/input.db`（OPEN_ITEMS P2-4）、`subprocess git` 硬依赖 | 存储无限增长；非 git 环境失败 | **放弃** | V2 用 `run_log`（WP1.4）：输入引用＋哈希，不整库拷贝 | M1 |

## 3 预测与评估（M4 基线、M8 候选线）

| 模块 | 路径 | 行数 | 质量评估 | 已知缺陷 | 建议 | 必改点 | 里程碑 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CQR 校准 `calibrator` | `mystock/ml/calibrator.py` | 161 | **好**：纯 numpy、公式与有限样本分位修正正确、有 10 个单测 | `split_calibrate` 无调用方（死代码，`:117`）；时间序列不满足可交换性，覆盖保证只是近似（文件内已提示实测） | **借用** | 删死代码；重写测试；预注册目标覆盖率 | M8 |
| 区间模型 `predictor` | `mystock/ml/predictor.py` | 374 | **中**：`IntervalModel` 把校准段从拟合段中**切开并留隔离行**（`:98-124`），做法正确；`predict_next_day` 把会话守卫、V1/V2 特征、拟合混在一个 ~100 行函数里 | 缺 LightGBM **静默回退** sklearn（`:44-48,53-68`；OPEN_ITEMS R-04）；float；`walk_forward_eval` 与生产路径并存 | **借用设计 → 重写 `predict_next_day`** | 后端缺失明确失败；拆分守卫/特征/拟合；不带 V2 隔夜特征 | M8 |
| 特征 `features` | `mystock/ml/features.py` | 154 | **中**：只用 T 日及以前信息，有「不跨缺口」处理 | 用 `adj_close/close` 比例调整 OHLC：该比例是抓取时点的**事后复权因子**，回看历史含此后才发生的分红调整（轻微前视，量级**需核实**）；标签用未复权价，口径混合（R-03） | **借用（V1 的 16 个特征）** | 核实前视量级；V2 特征组（`adr_ret/pre_ret`）不借 | M8 |
| 透明基线与指标 `evaluation` | `mystock/ml/evaluation.py` | 45 | **好（思想）**：`naive_vol`＝训练期收益/波动率分位×当日波动率，即方案首条 AI 线的透明基线；含 pinball/skill、块重采样 `block_interval` | 极度压缩；`block_interval` 的块长与种子写死，须按 §7 预注册；`vol_20d` 依赖 `features` | **借用设计** | 基线预测器与统计分开；块长度/种子入协议 | M4（WP4.5）、M5（WP5.5） |
| 切分与信号评估 `cv`、`signal_eval` | `mystock/ml/cv.py`、`signal_eval.py` | 63 / 147 | **好**：纯函数、说明了 purge 修什么、不修什么（调参窥视需锁箱） | `embargo` 字段在扩张窗下是 no-op | **借用** | 重写测试 | M8 |
| 目标/外部数据/尺度/模型矩阵 | `ml/models.py`、`scales.py`、`external.py`、`preopen.py`、`shadow.py` | 125/92/90/241/190 | 实验性；首轮矩阵已判负（OPEN_ITEMS） | 依赖 catboost/xgboost/arch 等可选包；shadow 含盘前/隔夜特征 | **放弃（归档）** | M8 如要新候选，另起实验 | — |

## 4 撮合、账户与盈亏（M2a/M5/M7）

| 模块 | 路径 | 行数 | 质量评估 | 已知缺陷 | 建议 | 必改点 | 里程碑 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 撮合与单标的账户 `simulator` | `mystock/ml/simulator.py` | 88 | **小而清晰**：先触低/先触高顺序、买价 `min(限价, 开盘)`（与方案「跳空按开盘价」的候选一致） | float；无部分成交、无成交量上限、无 tick/碎股、同 bar 双边无歧义标记；买卖静默忽略无效单（`:67,75`）；无费用 | **重写（仅参考）** | 按 M5「执行协议 ADR」；Decimal；歧义显式 | M5 |
| 受约束回放 `execution` | `mystock/ml/execution.py` | 151 | **中**：预留现金/库存、同 bar 歧义显式、拆股与股息应收的**意识**接近 V2 | 股息应收用**当日持仓×每股股息**（`:72-75`），正是方案 §6 不变量 4 已更正的错误基数；无税、无结算 T+N；单一 bps+固定费；`float`；压缩风格 | **仅参考** | 以 V2 账本不变量与 T-22/T-23 重写 | M2a/M5 |
| 已实现盈亏 `pnl` | `mystock/pnl.py` | 448 | **中**：测试较全（21 个，`tests/test_pnl.py`）；移动平均/FIFO/年度现金流三套口径并存且文档诚实 | E6：兜底成本是事后成本；不处理拆股；无费用；`float`；`analyze_stock` 的文字观察含价值判断（「小赚大亏」）需改成事实/推测分开 | **仅参考（FIFO 诊断回合思路用于 M7）**；不借 `compute_pnl`/`yearly_finance` | 诊断回合改名并带费用与拆股；成本口径入指标字典 | M7（WP7.4）、M5（WP5.4） |
| 回测/策略/bandit/RL | `ml/backtest.py`、`strategy.py`、`policy.py`、`offline_rl.py`、`calibrate.py` | 272/306/124/213/69 | 旧协议，E7/E8/E9 的来源；bandit/RL 为负结果 | 见总报告 §1 | **放弃** | — | — |

## 5 存储与配置（M1）

| 模块 | 路径 | 行数 | 质量评估 | 已知缺陷 | 建议 | 必改点 | 里程碑 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 库访问 `db` | `mystock/db.py` | 321 | **中**：UPSERT 幂等、列迁移、`get_connection_readonly`（`mode=ro` + `query_only`，`:32-36`）可用 | 无账户维度；UPSERT 覆盖当日（`replace_position_snapshot` 先删后插）；`purge_code` 不可逆删除；`snapshot_date` 用本机本地日期（`:89-90`） | **借用（仅 `get_connection_readonly` 模式）**，其余**重写** | V2 用只追加迁移与写表授权器 | M1（WP1.4） |
| 配置 `config` | `mystock/config.py` | 100 | **中**：环境变量覆盖交易密码；缺 `config.yaml` 回退模板并提示 | 模块级单例加载（`CONFIG = load_config()`）；无校验；无私有/公开配置分离；无标的名单 | **重写** | 公开模板＋`config/local/` 分离、缺字段明确报错（WP1.6） | M1 |
| Schema | `mystock/schema.sql`、`ml/schema.sql` | 207 / 178 | 见 E1–E4 | 全 `REAL`、无账户、无费用/资金流水 | **放弃**（仅供 M2b 导入器读取旧表） | — | M2b |

## 6 Web 与前端（M3）

| 模块 | 路径 | 行数 | 质量评估 | 已知缺陷 | 建议 | 必改点 | 里程碑 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Flask 应用 `app` | `mystock/web/app.py` | 507 | **中**：`get_db()` 只读模式（`:67-76`）是 V2 想要的；错误不泄露路径（`:83-85`） | 顶层 import `ml_api`→numpy/pandas（E11）；路由与 SQL 混在一个文件；`main()` 之后又注册路由（`:482-503`）；ML 业务硬编码标的 | **借用（`get_db` 与 503 处理）**，其余**重写**为视图框架 | 视图自动发现；无写入/无外部请求测试 | M3（WP3.1） |
| 数据状态 `data_status`＋`data-status.js` | `mystock/web/data_status.py`、`static/data-status.js` | 178 / 60 | **好（设计）**：状态枚举、来源时间 vs 采集时间、未知时区显示「时区未知」、旧库提示迁移而不在 Web 迁移 | 依赖 `ml.sessions`；专为 V1 表结构 | **借用设计** | 并入每视图的新鲜度头部 | M3（WP3.2） |
| 前端 `app.js` | `mystock/web/static/app.js` | 1713 | **中**：单文件、无构建；格式化/涨跌色工具（`plClass/fmtNum`）、图表挂载与销毁可参考 | 全局状态与渲染耦合；入金不剔除；缺口无标注；仅适配 V1 API | **重写（工具函数可拷贝）** | 视图模块 `panel.js` | M3 |
| 主题与样式 | `static/theme.js`（47）、`style.css`（726）、`_theme_head.html` | — | **好**：三态主题、首屏防闪烁、红涨绿跌变量 | CSS 绑 V1 DOM | **借用（`theme.js`；样式变量）** | 无 | M3（WP3.4） |
| K 线库 | `static/vendor/lightweight-charts.standalone.production.js` | 163,684 字节（单文件，v4.2.3） | 成熟第三方库；方案已决定本地 vendor | 许可与归属声明（文件头 `@license TradingView`）的具体条款**需核实** | **借用** | 保留许可头；记录版本与来源 | M3 |
| 公网报告 `report` | `mystock/ml/report.py` | 756 | 自包含 HTML 生成；仍走旧回测协议（R-01） | 与旧协议耦合；正则改 HTML（OPEN_ITEMS P3-6） | **放弃（待 D8）** | — | M10 |

## 7 脚本与工具（M1）

| 模块 | 路径 | 行数 | 建议 | 说明 |
| --- | --- | --- | --- | --- |
| 文档检查 | `scripts/check_docs.py` | 125 | **借用（参考）** | 方案 WP1.7；V2 命名/目录规则与 V1 相近 |
| `init.sh`/`update.sh`/`server.sh` | `scripts/*.sh` | 各 ~30–40 | **重写** | V2 用 `python -m mystock2`；V1 脚本无调度、直接 `conda activate mk` |
| `ml.sh` 与 H20 三脚本 | `scripts/ml.sh`、`ml_setup_h20.sh`、`ml_sync_h20.sh`、`ml_vllm.sh` | — | **放弃，且不得拷贝** | 含默认远端主机/目录与 GPU 机私有信息（此处不转录）；V2 为公开仓库，**这类默认值不得进入 V2** |

## 8 汇总

- **可直接借用（拷贝＋重写测试）**：`code_map`、`snapshot`、`calibrator`、`cv`、`signal_eval`、`features`（V1 的 16 个）、`theme.js`、`get_db` 只读模式、`lightweight-charts`、`check_docs.py`。
- **借用设计、代码重写**：`futu_client`、`yf_client`、`sessions`（＋日历数据重新生成并核对）、`rules`、`versions/pipeline`、`evaluation`、`data_status`。
- **重写（V1 仅参考）**：撮合（`simulator`/`execution`）、盈亏（`pnl`）、配置、库访问、前端主体。
- **放弃**：`runs`、回测/策略/bandit/RL、`report`（待 D8）、ML 实验矩阵与 shadow、GPU/发布脚本。
- **不确定、需在借用前核实**：`features` 的事后复权前视量级；lightweight-charts 的许可/归属要求；V1 测试在 `mk` 环境是否全绿（未运行）。
