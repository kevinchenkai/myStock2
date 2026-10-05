"""策略线与比较批次的登记、运行结果持久化、买入持有 provider。

- 批次（comparison_batch）在起点冻结**完整状态包**与参与线名单（§6A.4）；新候选以新批次加入，不拼接旧线。
- 运行结果以新 run_id 写入，冻结后只读（触发器）；重算不覆盖。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.core.db import atomic
from mystock2.core.money import floor_to_lots, to_db
from mystock2.core.timeutil import iso_utc, utc_now
from mystock2.ledger.fees import FeeRule, estimate, select_rule
from mystock2.scoreboard.engine import LineRun, MarketData, ProviderUnknown
from mystock2.scoreboard.metrics import summarize
from mystock2.scoreboard.types import BUY, ExecProtocol, LineState, SimOrder

LINE_KINDS = ("human_actual", "human_plan", "buyhold", "ai", "ai_lgbm", "ai_veto")


def create_batch(conn: sqlite3.Connection, batch_id: str, protocol: ExecProtocol, start_date: date, initial: LineState, e0: Decimal,
                 line_kinds: list[str], meta: dict | None = None) -> str:
    bad = [k for k in line_kinds if k not in LINE_KINDS]
    if bad:
        raise ValueError(f"未知策略线类型：{bad}")
    state_json = json.dumps({**initial.to_dict(), "_meta": meta or {}}, sort_keys=True)
    with atomic(conn):
        if conn.execute("SELECT 1 FROM comparison_batch WHERE batch_id=?", (batch_id,)).fetchone():
            raise ValueError(f"批次已存在：{batch_id}（修改＝新批次）")
        conn.execute(
            "INSERT INTO comparison_batch(batch_id, protocol_version, start_date, currency, e0, initial_state_json, state_hash, lines_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (batch_id, protocol.version, start_date.isoformat(), initial.currency, to_db(e0), state_json, initial.hash(), json.dumps(sorted(line_kinds)), iso_utc(utc_now())))
        for k in line_kinds:
            conn.execute("INSERT INTO strategy_line(line_id, batch_id, kind, params_hash, protocol_version, created_at) VALUES (?,?,?,?,?,?)",
                         (f"{batch_id}:{k}", batch_id, k, "", protocol.version, iso_utc(utc_now())))
    return batch_id


def batch_initial_state(conn: sqlite3.Connection, batch_id: str) -> LineState:
    """每条线从批次的同一完整状态包起跑（§6A.4）。"""
    row = conn.execute("SELECT initial_state_json, state_hash FROM comparison_batch WHERE batch_id=?", (batch_id,)).fetchone()
    st = LineState.from_dict(json.loads(row["initial_state_json"]))
    assert st.hash() == row["state_hash"], "批次初始状态被改动"
    return st


def save_run(conn: sqlite3.Connection, run_id: str, batch_id: str, protocol: ExecProtocol, evidence: list[str],
             line_runs: dict[str, LineRun], e0: Decimal, currency: str, extra: dict | None = None) -> None:
    """line_runs: {line_id: LineRun}。重复 run_id 报错（不覆盖）。"""
    with atomic(conn):
        conn.execute("INSERT INTO eval_run(run_id, batch_id, protocol_version, protocol_json, evidence_json, created_at) VALUES (?,?,?,?,?,?)",
                     (run_id, batch_id, protocol.version, json.dumps({**protocol.as_dict(), **(extra or {})}, sort_keys=True), json.dumps(sorted(evidence)), iso_utc(utc_now())))
        metrics = {}
        for line_id, run in line_runs.items():
            for res in run.results:
                conn.execute(
                    "INSERT INTO sleeve_daily(run_id, line_id, currency, date, status, equity, cash, unsettled, position_value, fees_day, fees_cum, positions_json, flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, line_id, currency, res.date.isoformat(), res.status, to_db(res.equity) if res.equity is not None else None,
                     to_db(res.cash) if res.cash is not None else None, to_db(res.unsettled) if res.unsettled is not None else None,
                     to_db(res.position_value) if res.position_value is not None else None, to_db(res.fees_day), None,
                     json.dumps({c: to_db(q) for c, q in res.positions.items()}, sort_keys=True), json.dumps(res.flags)))
                for f in res.fills:
                    conn.execute("INSERT INTO sim_fill(run_id, line_id, date, seq, code, side, qty, price, fee, bar_start, ambiguous, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (run_id, line_id, res.date.isoformat(), f.seq, f.code, f.side, to_db(f.qty), to_db(f.price), to_db(f.fee),
                                  iso_utc(f.bar_start) if f.bar_start else None, int(f.ambiguous), f.note))
            for d, st in run.start_states.items():
                conn.execute("INSERT INTO line_state(run_id, line_id, currency, date, state_json, state_hash) VALUES (?,?,?,?,?,?)",
                             (run_id, line_id, currency, d.isoformat(), json.dumps(st.to_dict(), sort_keys=True), st.hash()))
            metrics[line_id] = summarize(run.results, e0).as_dict()
        conn.execute("UPDATE eval_run SET metrics_json=? WHERE run_id=?", (json.dumps(metrics, sort_keys=True), run_id))


class BuyHoldProvider:
    """`D0` 后首个交易日，按预注册权重把可交易现金一次性买入（开盘市价、整手取整、余款留现金），此后永不交易（ADR 0002、§6A.8）。"""

    def __init__(self, md: MarketData, *, market: str, weights: dict[str, Decimal], first_day: date, lot_sizes: dict[str, int],
                 fee_rules: list[FeeRule], protocol: ExecProtocol, line_id: str = "buyhold"):
        if sum(weights.values(), Decimal(0)) > Decimal(1):
            raise ValueError("权重之和不得超过 1")
        self.md, self.market, self.weights, self.first_day = md, market, weights, first_day
        self.lot_sizes, self.fee_rules, self.protocol, self.line_id = lot_sizes, fee_rules, protocol, line_id

    def __call__(self, state: LineState, day: date) -> list[SimOrder]:
        if day != self.first_day:
            return []
        budget = state.tradable_cash()
        orders = []
        for code, w in sorted(self.weights.items()):
            bars = self.md.hourly(code, day)
            open_utc = cal.session(self.market, day).open_utc
            if not bars or bars[0].start - open_utc > timedelta(minutes=1):
                raise ProviderUnknown(f"{code} 缺少 {day} 的首根完整小时线：买入持有无法建仓，不得静默当作闲置现金")
            px, lot = bars[0].open, self.lot_sizes.get(code, 1)
            alloc = budget * w
            rule = select_rule(self.fee_rules, self.market, BUY, day.isoformat())
            qty = int(floor_to_lots(alloc / px, lot))
            while qty > 0:
                fee = estimate(rule, [(Decimal(qty), px)]).fee * self.protocol.fee_multiplier
                if Decimal(qty) * px + fee <= alloc:
                    break
                qty -= lot
            if qty > 0:
                orders.append(SimOrder(self.line_id, code, BUY, qty, None, "buyhold-initial"))
        return orders


class BatchTerminated(RuntimeError):
    """外部资金流进入正式模拟线：该批次终止，须新建批次（方案 §6A.5、T-43）。"""


def apply_external_flow(state: LineState, amount: Decimal) -> None:
    raise BatchTerminated(f"正式模拟线禁止外部注资/出金（{amount} {state.currency}）：批次终止，请新建批次；真实账户的入出金只影响 human_actual 的描述性路径")
