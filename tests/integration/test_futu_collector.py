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
        return {"US": [{"code": "US.NVDA", "qty": 15.0, "can_sell_qty": 15.0, "cost_price": 101.2345, "average_cost": 150.5, "diluted_cost": 101.2345}], "HK": []}[market]

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
    assert (rep.inserted, rep.pending) == (1, 3) and len(rep.notes) == 2 and len(open_pending(led)) == 3     # 认不出的方向/代码：进待匹配，不静默丢
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
    row = led.execute("SELECT cost_basis, average_cost, diluted_cost FROM snapshot_position").fetchone()
    assert (row["average_cost"], row["diluted_cost"]) == ("150.5", "101.2345")           # 平均成本与摊薄成本分开存（首跑核实：cost_price＝摊薄成本）
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


def test_dividend_and_withholding_tax_pair_into_one_dividend_group_and_unparseable_goes_pending(led):
    api = FakeApi()
    d = date(2026, 9, 10)
    api.flows[d] = [      # 合成测试值（格式取自真实首跑观察：备注含 (代码) dividend）
        {"cashflow_id": "C1", "clearing_date": "2026-09-10", "currency": "USD", "cashflow_type": "现金分红", "cashflow_amount": 41.86,
         "cashflow_remark": "SYNTH CORP COM(SYN) dividend, USD 0.91 per share"},
        {"cashflow_id": "C2", "clearing_date": "2026-09-10", "currency": "USD", "cashflow_type": "非美国居民预扣税", "cashflow_amount": -4.19,
         "cashflow_remark": "NRA withholding tax - SYNTH CORP COM(SYN) dividend, USD 0.91 per share"},
        {"cashflow_id": "C3", "clearing_date": "2026-09-10", "currency": "HKD", "cashflow_type": "现金分红", "cashflow_amount": 100, "cashflow_remark": "某某 派息"},
    ]
    tmap = {"现金分红": "DIVIDEND", "非美国居民预扣税": "DIVIDEND_WHT"}
    rep = collect_cash_flows(led, api, account_id=ACCT, acc_id=1, days=[d], type_map=tmap, **NOSLEEP)
    assert rep.rows == 3 and rep.pending == 1 and rep.inserted == 3          # 应收、支付、预扣税 3 个事件
    p = project(led, ACCT)
    assert p.cash == {"USD": D("37.67")} and not p.receivable                 # 现金＝总额−预扣税；应收结清
    again = collect_cash_flows(led, api, account_id=ACCT, acc_id=1, days=[d], type_map=tmap, **NOSLEEP)
    assert again.inserted == 0 and again.duplicate == 3                       # 幂等


def test_pending_item_is_auto_resolved_when_a_later_run_posts_it(led):
    api = FakeApi()
    api._deals["US"] = [deal(1, order="O1")]
    collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 3, 31), **NOSLEEP)
    api.fees = {"O1": [{"item": "佣金", "amount": 1.0, "currency": None}]}
    r1 = collect_order_fees(led, api, account_id=ACCT, acc_id=1, **NOSLEEP)                      # 币种未核实 → 进待匹配
    assert r1.pending == 1 and len(open_pending(led)) == 1
    r2 = collect_order_fees(led, api, account_id=ACCT, acc_id=1, assume_market_currency=True, **NOSLEEP)
    assert r2.inserted == 1 and open_pending(led) == []                                            # 入账后陈旧待匹配项自动结清


def test_dividend_code_formats_hk_two_payments_same_day_and_account_fees(led):
    from mystock2.collectors.futu import dividend_code

    assert dividend_code("SYNTH CORP COM(SYN) dividend, USD 0.91 per share", "USD") == "US.SYN"
    assert dividend_code("TSM 1.00000000 SHARES DIVIDENDS 0.608106 USD PER SHARE", "USD") == "US.TSM"
    assert dividend_code("24 F/D-HKD4.5/SH <SEHK 700 TENCENT> 11807 shares", "HKD") == "HK.00700"
    assert dividend_code("Handling Charge <SEHK 9988 X>", "USD") is None and dividend_code("乱七八糟", "HKD") is None          # 认不出就不猜
    api = FakeApi()
    d = date(2026, 5, 30)
    api.flows[d] = [      # 合成测试值
        {"cashflow_id": "H1", "clearing_date": "2026-05-30", "currency": "HKD", "cashflow_type": "现金股息", "cashflow_amount": 1000, "cashflow_remark": "F/D-HKD1/SH <SEHK 700 TENCENT> 1000 shares"},
        {"cashflow_id": "H2", "clearing_date": "2026-05-30", "currency": "HKD", "cashflow_type": "现金股息", "cashflow_amount": 500, "cashflow_remark": "S/D-HKD0.5/SH <SEHK 700 TENCENT> 1000 shares"},
        {"cashflow_id": "F1", "clearing_date": "2026-05-30", "currency": "HKD", "cashflow_type": "公司行动服务费", "cashflow_amount": -30, "cashflow_remark": "Handling Charge 1000 shares <SEHK 700 TENCENT>"},
        {"cashflow_id": "F2", "clearing_date": "2026-05-30", "currency": "HKD", "cashflow_type": "公司行动服务费", "cashflow_amount": -30, "cashflow_remark": "Handling Charge 1000 shares <SEHK 700 TENCENT>"},
    ]
    tmap = {"现金股息": "DIVIDEND", "公司行动服务费": "ACCOUNT_FEE"}
    rep = collect_cash_flows(led, api, account_id=ACCT, acc_id=1, days=[d], type_map=tmap, **NOSLEEP)
    assert rep.pending == 0 and not rep.conflicts and rep.inserted == 2 * 2 + 2     # 两笔股息各（应收+到账）＋两笔账户费用
    p = project(led, ACCT)
    assert p.cash == {"HKD": D("1440")} and not p.receivable                          # 1500−60；同日两笔股息各自成组，互不覆盖
    adj = led.execute("SELECT adjust_class FROM ledger_event WHERE event_type='ADJUST'").fetchall()
    assert {r["adjust_class"] for r in adj} == {"INVESTMENT"}                         # 计入业绩，不是外部流水
    assert collect_cash_flows(led, api, account_id=ACCT, acc_id=1, days=[d], type_map=tmap, **NOSLEEP).inserted == 0    # 幂等


