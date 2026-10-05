"""开发期探索：基线 vs LightGBM+CQR 在公开历史行情上的滚动评估（描述性；窗口被反复查看，不构成「全新留出集」证据）。"""
import json
import time
from datetime import date
from decimal import Decimal

from mystock2.collectors.quotes import YFinanceSource
from mystock2.core import calendars as cal
from mystock2.forecast import baseline, lgbm_cqr
from mystock2.forecast.baseline import Bar
from mystock2.forecast.evaluate import improvement, meets_gate, rolling_eval
from mystock2.instruments.code_map import market_of

CODES = ["US.NVDA", "US.TSLA", "US.PDD", "HK.00700", "HK.09988", "HK.01810"]
src = YFinanceSource()
P = lgbm_cqr.LGBMParams(n_rounds=100, min_train=200)   # 固定参数，不做事后调整
end = date(2026, 10, 2)
out = {}
for code in CODES:
    bars_raw = src.daily(code, date(2020, 6, 1), end)
    m = market_of(code)
    bars = [Bar(b.session_date, Decimal(b.open), Decimal(b.high), Decimal(b.low), Decimal(b.close), Decimal(b.adj_close) if b.adj_close else None,
                Decimal(b.volume) if b.volume else None) for b in bars_raw if cal.is_session(m, b.session_date)]
    n = len(bars)
    start = max(500, n - 400)         # 最近约 400 个交易日做评估起点
    t0 = time.time()
    base = rolling_eval(bars, lambda h: baseline.predict(h), start=start, step=5)
    cand = rolling_eval(bars, lambda h: lgbm_cqr.predict(h, P), start=start, step=5)
    out[code] = {
        "n_bars": n, "n_eval": base.n, "base_total": base.total, "lgbm_total": cand.total, "improvement": improvement(base, cand),
        "base_cover": [base.cover_low, base.cover_high], "lgbm_cover": [cand.cover_low, cand.cover_high], "secs": round(time.time() - t0, 1),
    }
    print(code, json.dumps(out[code]), flush=True)
gate = meets_gate({c: v["improvement"] for c, v in out.items()})
print("GATE", json.dumps(gate))
