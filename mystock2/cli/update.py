"""例行更新：`python -m mystock2 update --phase hk|us|pre`——一键「采集 → 预测 → 检查」，供 launchd/手动调用。

- 每一步是独立子进程（`python -m mystock2 …`），任何一步失败只记录、不中断后续步骤；最后汇总并以非零码退出，`--notify` 时弹 macOS 通知。
- 全是幂等的：重复运行不会重复入账（成交/费用/流水按规范键去重；行情按内容哈希版本化）。
- 只读采集：不解锁交易、不下单。富途 OpenD 没启动/没登录时，富途步骤失败，公开行情（yfinance）步骤照常执行。
- 账户号等私有参数在 config.yaml 的 `update:` 段（被 .gitignore 忽略），不进仓库。
阶段（本机时区 PDT 的建议时间见 scripts/install_launchd.sh）：
  hk  港股收盘后：富途（成交/订单/费用/快照/资金流水）＋港股行情＋预测＋对账
  us  美股收盘后：同上，美股
  pre 美股开盘前：富途快照/订单＋美股行情与小时线归档（小时线只给近 60 天，必须每天归档）
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

from mystock2.core import calendars as cal
from mystock2.core import db as dbmod
from mystock2.core.config import REPO_ROOT, ConfigError, load_config
from mystock2.core.timeutil import to_market_time
from mystock2.instruments.universe import load_universe

LOG_DIR = REPO_ROOT / "data" / "logs"
SETTLE_AFTER_CLOSE = timedelta(hours=1)    # 收盘后至少过这么久的成功运行才算「已完成」（成交/费用在收盘后还会陆续入账）
PHASES = {
    "hk": {"market": "HK", "futu": ("deals,orders,fees,snapshot", True), "forecast": True, "reconcile": True},
    "us": {"market": "US", "futu": ("deals,orders,fees,snapshot", True), "forecast": True, "reconcile": True},
    "pre": {"market": "US", "futu": ("orders,snapshot", False), "forecast": False, "reconcile": False},
}
FINAL_BUFFER = timedelta(minutes=30)


def run_step(argv: list[str], timeout: int = 3600) -> dict:
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=REPO_ROOT)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", "超时"
    tail = "\n".join((out + "\n" + err).strip().splitlines()[-6:])
    return {"rc": rc, "secs": round(time.time() - t0, 1), "tail": tail}


def codes_for(cfg, market: str, *, account: str | None = None, since: date | None = None) -> list[str]:
    """该市场需要更新行情（并检查新鲜度）的代码：名单 ∪ 当前持仓（账本投影与最新快照）∪ 回看窗口内有成交的标的。

    不再包含「账本里出现过的全部标的」：已清仓多年、已退市或长期停牌的标的永远没有新日线，会让每个时间点都判陈旧、
    全量重跑并发通知（审核 U-06）；它们已入库的历史行情不受影响。
    """
    codes: set[str] = set(_universe_codes())
    if cfg.db_path.exists():
        from mystock2.ledger.projection import project

        ro = dbmod.connect_ro(cfg.db_path)
        try:
            snap = ro.execute("SELECT snapshot_id FROM account_snapshot WHERE source!='v1-date-only' ORDER BY captured_at DESC LIMIT 1").fetchone()
            if snap is not None:
                codes |= {r[0] for r in ro.execute("SELECT code FROM snapshot_position WHERE snapshot_id=? AND CAST(qty AS REAL)!=0", (snap[0],))}
            if account:
                codes |= set(project(ro, account).positions)
            if since is not None:
                codes |= {r[0] for r in ro.execute("SELECT DISTINCT code FROM ledger_event WHERE event_type='FILL' AND event_at>=?", (since.isoformat(),))}
        finally:
            ro.close()
    return sorted(c for c in codes if c.startswith(market + "."))


def last_final_session(market: str, now: datetime) -> date:
    """已收盘（含缓冲）的最近一个交易日——日线应当已经有它的终值。"""
    d = now.astimezone(timezone.utc).date() + timedelta(days=1)
    for _ in range(14):
        d -= timedelta(days=1)
        if cal.is_session(market, d) and cal.session(market, d).close_utc + FINAL_BUFFER <= now:
            return d
    raise ConfigError(f"{market} 近 14 天没有已收盘的交易日（日历缺失？）")


def freshness(cfg, codes: list[str], market: str, now: datetime) -> list[str]:
    """日线新鲜度检查：每个代码应有「最近已收盘交易日」的终值（ok）行；缺的列出。"""
    if not cfg.db_path.exists() or not codes:
        return []
    want = last_final_session(market, now).isoformat()
    ro = dbmod.connect_ro(cfg.db_path)
    try:
        have = {r[0]: r[1] for r in ro.execute("SELECT code, MAX(session_date) FROM quote_daily WHERE quality='ok' GROUP BY code")}
    finally:
        ro.close()
    return [f"{c}: 最新终值 {have.get(c, '无')} < 应有 {want}" for c in codes if (have.get(c) or "") < want]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def next_open_session(market: str, now: datetime) -> date:
    """开盘时刻晚于 now 的第一个交易日（按交易所本地日期，不按 UTC 日期，审核 U-03）。"""
    d = to_market_time(now, market).date()
    if cal.is_session(market, d) and cal.session(market, d).open_utc > now:
        return d
    return cal.next_session(market, d)


def target_key(phase: str, market: str, now: datetime) -> str:
    """本阶段要达成的目标：hk/us＝该市场最近已收盘交易日；pre＝下一个将要开盘的美股交易日。"""
    return next_open_session(market, now).isoformat() if phase == "pre" else last_final_session(market, now).isoformat()


def load_state() -> dict:
    try:
        st = json.loads((LOG_DIR / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) else {}                  # 内容是 null/列表等：当作没有记录（重跑，安全侧）


def save_state(state: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LOG_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LOG_DIR / "state.json")                        # 原子替换：写到一半被杀不会留下坏文件


def state_key(phase: str, cfg) -> str:
    """状态按「阶段＋库」区分：用另一份配置（另一个库）跑成功，不能让真实库的同一目标被跳过（审核 U-09）。"""
    return f"{phase}@{cfg.db_path}"


def already_done(phase: str, market: str, now: datetime, rec, stale: list[str]) -> str | None:
    """增量检查：同一目标已**完整**成功完成、且日线没有陈旧 → 返回跳过原因；否则 None（需要运行）。
    hk/us 的成功记录必须发生在收盘 + 1 小时之后才算数（收盘后成交/费用还会陆续到）。
    「完整」＝富途步骤与资金流水都实际跑过；--no-futu、未配置账户、缺映射的成功不算（审核 U-01）。格式不对的记录一律当作需要运行。"""
    key = target_key(phase, market, now)
    if not isinstance(rec, dict) or not rec.get("ok") or rec.get("complete") is not True or rec.get("target") != key or stale:
        return None
    try:
        at = datetime.fromisoformat(str(rec["at"]))
        if at.tzinfo is None:
            return None
    except (KeyError, ValueError):
        return None
    if phase != "pre":
        close = cal.session(market, date.fromisoformat(key)).close_utc
        if at < close + SETTLE_AFTER_CLOSE:
            return None
    return f"目标 {key} 已于 {rec['at']} 完整成功完成，数据无陈旧，跳过"


def notify(title: str, text: str) -> None:
    try:
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "{title}"'], timeout=10, capture_output=True)
    except Exception:  # noqa: BLE001 — 通知失败不影响更新本身
        pass


def _acquire_lock(phase: str):
    """独占锁。锁文件里写持有者（pid、阶段、开始时间），拿不到锁时能说清是谁在跑、跑了多久（审核 U-05）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOG_DIR / "update.lock", "a+")             # noqa: SIM115 — 进程结束时释放；a+ 不截断别人的持有者信息
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.seek(0)
        holder = lock.read().strip()
        lock.close()
        return None, holder
    lock.seek(0)
    lock.truncate()
    lock.write(json.dumps({"pid": os.getpid(), "phase": phase, "at": _now().isoformat()}))
    lock.flush()
    return lock, ""


