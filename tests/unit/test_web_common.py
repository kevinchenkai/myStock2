"""公共约定：金额格式、红涨绿跌标记、不可用单元、新鲜度头部（LN-02）。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from mystock2.core.money import MoneyError
from mystock2.web import common as C

UTC = timezone.utc
NOW = datetime(2026, 3, 11, 6, 0, tzinfo=UTC)


def iso(dt):
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def test_money_text_always_carries_currency_and_groups_thousands():
    assert C.fmt_money(D("1234567.891"), "usd") == "1,234,567.89 USD"
    assert C.fmt_money("-80", "HKD") == "-80.00 HKD"
    assert C.fmt_money("0", "CNY") == "0.00 CNY"
    assert C.fmt_money("+5", "USD", sign=True) == "+5.00 USD"
    assert C.fmt_money("-0.001", "USD") == "0.00 USD"            # 舍入为零不显示负零


def test_rounding_is_bankers_not_float():
    assert C.fmt_decimal("0.125", 2) == "0.12"
    assert C.fmt_decimal("0.135", 2) == "0.14"
    assert C.fmt_decimal("2.675", 2) == "2.68"                     # float 会得到 2.67
    assert C.fmt_decimal("100000000000000000000.01", 2) == "100,000,000,000,000,000,000.01"


def test_price_and_qty_formats():
    assert C.fmt_price("106") == "106.00" and C.fmt_price("81.83181818") == "81.8318"
    assert C.fmt_qty("90.0000") == "90" and C.fmt_qty("33.33333") == "33.3333"


def test_red_up_green_down_direction_markers():
    assert C.money_cell("5", "USD", colored=True)["dir"] == "up"
    assert C.money_cell("-5", "USD", colored=True)["dir"] == "down"
    assert C.money_cell("0", "USD", colored=True)["dir"] == "flat"
    assert C.pct_cell("0.03", colored=True)["dir"] == "up" and C.pct_cell("-0.03", colored=True)["dir"] == "down"


def test_cash_and_cashflow_and_fx_are_never_colored():
    assert "dir" not in C.money_cell("5000", "USD")                # 现金/净现金流：不带涨跌方向
    r = C.rate_cell("7.8")
    assert "dir" not in r and r["fx"] is True                       # 汇率：中性色


def test_missing_values_are_unavailable_not_zero():
    for c in (C.money_cell(None, "USD", reason="缺行情"), C.price_cell(None), C.qty_cell(None), C.pct_cell(None), C.rate_cell(None)):
        assert c["na"] is True and c["text"] == "不可用" and c["v"] is None and "dir" not in c
    assert C.money_cell(None, "USD", reason="缺行情")["title"] == "缺行情"


def test_no_float_in_cells():
    c = C.money_cell(D("0.1") + D("0.2"), "USD")
    assert c["v"] == "0.3" and isinstance(c["v"], str)
    with pytest.raises(MoneyError):
        C.money_cell(0.1, "USD")                                    # float 一律拒绝


# ---- 新鲜度头部
def hdr(sources, hours=72, notes=()):
    return C.build_header("cached", C.freshness(sources, notes), NOW, hours)


def test_fresh_only_when_event_and_collected_times_known_and_recent():
    h = hdr([C.source("账本", iso(NOW - timedelta(hours=30)), iso(NOW - timedelta(hours=2)))])
    assert h["staleness"]["label"] == "新鲜" and h["data_mode_label"] == "定时缓存"
    assert h["event_at"] and h["collected_at"] and h["generated_at"] == iso(NOW)


@pytest.mark.parametrize("ev,col", [(None, "2026-03-11T05:00:00.000000Z"), ("2026-03-10T05:00:00.000000Z", None), (None, None), ("", "")])
def test_unknown_time_is_never_shown_as_fresh(ev, col):
    h = hdr([C.source("账本", ev, col)])
    assert h["staleness"]["label"] == "未知" and "新鲜" not in h["staleness"]["text"]
    assert h["sources"][0]["staleness"] == "未知"


def test_no_sources_is_unknown():
    h = C.build_header("daily", None, NOW, 72)
    assert h["staleness"]["label"] == "未知" and h["event_at"] is None and h["collected_at"] is None
    assert h["data_mode_label"] == "日线"


def test_stale_when_past_threshold_and_unknown_dominates():
    stale = C.source("行情", iso(NOW - timedelta(days=5)), iso(NOW - timedelta(days=4)))
    ok = C.source("账本", iso(NOW - timedelta(hours=3)), iso(NOW - timedelta(hours=1)))
    assert hdr([ok, stale])["staleness"]["label"] == "陈旧"
    unk = C.source("快照", None, None)
    assert hdr([ok, stale, unk])["staleness"]["label"] == "未知"     # 未知优先于陈旧：不确定就不说新鲜
    assert hdr([ok, unk])["staleness"]["label"] == "未知"


def test_recently_collected_but_behind_the_expected_session_is_stale():
    """审核 W-05/Q5：刚采集不等于新：事件时间落后于应有的最近收盘日时标陈旧；未知仍优先。"""
    h = hdr([C.source("行情", iso(NOW - timedelta(days=30)), iso(NOW - timedelta(minutes=5)), behind="行情停在 2026-02-09，应有 2026-03-10（US.NVDA）")])
    assert h["staleness"]["label"] == "陈旧" and "停在 2026-02-09" in h["staleness"]["text"]
    assert hdr([C.source("行情", None, iso(NOW), behind="x")])["staleness"]["label"] == "未知"
    assert hdr([C.source("行情", iso(NOW), iso(NOW))])["staleness"]["label"] == "新鲜"


def test_collected_in_the_future_is_clock_anomaly_not_fresh():
    h = hdr([C.source("账本", iso(NOW), iso(NOW + timedelta(days=1)))])
    assert h["staleness"]["label"] == "未知" and "时钟" in h["staleness"]["text"]


def test_header_uses_oldest_known_times_and_keeps_notes():
    a = C.source("a", iso(NOW - timedelta(hours=10)), iso(NOW - timedelta(hours=9)))
    b = C.source("b", iso(NOW - timedelta(hours=2)), iso(NOW - timedelta(hours=1)))
    h = hdr([a, b], notes=["说明"])
    assert h["event_at"] == iso(NOW - timedelta(hours=10)) and h["collected_at"] == iso(NOW - timedelta(hours=9)) and h["notes"] == ["说明"]


def test_unparseable_time_becomes_unknown():
    h = hdr([C.source("账本", "not-a-time", "2026-03-11")])        # 无时区/非法 → 未知
    assert h["staleness"]["label"] == "未知"


def test_no_cross_currency_sum_helper_exists():
    names = [n for n in dir(C) if "sum" in n.lower() or "total" in n.lower()]
    assert names == []
