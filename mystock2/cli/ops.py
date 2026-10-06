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
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

from mystock2.coach.decide import Prediction, StrategyParams, decide
from mystock2.coach.intents import plan_to_drafts, record_intent, reveal, select_human_plan
from mystock2.coach.tickets import freeze_tickets, latest_tickets
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
from mystock2.scoreboard.engine import LineRun, apply_splits, run_line
from mystock2.scoreboard.lines import BuyHoldProvider, batch_initial_state, create_batch, save_run
from mystock2.scoreboard.marketdata import DbMarketData
from mystock2.scoreboard.metrics import summarize
from mystock2.scoreboard.providers import HumanPlanProvider, TicketProvider
from mystock2.scoreboard.stats import ambiguous_dates, paired_diff
from mystock2.scoreboard.types import ExecProtocol, LineState, Lot

LOCAL_DIR = REPO_ROOT / "config" / "local"
REQUIRED_STRATEGY = ("k", "q_buy", "q_sell", "max_hold_days", "exit_q", "budget_slice")


ENV_NOW = "MYSTOCK2_ALLOW_NOW_OVERRIDE"


def _now(args):
    """当前时间。`--now` 只在显式设置环境变量 MYSTOCK2_ALLOW_NOW_OVERRIDE=1 时生效（测试/回放）；生产运行不得回填时间。"""
    if getattr(args, "now", None):
        if os.environ.get(ENV_NOW) != "1":
            raise ConfigError(f"--now 只用于测试：需显式设置环境变量 {ENV_NOW}=1（生产运行的记录时间/揭示时间/冻结时间必须是真实时钟）")
        return ensure_utc(args.now)
    return utc_now()


