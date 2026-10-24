"""Pydantic models shared across StockLens: Evidence, Move, Report, Card."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

InvestigatorName = Literal["atlas", "sherlock", "echo", "skeptic"]
ConfidenceLabel = Literal["High", "Medium", "Low", "Unknown"]


def _to_ist(value: datetime | None) -> datetime | None:
    """Require a timezone-aware datetime and normalise it to IST."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(IST)


class Evidence(BaseModel):
    id: str
    claim: str
    source_name: str
    source_tier: Literal[1, 2, 3]
    url: str
    # None = missing timestamp; such evidence is excluded by gate 5.
    first_public_at: datetime | None
    snippet: str
    investigator: InvestigatorName
    independence_group: str | None = None
    verified: bool = False

    _ist = field_validator("first_public_at")(lambda cls, v: _to_ist(v))


class Move(BaseModel):
    ticker: str
    move_start_at: datetime
    window_minutes: int
    pct_change: float
    volume_multiple: float
    sector_pct_change: float
    index_pct_change: float

    _ist = field_validator("move_start_at")(lambda cls, v: _to_ist(v))


class Report(BaseModel):
    investigator: InvestigatorName
    evidence: list[Evidence] = Field(default_factory=list)
    notes: str = ""


class RejectedEvidence(BaseModel):
    evidence: Evidence
    reason: str


class ScoreItem(BaseModel):
    rule: str
    points: int


class Card(BaseModel):
    ticker: str
    move: Move
    confidence_score: int = Field(ge=0, le=100)
    confidence_label: ConfidenceLabel
    leading_explanation: str | None = None
    accepted_evidence: list[Evidence] = Field(default_factory=list)
    rejected_evidence: list[RejectedEvidence] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    score_breakdown: list[ScoreItem] = Field(default_factory=list)
    explanation_text: str = ""
    limits: list[str] = Field(default_factory=list)
