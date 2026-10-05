"""视图公共约定（实施方案 §5 WP3.2）：金额格式、红涨绿跌标记、不可用单元、新鲜度头部、账户解析。

约定（框架强制，视图通过这里的构造函数遵守）：
- 金额一律 Decimal，展示时带币种；**币种之间不相加**（这里没有任何跨币种求和函数）。
- 涨跌用 `dir`：`up`（涨/盈）、`down`（跌/亏）、`flat`；前端 CSS 红涨绿跌。**汇率与现金流不带 `dir`**（中性色）。
- 缺失显示「不可用」/「未知」，不记零：`na_cell` 的 `v` 为 None，`text` 为「不可用」。
- 每个视图自带新鲜度头部：数据模式、事件时间、采集时间、陈旧度；任何时间未知则陈旧度为「未知」，绝不显示「新鲜」。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Iterable

from mystock2.core.money import dec, to_db
from mystock2.core.timeutil import TimeError, ensure_utc, iso_utc, utc_now

UNAVAILABLE = "不可用"
UNKNOWN = "未知"
DATA_MODES = {"daily": "日线", "cached": "定时缓存", "realtime": "近实时"}
SUPPORTED_CCYS = ("USD", "HKD", "CNY")


class ViewUnavailable(Exception):
    """数据缺失等业务状态（不是程序错误）：框架转成 status=unavailable 的 JSON，页面显示原因而不是崩溃。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def now_of(params: dict) -> datetime:
    """框架注入的当前时间（便于测试固定时钟）；直接调用 run 时回退到系统时间。"""
    return params.get("_now") or utc_now()


# ------------------------------------------------------------------ 数值格式
def _group(int_part: str) -> str:
    out, n = [], len(int_part)
    for i, ch in enumerate(int_part):
        if i and (n - i) % 3 == 0:
            out.append(",")
        out.append(ch)
    return "".join(out)


def fmt_decimal(value: Decimal | str, dp: int = 2, *, trim: bool = False, sign: bool = False) -> str:
    """千分位十进制文本；按 dp 位四舍六入五成双（银行家舍入）；trim=True 时去掉多余的尾随 0（保留至少 min 位见 fmt_price）。"""
    d = dec(value)
    with localcontext() as ctx:
        ctx.prec = 60
        q = d.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_EVEN)
    neg = q < 0
    text = format(abs(q), "f")
    int_part, _, frac = text.partition(".")
    if trim:
        frac = frac.rstrip("0")
    s = _group(int_part) + ("." + frac if frac else "")
    if neg:
        return "-" + s
    return ("+" + s) if sign and q > 0 else s


def fmt_money(value: Decimal | str, ccy: str, *, dp: int = 2, sign: bool = False) -> str:
    """金额文本：始终带币种，如 `1,234.50 USD`、`-80.00 HKD`。"""
    return f"{fmt_decimal(value, dp, sign=sign)} {ccy.upper()}"


def fmt_qty(value: Decimal | str) -> str:
    return fmt_decimal(value, 4, trim=True)


def fmt_price(value: Decimal | str) -> str:
    """价格：至少 2 位小数、至多 4 位。"""
    d = dec(value)
    text = fmt_decimal(d, 4, trim=True)
    int_part, _, frac = text.partition(".")
    return int_part + "." + (frac + "00")[: max(2, len(frac))]


def fmt_pct(value: Decimal | str, dp: int = 2, *, sign: bool = False) -> str:
    return fmt_decimal(dec(value) * 100, dp, sign=sign) + "%"


def direction(value: Decimal | str | None) -> str | None:
    """涨跌方向：正为 up、负为 down、零为 flat；None 返回 None（不着色）。"""
    if value is None:
        return None
    d = dec(value)
    return "up" if d > 0 else "down" if d < 0 else "flat"


# ------------------------------------------------------------------ 单元（前端按此渲染）
def _cell(text: str, **kw: Any) -> dict:
    c: dict[str, Any] = {"text": text}
    c.update({k: v for k, v in kw.items() if v is not None})
    return c


