"""标的名单（universe）校验器（CO-01、T-16）。

- 只接受完整富途代码；裸代码（如 `NV`、`0700`）被拒绝并给出候选，**不静默猜测**。
- tier：core 核心仓（只看不动）/ trade 交易仓（出操作单）/ watch 观察。
- 交易仓缺 `max_weight` / `max_lots` 时标 `executable=False` 与原因——**缺失关键参数则不生成可执行数量**（实施方案 §5 WP6.1、D4）。
- 真实名单放 `config/local/universe.yaml`（忽略，不提交）；仓库只含模板。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from mystock2.core.money import MoneyError, dec
from mystock2.instruments.code_map import CodeError, currency_of, futu_to_yf, market_of, suggest_candidates

TIERS = ("core", "trade", "watch")


@dataclass(frozen=True)
class UniverseEntry:
    code: str
    market: str
    currency: str
    yf_symbol: str
    tier: str
    thesis: str | None
    max_weight: Decimal | None
    max_lots: int | None
    pending_confirmation: bool
    executable: bool
    blockers: tuple[str, ...]


@dataclass
class UniverseReport:
    entries: list[UniverseEntry] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_universe(data: dict[str, Any] | None, known_codes: list[str] | tuple[str, ...] = ()) -> UniverseReport:
    rep = UniverseReport()
    items = (data or {}).get("instruments")
    if not isinstance(items, list) or not items:
        rep.errors.append({"code": None, "error": "empty_universe", "message": "instruments 必须是非空列表"})
        return rep
    seen: set[str] = set()
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict) or "code" not in raw:
            rep.errors.append({"index": idx, "error": "missing_code", "message": "每项必须含 code"})
            continue
        code = str(raw["code"]).strip()
        try:
            market = market_of(code)
        except CodeError as exc:
            rep.errors.append({
                "code": code, "error": "invalid_code", "message": str(exc),
                "candidates": suggest_candidates(code, known_codes),
            })
            continue
        if code in seen:
            rep.errors.append({"code": code, "error": "duplicate", "message": "重复标的"})
            continue
        seen.add(code)
        tier = str(raw.get("tier", "")).strip()
        if tier not in TIERS:
            rep.errors.append({"code": code, "error": "invalid_tier", "message": f"tier 必须是 {TIERS}，得到 {tier!r}"})
            continue

        blockers: list[str] = []
        max_weight: Decimal | None = None
        max_lots: int | None = None
        try:
            if raw.get("max_weight") is not None:
                max_weight = dec(str(raw["max_weight"]))
                if not (Decimal(0) < max_weight <= Decimal(1)):
                    raise MoneyError("max_weight 必须在 (0, 1]")
        except MoneyError as exc:
            rep.errors.append({"code": code, "error": "invalid_max_weight", "message": str(exc)})
            continue
        if raw.get("max_lots") is not None:
            ml = raw["max_lots"]
            if isinstance(ml, bool) or not isinstance(ml, int) or ml <= 0:
                rep.errors.append({"code": code, "error": "invalid_max_lots", "message": "max_lots 必须是正整数"})
                continue
            max_lots = ml
        pending = bool(raw.get("pending_confirmation", False))

        if tier == "trade":
            if max_weight is None:
                blockers.append("max_weight_missing")
            if max_lots is None:
                blockers.append("max_lots_missing")
            if pending:
                blockers.append("pending_confirmation")
        executable = tier == "trade" and not blockers
        if tier == "trade" and blockers:
            rep.warnings.append({"code": code, "warning": "not_executable", "blockers": blockers})
        rep.entries.append(UniverseEntry(
            code=code, market=market, currency=currency_of(code), yf_symbol=futu_to_yf(code), tier=tier,
            thesis=raw.get("thesis"), max_weight=max_weight, max_lots=max_lots,
            pending_confirmation=pending, executable=executable, blockers=tuple(blockers),
        ))
    return rep


def load_universe(path: str | Path, known_codes: list[str] | tuple[str, ...] = ()) -> UniverseReport:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"名单文件不存在：{p}（真实名单放 config/local/，模板见 config/universe.example.yaml）")
    return validate_universe(yaml.safe_load(p.read_text(encoding="utf-8")), known_codes)
