"""M3b（教练/记分牌/复盘/数据状态视图）测试的合成数据（合成测试值；不含任何真实信息）。其余 M3b 测试从这里导入。

在 `test_web_fixtures.build_demo_db` 的演示账本之上追加：比较批次 B1（美股，AI/人类计划/买入持有/真实成交四条线）、
AI 操作单（目标日 2026-03-11 与 2026-03-04，值使用**独特的哨兵值**，便于断言「密封时响应里绝没有这些值」）、人类线单、live_guidance 单、
暴露记录、记分牌 run、采集回执、预测版本、运行回执、协议冻结。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal as D

from mystock2.coach.decide import BUY, HOLD, SKIP, TicketDraft
from mystock2.coach.intents import reveal
from mystock2.coach.tickets import freeze_tickets
from mystock2.core import calendars as cal
from mystock2.core import db as dbmod
from mystock2.core.timeutil import iso_utc
from mystock2.scoreboard.lines import create_batch
from mystock2.scoreboard.types import ExecProtocol, LineState

from .test_web_fixtures import build_demo_db

UTC = timezone.utc
BATCH = "B1"
MARKET = "US"
TARGET = "2026-03-11"            # 展示用的最近目标日（演示时钟 NOW = 2026-03-11 06:00Z，早于项目截止）
OLD_TARGET = "2026-03-04"        # 复盘卡对应的目标日（NVDA 的卖出成交在这一天）
PROTO = "test-1"
STATE_REF = "STATE_REF_AAA"
FROZEN_AT = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)            # 协议冻结时间（早于全部操作单）
T_CLOSE = datetime(2026, 3, 10, 22, 30, tzinfo=UTC)           # close 阶段冻结
T_PREOPEN = datetime(2026, 3, 11, 5, 30, tzinfo=UTC)          # preopen 阶段冻结（早于 09:00 ET = 13:00Z 的截止）
T_OLD = datetime(2026, 3, 3, 22, 30, tzinfo=UTC)
DAYS = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10"]

# ---- 哨兵值：它们只会出现在 AI 单的内容里；密封时响应里绝不能有任何一个
SENT_LIMIT, SENT_QTY, SENT_RESERVED = "123.45", "777", "96000.65"
SENT_REASONS = ("edge_ok", "buy_target", "zz_sentinel_reason")
SENT_UNC = {"width": "0.07771", "c_rt": "0.00313"}
OLD_LIMIT, OLD_QTY = "88.88", "4242"
HUMAN_LIMIT, HUMAN_QTY = "55.55", "31337"
SENT_PRED_LOW, SENT_PRED_HIGH = "98.7654", "112.3456"
SENT_Y_LOW, SENT_Y_HIGH = "-0.098765", "0.054321"
LEAK_VALUES = [SENT_LIMIT, SENT_QTY, SENT_RESERVED, *SENT_REASONS, *SENT_UNC.values(), HUMAN_LIMIT, HUMAN_QTY, OLD_LIMIT, OLD_QTY,
               SENT_PRED_LOW, SENT_PRED_HIGH, SENT_Y_LOW, SENT_Y_HIGH]
LEAK_KEYS = ['"action"', '"limit_price"', '"qty"', '"reasons"', '"reason_json"', '"uncertainty"', '"frozen_hash"', '"state_ref"', '"reserved_cash"']
LEAK_WORDS = ["BUY", "SELL", "HOLD", "SKIP", "买入", "卖出", "持有", "不操作", "edge_ok", "no_edge", "buy_target"]


def _draft(code, action, limit=None, qty=None, reasons=(), unc=None, reserved=None):
    return TicketDraft(code, action, D(limit) if limit else None, qty, 1 if qty else None, D(reserved) if reserved else None, tuple(reasons), uncertainty=unc or {})


def _freeze(w, *, line, target, stage, drafts, now, kind="line_sim", state_ref=STATE_REF, protocol=PROTO, unavailable=None, deadline=None, ref_type="line_state"):
    t = date.fromisoformat(target)
    return freeze_tickets(w, batch_id=BATCH, line_id=f"{BATCH}:{line}", kind=kind, market=MARKET, target_session=t, stage=stage, drafts=drafts,
                          state_ref_type=ref_type, state_ref=state_ref, strategy_version="inv-policy-v1", protocol_version=protocol, generated_at=now, now=now,
                          deadline_at=deadline or cal.project_deadline(MARKET, t), unavailable_reason=unavailable)


def seed_batch(db, *, batch_id=BATCH, kinds=("ai", "human_plan", "buyhold", "human_actual"), codes=("US.NVDA", "US.TSLA", "US.AAPL")):
    w = dbmod.connect_writer(db, "scoreboard")
    create_batch(w, batch_id, ExecProtocol(), date(2026, 3, 2), LineState("USD", D("10000")), D("10000"), list(kinds),
                 meta={"market": MARKET, "codes": list(codes), "notes": ["cost_estimated:US.NVDA"]})
    w.close()


def seed_tickets(db, *, with_preopen=True, with_old=True):
    """AI 单：NVDA 买入（哨兵值）、TSLA 不操作（no_edge）、AAPL 缺数据（unavailable）；另有人类线单与 live_guidance 单（都不得出现在操作单视图）。"""
    w = dbmod.connect_writer(db, "coach")
    _freeze(w, line="ai", target=TARGET, stage="close", now=T_CLOSE, drafts=[
        _draft("US.NVDA", BUY, SENT_LIMIT, int(SENT_QTY), SENT_REASONS, SENT_UNC, SENT_RESERVED),
        _draft("US.TSLA", SKIP, reasons=("no_edge",)),
    ])
    _freeze(w, line="ai", target=TARGET, stage="close", now=T_CLOSE, unavailable="data_missing", drafts=[_draft("US.AAPL", SKIP)])
    if with_preopen:       # 盘前刷新：NVDA 的新版本（数量不同），TSLA 改为持有
        _freeze(w, line="ai", target=TARGET, stage="preopen", now=T_PREOPEN, drafts=[
            _draft("US.NVDA", BUY, "124.50", 555, ("edge_ok", "buy_target"), SENT_UNC, "69097.50"),
            _draft("US.TSLA", HOLD, reasons=("floor_above_predicted_high",)),
        ])
    _freeze(w, line="human_plan", target=TARGET, stage="human_plan", now=T_CLOSE,
            drafts=[_draft("US.NVDA", BUY, HUMAN_LIMIT, int(HUMAN_QTY), ("human_plan",))])
    _freeze(w, line="ai", target=TARGET, stage="close", now=T_CLOSE, kind="live_guidance", state_ref="SNAPSHOT_1", ref_type="account_snapshot",
            drafts=[_draft("US.NVDA", BUY, "77.77", 4747, ("live_guidance_sentinel",))])
    if with_old:
        _freeze(w, line="ai", target=OLD_TARGET, stage="close", now=T_OLD, drafts=[_draft("US.NVDA", BUY, OLD_LIMIT, int(OLD_QTY), ("edge_ok",))])
        _freeze(w, line="ai", target=OLD_TARGET, stage="preopen", now=datetime(2026, 3, 4, 15, 0, tzinfo=UTC), drafts=[_draft("US.AAPL", SKIP)])   # 晚于截止 → missed_deadline
    w.close()


def seed_late_ticket(db, *, target=TARGET):
    """直接写入一个 visible_at 晚于项目截止的 frozen 版本（正常冻结器会把它记成 missed_deadline；这里模拟库里已有这种行）。"""
    w = dbmod.connect_writer(db, "coach")
    row = w.execute("SELECT * FROM ticket WHERE kind='line_sim' AND line_id=? AND code='US.NVDA' AND target_session=? ORDER BY visible_at DESC LIMIT 1",
                    (f"{BATCH}:ai", target)).fetchone()
    cols = [c for c in row.keys()]
    vals = dict(zip(cols, tuple(row), strict=True))
    vals.update({"ticket_id": "late_ticket_0001", "frozen_hash": "late_hash_0001", "limit_price": "999.99", "qty": "9999", "visible_at": "2026-03-11T14:00:00.000000Z",
                 "frozen_at": "2026-03-11T14:00:00.000000Z", "stage": "preopen", "supersedes": row["ticket_id"]})
    w.execute(f"INSERT INTO ticket({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [vals[c] for c in cols])
    w.close()


def add_reveal(db, *, target=TARGET, at="2026-03-11T05:45:00.000000Z", channel="coach_show", batch=BATCH):
    w = dbmod.connect_writer(db, "coach")
    reveal(w, batch_id=batch, market=MARKET, target_session=date.fromisoformat(target), channel=channel, version_hashes=["h1"], at=at)
    w.close()


def seed_protocol(db, *, pilot=False, version=PROTO, frozen_at=FROZEN_AT):
    w = dbmod.connect_writer(db, "coach")
    summary = {"protocol_version": version, "hash": "ab" * 32, "pilot": pilot, "missing": ["strategy.max_hold_days"] if pilot else [], "universe_size": 3, "markets": ["US"]}
    w.execute("INSERT INTO protocol_freeze(protocol_version, protocol_hash, frozen_at, summary_json, code_sha) VALUES (?,?,?,?,?)",
              (version, "ab" * 32, iso_utc(frozen_at), json.dumps(summary), "deadbeef"))
    w.close()


# ---- 记分牌 run：权益序列（None 表示 UNKNOWN/PAUSED）
AI_EQ = ["10100", "10200", "10150", None, None, None, None]
AI_STATUS = ["OK", "OK", "OK", "UNKNOWN", "PAUSED", "PAUSED", "PAUSED"]
HP_EQ = ["10050", "10150", "10100", "10300", "10250", "10400", "10450"]
BH_EQ = ["10020", "10040", "10030", "10100", "10090", "10150", "10200"]
HA_EQ = ["9900", "9950", "9980", "10020", "10040", "10010", "10060"]       # human_actual：描述性（真实资金口径不同）


def seed_run(db, *, run_id="R1", batch=BATCH, created="2026-03-11T05:00:00.000000Z", ai_eq=AI_EQ, ai_status=AI_STATUS, metrics_json=None, with_actual=True,
             ai_state_hash="STATE_REF_AAA"):
    w = dbmod.connect_writer(db, "scoreboard")
    w.execute("INSERT INTO eval_run(run_id, batch_id, protocol_version, protocol_json, evidence_json, metrics_json, created_at) VALUES (?,?,?,?,?,?,?)",
              (run_id, batch, "exec-v1", json.dumps(ExecProtocol().as_dict()), json.dumps(["ev1", "ev2"]), metrics_json, iso_utc(created)))
    series = {"ai": (ai_eq, ai_status), "human_plan": (HP_EQ, ["OK"] * 7), "buyhold": (BH_EQ, ["OK"] * 7)}
    if with_actual:
        series["human_actual"] = (HA_EQ, ["OK"] * 7)
    for kind, (eq, st) in series.items():
        for d, e, s in zip(DAYS, eq, st, strict=True):
            flags = ["ambiguous_bar"] if (kind == "ai" and d == "2026-03-03") else []
            w.execute("INSERT INTO sleeve_daily(run_id, line_id, currency, date, status, equity, cash, unsettled, position_value, fees_day, fees_cum, positions_json, flags_json) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, f"{batch}:{kind}", "USD", d, s, e, None if e is None else "5000", None if e is None else "0",
                                                           None if e is None else str(D(e) - D("5000")), "1" if (kind == "ai" and d == "2026-03-02") else "0", None,
                                                           json.dumps({"US.NVDA": "10"}), json.dumps(flags)))
    w.execute("INSERT INTO sim_fill(run_id, line_id, date, seq, code, side, qty, price, fee, bar_start, ambiguous, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (run_id, f"{batch}:ai", "2026-03-02", 1, "US.NVDA", "BUY", "10", "100", "1", None, 0, None))
    w.execute("INSERT INTO line_state(run_id, line_id, currency, date, state_json, state_hash) VALUES (?,?,?,?,?,?)",
              (run_id, f"{batch}:ai", "USD", TARGET, "{}", ai_state_hash))
    w.close()


# ---- 数据状态
def seed_status(db):
    mk = dbmod.connect_writer(db, "market")
    rows = [
        ("a1", "US.NVDA", "daily", "yfinance", "ok", 7, None, "2026-03-10T23:00:00.000000Z"),
        ("a2", "US.NVDA", "daily", "yfinance", "error", 0, "HTTP 429 限流", "2026-03-11T05:00:00.000000Z"),
        ("a3", "HK.00700", "daily", "yfinance", "ok", 7, None, "2026-03-11T04:00:00.000000Z"),
        ("a4", "US.TSLA", "daily", "yfinance", "empty", 0, "返回空表", "2026-03-11T05:00:00.000000Z"),
        ("a5", "USDHKD", "fx", "yfinance", "ok", 7, None, "2026-03-10T23:00:00.000000Z"),
        ("a6", "US.OLD", "daily", "yfinance", "ok", 3, None, "2026-02-01T00:00:00.000000Z"),
    ]
    for aid, code, kind, src, st, n, detail, at in rows:
        mk.execute("INSERT INTO collection_log(attempt_id, run_id, code, kind, source, status, rows, detail, attempted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (aid, None, code, kind, src, st, n, detail, iso_utc(at)))
    mk.execute("INSERT INTO quote_hourly(code, bar_start, version, bar_end, source, open, high, low, close, volume, complete, received_at, content_hash) "
               "VALUES ('US.NVDA','2026-03-10T19:30:00.000000Z',1,'2026-03-10T20:30:00.000000Z','syn','100','101','99','100','1000',1,'2026-03-10T23:00:00.000000Z','h1')")
    mk.close()
    fw = dbmod.connect_writer(db, "forecast")
    for i, (code, target, gen) in enumerate([("US.NVDA", "2026-03-10", "2026-03-09T22:00:00.000000Z"), ("US.NVDA", TARGET, "2026-03-10T22:00:00.000000Z"),
                                             ("HK.00700", TARGET, "2026-03-10T10:00:00.000000Z")]):
        fw.execute("INSERT INTO prediction_version(prediction_id, code, as_of_session, target_session, model_version, feature_version, params_json, y_low, y_high, "
                   "low_price, high_price, scale, n_train, input_snapshot_ids, input_cutoff_at, generated_at, available_at, source_tag, content_hash, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (f"p{i}", code, "2026-03-09", target, "naive_vol-v1", "f1", "{}", SENT_Y_LOW, SENT_Y_HIGH, SENT_PRED_LOW, SENT_PRED_HIGH, "0.02", 250, "[]",
                    iso_utc(gen), iso_utc(gen), iso_utc(gen), "forward", f"ch{i}", iso_utc(gen)))
    fw.close()
    c = dbmod.connect_writer(db, "core")
    for rid, cmd, st, started, fin, detail, retry in [
        ("r1", "coach run --stage close", "ok", "2026-03-10T22:30:00.000000Z", "2026-03-10T22:31:00.000000Z", {"tickets": 3, "pilot": False, "missing": []}, None),
        ("r2", "collect futu", "partial", "2026-03-11T05:00:00.000000Z", "2026-03-11T05:02:00.000000Z", {"US": {"rows": 3}}, "US.TSLA daily"),
        ("r3", "scoreboard run", "failed", "2026-03-11T05:10:00.000000Z", "2026-03-11T05:10:05.000000Z", {"error": "ConfigError", "message": "SECRET_FREE_TEXT_999"}, None),
        ("r4", "db migrate", "running", "2026-03-11T01:00:00.000000Z", None, {}, None),
    ]:
        c.execute("INSERT INTO run_log(run_id, command, inputs_json, started_at, finished_at, status, detail_json, retry_scope) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, cmd, "{}", iso_utc(started), iso_utc(fin) if fin else None, st, json.dumps(detail), retry))
    c.close()


def build_ops_db(tmp_path, *, reveal_target=False, run=True, tickets=True, protocol=True, status=True, **kw):
    """演示账本 + 批次/操作单/run/协议/运行状态。`reveal_target=True` 时给目标日写一条暴露记录。"""
    db = build_demo_db(tmp_path, **kw)
    seed_batch(db)
    if tickets:
        seed_tickets(db)
    if protocol:
        seed_protocol(db)
    if run:
        seed_run(db)
    if status:
        seed_status(db)
    if reveal_target:
        add_reveal(db)
    return db
