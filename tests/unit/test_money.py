from decimal import Decimal

import pytest

from mystock2.core.money import (
    Money,
    MoneyError,
    dec,
    floor_to_lots,
    quantize,
    round_limit_price,
    to_db,
)


def test_float_is_rejected():
    with pytest.raises(MoneyError):
        dec(0.1)
    with pytest.raises(MoneyError):
        dec(True)


def test_nan_and_garbage_rejected():
    for bad in ("NaN", "Infinity", "abc", ""):
        with pytest.raises(MoneyError):
            dec(bad)


def test_decimal_has_no_float_drift():
    assert dec("0.1") + dec("0.2") == dec("0.3")


def test_to_db_is_canonical():
    assert to_db("1.2300") == "1.23"
    assert to_db("1E+2") == "100"
    assert to_db("-0.000") == "0"
    assert to_db(Decimal("123456789012345678.123456789")) == "123456789012345678.123456789"
    assert "E" not in to_db("0.00000001")


def test_quantize_half_even_and_step():
    assert quantize("10.125", "0.01") == Decimal("10.12")   # 银行家舍入
    assert quantize("10.135", "0.01") == Decimal("10.14")
    assert quantize("7", "5") == Decimal("5")
    with pytest.raises(MoneyError):
        quantize("1", "0")


def test_limit_price_rounding_is_conservative():
    assert round_limit_price("10.037", "0.01", "BUY") == Decimal("10.03")    # 买价向下
    assert round_limit_price("10.033", "0.01", "SELL") == Decimal("10.04")   # 卖价向上
    assert round_limit_price("10.03", "0.01", "BUY") == Decimal("10.03")     # 已合法则不变
    with pytest.raises(MoneyError):
        round_limit_price("1", "0.01", "HOLD")


def test_floor_to_lots():
    assert floor_to_lots("1999", 500) == Decimal("1500")
    assert floor_to_lots("499", 500) == Decimal("0")


def test_money_same_currency_arithmetic_and_cross_currency_forbidden():
    a, b = Money("100", "usd"), Money("25.5", "USD")
    assert (a + b) == Money("125.5", "USD")
    assert (a - b).amount == Decimal("74.5")
    with pytest.raises(MoneyError):
        a + Money("1", "HKD")
    with pytest.raises(MoneyError):
        Money("1", "US")
    assert str(Money("1.50", "HKD")) == "1.5 HKD"
