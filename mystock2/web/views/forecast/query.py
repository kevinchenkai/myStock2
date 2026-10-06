"""预测效果视图（只读）：基线（baseline）与 LightGBM+CQR（lgbm）对「次日日内最低/最高价区间」的预测 vs 实际。

预测对象（与 `forecast/` 一致，公式在此独立实现，视图不得导入预测/采集代码）：
- `y_low = low_{T+1}/close_T − 1`、`y_high = high_{T+1}/close_T − 1`（**复权**比例）；`low_price/high_price = 原始收盘价_T × (1 + y)`。
- 分位水平 α_low、α_high 取自每条预测的 `params_json`（默认 0.10 / 0.90）；名义尾部目标：P(实际低点 < 预测低点) ≈ α_low，P(实际高点 > 预测高点) ≈ 1 − α_high。
- 实际标签 = 目标日 OHLC 经复权因子（adj_close/close）折算后相对 T 日复权收盘价的比例（与评估口径相同）。
- pinball：`max(α·d, (α−1)·d)`，d = 实际 − 预测；raw 总损失 = 低侧均值 + 高侧均值。改善 = (基线 − 候选)/基线。
- V1 晋级门槛：等权平均改善 ≥ 5% 且至少 ⌈2n/3⌉ 个标的改善 > 0。**这里只作描述，不触发晋级**。

约定：
- `source_tag`：rebuilt＝事后重建（不是前向、不是当时可得，不能当作晋级证据）；forward＝当时生成并冻结。二者不混算，由参数 `source` 选择。
- 模型对比只在「两个模型都有预测、且目标日已有终值日线（quality='ok' 的最高 version）」的样本上做；样本 < 30 显示「不足」，不显示 0。
- **密封（§6A.2）**：目标日尚无终值日线、且该市场该目标日没有揭示记录的预测，价位与区间一律不返回，只显示「已密封」；目标日已结束的预测才展示数值。
- 不得导入 forecast/collectors/assistant；不得调用 utc_now()（时间经 common.now_of(params)）。
"""
from __future__ import annotations

import json
from decimal import Decimal

from mystock2.core.money import dec, to_db
from mystock2.core.timeutil import ensure_utc
from mystock2.instruments.code_map import CodeError, currency_of, market_of
from mystock2.web import common as C
from mystock2.web import sealing
from mystock2.web.valuation import expected_session

MIN_N = 30                                   # 样本不足阈值：低于它不给覆盖率/损失/改善
GATE_MEAN = Decimal("0.05")                  # V1 门槛：等权平均改善
ZERO = Decimal(0)
ONE = Decimal(1)
FAMILIES = ("baseline", "lgbm")
FAMILY_LABEL = {"baseline": "基线", "lgbm": "LightGBM+CQR"}
TAG_LABEL = {"rebuilt": "事后重建（rebuilt）", "forward": "前向（forward）"}
BANNER = "预测区间不是成交保证，不构成投资建议。"
REBUILT_WARNING = ("rebuilt＝事后重建：用历史数据在事后重新生成，不是前向、也不是当时可得的预测，不能当作晋级证据；"
                   "这里的对比窗口已被反复查看，只作描述，不触发任何晋级。")


# ------------------------------------------------------------------ 小工具
def _family(model_version: str) -> str | None:
    mv = (model_version or "").lower().replace("_", "-")
    if mv.startswith("naive"):
        return "baseline"
    if mv.startswith("lgbm"):
        return "lgbm"
    return None


def _alphas(params_json: str | None) -> tuple[Decimal, Decimal] | None:
    try:
        p = json.loads(params_json or "{}")
        lo, hi = dec(str(p["alpha_low"])), dec(str(p["alpha_high"]))
    except Exception:                                  # noqa: BLE001 — 参数缺失/损坏（含 MoneyError）：该样本不计入，不套默认值
        return None
    return (lo, hi) if ZERO < lo < hi < ONE else None


def _pin(y: Decimal, q: Decimal, alpha: Decimal) -> Decimal:
    d = y - q
    return max(alpha * d, (alpha - ONE) * d)


def _pct_text(v: Decimal, dp: int = 2) -> str:
    return C.fmt_pct(v, dp)


def _loss_text(v: Decimal) -> str:
    return C.fmt_decimal(v * 100, 4) + "%"


