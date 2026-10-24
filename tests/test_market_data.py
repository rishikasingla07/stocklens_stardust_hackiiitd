"""Tests for recorded market data (backend/market/data.py)."""

import json
from datetime import datetime

import pytest

from backend import config
from backend.market import data
from backend.models import IST, Move

CASES = {"case_high": "ACMEINFRA", "case_unknown": "ZENITHPOLY",
         "case_market_wide": "ORIONPHARMA"}


@pytest.mark.parametrize("case, ticker", CASES.items())
def test_move_matches_consensus_fixture(case, ticker):
    expected = Move(**json.loads(
        (config.FIXTURES_DIR / case / "consensus_input.json").read_text())["move"])
    assert data.get_move(case, ticker) == expected


def test_list_cases():
    assert data.list_cases() == {case: [ticker] for case, ticker in CASES.items()}


def test_unknown_case_or_ticker_raises():
    with pytest.raises(KeyError):
        data.get_ticker_data("case_nope", "ACMEINFRA")
    with pytest.raises(KeyError):
        data.get_ticker_data("case_high", "NOPE")


def test_price_at_uses_last_bar_at_or_before():
    bars = data.get_ticker_data("case_high", "ACMEINFRA").stock
    assert data.price_at(bars, datetime(2026, 10, 22, 10, 2, tzinfo=IST)) == 1250.0
    assert data.price_at(bars, datetime(2026, 10, 22, 10, 10, tzinfo=IST)) == 1250.0
    with pytest.raises(ValueError):
        data.price_at(bars, datetime(2026, 10, 22, 9, 0, tzinfo=IST))


def test_volume_between_excludes_start_bar():
    bars = data.get_ticker_data("case_high", "ACMEINFRA").stock
    start = datetime(2026, 10, 22, 10, 2, tzinfo=IST)
    end = datetime(2026, 10, 22, 10, 16, tzinfo=IST)
    assert data.volume_between(bars, start, end) == 100000


def test_visible_bars_hides_bars_newer_than_the_delay():
    bars = data.get_ticker_data("case_high", "ACMEINFRA").stock
    as_of = datetime(2026, 10, 22, 10, 31, tzinfo=IST)  # 15-minute delay -> up to 10:16
    assert [b.at.strftime("%H:%M") for b in data.visible_bars(bars, as_of)][-1] == "10:16"
