from datetime import date, timezone
from decimal import Decimal

import pytest

from mystock2.core import calendars as cal
from mystock2.core import db as dbmod
from mystock2.ledger import opening
from mystock2.ledger.events import EventDraft, ensure_account, fee_key, fill_key, post_event
from mystock2.market.bars import DailyBar, put_daily
from mystock2.replay.behavior import MIN_SAMPLE, behavior_metrics
from mystock2.replay.cards import build_cards, fills_and_fees, render_card_text
from mystock2.replay.rounds import build_rounds

UTC = timezone.utc
D = Decimal
ACCT, CODE = "A1", "US.NVDA"
DAYS = cal.session_days("US", date(2026, 3, 2), date(2026, 4, 30))


@pytest.fixture()
def env(tmp_path):
    p = tmp_path / "r.db"
    dbmod.migrate(p)
    led, mkt = dbmod.connect_writer(p, "ledger"), dbmod.connect_writer(p, "market")
    ensure_account(led, ACCT, "futu", "REAL", "USD")
    opening.record_opening(led, ACCT, "2026-03-01T00:00:00.000000Z", {}, {"USD": "100000"})
    # 行情：价格从 100 起每天 +1；high=close+2, low=close−2；复权价=收盘价（无分红）
    bars = []
    for i, d in enumerate(DAYS):
        c = 100 + i
        bars.append(DailyBar(CODE, d, str(c), str(c + 2), str(c - 2), str(c), str(c), "1000"))
    put_daily(mkt, bars, source="s", quality="ok")
    return led, mkt


def fill(led, deal, day, qty, price, fee=None, hour=15):
    at = f"{day.isoformat()}T{hour}:00:00Z"
    notional = D(abs(qty)) * D(str(price))
    post_event(led, EventDraft(fill_key(ACCT, deal), ACCT, "FILL", at, "USD", code=CODE, price=str(price), qty_delta=str(qty),
                               cash_delta=str(-notional if qty > 0 else notional), ref_deal_id=deal))
    if fee is not None:
        post_event(led, EventDraft(fee_key(ACCT, deal, "c"), ACCT, "FEE", at, "USD", cash_delta=str(-D(str(fee))), ref_deal_id=deal))


# ---------------------------------------------------------------- 回合（FIFO，RP-03）
def test_fifo_rounds_with_fees_partial_and_open_lots():
    f = [
        {"code": CODE, "date": DAYS[0], "qty": D(100), "price": D(100), "deal_id": "B1", "opening": False, "currency": "USD"},
        {"code": CODE, "date": DAYS[2], "qty": D(50), "price": D(110), "deal_id": "B2", "opening": False, "currency": "USD"},
        {"code": CODE, "date": DAYS[5], "qty": D(-120), "price": D(120), "deal_id": "S1", "opening": False, "currency": "USD"},
    ]
    rounds, open_lots = build_rounds(f, {"B1": D(2), "B2": D(1), "S1": D(3)})
    assert [(r.qty, r.cost_unit, r.holding_days) for r in rounds] == [(D(100), D(100), 7), (D(20), D(110), 5)]
    # 回合1：(120−100)×100 − 费用(买 2 + 卖 3×100/120)=2000−(2+2.5)；回合2：(120−110)×20 − (1×20/50 + 3×20/120)
    assert rounds[0].pnl_after_fees == D(2000) - D(2) - D("2.5") and rounds[1].pnl_after_fees == D(200) - D("0.4") - D("0.5")
    assert open_lots[CODE][0]["qty"] == D(30)                                   # 未平仓部分不算胜负