def _val_cell(text: str, v: Decimal, title: str | None = None) -> dict:
    c = C.text_cell(text, title=title)
    c["v"] = to_db(v.quantize(Decimal("0.0000000001")))
    return c


# ------------------------------------------------------------------ 读取
def _load_predictions(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT code, as_of_session, target_session, model_version, y_low, y_high, low_price, high_price, source_tag, generated_at, "
        "input_cutoff_at, params_json FROM prediction_version ORDER BY generated_at, rowid").fetchall()
    dedup: dict[tuple, dict] = {}
    for r in rows:
        key = (r["code"], r["as_of_session"], r["model_version"], r["source_tag"])
        dedup[key] = {"code": r["code"], "as_of": r["as_of_session"], "target": r["target_session"], "mv": r["model_version"],
                      "family": _family(r["model_version"]), "tag": r["source_tag"], "y_low": r["y_low"], "y_high": r["y_high"],
                      "low_price": r["low_price"], "high_price": r["high_price"], "generated_at": r["generated_at"],
                      "cutoff": r["input_cutoff_at"], "params_json": r["params_json"]}      # 同键多条：后生成者为准
    return list(dedup.values())


def _load_quotes(conn, codes: list[str]) -> dict[str, dict[str, dict]]:
    """每个标的、每个交易日取 quality='ok' 的最高 version。"""
    out: dict[str, dict[str, dict]] = {c: {} for c in codes}
    if not codes:
        return out
    marks = ",".join("?" * len(codes))
    sql = ("SELECT q.code, q.session_date, q.open, q.high, q.low, q.close, q.adj_close, q.event_at, q.received_at FROM quote_daily q "
           f"WHERE q.quality='ok' AND q.code IN ({marks}) AND q.version=(SELECT MAX(p.version) FROM quote_daily p "
           "WHERE p.code=q.code AND p.session_date=q.session_date AND p.quality='ok') ORDER BY q.code, q.session_date")
    for r in conn.execute(sql, codes):
        out[r["code"]][r["session_date"]] = dict(r)
    return out


def _pick_versions(preds: list[dict]) -> tuple[dict[str, str], list[str]]:
    """每个模型家族只取「最近生成」的那个 model_version（不混算不同版本）；其余版本与未识别的模型只在说明里列出。"""
    latest: dict[str, tuple[str, str]] = {}
    for p in preds:
        f = p["family"]
        if f and (f not in latest or p["generated_at"] > latest[f][0]):
            latest[f] = (p["generated_at"], p["mv"])
    chosen = {f: v[1] for f, v in latest.items()}
    notes = []
    all_versions = sorted({p["mv"] for p in preds})
    ignored = [v for v in all_versions if v not in chosen.values()]
    if ignored:
        notes.append("以下 model_version 未纳入（每个模型家族只取最近生成的版本，不识别的模型不展示）：" + "、".join(ignored))
    return chosen, notes


# ------------------------------------------------------------------ 样本
def _label(qa: dict | None, qt: dict | None) -> tuple[Decimal, Decimal] | None:
    """实际标签（复权口径，与评估一致）：low/high_{T+1}×(adj/close)_{T+1} ÷ adj_T − 1。缺任何一项返回 None。"""
    if qa is None or qt is None:
        return None
    try:
        adj_t, adj_n, close_n = dec(qa["adj_close"]), dec(qt["adj_close"]), dec(qt["close"])
        low_n, high_n = dec(qt["low"]), dec(qt["high"])
    except Exception:                                  # noqa: BLE001 — 缺失/损坏：该样本不可用
        return None
    if adj_t <= 0 or close_n <= 0 or adj_n <= 0:
        return None
    f = adj_n / close_n
    return low_n * f / adj_t - ONE, high_n * f / adj_t - ONE


