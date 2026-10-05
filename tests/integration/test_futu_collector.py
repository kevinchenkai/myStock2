"""富途采集器的映射与流程（假 API）。真实 OpenD 行为未验证——首跑须在负责人授权下核对。"""
from datetime import date
from decimal import Decimal

import pytest

from mystock2.collectors.futu import (
    FEE_BATCH,
    collect_cash_flows,
    collect_deals,
    collect_order_fees,
    collect_snapshot,
)
from mystock2.core import db as dbmod
from mystock2.ledger import opening
from mystock2.ledger.events import ensure_account, open_pending
from mystock2.ledger.projection import project
from mystock2.ledger.reconcile import reconcile

D = Decimal
ACCT = "FUTU-1"


class FakeApi:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self._deals = {"US": [], "HK": []}
        self.flows = {}
        self.fees = {}

    def deals(self, acc_id, market, start, end):
        self.calls.append(("deals", market, start, end))
        if (market, start) in self.fail:
            raise ConnectionError("OpenD 断线")
        return [d for d in self._deals[market] if str(start) <= d["create_time"][:10] <= str(end)]

    def positions(self, acc_id, market):
        return {"US": [{"code": "US.NVDA", "qty": 15.0, "can_sell_qty": 15.0, "cost_price": 101.2345}], "HK": []}[market]

    def funds(self, acc_id):
        return {"USD": {"cash": 899.0}, "HKD": {"cash": 0.0}}

    def order_fees(self, acc_id, order_ids):
        self.calls.append(("fees", len(order_ids)))
        return {o: self.fees.get(o, []) for o in order_ids}

    def cash_flow(self, acc_id, d):
        self.calls.append(("flow", d))
        return self.flows.get(d, [])


def deal(i, code="US.NVDA", side="BUY", price=100.0, qty=10, t="2026-03-03 10:30:00", order=None):
    return {"deal_id": f"D{i}", "order_id": order or f"O{i}", "code": code, "side": side, "price": price, "qty": qty, "create_time": t}


@pytest.fixture()
def led(tmp_path):
    p = tmp_path / "f.db"
    dbmod.migrate(p)
    return dbmod.connect_writer(p, "ledger")


NOSLEEP = {"sleep": lambda s: None}


def test_deals_windows_idempotency_and_mapping(led):
    api = FakeApi()
    api._deals["US"] = [deal(1), deal(2, side="SELL", qty=4, price=105.5, t="2026-05-20 11:00:00")]
    api._deals["HK"] = [deal(3, code="HK.00700", price=428.2, qty=100, t="2026-03-03 10:00:00")]
    rep = collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US", "HK"], start=date(2026, 3, 1), end=date(2026, 6, 30), **NOSLEEP)
    assert rep.ok and (rep.rows, rep.inserted) == (3, 3)
    wins = [c for c in api.calls if c[0] == "deals" and c[1] == "US"]
    assert [(w[2], w[3]) for w in wins] == [(date(2026, 3, 1), date(2026, 5, 19)), (date(2026, 5, 20), date(2026, 6, 30))]       # 80 天窗口
    assert collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US", "HK"], start=date(2026, 3, 1), end=date(2026, 6, 30), **NOSLEEP).duplicate == 3
    ev = {r["ref_deal_id"]: r for r in led.execute("SELECT * FROM ledger_event WHERE event_type='FILL'")}
    assert ev["D1"]["event_at"] == "2026-03-03T15:30:00.000000Z" and ev["D1"]["cash_delta"] == "-1000" and ev["D2"]["qty_delta"] == "-4" and ev["D2"]["cash_delta"] == "422"
    assert ev["D3"]["currency"] == "HKD" and ev["D3"]["event_at"] == "2026-03-03T02:00:00.000000Z"


def test_window_failure_is_partial_with_retry_scope_not_silent(led):
    api = FakeApi()
    api._deals["US"] = [deal(1), deal(2, t="2026-05-25 10:00:00")]
    api.fail.add(("US", date(2026, 3, 1)))
    rep = collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 6, 30), **NOSLEEP)
    assert not rep.ok and rep.inserted == 1 and "US 2026-03-01~2026-05-19" in rep.failed_scopes[0]       # 失败窗口可重试；成功窗口保留
    assert led.execute("SELECT COUNT(*) c FROM ledger_event").fetchone()["c"] == 1


