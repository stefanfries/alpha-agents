import pytest

from app.models.market import Ticker
from app.models.signals import UniverseResult
from scripts.analyze_adx_slope import SignalEvent, churn_rate, matching_delays
from scripts.analyze_entry_extension import (
    ExtensionObservation,
    extension_quintiles,
    resolve_index_tickers,
)
from scripts.analyze_relative_strength import (
    RelativeStrengthObservation,
    relative_tq_quintiles,
)
from scripts.analyze_volume_confirmation import VolumeObservation, volume_quintiles
from scripts.analyze_weekly_confirmation import churn_counts


def test_extension_quintiles_are_ordered_and_cover_every_observation():
    observations = [
        ExtensionObservation(symbol="A", date=f"2025-01-{index:02d}", extension_atr=float(index), returns_pct={})
        for index in range(1, 8)
    ]

    groups = extension_quintiles(list(reversed(observations)))

    assert [item.extension_atr for group in groups for item in group] == list(range(1, 8))
    assert [len(group) for group in groups] == [1, 1, 2, 1, 2]


@pytest.mark.asyncio
async def test_resolve_index_tickers_uses_universe_agent(monkeypatch):
    class FakeUniverseAgent:
        def __init__(self, _finhub, _wikipedia):
            pass

        async def run(self, _input):
            return UniverseResult(
                tickers=[Ticker(symbol="AAPL", isin="US0378331005")],
                source={"US0378331005": "NASDAQ100"},
                missing_isin=[],
                unresolved_indices=[],
            )

    class FakeTool:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("scripts.analyze_entry_extension.UniverseAgent", FakeUniverseAgent)
    monkeypatch.setattr("scripts.analyze_entry_extension.FinHubTool", FakeTool)
    monkeypatch.setattr("scripts.analyze_entry_extension.WikipediaIndexTool", FakeTool)

    result = await resolve_index_tickers("NASDAQ100")

    assert result == [Ticker(symbol="AAPL", isin="US0378331005")]


def test_adx_experiment_helpers_measure_churn_and_matching_delays():
    baseline = [SignalEvent(10, "NEW"), SignalEvent(15, "BREAK"), SignalEvent(30, "NEW")]
    variant = [SignalEvent(11, "NEW"), SignalEvent(18, "BREAK"), SignalEvent(30, "NEW")]

    assert churn_rate(baseline, 5) == 0.5
    assert matching_delays(baseline, variant, "BREAK") == [3]


def test_relative_tq_quintiles_are_ordered_and_cover_every_observation():
    observations = [
        RelativeStrengthObservation(
            symbol="A", date=f"2025-02-{index:02d}", relative_tq60=float(index), excess_returns_pct={}
        )
        for index in range(1, 8)
    ]

    groups = relative_tq_quintiles(list(reversed(observations)))

    assert [item.relative_tq60 for group in groups for item in group] == list(range(1, 8))
    assert [len(group) for group in groups] == [1, 1, 2, 1, 2]


def test_volume_quintiles_are_ordered_and_cover_every_observation():
    observations = [
        VolumeObservation(
            symbol="A", date=f"2025-03-{index:02d}", relative_volume20=float(index), returns_pct={}
        )
        for index in range(1, 8)
    ]

    groups = volume_quintiles(list(reversed(observations)))

    assert [item.relative_volume20 for group in groups for item in group] == list(range(1, 8))
    assert [len(group) for group in groups] == [1, 1, 2, 1, 2]


def test_weekly_confirmation_churn_uses_only_matching_new_events():
    events = [SignalEvent(10, "NEW"), SignalEvent(14, "BREAK"), SignalEvent(20, "NEW")]
    states = {10: True, 20: False}

    assert churn_counts(events, states, True, 5) == (1, 1)
    assert churn_counts(events, states, False, 5) == (0, 1)