def _build_samples(preds: list[dict], quotes: dict, tag: str) -> dict[str, dict]:
    """{family: {"rows": 该来源全部预测数, "samples": {(code, as_of): 样本}, "pending": 目标日未结束, "unscorable": 缺数据/分位不一致, "alphas": (lo, hi)|None}}"""
    out: dict[str, dict] = {}
    for fam in FAMILIES:
        rows = [p for p in preds if p["family"] == fam and p["tag"] == tag]
        counts: dict[tuple, int] = {}
        for p in rows:
            a = _alphas(p["params_json"])
            if a is not None:
                counts[a] = counts.get(a, 0) + 1
        alphas = max(counts, key=lambda k: (counts[k], k)) if counts else None
        samples, pending, unscorable = {}, 0, 0
        for p in rows:
            if _alphas(p["params_json"]) != alphas or alphas is None:
                unscorable += 1
                continue
            q = quotes.get(p["code"], {})
            if p["target"] not in q:
                pending += 1
                continue
            lab = _label(q.get(p["as_of"]), q.get(p["target"]))
            if lab is None:
                unscorable += 1
                continue
            yl, yh = lab
            y_low, y_high = dec(p["y_low"]), dec(p["y_high"])
            samples[(p["code"], p["as_of"])] = {
                "code": p["code"], "as_of": p["as_of"], "target": p["target"], "y_low": y_low, "y_high": y_high, "act_low": yl, "act_high": yh,
                "breach_low": yl < y_low, "breach_high": yh > y_high, "width": y_high - y_low,
                "pin_low": _pin(yl, y_low, alphas[0]), "pin_high": _pin(yh, y_high, alphas[1])}
        out[fam] = {"rows": len(rows), "samples": samples, "pending": pending, "unscorable": unscorable, "alphas": alphas}
    return out


def _stats(samples: list[dict]) -> dict:
    n = len(samples)
    if n == 0:
        return {"n": 0}
    pl = sum((s["pin_low"] for s in samples), ZERO)
    ph = sum((s["pin_high"] for s in samples), ZERO)
    return {"n": n, "breach_low": sum(s["breach_low"] for s in samples), "breach_high": sum(s["breach_high"] for s in samples),
            "width": sum((s["width"] for s in samples), ZERO) / n, "pinball": (pl + ph) / n, "pin_low": pl / n, "pin_high": ph / n}


def _cells(st: dict, enough: bool, present: bool) -> dict:
    if not present:
        return {k: C.na_cell("该模型没有此来源的预测") for k in ("cover_low", "cover_high", "width", "pinball")}
    if not enough:
        return {k: C.na_cell(f"样本 {st.get('n', 0)} < {MIN_N}", label="不足") for k in ("cover_low", "cover_high", "width", "pinball")}
    n = st["n"]
    return {"cover_low": _val_cell(_pct_text(Decimal(st["breach_low"]) / n, 1), Decimal(st["breach_low"]) / n, f"{st['breach_low']} / {n}"),
            "cover_high": _val_cell(_pct_text(Decimal(st["breach_high"]) / n, 1), Decimal(st["breach_high"]) / n, f"{st['breach_high']} / {n}"),
            "width": _val_cell(_pct_text(st["width"]), st["width"], "区间宽度 = y_high − y_low，相对 T 日收盘价"),
            "pinball": _val_cell(_loss_text(st["pinball"]), st["pinball"], "低侧 + 高侧 pinball 的样本均值，占收盘价比例")}


def _improvement(base: dict, cand: dict) -> Decimal | None:
    if not base.get("n") or not cand.get("n") or base["pinball"] == 0:
        return None
    return (base["pinball"] - cand["pinball"]) / base["pinball"]


def _imp_cell(v: Decimal | None, enough: bool, reason: str = "") -> dict:
    if not enough:
        return C.na_cell(reason or f"样本 < {MIN_N}", label="不足")
    if v is None:
        return C.na_cell(reason or "缺少一个模型或基线损失为 0")
    return _val_cell(("+" if v > 0 else "") + _pct_text(v, 1), v, "(基线 − LightGBM+CQR) / 基线；正＝LightGBM+CQR 损失更小")


