"""One month of recorded demo price history (fixtures/price_history.json) and unusual-move detection.

RECORDED DEMO DATA, NOT REAL PRICES. Detection reuses the trigger engine's rules
(backend/trigger/engine.py, TRIGGER_MIN_PCT_CHANGE and TRIGGER_MIN_VOLUME_MULTIPLE):

1. Every window inside one trading day from a bar to a later bar at most
   TRIGGER_DEFAULT_WINDOW_MINUTES x PRICE_HISTORY_WINDOW_FACTOR minutes away is
   checked: percent change start->end, and volume traded in the window against
   the ticker's normal volume per minute.
2. Overlapping (or touching) passing windows are merged into one event.
3. The event is trimmed: leading and trailing bar-to-bar steps that are flat
   (under PRICE_HISTORY_FLAT_STEP_PCT) or against the move's direction are not
   part of the move. If the trimmed span no longer passes, the untrimmed span
   (or else the strongest single window) is used.
4. The event becomes a Move through trigger.check_prices, so the numbers are the
   trigger engine's own.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from backend import config
from backend.models import Move
from backend.trigger import engine as trigger


@dataclass(frozen=True)
class Bar:
    at: datetime
    price: float
    volume: float  # traded since the previous bar
    sector: float
    index: float


@dataclass(frozen=True)
class Series:
    ticker: str
    name: str
    sector: str
    sector_index: str
    index: str
    normal_volume_per_minute: float
    bars: tuple[Bar, ...]


@dataclass(frozen=True)
class UnusualMove:
    move: Move
    end_at: datetime
    direction: str  # "up" | "down"

    def to_dict(self) -> dict:
        m = self.move
        return {
            "start_at": m.move_start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "window_minutes": m.window_minutes,
            "pct_change": m.pct_change,
            "volume_multiple": m.volume_multiple,
            "sector_pct_change": m.sector_pct_change,
            "index_pct_change": m.index_pct_change,
            "direction": self.direction,
        }


@lru_cache(maxsize=None)
def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def tickers(path: Path | None = None) -> list[str]:
    return list(_load(Path(path or config.PRICE_HISTORY_FILE))["tickers"])


def get_series(ticker: str, path: Path | None = None) -> Series | None:
    """The recorded month for a ticker, or None if it has no price history."""
    data = _load(Path(path or config.PRICE_HISTORY_FILE))["tickers"].get(ticker.upper())
    if data is None:
        return None
    bars = tuple(Bar(datetime.fromisoformat(b["at"]), b["price"], b["volume"], b["sector"], b["index"])
                 for b in data["bars"])
    return Series(ticker.upper(), data["name"], data["sector"], data["sector_index"], data["index"],
                  data["normal_volume_per_minute"], tuple(sorted(bars, key=lambda b: b.at)))


def _minutes(a: Bar, b: Bar) -> float:
    return (b.at - a.at).total_seconds() / 60


def _move(series: Series, i: int, j: int) -> Move | None:
    """The trigger engine's verdict on bars[i] -> bars[j] (a Move, or None if it does not pass)."""
    bars = series.bars
    minutes = _minutes(bars[i], bars[j])
    return trigger.check_prices(
        ticker=series.ticker,
        move_start_at=bars[i].at,
        start_price=bars[i].price,
        end_price=bars[j].price,
        window_volume=sum(b.volume for b in bars[i + 1:j + 1]),
        normal_volume=series.normal_volume_per_minute * minutes,
        sector_pct_change=trigger.pct_change(bars[i].sector, bars[j].sector),
        index_pct_change=trigger.pct_change(bars[i].index, bars[j].index),
        window_minutes=round(minutes),
    )


def _passing_windows(series: Series) -> list[tuple[int, int, Move]]:
    max_minutes = config.TRIGGER_DEFAULT_WINDOW_MINUTES * config.PRICE_HISTORY_WINDOW_FACTOR
    bars = series.bars
    found = []
    for i in range(len(bars)):
        j = i + 1
        while j < len(bars) and bars[j].at.date() == bars[i].at.date() \
                and _minutes(bars[i], bars[j]) <= max_minutes:
            move = _move(series, i, j)
            if move is not None:
                found.append((i, j, move))
            j += 1
    return found


def _trim(series: Series, i: int, j: int, sign: int) -> tuple[int, int]:
    """Drop flat or against-direction steps from both ends of bars[i..j]."""
    bars = series.bars
    flat = config.PRICE_HISTORY_FLAT_STEP_PCT

    def part_of_move(a: int, b: int) -> bool:
        return sign * trigger.pct_change(bars[a].price, bars[b].price) >= flat

    while i < j - 1 and not part_of_move(i, i + 1):
        i += 1
    while j > i + 1 and not part_of_move(j - 1, j):
        j -= 1
    return i, j


def detect_moves(series: Series) -> list[UnusualMove]:
    """Unusual moves in the series, oldest first. Overlapping windows are merged into one event."""
    windows = sorted(_passing_windows(series), key=lambda w: (w[0], w[1]))
    clusters: list[list[tuple[int, int, Move]]] = []
    for w in windows:
        if clusters and w[0] <= max(c[1] for c in clusters[-1]):
            clusters[-1].append(w)
        else:
            clusters.append([w])

    events = []
    for cluster in clusters:
        i, j = min(w[0] for w in cluster), max(w[1] for w in cluster)
        sign = 1 if series.bars[j].price >= series.bars[i].price else -1
        ti, tj = _trim(series, i, j, sign)
        strongest = max(cluster, key=lambda w: abs(w[2].pct_change))
        for a, b in ((ti, tj), (i, j), (strongest[0], strongest[1])):
            move = _move(series, a, b)
            if move is not None:
                events.append(UnusualMove(move, series.bars[b].at, "up" if move.pct_change > 0 else "down"))
                break
    return events


def find_move(series: Series, start_at: datetime) -> UnusualMove | None:
    """The detected move that starts exactly at start_at, or None."""
    return next((m for m in detect_moves(series) if m.move.move_start_at == start_at), None)
