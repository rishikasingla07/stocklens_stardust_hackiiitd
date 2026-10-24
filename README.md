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

| Part | Status |
|---|---|
| Consensus engine, scoring, six hard gates, tests | Done |
| Trigger engine (price and volume rules) and market data | Done, on recorded market data: it sets the "Move flagged" alerts on My Stocks and runs behind the Simulate move button. The plain Investigate path still takes its move from the recorded case file |
| API, Evidence Receipt, Challenge This | Done |
| Website (My Stocks, Investigation Card, Evidence Timeline, Plain/Expert) | Done |
| History: every Card saved to SQLite, reopened from the History drawer without re-running | Done |
| Explanations | Done: offline template; optional LLM version built, not tested live |
| Run live investigation (Sherlock live via TinyFish search, fetch, agent) | Built and being tested. Needs `STOCKLENS_MODE=live` and a TinyFish key; the move numbers are entered by the user, not taken from market data. Atlas, Echo and Skeptic are not connected live (recorded data only for the demo tickers) |
| Atlas, Echo and Skeptic live, TinyFish Monitor, live market data | Not built yet |
| Broker connect, mobile alerts, Hindi, embeddable API | Roadmap |

The three demo cases (ACMEINFRA High, ZENITHPOLY Unknown, ORIONPHARMA
market-wide) use recorded evidence in replay mode. Live retrieval is connected
for one investigator (Sherlock).

Overall: about 80% of the hackathon demo scope and about 40% of the full vision.

**Try it**

- Start the app (see [Run](#run)) and open http://127.0.0.1:8000/.
- Click a stock marked "Move flagged" (or press 1, 2 or 3) to see a recorded investigation.
- With a flagged stock selected, press **Simulate move** to watch the trigger engine flag the move from recorded prices.
- Press **Challenge This** to run the full Skeptic against the result.
- Open **History** to reopen any saved result exactly as it was.
- Live: set `STOCKLENS_MODE=live` and `TINYFISH_API_KEY` in `.env`, restart, then press **Run live investigation** (uses TinyFish credits, at most 10 calls per run).

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
