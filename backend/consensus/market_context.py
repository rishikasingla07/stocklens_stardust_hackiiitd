"""Market context: does sector/index movement explain most of the stock's move? (gate 4)."""

from dataclasses import dataclass, field

from backend import config
from backend.models import Move


@dataclass(frozen=True)
class MarketContext:
    sector_share: float
    index_share: float
    explained_share: float  # max of sector_share and index_share, 0..1
    market_leads: bool  # gate 4
    unusual: bool  # stock moved far more than sector and index
    summary: str
    alternatives: list[str] = field(default_factory=list)


def share_of_move(stock_pct: float, benchmark_pct: float) -> float:
    """Fraction (0..1) of the stock's move a same-direction benchmark move accounts for."""
    if stock_pct == 0 or benchmark_pct == 0 or (stock_pct > 0) != (benchmark_pct > 0):
        return 0.0
    return min(benchmark_pct / stock_pct, 1.0)


def level(share: float) -> str:
    for name, minimum in config.MARKET_SHARE_LEVELS:
        if share >= minimum:
            return name
    return "low"


def analyse(move: Move) -> MarketContext:
    sector = share_of_move(move.pct_change, move.sector_pct_change)
    index = share_of_move(move.pct_change, move.index_pct_change)
    explained = max(sector, index)
    leads = explained >= config.MARKET_EXPLAINS_THRESHOLD
    direction = "weakness" if move.pct_change < 0 else "strength"

    numbers = (
        f"sector {move.sector_pct_change:+.1f}% and index {move.index_pct_change:+.1f}% "
        f"explain {explained:.0%} of the stock's {move.pct_change:+.1f}% move"
    )
    if leads:
        summary = f"Market explanation leads: {numbers}."
    else:
        summary = f"Market explanation does not lead: {numbers}."

    return MarketContext(
        sector_share=sector,
        index_share=index,
        explained_share=explained,
        market_leads=leads,
        unusual=explained < config.UNUSUAL_MOVE_MAX_MARKET_SHARE,
        summary=summary,
        alternatives=[f"sector {direction}: {level(sector)}", f"index {direction}: {level(index)}"],
    )
