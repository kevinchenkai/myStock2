from pathlib import Path

import pytest

from mystock2.core.config import REPO_ROOT
from mystock2.instruments.code_map import CodeError, futu_to_yf, market_of, suggest_candidates, yf_to_futu
from mystock2.instruments.universe import load_universe, validate_universe


def test_code_map_rules():
    assert futu_to_yf("HK.00700") == "0700.HK"
    assert futu_to_yf("HK.09988") == "9988.HK"
    assert futu_to_yf("US.NVDA") == "NVDA"
    assert futu_to_yf("US.BRK.B") == "BRK-B"
    assert yf_to_futu("0700.HK") == "HK.00700"
    assert yf_to_futu("TSLA") == "US.TSLA"


@pytest.mark.parametrize("bad", ["NV", "0700", "NVDA", "hk.00700", "HK.700", "US.", "", "SH.600000"])
def test_bare_or_malformed_codes_rejected(bad):
    with pytest.raises(CodeError):
        market_of(bad)


def test_t16_bare_nv_rejected_with_candidates():
    rep = validate_universe({"instruments": [{"code": "NV", "tier": "trade"}]}, known_codes=["US.NVDA", "US.TSLA"])
    assert not rep.ok
    err = rep.errors[0]
    assert err["error"] == "invalid_code"
    assert "US.NVDA" in err["candidates"]            # 给候选但不静默猜测
    assert rep.entries == []


def test_bare_hk_number_gets_candidate():
    assert suggest_candidates("0700") == ["HK.00700"]


def test_trade_tier_without_params_is_not_executable():
    rep = validate_universe({"instruments": [
        {"code": "US.NVDA", "tier": "trade"},
        {"code": "US.TSLA", "tier": "trade", "max_weight": "0.1", "max_lots": 5},
        {"code": "HK.00700", "tier": "core"},
        {"code": "US.PDD", "tier": "trade", "max_weight": "0.1", "max_lots": 5, "pending_confirmation": True},
    ]})
    by = {e.code: e for e in rep.entries}
    assert rep.ok
    assert not by["US.NVDA"].executable and by["US.NVDA"].blockers == ("max_weight_missing", "max_lots_missing")
    assert by["US.TSLA"].executable
    assert not by["HK.00700"].executable                     # core 不出操作单
    assert by["US.PDD"].blockers == ("pending_confirmation",)
    assert by["HK.00700"].currency == "HKD" and by["US.TSLA"].currency == "USD"


@pytest.mark.parametrize("item,err", [
    ({"code": "US.NVDA", "tier": "boss"}, "invalid_tier"),
    ({"code": "US.NVDA", "tier": "trade", "max_weight": "1.5"}, "invalid_max_weight"),
    ({"code": "US.NVDA", "tier": "trade", "max_weight": "abc"}, "invalid_max_weight"),
    ({"code": "US.NVDA", "tier": "trade", "max_lots": 0}, "invalid_max_lots"),
    ({"code": "US.NVDA", "tier": "trade", "max_lots": 2.5}, "invalid_max_lots"),
])
def test_invalid_fields(item, err):
    assert validate_universe({"instruments": [item]}).errors[0]["error"] == err


def test_duplicates_and_empty():
    rep = validate_universe({"instruments": [{"code": "US.NVDA", "tier": "watch"}, {"code": "US.NVDA", "tier": "core"}]})
    assert rep.errors[0]["error"] == "duplicate"
    assert validate_universe({}).errors[0]["error"] == "empty_universe"


def test_template_universe_file_loads():
    rep = load_universe(REPO_ROOT / "config" / "universe.example.yaml")
    assert rep.ok
    assert {e.code for e in rep.entries} == {"US.NVDA", "HK.00700"}
    assert not next(e for e in rep.entries if e.code == "US.NVDA").executable   # pending_confirmation


def test_missing_file_message():
    with pytest.raises(FileNotFoundError, match="config/local"):
        load_universe(Path("/nonexistent/universe.yaml"))
