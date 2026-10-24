# StockLens — Specification

## Product

StockLens explains **why** an Indian (NSE) stock moved, with evidence and an
honest confidence level — or says **"Cause not confidently identified"**.

**Core rule:** AI gathers and explains; **deterministic Python code** decides
what counts as evidence and how confident we are.

**TinyFish** (web agents / search / fetch / browser / monitor) is used **only to
acquire evidence** from the web. It never scores or decides.

## Stack

- Python 3.11+
- FastAPI backend
- Plain HTML/CSS/JS frontend served by FastAPI (no build step)
- SQLite
- Pydantic models
- Secrets in `.env`, loaded with `python-dotenv` (`.env.example` committed;
  `.env` git-ignored)

## Folder layout

```
backend/
  app.py              FastAPI app, routes, static frontend
  config.py           ALL tunables (scoring, labels, tiers, MODE, gates)
  models.py           Pydantic models
  trigger/engine.py   Price/volume rules (no AI) flag a move, create the Move, start the investigation
  investigators/
    base.py           Investigator interface
    atlas.py          Macro: did the market or sector cause this?
    sherlock.py       Company: did the company itself announce something?
    echo.py           Crowd: what is the crowd saying? (context only)
    skeptic.py        Against: what argues against the leading cause?
  tinyfish/
    client.py         Live TinyFish API client
    replay.py         Replays recorded fixtures (offline / demo)
  consensus/
    timing.py         Was evidence public before the move started?
    independence.py   Groups evidence into independence groups
    market_context.py Sector / index explanation of the move
    scoring.py        Applies scoring points
    engine.py         Runs scoring + hard gates -> Card
  llm/
    extract.py        LLM extraction of claims from raw pages
    explain.py        LLM writes plain-language explanation of a Card
    prompts/          Prompt templates
  market/data.py      Price / volume / sector / index data
  db/schema.sql       SQLite schema
frontend/             index.html, app.js, styles.css
fixtures/             case_high/, case_unknown/, case_market_wide/
tests/
```

## Investigators

Investigators gather evidence only (via TinyFish). They never score or decide.

### Atlas — macro
- **Question:** did the market or sector cause this?
- **Does:** finds policy, sector and macro events in the move window and opens
  official release pages for exact dates.
- **TinyFish tools:** Search, Research, Agent.
- **Hands over:** a dated macro cause with a link.
- **Tier:** 1 or 2.

### Sherlock — company
- **Question:** did the company itself announce something?
- **Does:** locates NSE and BSE filings for the window, opens each filing and
  traces cited news back to it.
- **TinyFish tools:** Search, Agent, Fetch, Browser.
- **Hands over:** filing text, quoted snippet and official timestamp.
- **Tier:** filings are Tier 1; company news is Tier 2.

### Echo — crowd
- **Question:** what is the crowd saying?
- **Does:** reads public posts and commentary.
- **TinyFish tools:** Search, Fetch, Browser.
- **Tier:** always Tier 3, always `verified=False`.
- **Context only, never proof.**

### Skeptic — against
- **Question:** what argues against the leading cause?
- **Does:** hunts for denials, conflicts and earlier mentions.
- **TinyFish tools:** Search, Fetch.
- **Runs:** only when the user taps **"Challenge This"**.
- **Hands over:** counter-evidence, or a clean "none found".

### TinyFish Monitor (not an investigator)
TinyFish Monitor watches sources for new filings on held and watched stocks
and powers **alerts** and **timing checks**. It does **not** detect price
moves; the trigger engine does that with price and volume rules.

## Data models

**Evidence**
- `id`
- `claim`
- `source_name`
- `source_tier` — 1, 2 or 3
- `url`
- `first_public_at` — timezone-aware datetime, IST
- `snippet`
- `investigator` — `atlas` | `sherlock` | `echo` | `skeptic`
- `independence_group` — str or None
- `verified` — bool

**Move**
- `ticker`, `move_start_at`, `window_minutes`, `pct_change`,
  `volume_multiple`, `sector_pct_change`, `index_pct_change`

**Report** (one per investigator)
- `investigator`, `evidence` (list), `notes`

**Card** (final output)
- `ticker`, `move`
- `confidence_score` (0–100)
- `confidence_label` — `High` | `Medium` | `Low` | `Unknown`
- `leading_explanation` (or None)
- `accepted_evidence`
- `rejected_evidence` (each with a reason)
- `alternatives`
- `score_breakdown` — list of `{rule, points}`
- `explanation_text`
- `limits`

## Source tiers

| Tier | Meaning |
|------|---------|
| 1 | Primary — exchange filing, official release |
| 2 | Reputable media |
| 3 | Brokerage notes, blogs, social |

## Scoring points

| Rule | Points |
|------|-------:|
| Primary source confirms | +25 |
| Independent Tier 2 corroboration | +20 |
| Timing: public before move start | +20 |
| Move is unusual versus sector and index | +15 |
| Filing and media agree | +10 |
| No stronger alternative explanation | +10 |
| Partly anticipated (earlier hints) | −10 |

Score is clamped to 0–100.

## Labels

| Score | Label |
|-------|-------|
| ≥ 75 | High |
| 50–74 | Medium |
| 25–49 | Low |
| < 25 | Unknown |

## Six hard gates

Gates are applied after scoring and can only lower confidence (or change the
leading explanation). They are deterministic.

1. **No evidence public before move start** → `Unknown`.
2. **Only Tier 3 or unverified evidence** → cap at `Low`.
3. **Only one independence group** → cap at `Medium`.
4. **Sector/index explains most of the move** → market explanation leads.
5. **Evidence with missing or ambiguous timestamp** is excluded.
6. **Unresolved Skeptic counter-evidence** → cap at `Medium`.

When the label is `Unknown`, the card says "Cause not confidently identified".

## Modes

- `live` — investigators call TinyFish and the LLM for real.
- `replay` — investigators read recorded fixtures from `fixtures/`
  (deterministic, offline, for demos and tests).

## Demo fixtures

- `case_high` — clear filing + independent media, before the move → High.
- `case_unknown` — nothing public before the move → Unknown.
- `case_market_wide` — sector/index explains the move → market leads.
