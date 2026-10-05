"""账本性质测试（hypothesis）：幂等、插入顺序无关、重建等于独立的朴素计算。"""
import random
from decimal import Decimal

import pytest

pytest.importorskip("hypothesis")        # 共用环境 mk 未装 hypothesis 时跳过（mk2/dev 环境照常运行）
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from mystock2.ledger import opening
from mystock2.ledger.events import EventDraft, post_event
from mystock2.ledger.projection import project
from tests.unit.ledger_helpers import ACCT, T0, make_db

CODES = ["US.NVDA", "US.TSLA", "HK.00700"]

fill_st = st.tuples(st.sampled_from(CODES), st.integers(1, 500), st.integers(1, 9999), st.booleans(), st.integers(1, 20))


def draft(i, code, qty, px_cents, is_buy, day):
    px = Decimal(px_cents) / 100
    q = Decimal(qty) if is_buy else -Decimal(qty)
    ccy = "HKD" if code.startswith("HK.") else "USD"
    return EventDraft(f"fill:{ACCT}:P{i}", ACCT, "FILL", f"2026-03-{day + 2:02d}T15:00:00Z", ccy, code=code, price=str(px),
                      qty_delta=str(q), cash_delta=str(-q * px), ref_deal_id=f"P{i}")


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(fill_st, min_size=1, max_size=25), st.integers(0, 10_000))
def test_projection_matches_naive_and_ignores_insertion_order_and_replays(tmp_path_factory, fills, seed):
    drafts = [draft(i, *f) for i, f in enumerate(fills)]
    naive_pos: dict[str, Decimal] = {}
    naive_cash: dict[str, Decimal] = {}
    for d in drafts:
        naive_pos[d.code] = naive_pos.get(d.code, Decimal(0)) + Decimal(d.qty_delta)
        naive_cash[d.currency] = naive_cash.get(d.currency, Decimal(0)) + Decimal(d.cash_delta)
    naive_pos = {k: v for k, v in naive_pos.items() if v != 0}
    naive_cash = {k: v for k, v in naive_cash.items() if v != 0}

    results = []
    for order_seed in (None, seed):
        conn = make_db(tmp_path_factory.mktemp("p"))
        opening.record_opening(conn, ACCT, T0, {}, {})
        seq = list(drafts)
        if order_seed is not None:
            random.Random(order_seed).shuffle(seq)
        for d in seq:
            post_event(conn, d)
        for d in seq:                                       # 整批重放：幂等，不改变任何结果
            assert post_event(conn, d).status == "duplicate"
        p = project(conn, ACCT)
        results.append((p.positions, p.cash))
        conn.close()
    assert results[0] == results[1]
    assert results[0][0] == naive_pos and results[0][1] == naive_cash