def protocol_hash(loc: "Local") -> str:
    """协议、费用档案、结算规则与名单的联合哈希：冻结后任何变化都会改变它（新协议＝新批次）。"""
    body = {
        "protocol": loc.protocol,
        "fees": [[r.profile_id, r.market, r.side, r.basis, r.currency, str(r.pct_fee), str(r.min_fee), str(r.flat_fee), str(r.cap_fee), str(r.tax_pct), str(r.round_step), r.valid_from, r.valid_to]
                 for r in loc.fee_rules],
        "settlement": {m: r.lag_sessions for m, r in sorted(loc.settlement.items())},
        "universe": [[e.code, e.tier, str(e.max_weight), e.max_lots, e.pending_confirmation] for e in loc.universe.entries],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def check_protocol(b: "Batch", loc: "Local", allow_drift: bool) -> list[str]:
    """运行前核验：本地协议/费用/名单哈希必须与批次创建时一致。允许漂移时返回提示标签（输出标 drift，不得当作正式记录）。"""
    if b.protocol_hash and protocol_hash(loc) != b.protocol_hash:
        if not allow_drift:
            raise ConfigError("本地协议/费用档案/结算规则/名单与批次创建时不一致：冻结后修改＝新协议版本＝新比较批次（请新建批次；仅调试可加 --allow-drift，结果不得作为正式记录）")
        return ["drift"]
    return []


DRIFT_SUFFIX, DRIFT_NOTE = "+drift", "[drift]"


def _pv(loc: "Local", drift: list[str]) -> str:
    """写进单据的协议版本：漂移放行（--allow-drift）下产生的单据带 +drift 后缀，记分牌据此把整次 run 标 pilot（审核 P1-14）。"""
    return str(loc.protocol.get("protocol_version", "pilot")) + (DRIFT_SUFFIX if drift else "")


def drift_records(conn, batch_id: str) -> bool:
    """批次里是否有在协议漂移放行下产生的单据或人类计划（这类记录不得进入正式比较）。"""
    return bool(conn.execute("SELECT 1 FROM ticket WHERE batch_id=? AND protocol_version LIKE ? LIMIT 1", (batch_id, "%" + DRIFT_SUFFIX)).fetchone()
                or conn.execute("SELECT 1 FROM intent WHERE batch_id=? AND note LIKE ? LIMIT 1", (batch_id, DRIFT_NOTE + "%")).fetchone())


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
    protocol_hash: str | None = None
    pilot: bool = True


def load_batch(conn, batch_id: str) -> Batch:
    r = conn.execute("SELECT * FROM comparison_batch WHERE batch_id=?", (batch_id,)).fetchone()
    if not r:
        raise ConfigError(f"批次不存在：{batch_id}")
    meta = json.loads(r["initial_state_json"]).get("_meta", {})
    lines = {x["kind"]: x["line_id"] for x in conn.execute("SELECT kind, line_id FROM strategy_line WHERE batch_id=?", (batch_id,))}
    return Batch(batch_id, meta["market"], r["currency"], date.fromisoformat(r["start_date"]), dec(r["e0"]), meta["codes"], lines, meta.get("protocol_hash"),
                 bool(meta.get("pilot", True)))


def sessions_between(market: str, after: date, upto: date) -> list[date]:
    return cal.session_days(market, cal.next_session(market, after), upto) if upto > after else []


def split_table(conn, codes: list[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in conn.execute("SELECT code, effective_at, ratio_num, ratio_den FROM corporate_action WHERE kind='SPLIT' ORDER BY effective_at"):
        if r["code"] in codes:
            out.setdefault(r["code"], []).append((ensure_utc(r["effective_at"]), r["ratio_num"], r["ratio_den"]))
    return out


def lot_sizes(conn, codes: list[str], on: date) -> dict[str, int]:
    """每手股数来自已核实的证券规则；规则未知/未核实**失败关闭**（不默认 1 股）。"""
    out = {}
    for c in codes:
        try:
            lot = rule_for(conn, c, on.isoformat()).lot_size
        except RuleUnknown as exc:
            raise ConfigError(f"{c} 的证券规则未知或未核实（{exc}）：不能模拟/生成可执行数量，请先录入并核实 security_rule") from exc
        if not lot:
            raise ConfigError(f"{c} 的 lot_size 未知：不能模拟/生成可执行数量")
        out[c] = lot
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
        provider = HumanPlanProvider(conn, batch_id=b.batch_id, line_id=line_id, market=b.market, codes=b.codes, fee_rules=loc.fee_rules,
                                     lot_sizes=lot_sizes(conn, b.codes, b.start), protocol=proto,
                                     constraint_handling=(loc.protocol.get("human_plan") or {}).get("constraint_handling") or "reject")
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
    apply_splits(st, split_table(conn, b.codes), b.market, target)           # 与引擎一致：先拆股、再释放结算（同一份「目标日开盘状态」）
    st.unsettled = [(d, a) for d, a in st.unsettled if d > target]       # 结算日当天视为已结算
    return st, equity


# ------------------------------------------------------------------ 命令
def _conn_ro(cfg):
    return dbmod.connect_ro(cfg.db_path)


def _opener(cfg, owner):
    return dbmod.connect_writer(cfg.db_path, owner)


def _parse_positions(raw: str | None, md, d0: date, loc: "Local", market: str, currency: str) -> tuple[dict[str, list[Lot]], list[str]]:
    """开账时属于交易仓的初始库存。值可为数量，或 {"qty":..,"cost":"每股成本","acquired":"YYYY-MM-DD"}。
    缺成本→估计为 D0 未复权收盘价（标 cost_estimated）；缺买入日→按 D0（标 acquired_unknown，影响时间止损的持有天数）。"""
    from mystock2.instruments.code_map import currency_of

    entries = {e.code: e for e in loc.universe.entries if e.market == market and e.tier == "trade"}
    out: dict[str, list[Lot]] = {}
    notes: list[str] = []
    for code, v in (json.loads(raw) if raw else {}).items():
        if code not in entries:
            raise ConfigError(f"{code} 不是名单内 {market} 市场的交易仓标的（核心仓/名单外库存不进入正式模拟线）")
        if currency_of(code) != currency:
            raise ConfigError(f"{code} 的币种 {currency_of(code)} 与批次币种 {currency} 不一致")
        spec = v if isinstance(v, dict) else {"qty": v}
        px = md.close(code, d0)
        if spec.get("cost") is None:
            if px is None:
                raise ConfigError(f"{code} 在 {d0} 缺少未复权收盘价，无法估算成本")
            notes.append(f"cost_estimated:{code}")
        if spec.get("acquired") is None:
            notes.append(f"acquired_unknown:{code}")
        out[code] = [Lot(dec(str(spec["qty"])), dec(str(spec["cost"])) if spec.get("cost") is not None else px,
                         date.fromisoformat(spec["acquired"]) if spec.get("acquired") else d0)]
    return out, notes


def cmd_batch_create(args) -> int:
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    market, d0 = args.market.upper(), date.fromisoformat(args.d0)
    codes = [e.code for e in loc.universe.entries if e.market == market and e.tier == "trade"]
    ro = _conn_ro(cfg)
    md = DbMarketData(ro)
    init = LineState(args.currency.upper(), dec(args.budget))
    lots, notes = _parse_positions(args.positions, md, d0, loc, market, init.currency)
    init.lots = lots
    init.other_equity = dec(args.other_equity)                      # 开账时交易仓的应收−应付（如已除息未到账的股息）
    pos_value = Decimal(0)
    for c in init.lots:
        px = md.close(c, d0)
        if px is None:
            raise ConfigError(f"{c} 在 {d0} 缺少未复权收盘价，无法计算 E0")
        pos_value += init.qty(c) * px
    e0 = init.cash + pos_value + init.other_equity                  # E0 = B + 交易仓库存市值 + 期初应收 − 应付（§6A.5）
    h = protocol_hash(loc)
    fr = ro.execute("SELECT protocol_hash, summary_json FROM protocol_freeze WHERE protocol_version=?", (str(loc.protocol.get("protocol_version")),)).fetchone()
    frozen = bool(fr and fr["protocol_hash"] == h and not json.loads(fr["summary_json"]).get("pilot"))
    pilot = not frozen or bool(loc.missing_required())
    if pilot:
        notes.append("pilot:protocol_not_frozen_or_incomplete")
    kinds = args.lines.split(",")
    with _opener(cfg, "scoreboard") as w:
        create_batch(w, args.id, loc.exec_protocol(), d0, init, e0, kinds, meta={"market": market, "codes": codes, "notes": notes, "protocol_hash": h, "pilot": pilot})
    print(f"batch={args.id} market={market} e0={to_db(e0)} lines={kinds} pilot={pilot} notes={notes}")
    return 0


def cmd_protocol_freeze(args) -> int:
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    missing = loc.missing_required()
    if missing and not args.pilot:
        print("协议不完整，拒绝冻结（缺失项使所有运行保持 pilot）：" + ", ".join(missing), file=sys.stderr)
        return 2
    h = protocol_hash(loc)
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
    now = _now(args)
    market = args.market.upper()
    ro = _conn_ro(cfg)
    b = load_batch(ro, args.batch)
    drift = check_protocol(b, loc, getattr(args, "allow_drift", False))
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
            t_close, t_open = iso_utc(cal.session(market, t).close_utc), iso_utc(cal.session(market, target).open_utc)
            split_pending = {r["code"] for r in ro.execute("SELECT code FROM corporate_action WHERE kind='SPLIT' AND effective_at>? AND effective_at<=?",
                                                         (t_close, t_open))}
            n_total = 0
            for kind in ("ai", "ai_veto"):                       # 每条线从自己的状态生成机械基础单（§6A.1）
                if kind not in b.lines:
                    continue
                state, equity = state_at_open(ro, loc, b, kind, target)
                drafts = decide(state, market=market, target_session=target, universe=loc.universe.entries, predictions=preds, rules=rules,
                                params=params, fee_rules=loc.fee_rules, trade_equity=equity, split_pending=split_pending)
                ids = freeze_tickets(cw, batch_id=b.batch_id, line_id=b.lines[kind], kind="line_sim", market=market, target_session=target, stage=args.stage,
                                     drafts=drafts, state_ref_type="line_state", state_ref=state.hash(), strategy_version=params.version,
                                     protocol_version=_pv(loc, drift), generated_at=now, now=_now(args), deadline_at=deadline)   # 冻结时间取提交时的真实时钟
                n_total += len(ids)
            run.note(tickets=n_total, pilot=bool(missing or b.pilot or drift), missing=missing, drift=bool(drift))
            # 密封：只输出回执与计数，不输出动作/限价/数量
            pilot = bool(missing or b.pilot or drift)
            print(f"run_id={run.run_id} stage={args.stage} market={market} data_as_of={t} target={target} tickets_frozen={n_total} pilot={pilot}")
    return 0


def cmd_coach_show(args) -> int:
    """受控揭示：打印 AI 单并写暴露日志（先 intent add，再 show）。"""
    cfg = load_config(args.config)
    market = args.market.upper()
    target = date.fromisoformat(args.target)
    ro = _conn_ro(cfg)
    kind = "line_sim" if not args.live else "live_guidance"
    lines = [r["line_id"] for r in ro.execute("SELECT DISTINCT line_id FROM ticket WHERE batch_id=? AND market=? AND target_session=? AND kind=? AND status='frozen'",
                                              (args.batch, market, target.isoformat(), kind))]
    latest = {(ln, c): r for ln in lines if "human_plan" not in ln for c, r in latest_tickets(ro, args.batch, ln, kind, market, target.isoformat()).items()}
    with _opener(cfg, "coach") as w:
        reveal(w, batch_id=args.batch, market=market, target_session=target, channel="coach_show", version_hashes=[r["frozen_hash"] for r in latest.values()],
               at=_now(args))
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
    check_protocol(b, loc, getattr(args, "allow_drift", False))
    target = date.fromisoformat(args.target)
    st, _ = state_at_open(ro, loc, b, "human_plan", target)
    print(json.dumps({"state_hash": st.hash(), **st.to_dict(), "tradable_cash": to_db(st.tradable_cash())}, ensure_ascii=False, indent=2))
    return 0


def cmd_intent_add(args) -> int:
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    ro = _conn_ro(cfg)
    b = load_batch(ro, args.batch)
    drift = check_protocol(b, loc, getattr(args, "allow_drift", False))
    market, target = b.market, date.fromisoformat(args.target)
    st, _ = state_at_open(ro, loc, b, "human_plan", target)
    now = _now(args)
    try:
        rule = rule_for(ro, args.code, target.isoformat())
    except RuleUnknown:
        rule = None
    handling = (loc.protocol.get("human_plan") or {}).get("constraint_handling") or "reject"
    with _opener(cfg, "coach") as w:
        res = record_intent(w, batch_id=b.batch_id, line_id=b.lines["human_plan"], market=market, code=args.code, target_session=target, action=args.action.upper(),
                            limit_price=args.limit, qty=args.qty, state=st, state_hash=st.hash(), now=now, deadline_at=cal.project_deadline(market, target),
                            constraint_handling=handling, rule=rule, fee_rules=loc.fee_rules,
                            note=(DRIFT_NOTE + " " + (args.note or "")).strip() if drift else args.note)
    print(f"intent={res.intent_id} qty={res.qty} truncated={res.truncated} seen_ai={int(res.seen_ai)} late_record={int(res.late_record)} notes={list(res.notes)}")
    return 0


def cmd_human_plan_freeze(args) -> int:
    """截止时把人类计划冻结为 human_plan 线的 line_sim 单（缺失＝无订单，不补动机）。"""
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    ro = _conn_ro(cfg)
    b = load_batch(ro, args.batch)
    drift = check_protocol(b, loc, getattr(args, "allow_drift", False))
    target = date.fromisoformat(args.target)
    deadline = cal.project_deadline(b.market, target)
    st, _ = state_at_open(ro, loc, b, "human_plan", target)
    plan = select_human_plan(ro, batch_id=b.batch_id, line_id=b.lines["human_plan"], market=b.market, target_session=target, codes=b.codes, deadline_at=deadline)
    drafts = plan_to_drafts(plan, lot_sizes(ro, b.codes, target))
    now = _now(args)
    with _opener(cfg, "coach") as w:
        ids = freeze_tickets(w, batch_id=b.batch_id, line_id=b.lines["human_plan"], kind="line_sim", market=b.market, target_session=target, stage="human_plan",
                             drafts=drafts, state_ref_type="line_state", state_ref=st.hash(), strategy_version="human", protocol_version=_pv(loc, drift),
                             generated_at=min(now, deadline), now=now, deadline_at=deadline)
    print(f"human_plan_tickets={len(ids)} plan_missing={sum(1 for p in plan.values() if 'plan_missing' in p['flags'])}")
    return 0


def cmd_scoreboard_run(args) -> int:
    """重算各线到 --end 并写入一个新 run（不覆盖旧 run）；打印与库内一致的指标（SB-01：CLI/API 同一数值）。"""
    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    ro = _conn_ro(cfg)
    b = load_batch(ro, args.batch)
    drift = check_protocol(b, loc, getattr(args, "allow_drift", False))
    if drift_records(ro, b.batch_id):                      # 漂移期产生的单据/计划混在批次里：整次 run 只能是 pilot，即使本次运行没有漂移
        drift = drift + ["drift_records"]
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
            save_run(w, run_id, b.batch_id, loc.exec_protocol(), [], runs, b.e0, b.currency, extra={"protocol_hash": protocol_hash(loc), "pilot": bool(b.pilot or drift), "drift": bool(drift)})
    summary = {}
    for line_id, run in runs.items():
        summary[line_id] = summarize(run.results, b.e0).as_dict()
    ai, human, hold = (runs.get(b.lines.get(k, "")) for k in ("ai", "human_plan", "buyhold"))
    out = {"run_id": run_id, "batch": b.batch_id, "pilot": bool(b.pilot or drift), "drift": bool(drift), "lines": summary}
    for name, other in (("ai_vs_human_plan", human), ("ai_vs_buyhold", hold)):
        if ai and other:
            d = paired_diff(ai.results, other.results, b.e0)
            ex = paired_diff(ai.results, other.results, b.e0, exclude_dates=ambiguous_dates(ai.results, other.results))
            out[name] = {"n": d.n, "coverage": str(d.coverage), "cumulative_diff": str(d.cumulative), "n_excluding_ambiguous": ex.n}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_veto_export(args) -> int:
    """导出否决输入包（人工通道）。导出本身是一次受控揭示：先记录人类计划再导出（§6A.2、M9 顺序约束）。"""
    from mystock2.assistant.veto import FIELD_WHITELIST, build_pack, record_packet

    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    ro = _conn_ro(cfg)
    b = load_batch(ro, args.batch)
    check_protocol(b, loc, getattr(args, "allow_drift", False))
    target = date.fromisoformat(args.target)
    if not args.confirm_fields:
        print("将外发的字段（白名单）：\n  " + "\n  ".join(FIELD_WHITELIST) + "\n\n不含账号、客户号、成交编号与绝对金额。确认后加 --confirm-fields 重新执行。", file=sys.stderr)
        return 2
    missing_plan = [c for c in b.codes if not ro.execute("SELECT 1 FROM intent WHERE batch_id=? AND line_id=? AND target_session=? AND code=?",
                                                         (b.batch_id, b.lines["human_plan"], target.isoformat(), c)).fetchone()]
    if missing_plan and not args.no_human_plan:
        print(f"导出会揭示 AI 单：请先为 {missing_plan} 记录人类计划（intent add，含 no_trade），或加 --no-human-plan 接受「揭示前无记录」后果。", file=sys.stderr)
        return 2
    line_id = b.lines["ai_veto"]
    base = latest_tickets(ro, b.batch_id, line_id, "line_sim", b.market, target.isoformat())
    if not base:
        print("没有可导出的机械基础单（先 coach run）", file=sys.stderr)
        return 2
    now = _now(args)
    st, equity = state_at_open(ro, loc, b, "ai_veto", target)
    md = DbMarketData(ro)
    summary = []
    for code in sorted(base):
        q = st.qty(code)
        px = md.close(code, cal.prev_session(b.market, target))
        # 外发只给粗粒度比例（2 位小数）与「是否持有」：绝对股数 × 价格 ÷ 比例会还原账户规模
        summary.append({"code": code, "holding": "yes" if q > 0 else "no",
                        "cash_pct": str((st.cash / equity).quantize(Decimal("0.01"))) if equity else None,
                        "exposure_pct": str(((q * px) / equity).quantize(Decimal("0.01"))) if (equity and px) else None})
    from mystock2.market.bars import get_daily
    ohlcv = {c: [{"date": r["session_date"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"]}
                 for r in get_daily(ro, c, cal.prev_session(b.market, target) - timedelta(days=40), cal.prev_session(b.market, target))[-20:]] for c in base}
    events = json.loads(Path(args.events).read_text(encoding="utf-8")) if args.events else []
    pack = build_pack(market=b.market, target_session=target, base_tickets=list(base.values()), line_state_summary=summary, ohlcv=ohlcv, events=events,
                      input_cutoff_at=now)
    with _opener(cfg, "assistant") as aw, _opener(cfg, "coach") as cw:
        record_packet(aw, pack, batch_id=b.batch_id, line_id=line_id, exported_at=now)
        reveal(cw, batch_id=b.batch_id, market=b.market, target_session=target, channel="veto_export", version_hashes=list(pack.base_hashes.values()), at=now)
    out = Path(args.out) if args.out else REPO_ROOT / "exports" / f"veto_{pack.pack_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(pack.markdown, encoding="utf-8")
    print(f"pack_id={pack.pack_id} file={out} evidence={len(pack.evidence_ids)}（已写暴露日志 channel=veto_export；此后记录的人类计划标 seen_ai=1）")
    return 0


def cmd_veto_import(args) -> int:
    from mystock2.assistant.veto import PROMPT_VERSION, import_veto

    cfg = load_config(args.config)
    loc = load_local(args.local_dir)
    ro = _conn_ro(cfg)
    row = ro.execute("SELECT * FROM veto_packet WHERE pack_id=?", (args.pack,)).fetchone()
    if not row:
        print(f"未知输入包：{args.pack}", file=sys.stderr)
        return 2
    b = load_batch(ro, row["batch_id"])
    drift = check_protocol(b, loc, getattr(args, "allow_drift", False))
    target = date.fromisoformat(row["target_session"])
    st, _ = state_at_open(ro, loc, b, "ai_veto", target)
    text = Path(args.response).read_text(encoding="utf-8")
    now = _now(args)                                                       # 读完响应之后再取时间：以导入完成时刻判截止
    with _opener(cfg, "veto") as vw:
        res = import_veto(vw, ro, pack_id=args.pack, response_text=text, provider="manual", model_id=args.model_id, prompt_version=args.prompt_version or PROMPT_VERSION,
                          now=now, deadline_at=cal.project_deadline(b.market, target), strategy_version=loc.strategy_params().version,
                          protocol_version=_pv(loc, drift), state_ref=st.hash())
    print(f"status={res.status} reason={res.reason} tickets={len(res.tickets)}")
    return 0 if res.status == "applied" else 1


def cmd_collect_quotes(args) -> int:
    """公开行情采集（yfinance：日线/小时线/汇率；无需账户授权）。逐标的主备源、失败/空/陈旧写回执，不记零。

    小时线官方只承诺约 60 天回溯，必须**每天**运行以从首日起归档（WP4.3）。
    """
    from mystock2.collectors.quotes import YFinanceSource, collect_daily, collect_fx, collect_hourly

    cfg = load_config(args.config)
    loc = load_local(args.local_dir) if not args.codes else None
    codes = args.codes.split(",") if args.codes else [e.code for e in loc.universe.entries]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    sources = [YFinanceSource()]
    results = {}
    with _opener(cfg, "core") as rl, _opener(cfg, "market") as w:
        with run_log(rl, "collect quotes", {"codes": codes, "start": args.start, "end": args.end, "hourly": args.hourly}) as run:
            for code in codes:
                results[code] = collect_daily(w, sources, code, start, end, run_id=run.run_id)
                if args.hourly:
                    results[f"{code}:hourly"] = collect_hourly(w, sources, code, start, end, run_id=run.run_id)
            for pair in (args.fx.split(",") if args.fx else []):
                results[pair] = collect_fx(w, sources, pair, start, end, run_id=run.run_id)
            failed = [k for k, v in results.items() if v["status"] == "failed"]
            if failed:
                run.partial("重试：" + ",".join(failed))
            print(f"run_id={run.run_id}")
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return 0 if not any(v["status"] == "failed" for v in results.values()) else 1


def cmd_collect_futu(args) -> int:
    """富途采集（只读查询；**需负责人授权连接真实账户**；采集器尚未对真实 OpenD 验证，首跑先小范围）。"""
    from mystock2.collectors.futu import (
        FutuTradeApi,
        collect_cash_flows,
        collect_deals,
        collect_order_fees,
        collect_snapshot,
    )

    cfg = load_config(args.config)
    firm = (cfg.raw.get("futu") or {}).get("security_firm")
    if not firm:
        print("配置缺少 futu.security_firm（须由负责人确认券商主体，不硬编码；见 D10）", file=sys.stderr)
        return 2
    api = FutuTradeApi(cfg.futu.host, cfg.futu.port, firm)
    interval = float((cfg.raw.get("futu") or {}).get("min_interval", 3.2))
    markets = list(cfg.markets)
    what = set(args.what.split(","))
    out = {}
    with _opener(cfg, "core") as rl, _opener(cfg, "ledger") as w:
        with run_log(rl, "collect futu", {"what": sorted(what), "markets": markets}) as run:
            if "deals" in what:
                out["deals"] = collect_deals(w, api, account_id=args.account_id, acc_id=args.acc_id, markets=markets, start=date.fromisoformat(args.start),
                                             end=date.fromisoformat(args.end), min_interval=interval)
            if "orders" in what:
                from mystock2.collectors.futu import collect_orders
                out["orders"] = collect_orders(w, api, account_id=args.account_id, acc_id=args.acc_id, markets=markets, start=date.fromisoformat(args.start),
                                               end=date.fromisoformat(args.end), min_interval=interval)
            if "fees" in what:
                out["fees"] = collect_order_fees(w, api, account_id=args.account_id, acc_id=args.acc_id, min_interval=interval,
                                                   assume_market_currency=args.assume_market_currency)
            if "snapshot" in what:
                out["snapshot"] = collect_snapshot(w, api, account_id=args.account_id, acc_id=args.acc_id, markets=markets, captured_at=utc_now(), min_interval=interval)
            if "cashflow" in what:
                tmap = yaml.safe_load(Path(args.cashflow_map).read_text(encoding="utf-8")) if args.cashflow_map else {}
                d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
                days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
                if args.cashflow_file:                                    # 离线重放（不连 OpenD、不等限频）
                    from mystock2.collectors.futu import FileCashflowApi
                    out["cashflow"] = collect_cash_flows(w, FileCashflowApi(args.cashflow_file), account_id=args.account_id, acc_id=args.acc_id, days=days, type_map=tmap or {},
                                                         sleep=lambda _s: None, min_interval=0)
                else:
                    out["cashflow"] = collect_cash_flows(w, api, account_id=args.account_id, acc_id=args.acc_id, days=days, type_map=tmap or {}, min_interval=interval)
            if any(not r.ok for r in out.values()):
                run.partial("; ".join(sc for r in out.values() for sc in r.failed_scopes))
            run.note(**{k: {"rows": v.rows, "inserted": v.inserted, "duplicate": v.duplicate, "pending": v.pending, "ok": v.ok} for k, v in out.items()})
            print(f"run_id={run.run_id}")
    text = json.dumps({k: {**v.__dict__, "recon_only": {a: str(b) for a, b in v.recon_only.items()}} for k, v in out.items()}, ensure_ascii=False, indent=2, default=str)
    print(text.replace(str(args.account_id), "<account>").replace(str(args.acc_id), "<acc>"))      # 终端输出不带账户号（日志/截图/粘贴会外泄）
    return 0 if all(r.ok and not r.conflicts for r in out.values()) else 1


def cmd_v1_import(args) -> int:
    """V1 历史数据一次性只读导入（需负责人授权访问 V1 运行库；先 --dry-run 看报告）。"""
    from mystock2.collectors.v1_import import run_import

    cfg = load_config(args.config)
    with _opener(cfg, "ledger") as w:
        rep = run_import(args.v1_db, w, account_id=args.account_id, dry_run=args.dry_run)
    out = {**rep.__dict__, "max_price_rounding": str(rep.max_price_rounding), "total_notional_rounding": str(rep.total_notional_rounding), "dry_run": args.dry_run}
    if args.archive and not args.dry_run:                    # 其余 V1 数据：订单、名称、档案、资金流向，以及（给了 ML 库时）小时线/盘前价/V1 前向预测
        from mystock2.collectors import v1_archive as va
        v1 = va.open_ro(args.v1_db)
        ml = va.open_ro(args.v1_ml_db) if args.v1_ml_db else None
        try:
            with _opener(cfg, "ledger") as lw, _opener(cfg, "market") as mw, _opener(cfg, "forecast") as fw:
                out["archive"] = {"names": va.import_names(v1, lw), "orders": va.import_orders(v1, lw, account_id=args.account_id), "profiles": va.import_profiles(v1, mw),
                                  "capital_flow": va.import_capital_flow(v1, mw)}
                if ml is not None:
                    out["archive"].update(hourly=va.import_hourly(ml, mw), preopen=va.import_preopen(ml, mw), v1_predictions=va.import_predictions(ml, fw))
        finally:
            v1.close()
            if ml is not None:
                ml.close()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if not rep.conflicts else 1


def cmd_ledger(args) -> int:
    """账本命令：开账、对账、状态（只读 SQL 取快照，写入走 ledger 写连接）。"""
    from mystock2.ledger.events import open_pending
    from mystock2.ledger.opening import record_opening
    from mystock2.ledger.projection import project
    from mystock2.ledger.reconcile import reconcile

    cfg = load_config(args.config)
    ro = _conn_ro(cfg)

    def snap(sid):
        if sid in (None, "latest"):
            # 默认只取带真实采集时刻的快照：V1 日快照只有日期（captured_at 是当日 23:59:59Z 的占位），不能当开账点或对账点
            return ro.execute("SELECT * FROM account_snapshot WHERE account_id=? AND source!='v1-date-only' ORDER BY captured_at DESC LIMIT 1", (args.account_id,)).fetchone()
        return ro.execute("SELECT * FROM account_snapshot WHERE snapshot_id=? AND account_id=?", (sid, args.account_id)).fetchone()

    if args.lcmd == "open":
        sn = snap(args.snapshot)
        if sn is None:
            print("找不到快照（先 collect futu --what snapshot）", file=sys.stderr)
            return 2
        if getattr(args, "at", None):                      # 倒推开账：由该快照倒推更早的 t0（成交/流水必须已先采集）
            from mystock2.ledger.opening import derive_opening
            positions, cash, warns = derive_opening(ro, args.account_id, sn["snapshot_id"], args.at)
            for w_ in warns:
                print("警告：" + w_, file=sys.stderr)
            if warns and not args.accept_warnings:
                print("存在警告：核对后加 --accept-warnings 才开账", file=sys.stderr)
                return 2
            with _opener(cfg, "ledger") as w:
                ids = record_opening(w, args.account_id, args.at, positions, cash, snapshot_id=None)
            print(f"opening_at={args.at}（由快照倒推）positions={len(positions)} cash_ccys={sorted(cash)} events={len(ids)}（期初现金是倒推残差，不是真实期初现金）")
            return 0
        positions = {r["code"]: r["qty"] for r in ro.execute("SELECT code, qty FROM snapshot_position WHERE snapshot_id=?", (sn["snapshot_id"],))}
        cash = {r["currency"]: r["cash"] for r in ro.execute("SELECT currency, cash FROM snapshot_cash WHERE snapshot_id=?", (sn["snapshot_id"],))}
        with _opener(cfg, "ledger") as w:
            ids = record_opening(w, args.account_id, sn["captured_at"], positions, cash, snapshot_id=sn["snapshot_id"])
        print(f"opening_at={sn['captured_at']} positions={len(positions)} cash_ccys={sorted(cash)} events={len(ids)}（开账点不可改；此前成交只作描述）")
        return 0
    if args.lcmd == "reconcile":
        sn = snap(args.snapshot)
        if sn is None:
            print("找不到快照", file=sys.stderr)
            return 2
        known = {str(k): dec(v) for k, v in ((cfg.raw.get("reconcile") or {}).get("known_cash_diffs") or {}).items()}
        rep = reconcile(ro, args.account_id, sn["snapshot_id"], known_cash_diffs=known)
        print(json.dumps({"snapshot": sn["snapshot_id"], "ok": rep.ok, "position_diffs": rep.position_diffs, "cash_diffs": rep.cash_diffs,
                          "baseline_cash_diffs": rep.baseline_cash_diffs,
                          "open_pending": rep.open_pending, "incomplete_fx_groups": rep.incomplete_fx_groups, "warnings": rep.warnings}, ensure_ascii=False, indent=2))
        return 0 if rep.ok else 1
    p = project(ro, args.account_id)
    print(json.dumps({"opening_at": p.opening_at, "positions": {k: str(v) for k, v in p.positions.items()}, "cash": {k: str(v) for k, v in p.cash.items()},
                      "receivable": {k: str(v) for k, v in p.receivable.items()}, "external_flow": {k: str(v) for k, v in p.external_flow.items()},
                      "pre_opening_events": p.pre_opening_events, "pending": len(open_pending(ro)), "warnings": p.warnings}, ensure_ascii=False, indent=2))
    return 0


def cmd_replay(args) -> int:
    from mystock2.replay.behavior import behavior_metrics
    from mystock2.replay.cards import build_cards, fills_and_fees, render_card_text
    from mystock2.replay.rounds import build_rounds

    cfg = load_config(args.config)
    ro = _conn_ro(cfg)
    cards = build_cards(ro, ro, args.account, code=args.code)
    if args.rcmd == "cards":
        for c in cards:
            print(f"=== {c.code} {c.side} {c.local_date} deal={c.deal_id}")
            print(render_card_text(c))
        print(f"（共 {len(cards)} 张；全部为事后诊断，不定义「当时应成交的最优价」）")
        return 0
    fills, fees = fills_and_fees(ro, args.account)
    rounds, _ = build_rounds([f for f in fills if not args.code or f["code"] == args.code], fees)
    for m in behavior_metrics(cards, rounds):
        print(f"{m.name}: {m.display}  (n={m.n}; {m.definition})")
    return 0


def cmd_forecast_run(args) -> int:
    """预测留档：对名单（或 --codes）在 [from, to] 的每个交易日生成次日预测（baseline / lgbm）。

    历史区间一律 `source_tag=rebuilt`（事后重建：用「现在已有」的行情，不等于当时可得，**不得与前向样本混算正式指标**）；
    只有最近一个交易日、且以真实时钟运行时才可 `--tag forward`（教练流程里的前向预测由 `coach run` 生成）。
    """
    from mystock2.forecast.baseline import ForecastUnavailable
    from mystock2.forecast.run import generate
    from mystock2.instruments.code_map import market_of

    cfg = load_config(args.config)
    codes = args.codes.split(",") if args.codes else [e.code for e in load_universe(Path(args.local_dir or REPO_ROOT / "config" / "local") / "universe.yaml", ()).entries]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if args.tag == "forward" and start != end:
        print("--tag forward 只用于最近一个已收盘交易日（--start 与 --end 相同）；历史区间只能是 rebuilt（事后重建）", file=sys.stderr)
        return 2
    params = None                                                          # 基线参数取默认（预测留档不依赖协议文件）；lgbm 同
    now = _now(args)
    stats: dict[str, dict] = {}
    with _opener(cfg, "core") as rl, _opener(cfg, "market") as mw, _opener(cfg, "forecast") as fw:
        with run_log(rl, "forecast run", {"codes": codes, "start": args.start, "end": args.end, "model": args.model, "tag": args.tag}) as run:
            for code in codes:
                st = stats.setdefault(code, {"ok": 0, "unavailable": 0})
                d = start
                while d <= end:
                    if cal.is_session(market_of(code), d):
                        try:
                            generate(mw, fw, code, d, input_cutoff_at=now, source_tag=args.tag, now=now, model=args.model,
                                     params=params if args.model == "baseline" else None)
                            st["ok"] += 1
                        except ForecastUnavailable:
                            st["unavailable"] += 1
                    d += timedelta(days=1)
            run.note(**{c: v for c, v in stats.items()})
            print(f"run_id={run.run_id}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def register(sub) -> None:
    ld = {"help": "本地私有配置目录（默认 config/local/）"}
    fc = sub.add_parser("forecast", help="预测留档").add_subparsers(dest="fcmd", required=True)
    fr = fc.add_parser("run", help="对区间内每个交易日生成并留档次日预测（历史区间为 rebuilt）")
    for a_, kw in (("--start", {"required": True}), ("--end", {"required": True}), ("--codes", {"help": "逗号分隔；缺省为名单内全部"}),
                   ("--model", {"default": "baseline", "choices": ["baseline", "lgbm"]}), ("--tag", {"default": "rebuilt", "choices": ["rebuilt", "forward"]}),
                   ("--local-dir", ld)):
        fr.add_argument(a_, **kw)
    fr.set_defaults(fn=cmd_forecast_run)
    b = sub.add_parser("batch", help="比较批次").add_subparsers(dest="bcmd", required=True)
    c = b.add_parser("create", help="创建批次（冻结完整状态包）")
    for a, kw in (("--id", {"required": True}), ("--market", {"required": True}), ("--d0", {"required": True}), ("--currency", {"required": True}),
                  ("--budget", {"required": True}), ("--other-equity", {"default": "0", "help": "开账时交易仓的应收−应付（计入 E0）"}), ("--lines", {"default": "ai,human_plan,buyhold"}), ("--positions", {"help": 'JSON：{"US.NVDA": 10}（开账时交易仓初始库存）'})):
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
    r.add_argument("--now", help="测试用：覆盖当前时间（须设置环境变量 MYSTOCK2_ALLOW_NOW_OVERRIDE=1）")
    r.add_argument("--allow-drift", action="store_true", help="允许本地协议与批次创建时不一致（仅调试；结果标 drift/pilot）")
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
    s2.add_argument("--allow-drift", action="store_true")
    s2.add_argument("--local-dir", **ld)
    s2.set_defaults(fn=cmd_intent_state)
    a = it.add_parser("add", help="记录结构化人类计划")
    for k, kw in (("--batch", {"required": True}), ("--target", {"required": True}), ("--code", {"required": True}),
                  ("--action", {"required": True, "choices": ["buy", "sell", "hold", "no_trade", "BUY", "SELL", "HOLD", "NO_TRADE"]}), ("--limit", {}),
                  ("--qty", {"type": int}), ("--note", {}), ("--now", {})):
        a.add_argument(k, **kw)
    a.add_argument("--allow-drift", action="store_true")
    a.add_argument("--local-dir", **ld)
    a.set_defaults(fn=cmd_intent_add)
    fh = it.add_parser("freeze", help="截止时冻结人类计划为 human_plan 线的单")
    fh.add_argument("--batch", required=True)
    fh.add_argument("--target", required=True)
    fh.add_argument("--now")
    fh.add_argument("--allow-drift", action="store_true")
    fh.add_argument("--local-dir", **ld)
    fh.set_defaults(fn=cmd_human_plan_freeze)
    cl = sub.add_parser("collect", help="采集").add_subparsers(dest="ccl", required=True)
    cq = cl.add_parser("quotes", help="公开行情采集（yfinance）")
    for k, kw in (("--start", {"required": True}), ("--end", {"required": True}), ("--codes", {"help": "逗号分隔的富途代码（默认取名单）"}),
                  ("--fx", {"help": "逗号分隔币对，如 USDHKD,USDCNY"})):
        cq.add_argument(k, **kw)
    cq.add_argument("--hourly", action="store_true", help="同时采集小时线（须每天运行以从首日归档）")
    cq.add_argument("--local-dir", help="本地私有配置目录（默认 config/local/）")
    cq.set_defaults(fn=cmd_collect_quotes)
    cf = cl.add_parser("futu", help="富途只读采集（需授权；未经真实验证）")
    for k, kw in (("--account-id", {"required": True}), ("--acc-id", {"required": True, "type": int}), ("--start", {"required": True}), ("--end", {"required": True}),
                  ("--what", {"default": "deals,fees,snapshot"}), ("--assume-market-currency", {"action": "store_true", "help": "订单费用接口无币种字段：按成交市场币种入账（2026-10-05 首跑核实：港股印花税 0.1%、美股佣金 0.99 与市场币种一致）"}),
                  ("--cashflow-file", {"help": "JSONL：离线重放原始资金流水（不连 OpenD）"}),
                  ("--cashflow-map", {"help": "YAML：资金流水类型→入账方式（DEPOSIT/WITHDRAW/INTEREST/TAX/RECON_ONLY/DIVIDEND/DIVIDEND_WHT）"})):
        cf.add_argument(k, **kw)
    cf.set_defaults(fn=cmd_collect_futu)
    v1 = sub.add_parser("v1", help="V1 数据").add_subparsers(dest="v1cmd", required=True)
    vi1 = v1.add_parser("import", help="只读导入 V1 成交与日快照")
    vi1.add_argument("--v1-db", required=True)
    vi1.add_argument("--account-id", required=True, help="遗留账户占位（须与将来 Futu 采集同一 account_id）")
    vi1.add_argument("--dry-run", action="store_true")
    vi1.add_argument("--archive", action="store_true", help="同时迁移订单/名称/档案/资金流向（以及 --v1-ml-db 里的小时线/盘前价/V1 前向预测）")
    vi1.add_argument("--v1-ml-db", help="V1 的 ML 库（data/ml/mystock_ml.db）的只读备份路径")
    vi1.set_defaults(fn=cmd_v1_import)
    lg = sub.add_parser("ledger", help="账本").add_subparsers(dest="lcmd", required=True)
    for name, h in (("open", "以某个券商快照开账（不可改）"), ("reconcile", "对账：账本重建 vs 券商快照"), ("status", "账本投影摘要")):
        x = lg.add_parser(name, help=h)
        x.add_argument("--account-id", required=True)
        if name != "status":
            x.add_argument("--snapshot", help="快照 id（默认最新）")
        if name == "open":
            x.add_argument("--at", help="倒推开账：t0（带时区 ISO，如 2025-01-02T00:00:00Z）；期初股数由快照减去 t0 之后的成交得出，期初现金是倒推残差")
            x.add_argument("--accept-warnings", action="store_true", help="倒推有警告（如期初数量为负）时，核对后仍要开账")
        x.set_defaults(fn=cmd_ledger)
    rp = sub.add_parser("replay", help="复盘（事后诊断）").add_subparsers(dest="rcmd", required=True)
    for name, h in (("cards", "逐笔复盘卡"), ("behavior", "行为指标（含样本量）")):
        x = rp.add_parser(name, help=h)
        x.add_argument("--account", required=True)
        x.add_argument("--code")
        x.set_defaults(fn=cmd_replay)
    vt = sub.add_parser("veto", help="LLM 否决（人工通道）").add_subparsers(dest="vcmd", required=True)
    ve = vt.add_parser("export", help="导出否决输入包（会揭示 AI 单）")
    for k, kw in (("--batch", {"required": True}), ("--target", {"required": True}), ("--events", {"help": "JSON 文件：带 published_at 的资料列表"}),
                  ("--out", {}), ("--now", {})):
        ve.add_argument(k, **kw)
    ve.add_argument("--confirm-fields", action="store_true", help="确认外发字段白名单")
    ve.add_argument("--no-human-plan", action="store_true", help="接受在未记录人类计划时导出（后果：揭示前无记录）")
    ve.add_argument("--allow-drift", action="store_true")
    ve.add_argument("--local-dir", **ld)
    ve.set_defaults(fn=cmd_veto_export)
    vi = vt.add_parser("import", help="导入人工否决结果（严格 JSON）")
    for k, kw in (("--pack", {"required": True}), ("--response", {"required": True}), ("--model-id", {}), ("--prompt-version", {}), ("--now", {})):
        vi.add_argument(k, **kw)
    vi.add_argument("--allow-drift", action="store_true")
    vi.add_argument("--local-dir", **ld)
    vi.set_defaults(fn=cmd_veto_import)
    sc = sub.add_parser("scoreboard", help="记分牌").add_subparsers(dest="scmd", required=True)
    rn = sc.add_parser("run", help="重算各线并写入新 run")
    rn.add_argument("--batch", required=True)
    rn.add_argument("--end", required=True)
    rn.add_argument("--allow-drift", action="store_true")
    rn.add_argument("--local-dir", **ld)
    rn.set_defaults(fn=cmd_scoreboard_run)
