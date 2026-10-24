"""Market data: price, volume, sector and index changes for NSE tickers.

Reads recorded delayed data from fixtures/market_data.json (no network), and
uses the trigger engine to turn a case's move window into a Move.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from backend import config
from backend.models import Move
from backend.trigger import engine as trigger

MARKET_DATA_FILE = config.FIXTURES_DIR / "market_data.json"


@dataclass(frozen=True)
class Bar:
    """One point in a series. For the stock, volume is the volume traded since the previous bar."""
    at: datetime
    price: float
    volume: float = 0.0


@dataclass(frozen=True)
class TickerData:
    """Everything recorded for one ticker in one demo case."""
    ticker: str
    sector: str
    sector_index: str
    index: str
    move_start_at: datetime
    window_minutes: int
    normal_volume_per_minute: float
    stock: list[Bar]
    sector_series: list[Bar]
    index_series: list[Bar]

    @property
    def move_end_at(self) -> datetime:
        return self.move_start_at + timedelta(minutes=self.window_minutes)


@lru_cache(maxsize=None)
def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _bars(rows: list[dict]) -> list[Bar]:
    bars = [Bar(at=datetime.fromisoformat(r["at"]), price=r["price"], volume=r.get("volume", 0.0))
            for r in rows]
    return sorted(bars, key=lambda b: b.at)


def delay_minutes(path: Path = MARKET_DATA_FILE) -> int:
    """How many minutes behind real time the recorded feed is."""
    return _load(path)["delay_minutes"]


def list_cases(path: Path = MARKET_DATA_FILE) -> dict[str, list[str]]:
    """Map each case name to the tickers recorded for it."""
    return {case: list(tickers) for case, tickers in _load(path)["cases"].items()}


def get_ticker_data(case: str, ticker: str, path: Path = MARKET_DATA_FILE) -> TickerData:
    """Load the recorded series for one ticker in one case. Raises KeyError if missing."""
    cases = _load(path)["cases"]
    if case not in cases:
        raise KeyError(f"unknown case {case!r}; known: {sorted(cases)}")
    if ticker not in cases[case]:
        raise KeyError(f"unknown ticker {ticker!r} in {case}; known: {sorted(cases[case])}")
    raw = cases[case][ticker]
    return TickerData(
        ticker=ticker,
        sector=raw["sector"],
        sector_index=raw["sector_index"],
        index=raw["index"],
        move_start_at=datetime.fromisoformat(raw["move_start_at"]),
        window_minutes=raw["window_minutes"],
        normal_volume_per_minute=raw["normal_volume_per_minute"],
        stock=_bars(raw["stock"]),
        sector_series=_bars(raw["sector_series"]),
        index_series=_bars(raw["index_series"]),
    )


def price_at(bars: list[Bar], at: datetime) -> float:
    """Price of the last bar at or before `at`. Raises ValueError if there is none."""
    earlier = [b for b in bars if b.at <= at]
    if not earlier:
        raise ValueError(f"no bar at or before {at.isoformat()}")
    return earlier[-1].price


def pct_change_between(bars: list[Bar], start: datetime, end: datetime) -> float:
    """Percent change of a series from start to end."""
    return trigger.pct_change(price_at(bars, start), price_at(bars, end))


def volume_between(bars: list[Bar], start: datetime, end: datetime) -> float:
    """Volume traded after start, up to and including end."""
    return sum(b.volume for b in bars if start < b.at <= end)


def visible_bars(bars: list[Bar], as_of: datetime, delay: int | None = None) -> list[Bar]:
    """Bars a delayed feed would have shown at `as_of` (only those at least `delay` minutes old)."""
    delay = delay_minutes() if delay is None else delay
    cutoff = as_of - timedelta(minutes=delay)
    return [b for b in bars if b.at <= cutoff]


def get_move(case: str, ticker: str, path: Path = MARKET_DATA_FILE) -> Move | None:
    """Build the Move for a case's recorded window, or None if it does not pass the trigger."""
    data = get_ticker_data(case, ticker, path)
    start, end = data.move_start_at, data.move_end_at
    return trigger.check_prices(
        ticker=ticker,
        move_start_at=start,
        start_price=price_at(data.stock, start),
        end_price=price_at(data.stock, end),
        window_volume=volume_between(data.stock, start, end),
        normal_volume=data.normal_volume_per_minute * data.window_minutes,
        sector_pct_change=pct_change_between(data.sector_series, start, end),
        index_pct_change=pct_change_between(data.index_series, start, end),
        window_minutes=data.window_minutes,
    )
