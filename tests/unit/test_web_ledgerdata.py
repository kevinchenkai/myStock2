"""web/ledgerdata：开账成本证据、费用币种归属（审核 P1-2、P1-7）。合成测试值。"""
from decimal import Decimal as D

from mystock2.ledger.opening import add_split, create_snapshot, record_opening
from mystock2.ledger.pnl import compute_realized_pnl
from mystock2.web.ledgerdata import load_trades

from .ledger_helpers import ACCT, D3, T0, make_db, sell


def test_p1_2_opening_cost_evidence_is_converted_back_across_a_split(tmp_path):
    """开账 10 股（真实成本 100），之后 1 拆 2，拆股后快照平均成本 50：开账成本证据应为 100（pnl 再按因子调整成 50），不能被除两次。"""
    c = make_db(tmp_path)
    record_opening(c, ACCT, T0, {"US.X": "10"}, {"USD": "0"})
    add_split(c, "US.X", "2026-03-03T13:00:00Z", 2, 1)
    create_snapshot(c, ACCT, "2026-03-04T00:00:00Z", "futu", {"US.X": {"qty": "20", "average_cost": "50"}}, {"USD": {"cash": "0"}})
    sell(c, "s1", "US.X", "20", "60", D3)
    t = load_trades(c, ACCT)
    op = next(e for e in t.trade_events if e.kind == "OPENING")
    assert op.price == D(100)
    s = compute_realized_pnl(t.trade_events, t.opening_at).sells[0]
    assert s.avg_cost == D(50) and s.realized == D(200)


def test_p1_7_fee_in_another_currency_is_not_added_to_the_fill_currency(tmp_path):
    """给 USD 成交追加一笔 78 HKD 的平台费：不得并入 USD 合计、不得标成 USD；单列并加标签。"""
    from mystock2.core import db as dbmod

    from .test_web_fixtures import build_demo_db, fee, frozen_received, get_view, make_app

    path = build_demo_db(tmp_path)
    led = dbmod.connect_writer(path, "ledger")
    with frozen_received():
        fee(led, "d2", "78", "2026-03-04T15:00:00.000000Z", kind="platform", ccy="HKD")
    led.close()
    c = make_app(tmp_path, path).test_client()
    _, tr = get_view(c, "trades")
    row = next(r for r in tr["data"]["rows"] if r["deal_id"] == "d2")
    assert row["fee"]["text"] == "1.00 USD" and row["fee"]["tag"] == "另有外币费用"
    assert "78.00 HKD" in row["fee_detail"] and "78.00 USD" not in row["fee_detail"]
    assert row["net_cashflow"]["text"] == "+2,199.00 USD"
    _, pn = get_view(c, "pnl")
    usd = next(s for s in pn["data"]["summary"] if s["currency"] == "USD")
    assert "78" not in usd["fees_total"]["text"]