def _compare(per: dict, codes: list[str]) -> dict:
    present = {f: bool(per[f]["rows"]) for f in FAMILIES}
    both = present["baseline"] and present["lgbm"]
    keyset = (per["baseline"]["samples"].keys() & per["lgbm"]["samples"].keys()) if both else None

    def pick(fam: str, code: str | None) -> list[dict]:
        ss = per[fam]["samples"]
        keys = keyset if keyset is not None else ss.keys()
        return [ss[k] for k in sorted(keys) if code is None or k[0] == code]

    rows, per_name = [], {}
    included: list[str] = []
    for code in codes:
        sb, sl = _stats(pick("baseline", code)), _stats(pick("lgbm", code))
        n = (sb.get("n", 0) if both else max(sb.get("n", 0), sl.get("n", 0)))
        enough = n >= MIN_N
        row = {"code": code, "n": n, "enough": enough, "baseline": _cells(sb, enough, present["baseline"]), "lgbm": _cells(sl, enough, present["lgbm"])}
        imp = _improvement(sb, sl) if both else None
        row["improvement"] = _imp_cell(imp, enough, "" if both else "缺少一个模型")
        rows.append(row)
        if enough and both and imp is not None:
            per_name[code] = imp
            included.append(code)
    # 汇总（只含样本充足的标的）：覆盖率/宽度/pinball 按样本合并；改善按「等权平均各标的改善」（门槛口径）
    pool_b = [s for c in included for s in pick("baseline", c)] if both else [s for c in codes for s in pick("baseline", c)]
    pool_l = [s for c in included for s in pick("lgbm", c)] if both else [s for c in codes for s in pick("lgbm", c)]
    sb, sl = _stats(pool_b), _stats(pool_l)
    n_sum = max(sb.get("n", 0), sl.get("n", 0))
    enough = n_sum >= MIN_N and (bool(included) if both else True)
    mean = sum(per_name.values(), ZERO) / len(per_name) if per_name else None
    summary = {"code": f"汇总（{len(included)} 个标的）" if both else "汇总", "n": n_sum, "enough": enough,
               "baseline": _cells(sb, enough, present["baseline"]), "lgbm": _cells(sl, enough, present["lgbm"]),
               "improvement": _imp_cell(mean, enough and mean is not None, "缺少一个模型或无样本充足的标的")}
    return {"rows": rows, "summary": summary, "gate": _gate(per_name, both, [c for c in codes if c not in included])}


