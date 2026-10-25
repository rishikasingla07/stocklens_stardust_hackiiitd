"""Generate fixtures/price_history.json: one month of RECORDED DEMO 15-minute bars (not real prices).

Deterministic (seeded). For each demo ticker:
- 15-minute bars 09:15-15:30 IST on trading days (Mon-Fri; 2 Oct, Gandhi Jayanti,
  is skipped; this is a demo calendar, not the official NSE holiday list).
- On the recorded move day, the bars from fixtures/market_data.json are inserted
  verbatim, so the recorded move appears exactly as recorded there.
- One or two smaller unusual moves earlier in the month (stock-specific: the
  sector and index do not move with them).
Plus one fictional "quiet" ticker with no unusual moves.

Each bar's volume is the volume traded since the previous bar, as in market_data.json.
Run from the repo root:

    python scripts/make_price_history.py
"""

import json
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402
from backend.models import IST  # noqa: E402

SEED = 20261022
FIRST_DAY = date(2026, 9, 24)
LAST_DAY = date(2026, 10, 23)  # 30 calendar days
HOLIDAYS = {date(2026, 10, 2)}
OPEN, CLOSE, STEP = time(9, 15), time(15, 30), 15
NOISE = 0.0008         # per-bar price noise (0.08%): far below the trigger thresholds
INDEX_NOISE = 0.0004
GAP_MINUTES = 15       # grid bars this close to the recorded bars are dropped

# Smaller unusual moves earlier in the month: (day, start HH:MM, bars, total % change, volume x normal).
EXTRA_MOVES = {
    "ACMEINFRA": [(date(2026, 10, 7), "11:00", 2, 3.2, 2.4), (date(2026, 10, 14), "13:15", 2, -2.9, 2.1)],
    "ZENITHPOLY": [(date(2026, 10, 1), "10:00", 2, -3.0, 2.2)],
    "ORIONPHARMA": [(date(2026, 9, 29), "14:00", 2, 2.8, 2.5), (date(2026, 10, 12), "10:30", 2, -3.1, 2.0)],
}

QUIET = {"ticker": "LOTUSFOODS", "name": "Lotus Foods", "sector": "FMCG", "sector_index": "FMCG sector index",
         "index": "NIFTY 50", "price": 842.0, "sector_level": 15500.0, "index_level": 25000.0,
         "normal_volume_per_minute": 800}


def trading_days() -> list[date]:
    days, d = [], FIRST_DAY
    while d <= LAST_DAY:
        if d.weekday() < 5 and d not in HOLIDAYS:
            days.append(d)
        d += timedelta(days=1)
    return days


def grid(day: date) -> list[datetime]:
    t = datetime.combine(day, OPEN, IST)
    end = datetime.combine(day, CLOSE, IST)
    out = []
    while t <= end:
        out.append(t)
        t += timedelta(minutes=STEP)
    return out


def walk(rng: random.Random, n: int, sigma: float) -> list[float]:
    """n multiplicative steps of small noise (each 1 + N(0, sigma), clipped)."""
    return [1 + max(-3 * sigma, min(3 * sigma, rng.gauss(0, sigma))) for _ in range(n)]


def volume(rng: random.Random, per_minute: float, minutes: float, multiple: float = 1.0) -> int:
    return int(round(per_minute * minutes * multiple * rng.uniform(0.85, 1.15), -1))


