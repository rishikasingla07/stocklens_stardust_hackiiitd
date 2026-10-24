"""Trigger engine: detects unusual moves with deterministic price and volume rules only (no AI).

Flags a move when the percent change over a window and the volume multiple pass
thresholds, then creates the Move and starts the investigation.
TinyFish Monitor is separate: it watches sources for new filings and does not detect price moves.
"""
