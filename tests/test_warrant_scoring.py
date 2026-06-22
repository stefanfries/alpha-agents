"""Tests for warrant scoring logic and component functions."""

import math
from datetime import date, timedelta

import pytest

from app.policies.warrant_scoring import (
    WarrantScoringConfig,
    compute_warrant_score,
    score_days_to_expiry,
    score_delta,
    score_leverage,
    score_spread,
)


class TestScoreSpread:
    """Test spread scoring component."""

    def test_zero_spread(self):
        """0% spread yields max contribution."""
        config = WarrantScoringConfig()
        assert score_spread(0.0, config) == pytest.approx(0.40)

    def test_three_percent_spread(self):
        """3% spread yields min contribution (0.0)."""
        config = WarrantScoringConfig()
        assert score_spread(3.0, config) == pytest.approx(0.0)

    def test_one_point_five_percent_spread(self):
        """1.5% spread yields 50% of weight."""
        config = WarrantScoringConfig()
        assert score_spread(1.5, config) == pytest.approx(0.20)

    def test_spread_above_cutoff_yields_zero(self):
        """Spread above cutoff yields 0."""
        config = WarrantScoringConfig()
        assert score_spread(5.0, config) == pytest.approx(0.0)

    def test_none_spread(self):
        """None spread yields 0."""
        config = WarrantScoringConfig()
        assert score_spread(None, config) == 0.0


class TestScoreLeverage:
    """Test leverage scoring component."""

    def test_leverage_at_mean(self):
        """Leverage at mean (5×) yields max contribution."""
        config = WarrantScoringConfig()
        assert score_leverage(5.0, config) == pytest.approx(0.25)

    def test_leverage_zero_or_negative(self):
        """Zero or negative leverage yields 0."""
        config = WarrantScoringConfig()
        assert score_leverage(0.0, config) == 0.0
        assert score_leverage(-1.0, config) == 0.0

    def test_leverage_at_mean_plus_sigma(self):
        """Leverage at mean ± sigma yields ~0.12 (exp(-0.5) ≈ 0.606)."""
        config = WarrantScoringConfig()
        result = score_leverage(8.0, config)  # 5 + 3 sigma
        expected = 0.25 * math.exp(-0.5)
        assert result == pytest.approx(expected)

    def test_none_leverage(self):
        """None leverage yields 0."""
        config = WarrantScoringConfig()
        assert score_leverage(None, config) == 0.0


class TestScoreDaysToExpiry:
    """Test days-to-expiry scoring component."""

    def test_days_at_mean(self):
        """Days at mean (315) yields max contribution."""
        today = date(2026, 6, 22)
        maturity = (today + timedelta(days=315)).isoformat()
        config = WarrantScoringConfig()
        assert score_days_to_expiry(maturity, today, config) == pytest.approx(0.20)

    def test_days_past_today_zero(self):
        """Maturity on or before today yields 0."""
        today = date(2026, 6, 22)
        maturity = today.isoformat()
        config = WarrantScoringConfig()
        assert score_days_to_expiry(maturity, today, config) == 0.0

    def test_days_at_mean_plus_sigma(self):
        """Days at mean ± sigma yields ~0.10 (exp(-0.5) ≈ 0.606)."""
        today = date(2026, 6, 22)
        maturity = (today + timedelta(days=360)).isoformat()  # 315 + 45
        config = WarrantScoringConfig()
        result = score_days_to_expiry(maturity, today, config)
        expected = 0.20 * math.exp(-0.5)
        assert result == pytest.approx(expected)

    def test_invalid_date_format(self):
        """Invalid date format yields 0."""
        config = WarrantScoringConfig()
        assert score_days_to_expiry("invalid", date(2026, 6, 22), config) == 0.0

    def test_none_maturity(self):
        """None maturity yields 0."""
        config = WarrantScoringConfig()
        assert score_days_to_expiry(None, date(2026, 6, 22), config) == 0.0


