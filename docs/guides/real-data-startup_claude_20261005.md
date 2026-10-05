# 真实数据启动指南（从合成到真实前向观察）

| 项 | 内容 |
| --- | --- |
| 作者／日期 | Claude／2026-10-05 |
| 基线 | `main` 当前提交；对应实施方案 v1.1 §5 M0b–M6、§6A、§7 |
| 状态 | 指南（尚未执行任何一步真实操作）。**每个标 🔑 的步骤都需要负责人明确授权后才能执行** |
| 边界 | 本指南不含任何真实数值；`config/local/`、`data/`、`exports/` 均被 `.gitignore` 忽略，不会提交 |

目标：用你的真实账户把「基线 AI 线 + 人类计划线 + 买入持有线」跑起来，并开始**合格的前向观察**。前向观察的天数无法加速，所以先把下面的前置事项做完。

## 0 你需要先决定/提供的（来自 `docs/OPEN_ITEMS.md`）

| 编号 | 需要的内容 | 为什么阻塞 |
| --- | --- | --- |
| D1 | `NV` 是否指 NVDA | 确认前不出可执行操作单 |
| D3 | 开账日 `D0`、每币种预算 `B`（基准币种） | 决定共同初始权益 `E0` |
| D4 | 核心仓/交易仓边界；`max_weight`、`max_lots`；是否允许加仓 | 缺则不生成可执行数量 |
| D5 | 费用来源（券商接口/自填）与费率 | 费用档案缺失则运行报错（不会当 0） |
| D6 | `max_hold_days`、到期退出是否覆盖最低获利（默认覆盖） | 缺则不生成可执行数量 |
| D10 | 富途**券商主体/账户类型**、是否仅实盘 | 资金流水接口不支持 moomoo US；`futu.security_firm` 必填 |
| D12 | 是否愿意**事前**记录结构化人类计划 | 否则只能做描述性比较 |
| D14/D15 | 首次评审时点与显著性方法；风险处理选择（可明确「不设闸」） | 冻结协议前必须 |
| 另 | 结算周期（美 T+1 已核实；**港股 T+2 请按券商规则确认**）、HK/US 的 tick 档位与每手股数（`security_rule`） | 未配置则失败关闭 |

## 1 环境与私有配置（本机，不涉账户）

```bash
# 与 V1 共用默认环境 mk（Python 3.10；futu-api/yfinance/Flask/numpy/lightgbm 均已在其中），在仓库根目录直接运行，无需切换环境
cp config.example.yaml config.yaml          # web.port 开发期 8889；V1 仍在 8888
mkdir -p config/local
cp config/universe.example.yaml config/local/universe.yaml     # 改成真实名单（tier/max_weight/max_lots）
cp config/protocol.example.yaml config/local/protocol.yaml     # 逐项填写；null 项＝尚未决定
cp config/fees.example.yaml config/local/fees.yaml             # 填真实费率
python -m mystock2 db migrate
python -m mystock2 universe check --file config/local/universe.yaml
```

交易密码只通过环境变量 `MYSTOCK2_FUTU_TRADE_PWD`（若需要），配置文件里出现非空 `trade_pwd` 会被拒绝。

## 2 公开行情（无需授权，只涉及公开数据）

用 `YFinanceSource` 拉取名单标的的日线、小时线与汇率写入库（采集器逐标的主备源、失败不记零、小时线从首日起归档）。**小时线必须从现在开始每天归档**——yfinance 官方只承诺 60 天，缺了回不来。

```bash
python -m mystock2 collect quotes --start 2026-01-01 --end 2026-10-02 --fx USDHKD,USDCNY          # 日线（名单内标的）与汇率
python -m mystock2 collect quotes --start 2026-09-25 --end 2026-10-02 --hourly                      # 小时线：必须每天运行以从首日归档
```

回执写入 `run_log` 与 `collection_log`；失败/空结果如实记录并以非零退出，不记零。

