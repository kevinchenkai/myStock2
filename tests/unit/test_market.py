import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mystock2.collectors.quotes import collect_daily, collect_fx, collect_hourly
from mystock2.core import calendars as cal
from mystock2.market.bars import (
    BarError,
    DailyBar,
    HourlyBar,
    get_daily,
    hourly_archive_gaps,
    missing_sessions,
    put_daily,
    put_hourly,
)
from mystock2.market.evidence import EvidenceError, snapshot, snapshot_daily_bars, verify_inputs
from mystock2.market.fx import FxUnavailable, convert, get_rate, put_rate
from tests.unit.market_helpers import after_close, synth_bars, writers

UTC = timezone.utc
CODE = "US.NVDA"


@pytest.fixture()
def mk(tmp_path):
    m, f, i, p = writers(tmp_path)
    return m


def bar(d="2026-03-03", o="10", h="11", lo="9", c="10.5", adj="10.5"):
    return DailyBar(CODE, date.fromisoformat(d), o, h, lo, c, adj, "100")


# ------------------------------------------------------------ 日线存储
def test_put_daily_idempotent_and_versioned(mk):
    assert put_daily(mk, [bar()], source="s1") == {"inserted": 1, "new_version": 0, "duplicate": 0}
    assert put_daily(mk, [bar()], source="s1") == {"inserted": 0, "new_version": 0, "duplicate": 1}
    assert put_daily(mk, [bar(c="10.6", h="11.5")], source="s1")["new_version"] == 1      # 修订追加新版本
    rows = mk.execute("SELECT version FROM quote_daily ORDER BY version").fetchall()
    assert [r["version"] for r in rows] == [1, 2]                                         # 旧版本保留
    got = get_daily(mk, CODE, date(2026, 3, 1), date(2026, 3, 31))
    assert len(got) == 1 and got[0]["close"] == "10.6"
    with pytest.raises(sqlite3.DatabaseError, match="只追加"):
        mk.execute("UPDATE quote_daily SET close='1'")


def test_point_in_time_read_ignores_later_revisions(mk):
    t1, t2 = datetime(2026, 3, 3, 22, 0, tzinfo=UTC), datetime(2026, 3, 4, 9, 0, tzinfo=UTC)
    put_daily(mk, [bar()], source="s1", received_at=t1)
    put_daily(mk, [bar(c="10.9", h="11.5")], source="s1", received_at=t2)
    old = get_daily(mk, CODE, date(2026, 3, 3), date(2026, 3, 3), received_by=t1 + timedelta(minutes=1))
    assert old[0]["close"] == "10.5"                      # 当时可得的版本
    assert get_daily(mk, CODE, date(2026, 3, 3), date(2026, 3, 3))[0]["close"] == "10.9"


def test_invalid_bars_rejected(mk):
    with pytest.raises(BarError):
        put_daily(mk, [bar(h="9.5")], source="s1")                         # high < open/close
    with pytest.raises(BarError):
        put_daily(mk, [bar(o="0")], source="s1")
    with pytest.raises(BarError, match="非交易日"):
        put_daily(mk, [bar(d="2025-07-04")], source="s1")                  # 美股休市日
    assert mk.execute("SELECT COUNT(*) c FROM quote_daily").fetchone()["c"] == 0   # 整批回滚


def test_missing_sessions_listed_not_filled(mk):
    put_daily(mk, [bar("2026-03-02"), bar("2026-03-04")], source="s1")
    assert missing_sessions(mk, CODE, date(2026, 3, 2), date(2026, 3, 6)) == [date(2026, 3, 3), date(2026, 3, 5), date(2026, 3, 6)]


# ------------------------------------------------------------ 采集（T-10）
class FakeSource:
    def __init__(self, name, bars=None, exc=None):
        self.name, self._bars, self._exc = name, bars or [], exc

    def daily(self, code, start, end):
        if self._exc:
            raise self._exc
        return self._bars

    def hourly(self, code, start, end):
        if self._exc:
            raise self._exc
        return self._bars

    def fx_daily(self, pair, start, end):
        if self._exc:
            raise self._exc
        return self._bars


