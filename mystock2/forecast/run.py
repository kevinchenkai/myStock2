"""生成并留档一次预测（点时：只用「截止时点已收到」的行情）。"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from mystock2.core import calendars as cal
from mystock2.core.timeutil import ensure_utc, utc_now
from mystock2.forecast.baseline import BaselineParams, ForecastUnavailable, bars_from_rows
from mystock2.forecast.versions import record_prediction
from mystock2.instruments.code_map import market_of
from mystock2.market.bars import get_daily
from mystock2.market.evidence import snapshot_daily_bars


def generate(conn_market: sqlite3.Connection, conn_forecast: sqlite3.Connection, code: str, as_of_session: date, *,
             input_cutoff_at: datetime, source_tag: str = "forward", params: BaselineParams | None = None,
             history_calendar_days: int = 900, now: datetime | None = None, model: str = "baseline") -> str:
    """as_of_session 为数据截至的交易日 T。只使用 `received_at ≤ input_cutoff_at` 且 `quality='ok'` 的行情（未走完的当日 bar 不入）。

    conn_market 须是可写 market 连接（写证据快照）；conn_forecast 须是 forecast 写连接（写预测版本）。
    """
    if model == "lgbm":
        from mystock2.forecast import lgbm_cqr as impl
        params = params or impl.LGBMParams()
        history_calendar_days = max(history_calendar_days, int(params.train_days * 1.6) + 60)
    elif model == "baseline":
        from mystock2.forecast import baseline as impl
        params = params or BaselineParams()
    else:
        raise ForecastUnavailable(f"未知模型：{model}")
    market = market_of(code)
    if not cal.is_session(market, as_of_session):
        raise ForecastUnavailable(f"{as_of_session} 不是 {market} 交易日")
    cutoff = ensure_utc(input_cutoff_at)
    start = as_of_session - timedelta(days=history_calendar_days)
    rows = [r for r in get_daily(conn_market, code, start, as_of_session, received_by=cutoff) if r["quality"] == "ok"]
    if not rows or rows[-1]["session_date"] != as_of_session.isoformat():
        raise ForecastUnavailable(f"{code} 缺少 {as_of_session} 的终值日线（截止 {cutoff.isoformat()} 前未收到），不用旧数据冒充")
    bars = bars_from_rows(rows)
    pred = impl.predict(bars, params)
    trust = "exact" if source_tag == "forward" else "assumed_bar_end"
    sid = snapshot_daily_bars(conn_market, code, bars[0].session_date, as_of_session, received_by=cutoff, time_trust=trust)
    generated = ensure_utc(now or utc_now())
    return record_prediction(conn_forecast, code, pred, params, [sid], input_cutoff_at=cutoff, generated_at=generated, available_at=generated, source_tag=source_tag,
                             model_version=impl.MODEL_VERSION, feature_version=impl.FEATURE_VERSION)
