"""记分牌视图（M5 WP5.7；口径见 ADR 0002 与实施方案 §6A.5–6A.9）。

- 读 `eval_run`、`sleeve_daily`、`sim_fill`、`strategy_line`、`comparison_batch`，指标与统计调用 `scoreboard.metrics/stats` 纯函数（口径单一来源）。
- 三类证据路径**分区呈现**：正式模拟线（human_plan/ai/ai_veto/ai_lgbm/buyhold）、`human_actual`（描述性）、`live_guidance`（占位）；**不合成单一胜率结论**。
- UNKNOWN/PAUSED 日权益为 None：曲线断开并标注，不插值、不记零；配对差只在两条线均为 OK 的日期上计算并显示配对覆盖率；
  置信区间只作描述，不触发晋级。页头固定提示「模拟线结论不等于按指导操作的结论」（§6A.9）。
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.core.money import dec, to_db
from mystock2.core.timeutil import ensure_utc, iso_utc
from mystock2.scoreboard.metrics import summarize
from mystock2.scoreboard.stats import ambiguous_dates, block_bootstrap_ci, paired_diff
from mystock2.scoreboard.types import DayResult, SimFill
from mystock2.web import common as C

FORMAL_KINDS = ("human_plan", "ai", "ai_veto", "ai_lgbm", "buyhold")
KIND_TEXT = {"human_plan": "人类计划线（模拟）", "ai": "AI 线（模拟）", "ai_veto": "AI+LLM 否决线（模拟）", "ai_lgbm": "AI 候选线 LightGBM（模拟）",
             "buyhold": "买入持有（模拟）", "human_actual": "真实成交（描述性）"}
PAIRS = (("ai", "human_plan"), ("ai", "buyhold"))
BANNER = ("模拟线结论不等于按指导操作的结论：要说「按指导操作能赚得更多」，必须另有执行保真证据（live_guidance 与 ai 线的动作一致率、"
          "真实成交与 live_guidance 的一致率达到预注册阈值）。目前这些证据未提供，对外只能表述为「执行诊断」（方案 §6A.9）。")
CI_NOTE = "滚动/区间估计只作描述，不触发晋级（方案 §6A.7）"
STATUS_TEXT = {"OK": "正常", "UNKNOWN": "未知（无法确定成交或估值）", "PAUSED": "已暂停（自未知日起不评分）"}


def _json(text, default):
    try:
        return json.loads(text) if text else default
    except (ValueError, TypeError):
        return default


def _results(rows, fills) -> list[DayResult]:
    by_day: dict[str, list[SimFill]] = {}
    for f in fills:
        by_day.setdefault(f["date"], []).append(SimFill(date.fromisoformat(f["date"]), f["seq"], f["code"], f["side"], dec(f["qty"]), dec(f["price"]),
                                                       dec(f["fee"]), None, bool(f["ambiguous"]), f["note"] or ""))
    out = []
    for r in rows:
        fs = by_day.get(r["date"], [])
        res = DayResult(date.fromisoformat(r["date"]), r["status"], dec(r["equity"]) if r["equity"] is not None else None,
                        dec(r["cash"]) if r["cash"] is not None else None, dec(r["unsettled"]) if r["unsettled"] is not None else None,
                        dec(r["position_value"]) if r["position_value"] is not None else None, dec(r["fees_day"]) if r["fees_day"] is not None else Decimal(0),
                        {c: dec(q) for c, q in _json(r["positions_json"], {}).items()}, list(_json(r["flags_json"], [])), fs)
        res.traded_notional = sum((f.qty * f.price for f in fs), Decimal(0))
        out.append(res)
    return out


def _metric_cells(results: list[DayResult], e0: Decimal, ccy: str, reason: str | None = None) -> dict:
    if reason is not None or not results:
        na = C.na_cell(reason or "该线在本 run 中没有日度结果")
        return {k: na for k in ("cumulative_return", "max_drawdown", "turnover", "avg_exposure", "coverage", "fees_cum")} | \
               {"days_planned": None, "days_ok": None, "unknown_days": None, "paused_days": None, "ambiguous_days": None, "fills": None, "last_ok_date": None}
    s = summarize(results, e0)
    last_ok = next((r.date for r in reversed(results) if r.status == "OK"), None)
    return {
        "cumulative_return": C.pct_cell(s.cumulative_return, colored=True, reason="没有任何正常（OK）日，或 E0 为 0"),
        "max_drawdown": C.pct_cell(s.max_drawdown, reason="没有任何正常（OK）日"),
        "turnover": C.pct_cell(s.turnover, reason="平均权益不可用"),
        "avg_exposure": C.pct_cell(s.avg_exposure, reason="没有正常日或权益为 0"),
        "coverage": C.pct_cell(s.coverage, reason="没有计划日"),
        "fees_cum": C.money_cell(s.fees_cum, ccy),
        "days_planned": s.days_planned, "days_ok": s.days_ok, "unknown_days": s.unknown_days, "paused_days": s.paused_days,
        "ambiguous_days": s.ambiguous_days, "fills": s.fills, "last_ok_date": last_ok.isoformat() if last_ok else None,
    }


def _pct_or_na(v: float | None, reason: str, *, colored: bool = False) -> dict:
    return C.na_cell(reason) if v is None else C.pct_cell(Decimal(str(v)), colored=colored)


def _pair(a_kind: str, b_kind: str, runs: dict, e0: Decimal) -> dict:
    name = f"{KIND_TEXT[a_kind]} − {KIND_TEXT[b_kind]}"
    out = {"a": a_kind, "b": b_kind, "name": name, "note": "只在两条线同日均为 OK 的日期上配对；不是单一胜率结论"}
    if a_kind not in runs or b_kind not in runs:
        miss = [k for k in (a_kind, b_kind) if k not in runs]
        return out | {"available": False, "reason": "该批次/run 没有 " + "、".join(miss) + " 的日度结果", "text": C.na_cell("缺少对照线")}
    a, b = runs[a_kind], runs[b_kind]
    pd = paired_diff(a, b, e0)
    ex = paired_diff(a, b, e0, exclude_dates=ambiguous_dates(a, b))
    ci = block_bootstrap_ci([float(v) for _, v in sorted(pd.deltas.items())])
    return out | {
        "available": True, "paired_days": pd.n, "planned_days": min(len(a), len(b)),
        "coverage": C.pct_cell(pd.coverage, reason="没有计划日"),
        "cumulative": C.pct_cell(pd.cumulative, colored=True) if pd.n else C.na_cell("没有可配对的日期"),
        "mean": C.pct_cell(pd.mean, colored=True) if pd.mean is not None else C.na_cell("没有可配对的日期"),
        "excluding_ambiguous": {"paired_days": ex.n, "cumulative": C.pct_cell(ex.cumulative, colored=True) if ex.n else C.na_cell("没有可配对的日期")},
        "interval": {"n": ci["n"], "lo": _pct_or_na(ci["lo"], "样本不足以做块自助法"), "hi": _pct_or_na(ci["hi"], "样本不足以做块自助法"),
                     "mean": _pct_or_na(ci["mean"], "没有配对日", colored=True), "note": CI_NOTE},
    }


def _protocol_block(conn, batch, run) -> dict:
    meta = _json(batch["initial_state_json"], {}).get("_meta", {})
    freezes = {r["protocol_version"]: r for r in conn.execute("SELECT * FROM protocol_freeze")}
    used = conn.execute("SELECT protocol_version, MIN(generated_at) AS first_gen, COUNT(*) AS n FROM ticket WHERE batch_id=? GROUP BY protocol_version",
                        (batch["batch_id"],)).fetchall()
    coach, reasons = [], []
    for u in used:
        fz = freezes.get(u["protocol_version"])
        summary = _json(fz["summary_json"], {}) if fz else {}
        entry = {"version": u["protocol_version"], "frozen": fz is not None, "tickets": u["n"],
                 "hash": fz["protocol_hash"] if fz else None, "frozen_at": fz["frozen_at"] if fz else None, "code_sha": fz["code_sha"] if fz else None,
                 "pilot_in_summary": bool(summary.get("pilot")) if fz else None, "missing": list(summary.get("missing") or []) if fz else []}
        coach.append(entry)
        if fz is None:
            reasons.append(f"协议 {u['protocol_version']} 没有冻结登记（protocol_freeze）")
        else:
            if summary.get("pilot"):
                reasons.append(f"协议 {u['protocol_version']} 以 pilot 冻结（缺失项：{'、'.join(summary.get('missing') or []) or '未列出'}）")
            if ensure_utc(u["first_gen"]) < ensure_utc(fz["frozen_at"]):
                reasons.append(f"协议 {u['protocol_version']} 冻结之前就已生成操作单（这些记录不是确认样本）")
    if not used:
        reasons.append("批次内没有操作单，无法关联到某个冻结协议")
    return {
        "exec_protocol": {"version": (run or batch)["protocol_version"], "params": _json(run["protocol_json"], {}) if run else {}},
        "batch_protocol_version": batch["protocol_version"], "batch_state_hash": batch["state_hash"], "batch_created_at": batch["created_at"],
        "batch_notes": list(meta.get("notes") or []), "coach_protocols": coach,
        "pilot": {"value": bool(reasons), "reasons": reasons},
        "evidence_snapshots": len(_json(run["evidence_json"], [])) if run else None,
    }


def run(conn, params):
    now = C.now_of(params)
    batches = conn.execute("SELECT * FROM comparison_batch ORDER BY created_at DESC, batch_id").fetchall()
    if not batches:
        raise C.ViewUnavailable("no_batch", "还没有比较批次：尚未运行 batch create（比较批次与记分牌 run 由受控 CLI 写入，Web 只读）")
    run_rows = conn.execute("SELECT * FROM eval_run ORDER BY created_at DESC, run_id DESC").fetchall()
    want_b, want_r = (params.get("batch") or "").strip(), (params.get("run") or "").strip()
    if want_r:
        picked = next((r for r in run_rows if r["run_id"] == want_r), None)
        if picked is None:
            raise C.ViewUnavailable("run_not_found", f"记分牌 run 不存在：{want_r}")
        want_b = want_b or picked["batch_id"]
        if picked["batch_id"] != want_b:
            raise C.ViewUnavailable("run_batch_mismatch", f"run {want_r} 属于批次 {picked['batch_id']}，不是 {want_b}")
    batch = next((b for b in batches if b["batch_id"] == want_b), None) if want_b else None
    if want_b and batch is None:
        raise C.ViewUnavailable("batch_not_found", f"批次不存在：{want_b}")
    if batch is None:
        have = {r["batch_id"] for r in run_rows}
        batch = next((b for b in batches if b["batch_id"] in have), batches[0])
    bid, ccy, e0 = batch["batch_id"], batch["currency"], dec(batch["e0"])
    batch_runs = [r for r in run_rows if r["batch_id"] == bid]
    the_run = next((r for r in batch_runs if r["run_id"] == want_r), None) if want_r else (batch_runs[0] if batch_runs else None)
    meta = _json(batch["initial_state_json"], {}).get("_meta", {})
    market = meta.get("market")
    lines = conn.execute("SELECT line_id, kind, protocol_version, params_hash FROM strategy_line WHERE batch_id=? ORDER BY line_id", (bid,)).fetchall()

    results: dict[str, list[DayResult]] = {}
    other_ccy = 0
    if the_run is not None:
        srows = conn.execute("SELECT * FROM sleeve_daily WHERE run_id=? ORDER BY line_id, date", (the_run["run_id"],)).fetchall()
        frows = conn.execute("SELECT * FROM sim_fill WHERE run_id=? ORDER BY line_id, date, seq", (the_run["run_id"],)).fetchall()
        other_ccy = sum(1 for r in srows if r["currency"] != ccy)
        for ln in lines:
            rows = [r for r in srows if r["line_id"] == ln["line_id"] and r["currency"] == ccy]
            if rows:
                results[ln["line_id"]] = _results(rows, [f for f in frows if f["line_id"] == ln["line_id"]])

    saved = _json(the_run["metrics_json"], {}) if the_run is not None else {}
    mismatch = []

    def line_block(ln) -> dict:
        res = results.get(ln["line_id"])
        reason = None if the_run is not None else "没有记分牌 run：尚未运行 scoreboard run"
        cells = _metric_cells(res or [], e0, ccy, reason)
        last = res[-1] if res else None
        if res and ln["line_id"] in saved and the_run is not None:
            s = summarize(res, e0).as_dict()
            if any(str(s[k]) != str(saved[ln["line_id"]].get(k)) for k in ("cumulative_return", "days_ok", "fills")):
                mismatch.append(ln["line_id"])
        return {"line_id": ln["line_id"], "kind": ln["kind"], "name": KIND_TEXT.get(ln["kind"], ln["kind"]), "protocol_version": ln["protocol_version"],
                "has_results": bool(res), "latest_status": last.status if last else None,
                "latest_status_text": STATUS_TEXT.get(last.status, last.status) if last else "不可用", "latest_date": last.date.isoformat() if last else None,
                "metrics": cells}

    formal = [line_block(ln) for ln in lines if ln["kind"] in FORMAL_KINDS]
    actual = [line_block(ln) for ln in lines if ln["kind"] == "human_actual"]

    def chart_for(kinds_lines) -> dict | None:
        ids = [(ln["line_id"], ln["kind"]) for ln in kinds_lines if ln["line_id"] in results]
        if not ids:
            return None
        dates = sorted({r.date.isoformat() for lid, _ in ids for r in results[lid]})
        series, gaps = [], {}
        for lid, kind in ids:
            by = {r.date.isoformat(): r for r in results[lid]}
            pts = [to_db(by[d].equity) if d in by and by[d].status == "OK" and by[d].equity is not None else None for d in dates]
            series.append({"line_id": lid, "kind": kind, "name": KIND_TEXT.get(kind, kind), "points": pts})
            for d in dates:
                if d in by and by[d].status != "OK":
                    gaps.setdefault(d, []).append(f"{KIND_TEXT.get(kind, kind)}：{by[d].status}")
        return {"currency": ccy, "dates": dates, "series": series, "gaps": {d: "；".join(v) for d, v in gaps.items()},
                "note": "UNKNOWN/PAUSED 日不画点、曲线断开并用灰带标注；不插值、不记零"}

    formal_lines = [ln for ln in lines if ln["kind"] in FORMAL_KINDS]
    kind_runs = {ln["kind"]: results[ln["line_id"]] for ln in formal_lines if ln["line_id"] in results}
    pairs = [_pair(a, b, kind_runs, e0) if the_run is not None else
             {"a": a, "b": b, "name": f"{KIND_TEXT[a]} − {KIND_TEXT[b]}", "available": False, "reason": "没有记分牌 run", "text": C.na_cell("没有记分牌 run")}
             for a, b in PAIRS]

    warnings = []
    if the_run is None:
        warnings.append("该批次还没有记分牌 run：指标与曲线显示「不可用」，不显示 0")
    if other_ccy:
        warnings.append(f"run 中有 {other_ccy} 行币种与批次币种（{ccy}）不同，未纳入（币种之间不相加）")
    if mismatch:
        warnings.append("以下线按库内日度结果重算的指标与 run 内存档指标不一致：" + "、".join(mismatch))
    for b in formal + actual:
        if b["latest_status"] in ("UNKNOWN", "PAUSED"):
            warnings.append(f"{b['name']}（{b['line_id']}）截至 {b['latest_date']} 处于 {b['latest_status']}：自该日起暂停评分，不记零、不沿用假定库存")

    last_date = max((r.date for rs in results.values() for r in rs), default=None)
    event_at = None
    if last_date is not None and market in ("HK", "US"):
        try:
            event_at = cal.session(market, last_date).close_utc
        except cal.CalendarError:
            event_at = None
    srcs = [C.source("记分牌 run", event_at, the_run["created_at"] if the_run is not None else None)]
    return {
        "banner": BANNER, "batch_id": bid, "batches": [b["batch_id"] for b in batches], "currency": ccy, "e0": C.money_cell(e0, ccy),
        "start_date": batch["start_date"], "market": market, "lines_declared": _json(batch["lines_json"], []),
        "run": {"run_id": the_run["run_id"], "created_at": the_run["created_at"], "protocol_version": the_run["protocol_version"]} if the_run is not None else None,
        "runs": [{"run_id": r["run_id"], "created_at": r["created_at"]} for r in batch_runs],
        "protocol": _protocol_block(conn, batch, the_run),
        "formal": {"title": "正式模拟线 · 同起点、同费用、同资金约束", "lines": formal, "chart": chart_for(formal_lines), "pairs": pairs,
                   "note": "ai 与 human_plan、ai 与 buyhold 的配对差是两条独立的描述，不合并为单一胜率结论。"},
        "descriptive": {"title": "human_actual · 描述性：真实成交原样记账", "lines": actual,
                        "chart": chart_for([ln for ln in lines if ln["kind"] == "human_actual"]),
                        "note": "真实成交按实际记账，现金可为负、资金口径与模拟线不同；不进入正式比较，也不与模拟线配对。",
                        "available": bool(actual)},
        "live_guidance": {"status": "未提供", "text": C.text_cell("未提供", tag="M6 未实现"),
                          "note": "live_guidance（真实账户指导单）尚未实现；执行保真证据缺失，故模拟线结论不能等同于按指导操作的结论。"},
        "warnings": warnings, "now": iso_utc(now),
        "_freshness": C.freshness(srcs, [BANNER, "指标口径：R(T)=(E_T−E0)/E0；最大回撤、换手、覆盖率来自 scoreboard.metrics；配对差来自 scoreboard.stats"]),
    }