def logs(mk):
    return [dict(r) for r in mk.execute("SELECT source, kind, status, rows FROM collection_log ORDER BY rowid")]


def test_t10_primary_error_falls_back_to_backup_and_failures_are_kept(mk):
    now = after_close(date(2026, 3, 4))
    res = collect_daily(mk, [FakeSource("primary", exc=ConnectionError("boom")), FakeSource("backup", [bar("2026-03-03")])],
                        CODE, date(2026, 3, 2), date(2026, 3, 4), run_id="r1", now=now)
    assert res["status"] == "ok" and res["source"] == "backup" and res["attempts"] == [("primary", "error"), ("backup", "ok")]
    assert [(x["source"], x["status"]) for x in logs(mk)] == [("primary", "error"), ("backup", "ok")]
    assert get_daily(mk, CODE, date(2026, 3, 3), date(2026, 3, 3), source="backup")


def test_t10_empty_and_all_sources_failed_write_nothing_and_never_zero_fill(mk):
    res = collect_daily(mk, [FakeSource("a", []), FakeSource("b", exc=TimeoutError())], CODE, date(2026, 3, 2), date(2026, 3, 4), now=after_close(date(2026, 3, 4)))
    assert res["status"] == "failed" and res["rows"] == 0
    assert mk.execute("SELECT COUNT(*) c FROM quote_daily").fetchone()["c"] == 0
    assert [x["status"] for x in logs(mk)] == ["empty", "error"]


def test_current_session_bar_before_final_buffer_is_partial_not_ok(mk):
    d = date(2026, 3, 4)
    close = cal.session("US", d).close_utc
    res = collect_daily(mk, [FakeSource("a", [bar("2026-03-03"), bar("2026-03-04")])], CODE, date(2026, 3, 3), d, now=close + timedelta(minutes=5))
    assert res["status"] == "partial"
    q = {r["session_date"]: r["quality"] for r in mk.execute("SELECT session_date, quality FROM quote_daily")}
    assert q == {"2026-03-03": "ok", "2026-03-04": "partial"}


def test_non_session_rows_from_vendor_are_dropped(mk):
    res = collect_daily(mk, [FakeSource("a", [bar("2026-03-07"), bar("2026-03-03")])], CODE, date(2026, 3, 2), date(2026, 3, 8), now=after_close(date(2026, 3, 9)))
    assert res["rows"] == 1                                                  # 周六被丢弃
    assert [r["session_date"] for r in mk.execute("SELECT session_date FROM quote_daily")] == ["2026-03-03"]


def test_hourly_and_fx_collection_with_fallback(mk):
    hb = HourlyBar(CODE, datetime(2026, 3, 3, 14, 30, tzinfo=UTC), datetime(2026, 3, 3, 15, 30, tzinfo=UTC), "10", "11", "9", "10", "5", True)
    assert collect_hourly(mk, [FakeSource("x", exc=OSError()), FakeSource("y", [hb])], CODE, date(2026, 3, 3), date(2026, 3, 3))["source"] == "y"
    r = collect_fx(mk, [FakeSource("fx1", [(date(2026, 3, 3), "7.8")])], "USDHKD", date(2026, 3, 3), date(2026, 3, 3))
    assert r["status"] == "ok" and get_rate(mk, "USDHKD", date(2026, 3, 3))[0] == Decimal("7.8")


def test_hourly_archive_gaps(mk):
    s = cal.session("US", date(2026, 3, 3))
    put_hourly(mk, [HourlyBar(CODE, s.open_utc, s.open_utc + timedelta(hours=1), "10", "11", "9", "10", "5", True)], source="x")
    assert hourly_archive_gaps(mk, CODE, date(2026, 3, 3), date(2026, 3, 4)) == [{"session": "2026-03-04", "problem": "no_bars"}]
    put_hourly(mk, [HourlyBar(CODE, s.close_utc - timedelta(minutes=30), s.close_utc + timedelta(minutes=30), "10", "11", "9", "10", "5", True)], source="x")
    assert {"session": "2026-03-03", "problem": "bar_after_close"} in [{k: v for k, v in p.items() if k != "bar_start"} for p in hourly_archive_gaps(mk, CODE, date(2026, 3, 3), date(2026, 3, 3))]


