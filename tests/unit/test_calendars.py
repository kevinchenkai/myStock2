from datetime import date, datetime, timezone

import pytest

from mystock2.core import calendars as cal


def test_us_known_closures_and_sessions():
    assert not cal.is_session("US", "2025-07-04")      # 独立日
    assert not cal.is_session("US", "2025-11-27")      # 感恩节
    assert cal.is_session("US", "2025-07-03")
    assert not cal.is_session("US", "2025-07-05")      # 周六


def test_us_half_day_after_thanksgiving():
    s = cal.session("US", "2025-11-28")
    assert s.is_half_day
    assert s.close_utc == datetime(2025, 11, 28, 18, 0, tzinfo=timezone.utc)   # 13:00 ET = 18:00Z
    assert not cal.session("US", "2025-11-26").is_half_day


def test_us_dst_open_time_shifts():
    assert cal.session("US", "2026-01-15").open_utc.hour == 14 and cal.session("US", "2026-01-15").open_utc.minute == 30
    assert cal.session("US", "2026-07-15").open_utc.hour == 13 and cal.session("US", "2026-07-15").open_utc.minute == 30


def test_hk_lunch_break_and_new_year_closure():
    assert not cal.is_session("HK", "2025-01-29")      # 农历新年
    s = cal.session("HK", "2025-03-10")
    assert s.break_start_utc is not None and s.break_end_utc is not None
    assert s.break_start_utc < s.break_end_utc
    assert not s.is_half_day


def test_hk_half_day_new_year_eve():
    assert cal.session("HK", "2027-12-31").is_half_day


def test_hk_typhoon_closure_removed():
    assert not cal.is_session("HK", "2023-09-08")      # V1 记录的黑雨休市
    assert not cal.is_session("HK", "2023-09-01")


def test_next_and_prev_session_skip_weekend_and_holiday():
    assert cal.next_session("US", "2025-07-03") == date(2025, 7, 7)   # 7/4 休市 + 周末
    assert cal.prev_session("US", "2025-07-07") == date(2025, 7, 3)
    assert cal.next_session("US", "2025-07-04") == date(2025, 7, 7)   # 从非交易日出发也可


def test_out_of_coverage_fails_closed():
    with pytest.raises(cal.CalendarError):
        cal.is_session("US", "2028-01-03")
    with pytest.raises(cal.CalendarError):
        cal.is_session("HK", "2019-12-31")
    with pytest.raises(cal.CalendarError):
        cal.next_session("US", "2027-12-31")
    with pytest.raises(cal.CalendarError):
        cal.session("US", "2025-07-04")   # 非交易日取 session
    with pytest.raises(cal.CalendarError):
        cal.is_session("JP", "2025-01-01")


def test_project_deadline_uses_local_time_and_dst():
    d_winter = cal.project_deadline("US", "2026-01-15")
    d_summer = cal.project_deadline("US", "2026-07-15")
    assert (d_winter.hour, d_summer.hour) == (14, 13)
    hk = cal.project_deadline("HK", "2025-03-10")
    assert (hk.hour, hk.minute) == (0, 30)
    with pytest.raises(cal.CalendarError):
        cal.project_deadline("US", "2025-07-04")   # 休市日没有截止


def test_session_days_range_and_warnings():
    days = cal.session_days("US", "2025-12-22", "2025-12-26")
    assert date(2025, 12, 25) not in days and date(2025, 12, 24) in days
    assert cal.calendar_warnings(datetime(2026, 1, 1, tzinfo=timezone.utc)) == []
    w = cal.calendar_warnings(datetime(2027, 12, 1, tzinfo=timezone.utc))
    assert w and w[0]["status"] == "calendar_expiring"
    assert cal.calendar_warnings(datetime(2028, 2, 1, tzinfo=timezone.utc))[0]["status"] == "calendar_expired"
