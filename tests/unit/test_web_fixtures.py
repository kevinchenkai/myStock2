"""Web 测试的合成数据与应用工厂（合成测试值；不含任何真实账户信息）。其余 test_web_*.py 从这里导入。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal as D
from unittest.mock import patch

from mystock2.core import db as dbmod
from mystock2.core.config import parse_config
from mystock2.ledger.events import (
    EventDraft,
    SourceDraft,
    ensure_account,
    fee_key,
    fill_key,
    flow_key,
    post_event,
    post_fx,
)
from mystock2.ledger.opening import create_snapshot, record_opening
from mystock2.ledger.projection import project
from mystock2.market import fx as fxmod
from mystock2.market.bars import DailyBar, put_daily

from .ledger_helpers import ACCT

UTC = timezone.utc
T0 = "2026-03-02T00:00:00Z"                       # 开账时点
NOW = datetime(2026, 3, 11, 6, 0, tzinfo=UTC)      # 固定时钟：US 最近收盘日 03-10；HK 03-11 尚未收盘 → 03-10
RECEIVED = datetime(2026, 3, 11, 5, 0, tzinfo=UTC)

NVDA_CLOSES = {"2026-03-02": "100", "2026-03-03": "100", "2026-03-04": "110", "2026-03-05": "104", "2026-03-06": "104",
               "2026-03-10": "106"}                 # 03-09 缺行情（缺口）
TENCENT_CLOSES = {"2026-03-02": "300", "2026-03-03": "310", "2026-03-04": "312", "2026-03-05": "320", "2026-03-06": "318",
                  "2026-03-09": "315", "2026-03-10": "316"}
FX_DATES = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10"]


def make_config(db_path, *, port=8889, extra=None):
    data = {"futu": {"host": "127.0.0.1", "port": 11111, "trd_env": "REAL"}, "collect": {"markets": ["HK", "US"]},
            "db": {"path": str(db_path)}, "web": {"host": "127.0.0.1", "port": port}}
    data.update(extra or {})
    return parse_config(data, base=db_path.parent)


def buy(conn, deal_id, code, qty, price, at, source=None):
    return _fill(conn, deal_id, code, D(qty), price, at, source)


def sell(conn, deal_id, code, qty, price, at):
    return _fill(conn, deal_id, code, -D(qty), price, at)


def _fill(conn, deal_id, code, qty, price, at, source=None):
    ccy = "HKD" if code.startswith("HK.") else "USD"
    return post_event(conn, EventDraft(fill_key(ACCT, deal_id), ACCT, "FILL", at, ccy, code=code, price=str(price), qty_delta=str(qty),
                                       cash_delta=str(-qty * D(price)), ref_deal_id=deal_id), source=source, received_at=RECEIVED)


def fee(conn, deal_id, amount, at, kind="commission", ccy="USD"):
    return post_event(conn, EventDraft(fee_key(ACCT, deal_id, kind), ACCT, "FEE", at, ccy, cash_delta=str(-D(amount)), ref_deal_id=deal_id),
                      received_at=RECEIVED)


def bar(code, day, close):
    c = D(close)
    return DailyBar(code, date.fromisoformat(day), str(c), str(c + 1), str(c - 1), str(c), str(c), "1000000")


def put_closes(market_conn, code, closes, *, quality="ok", received=RECEIVED):
    put_daily(market_conn, [bar(code, d, c) for d, c in closes.items()], source="synthetic", received_at=received, quality=quality)


def put_fx(market_conn, pair, rate, dates=FX_DATES, received=RECEIVED):
    for d in dates:
        fxmod.put_rate(market_conn, pair, date.fromisoformat(d), rate, source="synthetic", event_at=f"{d}T21:00:00Z", received_at=received)


@contextmanager
def frozen_received():
    """让账本里没有 received_at 参数的写入（如开账事件）也使用固定的收到时间，保证新鲜度测试可复现。"""
    with patch("mystock2.ledger.events.utc_now", lambda: RECEIVED), patch("mystock2.ledger.opening.utc_now", lambda: RECEIVED):
        yield


def build_demo_db(tmp_path, *, quotes=True, fx=True, snapshot="match", opening_cost=True):
    with frozen_received():
        return _build_demo_db(tmp_path, quotes=quotes, fx=fx, snapshot=snapshot, opening_cost=opening_cost)


def _build_demo_db(tmp_path, *, quotes=True, fx=True, snapshot="match", opening_cost=True):
    """演示账本（合成）：开账持仓 NVDA 100（快照有成本 80）、0700 200（快照无成本）、USD 10000、HKD 50000；
    之后买卖、大额入金、换汇；开账日前有一笔卖出。"""
    path = tmp_path / "demo.db"
    dbmod.migrate(path)
    led = dbmod.connect_writer(path, "ledger")
    ensure_account(led, ACCT, "futu", "REAL", "USD")
    sid = create_snapshot(led, ACCT, T0, "futu",
                          {"US.NVDA": {"qty": "100", "cost_basis": "80" if opening_cost else None},
                           "HK.00700": {"qty": "200"}},
                          {"USD": {"cash": "10000"}, "HKD": {"cash": "50000"}})
    record_opening(led, ACCT, T0, {"US.NVDA": "100", "HK.00700": "200"}, {"USD": "10000", "HKD": "50000"}, snapshot_id=sid)
    sell(led, "d0", "US.NVDA", 5, 90, "2026-02-27T15:00:00Z")                    # 开账前：只作描述
    fee(led, "d0", "1", "2026-02-27T15:00:00Z")
    buy(led, "d1", "US.NVDA", 10, 100, "2026-03-03T15:00:00Z", source=SourceDraft("futu", "d1", {"id": "d1"}))
    buy(led, "d1", "US.NVDA", 10, 100, "2026-03-03T15:00:00Z", source=SourceDraft("csv", "row-7", {"id": "d1-csv"}))   # 同一成交经第二条通道到达
    fee(led, "d1", "1", "2026-03-03T15:00:00Z")
    fee(led, "d1", "0.5", "2026-03-03T15:00:00Z", kind="platform")
    sell(led, "d2", "US.NVDA", 20, 110, "2026-03-04T15:00:00Z")
    fee(led, "d2", "1", "2026-03-04T15:00:00Z")
    buy(led, "d3", "HK.00700", 100, 310, "2026-03-03T02:00:00Z")
    fee(led, "d3", "5", "2026-03-03T02:00:00Z", ccy="HKD")
    sell(led, "d4", "HK.00700", 50, 320, "2026-03-05T02:00:00Z")                  # 来自无成本证据的开账持仓＋买入
    post_event(led, EventDraft(flow_key(ACCT, "dep1", "DEPOSIT"), ACCT, "DEPOSIT", "2026-03-05T15:00:00Z", "USD", cash_delta="50000"), received_at=RECEIVED)
    post_fx(led, ACCT, "fx1", "2026-03-06T15:00:00Z", "USD", "1000", "HKD", "7800", received_at=RECEIVED)
    led.close()

    mk = dbmod.connect_writer(path, "market")
    if quotes:
        put_closes(mk, "US.NVDA", NVDA_CLOSES)
        put_closes(mk, "HK.00700", TENCENT_CLOSES)
    if fx:
        put_fx(mk, "USDHKD", "7.8")
        put_fx(mk, "USDCNY", "7.2")
    mk.close()

    if snapshot:
        led = dbmod.connect_writer(path, "ledger")
        p = project(led, ACCT, as_of="2026-03-10T01:00:00Z")
        pos = {c: {"qty": str(q)} for c, q in p.positions.items()}
        pos["US.NVDA"]["cost_basis"] = "85"                      # 券商成本（快照原值）；0700 的快照没有成本
        cash = {c: {"cash": str(v)} for c, v in p.cash.items()}
        if snapshot == "mismatch":
            pos["US.NVDA"] = {"qty": str(p.positions["US.NVDA"] + 3), "cost_basis": "85"}
            cash["USD"] = {"cash": str(p.cash["USD"] + D("12.34"))}
        create_snapshot(led, ACCT, "2026-03-10T01:00:00Z", "futu", pos, cash)
        led.close()
    return path


def make_app(tmp_path, db_path=None, *, config_extra=None, **kw):
    from mystock2.web.app import create_app

    db_path = db_path or build_demo_db(tmp_path)
    kw.setdefault("clock", lambda: NOW)
    kw.setdefault("universe_path", "")
    kw.setdefault("views_config", None)
    return create_app(make_config(db_path, extra=config_extra), **kw)


def get_view(client, view_id, **params):
    r = client.get(f"/api/v/{view_id}", query_string=params)
    return r.status_code, r.get_json()


def test_demo_db_builds_and_all_views_answer(tmp_path):
    app = make_app(tmp_path)
    c = app.test_client()
    for vid in ("account_overview", "holdings", "trades", "pnl", "equity_trend", "fx"):
        code, body = get_view(c, vid)
        assert code == 200 and body["status"] == "ok", (vid, body)