# ------------------------------------------------------------ 汇率
def test_fx_direct_inverse_stale_and_unavailable(mk):
    put_rate(mk, "USDHKD", date(2026, 3, 3), "7.8", source="s", event_at=datetime(2026, 3, 3, 21, tzinfo=UTC))
    rate, info = get_rate(mk, "USDHKD", date(2026, 3, 3))
    assert str(rate) == "7.8" and info["source"] == "s" and not info["inverse"]
    inv, info = get_rate(mk, "HKDUSD", date(2026, 3, 3))
    assert info["inverse"] and abs(inv * Decimal("7.8") - 1) < Decimal("1e-20")
    with pytest.raises(FxUnavailable):
        get_rate(mk, "USDHKD", date(2026, 3, 4))                         # 默认必须当日，不拿昨日冒充
    assert get_rate(mk, "USDHKD", date(2026, 3, 4), max_stale_days=1)[1]["stale_days"] == 1
    with pytest.raises(FxUnavailable):
        get_rate(mk, "USDCNY", date(2026, 3, 3))                         # 缺汇率：不可用
    amt, _ = convert(mk, "100", "USD", "HKD", date(2026, 3, 3))
    assert str(amt) == "780.0"
    assert convert(mk, "5", "USD", "USD", date(2026, 3, 3))[0] == 5
    assert put_rate(mk, "USDHKD", date(2026, 3, 3), "7.8", source="s", event_at=datetime(2026, 3, 3, tzinfo=UTC)) == "duplicate"
    with pytest.raises(ValueError):
        put_rate(mk, "USDUSD", date(2026, 3, 3), "1", source="s", event_at=datetime(2026, 3, 3, tzinfo=UTC))


# ------------------------------------------------------------ 证据快照（T-24/T-38）
def test_snapshot_idempotent_immutable_and_trust(mk):
    s1 = snapshot(mk, "note", "US.NVDA", {"a": 1}, received_at=datetime(2026, 3, 3, tzinfo=UTC))
    assert snapshot(mk, "note", "US.NVDA", {"a": 1}) == s1
    assert snapshot(mk, "note", "US.NVDA", {"a": 2}) != s1
    with pytest.raises(sqlite3.DatabaseError, match="不可变"):
        mk.execute("DELETE FROM evidence_snapshot")
    with pytest.raises(EvidenceError):
        snapshot(mk, "note", "x", {}, time_trust="made_up")


def test_t24_revision_creates_new_snapshot_old_one_still_verifiable(mk):
    t1, t2 = datetime(2026, 3, 3, 22, tzinfo=UTC), datetime(2026, 3, 4, 10, tzinfo=UTC)
    put_daily(mk, [bar()], source="s1", received_at=t1)
    s_old = snapshot_daily_bars(mk, CODE, date(2026, 3, 3), date(2026, 3, 3), received_by=t1 + timedelta(hours=1))
    put_daily(mk, [bar(c="10.9", h="11.5")], source="s1", received_at=t2)
    s_new = snapshot_daily_bars(mk, CODE, date(2026, 3, 3), date(2026, 3, 3))
    assert s_old != s_new
    # 截止在 t1 之后、t2 之前：旧快照合规，新快照（晚于截止才收到）违规
    cutoff = t1 + timedelta(hours=2)
    assert verify_inputs(mk, [s_old], cutoff) == []
    assert verify_inputs(mk, [s_new], cutoff) == [f"received_after_input_cutoff:{s_new}"]
    assert verify_inputs(mk, ["nope"], cutoff) == ["missing_snapshot:nope"]
    with pytest.raises(EvidenceError):
        snapshot_daily_bars(mk, CODE, date(2026, 4, 1), date(2026, 4, 2))


