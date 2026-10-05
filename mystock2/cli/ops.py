"""日常运行命令：批次、协议冻结、教练（操作单）、意图、记分牌（实施方案 §5 M6、§9）。

约定（实施方案 §6A.2 密封）：
- `coach run` **只输出运行回执与计数，绝不打印动作、限价或数量**；用户经 `coach show`（受控揭示，写暴露日志）才能看到 AI 单；
  在人类计划锁定之前先 `intent add`。
- 真实协议/名单/费用档案放 `config/local/`（被忽略）；缺失关键参数 → 不生成可执行数量，运行标 pilot。
- 命令相互独立；任何命令都不会对外发布、不会下单。
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

from mystock2.coach.decide import Prediction, StrategyParams, decide
from mystock2.coach.intents import plan_to_drafts, record_intent, reveal, select_human_plan
from mystock2.coach.tickets import freeze_tickets
from mystock2.core import calendars as cal
from mystock2.core import db as dbmod
from mystock2.core.config import REPO_ROOT, ConfigError, load_config
from mystock2.core.money import dec, to_db
from mystock2.core.runs import run_log
from mystock2.core.timeutil import ensure_utc, iso_utc, utc_now
from mystock2.forecast.baseline import BaselineParams, ForecastUnavailable
from mystock2.forecast.run import generate
from mystock2.instruments.security_rule import RuleUnknown, rule_for
from mystock2.instruments.universe import UniverseReport, load_universe
from mystock2.ledger.fees import FeeRule, load_fee_rules
from mystock2.ledger.settlement import SettlementRule
from mystock2.scoreboard.engine import LineRun, run_line
from mystock2.scoreboard.lines import BuyHoldProvider, batch_initial_state, create_batch, save_run
from mystock2.scoreboard.marketdata import DbMarketData
from mystock2.scoreboard.metrics import summarize
from mystock2.scoreboard.providers import HumanPlanProvider, TicketProvider
from mystock2.scoreboard.stats import ambiguous_dates, paired_diff
from mystock2.scoreboard.types import ExecProtocol, LineState, Lot

LOCAL_DIR = REPO_ROOT / "config" / "local"
REQUIRED_STRATEGY = ("k", "q_buy", "q_sell", "max_hold_days", "exit_q", "budget_slice")


# ------------------------------------------------------------------ 本地私有配置
@dataclass
class Local:
    protocol: dict
    universe: UniverseReport
    fee_rules: list[FeeRule]
    settlement: dict[str, SettlementRule]

    def strategy_params(self) -> StrategyParams:
        s = self.protocol.get("strategy") or {}
        d = lambda k: dec(str(s[k])) if s.get(k) is not None else None  # noqa: E731
        exit_cfg = s.get("exit_order") or {}
        return StrategyParams(
            version=str(self.protocol.get("strategy_version", "inv-policy-v1")), k=d("k"), q_buy=d("q_buy"), q_sell=d("q_sell"), min_gain=d("min_gain"),
            max_hold_days=int(s["max_hold_days"]) if s.get("max_hold_days") is not None else None, exit_q=d("exit_q"),
            exit_unfilled=str(exit_cfg.get("unfilled") or "carry"), budget_slice=d("budget_slice"),
            allow_add=s.get("allow_add"), slippage_bps=dec(str(s.get("slippage_bps", "0"))))

    def exec_protocol(self) -> ExecProtocol:
        e = self.protocol.get("execution") or {}
        return ExecProtocol(max_participation=dec(str(e.get("max_participation", "0.10"))), fee_multiplier=dec(str(e.get("fee_multiplier", "1"))),
                            slippage_bps=dec(str(e.get("slippage_bps", "0"))))

    def baseline_params(self) -> BaselineParams:
        b = self.protocol.get("baseline") or {}
        return BaselineParams(**{k: v for k, v in b.items() if k in BaselineParams.__dataclass_fields__})

    def missing_required(self) -> list[str]:
        s = self.protocol.get("strategy") or {}
        miss = [f"strategy.{k}" for k in REQUIRED_STRATEGY if s.get(k) is None]
        if (self.protocol.get("human_plan") or {}).get("constraint_handling") not in ("truncate", "reject"):
            miss.append("human_plan.constraint_handling")
        for m in {e.market for e in self.universe.entries}:
            if m not in self.settlement or self.settlement[m].lag_sessions is None:
                miss.append(f"settlement.{m}")
        return miss


def load_local(local_dir: Path | None = None, known_codes: list[str] | None = None) -> Local:
    d = Path(local_dir) if local_dir else LOCAL_DIR
    proto_p = d / "protocol.yaml"
    if not proto_p.exists():
        raise FileNotFoundError(f"缺少 {proto_p}（真实协议放 config/local/，模板见 config/protocol.example.yaml）")
    protocol = yaml.safe_load(proto_p.read_text(encoding="utf-8")) or {}
    universe = load_universe(d / "universe.yaml", known_codes or ())
    if not universe.ok:
        raise ConfigError(f"universe 校验失败：{universe.errors}")
    fees_p = d / "fees.yaml"
    fee_rules = load_fee_rules(fees_p) if fees_p.exists() else []
    settle = {m: SettlementRule(m, int(v) if v is not None else None) for m, v in (protocol.get("settlement") or {}).items()}
    return Local(protocol, universe, fee_rules, settle)


# ------------------------------------------------------------------ 批次上下文与线内状态
@dataclass
class Batch:
    batch_id: str
    market: str
    currency: str
    start: date
    e0: Decimal
    codes: list[str]
    lines: dict[str, str]          # kind -> line_id


def load_batch(conn, batch_id: str) -> Batch:
    r = conn.execute("SELECT * FROM comparison_batch WHERE batch_id=?", (batch_id,)).fetchone()
    if not r:
        raise ConfigError(f"批次不存在：{batch_id}")
    meta = json.loads(r["initial_state_json"]).get("_meta", {})
    lines = {x["kind"]: x["line_id"] for x in conn.execute("SELECT kind, line_id FROM strategy_line WHERE batch_id=?", (batch_id,))}
    return Batch(batch_id, meta["market"], r["currency"], date.fromisoformat(r["start_date"]), dec(r["e0"]), meta["codes"], lines)


def sessions_between(market: str, after: date, upto: date) -> list[date]:
    return cal.session_days(market, cal.next_session(market, after), upto) if upto > after else []


def split_table(conn, codes: list[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in conn.execute("SELECT code, effective_at, ratio_num, ratio_den FROM corporate_action WHERE kind='SPLIT' ORDER BY effective_at"):
        if r["code"] in codes:
            out.setdefault(r["code"], []).append((ensure_utc(r["effective_at"]), r["ratio_num"], r["ratio_den"]))
    return out


def lot_sizes(conn, codes: list[str], on: date) -> dict[str, int]:
    out = {}
    for c in codes:
        try:
            out[c] = rule_for(conn, c, on.isoformat()).lot_size or 1
        except RuleUnknown:
            out[c] = 1
    return out


def simulate_line(conn, loc: Local, b: Batch, kind: str, upto: date, *, buyhold_weights: dict | None = None) -> LineRun:
    """从批次起点逐日重算某条线到 upto（含），读取已冻结的操作单/人类计划；确定性、可复算，不写库。"""
    md = DbMarketData(conn)
    proto = loc.exec_protocol()
    init = batch_initial_state(conn, b.batch_id)
    line_id = b.lines[kind]
    if kind in ("ai", "ai_veto", "ai_lgbm"):
        provider = TicketProvider(conn, batch_id=b.batch_id, line_id=line_id, market=b.market, codes=b.codes)
    elif kind == "human_plan":
        provider = HumanPlanProvider(conn, batch_id=b.batch_id, line_id=line_id, market=b.market, codes=b.codes)
    elif kind == "buyhold":
        first = cal.next_session(b.market, b.start)
        provider = BuyHoldProvider(md, market=b.market, weights=buyhold_weights or {}, first_day=first, lot_sizes=lot_sizes(conn, b.codes, first),
                                   fee_rules=loc.fee_rules, protocol=proto, line_id=line_id)
    else:
        raise ConfigError(f"不支持模拟的线类型：{kind}")
    sessions = sessions_between(b.market, b.start, upto)
    return run_line(md, market=b.market, currency=b.currency, initial=init, sessions=sessions, provider=provider, protocol=proto, fee_rules=loc.fee_rules,
                    settlement=loc.settlement[b.market], lot_sizes=lot_sizes(conn, b.codes, b.start), splits=split_table(conn, b.codes))


def state_at_open(conn, loc: Local, b: Batch, kind: str, target: date) -> tuple[LineState, Decimal]:
    """目标日开盘前的线内状态与交易仓权益（用于预算）。"""
    prev = cal.prev_session(b.market, target)
    if prev < b.start:
        prev = b.start
    if prev == b.start:
        st = batch_initial_state(conn, b.batch_id)
        equity = b.e0
    else:
        run = simulate_line(conn, loc, b, kind, prev, buyhold_weights=None)
        st = run.final_state.copy()
        ok = [r for r in run.results if r.status == "OK"]
        if not run.results or run.results[-1].status != "OK":
            raise ConfigError(f"{kind} 线在 {prev} 前进入 UNKNOWN/PAUSED：先补齐行情并重算（不得用假定库存继续）")
        equity = ok[-1].equity
    st.unsettled = [(d, a) for d, a in st.unsettled if d > target]       # 结算日当天视为已结算
    return st, equity


# ------------------------------------------------------------------ 命令
def _conn_ro(cfg):
    return dbmod.connect_ro(cfg.db_path)


def _opener(cfg, owner):
    return dbmod.connect_writer(cfg.db_path, owner)


def cmd_batch_create(args) -> int:
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    market, d0 = args.market.upper(), date.fromisoformat(args.d0)
    codes = [e.code for e in loc.universe.entries if e.market == market and e.tier == "trade"]
    ro = _conn_ro(cfg)
    md = DbMarketData(ro)
    budget = dec(args.budget)
    init = LineState(args.currency.upper(), budget)
    notes = []
    for code, qty in (json.loads(args.positions) if args.positions else {}).items():          # 开账时属于交易仓的初始库存（来自开账快照，由负责人核对）
        px = md.close(code, d0)
        if px is None:
            raise ConfigError(f"{code} 在 {d0} 缺少未复权收盘价，无法估值/估成本")
        init.lots[code] = [Lot(dec(str(qty)), px, d0)]
        notes.append(f"cost_estimated:{code}")
    pos_value = sum((init.qty(c) * md.close(c, d0) for c in init.lots), Decimal(0))
    e0 = init.cash + pos_value
    kinds = args.lines.split(",")
    with _opener(cfg, "scoreboard") as w:
        create_batch(w, args.id, loc.exec_protocol(), d0, init, e0, kinds, meta={"market": market, "codes": codes, "notes": notes})
    print(f"batch={args.id} market={market} e0={to_db(e0)} lines={kinds} notes={notes}")
    return 0


def cmd_protocol_freeze(args) -> int:
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    missing = loc.missing_required()
    if missing and not args.pilot:
        print("协议不完整，拒绝冻结（缺失项使所有运行保持 pilot）：" + ", ".join(missing), file=sys.stderr)
        return 2
    body = json.dumps(loc.protocol, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256(body.encode()).hexdigest()
    version = str(loc.protocol.get("protocol_version") or "")
    if not version or version == "TEMPLATE":
        print("protocol_version 未填写（模板值 TEMPLATE 不可冻结）", file=sys.stderr)
        return 2
    summary = {"protocol_version": version, "hash": h, "pilot": bool(missing), "missing": missing, "universe_size": len(loc.universe.entries),
               "markets": sorted({e.market for e in loc.universe.entries})}
    with _opener(cfg, "coach") as w:
        w.execute("INSERT INTO protocol_freeze(protocol_version, protocol_hash, frozen_at, summary_json, code_sha) VALUES (?,?,?,?,?)",
                  (version, h, iso_utc(utc_now()), json.dumps(summary, sort_keys=True), args.code_sha))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _target_for(stage: str, market: str, now: datetime, as_of: date | None) -> tuple[date, date]:
    """返回 (数据截至日 T, 目标日)。close 阶段：T=最近已走完的交易日，目标=下一交易日；preopen：目标=今日（若为交易日）。"""
    if stage == "close":
        t = as_of or _latest_closed(market, now)
        return t, cal.next_session(market, t)
    today = as_of or now.astimezone(cal.MARKET_TZ[market]).date()
    if not cal.is_session(market, today):
        raise ConfigError(f"{today} 不是 {market} 交易日")
    return cal.prev_session(market, today), today


def _latest_closed(market: str, now: datetime) -> date:
    from mystock2.collectors.quotes import FINAL_BUFFER
    day = now.astimezone(cal.MARKET_TZ[market]).date() + timedelta(days=1)
    while True:
        day -= timedelta(days=1)
        if cal.is_session(market, day) and cal.session(market, day).close_utc + FINAL_BUFFER <= now:
            return day


def cmd_coach_run(args) -> int:
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    now = ensure_utc(args.now) if args.now else utc_now()
    market = args.market.upper()
    ro = _conn_ro(cfg)
    b = load_batch(ro, args.batch)
    t, target = _target_for(args.stage, market, now, date.fromisoformat(args.as_of) if args.as_of else None)
    deadline = cal.project_deadline(market, target)
    params, missing = loc.strategy_params(), loc.missing_required()
    entries = [e for e in loc.universe.entries if e.market == market]
    with _opener(cfg, "core") as rl, _opener(cfg, "market") as mw, _opener(cfg, "forecast") as fw, _opener(cfg, "coach") as cw:
        with run_log(rl, f"coach run --stage {args.stage}", {"batch": args.batch, "market": market, "target": target.isoformat()}) as run:
            preds: dict[str, Prediction | None] = {}
            rules = {}
            for e in entries:
                if e.tier != "trade":
                    continue
                try:
                    pid = generate(mw, fw, e.code, t, input_cutoff_at=now, params=loc.baseline_params(), now=now)
                    row = ro.execute("SELECT * FROM prediction_version WHERE prediction_id=?", (pid,)).fetchone()
                    close = dec(ro.execute("SELECT close FROM quote_daily WHERE code=? AND session_date=? AND quality='ok' ORDER BY version DESC LIMIT 1",
                                           (e.code, t.isoformat())).fetchone()["close"])
                    preds[e.code] = Prediction(pid, close, dec(row["low_price"]), dec(row["high_price"]), row["n_train"])
                except ForecastUnavailable:
                    preds[e.code] = None
                try:
                    rules[e.code] = rule_for(ro, e.code, target.isoformat())
                except RuleUnknown:
                    rules[e.code] = None
            n_total = 0
            for kind in ("ai",):
                if kind not in b.lines:
                    continue
                state, equity = state_at_open(ro, loc, b, kind, target)
                drafts = decide(state, market=market, target_session=target, universe=loc.universe.entries, predictions=preds, rules=rules,
                                params=params, fee_rules=loc.fee_rules, trade_equity=equity)
                ids = freeze_tickets(cw, batch_id=b.batch_id, line_id=b.lines[kind], kind="line_sim", market=market, target_session=target, stage=args.stage,
                                     drafts=drafts, state_ref_type="line_state", state_ref=state.hash(), strategy_version=params.version,
                                     protocol_version=loc.protocol.get("protocol_version", "pilot"), generated_at=now, now=now, deadline_at=deadline)
                n_total += len(ids)
            run.note(tickets=n_total, pilot=bool(missing), missing=missing)
            # 密封：只输出回执与计数，不输出动作/限价/数量
            print(f"run_id={run.run_id} stage={args.stage} market={market} data_as_of={t} target={target} tickets_frozen={n_total} pilot={bool(missing)}")
    return 0


def cmd_coach_show(args) -> int:
    """受控揭示：打印 AI 单并写暴露日志（先 intent add，再 show）。"""
    cfg = load_config(args.config)
    market = args.market.upper()
    target = date.fromisoformat(args.target)
    ro = _conn_ro(cfg)
    rows = ro.execute("SELECT * FROM ticket WHERE batch_id=? AND market=? AND target_session=? AND kind=? AND status='frozen' ORDER BY code, visible_at, rowid",
                      (args.batch, market, target.isoformat(), "line_sim" if not args.live else "live_guidance")).fetchall()
    latest = {}
    for r in rows:
        if "human_plan" not in r["line_id"]:
            latest[(r["line_id"], r["code"])] = r
    with _opener(cfg, "coach") as w:
        reveal(w, batch_id=args.batch, market=market, target_session=target, channel="coach_show", version_hashes=[r["frozen_hash"] for r in latest.values()],
               at=ensure_utc(args.now) if args.now else None)
    for (line, code), r in sorted(latest.items()):
        print(f"{line} {code} {r['action']} limit={r['limit_price']} qty={r['qty']} reasons={r['reason_json']} deadline={r['deadline_at']}")
    print(f"（已写暴露日志：{len(latest)} 张；此后记录的人类计划将标 seen_ai=1 且不进入 human_plan 线）")
    return 0


def cmd_coach_status(args) -> int:
    """不含动作/限价：只统计状态与覆盖。"""
    cfg = load_config(args.config)
    ro = _conn_ro(cfg)
    q = "SELECT target_session, stage, status, COUNT(*) n FROM ticket WHERE batch_id=? AND kind=?"
    p = [args.batch, "line_sim"]
    if args.market:
        q += " AND market=?"
        p.append(args.market.upper())
    for r in ro.execute(q + " GROUP BY target_session, stage, status ORDER BY target_session", p):
        print(f"{r['target_session']} {r['stage']:<10} {r['status']:<16} {r['n']}")
    e = ro.execute("SELECT COUNT(*) n, MIN(revealed_at) m FROM intent_exposure WHERE batch_id=?", (args.batch,)).fetchone()
    print(f"exposures={e['n']} first_reveal={e['m']}")
    return 0


def cmd_intent_state(args) -> int:
    """展示「该人类线自己的状态」（记录计划前必看；不含 AI 单）。"""
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    ro = _conn_ro(cfg)
    b = load_batch(ro, args.batch)
    target = date.fromisoformat(args.target)
    st, _ = state_at_open(ro, loc, b, "human_plan", target)
    print(json.dumps({"state_hash": st.hash(), **st.to_dict(), "tradable_cash": to_db(st.tradable_cash())}, ensure_ascii=False, indent=2))
    return 0


def cmd_intent_add(args) -> int:
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    ro = _conn_ro(cfg)
    b = load_batch(ro, args.batch)
    market, target = b.market, date.fromisoformat(args.target)
    st, _ = state_at_open(ro, loc, b, "human_plan", target)
    now = ensure_utc(args.now) if args.now else utc_now()
    try:
        rule = rule_for(ro, args.code, target.isoformat())
    except RuleUnknown:
        rule = None
    handling = (loc.protocol.get("human_plan") or {}).get("constraint_handling") or "reject"
    with _opener(cfg, "coach") as w:
        res = record_intent(w, batch_id=b.batch_id, line_id=b.lines["human_plan"], market=market, code=args.code, target_session=target, action=args.action.upper(),
                            limit_price=args.limit, qty=args.qty, state=st, state_hash=st.hash(), now=now, deadline_at=cal.project_deadline(market, target),
                            constraint_handling=handling, rule=rule, fee_rules=loc.fee_rules, note=args.note)
    print(f"intent={res.intent_id} qty={res.qty} truncated={res.truncated} seen_ai={int(res.seen_ai)} late_record={int(res.late_record)} notes={list(res.notes)}")
    return 0


def cmd_human_plan_freeze(args) -> int:
    """截止时把人类计划冻结为 human_plan 线的 line_sim 单（缺失＝无订单，不补动机）。"""
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    ro = _conn_ro(cfg)
    b = load_batch(ro, args.batch)
    target = date.fromisoformat(args.target)
    deadline = cal.project_deadline(b.market, target)
    st, _ = state_at_open(ro, loc, b, "human_plan", target)
    plan = select_human_plan(ro, batch_id=b.batch_id, line_id=b.lines["human_plan"], market=b.market, target_session=target, codes=b.codes, deadline_at=deadline)
    drafts = plan_to_drafts(plan, lot_sizes(ro, b.codes, target))
    now = ensure_utc(args.now) if args.now else utc_now()
    with _opener(cfg, "coach") as w:
        ids = freeze_tickets(w, batch_id=b.batch_id, line_id=b.lines["human_plan"], kind="line_sim", market=b.market, target_session=target, stage="human_plan",
                             drafts=drafts, state_ref_type="line_state", state_ref=st.hash(), strategy_version="human", protocol_version=loc.protocol.get("protocol_version", "pilot"),
                             generated_at=min(now, deadline), now=now, deadline_at=deadline)
    print(f"human_plan_tickets={len(ids)} plan_missing={sum(1 for p in plan.values() if 'plan_missing' in p['flags'])}")
    return 0


def cmd_scoreboard_run(args) -> int:
    """重算各线到 --end 并写入一个新 run（不覆盖旧 run）；打印与库内一致的指标（SB-01：CLI/API 同一数值）。"""
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    ro = _conn_ro(cfg)
    b = load_batch(ro, args.batch)
    end = date.fromisoformat(args.end)
    weights = {c: Decimal(1) / len(b.codes) for c in b.codes} if b.codes else {}       # 默认：交易仓标的等权（协议可预注册覆盖）
    for c, w in ((loc.protocol.get("buyhold") or {}).get("weights") or {}).items():
        weights[c] = dec(str(w))
    runs = {}
    for kind, line_id in b.lines.items():
        if kind == "human_actual":
            continue
        runs[line_id] = simulate_line(ro, loc, b, kind, end, buyhold_weights=weights if kind == "buyhold" else None)
    with run_log(_opener(cfg, "core"), "scoreboard run", {"batch": b.batch_id, "end": args.end}) as rl:
        run_id = rl.run_id
        with _opener(cfg, "scoreboard") as w:
            save_run(w, run_id, b.batch_id, loc.exec_protocol(), [], runs, b.e0, b.currency)
    summary = {}
    for line_id, run in runs.items():
        summary[line_id] = summarize(run.results, b.e0).as_dict()
    ai, human, hold = (runs.get(b.lines.get(k, "")) for k in ("ai", "human_plan", "buyhold"))
    out = {"run_id": run_id, "batch": b.batch_id, "lines": summary}
    for name, other in (("ai_vs_human_plan", human), ("ai_vs_buyhold", hold)):
        if ai and other:
            d = paired_diff(ai.results, other.results, b.e0)
            ex = paired_diff(ai.results, other.results, b.e0, exclude_dates=ambiguous_dates(ai.results, other.results))
            out[name] = {"n": d.n, "coverage": str(d.coverage), "cumulative_diff": str(d.cumulative), "n_excluding_ambiguous": ex.n}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def register(sub) -> None:
    ld = {"help": "本地私有配置目录（默认 config/local/）"}
    b = sub.add_parser("batch", help="比较批次").add_subparsers(dest="bcmd", required=True)
    c = b.add_parser("create", help="创建批次（冻结完整状态包）")
    for a, kw in (("--id", {"required": True}), ("--market", {"required": True}), ("--d0", {"required": True}), ("--currency", {"required": True}),
                  ("--budget", {"required": True}), ("--lines", {"default": "ai,human_plan,buyhold"}), ("--positions", {"help": 'JSON：{"US.NVDA": 10}（开账时交易仓初始库存）'})):
        c.add_argument(a, **kw)
    c.add_argument("--local-dir", **ld)
    c.set_defaults(fn=cmd_batch_create)
    p = sub.add_parser("protocol", help="协议").add_subparsers(dest="pcmd", required=True)
    f = p.add_parser("freeze", help="冻结协议（缺必填项则拒绝；--pilot 登记为 pilot）")
    f.add_argument("--local-dir", **ld)
    f.add_argument("--pilot", action="store_true")
    f.add_argument("--code-sha")
    f.set_defaults(fn=cmd_protocol_freeze)
    co = sub.add_parser("coach", help="教练（操作单）").add_subparsers(dest="ccmd", required=True)
    r = co.add_parser("run", help="生成并冻结操作单（密封：不打印动作/限价）")
    r.add_argument("--batch", required=True)
    r.add_argument("--market", required=True)
    r.add_argument("--stage", choices=["close", "preopen"], required=True)
    r.add_argument("--as-of", help="数据截至的交易日（默认自动）")
    r.add_argument("--now", help="测试用：覆盖当前时间（带时区 ISO）")
    r.add_argument("--local-dir", **ld)
    r.set_defaults(fn=cmd_coach_run)
    s = co.add_parser("show", help="受控揭示 AI 单（写暴露日志）")
    s.add_argument("--batch", required=True)
    s.add_argument("--market", required=True)
    s.add_argument("--target", required=True)
    s.add_argument("--live", action="store_true", help="显示 live_guidance 而非模拟线单")
    s.add_argument("--now", help="测试用：覆盖当前时间（带时区 ISO）")
    s.set_defaults(fn=cmd_coach_show)
    st = co.add_parser("status", help="状态与覆盖（不含动作/限价）")
    st.add_argument("--batch", required=True)
    st.add_argument("--market")
    st.set_defaults(fn=cmd_coach_status)
    it = sub.add_parser("intent", help="人类计划").add_subparsers(dest="icmd", required=True)
    s2 = it.add_parser("state", help="展示人类线自己的状态")
    s2.add_argument("--batch", required=True)
    s2.add_argument("--target", required=True)
    s2.add_argument("--local-dir", **ld)
    s2.set_defaults(fn=cmd_intent_state)
    a = it.add_parser("add", help="记录结构化人类计划")
    for k, kw in (("--batch", {"required": True}), ("--target", {"required": True}), ("--code", {"required": True}),
                  ("--action", {"required": True, "choices": ["buy", "sell", "hold", "no_trade", "BUY", "SELL", "HOLD", "NO_TRADE"]}), ("--limit", {}),
                  ("--qty", {"type": int}), ("--note", {}), ("--now", {})):
        a.add_argument(k, **kw)
    a.add_argument("--local-dir", **ld)
    a.set_defaults(fn=cmd_intent_add)
    fh = it.add_parser("freeze", help="截止时冻结人类计划为 human_plan 线的单")
    fh.add_argument("--batch", required=True)
    fh.add_argument("--target", required=True)
    fh.add_argument("--now")
    fh.add_argument("--local-dir", **ld)
    fh.set_defaults(fn=cmd_human_plan_freeze)
    sc = sub.add_parser("scoreboard", help="记分牌").add_subparsers(dest="scmd", required=True)
    rn = sc.add_parser("run", help="重算各线并写入新 run")
    rn.add_argument("--batch", required=True)
    rn.add_argument("--end", required=True)
    rn.add_argument("--local-dir", **ld)
    rn.set_defaults(fn=cmd_scoreboard_run)