def test_bad_rows_missing_deal_id_and_conflicts(led):
    api = FakeApi()
    bad = deal(9)
    del bad["deal_id"]
    api._deals["US"] = [deal(1), bad, deal(2, side="HOLD"), {"deal_id": "D3", "code": "XX.1", "side": "BUY", "price": 1, "qty": 1, "create_time": "2026-03-03 10:00:00"}]
    rep = collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 3, 31), **NOSLEEP)
    assert (rep.inserted, rep.pending) == (1, 1) and len(rep.notes) == 2 and len(open_pending(led)) == 1
    api._deals["US"] = [deal(1, price=101.0)]                                                              # 同 deal_id 不同价：真冲突
    assert collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 3, 31), **NOSLEEP).conflicts


def test_snapshot_then_reconcile_fees_close_the_cash_gap(led):
    api = FakeApi()
    ensure_account(led, ACCT, "futu", "REAL")
    opening.record_opening(led, ACCT, "2026-03-01T00:00:00.000000Z", {"US.NVDA": "5"}, {"USD": "1900"})
    api._deals["US"] = [deal(1, price=100.0, qty=10)]
    collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 3, 31), **NOSLEEP)
    rep = collect_snapshot(led, api, account_id=ACCT, acc_id=1, markets=["US", "HK"], captured_at="2026-03-04T21:00:00.000000Z", **NOSLEEP)
    assert rep.ok and led.execute("SELECT cost_basis FROM snapshot_position").fetchone()["cost_basis"] == "101.2345"
    sid = led.execute("SELECT snapshot_id FROM account_snapshot").fetchone()["snapshot_id"]
    r = reconcile(led, ACCT, sid)
    assert project(led, ACCT).positions == {"US.NVDA": D(15)} and not r.position_diffs                      # 持仓逐标的一致
    assert not r.ok and [d["currency"] for d in r.cash_diffs] == ["USD"] and r.cash_diffs[0]["diff"] == "1"   # 现金差 1：未对账项如实列出（缺费用）
    api.fees = {"O1": [{"item": "Commission", "amount": 1.0, "currency": "USD"}]}
    collect_order_fees(led, api, account_id=ACCT, acc_id=1, **NOSLEEP)
    assert reconcile(led, ACCT, sid).ok                                                                       # 补上订单费用后对齐


def test_snapshot_failure_writes_nothing(led):
    class Broken(FakeApi):
        def funds(self, acc_id):
            raise TimeoutError("accinfo")
    rep = collect_snapshot(led, Broken(), account_id=ACCT, acc_id=1, markets=["US"], captured_at="2026-03-04T21:00:00.000000Z", **NOSLEEP)
    assert not rep.ok and led.execute("SELECT COUNT(*) c FROM account_snapshot").fetchone()["c"] == 0           # 缺失显式，不记零


