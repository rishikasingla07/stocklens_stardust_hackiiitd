"""Replay client: serves recorded TinyFish responses from fixtures/ when MODE == 'replay'.

Recorded evidence lives in fixtures/<case>/tinyfish/<investigator>.json
(written by scripts/split_fixtures.py). No network, fully deterministic.
"""

import json
from pathlib import Path

from backend import config
from backend.models import Evidence, Move


def _case_dir(case_folder: str | Path) -> Path:
    """Accept a case name ("case_high") or a path to the case folder."""
    path = Path(case_folder)
    return path if path.is_absolute() else config.FIXTURES_DIR / path


def load_evidence(case_folder: str | Path, investigator: str) -> list[Evidence]:
    """Recorded evidence for one investigator in one case; empty list if there is no file."""
    path = _case_dir(case_folder) / config.REPLAY_SUBDIR / f"{investigator}.json"
    if not path.exists():
        return []
    return [Evidence(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


def find_case(ticker: str) -> tuple[Path, Move] | None:
    """Find the recorded case for a ticker: (case folder, Move), or None if there is none."""
    for path in sorted(config.FIXTURES_DIR.glob("case_*/consensus_input.json")):
        move = Move(**json.loads(path.read_text(encoding="utf-8"))["move"])
        if move.ticker.upper() == ticker.upper():
            return path.parent, move
    return None
