"""
Warrant scoring evaluation and component functions.

This module provides pure, reusable warrant scoring helpers extracted
from WarrantSelectionAgent. All functions are stateless and accept
structured input data.
"""

import math
from dataclasses import dataclass
from datetime import date


@dataclass
class WarrantScoringConfig:
    """Configuration for warrant scoring component weights and parameters."""
    spread_weight: float = 0.40
    spread_cutoff_pct: float = 3.0
    
    leverage_weight: float = 0.25
    leverage_mean: float = 5.0
    leverage_sigma: float = 3.0
    
    days_weight: float = 0.20
    days_mean: float = 315  # ~midpoint of 9–12 month window
    days_sigma: float = 45.0
    
    delta_weight: float = 0.15
    delta_peak: float = 0.5
    delta_half_width: float = 0.5


def score_spread(spread_pct: float | None, config: WarrantScoringConfig) -> float:
    """
    Score spread as fraction of max weight. Lower spread is better.
    Linear: 0% → 1.0, 3% → 0.0, >3% → 0.0.
    """
    if spread_pct is None:
        return 0.0
    component = max(0.0, 1.0 - spread_pct / config.spread_cutoff_pct)
    return config.spread_weight * component


def score_leverage(leverage: float | None, config: WarrantScoringConfig) -> float:
    """
    Score leverage as Gaussian centered at mean (default 5×).
    Peak contribution is at leverage_mean, decays symmetrically.
    """
    if leverage is None or leverage <= 0:
        return 0.0
    component = math.exp(-0.5 * ((leverage - config.leverage_mean) / config.leverage_sigma) ** 2)
    return config.leverage_weight * component


def score_days_to_expiry(maturity_date: str | None, today: date, config: WarrantScoringConfig) -> float:
    """
    Score days to expiry as Gaussian centered at days_mean (default 315 days ~9–12 month midpoint).
    Returns 0 if maturity_date cannot be parsed or days <= 0.
    """
    if maturity_date is None:
        return 0.0
    try:
        maturity = date.fromisoformat(str(maturity_date))
        days = (maturity - today).days
        if days <= 0:
            return 0.0
        component = math.exp(-0.5 * ((days - config.days_mean) / config.days_sigma) ** 2)
        return config.days_weight * component
    except (ValueError, TypeError):
        return 0.0


def score_delta(delta: float | None, config: WarrantScoringConfig) -> float:
    """
    Score delta (for ATM calls, ideally 0.5) as linear falloff from peak.
    Peak at delta_peak (0.5), linear decay with half-width delta_half_width (0.5).
    """
    if delta is None:
        return 0.0
    component = max(0.0, 1.0 - abs(delta - config.delta_peak) / config.delta_half_width)
    return config.delta_weight * component


def compute_warrant_score(
    spread_pct: float | None,
    leverage: float | None,
    maturity_date: str | None,
    delta: float | None,
    today: date,
    config: WarrantScoringConfig | None = None,
) -> float:
    """
    Compute total warrant score as sum of weighted components.
    
    Args:
        spread_pct: bid-ask spread as a percentage (e.g., 0.5 for 0.5%)
        leverage: warrant leverage as multiple (e.g., 5.0 for 5×)
        maturity_date: ISO date string (YYYY-MM-DD)
        delta: option delta (0.0 to 1.0)
        today: reference date for days-to-expiry calculation
        config: scoring configuration; uses default if None
    
    Returns:
        Scalar score (typically 0.0 to ~1.0, sum of component weights).
    """
    if config is None:
        config = WarrantScoringConfig()
    
    return (
        score_spread(spread_pct, config)
        + score_leverage(leverage, config)
        + score_days_to_expiry(maturity_date, today, config)
        + score_delta(delta, config)
    )