def test_opening_inventory_has_unknown_cost_and_missing_fees_are_flagged():
    f = [{"code": CODE, "date": DAYS[0], "qty": D(10), "price": D(0), "deal_id": "O", "opening": True, "currency": "USD"},
         {"code": CODE, "date": DAYS[3], "qty": D(-10), "price": D(120), "deal_id": "S", "opening": False, "currency": "USD"},
         {"code": CODE, "date": DAYS[4], "qty": D(5), "price": D(100), "deal_id": "B", "opening": False, "currency": "USD"},
         {"code": CODE, "date": DAYS[6], "qty": D(-5), "price": D(110), "deal_id": "S2", "opening": False, "currency": "USD"}]
    rounds, _ = build_rounds(f, {"S": D(1)})
    assert rounds[0].pnl_after_fees is None and "cost_unknown" in rounds[0].flags            # 期初库存：不给精确盈亏
    assert rounds[1].pnl_after_fees == D(50) - 0 and "fees_missing" in rounds[1].flags        # 缺费用：费用前口径并标注
    sold_short, _ = build_rounds([{"code": CODE, "date": DAYS[0], "qty": D(-1), "price": D(1), "deal_id": "X", "opening": False, "currency": "USD"}], {})
    assert "sold_without_inventory" in sold_short[0].flags


# ---------------------------------------------------------------- 复盘卡（RP-01、RP-04）
def test_cards_use_ledger_facts_not_resimulation_and_mark_gaps(env):
    led, mkt = env
    fill(led, "B1", DAYS[3], 10, 102, fee=1.5)          # DAYS[3] 收盘 103，high 105 low 101 → 买在 (102−101)/4=0.25
    fill(led, "S1", DAYS[10], -10, 112)                 # 无费用记录；DAYS[10] 收盘 110，high 112 low 108 → 卖在 112=最高
    cards = build_cards(led, mkt, ACCT)
    b, s = cards
    assert (b.side, b.price, b.qty, b.fee, b.inventory_before, b.inventory_after) == ("BUY", D(102), D(10), D("1.5"), D(0), D(10))
    assert b.execution["range_position"] == "0.25"
    assert s.execution["range_position"] == "0"                                  # 卖出越接近最高越小：(1−1)=0
    assert (s.inventory_before, s.inventory_after) == (D(10), D(0)) and s.fee is None and "费用未记录（费用前口径）" in s.gaps
    assert "动机未记录" in b.gaps and "动机未记录" in s.gaps                       # 不补写动机
    # 成交后结果（复权口径）：买 102 → 1 日后收盘 104（+1.96%），5 日后 108，20 日后 123
    h = b.outcome["horizons"]
    assert D(h[1]).quantize(D("0.0001")) == D("0.0196") and D(h[5]).quantize(D("0.0001")) == D("0.0588") and D(h[20]).quantize(D("0.0001")) == D("0.2059")
    assert D(b.outcome["max_favorable"]) > 0 and D(b.outcome["max_adverse"]) < D("0.03")
    text = render_card_text(b)
    assert "【事实】" in text and "【诊断】" in text and "【缺口】动机未记录" in text and "【推测】" in text


def test_card_outcome_not_matured_is_none_not_zero_and_degenerate_range_skipped(env):
    led, mkt = env
    fill(led, "B9", DAYS[-2], 1, 150)                   # 倒数第二天：20 日后尚未到期
    (c,) = build_cards(led, mkt, ACCT)
    assert c.outcome["horizons"][20] is None and c.outcome["horizons"][1] is not None
    put_daily(mkt, [DailyBar(CODE, date(2026, 5, 4), "100", "100", "100", "100", "100", "1")], source="flat", quality="ok")
    fill(led, "B10", date(2026, 5, 4), 1, 100)
    flat = [x for x in build_cards(led, mkt, ACCT) if x.deal_id == "B10"][0]
    assert flat.execution["range_position"] is None                                # 区间退化：不计算


