"""数据状态视图（M4/M6 运维信息；只读）。

- 采集：按（标的、种类、来源）列最近一次尝试与最近一次成功；失败/空/部分/陈旧一律显示，不记零；最近成功太久标「陈旧」。
- 行情：每个标的最新日线日期 vs 应有的最近已收盘交易日；缺口交易日用 `core.calendars` 与 `market.bars.missing_sessions` 的逻辑；
  小时线归档的粗检；汇率只列最新日期（外汇无交易所日历，不判缺口）。
- 预测：版本数量与最新目标日——**只有计数与日期，绝不返回预测区间/限价**（密封，§6A.2）。
- 运行回执：状态、起止、可重试范围；详情只保留数值/布尔摘要与错误类型，不回显自由文本。
- 协议冻结：是否已冻结、是否 pilot（`protocol_freeze.summary_json`）；账户快照新鲜度。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from mystock2.core import calendars as cal
from mystock2.core.timeutil import ensure_utc, iso_utc, to_market_time
from mystock2.instruments.code_map import CodeError, market_of
from mystock2.market.bars import get_daily, hourly_archive_gaps, missing_sessions
from mystock2.web import common as C
from mystock2.web.valuation import expected_session

PROBLEM = {"error": "最近一次失败", "empty": "最近一次为空", "stale": "最近一次陈旧", "partial": "最近一次部分成功"}
KIND_TEXT = {"daily": "日线", "hourly": "小时线", "fx": "汇率"}
MAX_LISTED_GAPS = 20


def _age_hours(ts: str | None, now) -> float | None:
    return None if ts is None else (ensure_utc(now) - ensure_utc(ts)).total_seconds() / 3600


def _age_text(hours: float | None) -> str:
    if hours is None:
        return C.UNKNOWN
    return f"{round(hours * 60)} 分钟" if hours < 1.5 else f"{round(hours)} 小时" if hours < 48 else f"{round(hours / 24)} 天"


def _collection(conn, now, stale_hours: int) -> list[dict]:
    groups = conn.execute(
        "SELECT code, kind, source, COUNT(*) AS n, SUM(status='ok') AS n_ok, SUM(status='empty') AS n_empty, SUM(status='error') AS n_error, "
        "SUM(status='stale') AS n_stale, SUM(status='partial') AS n_partial, MAX(attempted_at) AS last_attempt, "
        "MAX(CASE WHEN status='ok' THEN attempted_at END) AS last_ok FROM collection_log GROUP BY code, kind, source").fetchall()
    out = []
    for g in groups:
        last = conn.execute("SELECT status, rows, detail, attempted_at FROM collection_log WHERE code=? AND kind=? AND source=? ORDER BY attempted_at DESC, attempt_id DESC LIMIT 1",
                            (g["code"], g["kind"], g["source"])).fetchone()
        ok_age = _age_hours(g["last_ok"], now)
        if g["last_ok"] is None:
            state, label = "never_ok", "从未成功"
        elif last["status"] in PROBLEM:
            state, label = last["status"], PROBLEM[last["status"]]
        elif ok_age is not None and ok_age > stale_hours:
            state, label = "stale_age", f"陈旧（距最近成功 {_age_text(ok_age)}）"
        else:
            state, label = "ok", "正常"
        detail = (last["detail"] or "")[:200] if last["status"] != "ok" else ""
        out.append({
            "code": g["code"], "kind": g["kind"], "kind_text": KIND_TEXT.get(g["kind"], g["kind"]), "source": g["source"],
            "state": state, "state_text": label, "problem": state != "ok",
            "last_attempt_at": g["last_attempt"], "last_status": last["status"], "last_ok_at": g["last_ok"], "last_ok_age": C.text_cell(_age_text(ok_age)) if g["last_ok"] else C.na_cell("从未成功"),
            "last_rows": last["rows"], "last_detail": detail,
            "counts": {"attempts": g["n"], "ok": g["n_ok"], "empty": g["n_empty"], "error": g["n_error"], "stale": g["n_stale"], "partial": g["n_partial"]},
        })
    out.sort(key=lambda r: (not r["problem"], r["kind"], r["code"], r["source"]))
    return out


def _safe_market(code: str) -> str | None:
    try:
        return market_of(code)
    except CodeError:
        return None


def _quotes(conn, now, gap_days: int) -> list[dict]:
    codes = sorted({r["code"] for r in conn.execute("SELECT DISTINCT code FROM quote_daily")} | {r["code"] for r in conn.execute("SELECT DISTINCT code FROM quote_hourly")} |
                   {r["code"] for r in conn.execute("SELECT DISTINCT code FROM collection_log WHERE kind IN ('daily','hourly')")})
    out = []
    for code in codes:
        market = _safe_market(code)
        latest = conn.execute("SELECT session_date, quality, source, event_at, received_at FROM quote_daily WHERE code=? ORDER BY session_date DESC, version DESC LIMIT 1", (code,)).fetchone()
        first = conn.execute("SELECT MIN(session_date) AS d FROM quote_daily WHERE code=?", (code,)).fetchone()["d"]
        hourly = conn.execute("SELECT bar_start, complete, received_at, source FROM quote_hourly WHERE code=? ORDER BY bar_start DESC, version DESC LIMIT 1", (code,)).fetchone()
        exp = expected_session(market, now) if market else None
        row = {"code": code, "market": market, "expected_session": exp.isoformat() if exp else None,
               "latest_daily": {"session_date": latest["session_date"], "quality": latest["quality"], "source": latest["source"], "received_at": latest["received_at"]} if latest else None,
               "latest_hourly": {"bar_start": hourly["bar_start"], "complete": bool(hourly["complete"]), "received_at": hourly["received_at"], "source": hourly["source"]} if hourly else None,
               "lag_sessions": None, "missing_sessions": [], "missing_count": None, "non_ok_days": None, "hourly_problem_days": None, "state": "no_quotes", "state_text": "无日线行情"}
        if latest and exp and market:
            try:
                lag = len(cal.session_days(market, date.fromisoformat(latest["session_date"]) + timedelta(days=1), exp)) if latest["session_date"] < exp.isoformat() else 0
                start = max(exp - timedelta(days=gap_days), date.fromisoformat(first))
                miss = missing_sessions(conn, code, start, exp) if start <= exp else []
                non_ok = [r for r in get_daily(conn, code, start, exp) if r["quality"] != "ok"] if start <= exp else []
            except cal.CalendarError:
                lag, miss, non_ok = None, [], []
            row.update({"lag_sessions": lag, "missing_sessions": [d.isoformat() for d in miss[:MAX_LISTED_GAPS]], "missing_count": len(miss), "non_ok_days": len(non_ok)})
            if lag:
                row.update({"state": "behind", "state_text": f"落后 {lag} 个交易日"})
            elif miss:
                row.update({"state": "gaps", "state_text": f"最新，但区间内缺 {len(miss)} 个交易日"})
            elif non_ok:
                row.update({"state": "quality", "state_text": f"最新，但区间内有 {len(non_ok)} 日质量非 ok"})
            else:
                row.update({"state": "ok", "state_text": "最新且区间内无缺口"})
        elif latest:
            row.update({"state": "unknown", "state_text": "无法判断，缺市场或日历覆盖"})
        if hourly and market and exp:
            first_h = to_market_time(conn.execute("SELECT MIN(bar_start) AS b FROM quote_hourly WHERE code=?", (code,)).fetchone()["b"], market).date()
            start_h = max(exp - timedelta(days=gap_days), first_h)
            try:
                row["hourly_problem_days"] = len({p["session"] for p in hourly_archive_gaps(conn, code, start_h, exp)}) if start_h <= exp else 0
            except cal.CalendarError:
                row["hourly_problem_days"] = None
        row["problem"] = row["state"] != "ok"
        out.append(row)
    out.sort(key=lambda r: (not r["problem"], r["code"]))
    return out


def _fx(conn, now) -> list[dict]:
    out = []
    for r in conn.execute("SELECT pair, MAX(rate_date) AS d FROM fx_rate GROUP BY pair ORDER BY pair"):
        last = conn.execute("SELECT source, received_at, event_at FROM fx_rate WHERE pair=? AND rate_date=? ORDER BY version DESC LIMIT 1", (r["pair"], r["d"])).fetchone()
        age = (ensure_utc(now).date() - date.fromisoformat(r["d"])).days
        out.append({"pair": r["pair"], "latest_rate_date": r["d"], "source": last["source"], "received_at": last["received_at"], "age_days": age,
                    "note": "汇率没有交易所日历，只显示最新日期，不判断缺口"})
    return out


def _predictions(conn) -> dict:
    """只有计数与日期。刻意不选取 y_low/y_high/low_price/high_price 等区间字段（密封，§6A.2）。"""
    by_model = [{"model_version": r["model_version"], "source_tag": r["source_tag"], "count": r["n"], "latest_target_session": r["t"], "latest_generated_at": r["g"]}
                for r in conn.execute("SELECT model_version, source_tag, COUNT(*) AS n, MAX(target_session) AS t, MAX(generated_at) AS g FROM prediction_version "
                                      "GROUP BY model_version, source_tag ORDER BY model_version, source_tag")]
    by_code = [{"code": r["code"], "count": r["n"], "latest_target_session": r["t"], "latest_generated_at": r["g"]}
               for r in conn.execute("SELECT code, COUNT(*) AS n, MAX(target_session) AS t, MAX(generated_at) AS g FROM prediction_version GROUP BY code ORDER BY code")]
    total = conn.execute("SELECT COUNT(*) AS n, MAX(target_session) AS t, MAX(generated_at) AS g FROM prediction_version").fetchone()
    return {"total": total["n"], "latest_target_session": total["t"], "latest_generated_at": total["g"], "by_model": by_model, "by_code": by_code,
            "note": "只显示版本数量与最新目标日；预测区间与由它推出的限价属于密封内容，在揭示前不经 Web 显示。"}


def _detail_summary(text: str | None) -> dict:
    try:
        d = json.loads(text or "{}")
    except ValueError:
        return {}
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, (bool, int)):
                out[str(k)] = v
        if isinstance(d.get("error"), str):
            out["error"] = d["error"]
    return out


def _runs(conn, now, limit: int) -> dict:
    rows = conn.execute("SELECT run_id, command, started_at, finished_at, status, detail_json, retry_scope FROM run_log ORDER BY started_at DESC, run_id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        age = _age_hours(r["started_at"], now)
        dangling = r["status"] == "running" and age is not None and age > 1
        out.append({"run_id": r["run_id"], "command": r["command"], "status": r["status"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                    "retry_scope": r["retry_scope"], "detail": _detail_summary(r["detail_json"]), "problem": r["status"] in ("failed", "partial") or dangling,
                    "note": "仍在运行或已中断（超过 1 小时未结束）" if dangling else ""})
    last_by_cmd = []
    for r in conn.execute("SELECT command, MAX(started_at) AS s FROM run_log GROUP BY command ORDER BY command"):
        x = conn.execute("SELECT run_id, status, started_at FROM run_log WHERE command=? AND started_at=? ORDER BY run_id DESC LIMIT 1", (r["command"], r["s"])).fetchone()
        last_by_cmd.append({"command": r["command"], "run_id": x["run_id"], "status": x["status"], "started_at": x["started_at"]})
    return {"recent": out, "last_by_command": last_by_cmd}


def _snapshots(conn, now) -> list[dict]:
    out = []
    for a in conn.execute("SELECT account_id FROM account ORDER BY account_id"):
        s = conn.execute("SELECT snapshot_id, captured_at, source FROM account_snapshot WHERE account_id=? ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1", (a["account_id"],)).fetchone()
        n = conn.execute("SELECT COUNT(*) AS n FROM account_snapshot WHERE account_id=?", (a["account_id"],)).fetchone()["n"]
        if s is None:
            out.append({"account_id": a["account_id"], "snapshots": 0, "captured_at": None, "age": C.na_cell("没有快照"), "source": None, "positions": None, "currencies": None})
            continue
        npos = conn.execute("SELECT COUNT(*) AS n FROM snapshot_position WHERE snapshot_id=?", (s["snapshot_id"],)).fetchone()["n"]
        ncash = conn.execute("SELECT COUNT(*) AS n FROM snapshot_cash WHERE snapshot_id=?", (s["snapshot_id"],)).fetchone()["n"]
        out.append({"account_id": a["account_id"], "snapshots": n, "captured_at": s["captured_at"], "age": _age_text(_age_hours(s["captured_at"], now)),
                    "source": s["source"], "positions": npos, "currencies": ncash})
    return out


def _protocols(conn) -> dict:
    rows = []
    for r in conn.execute("SELECT * FROM protocol_freeze ORDER BY frozen_at, protocol_version"):
        try:
            summary = json.loads(r["summary_json"])
        except ValueError:
            summary = {}
        rows.append({"protocol_version": r["protocol_version"], "hash": r["protocol_hash"], "frozen_at": r["frozen_at"], "code_sha": r["code_sha"],
                     "pilot": bool(summary.get("pilot")), "missing": list(summary.get("missing") or []), "universe_size": summary.get("universe_size"),
                     "markets": list(summary.get("markets") or [])})
    if not rows:
        verdict = {"frozen": False, "pilot": True, "text": "尚未冻结任何协议：所有记录标 pilot，不是确认样本"}
    elif rows[-1]["pilot"]:
        verdict = {"frozen": True, "pilot": True, "text": "最近一次冻结标 pilot（协议有缺失项）：记录仍不是确认样本"}
    else:
        verdict = {"frozen": True, "pilot": False, "text": "已冻结且完整（非 pilot）"}
    return {"rows": rows, "verdict": verdict}


def run(conn, params):
    now = C.now_of(params)
    collection = _collection(conn, now, params.get("collect_stale_hours", 96))
    quotes = _quotes(conn, now, params.get("gap_days", 30))
    fx = _fx(conn, now)
    preds = _predictions(conn)
    runs = _runs(conn, now, params.get("runs", 20))
    snaps = _snapshots(conn, now)
    protocols = _protocols(conn)
    summary = {
        "collection_problems": sum(1 for r in collection if r["problem"]), "collection_groups": len(collection),
        "quote_problems": sum(1 for r in quotes if r["problem"]), "quote_codes": len(quotes),
        "run_problems": sum(1 for r in runs["recent"] if r["problem"]),
        "prediction_versions": preds["total"], "protocol_frozen": protocols["verdict"]["frozen"], "pilot": protocols["verdict"]["pilot"],
    }

    srcs = []
    r = conn.execute("SELECT MAX(attempted_at) AS a FROM collection_log").fetchone()
    if r["a"]:
        srcs.append(C.source("采集回执", r["a"], r["a"]))
    r = conn.execute("SELECT MAX(event_at) AS e, MAX(received_at) AS r FROM quote_daily").fetchone()
    if r["e"]:
        late = [q["code"] for q in quotes if q.get("state") == "behind"]
        srcs.append(C.source("日线行情", r["e"], r["r"], behind=f"{len(late)} 个标的落后于应有的最近收盘日（如 {late[0]}）" if late else None))
    r = conn.execute("SELECT MAX(started_at) AS s, MAX(COALESCE(finished_at, started_at)) AS f FROM run_log").fetchone()
    if r["s"]:
        srcs.append(C.source("运行回执", r["s"], r["f"]))
    r = conn.execute("SELECT MAX(captured_at) AS c FROM account_snapshot").fetchone()
    if r["c"]:
        srcs.append(C.source("券商快照", r["c"], r["c"]))
    if not srcs:
        srcs.append(C.source("数据状态", None, None))
    notes = ["缺失显示「不可用/未知」，不记零；陈旧的数据不当作新数据"]
    notes += [f"日历提示：{w['status']}（剩余 {w['calendar_days_left']} 天）" for w in cal.calendar_warnings(now)]
    return {
        "summary": summary, "collection": collection, "quotes": quotes, "fx": fx, "predictions": preds, "runs": runs, "snapshots": snaps,
        "protocols": protocols, "params": {"gap_days": params.get("gap_days", 30), "collect_stale_hours": params.get("collect_stale_hours", 96)},
        "calendar_warnings": cal.calendar_warnings(now), "now": iso_utc(now), "_freshness": C.freshness(srcs, notes),
    }
