"""
Comprehensive tests for monitoring enhancements (Step 2-6).

Tests cover:
- _monitoring_score() method with various snapshot states
- PositionReview field population from warrant snapshots
- decision_reason setting for SELL/ROLL/KEEP actions
- Orchestrator metadata collection and wiring
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.agents.monitoring import MonitoringAgent, MonitoringInput, WarrantSnapshot
from app.config import MonitoringSettings
from app.models.market import Position, Ticker
from app.models.signals import (
    MonitoringResult,
    PositionReview,
    ResearchResult,
    RollReplacement,
    SelectionResult,
    WarrantSelectionResult,
)


class TestMonitoringScore:
    """Tests for _monitoring_score() method."""

    def test_monitoring_score_returns_none_for_none_snapshot(self):
        """Test that _monitoring_score returns None when snapshot is None."""
        settings = MonitoringSettings()
        agent = MonitoringAgent(settings=settings)
        result = agent._monitoring_score(None)
        assert result is None

    def test_monitoring_score_returns_valid_float_for_full_snapshot(self):
        """Test that _monitoring_score returns 0-1 float for complete snapshot."""
        settings = MonitoringSettings()
        agent = MonitoringAgent(settings=settings)
        snapshot = WarrantSnapshot(
            warrant_isin="TEST_ISIN",
            spread_pct=1.5,
            leverage=5.0,
            delta=0.5,
            days_to_maturity=90,
        )
        score = agent._monitoring_score(snapshot)
        assert score is not None
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_monitoring_score_penalizes_high_spread(self):
        """Test that higher spread lowers score."""
        settings = MonitoringSettings()
        agent = MonitoringAgent(settings=settings)
        snapshot_good = WarrantSnapshot(
            warrant_isin="GOOD",
            spread_pct=1.0,  # low spread
            leverage=5.0,
            delta=0.5,
            days_to_maturity=90,
        )
        snapshot_bad = WarrantSnapshot(
            warrant_isin="BAD",
            spread_pct=4.0,  # high spread
            leverage=5.0,
            delta=0.5,
            days_to_maturity=90,
        )
        score_good = agent._monitoring_score(snapshot_good)
        score_bad = agent._monitoring_score(snapshot_bad)
        assert score_good is not None
        assert score_bad is not None
        assert score_good > score_bad, "Higher spread should result in lower score"

    def test_monitoring_score_penalizes_extreme_leverage(self):
        """Test that leverage outside 3-8x range lowers score."""
        settings = MonitoringSettings()
        agent = MonitoringAgent(settings=settings)
        snapshot_optimal = WarrantSnapshot(
            warrant_isin="OPT",
            spread_pct=1.5,
            leverage=5.5,  # middle of optimal range
            delta=0.5,
            days_to_maturity=90,
        )
        snapshot_low_lev = WarrantSnapshot(
            warrant_isin="LOW",
            spread_pct=1.5,
            leverage=2.0,  # too low
            delta=0.5,
            days_to_maturity=90,
        )
        score_opt = agent._monitoring_score(snapshot_optimal)
        score_low = agent._monitoring_score(snapshot_low_lev)
        assert score_opt is not None
        assert score_low is not None
        assert score_opt > score_low, "Leverage outside range should lower score"

    def test_monitoring_score_penalizes_short_maturity(self):
        """Test that short maturity (< 60 days) lowers score."""
        settings = MonitoringSettings()
        agent = MonitoringAgent(settings=settings)
        snapshot_good = WarrantSnapshot(
            warrant_isin="GOOD",
            spread_pct=1.5,
            leverage=5.0,
            delta=0.5,
            days_to_maturity=90,  # good maturity
        )
        snapshot_short = WarrantSnapshot(
            warrant_isin="SHORT",
            spread_pct=1.5,
            leverage=5.0,
            delta=0.5,
            days_to_maturity=30,  # short maturity
        )
        score_good = agent._monitoring_score(snapshot_good)
        score_short = agent._monitoring_score(snapshot_short)
        assert score_good is not None
        assert score_short is not None
        assert score_good > score_short, "Short maturity should lower score"

    def test_monitoring_score_penalizes_extreme_delta(self):
        """Test that delta outside 0.3-0.7 range lowers score."""
        settings = MonitoringSettings()
        agent = MonitoringAgent(settings=settings)
        snapshot_optimal = WarrantSnapshot(
            warrant_isin="OPT",
            spread_pct=1.5,
            leverage=5.0,
            delta=0.5,  # middle of optimal range
            days_to_maturity=90,
        )
        snapshot_high_delta = WarrantSnapshot(
            warrant_isin="HIGH",
            spread_pct=1.5,
            leverage=5.0,
            delta=0.9,  # too high
            days_to_maturity=90,
        )
        score_opt = agent._monitoring_score(snapshot_optimal)
        score_high = agent._monitoring_score(snapshot_high_delta)
        assert score_opt is not None
        assert score_high is not None
        assert score_opt > score_high, "Delta outside range should lower score"


class TestPositionReviewFieldPopulation:
    """Tests for populating PositionReview fields from warrant snapshots."""

    def test_position_review_has_all_new_fields(self):
        """Test that PositionReview model has all new health fields."""
        review = PositionReview(
            underlying_symbol="A",
            warrant_isin="ISIN1",
            warrant_wkn="WKN1",
        )
        assert hasattr(review, "spread_pct")
        assert hasattr(review, "leverage")
        assert hasattr(review, "delta")
        assert hasattr(review, "days_to_maturity")
        assert hasattr(review, "monitoring_score")
        assert hasattr(review, "decision_reason")
        # Fields should be None by default
        assert review.spread_pct is None
        assert review.leverage is None
        assert review.delta is None
        assert review.days_to_maturity is None
        assert review.monitoring_score is None
        assert review.decision_reason is None

    def test_position_review_with_all_health_fields_set(self):
        """Test PositionReview can be created with all health fields."""
        review = PositionReview(
            underlying_symbol="A",
            warrant_isin="ISIN1",
            warrant_wkn="WKN1",
            spread_pct=1.5,
            leverage=5.0,
            delta=0.5,
            days_to_maturity=90,
            monitoring_score=0.85,
            decision_reason="warrant healthy, trend intact",
        )
        assert review.spread_pct == 1.5
        assert review.leverage == 5.0
        assert review.delta == 0.5
        assert review.days_to_maturity == 90
        assert review.monitoring_score == 0.85
        assert review.decision_reason == "warrant healthy, trend intact"


class TestDecisionReason:
    """Tests for decision_reason field being set correctly."""

    def test_monitoring_result_has_decision_reason_field(self):
        """Test that PositionReview has decision_reason field in MonitoringResult."""
        review = PositionReview(
            underlying_symbol="A",
            warrant_isin="ISIN1",
            warrant_wkn="WKN1",
            decision_reason="test reason",
        )
        result = MonitoringResult(
            positions_to_sell=[review],
            positions_to_keep=[],
            positions_to_roll=[],
            entry_candidates=[],
            free_positions=5,
            excluded_symbols=[],
        )
        assert result.positions_to_sell[0].decision_reason == "test reason"


class TestOrchestrationMetadataFields:
    """Tests for metadata fields on MonitoringResult and WarrantSelectionResult."""

    def test_monitoring_result_has_metadata_fields(self):
        """Test that MonitoringResult has metadata fields."""
        result = MonitoringResult(
            positions_to_sell=[],
            positions_to_keep=[],
            positions_to_roll=[],
            entry_candidates=[],
            free_positions=5,
            excluded_symbols=[],
            keep_existing_isins=["ISIN1"],
            roll_underlyings=["A"],
            roll_keep_underlyings=["B"],
        )
        assert result.keep_existing_isins == ["ISIN1"]
        assert result.roll_underlyings == ["A"]
        assert result.roll_keep_underlyings == ["B"]

    def test_monitoring_result_metadata_fields_default_to_empty(self):
        """Test that metadata fields default to empty lists."""
        result = MonitoringResult(
            positions_to_sell=[],
            positions_to_keep=[],
            positions_to_roll=[],
            entry_candidates=[],
            free_positions=5,
            excluded_symbols=[],
        )
        assert result.keep_existing_isins == []
        assert result.roll_underlyings == []
        assert result.roll_keep_underlyings == []

    def test_warrant_selection_result_has_metadata_fields(self):
        """Test that WarrantSelectionResult has metadata fields."""
        result = WarrantSelectionResult(
            selected=[],
            top3={},
            analyzed_count={},
            skipped=[],
            keep_existing_isins=["ISIN1"],
            roll_underlyings=["A"],
            roll_keep_underlyings=["B"],
        )
        assert result.keep_existing_isins == ["ISIN1"]
        assert result.roll_underlyings == ["A"]
        assert result.roll_keep_underlyings == ["B"]

    def test_warrant_selection_result_metadata_fields_default_to_empty(self):
        """Test that WarrantSelectionResult metadata fields default to empty lists."""
        result = WarrantSelectionResult(
            selected=[],
            top3={},
            analyzed_count={},
            skipped=[],
        )
        assert result.keep_existing_isins == []
        assert result.roll_underlyings == []
        assert result.roll_keep_underlyings == []


@pytest.mark.asyncio
async def test_position_review_captures_snapshot_data(monkeypatch):
    """Test that monitoring agent populates PositionReview with snapshot data."""
    from app.orchestrator import Pipeline

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict):
        return [
            Position(
                ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                quantity=Decimal("1"),
                avg_cost=Decimal("0"),
            )
        ]

    async def fake_warrant_underlying_map(_run: dict, _current_holdings=None):
        return {"ISIN1": "A"}

    async def fake_held_since(_run: dict):
        return {"WKN1": date.today() - timedelta(days=30)}

    async def fake_break_confirmed(_run: dict, _screening: SelectionResult):
        return set()

    async def fake_snapshots(isins: list[str]):
        return {
            "ISIN1": WarrantSnapshot(
                warrant_isin="ISIN1",
                spread_pct=1.8,
                leverage=5.5,
                delta=0.5,
                days_to_maturity=100,
            )
        }

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_warrant_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_held_since", fake_held_since)
    monkeypatch.setattr(pipeline, "_break_confirmed_symbols", fake_break_confirmed)
    monkeypatch.setattr(pipeline, "_fetch_warrant_snapshots", fake_snapshots)

    screening = SelectionResult(
        selected=[Ticker(symbol="A")],
        scores={"A": 1.0},
        rationale={},
        trend_signals={"A": "HOLD"},
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 5}},
    }

    result = await pipeline._run_monitoring(run)

    # Should be in keep since trend is HOLD and warrant is not degraded
    assert len(result.positions_to_keep) > 0
    position = result.positions_to_keep[0]
    
    # Verify snapshot data was copied to PositionReview
    assert position.spread_pct == 1.8
    assert position.leverage == 5.5
    assert position.delta == 0.5
    assert position.days_to_maturity == 100
    assert position.monitoring_score is not None
    assert position.monitoring_score > 0.0
    assert position.decision_reason == "warrant healthy, trend intact"


@pytest.mark.asyncio
async def test_decision_reason_set_on_sell(monkeypatch):
    """Test that decision_reason is set correctly on SELL action."""
    from app.config import MonitoringSettings
    from app.orchestrator import Pipeline

    # Config to trigger degradation
    settings = MonitoringSettings(
        spread_max_pct=1.0,  # threshold to trigger degradation
        leverage_min=3.0,
        leverage_max=8.0,
        days_to_maturity_min=60,
        delta_min=0.3,
        delta_max=0.7,
    )

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict):
        return [
            Position(
                ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                quantity=Decimal("1"),
                avg_cost=Decimal("0"),
            )
        ]

    async def fake_warrant_underlying_map(_run: dict, _current_holdings=None):
        return {"ISIN1": "A"}

    async def fake_held_since(_run: dict):
        return {"WKN1": date.today() - timedelta(days=180)}

    async def fake_break_confirmed(_run: dict, _screening: SelectionResult):
        return {"A"}  # Confirm break for A

    async def fake_snapshots(isins: list[str]):
        return {
            "ISIN1": WarrantSnapshot(
                warrant_isin="ISIN1",
                spread_pct=1.5,
                leverage=5.0,
                delta=0.5,
                days_to_maturity=100,
            )
        }

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_warrant_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_held_since", fake_held_since)
    monkeypatch.setattr(pipeline, "_break_confirmed_symbols", fake_break_confirmed)
    monkeypatch.setattr(pipeline, "_fetch_warrant_snapshots", fake_snapshots)

    screening = SelectionResult(
        selected=[Ticker(symbol="A")],
        scores={"A": 1.0},
        rationale={},
        trend_signals={"A": "BREAK"},  # BREAK signal
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 5}},
    }

    result = await pipeline._run_monitoring(run)

    # Should be in sell due to BREAK confirmed
    assert len(result.positions_to_sell) > 0
    position = result.positions_to_sell[0]
    assert position.decision_reason == "trend break confirmed"


@pytest.mark.asyncio
async def test_orchestrator_collects_keep_existing_metadata(monkeypatch):
    """Monitoring metadata should expose roll underlyings without replacement resolution."""
    from app.orchestrator import Pipeline

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict):
        return [
            Position(
                ticker=Ticker(symbol="WKN1", isin="ISIN_A"),
                quantity=Decimal("1"),
                avg_cost=Decimal("0"),
            )
        ]

    async def fake_warrant_underlying_map(_run: dict, _current_holdings=None):
        return {"ISIN_A": "A"}

    async def fake_held_since(_run: dict):
        return {"WKN1": date.today() - timedelta(days=30)}

    async def fake_break_confirmed(_run: dict, _screening: SelectionResult):
        return set()

    async def fake_snapshots(isins: list[str]):
        return {
            "ISIN_A": WarrantSnapshot(
                warrant_isin="ISIN_A",
                spread_pct=3.5,  # degraded
                leverage=5.0,
                delta=0.5,
                days_to_maturity=100,
            )
        }

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_warrant_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_held_since", fake_held_since)
    monkeypatch.setattr(pipeline, "_break_confirmed_symbols", fake_break_confirmed)
    monkeypatch.setattr(pipeline, "_fetch_warrant_snapshots", fake_snapshots)

    screening = SelectionResult(
        selected=[Ticker(symbol="A")],
        scores={"A": 1.0},
        rationale={},
        trend_signals={"A": "HOLD"},
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 5}},
    }

    result = await pipeline._run_monitoring(run)

    # Monitoring is classification-only: roll symbols are exposed, keep/replacement metadata is empty.
    assert result.keep_existing_isins == []
    assert "A" in result.roll_underlyings
    assert result.roll_keep_underlyings == []


@pytest.mark.asyncio
async def test_missing_trend_signal_key_does_not_sell():
    """If symbol is absent from trend_signals, monitoring must not treat it as confirmed earlier BREAK."""
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)
    position = Position(
        ticker=Ticker(symbol="WKN1", isin="ISIN1"),
        quantity=Decimal("1"),
        avg_cost=Decimal("0"),
    )
    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={},
            underlying_names={"ASML": "ASML Holding N.V."},
            current_holdings=[position],
            warrant_underlying_map={"ISIN1": "ASML"},
            held_since_map={},
            warrant_snapshots={},
            break_confirmed_symbols=set(),
            max_positions=5,
        )
    )

    assert len(result.positions_to_sell) == 0
    assert len(result.positions_to_keep) == 1
    assert result.positions_to_keep[0].decision_reason == "no signal"


@pytest.mark.asyncio
async def test_run_monitoring_normalizes_dotted_symbol_to_screening_symbol(monkeypatch):
    from app.orchestrator import Pipeline

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict):
        return [
            Position(
                ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                quantity=Decimal("1"),
                avg_cost=Decimal("0"),
            )
        ]

    async def fake_warrant_underlying_map(_run: dict, _current_holdings=None):
        return {"ISIN1": "ASML.AS"}

    async def fake_held_since(_run: dict):
        return {"WKN1": date.today() - timedelta(days=30)}

    async def fake_break_confirmed(_run: dict, _screening: SelectionResult):
        return set()

    async def fake_snapshots(_isins: list[str]):
        return {}

    async def fake_names_from_universe(**_kwargs):
        return {}

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_warrant_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_held_since", fake_held_since)
    monkeypatch.setattr(pipeline, "_break_confirmed_symbols", fake_break_confirmed)
    monkeypatch.setattr(pipeline, "_fetch_warrant_snapshots", fake_snapshots)
    monkeypatch.setattr(pipeline, "_resolve_underlying_names_from_universe", fake_names_from_universe)

    screening = SelectionResult(
        selected=[Ticker(symbol="ASML")],
        scores={"ASML": 1.0},
        rationale={},
        trend_signals={"ASML": "HOLD"},
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 5}},
    }

    result = await pipeline._run_monitoring(run)
    assert len(result.positions_to_sell) == 0
    assert len(result.positions_to_keep) == 1
    assert result.positions_to_keep[0].underlying_symbol == "ASML"
    assert result.positions_to_keep[0].decision_reason == "warrant healthy, trend intact"
