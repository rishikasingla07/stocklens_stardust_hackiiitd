"""Trigger engine: detects unusual moves with deterministic price and volume rules only (no AI).

Flags a move when the percent change over a window and the volume multiple pass
thresholds, then creates the Move and starts the investigation.
TinyFish Monitor is separate: it watches sources for new filings and does not detect price moves.
"""

from datetime import datetime

from backend import config
from backend.models import Move


def pct_change(start_price: float, end_price: float) -> float:
    """Percent change from start_price to end_price, e.g. 100 -> 94.2 is -5.8."""
    if start_price <= 0:
        raise ValueError("start_price must be positive")
    return (end_price - start_price) / start_price * 100


def volume_multiple(window_volume: float, normal_volume: float) -> float:
    """Volume traded in the window divided by the normal volume for a window of the same length."""
    if normal_volume <= 0:
        raise ValueError("normal_volume must be positive")
    return window_volume / normal_volume


def passes_thresholds(pct: float, vol_multiple: float) -> bool:
    """True when the move is big enough in either direction AND volume is unusually high."""
    return (abs(pct) >= config.TRIGGER_MIN_PCT_CHANGE
            and vol_multiple >= config.TRIGGER_MIN_VOLUME_MULTIPLE)


def check_move(
    ticker: str,
    move_start_at: datetime,
    pct: float,
    vol_multiple: float,
    sector_pct_change: float,
    index_pct_change: float,
    window_minutes: int | None = None,
) -> Move | None:
    """Return a Move if pct and vol_multiple both pass the thresholds, otherwise None.

    pct and the sector/index changes are percent changes over the same window.
    """
    if not passes_thresholds(pct, vol_multiple):
        return None
    return Move(
        ticker=ticker,
        move_start_at=move_start_at,
        window_minutes=window_minutes or config.TRIGGER_DEFAULT_WINDOW_MINUTES,
        pct_change=round(pct, 2),
        volume_multiple=round(vol_multiple, 2),
        sector_pct_change=round(sector_pct_change, 2),
        index_pct_change=round(index_pct_change, 2),
    )


def check_prices(
    ticker: str,
    move_start_at: datetime,
    start_price: float,
    end_price: float,
    window_volume: float,
    normal_volume: float,
    sector_pct_change: float,
    index_pct_change: float,
    window_minutes: int | None = None,
) -> Move | None:
    """Same as check_move, but from raw prices and volumes instead of precomputed numbers."""
    return check_move(
        ticker=ticker,
        move_start_at=move_start_at,
        pct=pct_change(start_price, end_price),
        vol_multiple=volume_multiple(window_volume, normal_volume),
        sector_pct_change=sector_pct_change,
        index_pct_change=index_pct_change,
        window_minutes=window_minutes,
    )
