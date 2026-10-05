"""证券规则（lot/tick，带有效期与来源；WP4.4）。

**不假设每手股数或 tick 档位是常数**：规则以数据存放；未配置/未核实时 `RuleUnknown`（失败关闭）。
tick 档位 JSON：按价格升序的区间列表，如 [{"lt":"0.25","tick":"0.001"},{"lt":"0.5","tick":"0.005"},{"tick":"0.01"}]（最后一项无 lt＝兜底）。
档位的真实数值与生效日期须依据交易所公告核实后录入（M0a），本模块不内置任何市场的价位表。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from mystock2.core.db import atomic
from mystock2.core.money import dec, round_limit_price
from mystock2.core.timeutil import iso_utc, utc_now


class RuleUnknown(LookupError):
    pass


@dataclass(frozen=True)
class SecurityRule:
    code: str
    valid_from: str
    valid_to: str | None
    lot_size: int | None
    tick_bands: tuple[tuple[Decimal | None, Decimal], ...]   # ((上界(不含)|None, tick), ...)
    source: str
    verified: bool

    def tick_for(self, price) -> Decimal:
        if not self.tick_bands:
            raise RuleUnknown(f"{self.code} 未配置 tick 档位")
        p = dec(price)
        for upper, tick in self.tick_bands:
            if upper is None or p < upper:
                return tick
        raise RuleUnknown("tick 档位未覆盖该价格")

    def legal_limit(self, price, side: str) -> Decimal:
        """限价保守舍入到合法 tick（买向下、卖向上）。舍入跨档后以新价位所在档位复核，必要时再舍入。"""
        px = dec(price)
        for _ in range(3):
            tick = self.tick_for(px)
            rounded = round_limit_price(px, tick, side)
            if self.tick_for(rounded) == tick:
                return rounded
            px = rounded
        raise RuleUnknown("tick 舍入不收敛（档位配置可能有误）")


def parse_bands(tick_json: str | None) -> tuple[tuple[Decimal | None, Decimal], ...]:
    if not tick_json:
        return ()
    bands = []
    for item in json.loads(tick_json):
        bands.append((dec(item["lt"]) if item.get("lt") is not None else None, dec(item["tick"])))
    uppers = [b[0] for b in bands if b[0] is not None]
    if uppers != sorted(uppers) or any(b[0] is None for b in bands[:-1]):
        raise ValueError("tick 档位必须按价格升序，且只有最后一项可无上界")
    return tuple(bands)


def put_rule(conn: sqlite3.Connection, code: str, valid_from: str, *, lot_size: int | None, tick_json: str | None, source: str,
             verified: bool = False, valid_to: str | None = None) -> None:
    parse_bands(tick_json)   # 校验
    with atomic(conn):
        conn.execute("INSERT OR IGNORE INTO security_rule(code, valid_from, valid_to, lot_size, tick_json, source, verified, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (code, valid_from, valid_to, lot_size, tick_json, source, int(verified), iso_utc(utc_now())))


def rule_for(conn: sqlite3.Connection, code: str, on_date: str, *, require_verified: bool = True) -> SecurityRule:
    row = conn.execute(
        "SELECT * FROM security_rule WHERE code=? AND valid_from<=? AND (valid_to IS NULL OR ?<valid_to) ORDER BY valid_from DESC LIMIT 1",
        (code, on_date, on_date)).fetchone()
    if not row:
        raise RuleUnknown(f"{code} 在 {on_date} 没有证券规则（规则未知，不出可执行数量）")
    if require_verified and not row["verified"]:
        raise RuleUnknown(f"{code} 的证券规则尚未核实（verified=0）")
    return SecurityRule(code, row["valid_from"], row["valid_to"], row["lot_size"], parse_bands(row["tick_json"]), row["source"], bool(row["verified"]))