def text_cell(text: str, *, tag: str | None = None, title: str | None = None) -> dict:
    return _cell(text, tag=tag, title=title)


def na_cell(reason: str | None = None, *, label: str = UNAVAILABLE) -> dict:
    """缺失值：v 为 None，不是 0。"""
    c: dict[str, Any] = {"text": label, "v": None, "na": True}
    if reason:
        c["title"] = reason
    return c


def money_cell(value: Decimal | str | None, ccy: str, *, colored: bool = False, sign: bool = False, tag: str | None = None,
               title: str | None = None, reason: str | None = None) -> dict:
    """金额单元：`colored=True` 时按涨跌着色（红涨绿跌），否则中性（现金、汇率、净现金流不着色）。"""
    if value is None:
        return na_cell(reason)
    d = dec(value)
    return _cell(fmt_money(d, ccy, sign=sign), v=to_db(d), ccy=ccy.upper(), dir=direction(d) if colored else None, tag=tag, title=title)


def qty_cell(value: Decimal | str | None, *, reason: str | None = None) -> dict:
    if value is None:
        return na_cell(reason)
    return _cell(fmt_qty(value), v=to_db(value))


def price_cell(value: Decimal | str | None, ccy: str | None = None, *, tag: str | None = None, title: str | None = None,
               reason: str | None = None) -> dict:
    if value is None:
        return na_cell(reason)
    text = fmt_price(value) + (f" {ccy.upper()}" if ccy else "")
    return _cell(text, v=to_db(value), ccy=ccy.upper() if ccy else None, tag=tag, title=title)


def pct_cell(value: Decimal | str | None, *, colored: bool = False, reason: str | None = None) -> dict:
    if value is None:
        return na_cell(reason)
    return _cell(fmt_pct(value, sign=colored), v=to_db(value), dir=direction(value) if colored else None)


def rate_cell(value: Decimal | str | None, *, reason: str | None = None) -> dict:
    """汇率单元：永远中性色（不带 dir）。"""
    if value is None:
        return na_cell(reason)
    return _cell(fmt_decimal(value, 6, trim=True), v=to_db(value), fx=True)


# ------------------------------------------------------------------ 新鲜度
def source(name: str, event_at: Any, collected_at: Any) -> dict:
    """新鲜度来源条目：event_at＝数据反映的时刻；collected_at＝系统收到/采集的时刻；任一为 None 表示未知。"""
    return {"name": name, "event_at": _iso_or_none(event_at), "collected_at": _iso_or_none(collected_at)}


def freshness(sources: Iterable[dict], notes: Iterable[str] = ()) -> dict:
    """视图 run 的返回值里放在 `_freshness` 键下；框架据此生成头部。"""
    return {"sources": list(sources), "notes": list(notes)}


def _iso_or_none(v: Any) -> str | None:
    if v is None or v == "":
        return None
    try:
        return iso_utc(v)
    except TimeError:
        return None


def _age_text(seconds: float) -> str:
    s = int(abs(seconds))
    if s < 90:
        return f"{s} 秒"
    if s < 5400:
        return f"{round(s / 60)} 分钟"
    if s < 36 * 3600:
        return f"{round(s / 3600)} 小时"
    return f"{round(s / 86400)} 天"


