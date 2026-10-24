"""Tests for the deterministic trigger engine (backend/trigger/engine.py)."""

import json
from datetime import datetime

import pytest

from backend import config
from backend.models import IST, Move
from backend.trigger import engine

MOVE_START = datetime(2026, 10, 22, 10, 2, tzinfo=IST)


def check(pct: float, vol: float, **overrides) -> Move | None:
    data = dict(ticker="TEST", move_start_at=MOVE_START, pct=pct, vol_multiple=vol,
                sector_pct_change=-0.7, index_pct_change=-0.2, window_minutes=42)
    data.update(overrides)
    return engine.check_move(**data)


# --- maths -------------------------------------------------------------------

def test_pct_change():
    assert engine.pct_change(100, 94.2) == pytest.approx(-5.8)
    assert engine.pct_change(200, 208.4) == pytest.approx(4.2)


def test_volume_multiple():
    assert engine.volume_multiple(31_000, 10_000) == pytest.approx(3.1)


@pytest.mark.parametrize("func, args", [
    (engine.pct_change, (0, 10)),
    (engine.volume_multiple, (100, 0)),
])
def test_zero_baseline_is_rejected(func, args):
    with pytest.raises(ValueError):
        func(*args)


# --- thresholds --------------------------------------------------------------

def test_big_fall_on_high_volume_is_flagged():
    move = check(-5.8, 3.1)
    assert move == Move(ticker="TEST", move_start_at=MOVE_START, window_minutes=42,
                        pct_change=-5.8, volume_multiple=3.1,
                        sector_pct_change=-0.7, index_pct_change=-0.2)


def test_big_rise_is_flagged_too():
    assert check(4.2, 2.6) is not None


def test_big_move_on_normal_volume_is_ignored():
    assert check(-5.8, 1.0) is None


def test_high_volume_without_a_big_move_is_ignored():
    assert check(-0.5, 5.0) is None


def test_values_exactly_at_thresholds_pass():
    assert check(-config.TRIGGER_MIN_PCT_CHANGE, config.TRIGGER_MIN_VOLUME_MULTIPLE) is not None


def test_values_just_below_thresholds_fail():
    assert check(config.TRIGGER_MIN_PCT_CHANGE - 0.01, 10) is None
    assert check(10, config.TRIGGER_MIN_VOLUME_MULTIPLE - 0.01) is None


def test_default_window_is_used_when_none_given():
    move = check(-5.8, 3.1, window_minutes=None)
    assert move.window_minutes == config.TRIGGER_DEFAULT_WINDOW_MINUTES


def test_check_prices_builds_move_from_raw_numbers():
    move = engine.check_prices(
        ticker="ACMEINFRA", move_start_at=MOVE_START, start_price=100, end_price=94.2,
        window_volume=31_000, normal_volume=10_000,
        sector_pct_change=-0.7, index_pct_change=-0.2, window_minutes=42)
    assert move.pct_change == -5.8
    assert move.volume_multiple == 3.1
    assert move.move_start_at.tzinfo is not None


# --- demo fixtures -----------------------------------------------------------

@pytest.mark.parametrize("case", ["case_high", "case_unknown", "case_market_wide"])
def test_every_demo_case_triggers(case):
    expected = Move(**json.loads(
        (config.FIXTURES_DIR / case / "consensus_input.json").read_text())["move"])
    move = check(expected.pct_change, expected.volume_multiple,
                 ticker=expected.ticker, move_start_at=expected.move_start_at,
                 window_minutes=expected.window_minutes,
                 sector_pct_change=expected.sector_pct_change,
                 index_pct_change=expected.index_pct_change)
    assert move == expected
