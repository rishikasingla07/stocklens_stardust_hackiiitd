# StockLens

Explains **why** an NSE stock moved, with evidence and an honest confidence
level — or says "Cause not confidently identified". AI gathers and explains;
deterministic Python decides. See [SPEC.md](SPEC.md).

## What it does

When a stock you hold or watch makes an unusual move, StockLens collects
evidence about why: exchange filings, news, market and sector moves, and
crowd chatter. It checks each item: was it public before the move started,
is it independent of the other items, does the market explain the move
instead? It then gives a confidence label (High, Medium, Low or Unknown) with
a score out of 100, a timeline of the evidence, and a breakdown of every point.
If the evidence is not strong enough, it says "Cause not confidently
identified" rather than guessing.

**Core rule:** AI gathers and explains; **deterministic code decides** what
counts as evidence and how confident we are. TinyFish is used only to acquire
evidence from the web, and the LLM only puts an already-decided Card into
words.

## How it works

1. **Trigger:** price and volume rules (no AI) flag a move when the percent
   change over a window and the volume multiple pass thresholds, producing a
   `Move`.
2. **Investigators:** Atlas (macro), Sherlock (company filings and news) and
   Echo (crowd) gather evidence in parallel. Skeptic (counter-evidence) runs
   in full on "Challenge This"; by default only its earlier-hint lookup runs.
   Each investigator's tier rules are enforced in code.
3. **Extraction:** an LLM turns raw fetched pages into structured `Evidence`
   (claim, source, tier, timestamp, snippet).
4. **Consensus (deterministic):** timing (public strictly before the move;
   missing or ambiguous timestamps excluded), independence (syndicated copies
   count as one source), market context (does the sector or index explain the
   move?), then scoring and the six hard gates, which can only lower
   confidence.
5. **Card:** score, label, leading explanation, accepted and rejected
   evidence (each rejection with a reason), alternatives, score breakdown and
   limits.
6. **Explanation:** an LLM writes `explanation_text` from the Card. If no LLM
   key is set, the call fails, or the reply states a different confidence
   number, an offline template is used instead.

See [SPEC.md](SPEC.md) for the scoring points, gates and the "Consensus
assumptions" behind them. Steps 1 and 3 exist as tested modules but are not
yet in the running pipeline (see [Status](#status)).

## Demo cases

Three recorded cases in `fixtures/`. The companies and media outlets are
fictional; they exist only for the demo and tests.

| Ticker | Result | What it shows |
|---|---|---|
| ACMEINFRA | **High, 90** | A Tier 1 NSE filing (guidance cut) public 14 minutes before the move, corroborated by independent Tier 2 media. A brokerage note published after the move began is rejected. An earlier management hint costs −10 ("partly anticipated"). |
| ZENITHPOLY | **Unknown, 15** | Nothing was public before the move: news and forum posts came after it, and a blog post has no timestamp. The card says "Cause not confidently identified" and lists why each item failed. |
| ORIONPHARMA | **Medium, 50, market-wide** | The pharma sector fell 2.3% against the stock's 2.6%, so the sector explains about 88% of the move and the market explanation leads (gate 4). |

To run them, start the app (see [Run](#run)), open http://127.0.0.1:8000/ and
click a stock marked "Move flagged". Stocks without a flagged move show "No
investigation available". Or call the API directly:

```bash
curl -X POST http://127.0.0.1:8000/investigate/ACMEINFRA
curl -X POST http://127.0.0.1:8000/challenge/ACMEINFRA   # same Card, full Skeptic
curl "http://127.0.0.1:8000/receipt/ACMEINFRA?format=html"
```

The expected results are also checked by the tests (`pytest`).

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

## Status

**Works now (replay mode, offline):**
- The full flow from recorded evidence to Card: investigators read the
  recorded evidence for each case from `fixtures/case_*/tinyfish/`, the
  consensus engine scores it, and the Card is returned.
- The deterministic consensus engine: timing, independence, market context,
  scoring and all six gates, with tests.
- API: `/stocks`, `/investigate/{ticker}`, `/challenge/{ticker}`,
  `/receipt/{ticker}` (JSON download and printable HTML), `/health`.
- Website: My Stocks, Investigation Card, Evidence Timeline, Score breakdown,
  Unknown state, Alternatives and Limits, Plain/Expert toggle, Evidence
  Receipt and Challenge This. It works offline with no external libraries.
- Explanations: offline template explanations for every Card. If
  `LLM_API_KEY` is set, the LLM writes the explanation instead (Anthropic,
  OpenAI or Gemini), with the offline text as the fallback.
- Built and tested but **not yet used by the pipeline**: the trigger engine,
  the market-data reader (recorded, delayed data in
  `fixtures/market_data.json`), and LLM evidence extraction.

**Not connected yet:**
- **Live TinyFish calls.** The client is a stub that raises "not connected";
  only the per-investigation call budget is in place.
- **Live gathering for every investigator** (Atlas, Sherlock, Echo and
  Skeptic). All evidence comes from recorded fixtures, so keep
  `STOCKLENS_MODE=replay`. Live mode does not work yet.
- **Live market data and move detection.** Each case's Move comes from its
  recorded `consensus_input.json`, and the prices on My Stocks are hardcoded
  demo values.
- **TinyFish Monitor** (filing alerts and timing checks).
- **SQLite storage.** `backend/db/schema.sql` is a placeholder and nothing
  is stored.

## Layout

- `backend/config.py` — every tunable (scoring, label cut-offs, tiers, MODE, gates)
- `backend/models.py` — Evidence, Move, Report, Card
- `backend/investigators/` — Atlas (macro: market/sector cause), Sherlock (company: NSE/BSE filings and company news), Echo (crowd: Tier 3, context only), Skeptic (against: runs on "Challenge This")
- `backend/trigger/` — detects unusual moves with price and volume rules only (no AI): percent change over a window and volume multiple must pass thresholds; creates the Move and starts the investigation. TinyFish Monitor is separate: it watches sources for new filings on held and watched stocks for alerts and timing checks, and does not detect price moves
- `backend/tinyfish/` — evidence acquisition only (live client + replay)
- `backend/consensus/` — deterministic timing, independence, market context, scoring, gates
- `backend/llm/` — extraction and explanation text
- `fixtures/` — recorded demo cases
- `backend/app.py` — API routes and the static frontend
- `backend/pipeline.py` — Move → investigators (in parallel) → consensus engine → Card
- `backend/market/` — recorded market data reader
- `frontend/` — the website (plain HTML/CSS/JS, no build step)
- `scripts/split_fixtures.py` — splits each case into per-investigator replay files
- `tests/` — pytest suite

## How we built this

StockLens was designed by Team Stardust: the idea, the evidence rules, the
scoring and confidence model, and the architecture. We implemented it during
the hackathon with AI-assisted coding (Claude Code), and reviewed and tested
the code ourselves.

## Team

Team Stardust: _Name 1_, _Name 2_, _Name 3_, _Name 4_
