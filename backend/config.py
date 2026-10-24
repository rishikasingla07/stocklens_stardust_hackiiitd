"""All StockLens tunables live here. Change behaviour by editing this file, not the logic."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- Runtime -----------------------------------------------------------------

# "live" = call TinyFish + LLM; "replay" = serve recorded fixtures.
MODE = os.getenv("STOCKLENS_MODE", "replay")
if MODE not in ("live", "replay"):
    raise ValueError(f"STOCKLENS_MODE must be 'live' or 'replay', got {MODE!r}")

TINYFISH_API_KEY = os.getenv("TINYFISH_API_KEY", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
DB_PATH = Path(os.getenv("STOCKLENS_DB_PATH", ROOT / "stocklens.db"))
FIXTURES_DIR = ROOT / "fixtures"
FRONTEND_DIR = ROOT / "frontend"

# --- Source tiers ------------------------------------------------------------

TIER_NAMES = {
    1: "Primary (exchange filing, official release)",
    2: "Reputable media",
    3: "Brokerage notes / blogs / social",
}

# --- Scoring -----------------------------------------------------------------

SCORING_POINTS = {
    "primary_source_confirms": 25,
    "independent_tier2_corroboration": 20,
    "timing_public_before_move": 20,
    "move_unusual_vs_sector_and_index": 15,
    "filing_and_media_agree": 10,
    "no_stronger_alternative": 10,
    "partly_anticipated": -10,
}

SCORE_MIN = 0
SCORE_MAX = 100

# Minimum score for each label, checked highest first; anything below is Unknown.
LABEL_CUTOFFS = [
    ("High", 75),
    ("Medium", 50),
    ("Low", 25),
]
UNKNOWN_LABEL = "Unknown"
UNKNOWN_TEXT = "Cause not confidently identified"

# Label ordering, used to apply caps.
LABEL_RANK = {"Unknown": 0, "Low": 1, "Medium": 2, "High": 3}

# --- Hard gates --------------------------------------------------------------

# Gate 4: share of the stock's move that sector/index must explain for the
# market explanation to lead.
MARKET_EXPLAINS_THRESHOLD = 0.6

GATES = [
    {"id": 1, "name": "no_evidence_before_move", "effect": "force", "label": "Unknown",
     "description": "No evidence public before move start -> Unknown."},
    {"id": 2, "name": "only_tier3_or_unverified", "effect": "cap", "label": "Low",
     "description": "Only Tier 3 or unverified evidence -> cap at Low."},
    {"id": 3, "name": "single_independence_group", "effect": "cap", "label": "Medium",
     "description": "Only one independence group -> cap at Medium."},
    {"id": 4, "name": "market_explains_move", "effect": "market_leads", "label": None,
     "description": "If sector/index explains most of the move -> market explanation leads."},
    {"id": 5, "name": "missing_or_ambiguous_timestamp", "effect": "exclude", "label": None,
     "description": "Evidence with missing or ambiguous timestamp is excluded."},
    {"id": 6, "name": "unresolved_skeptic_counter", "effect": "cap", "label": "Medium",
     "description": "Unresolved Skeptic counter-evidence -> cap at Medium."},
]

# --- Consensus engine tunables (backend/consensus/) -------------------------
# See "Consensus assumptions" in SPEC.md for the rules these feed.

# Human-readable names for score_breakdown entries, keyed like SCORING_POINTS.
RULE_LABELS = {
    "primary_source_confirms": "Primary source confirms",
    "independent_tier2_corroboration": "Independent Tier 2 corroboration",
    "timing_public_before_move": "Public before the move started",
    "move_unusual_vs_sector_and_index": "Move is unusual versus sector and index",
    "filing_and_media_agree": "Filing and media agree",
    "no_stronger_alternative": "No stronger alternative explanation",
    "partly_anticipated": "Partly anticipated (earlier hints)",
}

# Independence: two snippets at or above this difflib ratio are the same story.
NEAR_DUPLICATE_SIMILARITY = 0.85

# Two claims at or above this difflib ratio are treated as saying the same thing.
CLAIM_AGREEMENT_SIMILARITY = 0.6

# A verified Tier 2 report published after the move began may still count as
# corroboration if it agrees with a verified Tier 1 source public before the move.
ALLOW_LATE_TIER2_CORROBORATION = True

# A timestamp of exactly 00:00:00 IST on the move's own day is treated as a
# date-only (ambiguous) timestamp and excluded (gate 5).
MIDNIGHT_ON_MOVE_DAY_IS_AMBIGUOUS = True

# Market context: sector/index share of the move below this is "unusual" (+15).
UNUSUAL_MOVE_MAX_MARKET_SHARE = 0.3

# Bands for the "sector weakness: low/medium/high" alternatives text.
MARKET_SHARE_LEVELS = [
    ("high", MARKET_EXPLAINS_THRESHOLD),
    ("medium", UNUSUAL_MOVE_MAX_MARKET_SHARE),
    ("low", 0.0),
]

CARD_LIMITS = [
    "An attribution backed by evidence, not proof of causation. Not investment advice.",
]

# --- Trigger engine tunables (backend/trigger/) ------------------------------
# A move is flagged only when BOTH thresholds pass. Set low enough that all
# three demo fixtures trigger (case_market_wide is -2.6% on 1.8x volume).

# Minimum absolute percent change over the window (rises and falls).
TRIGGER_MIN_PCT_CHANGE = 2.5

# Minimum window volume divided by normal volume for a window of the same length.
TRIGGER_MIN_VOLUME_MULTIPLE = 1.5

# Window length used when the caller does not give one.
TRIGGER_DEFAULT_WINDOW_MINUTES = 30

# --- Investigators, TinyFish and pipeline tunables ---------------------------
# (backend/investigators/, backend/tinyfish/, backend/pipeline.py)

# Hard limit on live TinyFish calls for one investigation (all investigators
# together). Credits are tight, so the budget raises instead of overspending.
MAX_TINYFISH_CALLS_PER_INVESTIGATION = 10

# Sherlock: evidence hosted on these domains is an exchange filing (Tier 1);
# anything else Sherlock finds is company news (Tier 2).
EXCHANGE_FILING_DOMAINS = ("nseindia.com", "bseindia.com")

# Replay mode reads fixtures/<case>/<this folder>/<investigator>.json.
REPLAY_SUBDIR = "tinyfish"