## 3 🔑 M0b：只读核对 V1（授权访问 V1 运行库）

```bash
python -m mystock2 v1 import --v1-db <V1 的 data/mystock.db 路径> --account-id <遗留账户占位> --dry-run
```

`--dry-run` 只读 V1 并打印报告（成交数、跳过原因、量化舍入差、时区推断数、缺口），不写任何库。确认 `account-id` 与将来 Futu 采集同一个后，再去掉 `--dry-run` 真正导入。V1 库只以 `mode=ro` 打开，V1 文件不会被改动。

## 4 🔑 M2a：连接 OpenD（授权连接真实账户；**采集器尚未对真实 OpenD 验证**）

先小范围试跑并逐项核对字段（见 `docs/records/m2a-ledger-core_claude_20261005.md` 补记）：

```bash
# config.yaml 的 futu 段需有 security_firm、min_interval（与 V1 错峰，V1 已占用约 94% 历史接口额度）
python -m mystock2 collect futu --account-id <ID> --acc-id <acc_id> --start 2026-09-01 --end 2026-09-30 --what snapshot
python -m mystock2 collect futu --account-id <ID> --acc-id <acc_id> --start 2026-09-01 --end 2026-09-30 --what deals,fees
python -m mystock2 collect futu ... --what cashflow --cashflow-map config/local/futu_cashflow_map.yaml   # 先不带映射看未映射类型，再填映射
```

首跑要核对：成交字段与时间、`order_fee_query` 的币种与港股费用项、资金流水的类型字符串与金额符号、`accinfo_query` 的币种拆分、`cost_price` 是否每股。

## 5 开账与对账

```bash
python -m mystock2 ledger open --account-id <ID>                 # 以最新快照开账（t0 不可改；此前成交只作描述）
python -m mystock2 ledger reconcile --account-id <ID>            # 持仓必须逐标的一致；现金差异逐项解释
python -m mystock2 ledger status --account-id <ID>
```

对账不一致时**不要**继续：先解决（缺费用、缺资金流、公司行动未识别）。

## 6 批次、协议冻结与每日流程

1. 填好 `config/local/protocol.yaml` 的全部必填项（`python -m mystock2 protocol freeze` 会告诉你缺什么），先 `--pilot` 试运行，**确认无误再正式冻结**（冻结前的记录一律 `pilot`，不转为确认样本）。
2. `batch create --id B1 --market US --d0 <D0> --currency USD --budget <B> --lines ai,human_plan,buyhold [--positions '{"US.NVDA": 10}']`（开账时属于交易仓的库存；无券商成本时单位成本估计为 D0 收盘价并在批次元数据标 `cost_estimated`）。
3. **每日**（每个市场各一次）：
   - 收盘后：采集行情/成交 → `coach run --batch B1 --market US --stage close`（只输出回执，**不打印任何动作/限价**）。
   - **先** `intent state` 看人类线自己的状态，再 `intent add` 记录你自己的计划（含 `no_trade`）；
   - 然后才 `coach show`（受控揭示，写暴露日志，此后记录的计划标 `seen_ai=1`）。
   - 开盘前：`coach run --stage preopen`；截止时 `intent freeze`；交易后导入真实成交。
4. 定期 `scoreboard run --batch B1 --end <日期>`（新 run，不覆盖旧 run）。

## 7 前向观察期的纪律（摘自方案 §6A、§7）

- 不看 AI 单就先记人类计划；`seen_ai=0` 才算独立对照。
- 缺失计划＝「无订单」且计入分母；不事后补写。
- 区间宽度门槛、参数、窗口属冻结协议，**不依据事后高低点调整**；改动＝新协议版本＝新批次。
- 小样本期（<120 交易日）结论一律「继续观察」；滚动区间只作描述，不触发晋级；模拟线结论不等于「按指导操作的结论」（§6A.9）。
