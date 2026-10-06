# 日常数据更新（命令与例行任务）

| 项 | 内容 |
| --- | --- |
| 作者／日期 | Claude／2026-10-05 |
| 环境 | 与 V1 共用默认 conda 环境 `mk`（Python 3.10），在仓库根目录直接运行，无需切换环境、无需安装 |
| 前提 | 富途 OpenD 已启动并登录（只读采集；不解锁交易、不下单）。OpenD 没开时富途步骤失败，公开行情（yfinance）照常更新 |
| 私有配置 | `config.yaml` 的 `update:` 段（`account_id`、`acc_id`、`lookback_days`），被 `.gitignore` 忽略；富途流水映射在 `config/local/futu_cashflow_map.yaml` |

## 一条命令（手动）

```bash
python -m mystock2 update --phase us      # 美股收盘后：富途（成交/订单/费用/快照/流水）+ 美股行情 + 小时线归档 + 预测 + 对账
python -m mystock2 update --phase hk      # 港股收盘后：同上，港股
python -m mystock2 update --phase pre     # 美股开盘前：富途订单/快照 + 美股行情与小时线归档（轻量）
python -m mystock2 update --phase us --no-futu    # OpenD 没开：只更新公开行情
python -m mystock2 update --phase us --lookback 30   # 回看更久（补漏）
```

- 每步独立、幂等、失败不中断；最后输出汇总 JSON，**有失败或日线陈旧则退出码为 1**；加 `--notify` 弹 macOS 通知。
- 日志：`data/logs/update_<日期>_<阶段>.json`（含每步输出尾部）；launchd 的标准输出在 `data/logs/launchd_<阶段>.*.log`。
- 同一时刻只允许一个更新在跑（`data/logs/update.lock`），重叠的会被跳过。

## 例行任务（launchd，本机 Mac）

```bash
bash scripts/install_launchd.sh install     # 安装并加载 3 个任务（用户级 LaunchAgents）
bash scripts/install_launchd.sh status      # 查看
bash scripts/install_launchd.sh uninstall   # 卸载
```

| 任务 | 时间（本机 PDT） | 对应 | 做什么 |
| --- | --- | --- | --- |
| `com.mystock2.update.hk` | 周一至周五 02:15（主）、03:30、05:30、08:00（备份）；周六 10:00（收尾） | 港股 16:00 HKT 收盘＝本机 01:00（冬令 00:00） | 港股收盘后更新 |
| `com.mystock2.update.pre` | 周一至周五 05:45、06:05、06:20 | 美股 09:30 ET 开盘＝本机 06:30 | 盘前：富途订单/快照、美股行情、小时线归档（每天一次） |
| `com.mystock2.update.us` | 周一至周五 14:00（主）、14:45、16:00、18:00、21:00（备份）；周六 10:30（收尾） | 美股 16:00 ET 收盘＝本机 13:00 | 美股收盘后更新 |

**增量检查（备份时间点不会重复干活）**：每个阶段的目标是「该市场最近已收盘交易日」（pre 是「今天」）。运行结果记在 `data/logs/state.json`：同一目标已成功完成、**且成功发生在收盘 1 小时之后**（收盘后成交/费用还会陆续到）、**且日线没有陈旧**时，后面的时间点直接跳过（不连富途、不拉行情），只输出「已完成，跳过」。上次失败、日线陈旧、OpenD 当时没开，备份时间点会自动重试。`--force` 可忽略检查强制完整运行。

- Mac 睡眠时 launchd 会在唤醒后补跑；OpenD 需要在跑。节假日休市日：采集幂等，多跑无害（日历外的日线行会被丢弃）。
- **小时线只有近 60 天**（yfinance 官方），所以每天归档不能断；`pre/hk/us` 都会归档。

## 手动单步命令（排错/补漏）

```bash
python -m mystock2 collect futu --account-id main --acc-id <acc_id> --start 2026-10-01 --end 2026-10-05 --what deals,orders,fees,snapshot --assume-market-currency
python -m mystock2 collect futu --account-id main --acc-id <acc_id> --start 2026-10-01 --end 2026-10-05 --what cashflow --cashflow-map config/local/futu_cashflow_map.yaml
python -m mystock2 collect quotes --codes US.NVDA,HK.00700 --start 2026-09-20 --end 2026-10-05 --fx USDHKD,USDCNY
python -m mystock2 collect quotes --codes US.NVDA,HK.00700 --start 2026-09-20 --end 2026-10-05 --hourly
python -m mystock2 forecast run --start 2026-10-01 --end 2026-10-05 --model baseline     # 或 --model lgbm
python -m mystock2 ledger reconcile --account-id main                                   # 账本 vs 最新券商快照
python -m mystock2 web                                                                  # 只读 Web：http://127.0.0.1:8889/
```

- 对账基线：美元现金有已登记的既知残差 −13.84（FR-6），写在本机 `config.yaml` 的 `reconcile.known_cash_diffs`。对账输出仍在 `baseline_cash_diffs` 列出它；只有**偏离基线**（超过容差 0.01）、持仓不一致、待匹配或不完整 FX 组才算失败。查清并入账后删掉该基线。
- 对账不一致时先看 `data/logs` 与「数据状态」页，**不要**继续生成操作单。
- 资金流水里出现未映射的新类型会进待匹配队列（`ledger status` 可见数量）；确认含义后把类型加进 `config/local/futu_cashflow_map.yaml`（`DIVIDEND`/`DIVIDEND_WHT`/`ACCOUNT_FEE`/`EXTERNAL`/`WITHDRAW`/`DEPOSIT`/`INTEREST`/`TAX`/`RECON_ONLY`），再用 `--lookback` 重放（显式给 `--lookback` 的运行不会被增量检查跳过）。**已入账的类型改映射不会重放出第二笔**：同一流水号会报冲突，需要走更正流程。

## 重建（删库重来）

V2 账本是派生库，可以重建（V1 数据、富途历史、近期行情都能重新取）；但库里也有**补不回**的数据：V2 自己归档的小时线超过 60 天的部分、将来的前向记录（前向预测、操作单、人类计划、暴露日志）。所以脚本会先校验全部输入、拿例行更新的锁、把现库备份到 `backups/`，库里有前向记录时拒绝（确需重建另加 `DROP_FORWARD_RECORDS=yes`），删除的是当前配置里的 `db.path`：

```bash
CONFIRM=yes ACC_ID=<acc_id> V1_DB=data/v1_snapshot_20261005.db V1_ML_DB=data/v1_ml_snapshot_20261005.db OPEN_AT=2024-10-20T00:00:00Z \
CASHFLOW_FILE=data/cashflow_raw_<日期>.jsonl PY=/opt/anaconda3/envs/mk/bin/python bash scripts/rebuild_real_db.sh
```

资金流水逐日请求约 3 秒/天，所以保留了原始 JSONL 用于离线重放（`--cashflow-file`）。重建后再跑行情与预测（见上）。
