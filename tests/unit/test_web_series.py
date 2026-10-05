"""资产趋势的纯计算：与账本投影一致、T-07（入金当天不显示为收益）、缺口不插值、未复权价与拆股配对（T-40）。"""
from datetime import date, datetime, timezone
from decimal import Decimal as D

from mystock2.core import db as dbmod
from mystock2.ledger.events import EventDraft, correct_event, ensure_account, fill_key, flow_key, post_event, post_fx
from mystock2.ledger.opening import add_split, record_opening
from mystock2.ledger.projection import load_events, project
from mystock2.web.series import LedgerState, build_equity_series, replay_states

UTC = timezone.utc
A = "A1"
T0 = "2026-03-02T00:00:00.000000Z"


def dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fill(conn, deal, code, qty, px, at):
    ccy = "HKD" if code.startswith("HK.") else "USD"
    q = D(qty)
    return post_event(conn, EventDraft(fill_key(A, deal), A, "FILL", at, ccy, code=code, price=str(px), qty_delta=str(q),
                                       cash_delta=str(-q * D(px)), ref_deal_id=deal))


def deposit(conn, fid, amt, at, ccy="USD"):
    return post_event(conn, EventDraft(flow_key(A, fid, "DEPOSIT"), A, "DEPOSIT", at, ccy, cash_delta=str(amt)))


def new_db(tmp_path):
    p = tmp_path / "s.db"
    dbmod.migrate(p)
    c = dbmod.connect_writer(p, "ledger")
    ensure_account(c, A, "futu", "REAL", "USD")
    return c


def test_replay_matches_projection_at_every_cutoff(tmp_path):
    c = new_db(tmp_path)
    record_opening(c, A, T0, {"US.NVDA": "10", "HK.00700": "100"}, {"USD": "1000", "HKD": "5000"})
    fill(c, "b1", "US.NVDA", 5, 100, "2026-03-03T15:00:00.000000Z")
    fill(c, "s0", "US.NVDA", -2, 100, "2026-02-20T15:00:00.000000Z")           # 开账前：不参与
    add_split(c, "US.NVDA", "2026-03-04T00:00:00.000000Z", 2, 1)
    fill(c, "b2", "US.NVDA", 4, 55, "2026-03-04T00:00:00.000000Z")             # 与拆股同刻：已是拆股后数量
    deposit(c, "d1", 500, "2026-03-05T15:00:00.000000Z")
    post_fx(c, A, "g1", "2026-03-05T16:00:00.000000Z", "USD", "100", "HKD", "780")
    fill(c, "b3", "HK.00700", 50, 300, "2026-03-06T02:00:00.000000Z")
    correct_event(c, "fill:A1:b3", EventDraft("fill:A1:b3", A, "FILL", "2026-03-06T02:00:00.000000Z", "HKD", code="HK.00700", price="301",
                                              qty_delta="50", cash_delta="-15050", ref_deal_id="b3"), "req-1")
    post_event(c, EventDraft(flow_key(A, "w1", "WITHDRAW"), A, "WITHDRAW", "2026-03-07T10:00:00.000000Z", "USD", cash_delta="-200"))
    cuts = [dt(x) for x in ("2026-03-01T00:00:00.000000Z", "2026-03-02T00:00:00.000000Z", "2026-03-03T20:00:00.000000Z", "2026-03-04T00:00:00.000000Z", "2026-03-04T01:00:00.000000Z",
                           "2026-03-05T20:00:00.000000Z", "2026-03-06T08:00:00.000000Z", "2026-03-07T20:00:00.000000Z", "2026-03-20T00:00:00.000000Z")]
    rows = load_events(c, A)
    splits = [dict(r) for r in c.execute("SELECT code, effective_at, ratio_num, ratio_den FROM corporate_action")]
    got = replay_states(rows, splits, T0, cuts)
    for cut, st in zip(cuts, got, strict=True):
        p = project(c, A, as_of=cut)
        assert st.positions == p.positions, cut
        assert st.cash == p.cash, cut
        assert st.receivable == p.receivable, cut
        assert st.external_flow == p.external_flow, cut


def test_deposit_day_loss_is_not_shown_as_profit_T07():
    """T-07：大额入金当天标的下跌：账户权益上升，但「剔除外部资金流的收益」下降（入金不是收益）。"""
    days = [date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)]
    st = [
        LedgerState(positions={"US.NVDA": D(100)}, cash={"USD": D(1000)}),
        LedgerState(positions={"US.NVDA": D(100)}, cash={"USD": D(1000)}),
        LedgerState(positions={"US.NVDA": D(100)}, cash={"USD": D(1_001_000)}, external_flow={"USD": D(1_000_000)}),   # 入金 100 万
    ]
    prices = {"US.NVDA": {"2026-03-03": D(100), "2026-03-04": D(100), "2026-03-05": D(90)}}      # 入金当天下跌 10
    pts, base = build_equity_series("USD", days, st, {"US.NVDA": "USD"}, prices)
    assert base == "2026-03-03"
    eq = [p["equity"] for p in pts]
    assert eq == [D(11000), D(11000), D(1_010_000)]                  # 权益被入金推高
    assert pts[2]["profit"] == D(-1000)                              # 收益只反映下跌：100 股 × −10
    assert pts[2]["flow"] == D(1_000_000) and pts[0]["flow"] == 0
    assert pts[2]["profit"] < 0 < pts[2]["equity"] - pts[0]["equity"]


