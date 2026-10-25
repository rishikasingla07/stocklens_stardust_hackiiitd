"""Split each fixtures/case_*/consensus_input.json into per-investigator replay files.

Writes fixtures/case_*/tinyfish/<investigator>.json (an empty list if that
investigator has no evidence). Run from the repo root:

    python scripts/split_fixtures.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402

INVESTIGATORS = ("atlas", "sherlock", "echo")


def split_case(case_dir: Path) -> dict[str, int]:
    """Write one file per investigator for a case; return how many items each got."""
    data = json.loads((case_dir / "consensus_input.json").read_text(encoding="utf-8"))
    out_dir = case_dir / config.REPLAY_SUBDIR
    out_dir.mkdir(exist_ok=True)
    counts = {}
    for name in INVESTIGATORS:
        items = [e for e in data["evidence"] if e["investigator"] == name]
        (out_dir / f"{name}.json").write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
        counts[name] = len(items)
    return counts


def main() -> None:
    for case_dir in sorted(config.FIXTURES_DIR.glob("case_*")):
        if (case_dir / "consensus_input.json").exists():
            print(case_dir.name, split_case(case_dir))


if __name__ == "__main__":
    main()