def build_series(rng: random.Random, spec: dict, recorded: dict | None, extras: list) -> list[dict]:
    """Bars for one ticker. `recorded` is its market_data.json entry (None for the quiet ticker)."""
    per_min = spec["normal_volume_per_minute"]
    days = trading_days()
    rec_bars = recorded["stock"] if recorded else []
    rec_day = datetime.fromisoformat(rec_bars[0]["at"]).date() if rec_bars else None

    # Timestamps: the grid, with the recorded bars replacing the grid around them.
    stamps: list[tuple[datetime, dict | None]] = []
    for day in days:
        if day == rec_day:
            first = datetime.fromisoformat(rec_bars[0]["at"]) - timedelta(minutes=GAP_MINUTES)
            last = datetime.fromisoformat(rec_bars[-1]["at"]) + timedelta(minutes=GAP_MINUTES)
            before = [(t, None) for t in grid(day) if t <= first]
            after = [(t, None) for t in grid(day) if t >= last]
            stamps += before + [(datetime.fromisoformat(b["at"]), b) for b in rec_bars] + after
        else:
            stamps += [(t, None) for t in grid(day)]

    # Extra moves: per-bar return and volume multiple for the bars that make them up.
    boost: dict[datetime, tuple[float, float]] = {}
    for day, start, n, pct, vol_x in extras:
        t0 = datetime.combine(day, time.fromisoformat(start), IST)
        step = (1 + pct / 100) ** (1 / n) - 1
        for k in range(1, n + 1):
            boost[t0 + timedelta(minutes=STEP * k)] = (step, vol_x)

    # Prices before the recorded day are a walk scaled so it ends at the recorded opening level.
    rec_idx = next((i for i, (_, b) in enumerate(stamps) if b is not None), None)
    stock_open = rec_bars[0]["price"] if rec_bars else spec["price"]
    sector_open = recorded["sector_series"][0]["price"] if recorded else spec["sector_level"]
    index_open = recorded["index_series"][0]["price"] if recorded else spec["index_level"]
    rec_sector = {s["at"]: s["price"] for s in recorded["sector_series"]} if recorded else {}
    rec_index = {s["at"]: s["price"] for s in recorded["index_series"]} if recorded else {}

    n = len(stamps)
    stock_steps = walk(rng, n, NOISE)
    sector_steps = walk(rng, n, INDEX_NOISE)
    index_steps = walk(rng, n, INDEX_NOISE)
    for i, (t, _) in enumerate(stamps):
        if t in boost:
            stock_steps[i] = 1 + boost[t][0]

    def path(start_level: float, steps: list[float], anchor_idx: int | None) -> list[float]:
        levels, level = [], 1.0
        for s in steps:
            level *= s
            levels.append(level)
        if anchor_idx is None:  # quiet ticker: just start at its level
            return [start_level * v / levels[0] for v in levels]
        # Scale the pre-recorded part so the bar just before the recorded bars sits at the recorded open.
        scale = start_level / levels[max(anchor_idx - 1, 0)]
        return [v * scale for v in levels]

    stock = path(stock_open, stock_steps, rec_idx)
    sector = path(sector_open, sector_steps, rec_idx)
    index = path(index_open, index_steps, rec_idx)

    bars, prev_t = [], None
    last_rec = None
    for i, (t, rec) in enumerate(stamps):
        if rec is not None:
            price, vol = rec["price"], rec["volume"]
            sec, idx = rec_sector[rec["at"]], rec_index[rec["at"]]
            last_rec = (i, price, sec, idx)
        else:
            if last_rec is not None and i > last_rec[0]:
                # After the recorded bars: continue from where the recording ends.
                j, p0, s0, x0 = last_rec
                price = p0 * stock[i] / stock[j]
                sec = s0 * sector[i] / sector[j]
                idx = x0 * index[i] / index[j]
            else:
                price, sec, idx = stock[i], sector[i], index[i]
            minutes = STEP if prev_t is None or prev_t.date() != t.date() else (t - prev_t).total_seconds() / 60
            vol = volume(rng, per_min, minutes, boost.get(t, (0, 1.0))[1])
        bars.append({"at": t.isoformat(), "price": round(price, 2), "volume": vol,
                     "sector": round(sec, 2), "index": round(idx, 2)})
        prev_t = t
    return bars


def main() -> None:
    market = json.loads((config.FIXTURES_DIR / "market_data.json").read_text(encoding="utf-8"))
    rng = random.Random(SEED)
    out = {
        "description": ("RECORDED DEMO DATA, NOT REAL PRICES. One month of 15-minute bars generated by "
                        "scripts/make_price_history.py (seeded). Companies are fictional. On each demo "
                        "ticker's recorded move day the bars from market_data.json are inserted verbatim. "
                        "Volume on each bar is the volume traded since the previous bar."),
        "generated_by": "scripts/make_price_history.py",
        "seed": SEED,
        "bar_minutes": STEP,
        "tickers": {},
    }
    for case, tickers in market["cases"].items():
        for ticker, rec in tickers.items():
            spec = {"normal_volume_per_minute": rec["normal_volume_per_minute"]}
            out["tickers"][ticker] = {
                "name": {"ACMEINFRA": "ACME Infra", "ZENITHPOLY": "Zenith Polymers",
                         "ORIONPHARMA": "Orion Pharma"}.get(ticker, ticker),
                "sector": rec["sector"], "sector_index": rec["sector_index"], "index": rec["index"],
                "normal_volume_per_minute": rec["normal_volume_per_minute"],
                "bars": build_series(rng, spec, rec, EXTRA_MOVES.get(ticker, [])),
            }
    out["tickers"][QUIET["ticker"]] = {
        "name": QUIET["name"], "sector": QUIET["sector"], "sector_index": QUIET["sector_index"],
        "index": QUIET["index"], "normal_volume_per_minute": QUIET["normal_volume_per_minute"],
        "bars": build_series(rng, QUIET, None, []),
    }
    path = config.FIXTURES_DIR / "price_history.json"
    # One bar per line keeps the file readable and the diffs small.
    lines = [json.dumps({k: v for k, v in out.items() if k != "tickers"}, indent=2)[:-2] + ',\n  "tickers": {']
    for n, (ticker, data) in enumerate(out["tickers"].items()):
        head = {k: v for k, v in data.items() if k != "bars"}
        lines.append(f'    "{ticker}": {{')
        lines += [f'      "{k}": {json.dumps(v)},' for k, v in head.items()]
        lines.append('      "bars": [')
        lines.append(",\n".join(f"        {json.dumps(b)}" for b in data["bars"]))
        lines.append("      ]")
        lines.append("    }" + ("," if n < len(out["tickers"]) - 1 else ""))
    lines.append("  }\n}\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    json.loads(path.read_text(encoding="utf-8"))  # must be valid JSON
    for ticker, data in out["tickers"].items():
        print(f"{ticker}: {len(data['bars'])} bars")


if __name__ == "__main__":
    main()
