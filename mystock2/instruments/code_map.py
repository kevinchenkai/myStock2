"""富途代码 <-> yfinance 代码映射（纯函数）。

借用自 V1 `kevinchenkai/myStock` 的 `mystock/code_map.py`（提交 ca16e31ad8f78b23e2ceabbd58dba0a01b55e35e），
映射规则沿用，测试在 V2 重写；新增严格的格式校验与币种推断（V1 对无法识别的代码「原样返回」，V2 改为显式报错）。

| 市场 | 富途格式  | yfinance 格式 | 规则                                          |
| ---- | -------- | ------------- | --------------------------------------------- |
| 港股 | HK.00700 | 0700.HK       | 去前缀 → 数字去前导 0 后补足 4 位 → 加 .HK     |
| 美股 | US.NVDA  | NVDA          | 去前缀，直接用 ticker                          |
"""
from __future__ import annotations

import re

HK_RE = re.compile(r"^HK\.\d{5}$")
US_RE = re.compile(r"^US\.[A-Z][A-Z0-9]{0,5}([.\-][A-Z])?$")
CURRENCY = {"HK": "HKD", "US": "USD"}


class CodeError(ValueError):
    pass


def market_of(futu_code: str) -> str:
    """完整富途代码的市场；格式不合法则报错（不接受裸代码）。"""
    if HK_RE.match(futu_code or ""):
        return "HK"
    if US_RE.match(futu_code or ""):
        return "US"
    raise CodeError(f"不是合法的完整富途代码：{futu_code!r}（应如 US.NVDA、HK.00700）")


def currency_of(futu_code: str) -> str:
    return CURRENCY[market_of(futu_code)]


def normalize_hk_number(num: str) -> str:
    """去前导 0 后补足 4 位；超过 4 位保持原样（例：00700→0700，09988→9988，100000→100000）。"""
    stripped = num.lstrip("0") or "0"
    return stripped.zfill(4) if len(stripped) < 4 else stripped


def futu_to_yf(futu_code: str) -> str:
    m = market_of(futu_code)
    symbol = futu_code.split(".", 1)[1]
    if m == "HK":
        return f"{normalize_hk_number(symbol)}.HK"
    return symbol.replace(".", "-")  # yfinance 以连字符表示类股，如 BRK-B


def yf_to_futu(yf_symbol: str) -> str:
    """尽力还原：0700.HK→HK.00700；AAPL→US.AAPL。仅用于展示与导入核对，结果必须再经 market_of 校验。"""
    if not yf_symbol:
        raise CodeError("空代码")
    s = yf_symbol.strip().upper()
    if s.endswith(".HK"):
        return "HK." + (s[:-3].lstrip("0") or "0").zfill(5)
    return "US." + s.replace("-", ".")


def suggest_candidates(raw: str, known_codes: list[str] | tuple[str, ...] = ()) -> list[str]:
    """对「裸代码」给出候选完整代码（不静默猜测，只列出候选供人确认）。

    NV   -> 已知名单中以 NV 开头的美股（如 US.NVDA），以及 US.NV
    0700 -> HK.00700
    """
    r = (raw or "").strip().upper()
    out: list[str] = []
    if not r:
        return out
    if r.isdigit():
        out.append("HK." + r.lstrip("0").zfill(5) if r.lstrip("0") else "HK.00000")
        return out
    for code in known_codes:
        sym = code.split(".", 1)[-1].upper()
        if code.upper().startswith("US.") and (sym.startswith(r) or r.startswith(sym)):
            out.append(code)
    plain = f"US.{r}"
    if US_RE.match(plain) and plain not in out:
        out.append(plain)
    return out