def build_header(data_mode: str, fresh: dict | None, now: datetime, stale_after_hours: float, *, extra_notes: Iterable[str] = ()) -> dict:
    """把视图提供的来源时间折算成头部。规则：任一来源缺事件时间或采集时间 → 整体「未知」；
    超过阈值 → 「陈旧」；全部已知且在阈值内 → 「新鲜」。无来源条目也是「未知」。"""
    now = ensure_utc(now)
    fresh = fresh or {}
    notes = list(fresh.get("notes", [])) + list(extra_notes)
    entries = []
    labels = []
    ev_known, col_known = [], []
    for s in fresh.get("sources", []):
        ev, col = _iso_or_none(s.get("event_at")), _iso_or_none(s.get("collected_at"))
        seconds = None
        if col is not None:
            seconds = (now - ensure_utc(col)).total_seconds()
        if ev is None or col is None:
            label, text = UNKNOWN, f"{UNKNOWN}（缺少{'事件时间' if ev is None else '采集时间'}）"
        elif seconds is not None and seconds < -300:
            label, text = UNKNOWN, f"{UNKNOWN}（采集时间晚于当前时间，时钟异常）"
        elif seconds is not None and seconds > stale_after_hours * 3600:
            label, text = "陈旧", f"陈旧（距采集已 {_age_text(seconds)}）"
        else:
            label, text = "新鲜", f"新鲜（{_age_text(max(seconds or 0, 0))}前采集）"
        if ev:
            ev_known.append(ev)
        if col:
            col_known.append(col)
        labels.append(label)
        entries.append({"name": s.get("name", ""), "event_at": ev, "collected_at": col, "staleness": label, "text": text,
                        "seconds": None if seconds is None else int(seconds)})
    if not entries:
        overall, text = UNKNOWN, f"{UNKNOWN}（视图未提供时间信息）"
    elif UNKNOWN in labels:
        overall, text = UNKNOWN, f"{UNKNOWN}（" + "；".join(f"{e['name']}：{e['text']}" for e in entries if e["staleness"] == UNKNOWN) + "）"
    elif "陈旧" in labels:
        overall, text = "陈旧", "；".join(f"{e['name']}：{e['text']}" for e in entries if e["staleness"] == "陈旧")
    else:
        overall = "新鲜"
        text = entries[0]["text"] if len(entries) == 1 else "；".join(f"{e['name']}：{e['text']}" for e in entries)
    oldest_col = min(col_known) if col_known else None
    return {
        "data_mode": data_mode, "data_mode_label": DATA_MODES.get(data_mode, UNKNOWN),
        # 综合时间取最旧的已知来源（最弱一环）
        "event_at": min(ev_known) if ev_known else None, "collected_at": oldest_col,
        "staleness": {"label": overall, "text": text,
                      "seconds": None if oldest_col is None else int((now - ensure_utc(oldest_col)).total_seconds())},
        "sources": entries, "stale_after_hours": stale_after_hours, "generated_at": iso_utc(now), "notes": notes,
    }


# ------------------------------------------------------------------ 账户解析与账本来源时间
def list_accounts(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT account_id, broker, trd_env, base_ccy, note FROM account ORDER BY account_id")]


def resolve_account(conn: sqlite3.Connection, params: dict) -> tuple[dict, list[dict]]:
    accts = list_accounts(conn)
    if not accts:
        raise ViewUnavailable("no_account", "账本中还没有账户：尚未采集或导入（采集/导入由受控 CLI 写入，Web 只读）")
    want = (params.get("account") or "").strip()
    if not want:
        return accts[0], accts
    for a in accts:
        if a["account_id"] == want:
            return a, accts
    raise ViewUnavailable("account_not_found", f"账户不存在：{want}")


def ledger_source(conn: sqlite3.Connection, account_id: str) -> dict:
    r = conn.execute("SELECT MAX(event_at) AS e, MAX(received_at) AS r FROM ledger_event WHERE account_id=?", (account_id,)).fetchone()
    return source("账本事件", r["e"], r["r"])


def latest_snapshot(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    # 只取带真实采集时刻的券商快照：V1 日快照（source='v1-date-only'）的 captured_at 只是占位，不能用于对账/成本
    return conn.execute("SELECT * FROM account_snapshot WHERE account_id=? AND source!='v1-date-only' ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1",
                        (account_id,)).fetchone()


def snapshot_source(snap: sqlite3.Row | None) -> dict:
    if snap is None:
        return source("券商快照", None, None)
    return source("券商快照", snap["captured_at"], snap["captured_at"])
