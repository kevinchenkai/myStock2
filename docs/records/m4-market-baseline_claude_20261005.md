# M4 行情与预测基线 完成回执

| 项 | 内容 |
| --- | --- |
| 里程碑 | M4（实施方案 §5 M4） |
| 基线 | `main` @ `7a10b73`（M2a 核心）→ 本回执所在提交见 Git |
| 状态 | **已完成（合成数据＋一次公开行情冒烟）**：WP4.1–4.6 落地；`pytest` 126 项通过，`ruff` 通过 |
| 依赖 | 新增 `yfinance==1.7.0`（与 V1 同版本）、测试用 `numpy`；迁移 `0003_market.sql` |

## 做了什么

| WP | 产出 | 位置 |
| --- | --- | --- |
| 4.1 | 日线/小时线/汇率版本化存储（原始价与复权价分列；修订追加新版本；非交易日与 OHLC 不自洽拒绝）；`missing_sessions` 列缺口不填充；采集器逐标的主/备源，失败/空/陈旧/partial 全部回执，不记零 | `market/bars.py`、`market/fx.py`、`collectors/quotes.py` |
| 4.2 | 不可变证据快照（内容哈希）＋时间链校验：晚于输入截止收到的行情被 `verify_inputs` 拒绝；行情修订产生新快照、旧快照仍可验证；`time_trust` 区分 exact / assumed_bar_end | `market/evidence.py` |
| 4.3 | 小时线从首日归档：版本化存储、bar 不得越过收盘（采集时按日历裁剪）、归档完整性粗检（无 bar、收盘后 bar、午休内 bar、无完整 bar） | `market/bars.py`、`collectors/quotes.py` |
| 4.4 | 证券规则：lot/tick 带有效期、来源、`verified` 标记；未知/未核实失败关闭；tick 档位按数据配置（**不内置任何市场价位表**）；限价买向下卖向上舍入并跨档复核 | `instruments/security_rule.py` |
| 4.5 | 透明基线预测器（标准化经验分位，纯 Python）：用复权价算收益与标签、原始收盘价换价位；只用 ≤T 的数据；输入不足/缺复权价/波动为 0/区间倒挂 → `ForecastUnavailable`，不外推 | `forecast/baseline.py` |
| 4.6 | 预测版本不可覆盖（触发器）；写入前校验证据快照与时间链；`forward`/`rebuilt` 标签分开；点时生成 `generate()`（只用 `received_at ≤ 截止` 且 `quality='ok'` 的行情，当日 partial 不入） | `forecast/versions.py`、`forecast/run.py` |

## 验证

- 预测器与独立 numpy 计算逐项一致（尺度、两侧分位、训练样本数）；确定性；扰动 T 之后的 bar 不改变 T 处预测；拆股场景下复权口径连续、价位按拆股后原始价；样本内低价命中率落在 α_low 附近（宽松校准检验，**不是收益证明**）。
- T-10（主备源/空结果/部分成功）、T-24（修订产生新快照、晚到数据不得进入较早截止）、T-38（晚到的 T 日行情不得被较早截止的预测使用，且不得用旧数据冒充）均有测试。
- **一次真实冒烟**：用 `YFinanceSource` 拉取 US.NVDA、HK.00700 近两周日线与 USDHKD 日汇率到**临时库**（scratchpad，不入库）：回执 ok、行数合理。公开行情接口，不涉及任何私有数据或账户。

## 偏差与说明（如实）

1. **依赖边界调整**：`collectors` 现在允许依赖 `ledger` 与 `market`（采集器把外部数据写入账本/行情），方案 §3.4 的原图未写；已更新 `tests/test_import_boundaries.py`。`forecast` 不依赖 `ledger`（保持）。
2. **`atomic()` 迁到 `core.db`**，供各模块共用（原在 `ledger.events`）。
3. 预测器的分位参数（`alpha_low=0.10`、`alpha_high=0.90`、窗口 20、训练 250、最小样本 60）是**版本参数的占位默认值，未依据数据调参**；M6 冻结协议时由负责人确认或经预注册选择，改动即新 `model_version`。
4. `put_daily` 的原始价来自供应商 float64，以 4 位小数入库（`_px`）；这是供应商精度限制，不是账本精度。
5. HK/US 小时线的供应商切分（如美股 09:30 起的整点 bar、港股午休前后）**未核实**；归档完整性只做结构检查，M0a 核实后再加严。
6. 近实时 `quoted` 不在本里程碑（M10）。

## 没做什么（边界）

- 未连接富途 OpenD 或任何券商接口；未读取真实账户数据；未读写 V1 数据库/运行目录；未改 V1 任何文件；未重启 V1；未碰 8888。
- 对外网络：仅向 yfinance（公开行情）发起了冒烟请求，请求内容为公开标的与日期区间；未向外部模型发送内容；未发布；无调度；无交易。
- 未写入 `data/mystock2.db` 真实数据。