def test_synth_helper_produces_calendar_days():
    bs = synth_bars(CODE, 30, date(2026, 3, 4))
    assert len(bs) == 30 and all(cal.is_session("US", b.session_date) for b in bs)


def test_get_daily_prefers_ok_over_later_partial(mk):
    t1, t2 = datetime(2026, 3, 3, 22, 0, tzinfo=UTC), datetime(2026, 3, 4, 9, 0, tzinfo=UTC)
    put_daily(mk, [bar()], source="s1", received_at=t1)
    put_daily(mk, [bar(c="10.9", h="11.5")], source="s1", received_at=t2, quality="partial")
    got = get_daily(mk, CODE, date(2026, 3, 3), date(2026, 3, 3))
    assert got[0]["close"] == "10.5" and got[0]["quality"] == "ok"      # 终值不被更晚的残缺行覆盖


def test_vendor_row_with_inconsistent_ohlc_is_rejected_alone_and_leaves_a_gap(mk):
    now = after_close(date(2026, 3, 5))
    bad = bar("2026-03-04", o="10", h="11", lo="10.2", c="10.8")            # 开盘价低于最低价：供应商自相矛盾（真实首跑出现过）
    res = collect_daily(mk, [FakeSource("v", [bar("2026-03-03"), bad, bar("2026-03-05")])], CODE, date(2026, 3, 2), date(2026, 3, 5), run_id="r1", now=now)
    assert res["status"] == "partial" and res["rejected_invalid_ohlc"] == ["2026-03-04"]
    assert [r["session_date"] for r in get_daily(mk, CODE, date(2026, 3, 1), date(2026, 3, 31))] == ["2026-03-03", "2026-03-05"]   # 好行入库，坏行是缺口


def test_nan_rows_from_vendor_are_dropped_not_fatal(mk):
    now = after_close(date(2026, 3, 5))
    nan_bar = DailyBar(CODE, date(2026, 3, 4), "nan", "nan", "nan", "nan", "nan", "0")
    res = collect_daily(mk, [FakeSource("v", [bar("2026-03-03"), nan_bar, bar("2026-03-05")])], CODE, date(2026, 3, 2), date(2026, 3, 5), run_id="r1", now=now)
    assert res["status"] == "partial" and res["rejected_invalid_ohlc"] == ["2026-03-04"]


def test_fx_bad_rows_are_dropped_not_the_whole_pair(mk):
    """审核 C-05：汇率有一行 NaN/非正：丢该行（留缺口），其余照常入库并留回执。"""
    rows = [(date(2026, 3, 2), "7.8"), (date(2026, 3, 3), "nan"), (date(2026, 3, 4), "0"), (date(2026, 3, 5), "7.81")]
    r = collect_fx(mk, [FakeSource("fx1", rows)], "USDHKD", date(2026, 3, 2), date(2026, 3, 5))
    assert r["status"] == "ok" and r["rows"] == 2
    assert [x["rate_date"] for x in mk.execute("SELECT rate_date FROM fx_rate ORDER BY rate_date")] == ["2026-03-02", "2026-03-05"]
    assert "dropped_invalid=2" in mk.execute("SELECT detail FROM collection_log WHERE kind='fx'").fetchone()["detail"]


def test_read_only_uri_escapes_question_marks_and_hashes(tmp_path):
    """审核 P3：库路径含 ? 或 # 时，只读连接仍是只读，且不会另建文件。"""
    import sqlite3

    from mystock2.core import db as dbmod
    d = tmp_path / "a?b#c"
    d.mkdir()
    p = d / "x.db"
    sqlite3.connect(p).execute("CREATE TABLE t(x)")
    ro = dbmod.connect_ro(p)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("CREATE TABLE u(x)")
    assert sorted(x.name for x in tmp_path.iterdir()) == ["a?b#c"]
