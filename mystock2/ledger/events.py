"""规范业务事件的校验与写入（实施方案 §5 M2a、§6 账本不变量）。

要点：
- 规范键 `business_key` 跨版本、跨来源稳定；来源证据（source_record）与事件多对多关联。
- 幂等：同键同版本同内容的重复到达是 no-op（只补证据链接）；内容冲突报 `LedgerConflict`，调用方应转入待匹配队列。
- 更正＝原子地追加 REVERSAL（取反**当前有效版本**）＋新版本；同一旧版本不得被冲销两次（库层唯一索引 + 代码检查）。
- 多腿事件（FX）原子写入；缺一腿不生效。
- 撤单/失败/未成交不产生账本事件（由采集层保证，不在此建模）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from mystock2.core.db import atomic
from mystock2.core.money import MoneyError, dec, to_db
from mystock2.core.timeutil import TimeError, iso_utc, utc_now
from mystock2.instruments.code_map import CodeError, currency_of, market_of

EVENT_TYPES = (
    "OPENING_POSITION", "OPENING_CASH", "FILL", "FEE", "DIVIDEND_ACCRUAL", "DIVIDEND_PAYMENT",
    "DIVIDEND_SHORTFALL", "DEPOSIT", "WITHDRAW", "FX", "INTEREST", "TAX", "ADJUST", "REVERSAL",
)
ADJUST_CLASSES = ("EXTERNAL_FLOW", "INVESTMENT", "OTHER")
FILL_CASH_TOLERANCE = Decimal("0.01")   # 券商对成交额按币种最小单位舍入


class LedgerError(ValueError):
    pass


class LedgerConflict(LedgerError):
    """同一规范键与版本已存在但内容不同。"""


class IdentityInsufficient(LedgerError):
    """来源记录缺少构成规范键所需的身份信息：应转入待匹配队列，而不是入账。"""


@dataclass(frozen=True)
class EventDraft:
    business_key: str
    account_id: str
    event_type: str
    event_at: object            # datetime | str（带时区）
    currency: str
    market: str | None = None
    code: str | None = None
    price: str | None = None
    qty_delta: str = "0"
    cash_delta: str = "0"
    recv_delta: str = "0"
    attrib_amount: str | None = None
    ref_deal_id: str | None = None
    ref_order_id: str | None = None
    ref_event_key: str | None = None
    group_id: str | None = None
    leg_id: str | None = None
    adjust_class: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class SourceDraft:
    source: str                  # futu | v1 | csv | manual
    source_id: str
    payload: dict = field(default_factory=dict)   # 已脱敏的原始字段，用于计算内容哈希
    raw_ref: str | None = None


@dataclass(frozen=True)
class PostResult:
    event_id: str
    status: str                  # inserted | duplicate


# ------------------------------------------------------------------ 业务键
def fill_key(account_id: str, deal_id: str | None) -> str:
    if not deal_id:
        raise IdentityInsufficient("成交缺少 deal_id，无法构成规范键，进入待匹配")
    return f"fill:{account_id}:{deal_id}"


def fee_key(account_id: str, deal_id: str | None, kind: str) -> str:
    if not deal_id:
        raise IdentityInsufficient("费用缺少所属成交 deal_id")
    return f"fee:{account_id}:{deal_id}:{kind}"


def flow_key(account_id: str, flow_id: str | None, kind: str) -> str:
    if not flow_id:
        raise IdentityInsufficient("资金流水缺少流水号")
    return f"{kind.lower()}:{account_id}:{flow_id}"


# ------------------------------------------------------------------ 校验
def _num(name: str, v: str | None) -> Decimal:
    try:
        return dec(v if v is not None else "0")
    except MoneyError as exc:
        raise LedgerError(f"{name} 非法：{exc}") from exc


def validate(d: EventDraft) -> dict:
    """校验并返回规范化字段字典（金额为规范十进制字符串，时间为 UTC 文本）。"""
    try:
        return _validate(d)
    except (TimeError, MoneyError) as exc:
        raise LedgerError(str(exc)) from exc


def _validate(d: EventDraft) -> dict:
    if d.event_type not in EVENT_TYPES or d.event_type == "REVERSAL":
        raise LedgerError(f"不允许直接写入的事件类型：{d.event_type!r}")
    if not d.business_key or not d.account_id:
        raise LedgerError("business_key 与 account_id 必填")
    cur = (d.currency or "").upper()
    if len(cur) != 3 or not cur.isalpha():
        raise LedgerError(f"币种非法：{d.currency!r}")
    qty, cash, recv = _num("qty_delta", d.qty_delta), _num("cash_delta", d.cash_delta), _num("recv_delta", d.recv_delta)
    t = d.event_type
    needs_code = t in ("OPENING_POSITION", "FILL", "DIVIDEND_ACCRUAL", "DIVIDEND_PAYMENT", "DIVIDEND_SHORTFALL")
    if needs_code:
        try:
            m = market_of(d.code or "")
        except CodeError as exc:
            raise LedgerError(str(exc)) from exc
        if d.market and d.market != m:
            raise LedgerError(f"market 与 code 不一致：{d.market} vs {d.code}")
    elif d.code:
        raise LedgerError(f"{t} 不应带 code")

    if t == "OPENING_POSITION":
        if not (qty > 0 and cash == 0 and recv == 0):
            raise LedgerError("OPENING_POSITION 需 qty>0 且无现金/应收")
    elif t == "OPENING_CASH":
        if not (qty == 0 and recv == 0):
            raise LedgerError("OPENING_CASH 只含现金")
    elif t == "FILL":
        if qty == 0 or recv != 0:
            raise LedgerError("FILL 的 qty 不能为 0，且无应收")
        if d.price is None or _num("price", d.price) <= 0:
            raise LedgerError("FILL 必须带正的成交价")
        if cur != currency_of(d.code):
            raise LedgerError(f"FILL 币种 {cur} 与标的币种 {currency_of(d.code)} 不一致")
        if abs(cash + qty * _num("price", d.price)) > FILL_CASH_TOLERANCE:
            raise LedgerError("FILL 的 cash_delta 应等于 -(qty×price)（不含费用；容差 0.01）")
    elif t in ("FEE", "TAX"):
        if qty != 0 or recv != 0 or cash > 0:
            raise LedgerError(f"{t} 只能是现金支出（cash_delta ≤ 0），无数量/应收")
        if not (d.ref_deal_id or d.ref_event_key):
            raise LedgerError(f"{t} 必须归属某笔成交或业务事件（ref_deal_id / ref_event_key）")
    elif t == "DIVIDEND_ACCRUAL":
        if not (qty == 0 and cash == 0 and recv > 0 and d.group_id):
            raise LedgerError("DIVIDEND_ACCRUAL 需 recv_delta>0、无现金/数量，并带 group_id")
    elif t == "DIVIDEND_PAYMENT":
        if not (qty == 0 and cash > 0 and recv < 0 and d.group_id):
            raise LedgerError("DIVIDEND_PAYMENT 需 cash>0、recv<0（结清应收），并带 group_id")
    elif t == "DIVIDEND_SHORTFALL":
        if not (qty == 0 and cash == 0 and recv == 0 and d.group_id and _num("attrib_amount", d.attrib_amount) > 0):
            raise LedgerError("DIVIDEND_SHORTFALL 为非现金归因：cash/recv 必须为 0，且 attrib_amount>0、带 group_id")
    elif t == "DEPOSIT":
        if not (qty == 0 and recv == 0 and cash > 0):
            raise LedgerError("DEPOSIT 需 cash>0")
    elif t == "WITHDRAW":
        if not (qty == 0 and recv == 0 and cash < 0):
            raise LedgerError("WITHDRAW 需 cash<0")
    elif t == "FX":
        if not (d.group_id and d.leg_id and qty == 0 and recv == 0 and cash != 0):
            raise LedgerError("FX 腿需 group_id、leg_id 与非零现金")
    elif t == "INTEREST":
        if not (qty == 0 and recv == 0 and cash != 0):
            raise LedgerError("INTEREST 需非零现金")
    elif t == "ADJUST":
        if d.adjust_class not in ADJUST_CLASSES or not (d.note or "").strip():
            raise LedgerError("ADJUST 必须带 adjust_class（EXTERNAL_FLOW/INVESTMENT/OTHER）与原因说明")
        if qty != 0 or recv != 0:
            raise LedgerError("ADJUST 只能调整现金")
    if t != "ADJUST" and d.adjust_class:
        raise LedgerError("adjust_class 只用于 ADJUST")

    fields = {
        "business_key": d.business_key, "account_id": d.account_id, "event_type": t,
        "event_at": iso_utc(d.event_at), "market": market_of(d.code) if needs_code else None,
        "code": d.code if needs_code else None, "currency": cur,
        "price": to_db(d.price) if d.price is not None else None,
        "qty_delta": to_db(qty), "cash_delta": to_db(cash), "recv_delta": to_db(recv),
        "attrib_amount": to_db(d.attrib_amount) if d.attrib_amount is not None else None,
        "ref_deal_id": d.ref_deal_id, "ref_order_id": d.ref_order_id, "ref_event_key": d.ref_event_key,
        "group_id": d.group_id, "leg_id": d.leg_id, "adjust_class": d.adjust_class, "note": d.note,
    }
    return fields


def content_hash(fields: dict) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 来源证据
def record_source(conn: sqlite3.Connection, src: SourceDraft, received_at=None) -> str:
    h = hashlib.sha256(json.dumps(src.payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    sid = hashlib.sha256(f"{src.source}|{src.source_id}|{h}".encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO source_record(source_record_id, source, source_id, content_hash, raw_ref, received_at) VALUES (?,?,?,?,?,?)",
        (sid, src.source, src.source_id, h, src.raw_ref, iso_utc(received_at or utc_now())),
    )
    return sid


def _link(conn, source_record_id: str | None, event_id: str) -> None:
    if source_record_id:
        conn.execute("INSERT OR IGNORE INTO source_link(source_record_id, event_id) VALUES (?,?)", (source_record_id, event_id))


# ------------------------------------------------------------------ 写入
_COLS = (
    "event_id", "business_key", "event_version", "account_id", "event_type", "event_at", "received_at", "market", "code",
    "currency", "price", "qty_delta", "cash_delta", "recv_delta", "attrib_amount", "ref_deal_id", "ref_order_id",
    "ref_event_key", "group_id", "leg_id", "corrects_event_id", "correction_request_id", "adjust_class", "note",
    "content_hash", "created_at",
)


def _insert(conn, fields: dict, version: int, received_at, *, corrects: str | None = None, request_id: str | None = None,
            chash: str | None = None, event_type: str | None = None) -> str:
    event_id = f"{fields['business_key']}#{version}"
    row = dict(fields)
    row.update(
        event_id=event_id, event_version=version, received_at=iso_utc(received_at or utc_now()),
        corrects_event_id=corrects, correction_request_id=request_id,
        content_hash=chash or content_hash(fields), created_at=iso_utc(utc_now()),
    )
    if event_type:
        row["event_type"] = event_type
    conn.execute(f"INSERT INTO ledger_event({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})", [row[c] for c in _COLS])
    return event_id


def post_event(conn: sqlite3.Connection, draft: EventDraft, *, source: SourceDraft | None = None, received_at=None) -> PostResult:
    """幂等写入版本 1 的规范事件。"""
    fields = validate(draft)
    h = content_hash(fields)
    with atomic(conn):
        _require_account(conn, draft.account_id)
        sid = record_source(conn, source, received_at) if source else None
        event_id = f"{draft.business_key}#1"
        existing = conn.execute("SELECT content_hash FROM ledger_event WHERE event_id=?", (event_id,)).fetchone()
        if existing:
            if existing["content_hash"] != h:
                raise LedgerConflict(f"规范键 {draft.business_key} 已存在但内容不同；请转入待匹配/走更正流程")
            _link(conn, sid, event_id)
            return PostResult(event_id, "duplicate")
        # 同一业务键若已有更高版本（已被更正），不允许再写版本 1
        if conn.execute("SELECT 1 FROM ledger_event WHERE business_key=?", (draft.business_key,)).fetchone():
            raise LedgerConflict(f"规范键 {draft.business_key} 已存在其他版本")
        _insert(conn, fields, 1, received_at, chash=h)
        _link(conn, sid, event_id)
        return PostResult(event_id, "inserted")


def _require_account(conn, account_id: str) -> None:
    if not conn.execute("SELECT 1 FROM account WHERE account_id=?", (account_id,)).fetchone():
        raise LedgerError(f"账户不存在：{account_id}")


def ensure_account(conn: sqlite3.Connection, account_id: str, broker: str, trd_env: str, base_ccy: str | None = None, note: str | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO account(account_id, broker, trd_env, base_ccy, note) VALUES (?,?,?,?,?)",
        (account_id, broker, trd_env, base_ccy, note),
    )


def _versions(conn, business_key: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM ledger_event WHERE business_key=? ORDER BY event_version", (business_key,)).fetchall()


def correct_event(conn: sqlite3.Connection, business_key: str, new_draft: EventDraft | None, request_id: str, *,
                  source: SourceDraft | None = None, received_at=None) -> list[str]:
    """更正：原子地追加 REVERSAL（取反当前有效版本）＋新版本事件；new_draft=None 表示单纯取消。

    幂等键为 request_id；返回本次涉及的 event_id 列表（重复请求返回首次的结果）。
    """
    if not request_id:
        raise LedgerError("更正必须带 correction_request_id（幂等键）")
    if new_draft is not None and new_draft.business_key != business_key:
        raise LedgerError("更正后的事件必须沿用同一 business_key")
    new_fields = validate(new_draft) if new_draft is not None else None
    with atomic(conn):
        done = conn.execute("SELECT event_id FROM ledger_event WHERE correction_request_id=? ORDER BY event_version", (request_id,)).fetchall()
        if done:
            if any(r["event_id"].rsplit("#", 1)[0] != business_key for r in done):
                raise LedgerConflict("同一 correction_request_id 已用于其他业务事件")
            return [r["event_id"] for r in done]
        rows = _versions(conn, business_key)
        if not rows:
            raise LedgerError(f"没有可更正的事件：{business_key}")
        last = rows[-1]
        sid = record_source(conn, source, received_at) if source else None
        ids: list[str] = []
        version = last["event_version"]
        if last["event_type"] != "REVERSAL":
            rev = {k: last[k] for k in (
                "business_key", "account_id", "event_at", "market", "code", "currency", "price", "ref_deal_id",
                "ref_order_id", "ref_event_key", "group_id", "leg_id", "adjust_class")}
            rev.update(
                event_type="REVERSAL", qty_delta=to_db(-dec(last["qty_delta"])), cash_delta=to_db(-dec(last["cash_delta"])),
                recv_delta=to_db(-dec(last["recv_delta"])), attrib_amount=None, note=f"reverses {last['event_type']}#{last['event_version']}",
            )
            version += 1
            ids.append(_insert(conn, rev, version, received_at, corrects=last["event_id"], request_id=request_id))
        elif new_fields is None:
            raise LedgerError(f"{business_key} 已被取消")
        if new_fields is not None:
            version += 1
            ids.append(_insert(conn, new_fields, version, received_at, request_id=request_id))
        for eid in ids:
            _link(conn, sid, eid)
        return ids


def post_fx(conn: sqlite3.Connection, account_id: str, group_id: str, event_at, from_ccy: str, from_amount: str,
            to_ccy: str, to_amount: str, *, source: SourceDraft | None = None, received_at=None) -> list[PostResult]:
    """换汇：两个币种腿以 group_id 原子写入；缺一腿则整组不生效。"""
    if from_ccy.upper() == to_ccy.upper():
        raise LedgerError("换汇两腿币种必须不同")
    a, b = dec(from_amount), dec(to_amount)
    if a <= 0 or b <= 0:
        raise LedgerError("换汇金额必须为正")
    legs = [
        EventDraft(f"fx:{account_id}:{group_id}:out", account_id, "FX", event_at, from_ccy, cash_delta=to_db(-a), group_id=group_id, leg_id="out"),
        EventDraft(f"fx:{account_id}:{group_id}:in", account_id, "FX", event_at, to_ccy, cash_delta=to_db(b), group_id=group_id, leg_id="in"),
    ]
    with atomic(conn):
        return [post_event(conn, leg, source=source, received_at=received_at) for leg in legs]


def post_dividend(conn: sqlite3.Connection, account_id: str, group_id: str, code: str, ccy: str, *, accrual_at, gross: str,
                  payment_at=None, cash_received: str | None = None, withholding_tax: str | None = None,
                  source: SourceDraft | None = None) -> list[PostResult]:
    """股息：除息日形成应收**总额** G；支付日必须结清全部 G（实施方案 §6 不变量 4）。

    三种数据情形：
      ① 总额 G 与预扣税 W 分别已知：现金＋G、应收−G，W 为独立 TAX（唯一的现金扣减）；
      ② 只知净额 N 与 W：还原 G=N+W，同①（本函数接受 gross=G 与 withholding_tax=W，cash 由 G 推出）；
      ③ 只知净额 N、无拆分：现金＋N、应收−G，差额 G−N 记 DIVIDEND_SHORTFALL（非现金归因），不再扣现金。
    cash_received 仅在情形③传入（此时 withholding_tax 须为 None）。
    """
    g = dec(gross)
    if g <= 0:
        raise LedgerError("股息总额必须为正")
    market = market_of(code)
    base = f"div:{account_id}:{group_id}"
    out: list[PostResult] = []
    with atomic(conn):
        out.append(post_event(conn, EventDraft(f"{base}:accrual", account_id, "DIVIDEND_ACCRUAL", accrual_at, ccy, market=market, code=code,
                                              recv_delta=to_db(g), group_id=group_id), source=source))
        if payment_at is None:
            return out
        if cash_received is not None and withholding_tax is not None:
            raise LedgerError("情形③（只知净额）不应同时给出预扣税")
        if cash_received is not None:
            n = dec(cash_received)
            if not (0 < n <= g):
                raise LedgerError("净额必须在 (0, 总额] 内")
            out.append(post_event(conn, EventDraft(f"{base}:payment", account_id, "DIVIDEND_PAYMENT", payment_at, ccy, market=market, code=code,
                                                  cash_delta=to_db(n), recv_delta=to_db(-g), group_id=group_id), source=source))
            if n < g:
                out.append(post_event(conn, EventDraft(f"{base}:shortfall", account_id, "DIVIDEND_SHORTFALL", payment_at, ccy, market=market, code=code,
                                                      attrib_amount=to_db(g - n), group_id=group_id,
                                                      note="净额支付，差额性质未知（税/费/汇差）"), source=source))
            return out
        out.append(post_event(conn, EventDraft(f"{base}:payment", account_id, "DIVIDEND_PAYMENT", payment_at, ccy, market=market, code=code,
                                              cash_delta=to_db(g), recv_delta=to_db(-g), group_id=group_id), source=source))
        if withholding_tax is not None and dec(withholding_tax) > 0:
            w = dec(withholding_tax)
            if w >= g:
                raise LedgerError("预扣税不应不小于总额")
            out.append(post_event(conn, EventDraft(f"{base}:tax", account_id, "TAX", payment_at, ccy, cash_delta=to_db(-w), ref_event_key=f"{base}:payment",
                                                  group_id=group_id), source=source))
        return out


# ------------------------------------------------------------------ 待匹配队列
def queue_pending(conn: sqlite3.Connection, source: SourceDraft, reason: str, received_at=None) -> str:
    with atomic(conn):
        sid = record_source(conn, source, received_at)
        pid = hashlib.sha256(f"{sid}|{reason}".encode("utf-8")).hexdigest()[:24]
        conn.execute("INSERT OR IGNORE INTO pending_match(pending_id, source_record_id, reason, created_at) VALUES (?,?,?,?)",
                     (pid, sid, reason, iso_utc(utc_now())))
        return pid


def resolve_pending(conn: sqlite3.Connection, pending_id: str, resolution: str, event_id: str | None = None, note: str | None = None) -> None:
    if resolution not in ("posted", "duplicate", "rejected"):
        raise LedgerError("resolution 必须是 posted/duplicate/rejected")
    conn.execute("INSERT INTO pending_resolution(pending_id, resolution, event_id, note, resolved_at) VALUES (?,?,?,?,?)",
                 (pending_id, resolution, event_id, note, iso_utc(utc_now())))


def open_pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.* FROM pending_match p LEFT JOIN pending_resolution r ON r.pending_id=p.pending_id WHERE r.pending_id IS NULL ORDER BY p.created_at"
    ).fetchall()