def test_external_rule_uses_amount_sign_for_direction(led):
    api = FakeApi()
    d = date(2026, 4, 1)
    api.flows[d] = [      # 合成测试值
        {"cashflow_id": "E1", "clearing_date": "2026-04-01", "currency": "HKD", "cashflow_type": "其他", "cashflow_amount": 5000, "cashflow_remark": ""},
        {"cashflow_id": "E2", "clearing_date": "2026-04-01", "currency": "HKD", "cashflow_type": "其他", "cashflow_amount": -2000, "cashflow_remark": ""},
        {"cashflow_id": "E3", "clearing_date": "2026-04-01", "currency": "USD", "cashflow_type": "资产迁移", "cashflow_amount": 100, "cashflow_remark": "Account Upgrade"},
    ]
    rep = collect_cash_flows(led, api, account_id=ACCT, acc_id=1, days=[d], type_map={"其他": "EXTERNAL", "资产迁移": "DEPOSIT"}, **NOSLEEP)
    assert rep.inserted == 3 and rep.pending == 0
    kinds = {r["event_type"]: r["cash_delta"] for r in led.execute("SELECT event_type, cash_delta FROM ledger_event WHERE currency='HKD'")}
    assert kinds == {"DEPOSIT": "5000", "WITHDRAW": "-2000"}


def test_short_sell_buy_back_and_cancelled_deals_are_not_booked_silently(led):
    """审核 P1-4：SELL_SHORT/BUY_BACK 不按买卖猜，被券商取消（CANCELLED）的成交不能入账；都进待匹配，对账能看见。"""
    api = FakeApi()
    cancelled = deal(4)
    cancelled["status"] = "CANCELLED"
    api._deals["US"] = [deal(1, side="SELL_SHORT"), deal(2, side="BUY_BACK"), deal(3), cancelled]
    rep = collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 3, 31), **NOSLEEP)
    assert (rep.inserted, rep.pending) == (1, 3)
    assert [r["ref_deal_id"] for r in led.execute("SELECT ref_deal_id FROM ledger_event WHERE event_type='FILL'")] == ["D3"]
    reasons = sorted(r["reason"] for r in open_pending(led))
    assert any("SELL_SHORT" in x for x in reasons) and any("BUY_BACK" in x for x in reasons) and any("CANCELLED" in x for x in reasons)


def test_fee_titles_changing_language_do_not_double_book_and_negative_or_repeated_items_are_handled(led):
    """审核 P1-5：费用项标题是展示文本（OpenD 语言切换会变），金额构成与已入账一致时一律视为重复；
    标题变了且金额也变了 → 冲突不入账。负费用（返还）进待匹配；同一订单两条同名同额费用都保留。"""
    api = FakeApi()
    api._deals["US"] = [deal(1, order="O1"), deal(2, order="O2")]
    collect_deals(led, api, account_id=ACCT, acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 3, 31), **NOSLEEP)
    api.fees = {"O1": [{"item": "Commission", "amount": 0.99, "currency": "USD"}, {"item": "Platform Fee", "amount": 1.0, "currency": "USD"}],
                "O2": [{"item": "Settlement Fee", "amount": 0.3, "currency": "USD"}, {"item": "Settlement Fee", "amount": 0.3, "currency": "USD"},
                       {"item": "Rebate", "amount": -0.5, "currency": "USD"}]}
    first = collect_order_fees(led, api, account_id=ACCT, acc_id=1, **NOSLEEP)
    assert (first.inserted, first.pending) == (4, 1)
    total = lambda: sum(D(r["cash_delta"]) for r in led.execute("SELECT cash_delta FROM ledger_event WHERE event_type='FEE'"))  # noqa: E731
    assert total() == D("-2.59")
    api.fees["O1"] = [{"item": "佣金", "amount": 0.99, "currency": "USD"}, {"item": "平台使用费", "amount": 1.0, "currency": "USD"}]
    again = collect_order_fees(led, api, account_id=ACCT, acc_id=1, **NOSLEEP)
    assert again.inserted == 0 and total() == D("-2.59") and "fee_titles_changed:O1" in again.notes
    api.fees["O1"] = [{"item": "佣金", "amount": 1.99, "currency": "USD"}, {"item": "平台使用费", "amount": 1.0, "currency": "USD"}]
    changed = collect_order_fees(led, api, account_id=ACCT, acc_id=1, **NOSLEEP)
    assert changed.inserted == 0 and changed.conflicts and total() == D("-2.59")
