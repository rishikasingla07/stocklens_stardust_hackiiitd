"""Stock catalog for the watchlist picker: fixtures/stock_catalog.json.

demo_coverage=True means a recorded demo case exists for the ticker. The other
entries are real NSE names listed for the picker only: no prices or evidence.
"""

import json
from functools import lru_cache
from pathlib import Path

from backend import config

FIELDS = ("ticker", "name", "sector", "demo_coverage")


@lru_cache(maxsize=None)
def _load(path: Path) -> tuple[dict, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    stocks = []
    for item in data["stocks"]:
        stocks.append({"ticker": item["ticker"].upper(), "name": item["name"], "sector": item["sector"],
                       "demo_coverage": bool(item["demo_coverage"]),
                       "fictional": bool(item.get("fictional", False))})
    return tuple(stocks)


def stocks(path: Path | None = None) -> list[dict]:
    """Every catalog entry: demo-coverage stocks first, then the rest by name."""
    items = [dict(s) for s in _load(Path(path or config.CATALOG_FILE))]
    return sorted(items, key=lambda s: (not s["demo_coverage"], s["name"].lower()))


def by_ticker(path: Path | None = None) -> dict[str, dict]:
    return {s["ticker"]: s for s in stocks(path)}