def test_withdraw_does_not_create_a_loss():
    days = [date(2026, 3, 3), date(2026, 3, 4)]
    st = [LedgerState(cash={"USD": D(1000)}), LedgerState(cash={"USD": D(400)}, external_flow={"USD": D(-600)})]
    pts, _ = build_equity_series("USD", days, st, {}, {})
    assert pts[1]["equity"] == D(400) and pts[1]["profit"] == 0


def test_fx_transfer_is_not_profit_in_single_currency_curve():
    days = [date(2026, 3, 3), date(2026, 3, 4)]
    st = [LedgerState(cash={"USD": D(1000), "HKD": D(0)}),
          LedgerState(cash={"USD": D(900), "HKD": D(780)}, fx_net={"USD": D(-100), "HKD": D(780)})]
    usd, _ = build_equity_series("USD", days, st, {}, {})
    hkd, _ = build_equity_series("HKD", days, st, {}, {})
    assert usd[1]["profit"] == 0 and hkd[1]["profit"] == 0         # 换汇是桶间转移，不是该币种的收益


def test_missing_close_is_gap_never_interpolated_or_zero():
    days = [date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5)]
    held = LedgerState(positions={"US.NVDA": D(10)}, cash={"USD": D(100)})
    pts, base = build_equity_series("USD", days, [held] * 3, {"US.NVDA": "USD"}, {"US.NVDA": {"2026-03-03": D(10), "2026-03-05": D(12)}})
    assert [p["status"] for p in pts] == ["ok", "gap", "ok"]
    assert pts[1]["missing"] == ["US.NVDA"]
    assert pts[1]["equity"] is None and pts[1]["market_value"] is None and pts[1]["profit"] is None   # 不是 0，也没有插值
    assert pts[2]["profit"] == D(20)


def test_no_positions_means_zero_market_value_not_a_gap():
    pts, base = build_equity_series("USD", [date(2026, 3, 3)], [LedgerState(cash={"USD": D(50)})], {}, {})
    assert pts[0]["status"] == "ok" and pts[0]["market_value"] == 0 and pts[0]["equity"] == 50 and base == "2026-03-03"


def test_all_gaps_means_no_base_and_no_profit():
    days = [date(2026, 3, 3)]
    pts, base = build_equity_series("USD", days, [LedgerState(positions={"US.NVDA": D(1)})], {"US.NVDA": "USD"}, {})
    assert base is None and pts[0]["profit"] is None


def test_split_pairs_unadjusted_price_with_projected_quantity_T40():
    """T-40：拆股后用未复权价 × 投影数量：10 股×100 = 20 股×50 = 1,000，权益不因拆股跳变。"""
    days = [date(2026, 3, 3), date(2026, 3, 4)]
    rows = [{"event_at": "2026-03-02T00:00:00.000000Z", "event_type": "OPENING_POSITION", "currency": "USD", "code": "US.NVDA", "qty_delta": "10",
             "cash_delta": "0", "recv_delta": "0", "adjust_class": None, "note": None}]
    splits = [{"code": "US.NVDA", "effective_at": "2026-03-03T22:00:00.000000Z", "ratio_num": 2, "ratio_den": 1}]
    states = replay_states(rows, splits, T0, [dt("2026-03-03T21:00:00.000000Z"), dt("2026-03-04T21:00:00.000000Z")])
    assert [s.positions["US.NVDA"] for s in states] == [D(10), D(20)]
    pts, _ = build_equity_series("USD", days, states, {"US.NVDA": "USD"}, {"US.NVDA": {"2026-03-03": D(100), "2026-03-04": D(50)}})
    assert pts[0]["market_value"] == pts[1]["market_value"] == D(1000)


def test_dividend_receivable_counts_in_equity_with_unadjusted_price():
    days = [date(2026, 3, 3)]
    pts, _ = build_equity_series("USD", days, [LedgerState(positions={"US.NVDA": D(10)}, cash={"USD": D(0)}, receivable={"USD": D(100)})],
                                 {"US.NVDA": "USD"}, {"US.NVDA": {"2026-03-03": D(100)}})
    assert pts[0]["equity"] == D(1100)