def _gate(per_name: dict[str, Decimal], both: bool, excluded: list[str]) -> dict:
    n = len(per_name)
    base = {"excluded": excluded, "disclaimer": "仅作描述：对比窗口已被反复查看，且样本若为事后重建则不是前向证据，不触发任何晋级。"}
    if not both or n == 0:
        return {**base, "available": False, "text": "不可用：需要两个模型都有样本，且至少一个标的样本 ≥ %d。" % MIN_N}
    need = -(-2 * n // 3)
    mean = sum(per_name.values(), ZERO) / n
    improved = sum(v > 0 for v in per_name.values())
    ok = mean >= GATE_MEAN and improved >= need
    verdict = "达到" if ok else "未达到"
    text = (f"{verdict} V1 门槛的数值条件（均值改善 {_pct_text(mean, 1)}、{improved}/{n} 个标的改善；"
            f"门槛：均值 ≥ 5% 且 ≥ ⌈2n/3⌉ = {need} 个标的改善）")
    return {**base, "available": True, "pass": ok, "mean_improvement": to_db(mean), "names_improved": improved, "n_names": n, "names_needed": need, "text": text}


# ------------------------------------------------------------------ 图表
def _chart(code: str, window: int, per: dict, preds: list[dict], quotes: dict, tag: str) -> dict | None:
    q = quotes.get(code, {})
    days = sorted(q)[-window:]
    if not days:
        return None
    by_target: dict[str, dict[str, dict]] = {f: {} for f in FAMILIES}
    for p in preds:                                   # 同一目标日多条时取 as_of 最新者
        if p["code"] == code and p["tag"] == tag and p["family"]:
            cur = by_target[p["family"]].get(p["target"])
            if cur is None or p["as_of"] > cur["as_of"]:
                by_target[p["family"]][p["target"]] = p
    actual, prev_close = [], None
    models = {f: [] for f in FAMILIES}
    for d in days:
        r = q[d]
        close = dec(r["close"])
        # 先用「前一个交易日」判涨跌；窗口首日用全历史里更早的一天
        if prev_close is None:
            earlier = [x for x in q if x < d]
            prev_close = dec(q[max(earlier)]["close"]) if earlier else None
        direction = C.direction(close - prev_close) if prev_close is not None else None
        actual.append({"low": to_db(dec(r["low"])), "high": to_db(dec(r["high"])), "close": to_db(close), "dir": direction})
        prev_close = close
        for f in FAMILIES:
            p = by_target[f].get(d)
            if p is None:
                models[f].append(None)
                continue
            s = per[f]["samples"].get((code, p["as_of"]))
            models[f].append({"low": p["low_price"], "high": p["high_price"],
                              "breach_low": None if s is None else s["breach_low"], "breach_high": None if s is None else s["breach_high"]})
    ccy = _ccy(code)
    return {"code": code, "currency": ccy, "dates": days, "actual": actual,
            "models": [{"role": f, "name": FAMILY_LABEL[f], "points": models[f]} for f in FAMILIES],
            "note": ("竖条＝实际日内最低到最高（红涨绿跌按收盘价较前收）；带＝模型对该日的预测区间（预测价位，原始价口径）；"
                     "空心圈＝实际低点跌破预测低点或实际高点突破预测高点（按复权比例判定，与表格口径一致）。价位单位：" + ccy + "。")}


def _ccy(code: str) -> str:
    try:
        return currency_of(code)
    except (CodeError, KeyError):
        return ""


# ------------------------------------------------------------------ 最新预测（密封）
def _is_revealed(conn, code: str, target: str, now) -> bool:
    try:
        market = market_of(code)
    except CodeError:
        return False
    batches = [r["batch_id"] for r in conn.execute("SELECT DISTINCT batch_id FROM intent_exposure WHERE market=? AND target_session=?", (market, target))]
    return any(sealing.reveal_info(conn, b, market, target, now) is not None for b in batches)


def _settled(quotes: dict, code: str, target: str, now) -> bool:
    r = quotes.get(code, {}).get(target)
    return r is not None and ensure_utc(r["received_at"]) <= ensure_utc(now)


def _level_cells(p: dict | None, base_close: Decimal | None, ccy: str, why_missing: str) -> dict:
    if p is None:
        return {"low": C.na_cell(why_missing), "high": C.na_cell(why_missing)}
    out = {}
    for key, field in (("low", "low_price"), ("high", "high_price")):
        px = dec(p[field])
        cell = dict(C.price_cell(px, ccy), text=f"{C.fmt_decimal(px, 2)} {ccy}")             # 展示 2 位小数；v 保留留档精度
        if base_close is not None and base_close > 0:
            rel = px / base_close - ONE
            cell = dict(cell, rel=C.pct_cell(rel, colored=True))
        else:
            cell = dict(cell, rel=C.na_cell("缺少收盘价"))
        out[key] = cell
    return out


def _latest_row(conn, code: str, ps: list[dict], q: dict, quotes: dict, now, *, as_of: str, kind: str, base_day: str | None,
                base_close: Decimal | None, lag: bool, forward_targets: set[tuple[str, str]]) -> dict:
    ccy = _ccy(code)
    at = [p for p in ps if p["as_of"] == as_of]
    target = max(p["target"] for p in at)
    chosen = {}
    for f in FAMILIES:
        cand = sorted((p for p in at if p["family"] == f), key=lambda p: p["generated_at"])
        own_latest = max((x["as_of"] for x in ps if x["family"] == f), default=None)
        why = "该模型没有此 as_of 的预测" + (f"（最近为 {own_latest}）" if own_latest else "")
        chosen[f] = (cand[-1] if cand else None, why)
    settled = _settled(quotes, code, target, now)
    # 密封只针对「前向」预测（它们进入记分牌/人类计划对照）；事后重建（rebuilt）的预测不属于任何前向样本，不密封（负责人 2026-10-05 决定）。
    # 一旦该目标日有 forward 预测（任何版本，不受「只展示最近版本」的过滤影响），仍按 §6A.2 密封。
    rebuilt_only = all(p["tag"] == "rebuilt" for p in at) and (code, target) not in forward_targets
    sealed = not (settled or rebuilt_only or _is_revealed(conn, code, target, now))
    out = {"code": code, "kind": kind, "as_of": as_of, "target": target, "currency": ccy, "sealed": sealed,
           "sources": sorted({p["tag"] for p in at}), "base_date": base_day,
           "base_close": C.price_cell(base_close, ccy) if base_close is not None else C.na_cell("缺少收盘价"), "lag": lag and kind == "latest",
           "pred_lag": kind == "latest" and base_day is not None and as_of < base_day}
    if sealed:
        out["status"] = "已密封"
        out["status_title"] = f"目标日 {target} 尚无终值日线且没有揭示记录：区间与价位在目标日结束前不显示（实施方案 §6A.2）"
        out["models"] = {f: None for f in FAMILIES}
        out["actual"] = None
        return out
    out["status"] = "已结算" if settled else ("事后重建（未密封）" if rebuilt_only and not _is_revealed(conn, code, target, now) else "已揭示")
    out["status_title"] = ""
    out["models"] = {f: _level_cells(chosen[f][0], base_close, ccy, chosen[f][1]) for f in FAMILIES}
    tq = q.get(target) if settled else None
    out["actual"] = {"low": C.price_cell(dec(tq["low"]), ccy), "high": C.price_cell(dec(tq["high"]), ccy)} if tq else None
    return out


def _latest_rows(conn, preds: list[dict], quotes: dict, now, forward_targets: set[tuple[str, str]]) -> list[dict]:
    by_code: dict[str, list[dict]] = {}
    for p in preds:
        if p["family"]:
            by_code.setdefault(p["code"], []).append(p)
    rows = []
    for code in sorted(by_code):
        ps = by_code[code]
        q = quotes.get(code, {})
        last_day = max(q) if q else None
        last_close = dec(q[last_day]["close"]) if last_day else None
        try:
            exp = expected_session(market_of(code), now)
        except CodeError:
            exp = None
        lag = last_day is not None and exp is not None and last_day < exp.isoformat()
        latest = _latest_row(conn, code, ps, q, quotes, now, as_of=max(p["as_of"] for p in ps), kind="latest", base_day=last_day,
                             base_close=last_close, lag=bool(lag), forward_targets=forward_targets)
        rows.append(latest)
        if latest["sealed"]:
            done = sorted({p["as_of"] for p in ps if _settled(quotes, code, p["target"], now)})
            if done:                                         # 最近一个目标日已结束的预测（相对「当时」的收盘价）
                a = done[-1]
                qa = q.get(a)
                rows.append(_latest_row(conn, code, ps, q, quotes, now, as_of=a, kind="last_settled", base_day=a if qa else None,
                                        base_close=dec(qa["close"]) if qa else None, lag=False, forward_targets=forward_targets))
    return rows


# ------------------------------------------------------------------ 入口
def _universe_codes(path) -> set[str]:
    if not path:
        return set()
    try:
        from mystock2.instruments.universe import load_universe
        return {e.code for e in load_universe(path).entries}
    except Exception:                                      # noqa: BLE001 — 名单只用于过滤展示，读不到就不过滤
        return set()


def run(conn, params):
    now = C.now_of(params)
    tag = params.get("source") or "rebuilt"
    window = int(params.get("window") or 120)

    all_preds = _load_predictions(conn)
    in_universe = _universe_codes(params.get("_universe_path"))
    if in_universe:                                       # 名单变更后，已留档但不在当前名单内的标的不再展示（留档本身不删）
        all_preds = [p for p in all_preds if p["code"] in in_universe]
    if not all_preds:
        raise C.ViewUnavailable("no_predictions", "还没有任何预测版本留档：预测由受控 CLI 生成（Web 只读，不训练、不预测）")
    # 密封与来源计数必须看**全部**版本：只展示最近版本的过滤若先于它们，前向预测会被滤掉（审核 P0-2）
    forward_targets = {(p["code"], p["target"]) for p in all_preds if p["tag"] == "forward"}
    chosen, version_notes = _pick_versions(all_preds)
    preds = [p for p in all_preds if p["family"] and chosen.get(p["family"]) == p["mv"]]
    codes_all = sorted({p["code"] for p in preds})
    quotes = _load_quotes(conn, codes_all)
    per = _build_samples(preds, quotes, tag)

    # ---- 来源说明
    prov = []
    for t in ("rebuilt", "forward"):
        ps = [p for p in preds if p["tag"] == t]
        prov.append({"tag": t, "label": TAG_LABEL[t], "count": len(ps), "first_as_of": min((p["as_of"] for p in ps), default=None),
                     "last_as_of": max((p["as_of"] for p in ps), default=None), "codes": len({p["code"] for p in ps})})
    forward_n = next(x["count"] for x in prov if x["tag"] == "forward")
    forward_all = sum(1 for p in all_preds if p["family"] and p["tag"] == "forward")
    rebuilt_n = next(x["count"] for x in prov if x["tag"] == "rebuilt")
    warnings = list(version_notes)
    if tag == "rebuilt" or rebuilt_n:
        warnings.append(REBUILT_WARNING)
    if forward_all > forward_n:                          # 前向预测属于未展示的旧版本：照实说明，不能报成 0
        warnings.append(f"另有 {forward_all - forward_n} 条前向（forward）预测属于未展示的模型版本（见上方版本说明）；它们仍按 §6A.2 密封。")
    if forward_all == 0:
        warnings.append("前向（forward）预测样本数为 0：目前没有任何可作为晋级证据的前向样本。")

    # ---- 模型元数据
    models = []
    for f in FAMILIES:
        info = per[f]
        a = info["alphas"]
        models.append({"role": f, "label": FAMILY_LABEL[f], "version": chosen.get(f) or C.UNAVAILABLE, "predictions": info["rows"],
                       "scored": len(info["samples"]), "pending": info["pending"], "unscorable": info["unscorable"],
                       "alpha_low": None if a is None else to_db(a[0]), "alpha_high": None if a is None else to_db(a[1]),
                       "target_low": C.na_cell("缺少分位参数") if a is None else _val_cell(_pct_text(a[0], 1), a[0]),
                       "target_high": C.na_cell("缺少分位参数") if a is None else _val_cell(_pct_text(ONE - a[1], 1), ONE - a[1])})
        if info["unscorable"]:
            warnings.append(f"{FAMILY_LABEL[f]}：{info['unscorable']} 条预测因缺实际值、复权价或分位参数与多数不一致而未计入")

    codes_tag = sorted({p["code"] for p in preds if p["tag"] == tag})
    compare = _compare(per, codes_tag)
    if len({len(per[f]["samples"]) for f in FAMILIES}) > 1 and all(per[f]["rows"] for f in FAMILIES):
        warnings.append("两个模型的预测起点不同：对比只在两个模型都有预测且目标日已有实际值的样本上做（共同样本）。")

    # ---- 图表标的
    sym = (params.get("symbol") or "").strip()
    counts = {c: sum(1 for f in FAMILIES for k in per[f]["samples"] if k[0] == c) for c in codes_all}
    if sym and sym not in codes_all:
        warnings.append(f"标的 {sym} 没有预测：已改用默认标的")
        sym = ""
    if not sym:
        sym = max(codes_tag, key=lambda c: (counts.get(c, 0), c), default="") if codes_tag else (codes_all[0] if codes_all else "")
    chart = _chart(sym, window, per, preds, quotes, tag) if sym else None
    if chart is not None and not codes_tag:
        chart = None

    latest = _latest_rows(conn, preds, quotes, now, forward_targets)

    # ---- 新鲜度
    last_pred = max(preds, key=lambda p: (p["as_of"], p["generated_at"])) if preds else None
    q_latest = None
    for c in codes_all:
        for d, r in quotes[c].items():
            if q_latest is None or (d, r["event_at"]) > (q_latest[0], q_latest[1]["event_at"]):
                q_latest = (d, r)
    srcs = [C.source("预测版本（generated_at；重建则为重建时刻）", last_pred["cutoff"] if last_pred else None, last_pred["generated_at"] if last_pred else None),
            C.source("日线行情", q_latest[1]["event_at"] if q_latest else None, q_latest[1]["received_at"] if q_latest else None)]
    lag_codes = [r["code"] for r in latest if r.get("lag")]
    fresh_notes = []
    pred_lag = [f"{r['code']}（as_of {r['as_of']} < 行情 {r['base_date']}）" for r in latest if r.get("pred_lag")]
    if pred_lag:
        fresh_notes.append("预测落后于行情（还没有以最新行情日为 as_of 的预测，不用旧预测冒充当前预测）：" + "、".join(pred_lag))
    if lag_codes:
        fresh_notes.append("行情落后于应有的最近收盘日：" + "、".join(lag_codes))
    fresh = {"latest_prediction": None if last_pred is None else {"as_of": last_pred["as_of"], "generated_at": last_pred["generated_at"], "source": last_pred["tag"]},
             "latest_quote_date": q_latest[0] if q_latest else None, "latest_quote_received_at": q_latest[1]["received_at"] if q_latest else None}

    return {
        "banner": BANNER, "source": tag, "source_label": TAG_LABEL[tag], "provenance": prov, "forward_count": forward_n, "forward_count_all_versions": forward_all, "rebuilt_count": rebuilt_n,
        "models": models, "min_n": MIN_N, "compare": compare, "codes": codes_all, "symbol": sym, "window": window, "chart": chart, "latest": latest,
        "freshness": fresh, "warnings": warnings,
        "_freshness": C.freshness(srcs, notes=fresh_notes + ([REBUILT_WARNING] if tag == "rebuilt" else [])),
    }
