# M1 地基 完成回执

| 项 | 内容 |
| --- | --- |
| 里程碑 | M1（实施方案 §4.2、§5 M1） |
| 基线 | `main` @ `a6a78e5`（方案 v1.0 定稿）→ 本回执所在提交见 Git |
| 分支 | `main`（绿地仓库，小批提交） |
| 状态 | **已完成**：WP1.1–1.8 全部落地；`pytest` 67 项通过，`ruff` 无告警，`check_docs` OK |
| 环境 | conda `mk2`，Python 3.11.17；PyYAML 6.0.3、Flask 3.1.3、pytest 9.1.1、hypothesis 6.168.4、ruff 0.16.10（D9 默认值已采用） |

## 做了什么

| WP | 产出 | 位置 |
| --- | --- | --- |
| 1.1 | 包骨架、`pyproject.toml`、`environment.yml`、`python -m mystock2 --help` | `mystock2/`、`pyproject.toml` |
| 1.2 | `core.money`：float/bool/NaN 一律拒绝；规范十进制字符串；步长舍入；限价保守舍入（买向下、卖向上）；`Money` 禁止跨币种加减。`core.timeutil`：naive 时间拒绝、UTC 规范化、夏令时换算、F07 时间链检查 | `mystock2/core/money.py`、`timeutil.py` |
| 1.3 | `core.calendars`：HK/US 日历（V1 冻结 CSV 逐字节拷贝，来源见 PROVENANCE.md），失败关闭、半日市识别、项目截止按本地时区换算；覆盖 2020–2027，到期告警 | `mystock2/core/calendars.py`、`data/calendars/` |
| 1.4 | `core.db`：只读连接（`mode=ro`+`query_only`）、**写表授权器**（`set_authorizer`，未登记表与全部 DDL 默认拒绝）、迁移器（事务、校验和防篡改、版本连续性）；`core.runs`：`run_log` 回执 | `mystock2/core/db.py`、`runs.py`、`migrations/0001_core.sql` |
| 1.5 | `instruments.code_map`（借用 V1 规则、V2 重写测试，严格格式校验）、`universe` 校验器（T-16：`NV` 被拒并给候选；交易仓缺参数则不可执行） | `mystock2/instruments/` |
| 1.6 | 配置加载校验：Web 仅回环、开发期端口 8889、密码只走环境变量、缺字段报错；`config.example.yaml`、`config/*.example.yaml` 模板；`config/local/` 已忽略 | `mystock2/core/config.py`、`config/` |
| 1.7 | `tests/test_import_boundaries.py`（import 图强制 §3.4）、`scripts/check_docs.py`（链接与命名） | `tests/`、`scripts/` |
| 1.8 | AGENTS.md 回填验证命令；README 增加快速开始 | — |

## 验证

- `python -m pytest -q`：67 项通过（单元＋集成；含 T-08 部分、T-16）。
- `python -m ruff check .`：通过。`python scripts/check_docs.py`：OK。
- 验证过的关键行为：只读连接写入被拒；`instruments` 写连接不能写 `run_log`/`schema_migration`、不能 DDL；未登记的表对所有写入者失败关闭；迁移失败整体回滚；已应用迁移被改动则拒绝；`web` 目录不得出现写连接。

## 偏差与说明（如实）

1. **日历的 deadline/final_at 列不使用**：V1 的 09:00 HKT / 09:30 ET 是 V1 自己的协议；V2 的项目截止由 `project_deadline()` 按协议配置（默认 HK 08:30、US 09:00）计算。已在 PROVENANCE.md 说明。
2. **日历的恶劣天气处理**仅沿用 V1 已剔除的 2023-09-01、2023-09-08 两个休市日，**不是完整认证**；M0a 预审将复核（WP 0a.3）。
3. **`instrument` 表仅建表**，尚无写入逻辑（M2a/M4 用）。
4. T-08 在 M1 仅覆盖日历层（节假日、半日市、夏令时、午休字段）；「任务启动在截止前、完成在截止后」的 T-09 属 M6。
5. 方案 §5 WP1.4 的「研究类连接写账本被拒」：此刻尚无 `ledger_*` 表，测试以「未登记写入者不可连接」「他人表不可写」代替，账本表在 M2a 登记后补测。

## 没做什么（边界）

- 未连接富途 OpenD 或任何券商接口；未读取任何真实账户数据；未读写 V1 的数据库或运行目录；未修改 V1 任何文件；未重启 V1 服务；未触碰 8888 端口。
- 未向外部模型发送内容；未发布公开内容；未新增调度；无任何交易操作。
- 未创建 `data/mystock2.db` 的真实数据（测试均用临时目录）。