def test_order_fees_have_stable_order_item_identity_and_unknown_currency_goes_pending(led):
    api = FakeApi()
    api._deals["US"] = [deal(1, order="O1", t="2026-03-03 10:30:00"), deal(3, order="O2")]
    collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 3, 31), **NOSLEEP)
    api.fees = {"O1": [{"item": "Commission", "amount": 1.0, "currency": "USD"}, {"item": "Platform Fee", "amount": 0.99, "currency": "USD"}],
                "O2": [{"item": "Commission", "amount": 0, "currency": "USD"}, {"item": "SEC Fee", "amount": 0.01, "currency": None}]}
    rep = collect_order_fees(led, api, account_id=ACCT, acc_id=1, **NOSLEEP)
    assert (rep.inserted, rep.pending) == (2, 1)                                                              # 金额为 0 跳过；缺币种进待匹配，不猜 USD
    keys = sorted(r["business_key"] for r in led.execute("SELECT business_key FROM ledger_event WHERE event_type='FEE'"))
    assert keys == [f"fee:{ACCT}:order:O1:commission", f"fee:{ACCT}:order:O1:platform_fee"]
    # 同订单又来了第二笔成交：重复采集不会因「最后一笔成交」变了而重复入账
    api._deals["US"].append(deal(2, order="O1", t="2026-03-03 10:31:00"))
    collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 3, 31), **NOSLEEP)
    again = collect_order_fees(led, api, account_id=ACCT, acc_id=1, **NOSLEEP)
    assert again.inserted == 0 and led.execute("SELECT COUNT(*) c FROM ledger_event WHERE event_type='FEE'").fetchone()["c"] == 2
    api.fees["O1"][0]["amount"] = 2.0                                                                         # 订单费变了：冲突并报告，不静默覆盖也不重复入账
    changed = collect_order_fees(led, api, account_id=ACCT, acc_id=1, **NOSLEEP)
    assert changed.inserted == 0 and changed.conflicts and "请人工更正" in changed.conflicts[0]
    assert led.execute("SELECT cash_delta FROM ledger_event WHERE business_key=?", (f"fee:{ACCT}:order:O1:commission",)).fetchone()["cash_delta"] == "-1"
    # 显式确认「按成交市场币种」后，缺币种的费用才入账
    ok = collect_order_fees(led, api, account_id=ACCT, acc_id=1, assume_market_currency=True, **NOSLEEP)
    assert ok.inserted == 1 and FEE_BATCH == 400


def test_cash_flows_require_explicit_mapping_unknown_goes_pending_and_trade_flows_are_recon_only(led):
    api = FakeApi()
    d = date(2026, 3, 3)
    api.flows[d] = [
        {"cashflow_id": "C1", "clearing_date": "2026-03-03", "currency": "USD", "cashflow_type": "DEPOSIT_X", "cashflow_amount": 5000},
        {"cashflow_id": "C2", "clearing_date": "2026-03-03", "currency": "USD", "cashflow_type": "TRADE_X", "cashflow_amount": -1000},        # 买入：已由成交记账
        {"cashflow_id": "C3", "clearing_date": "2026-03-03", "currency": "USD", "cashflow_type": "MYSTERY", "cashflow_amount": 7},
        {"cashflow_id": "C4", "clearing_date": "2026-03-03", "currency": "USD", "cashflow_type": "WITHDRAW_X", "cashflow_amount": -200},
        {"cashflow_id": "C5", "clearing_date": "2026-03-03", "currency": "USD", "cashflow_type": "DEPOSIT_X", "cashflow_amount": -1},          # 方向与映射不符
    ]
    tmap = {"DEPOSIT_X": "DEPOSIT", "WITHDRAW_X": "WITHDRAW", "TRADE_X": "RECON_ONLY"}
    rep = collect_cash_flows(led, api, account_id=ACCT, acc_id=1, days=[d], type_map=tmap, **NOSLEEP)
    assert (rep.rows, rep.inserted, rep.pending) == (5, 2, 2) and rep.recon_only == {"USD:TRADE_X": D(-1000)}
    kinds = {r["event_type"]: r["cash_delta"] for r in led.execute("SELECT event_type, cash_delta FROM ledger_event")}
    assert kinds == {"DEPOSIT": "5000", "WITHDRAW": "-200"}                                                    # 成交类流水不入账：现金只扣一次（T-20）
    assert {r["reason"] for r in open_pending(led)} == {"未映射的资金流水类型：MYSTERY", "DEPOSIT_X 的金额方向与映射 DEPOSIT 不符"}
    assert collect_cash_flows(led, api, account_id=ACCT, acc_id=1, days=[d], type_map=tmap, **NOSLEEP).duplicate == 2
    with pytest.raises(ValueError, match="非法入账方式"):
        collect_cash_flows(led, api, account_id=ACCT, acc_id=1, days=[d], type_map={"MYSTERY": "ADJUST"}, **NOSLEEP)
    # 资金流水接口失败：记 partial 与可重试日期
    bad = FakeApi()
    bad.cash_flow = lambda acc, dd: (_ for _ in ()).throw(ConnectionError("x"))
    r2 = collect_cash_flows(led, bad, account_id=ACCT, acc_id=1, days=[d], type_map=tmap, **NOSLEEP)
    assert not r2.ok and "cash_flow 2026-03-03" in r2.failed_scopes[0]