class TestScoreDelta:
    """Test delta scoring component."""

    def test_delta_at_peak(self):
        """Delta at peak (0.5) yields max contribution."""
        config = WarrantScoringConfig()
        assert score_delta(0.5, config) == pytest.approx(0.15)

    def test_delta_zero(self):
        """Delta at 0.0 yields 0 (at edge of half-width)."""
        config = WarrantScoringConfig()
        assert score_delta(0.0, config) == pytest.approx(0.0)

    def test_delta_one(self):
        """Delta at 1.0 yields 0 (at edge of half-width)."""
        config = WarrantScoringConfig()
        assert score_delta(1.0, config) == pytest.approx(0.0)

    def test_delta_at_peak_plus_quarter_width(self):
        """Delta at peak ± 0.25 yields 50% of weight."""
        config = WarrantScoringConfig()
        assert score_delta(0.75, config) == pytest.approx(0.075)
        assert score_delta(0.25, config) == pytest.approx(0.075)

    def test_none_delta(self):
        """None delta yields 0."""
        config = WarrantScoringConfig()
        assert score_delta(None, config) == 0.0


class TestComputeWarrantScore:
    """Test full warrant score computation."""

    def test_all_none_fields(self):
        """All None fields yield total score of 0."""
        today = date(2026, 6, 22)
        score = compute_warrant_score(None, None, None, None, today)
        assert score == 0.0

    def test_ideal_warrant(self):
        """Ideal warrant with optimal values across components."""
        today = date(2026, 6, 22)
        config = WarrantScoringConfig()
        
        # Optimal values
        spread_pct = 0.0  # best
        leverage = 5.0  # at mean
        maturity = (today + timedelta(days=315)).isoformat()  # at mean
        delta = 0.5  # at peak
        
        score = compute_warrant_score(spread_pct, leverage, maturity, delta, today, config)
        expected = 0.40 + 0.25 + 0.20 + 0.15
        assert score == pytest.approx(expected)

    def test_partial_data_warrant(self):
        """Warrant with only some fields populated."""
        today = date(2026, 6, 22)
        config = WarrantScoringConfig()
        
        # Only spread and leverage
        spread_pct = 1.0
        leverage = 5.0
        
        score = compute_warrant_score(spread_pct, leverage, None, None, today, config)
        spread_component = score_spread(1.0, config)
        leverage_component = score_leverage(5.0, config)
        expected = spread_component + leverage_component
        assert score == pytest.approx(expected)

    def test_custom_config(self):
        """Custom config with different weights."""
        today = date(2026, 6, 22)
        config = WarrantScoringConfig(
            spread_weight=0.5,
            leverage_weight=0.5,
            days_weight=0.0,
            delta_weight=0.0,
        )
        
        spread_pct = 0.0
        leverage = 5.0
        
        score = compute_warrant_score(spread_pct, leverage, None, None, today, config)
        expected = 0.5 + 0.5  # both at max
        assert score == pytest.approx(expected)

    def test_default_config_created(self):
        """compute_warrant_score creates default config if None."""
        today = date(2026, 6, 22)
        maturity = (today + timedelta(days=315)).isoformat()
        
        score_with_default = compute_warrant_score(0.0, 5.0, maturity, 0.5, today, None)
        score_with_explicit = compute_warrant_score(0.0, 5.0, maturity, 0.5, today, WarrantScoringConfig())
        
        assert score_with_default == pytest.approx(score_with_explicit)


class TestConfigDefaults:
    """Test default configuration values."""

    def test_config_has_correct_defaults(self):
        """WarrantScoringConfig has expected default values."""
        config = WarrantScoringConfig()
        assert config.spread_weight == 0.40
        assert config.spread_cutoff_pct == 3.0
        assert config.leverage_weight == 0.25
        assert config.leverage_mean == 5.0
        assert config.leverage_sigma == 3.0
        assert config.days_weight == 0.20
        assert config.days_mean == 315
        assert config.days_sigma == 45.0
        assert config.delta_weight == 0.15
        assert config.delta_peak == 0.5
        assert config.delta_half_width == 0.5