def test_cards_pick_up_prior_intents_and_pre_existing_tickets(env):
    led, mkt = env
    d = DAYS[3]
    fill(led, "B1", d, 10, 102, hour=16)
    led.close()
    p = [x for x in __import__("pathlib").Path(env[1].execute("PRAGMA database_list").fetchone()["file"]).parent.glob("r.db")][0]
    cw = dbmod.connect_writer(p, "coach")
    cw.execute("INSERT INTO intent(intent_id,batch_id,line_id,market,code,target_session,action,limit_price,qty,state_hash,recorded_at,seen_ai,late_record,frozen_hash) "
               "VALUES ('i1','B','B:h','US',?,?,'BUY','102','10','s',?,0,0,'h1')", (CODE, d.isoformat(), f"{d.isoformat()}T12:00:00Z"))
    cw.execute("INSERT INTO ticket(ticket_id,batch_id,line_id,kind,market,code,target_session,stage,status,action,limit_price,qty,reason_json,invalidate_json,uncertainty_json,"
               "strategy_version,protocol_version,state_ref_type,state_ref,generated_at,frozen_at,visible_at,deadline_at,frozen_hash) VALUES "
               "('t1','B','B:ai','line_sim','US',?,?,'close','frozen','BUY','101','10','[]','[]','{}','v','p','line_state','s','x','x',?, 'x','h2')",
               (CODE, d.isoformat(), f"{d.isoformat()}T10:00:00Z"))
    cw.execute("INSERT INTO ticket(ticket_id,batch_id,line_id,kind,market,code,target_session,stage,status,action,limit_price,qty,reason_json,invalidate_json,uncertainty_json,"
               "strategy_version,protocol_version,state_ref_type,state_ref,generated_at,frozen_at,visible_at,deadline_at,frozen_hash) VALUES "
               "('t2','B','B:human_plan','line_sim','US',?,?,'human_plan','frozen','BUY','102','10','[]','[]','{}','human','p','line_state','s','x','x',?, 'x','h3')",
               (CODE, d.isoformat(), f"{d.isoformat()}T10:30:00Z"))                 # 人类计划线冻结的单：不是 AI 单（审核 P3）
    led2 = dbmod.connect_ro(p)
    (c,) = build_cards(led2, dbmod.connect_ro(p), ACCT)
    assert len(c.evidence["intents"]) == 1 and len(c.evidence["tickets_existing"]) == 1 and "动机未记录" not in c.gaps


def test_pre_opening_history_is_excluded_from_cards_and_rounds(env):
    led, mkt = env
    fill(led, "OLD", date(2026, 2, 20), 100, 90)        # 开账日（2026-03-01）之前的历史成交：只作描述
    fill(led, "NEW", DAYS[3], 1, 102)
    fills, _ = fills_and_fees(led, ACCT)
    assert [f["deal_id"] for f in fills if not f["opening"]] == ["NEW"]


# ---------------------------------------------------------------- 行为指标（RP-02）
def test_behavior_metrics_report_insufficient_not_zero(env):
    led, mkt = env
    fill(led, "B1", DAYS[3], 10, 102)
    cards = build_cards(led, mkt, ACCT)
    ms = {m.name: m for m in behavior_metrics(cards, [])}
    m = ms["买入执行质量（区间位置均值，越小越好）"]
    assert m.value is None and m.display == "不足" and m.n == 1 and "不足" in m.definition      # 空/小样本显示「不足」
    assert ms["已平仓回合胜率（描述，不评判）"].display == "不足" and ms["已平仓回合胜率（描述，不评判）"].n == 0


def test_behavior_metrics_with_enough_samples(env):
    led, mkt = env
    # 6 次买入（价格逐次抬高：区间位置递增）+ 6 次卖出，形成 6 个回合
    for i in range(MIN_SAMPLE + 1):
        fill(led, f"B{i}", DAYS[2 + 3 * i], 10, 100 + 2 + 3 * i + (i % 3) - 1)
        fill(led, f"S{i}", DAYS[3 + 3 * i], -10, 100 + 3 + 3 * i + 1 + (1 if i % 2 else -3))
    cards = build_cards(led, mkt, ACCT)
    fills, fees = fills_and_fees(led, ACCT)
    rounds, _ = build_rounds(fills, fees)
    ms = {m.name: m for m in behavior_metrics(cards, rounds)}
    buy_q = ms["买入执行质量（区间位置均值，越小越好）"]
    assert buy_q.value is not None and buy_q.n == MIN_SAMPLE + 1 and 0 <= Decimal(buy_q.value) <= 1
    assert Decimal(ms["已平仓回合胜率（描述，不评判）"].value) <= 1 and ms["平均持有天数（已平仓回合）"].n == len(rounds)
    sold_up = ms["卖出后 5 个交易日继续上涨的比例"]
    assert sold_up.value is not None and Decimal(sold_up.value) == 1               # 价格单调上涨：卖出后 5 日都更高（描述，不评判）
