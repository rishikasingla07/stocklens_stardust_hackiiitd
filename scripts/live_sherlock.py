"""Live demo: run ONLY Sherlock against real TinyFish for one company and move time.

Refuses to run unless STOCKLENS_MODE=live and TINYFISH_API_KEY is set in .env.
No LLM key is needed: filings are read with the TinyFish Agent. Uses at most
config.MAX_TINYFISH_CALLS_PER_INVESTIGATION calls (normally up to 5: one
search, then per filing page one Agent call and one Fetch to verify the
snippet). Repeating the same command is served from fixtures/live_cache/ and
costs nothing. Run from the repo root:

    python scripts/live_sherlock.py "Infosys" 2026-10-22T10:02
    python scripts/live_sherlock.py INFY 2026-10-22T10:02 --save-case case_live

--save-case writes the evidence to fixtures/<case>/tinyfish/sherlock.json and
copies the raw responses beside it, so the run can be replayed offline. The
case folder must already exist with a consensus_input.json.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402
from backend.investigators.sherlock import Sherlock  # noqa: E402
from backend.models import IST, Move  # noqa: E402
from backend.tinyfish import cache  # noqa: E402
from backend.tinyfish.client import TinyFishClient  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Sherlock live for one company.")
    parser.add_argument("company", help='NSE ticker or company name, e.g. INFY or "Infosys"')
    parser.add_argument("move_start", help="move start time, e.g. 2026-10-22T10:02 (IST if no offset)")
    parser.add_argument("--window", type=int, default=config.TRIGGER_DEFAULT_WINDOW_MINUTES,
                        help="move window in minutes")
    parser.add_argument("--save-case", metavar="CASE",
                        help="write the evidence as replay fixtures for fixtures/CASE")
    return parser.parse_args(argv)


def preflight() -> str | None:
    """Why the demo must not run, or None if it may."""
    if config.MODE != "live":
        return f"STOCKLENS_MODE is {config.MODE!r}; set STOCKLENS_MODE=live in .env to run this demo."
    if not config.TINYFISH_API_KEY.strip():
        return "TINYFISH_API_KEY is not set in .env."
    return None


def build_move(company: str, move_start: str, window: int) -> Move:
    """A Move for the demo. Price/volume fields are 0: Sherlock only uses ticker and time."""
    start = datetime.fromisoformat(move_start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=IST)
    ticker = "".join(company.split()).upper()
    return Move(ticker=ticker, move_start_at=start, window_minutes=window, pct_change=0.0,
                volume_multiple=0.0, sector_pct_change=0.0, index_pct_change=0.0)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    problem = preflight()
    if problem:
        print(f"Refusing to run: {problem}")
        return 1

    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    move = build_move(args.company, args.move_start, args.window)
    client = TinyFishClient()
    print(f"Sherlock live: {args.company} | move start {move.move_start_at.isoformat()}")
    report = asyncio.run(Sherlock(client, mode="live", company=args.company).run(move))

    print(f"\nNotes: {report.notes}")
    print(f"Evidence found: {len(report.evidence)}")
    for e in report.evidence:
        print(f"\n- source:          {e.source_name} (Tier {e.source_tier}, "
              f"{'verified' if e.verified else 'unverified'})")
        print(f"  first_public_at: {e.first_public_at.isoformat() if e.first_public_at else None}")
        print(f"  claim:           {e.claim}")
        print(f"  snippet:         {e.snippet}")
        print(f"  url:             {e.url}")
    agent_calls = sum(1 for call in client.budget.calls if call.startswith("agent"))
    print(f"\nTinyFish calls used: {client.budget.used}/{client.budget.limit} "
          f"(cache hits, free: {client.cache_hits})")
    print(f"Agent calls made (paid, check on the TinyFish dashboard): {agent_calls}")

    if args.save_case:
        if not report.evidence:
            print(f"Nothing saved to {args.save_case}: no evidence.")
        else:
            print(f"Saved evidence: {cache.save_replay_evidence(args.save_case, 'sherlock', report.evidence)}")
            for path in dict.fromkeys(client.cache_files):
                print(f"Copied response: {cache.copy_to_case(path, args.save_case)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
