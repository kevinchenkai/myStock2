"""生成**合成**演示库并可选启动只读 Web（用于截图、UI 走查；不含任何真实账户信息，全部为合成测试值）。

    python scripts/make_demo_db.py --out /tmp/demo/demo.db                 # 只生成
    python scripts/make_demo_db.py --out /tmp/demo/demo.db --serve 8890    # 生成后在 127.0.0.1:8890 启动（Ctrl+C 停止）

- 固定随机种子：同参数生成同一个库；时钟固定在 `--now`（默认 2026-10-05T06:00Z），页面新鲜度可复现。
- 只用账本/行情/预测的公开写入函数（走 TABLE_OWNERS 授权），不碰 `data/` 下的真实库。
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mystock2.core import calendars as cal  # noqa: E402
from mystock2.core import db as dbmod  # noqa: E402
from mystock2.core.config import parse_config  # noqa: E402
from mystock2.core.timeutil import iso_utc  # noqa: E402
from mystock2.ledger.events import (  # noqa: E402
    EventDraft,
    ensure_account,
    fee_key,
    fill_key,
    flow_key,
    post_dividend,
    post_event,
    post_fx,
)
from mystock2.ledger.opening import create_snapshot, record_opening  # noqa: E402
from mystock2.ledger.projection import project  # noqa: E402
from mystock2.market import fx as fxmod  # noqa: E402
from mystock2.market.bars import DailyBar, put_daily  # noqa: E402

UTC = timezone.utc
ACCT = "demo"
CODES = {  # 代码 → (中文名, 起始价)
    "US.NVDA": ("英伟达（合成）", "120"), "US.TSLA": ("特斯拉（合成）", "240"), "US.PDD": ("拼多多（合成）", "110"),
    "HK.00700": ("腾讯控股（合成）", "380"), "HK.09988": ("阿里巴巴（合成）", "85"), "HK.09926": ("康方生物（合成）", "60"),
}
LOT = {"US": 1, "HK": 100}
Q2 = Decimal("0.01")


def _q(x: Decimal, step: Decimal = Q2) -> Decimal:
    return x.quantize(step, rounding=ROUND_HALF_EVEN)


def sessions(market: str, start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if cal.is_session(market, d):
            out.append(d)
        d += timedelta(days=1)
    return out


def close_utc(market: str, d: date) -> datetime:
    return datetime.combine(d, time(8, 0) if market == "HK" else time(20, 0), UTC)


def build(out: Path, now: datetime, seed: int = 7) -> Path:
    rng = random.Random(seed)
    if out.exists():
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    dbmod.migrate(out)
    start, t0_day, end = date(2025, 1, 2), date(2025, 6, 30), (now - timedelta(days=1)).date()
    received = now - timedelta(hours=1)

    # 行情：几何随机游走（合成）
    closes: dict[str, dict[date, Decimal]] = {}
    mk = dbmod.connect_writer(out, "market")
    for code, (_, p0) in CODES.items():
        m = code.split(".")[0]
        px, bars, series = Decimal(p0), [], {}
        for d in sessions(m, start, end):
            o = _q(px * Decimal(str(1 + rng.gauss(0, 0.006))))
            c = _q(o * Decimal(str(1 + rng.gauss(0.0004, 0.022))))
            h = _q(max(o, c) * Decimal(str(1 + abs(rng.gauss(0, 0.008)))))
            lo = _q(min(o, c) * Decimal(str(1 - abs(rng.gauss(0, 0.008)))))
            bars.append(DailyBar(code, d, str(o), str(h), str(lo), str(c), str(c), str(rng.randint(5, 60) * 100000)))
            series[d], px = c, c
        put_daily(mk, bars, source="synthetic", received_at=received)
        closes[code] = series
    rate = Decimal("7.18")
    for d in sessions("US", start, end):
        rate = _q(rate * Decimal(str(1 + rng.gauss(0, 0.0015))), Decimal("0.0001"))
        fxmod.put_rate(mk, "USDCNY", d, str(rate), source="synthetic", event_at=f"{d}T21:00:00Z", received_at=received)
        fxmod.put_rate(mk, "USDHKD", d, str(_q(Decimal("7.8") + Decimal(str(rng.gauss(0, 0.004))), Decimal("0.0001"))),
                       source="synthetic", event_at=f"{d}T21:00:00Z", received_at=received)
    mk.close()

    # 账本：开账（2025-06-30）＋其后买卖/费用/股息/入金/换汇
    led = dbmod.connect_writer(out, "ledger")
    with patch("mystock2.ledger.events.utc_now", lambda: received), patch("mystock2.ledger.opening.utc_now", lambda: received):
        ensure_account(led, ACCT, "futu", "REAL", "USD")
        t0 = datetime.combine(t0_day, time(0, 0), UTC)
        open_pos = {"US.NVDA": "100", "HK.00700": "400", "US.TSLA": "30"}
        open_cash = {"USD": "30000", "HKD": "150000"}
        sid = create_snapshot(led, ACCT, t0, "futu", {"US.NVDA": {"qty": "100", "average_cost": "105"}, "HK.00700": {"qty": "400", "average_cost": "350"},
                                                      "US.TSLA": {"qty": "30"}}, {c: {"cash": v} for c, v in open_cash.items()})
        record_opening(led, ACCT, t0, open_pos, open_cash, snapshot_id=sid)
        held = {c: Decimal(q) for c, q in open_pos.items()}
        cash = {c: Decimal(v) for c, v in open_cash.items()}
        n, orders = 0, []
        for code in CODES:
            m = code.split(".")[0]
            ccy = "HKD" if m == "HK" else "USD"
            days = [d for d in sessions(m, t0_day + timedelta(days=1), end)]
            for d in sorted(rng.sample(days, 22)):
                px = closes[code][d]
                qty = Decimal(LOT[m] * (rng.randint(1, 4) if m == "HK" else rng.randint(5, 40)))
                side = "SELL" if held.get(code, 0) >= qty and rng.random() < 0.45 else "BUY"
                if side == "BUY" and cash[ccy] < qty * px * Decimal("1.01"):
                    continue
                n += 1
                deal, at = f"D{n:04d}", close_utc(m, d) - timedelta(hours=2, minutes=rng.randint(0, 90))
                signed = qty if side == "BUY" else -qty
                price = _q(px * Decimal(str(1 + rng.gauss(0, 0.004))))
                post_event(led, EventDraft(fill_key(ACCT, deal), ACCT, "FILL", at, ccy, code=code, price=str(price), qty_delta=str(signed),
                                           cash_delta=str(_q(-signed * price)), ref_deal_id=deal, ref_order_id=f"O{n:04d}"), received_at=received)
                f = max(Decimal("1.00"), _q(qty * price * Decimal("0.0005")))
                post_event(led, EventDraft(fee_key(ACCT, deal, "commission"), ACCT, "FEE", at, ccy, cash_delta=str(-f), ref_deal_id=deal), received_at=received)
                held[code] = held.get(code, Decimal(0)) + signed
                cash[ccy] += -signed * price - f
                orders.append((f"O{n:04d}", m, code, side, str(price), str(qty), "FILLED_ALL", at))
                if rng.random() < 0.25:
                    n += 1
                    orders.append((f"O{n:04d}", m, code, side, str(_q(price * Decimal("0.97"))), str(qty), "CANCELLED_ALL", at - timedelta(minutes=30)))
        for i, (code, gross, tax) in enumerate((("US.NVDA", "4.00", "0.40"), ("HK.00700", "1360.00", None), ("US.TSLA", "0", None))):
            if gross != "0":
                d = date(2025, 9, 15) + timedelta(days=30 * i)
                ccy = "HKD" if code.startswith("HK") else "USD"
                post_dividend(led, ACCT, f"g{i}", code, ccy, accrual_at=f"{d}T12:00:00Z", gross=gross, payment_at=f"{d}T12:00:00Z", withholding_tax=tax)
        post_event(led, EventDraft(flow_key(ACCT, "dep1", "DEPOSIT"), ACCT, "DEPOSIT", "2025-11-03T15:00:00Z", "USD", cash_delta="20000"), received_at=received)
        post_event(led, EventDraft(flow_key(ACCT, "wd1", "WITHDRAW"), ACCT, "WITHDRAW", "2026-04-01T03:00:00Z", "HKD", cash_delta="-30000"), received_at=received)
        post_fx(led, ACCT, "fx1", "2026-01-12T15:00:00Z", "USD", "5000", "HKD", "39000", received_at=received)
        for oid, m, code, side, price, qty, status, at in orders:
            led.execute("INSERT INTO broker_order(account_id, order_id, market, code, side, order_type, status, price, qty, dealt_qty, dealt_avg_price, created_at, "
                        "updated_at, time_trust, source, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ACCT, oid, m, code, side, "NORMAL", status, price, qty, qty if status == "FILLED_ALL" else "0",
                         price if status == "FILLED_ALL" else None, iso_utc(at), iso_utc(at), "exact", "synthetic", iso_utc(received)))
        for code, (name, _) in CODES.items():
            led.execute("INSERT INTO instrument_name(code, name, source, updated_at) VALUES (?,?,?,?)", (code, name, "synthetic", iso_utc(received)))
        snap_at = now - timedelta(hours=2)
        p = project(led, ACCT, as_of=snap_at)
        pos = {c: {"qty": str(q), "average_cost": str(_q(closes[c][max(closes[c])] * Decimal("0.9")))} for c, q in p.positions.items()}
        create_snapshot(led, ACCT, snap_at, "futu", pos, {c: {"cash": str(v)} for c, v in p.cash.items()})
    led.close()

    # 预测（事后重建，rebuilt）：最近约 80 个交易日的基线预测
    from mystock2.forecast.run import generate
    mw, fw = dbmod.connect_writer(out, "market"), dbmod.connect_writer(out, "forecast")
    for code in CODES:
        m = code.split(".")[0]
        for d in sessions(m, end - timedelta(days=115), end):
            generate(mw, fw, code, d, input_cutoff_at=received, source_tag="rebuilt", now=received)
    mw.close()
    fw.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="演示库路径（不要指向 data/ 下的真实库）")
    ap.add_argument("--now", default="2026-10-05T06:00:00+00:00", help="固定时钟（ISO，带时区）")
    ap.add_argument("--serve", type=int, help="生成后在 127.0.0.1:<端口> 启动只读 Web（开发演示用 8890）")
    ap.add_argument("--keep", action="store_true", help="库已存在时不重建，直接启动")
    a = ap.parse_args()
    out, now = Path(a.out).resolve(), datetime.fromisoformat(a.now).astimezone(UTC)
    if (Path(__file__).resolve().parents[1] / "data") in out.parents:
        print("拒绝：演示库不能放在 data/ 下（那里是真实私有数据）", file=sys.stderr)
        return 2
    if not (a.keep and out.exists()):
        build(out, now)
        print(f"演示库（合成）：{out}", file=sys.stderr)
    if a.serve:
        from mystock2.web.app import serve
        cfg = parse_config({"futu": {"host": "127.0.0.1", "port": 11111, "trd_env": "REAL"}, "collect": {"markets": ["HK", "US"]},
                           "db": {"path": str(out)}, "web": {"host": "127.0.0.1", "port": a.serve}}, base=out.parent)
        serve(cfg, clock=lambda: now, universe_path="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
