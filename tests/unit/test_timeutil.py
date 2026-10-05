from datetime import datetime, timedelta, timezone

import pytest

from mystock2.core.timeutil import EvidenceTimes, TimeError, check_time_chain, ensure_utc, iso_utc, market_local_to_utc

T0 = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)


def test_naive_time_is_rejected():
    with pytest.raises(TimeError):
        ensure_utc(datetime(2026, 1, 1, 0, 0))
    with pytest.raises(TimeError):
        ensure_utc("2026-01-01T00:00:00")


def test_iso_utc_normalizes_offsets_and_z():
    assert iso_utc("2026-01-01T08:00:00+08:00") == "2026-01-01T00:00:00.000000Z"
    assert iso_utc("2026-01-01T00:00:00.000000Z") == "2026-01-01T00:00:00.000000Z"
    assert iso_utc(datetime(2026, 1, 1, 0, 0, 0, 5, tzinfo=timezone.utc)) == "2026-01-01T00:00:00.000005Z"


def test_dst_changes_utc_offset_of_us_open_deadline():
    # 美股 09:00 ET：冬令 UTC-5 → 14:00Z；夏令 UTC-4 → 13:00Z
    assert market_local_to_utc("US", 2026, 1, 15, 9).hour == 14
    assert market_local_to_utc("US", 2026, 7, 15, 9).hour == 13
    # 港股无夏令时，08:30 HKT = 00:30Z
    assert market_local_to_utc("HK", 2026, 7, 15, 8, 30).hour == 0


def test_time_chain_ok_and_violations():
    ok = EvidenceTimes(T0, T0 + timedelta(minutes=1), T0 + timedelta(minutes=2), T0 + timedelta(minutes=3), T0 + timedelta(minutes=10))
    assert check_time_chain(ok) == []
    # T-38：08:10 冻结的版本引用 08:20 才收到的资料
    late = EvidenceTimes(
        received_at=T0 + timedelta(minutes=20),
        input_cutoff_at=T0 + timedelta(minutes=5),
        generated_at=T0 + timedelta(minutes=6),
        frozen_at=T0 + timedelta(minutes=10),
        deadline_at=T0 + timedelta(minutes=30),
    )
    assert check_time_chain(late) == ["received_after_input_cutoff"]
    over = EvidenceTimes(T0, T0, T0, T0 + timedelta(minutes=31), T0 + timedelta(minutes=30))
    assert check_time_chain(over) == ["frozen_after_deadline"]
