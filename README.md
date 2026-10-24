# StockLens

Explains **why** an NSE stock moved, with evidence and an honest confidence
level — or says "Cause not confidently identified". AI gathers and explains;
deterministic Python decides. See [SPEC.md](SPEC.md).

## Run

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in keys; MODE=replay works offline
uvicorn backend.app:app --reload
```

- Health check: http://127.0.0.1:8000/health
- Frontend: http://127.0.0.1:8000/
- API docs: http://127.0.0.1:8000/docs

Run from the repo root (so `backend` is importable).

## Tests

```bash
pytest
```

## Layout

- `backend/config.py` — every tunable (scoring, label cut-offs, tiers, MODE, gates)
- `backend/models.py` — Evidence, Move, Report, Card
- `backend/investigators/` — Atlas (macro: market/sector cause), Sherlock (company: NSE/BSE filings and company news), Echo (crowd: Tier 3, context only), Skeptic (against: runs on "Challenge This")
- `backend/trigger/` — detects unusual moves with price and volume rules only (no AI): percent change over a window and volume multiple must pass thresholds; creates the Move and starts the investigation. TinyFish Monitor is separate: it watches sources for new filings on held and watched stocks for alerts and timing checks, and does not detect price moves
- `backend/tinyfish/` — evidence acquisition only (live client + replay)
- `backend/consensus/` — deterministic timing, independence, market context, scoring, gates
- `backend/llm/` — extraction and explanation text
- `fixtures/` — recorded demo cases
