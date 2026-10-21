from datetime import date

import pytest

pytest.importorskip("ibapi", reason="缺少 ibapi")

from vnpy.trader.constant import Exchange  # noqa: E402

from vnpy_ib.future_alias import is_alias, resolve_month  # noqa: E402
from vnpy_ib.ib_gateway import generate_ib_contract  # noqa: E402


@pytest.mark.parametrize(
    ("symbol", "today", "expected"),
    [
        ("MNQ", date(2026, 10, 21), "202612"),
        ("MNQ", date(2026, 12, 9), "202612"),
        ("MNQ", date(2026, 12, 10), "202703"),
        ("MCL", date(2026, 10, 21), "202612"),
        ("MCL", date(2026, 11, 12), "202701"),
        ("MGC", date(2026, 10, 21), "202612"),
        ("MGC", date(2026, 11, 24), "202702"),
        ("ZN", date(2026, 11, 23), "202612"),
        ("ZN", date(2026, 11, 24), "202703"),
    ],
)
def test_resolve_month(symbol: str, today: date, expected: str) -> None:
    assert resolve_month(symbol, today) == expected


def test_is_alias() -> None:
    assert is_alias("MNQ")
    assert is_alias("mnq")
    assert not is_alias("MNQ-202612-USD-FUT")
    assert not is_alias("265598")


def test_generate_ib_contract_alias() -> None:
    contract = generate_ib_contract("MNQ", Exchange.CME)

    assert contract is not None
    assert contract.symbol == "MNQ"
    assert contract.secType == "FUT"
    assert contract.currency == "USD"
    assert contract.exchange == "CME"
    assert len(contract.lastTradeDateOrContractMonth) == 6