def cmd_update(args) -> int:
    """入口：任何未预期的异常（日历越界、解释器缺失、状态文件损坏……）也要留日志、记失败、按需通知，不静默退出（审核 U-04）。"""
    try:
        return _update(args)
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        print(f"例行更新异常中止：{msg}", file=sys.stderr)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / f"update_{_now().date().isoformat()}_{args.phase}_error.json").write_text(
                json.dumps({"phase": args.phase, "at": _now().isoformat(), "error": msg}, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            if args.notify:
                notify("myStock2 更新", f"{args.phase}：异常中止（{type(exc).__name__}），详见 data/logs/")
        return 1


def _update(args) -> int:
    cfg = load_config(args.config)
    ph = PHASES[args.phase]
    ucfg = cfg.raw.get("update") or {}
    account, acc_id = ucfg.get("account_id"), ucfg.get("acc_id")
    now = _now()
    today = now.date()
    if args.lookback is not None and not 1 <= args.lookback <= 365:
        raise ConfigError(f"--lookback 应在 1–365 天之间：{args.lookback}")
    lookback = int(args.lookback if args.lookback is not None else ucfg.get("lookback_days", 10))
    start, end = (today - timedelta(days=lookback)).isoformat(), today.isoformat()
    py = [sys.executable, "-m", "mystock2"] + (["--config", args.config] if args.config else [])
    market = ph["market"]
    codes = codes_for(cfg, market, account=account, since=today - timedelta(days=lookback + 5))
    lock, holder = _acquire_lock(args.phase)
    if lock is None:
        print(f"另一个更新正在运行，本次跳过（持有者：{holder or '未知'}）", file=sys.stderr)
        with open(LOG_DIR / "update_lock_skipped.log", "a", encoding="utf-8") as f:
            f.write(f"{now.isoformat()} {args.phase} skipped; holder={holder}\n")
        try:
            started = datetime.fromisoformat(json.loads(holder)["at"])
            if args.notify and now - started > timedelta(hours=2):
                notify("myStock2 更新", f"{args.phase}：上一次更新已运行超过 2 小时仍未结束，本次跳过")
        except (ValueError, KeyError, TypeError):
            pass
        return 0

    state = load_state()
    skey = state_key(args.phase, cfg)
    stale0 = freshness(cfg, codes, market, now) if codes else []
    force = args.force or args.lookback is not None            # 显式给了回看天数＝手动补漏：不被增量检查跳过（审核 U-02）
    why = None if force else already_done(args.phase, market, now, state.get(skey), stale0)
    if why:
        print(json.dumps({"phase": args.phase, "skipped": True, "reason": why}, ensure_ascii=False))
        return 0

    steps: list[tuple[str, list[str]]] = []          # (名称, argv)
    incomplete: list[str] = []                       # 没有实际跑的步骤：本次成功不算「完整」，后续时间点不会因此跳过
    if not args.no_futu and account and acc_id:
        what, with_cash = ph["futu"]
        base = py + ["collect", "futu", "--account-id", str(account), "--acc-id", str(acc_id), "--start", start, "--end", end]
        steps.append(("futu:" + what, base + ["--what", what, "--assume-market-currency"]))
        cmap = REPO_ROOT / "config" / "local" / "futu_cashflow_map.yaml"
        if with_cash and cmap.exists():
            steps.append(("futu:cashflow", base + ["--what", "cashflow", "--cashflow-map", str(cmap)]))
        elif with_cash:
            print(f"提示：缺少资金流水类型映射 {cmap}，跳过资金流水", file=sys.stderr)
            incomplete.append("futu:cashflow（缺映射）")
    elif args.no_futu:
        incomplete.append("futu（--no-futu）")
    else:
        print("提示：config.yaml 的 update.account_id / update.acc_id 未配置，跳过富途步骤", file=sys.stderr)
        incomplete.append("futu（未配置账户）")
    if codes:
        cs = ",".join(codes)
        hourly_days = min(max(lookback, 5), 58)               # 小时线供应商只给约 60 天：跟随回看天数补缺口（审核 U-07）
        steps.append(("quotes:daily", py + ["collect", "quotes", "--codes", cs, "--start", start, "--end", end, "--fx", "USDHKD,USDCNY"]))
        steps.append(("quotes:hourly", py + ["collect", "quotes", "--codes", cs, "--start", (today - timedelta(days=hourly_days)).isoformat(), "--end", end, "--hourly"]))
    if ph["forecast"]:
        ucodes = ",".join(c for c in codes if c in _universe_codes())
        if ucodes:
            f0 = (today - timedelta(days=4)).isoformat()
            for model in ("baseline", "lgbm"):
                steps.append((f"forecast:{model}", py + ["forecast", "run", "--codes", ucodes, "--start", f0, "--end", end, "--model", model]))
    if ph["reconcile"] and account:
        steps.append(("ledger:reconcile", py + ["ledger", "reconcile", "--account-id", str(account)]))

    results, failed = [], []
    for name, argv in steps:
        r = run_step(argv)
        r["name"] = name
        results.append(r)
        print(f"[{name}] rc={r['rc']} {r['secs']}s", file=sys.stderr)
        if r["rc"] != 0:
            failed.append(name if name != "ledger:reconcile" else "ledger:reconcile（有差异或待匹配，见日志）")
    stale = freshness(cfg, codes, market, now) if codes else []
    summary = {"phase": args.phase, "at": now.isoformat(), "start": start, "end": end, "codes": len(codes), "failed": failed, "stale": stale,
               "incomplete": incomplete, "steps": [{k: r[k] for k in ("name", "rc", "secs")} for r in results]}
    stamp = now.strftime("%H%M")                               # 同日同阶段多次运行各留一份日志（不覆盖前一次的失败细节）
    (LOG_DIR / f"update_{today.isoformat()}_{args.phase}_{stamp}.json").write_text(json.dumps({**summary, "details": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    bad = bool(failed or stale)
    state[skey] = {"target": target_key(args.phase, market, now), "at": _now().isoformat(), "ok": not bad, "complete": not incomplete}
    save_state(state)
    if bad and args.notify:
        notify("myStock2 更新", f"{args.phase}：失败 {len(failed)} 步，陈旧 {len(stale)} 个标的，详见 data/logs/")
    return 1 if bad else 0


def _universe_codes() -> set[str]:
    p = REPO_ROOT / "config" / "local" / "universe.yaml"
    return {e.code for e in load_universe(p, ()).entries} if p.exists() else set()


def register(sub) -> None:
    u = sub.add_parser("update", help="例行更新：采集 → 预测 → 检查（幂等；供 launchd/手动）")
    u.add_argument("--phase", required=True, choices=sorted(PHASES))
    u.add_argument("--lookback", type=int, help="回看天数（默认 config 的 update.lookback_days 或 10）")
    u.add_argument("--no-futu", action="store_true", help="跳过富途步骤（OpenD 未启动时只更新公开行情）")
    u.add_argument("--force", action="store_true", help="忽略增量检查，强制完整运行")
    u.add_argument("--notify", action="store_true", help="失败/陈旧时弹 macOS 通知")
    u.set_defaults(fn=cmd_update)
