"""Response cache for live TinyFish calls, and helpers to turn a live run into replay fixtures.

Each real response is saved as fixtures/live_cache/<hash>.json, keyed on the
request (tool, method, URL, params, body). The API key is never part of the
key or the file.
"""

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from backend import config
from backend.models import IST, Evidence


def cache_key(tool: str, method: str, url: str, params: dict | None, body: dict | None) -> str:
    """Stable hash of a request; identical requests get the same key."""
    request = {"tool": tool, "method": method, "url": url, "params": params, "body": body}
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:32]


def cache_path(key: str, cache_dir: Path | None = None) -> Path:
    return (cache_dir or config.TINYFISH_CACHE_DIR) / f"{key}.json"


def load(key: str, cache_dir: Path | None = None) -> dict | None:
    """The cached response for a request key, or None."""
    path = cache_path(key, cache_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["response"]


def save(key: str, request: dict, response: dict, cache_dir: Path | None = None) -> Path:
    """Write one response (with the request that produced it) to the cache."""
    path = cache_path(key, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"request": request, "saved_at": datetime.now(IST).isoformat(), "response": response}
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _case_replay_dir(case: str) -> Path:
    case_dir = config.FIXTURES_DIR / case
    if not (case_dir / "consensus_input.json").exists():
        raise FileNotFoundError(f"{case_dir} is not a case folder (no consensus_input.json)")
    return case_dir / config.REPLAY_SUBDIR


def copy_to_case(cache_file: Path | str, case: str) -> Path:
    """Copy one cached response into fixtures/<case>/tinyfish/responses/ (raw provenance)."""
    source = Path(cache_file)
    if not source.is_absolute() and not source.exists():
        source = cache_path(source.stem)  # allow passing just the hash
    target = _case_replay_dir(case) / "responses" / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def save_replay_evidence(case: str, investigator: str, evidence: list[Evidence]) -> Path:
    """Write live Evidence as fixtures/<case>/tinyfish/<investigator>.json so replay serves it.

    Refuses to overwrite a non-empty file, so teammates' fixtures are never lost.
    """
    target = _case_replay_dir(case) / f"{investigator}.json"
    if target.exists() and json.loads(target.read_text(encoding="utf-8")):
        raise FileExistsError(f"{target} already has evidence; not overwriting")
    target.parent.mkdir(parents=True, exist_ok=True)
    items = [e.model_dump(mode="json") for e in evidence]
    target.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target
