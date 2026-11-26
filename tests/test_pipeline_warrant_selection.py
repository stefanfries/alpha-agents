from datetime import date, timedelta

import pytest

from app.agents.warrant_selection import WarrantSelectionAgent
from app.models.market import Ticker
from app.models.signals import (
    ResearchResult,
    RollCandidate,
    SelectedWarrant,
    SelectionResult,
    WarrantSelectionResult,
)


def test_warrant_selection_extracts_midprice_from_bid_ask_quote():
    price = WarrantSelectionAgent._extract_quote_price(
        {
            "name": "ASML Holding",
            "isin": "NL0010273215",
            "bid": 1660.0,
            "ask": 1661.2,
            "spread_percent": 0.0722369371538674,
            "currency": "EUR",
        }
    )

    assert price == pytest.approx(1660.6)


@pytest.mark.asyncio
async def test_run_warrant_selection_uses_maturity_override(monkeypatch):
    from app import orchestrator as orchestrator_module

    captured: dict[str, float] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["min_days"] = kwargs["min_days_to_expiry"]
            captured["max_days"] = kwargs["max_days_to_expiry"]
            captured["strike_min_factor"] = kwargs["strike_min_factor"]
            captured["strike_max_factor"] = kwargs["strike_max_factor"]
            captured["min_score"] = kwargs["min_score"]
            captured["spread_max_pct"] = kwargs["spread_max_pct"]

        async def run(self, _input: SelectionResult) -> WarrantSelectionResult:
            return WarrantSelectionResult(selected=[], skipped=[])

    class FakeFinHub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    pipeline = orchestrator_module.Pipeline()

    async def fake_wake(*_args, **_kwargs) -> None:
        return None

    async def fake_overrides_map() -> dict[str, str]:
        return {}

    monkeypatch.setattr(orchestrator_module, "WarrantSelectionAgent", FakeAgent)
    monkeypatch.setattr(orchestrator_module, "FinHubTool", FakeFinHub)
    monkeypatch.setattr(orchestrator_module.warrant_availability, "overrides_map", fake_overrides_map)
    monkeypatch.setattr(pipeline, "_wake_finhub", fake_wake)

    run = {
        "execution_id": "exec4",
        "config_overrides": {
            "warrant_selection": {
                "min_days_to_expiry": 300,
                "max_days_to_expiry": 540,
                "strike_min_factor": 0.96,
                "strike_max_factor": 1.01,
                "min_score": 0.55,
                "spread_max_pct": 1.9,
            }
        },
        "stages": {
            "monitoring": {
                "result": {
                    "positions_to_sell": [],
                    "positions_to_keep": [],
                    "positions_to_roll": [],
                    "entry_candidates": [{"symbol": "A", "isin": None, "name": None}],
                    "free_positions": 1,
                    "excluded_symbols": [],
                    "keep_existing_isins": [],
                    "roll_underlyings": [],
                    "roll_keep_underlyings": [],
                }
            },
            "screening": {
                "result": SelectionResult(
                    selected=[Ticker(symbol="A")],
                    scores={"A": 1.0},
                    rationale={},
                ).model_dump(mode="json")
            },
            "research": {
                "result": ResearchResult(
                    tickers=[Ticker(symbol="A")],
                    bars={},
                    fundamentals={"A": {"currentPrice": 123.0}},
                ).model_dump(mode="json")
            },
        },
    }

    await pipeline._run_warrant_selection(run)

    assert captured == {
        "min_days": 300,
        "max_days": 540,
        "strike_min_factor": 0.96,
        "strike_max_factor": 1.01,
        "min_score": 0.55,
        "spread_max_pct": 1.9,
    }


@pytest.mark.asyncio
async def test_run_warrant_selection_honors_configured_spread_without_clamp(monkeypatch):
    from app import orchestrator as orchestrator_module

    captured: dict[str, float] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["spread_max_pct"] = kwargs["spread_max_pct"]

        async def run(self, _input: SelectionResult) -> WarrantSelectionResult:
            return WarrantSelectionResult(selected=[], skipped=[])

    class FakeFinHub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    pipeline = orchestrator_module.Pipeline()

    async def fake_wake(*_args, **_kwargs) -> None:
        return None

    async def fake_overrides_map() -> dict[str, str]:
        return {}

    monkeypatch.setattr(orchestrator_module, "WarrantSelectionAgent", FakeAgent)
    monkeypatch.setattr(orchestrator_module, "FinHubTool", FakeFinHub)
    monkeypatch.setattr(orchestrator_module.warrant_availability, "overrides_map", fake_overrides_map)
    monkeypatch.setattr(pipeline, "_wake_finhub", fake_wake)

    run = {
        "execution_id": "exec5",
        "config_overrides": {
            "warrant_selection": {
                "spread_max_pct": 20.0,
            },
            "monitoring": {
                "warrant_health": {
                    "spread_max_pct": 2.2,
                }
            },
        },
        "stages": {
            "monitoring": {
                "result": {
                    "positions_to_sell": [],
                    "positions_to_keep": [],
                    "positions_to_roll": [],
                    "entry_candidates": [{"symbol": "A", "isin": None, "name": None}],
                    "free_positions": 1,
                    "excluded_symbols": [],
                    "keep_existing_isins": [],
                    "roll_underlyings": [],
                    "roll_keep_underlyings": [],
                }
            },
            "screening": {
                "result": SelectionResult(
                    selected=[Ticker(symbol="A")],
                    scores={"A": 1.0},
                    rationale={},
                ).model_dump(mode="json")
            },
            "research": {
                "result": ResearchResult(
                    tickers=[Ticker(symbol="A")],
                    bars={},
                    fundamentals={"A": {"currentPrice": 123.0}},
                ).model_dump(mode="json")
            },
        },
    }

    await pipeline._run_warrant_selection(run)

    # The configured selection spread cap is honored verbatim (no clamp to monitoring).
    assert captured["spread_max_pct"] == 20.0


@pytest.mark.asyncio
async def test_warrant_selection_caps_to_max_selected_with_backfill():
    # Rank order A > B > C > D. B has no warrant; cap = 2 free slots.
    # Expect A and C selected (C backfills B's missing warrant); D dropped (no slot).
    class FakeFinHub:
        async def get_warrants(self, **kwargs):
            underlying = kwargs.get("underlying")
            if underlying == "ISIN_B":
                return []
            return [{"isin": f"W_{underlying}"}]

        async def get_warrant_detail(self, isin: str):
            return {
                "isin": isin,
                "wkn": isin,
                "market_data": {"spread_percent": 0.5, "bid": 1.0, "ask": 1.1},
                "analytics": {"leverage": 5.0, "delta": 0.5},
                "reference_data": {"maturity_date": (date.today() + timedelta(days=330)).isoformat(), "is_capped": False},
            }

    agent = WarrantSelectionAgent(
        finhub=FakeFinHub(),
        prices={"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0},
        max_selected=2,
    )

    result = await agent.run(
        SelectionResult(
            selected=[
                Ticker(symbol="A", isin="ISIN_A"),
                Ticker(symbol="B", isin="ISIN_B"),
                Ticker(symbol="C", isin="ISIN_C"),
                Ticker(symbol="D", isin="ISIN_D"),
            ],
            scores={"A": 1.0, "B": 0.9, "C": 0.8, "D": 0.7},
            rationale={},
        )
    )

    assert [w.underlying.symbol for w in result.selected] == ["A", "C"]
    assert result.skipped == ["B"]  # only the genuine no-warrant case
    assert "D" not in result.skipped  # overflow is dropped, not mislabeled as no-warrant


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ranking_metric", "expected_symbols", "benchmark", "partial_symbols"),
    [
        ("tsi", ["A", "B"], "ROLL", ["A"]),
        ("tq20", ["WEAK"], "ROLL", []),
        ("tq60", ["WEAK"], "KEEP", ["WEAK"]),
    ],
)
async def test_warrant_challengers_use_monitoring_ranks_within_screened_pool(
    monkeypatch, caplog, ranking_metric, expected_symbols, benchmark, partial_symbols,
):
    from app import orchestrator as orchestrator_module
    from app.models.signals import MarketGate, MonitoringResult, PositionReview
    from app.policies.screening_ranking import build_screening_ranks

    metrics = {
        "tsi": dict(A=90, B=70, TIE=60, WEAK=50, OUT=100, BLOCKED=95, KEEP=80, ROLL=60, SELL=-10),
        "tq20": dict(A=1, B=2, TIE=3, WEAK=4, OUT=6, BLOCKED=7, KEEP=5, ROLL=3, SELL=-10),
        "tq60": dict(A=-5, B=-4, TIE=-3, WEAK=-2, OUT=0, BLOCKED=1, KEEP=-3, ROLL=-1, SELL=-10),
    }
    screened = ["A", "B", "TIE", "WEAK", "BLOCKED"]
    screening = SelectionResult(
        selected=[Ticker(symbol=symbol) for symbol in screened],
        all_tickers=[Ticker(symbol=symbol) for symbol in metrics["tsi"]],
        scores=metrics["tq60"], tq_short=metrics["tq20"], tsi=metrics["tsi"],
        policy_results={symbol: {} for symbol in metrics["tsi"]}, rationale={},
        market_gate=MarketGate(status="open"),
    )
    reviews = {
        symbol: PositionReview(underlying_symbol=symbol, warrant_isin=symbol, warrant_wkn=symbol)
        for symbol in ("KEEP", "ROLL", "SELL")
    }
    monitoring = MonitoringResult(
        positions_to_keep=[reviews["KEEP"]], positions_to_roll=[reviews["ROLL"]],
        positions_to_sell=[reviews["SELL"]],
        entry_candidates=[Ticker(symbol=symbol) for symbol in ["A", "B", "TIE", "WEAK", "OUT"]],
        free_positions=0, excluded_symbols=["KEEP", "ROLL", "SELL", "BLOCKED"],
    )
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, selection):
            captured["selection"] = selection
            return WarrantSelectionResult(selected=[], skipped=[])

    class FakeFinHub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def fake_overrides():
        return {}

    async def fake_wake(*args):
        pass

    progress_updates = []

    async def fake_update_stage_progress(execution_id, stage, progress):
        progress_updates.append((execution_id, stage, progress))

    pipeline = orchestrator_module.Pipeline()
    monkeypatch.setattr(orchestrator_module, "WarrantSelectionAgent", FakeAgent)
    monkeypatch.setattr(orchestrator_module, "FinHubTool", FakeFinHub)
    monkeypatch.setattr(orchestrator_module.warrant_availability, "overrides_map", fake_overrides)
    monkeypatch.setattr(orchestrator_module, "update_stage_progress", fake_update_stage_progress)
    monkeypatch.setattr(pipeline, "_wake_finhub", fake_wake)
    run = {
        "execution_id": "challengers",
        "config_overrides": {"screening": {"ranking_metric": ranking_metric}},
        "stages": {
            "screening": {"result": screening.model_dump(mode="json")},
            "monitoring": {"result": monitoring.model_dump(mode="json")},
            "research": {"result": ResearchResult(tickers=[], bars={}, fundamentals={}).model_dump(mode="json")},
        },
    }

    result = await pipeline._run_warrant_selection(run)

    assert captured["challenger_symbols"] == expected_symbols
    assert captured["max_selected"] == 0
    assert captured["selection"].selected == monitoring.entry_candidates
    assert result.challenger_ranking_metric == ranking_metric
    assert result.challenger_benchmark_symbol == benchmark
    assert result.challenger_benchmark_rank == build_screening_ranks(screening, ranking_metric)[benchmark]["rank"]
    assert result.challenger_unranked_incumbents == []
    await captured["on_progress"]("roll", 1, 2, ["ROLL"])
    assert progress_updates[-1] == (
        "challengers", "warrant_selection",
        {"phase": "roll", "done": 1, "total": 2, "active": ["ROLL"],
         "challenger_count": len(expected_symbols)},
    )

    run["stages"]["screening"]["result"]["market_gate"] = {
        "status": "blocked", "reasons": ["Benchmark SuperTrend bearish"],
    }
    await pipeline._run_warrant_selection(run)
    assert [t.symbol for t in captured["selection"].selected] == ["A", "B", "TIE", "WEAK"]
    assert captured["challenger_symbols"] == expected_symbols
    assert captured["max_selected"] == 0
    run["stages"]["screening"]["result"]["market_gate"] = None
    await pipeline._run_warrant_selection(run)
    assert [t.symbol for t in captured["selection"].selected] == ["A", "B", "TIE", "WEAK"]
    assert captured["max_selected"] == 0
    run["stages"]["screening"]["result"]["market_gate"] = {"status": "open"}

    # Incumbents need not appear in Screening's top-N entry shortlist to be ranked.
    run["stages"]["screening"]["result"]["policy_results"].pop("ROLL")
    result = await pipeline._run_warrant_selection(run)
    assert captured["challenger_symbols"] == partial_symbols
    assert result.challenger_benchmark_symbol == "KEEP"
    assert result.challenger_unranked_incumbents == ["ROLL"]
    assert "benchmark uses ranked holdings only" in caplog.text

    run["stages"]["screening"]["result"]["policy_results"].pop("KEEP")
    result = await pipeline._run_warrant_selection(run)
    assert captured["challenger_symbols"] == []
    assert result.challenger_benchmark_symbol is None
    assert result.challenger_unranked_incumbents == ["KEEP", "ROLL"]

    run["stages"]["monitoring"]["result"]["positions_to_keep"] = []
    run["stages"]["monitoring"]["result"]["positions_to_roll"] = []
    result = await pipeline._run_warrant_selection(run)
    assert captured["challenger_symbols"] == []
    assert result.challenger_benchmark_symbol is None
    assert result.challenger_unranked_incumbents == []


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stage", ["screening", "monitoring", "unevaluated"])
async def test_blocked_market_gate_searches_only_advisory_challengers(monkeypatch, blocked_stage):
    from app import orchestrator as orchestrator_module
    from app.models.signals import MarketGate, MonitoringResult, PositionReview

    symbols = ["BETTER", "NO_WARRANT", "HELD", "RECENT", "WEAKER"]
    screening = SelectionResult(
        selected=[Ticker(symbol=symbol) for symbol in symbols],
        scores={}, rationale={},
        tsi={"BETTER": 90, "NO_WARRANT": 80, "HELD": 75, "RECENT": 70, "KEEP": 60, "WEAKER": 50},
        policy_results={symbol: {} for symbol in [*symbols, "KEEP"]},
        market_gate=MarketGate(status="blocked" if blocked_stage == "screening" else "open"),
    )
    if blocked_stage == "unevaluated":
        screening.market_gate = None
    monitoring = MonitoringResult(
        positions_to_keep=[PositionReview(underlying_symbol="KEEP", warrant_isin="W_KEEP", warrant_wkn="KEEP")],
        positions_to_roll=[], positions_to_sell=[],
        entry_candidates=[Ticker(symbol="WEAKER")],
        free_positions=3, excluded_symbols=["HELD", "RECENT"],
        market_gate=MarketGate(status="blocked" if blocked_stage == "monitoring" else "open"),
    )
    lookups = []

    async def fake_pick_best(self, ticker, *args):
        lookups.append(ticker.symbol)
        if ticker.symbol == "NO_WARRANT":
            return None, [], 0, "no suitable warrant"
        best = SelectedWarrant(
            underlying=ticker, warrant_isin=f"W_{ticker.symbol}",
            warrant_wkn=ticker.symbol, score=0.8, rationale="test",
        )
        return best, [best], 1, None

    class FakeFinHub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def fake_overrides():
        return {}

    async def fake_wake(*args):
        pass

    async def fake_progress(*args):
        pass

    pipeline = orchestrator_module.Pipeline()
    monkeypatch.setattr(WarrantSelectionAgent, "_pick_best", fake_pick_best)
    monkeypatch.setattr(orchestrator_module, "FinHubTool", FakeFinHub)
    monkeypatch.setattr(orchestrator_module.warrant_availability, "overrides_map", fake_overrides)
    monkeypatch.setattr(orchestrator_module, "update_stage_progress", fake_progress)
    monkeypatch.setattr(pipeline, "_wake_finhub", fake_wake)
    run = {
        "execution_id": "blocked",
        "config_overrides": {"screening": {"ranking_metric": "tsi", "market_gate_supertrend": True}},
        "stages": {
            "screening": {"result": screening.model_dump(mode="json")},
            "monitoring": {"result": monitoring.model_dump(mode="json")},
            "research": {"result": ResearchResult(tickers=[], bars={}, fundamentals={}).model_dump(mode="json")},
        },
    }

    result = await pipeline._run_warrant_selection(run)

    assert lookups == ["BETTER", "NO_WARRANT"]
    assert result.challenger_symbols == lookups
    assert [w.underlying.symbol for w in result.challenger_selected] == ["BETTER"]
    assert result.selected == []
    assert result.skipped_reasons == {"NO_WARRANT": "no suitable warrant"}
    assert result.entry_blocked_by_market_gate


@pytest.mark.asyncio
@pytest.mark.parametrize("max_selected", [0, -1, 1])
async def test_warrant_challengers_reuse_entry_lookups_without_expanding_capacity(monkeypatch, max_selected):
    lookups = []
    progress = []

    async def pick_best(ticker, *args):
        lookups.append(ticker.symbol)
        if ticker.symbol == "MISSING":
            return None, [], 0, "no suitable warrant"
        if ticker.symbol == "ERROR":
            raise RuntimeError("lookup unavailable")
        best = SelectedWarrant(
            underlying=ticker, warrant_isin=f"W_{ticker.symbol}",
            warrant_wkn=ticker.symbol, score=0.8, rationale="test",
        )
        return best, [best], 1, None

    async def on_progress(phase, done, total, active):
        assert phase == "entry"
        progress.append((done, total))

    agent = WarrantSelectionAgent(
        finhub=None, prices={}, max_selected=max_selected,
        challenger_symbols=["A", "B", "MISSING", "ERROR", "OUTSIDE"],
        on_progress=on_progress,
    )
    monkeypatch.setattr(agent, "_pick_best", pick_best)
    result = await agent.run(SelectionResult(
        selected=[Ticker(symbol=symbol) for symbol in ["A", "B", "MISSING", "ERROR", "C"]],
        scores={}, rationale={},
    ))
    expected_lookups = ["A", "B", "MISSING", "ERROR"] + (["C"] if max_selected > 0 else [])
    assert lookups == expected_lookups
    assert [w.underlying.symbol for w in result.selected] == (["A"] if max_selected > 0 else [])
    assert [w.underlying.symbol for w in result.challenger_selected] == ["A", "B"]
    assert result.challenger_symbols == ["A", "B", "MISSING", "ERROR"]
    assert result.top3["B"] == [result.challenger_selected[1]]
    assert result.skipped == ["MISSING", "ERROR"]
    assert result.skipped_reasons["ERROR"] == "lookup failed"
    assert result.roll_selected == []
    assert result.sell_existing_isins == []
    assert {total for _, total in progress} == {len(expected_lookups)}
    stored = WarrantSelectionResult.model_validate_json(result.model_dump_json())
    assert stored.challenger_selected == result.challenger_selected


@pytest.mark.asyncio
@pytest.mark.parametrize("max_selected", [0, -1])
async def test_warrant_selection_skips_entries_when_no_slots_but_searches_rolls(max_selected):
    class FakeFinHub:
        def __init__(self):
            self.warrant_lookups = []
            self.detail_lookups = []

        async def get_warrants(self, **kwargs):
            self.warrant_lookups.append(kwargs["underlying"])
            return [{"isin": f"W_{kwargs['underlying']}"}]

        async def get_warrant_detail(self, isin: str):
            self.detail_lookups.append(isin)
            return {
                "isin": isin,
                "wkn": "NEW123",
                "market_data": {"spread_percent": 0.5, "bid": 1.0, "ask": 1.1},
                "analytics": {"leverage": 5.0, "delta": 0.5},
                "reference_data": {
                    "maturity_date": (date.today() + timedelta(days=330)).isoformat(),
                    "is_capped": False,
                },
            }

    finhub = FakeFinHub()
    progress_updates = []

    async def on_progress(phase, done, total, active):
        progress_updates.append((phase, done, total, active))

    agent = WarrantSelectionAgent(
        finhub=finhub,
        prices={"A": 100.0, "B": 100.0, "R": 100.0},
        max_selected=max_selected,
        on_progress=on_progress,
        roll_candidates=[RollCandidate(
            underlying=Ticker(symbol="R", isin="ISIN_R"),
            warrant_isin="OLD_R", warrant_wkn="OLDR",
            spread_pct=2.4, leverage=2.0, delta=0.2, days_to_maturity=70,
        )],
    )

    result = await agent.run(SelectionResult(
        selected=[Ticker(symbol="A", isin="ISIN_A"), Ticker(symbol="B", isin="ISIN_B")],
        scores={}, rationale={},
    ))

    assert finhub.warrant_lookups
    assert set(finhub.warrant_lookups) == {"ISIN_R"}
    assert finhub.detail_lookups == ["W_ISIN_R"]
    assert result.selected == []
    assert result.skipped == []
    assert result.skipped_reasons == {}
    assert result.roll_underlyings == ["R"]
    assert [w.warrant_wkn for w in result.roll_selected] == ["NEW123"]
    assert progress_updates == [("roll", 0, 1, ["R"]), ("roll", 1, 1, [])]


@pytest.mark.asyncio
async def test_roll_progress_counts_completed_and_failed_lookups(monkeypatch):
    import asyncio

    updates = []

    async def on_progress(phase, done, total, active):
        updates.append((phase, done, total, active))

    async def pick_best(ticker, *args):
        await asyncio.sleep(0)
        if ticker.symbol == "R2":
            raise RuntimeError("lookup failed")
        return None

    agent = WarrantSelectionAgent(
        finhub=None, prices={}, on_progress=on_progress,
        roll_candidates=[
            RollCandidate(underlying=Ticker(symbol=s), warrant_isin=s, warrant_wkn=s)
            for s in ("R1", "R2")
        ],
    )
    monkeypatch.setattr(agent, "_pick_best", pick_best)
    await agent.run(SelectionResult(selected=[], scores={}, rationale={}))

    assert updates[0] == ("roll", 0, 2, ["R1"])
    assert ("roll", 0, 2, ["R1", "R2"]) in updates
    assert updates[-1] == ("roll", 2, 2, [])
    assert [done for _, done, _, _ in updates] == [0, 0, 1, 2]


@pytest.mark.parametrize("remaining_symbols", [["C", "A"], ["C"]])
def test_warrant_selection_table_preserves_screening_ranks(remaining_symbols):
    import re

    from app.config import WarrantSelectionSettings
    from app.routes.pipeline import templates

    screening = SelectionResult(
        selected=[Ticker(symbol=symbol) for symbol in ["A", "B", "C"]],
        scores={"A": 0.1, "B": 0.2, "C": 0.3},
        rationale={},
    )
    warrants = WarrantSelectionResult(
        selected=[
            SelectedWarrant(
                underlying=Ticker(symbol=symbol),
                warrant_isin=f"W_{symbol}",
                warrant_wkn=f"W_{symbol}",
                score=0.5,
                rationale="test",
            )
            for symbol in remaining_symbols
        ],
        skipped=["B"],
    )
    html = templates.env.get_template("stages/warrant_selection.html").render(
        execution={
            "execution_id": "exec1",
            "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        },
        qs_id="qs1",
        stage_status="awaiting_review",
        stage_result=warrants.model_dump(mode="json"),
        warrant_selection_cfg={
            **WarrantSelectionSettings().model_dump(),
            "min_months": 9,
            "max_months": 15,
        },
    )
    rows = re.findall(
        r'<td class="text-muted small">(\d+)</td>\s*'
        r'<td><span class="badge bg-secondary">ENTRY</span></td>\s*'
        r'<td>\s*<code>([^<]+)</code>',
        html,
    )
    expected = [(str(index), ticker.symbol) for index, ticker in enumerate(screening.selected, 1)
                if ticker.symbol in remaining_symbols]
    assert rows == expected


def test_warrant_selection_zero_slot_progress_message_counts_unsearched_candidates():
    from app.routes.pipeline import templates

    html = templates.env.get_template("stages/warrant_selection.html").render(
        execution={
            "execution_id": "exec1",
            "stages": {
                "monitoring": {"result": {
                    "free_positions": 0,
                    "entry_candidates": [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}],
                    "positions_to_roll": [{"underlying_symbol": "R"}],
                }},
            },
        },
        qs_id="qs1",
        stage_status="running",
        stage_progress={"done": 2, "total": 3, "active": ["STALE"]},
    )

    assert "No free entry slots: ordinary entries are disabled." in html
    assert "Eligible momentum challengers are still searched for advisory comparison." in html
    assert "Roll replacement searches continue independently." in html
    assert "2 / 3 underlyings" not in html
    assert "STALE" not in html
    assert "Analyzing:" not in html


def test_warrant_challenger_rows_are_advisory_and_deduplicated():
    from app.config import WarrantSelectionSettings
    from app.routes.pipeline import templates

    warrants = [
        SelectedWarrant(
            underlying=Ticker(symbol=symbol), warrant_isin=f"W_{symbol}",
            warrant_wkn=symbol, score=0.8, rationale="test",
        )
        for symbol in ("A", "B")
    ]
    html = templates.env.get_template("stages/warrant_selection.html").render(
        execution={"execution_id": "test", "stages": {"screening": {"result": {
            "selected": [{"symbol": "A"}, {"symbol": "B"}],
        }}}},
        qs_id="qs", stage_status="awaiting_review",
        stage_result=WarrantSelectionResult(
            selected=[warrants[0]], challenger_selected=warrants, skipped=[],
            challenger_symbols=["A", "B"], challenger_ranking_metric="tq20",
            challenger_benchmark_symbol="KEEP", challenger_benchmark_rank=8,
            challenger_unranked_incumbents=["UNKNOWN"],
            top3={"A": [warrants[0]], "B": [warrants[1]]},
        ).model_dump(mode="json"),
        warrant_selection_cfg={
            **WarrantSelectionSettings().model_dump(), "min_months": 9, "max_months": 15,
        },
    )
    assert html.count('>ENTRY</span>') == 1
    assert html.count('>CHALLENGER</span>') == 1
    assert html.count('data-top3=') == 2
    assert "universe-wide TQ-20 ranks" in html
    assert "rank 8" in html
    assert "No momentum replacements or minimum metric deltas are applied." in html
    assert "<code>UNKNOWN</code>" in html
    assert "unranked holdings are not treated as weakest" in html


def test_warrant_selection_displays_blocked_market_gate():
    from app.config import WarrantSelectionSettings
    from app.routes.pipeline import templates

    html = templates.env.get_template("stages/warrant_selection.html").render(
        execution={"execution_id": "test", "stages": {}},
        qs_id="qs", stage_status="awaiting_review",
        stage_result=WarrantSelectionResult(
            selected=[], skipped=[], entry_blocked_by_market_gate=True,
        ).model_dump(mode="json"),
        warrant_selection_cfg={
            **WarrantSelectionSettings().model_dump(), "min_months": 9, "max_months": 15,
        },
    )
    assert "The market gate blocks new entries. Challenger warrants are advisory only" in html


def test_warrant_challenger_progress_is_visible_without_entry_slots():
    from app.routes.pipeline import templates

    html = templates.env.get_template("stages/warrant_selection.html").render(
        execution={"execution_id": "test", "stages": {"monitoring": {"result": {
            "free_positions": 0, "entry_candidates": [{"symbol": "A"}], "positions_to_roll": [],
        }}}},
        qs_id="qs", stage_status="running",
        stage_progress={"done": 0, "total": 1, "active": ["A"], "challenger_count": 1},
    )
    assert "0 / 1 underlyings" in html
    assert "Analyzing:" in html


def test_roll_progress_is_visible_without_entry_slots():
    from app.routes.pipeline import templates

    html = templates.env.get_template("stages/warrant_selection.html").render(
        execution={"execution_id": "test", "stages": {"monitoring": {"result": {
            "free_positions": 0, "positions_to_roll": [{"underlying_symbol": "R"}],
        }}}},
        qs_id="qs", stage_status="running",
        stage_progress={"phase": "roll", "done": 1, "total": 3, "active": ["R"]},
    )
    assert "Searching roll replacements via FinHub" in html
    assert "1 / 3 underlyings" in html
    assert 'aria-valuenow="1"' in html
    assert "33%" in html
    assert "<code>R</code>" in html


def _roll_finhub(wkn: str = "NEW123", maturity_days: int = 330):
    mat = (date.today() + timedelta(days=maturity_days)).isoformat()

    class FakeFinHub:
        async def get_warrants(self, **kwargs):
            return [{"isin": f"W_{kwargs.get('underlying')}"}]

        async def get_warrant_detail(self, isin: str):
            return {
                "isin": isin,
                "wkn": wkn,
                "market_data": {"spread_percent": 0.5, "bid": 1.0, "ask": 1.1},
                "analytics": {"leverage": 5.0, "delta": 0.5},
                "reference_data": {"maturity_date": mat, "is_capped": False},
            }

    return FakeFinHub()


@pytest.mark.asyncio
async def test_warrant_selection_rolls_when_replacement_better():
    # Incumbent is degraded (wide spread, low leverage/delta, short maturity);
    # replacement is near-optimal → score improvement clears the 0.10 margin.
    agent = WarrantSelectionAgent(
        finhub=_roll_finhub(),
        prices={"A": 100.0},
        roll_candidates=[RollCandidate(
            underlying=Ticker(symbol="A", isin="ISIN_A"),
            warrant_isin="OLD_ISIN", warrant_wkn="OLD123",
            spread_pct=2.4, leverage=2.0, delta=0.2, days_to_maturity=70,
        )],
    )
    result = await agent.run(SelectionResult(selected=[], scores={}, rationale={}))

    assert result.roll_underlyings == ["A"]
    assert result.roll_sell_underlyings == []
    assert [w.warrant_wkn for w in result.roll_selected] == ["NEW123"]
    assert "A" in result.roll_incumbents
    assert result.roll_incumbents["A"].warrant_isin == "OLD_ISIN"


@pytest.mark.asyncio
async def test_warrant_selection_recommends_sell_when_replacement_not_better():
    # Incumbent metrics match the replacement exactly → no score margin → the
    # incumbent is a known-degraded roll candidate, so recommend SELL, not keep.
    agent = WarrantSelectionAgent(
        finhub=_roll_finhub(),
        prices={"A": 100.0},
        roll_candidates=[RollCandidate(
            underlying=Ticker(symbol="A", isin="ISIN_A"),
            warrant_isin="OLD_ISIN", warrant_wkn="OLD123",
            spread_pct=0.5, leverage=5.0, delta=0.5, days_to_maturity=330,
        )],
    )
    result = await agent.run(SelectionResult(selected=[], scores={}, rationale={}))

    assert result.roll_sell_underlyings == ["A"]
    assert result.roll_underlyings == []
    assert result.roll_selected == []
    assert result.sell_existing_isins == ["OLD_ISIN"]


@pytest.mark.asyncio
async def test_warrant_selection_rolls_ignore_entry_slot_cap():
    # Entry cap of 1 is filled by underlying A; a degraded roll candidate R must
    # still be rolled — rolls are 1:1 replacements and do not consume entry slots.
    agent = WarrantSelectionAgent(
        finhub=_roll_finhub(),
        prices={"A": 100.0, "R": 100.0},
        max_selected=1,
        roll_candidates=[RollCandidate(
            underlying=Ticker(symbol="R", isin="ISIN_R"),
            warrant_isin="OLD_R", warrant_wkn="OLDR",
            spread_pct=3.0, leverage=1.5, delta=0.1, days_to_maturity=65,
        )],
    )
    result = await agent.run(SelectionResult(
        selected=[Ticker(symbol="A", isin="ISIN_A")],
        scores={"A": 1.0}, rationale={},
    ))

    assert len(result.selected) == 1        # entry cap respected
    assert result.roll_underlyings == ["R"] # roll not blocked by the entry cap


@pytest.mark.asyncio
async def test_warrant_selection_adapts_strike_interval_when_candidate_count_low():
    class FakeFinHub:
        def __init__(self) -> None:
            self.selection_strike_windows: list[tuple[float | None, float | None]] = []
            self.call_count = 0

        async def get_warrants(self, **kwargs):
            preselection = kwargs.get("preselection")
            if preselection != "CALL":
                return []

            self.call_count += 1
            self.selection_strike_windows.append((kwargs.get("strike_min"), kwargs.get("strike_max")))
            if self.call_count == 1:
                return [{"isin": "W1"}, {"isin": "W2"}, {"isin": "W3"}]
            if self.call_count == 2:
                return [{"isin": "W1"}, {"isin": "W2"}, {"isin": "W3"}, {"isin": "W4"}]
            return [
                {"isin": "W1"},
                {"isin": "W2"},
                {"isin": "W3"},
                {"isin": "W4"},
                {"isin": "W5"},
                {"isin": "W6"},
            ]

        async def get_warrant_detail(self, isin: str):
            return {
                "isin": isin,
                "wkn": isin,
                "market_data": {"spread_percent": 0.5, "bid": 1.0, "ask": 1.1},
                "analytics": {"leverage": 5.0, "delta": 0.5},
                "reference_data": {"maturity_date": (date.today() + timedelta(days=330)).isoformat()},
            }

    finhub = FakeFinHub()
    agent = WarrantSelectionAgent(
        finhub=finhub,
        prices={"A": 100.0},
        strike_min_factor=0.95,
        strike_max_factor=1.00,
    )

    result = await agent.run(
        SelectionResult(
            selected=[Ticker(symbol="A", isin="ISIN1")],
            scores={"A": 1.0},
            rationale={},
        )
    )

    assert len(result.selected) == 1
    assert result.analyzed_count["A"] == 6
    assert len(finhub.selection_strike_windows) == 3
    first_min, first_max = finhub.selection_strike_windows[0]
    third_min, third_max = finhub.selection_strike_windows[2]
    assert first_min is not None and first_max is not None
    assert third_min is not None and third_max is not None
    assert (third_max - third_min) > (first_max - first_min)


@pytest.mark.asyncio
async def test_warrant_selection_skips_when_no_candidate_exceeds_min_score():
    class FakeFinHub:
        async def get_warrants(self, **_kwargs):
            return [{"isin": "W1"}, {"isin": "W2"}]

        async def get_warrant_detail(self, isin: str):
            return {
                "isin": isin,
                "wkn": isin,
                "market_data": {"spread_percent": 8.0, "bid": 1.0, "ask": 1.1},
                "analytics": {"leverage": None, "delta": None},
                "reference_data": {"maturity_date": None},
            }

    agent = WarrantSelectionAgent(
        finhub=FakeFinHub(),
        prices={"A": 100.0},
        min_score=0.99,
        spread_max_pct=None,
    )

    result = await agent.run(
        SelectionResult(
            selected=[Ticker(symbol="A", isin="ISIN1")],
            scores={"A": 1.0},
            rationale={},
        )
    )

    assert result.selected == []
    assert result.skipped == ["A"]
    assert "A" in result.skipped_reasons
    assert "min score 0.99" in result.skipped_reasons["A"]


@pytest.mark.asyncio
async def test_warrant_selection_applies_spread_max_cap():
    captured_spread_limits: list[float | None] = []

    class FakeFinHub:
        async def get_warrants(self, **kwargs):
            captured_spread_limits.append(kwargs.get("spread_ask_pct_max"))
            return [{"isin": "W1"}]

        async def get_warrant_detail(self, isin: str):
            return {
                "isin": isin,
                "wkn": isin,
                "market_data": {"spread_percent": 3.2, "bid": 1.0, "ask": 1.1},
                "analytics": {"leverage": 5.0, "delta": 0.5},
                "reference_data": {"maturity_date": (date.today() + timedelta(days=330)).isoformat(), "is_capped": False},
            }

    agent = WarrantSelectionAgent(
        finhub=FakeFinHub(),
        prices={"A": 100.0},
        spread_max_pct=2.5,
    )

    result = await agent.run(
        SelectionResult(
            selected=[Ticker(symbol="A", isin="ISIN1")],
            scores={"A": 1.0},
            rationale={},
        )
    )

    # Spread cap is enforced locally in detail filtering, not in the get_warrants API query.
    assert all(limit is None for limit in captured_spread_limits)
    assert result.selected == []
    assert result.skipped == ["A"]
    assert result.skipped_reasons["A"] == "all candidates above configured spread cap"


@pytest.mark.asyncio
async def test_warrant_selection_reports_only_capped_when_all_candidates_capped():
    today = date.today()

    class FakeFinHub:
        async def get_warrants(self, **kwargs):
            # Selection path returns candidates, all of which are capped.
            return [{"isin": "W1"}, {"isin": "W2"}]

        async def get_warrant_detail(self, isin: str):
            return {
                "isin": isin,
                "wkn": isin,
                "market_data": {"spread_percent": 0.5, "bid": 1.0, "ask": 1.1},
                "analytics": {"leverage": 5.0, "delta": 0.5},
                "reference_data": {"maturity_date": (today + timedelta(days=330)).isoformat(), "is_capped": True},
            }

    agent = WarrantSelectionAgent(
        finhub=FakeFinHub(),
        prices={"A": 100.0},
        spread_max_pct=2.5,
    )

    result = await agent.run(
        SelectionResult(
            selected=[Ticker(symbol="A", isin="ISIN1")],
            scores={"A": 1.0},
            rationale={},
        )
    )

    assert result.selected == []
    assert result.skipped == ["A"]
    assert result.skipped_reasons["A"] == "only capped call warrants available"


@pytest.mark.asyncio
async def test_warrant_selection_reports_no_candidates_when_none_found():
    class FakeFinHub:
        async def get_warrants(self, **kwargs):
            return []

        async def get_warrant_detail(self, isin: str):
            return None

    agent = WarrantSelectionAgent(
        finhub=FakeFinHub(),
        prices={"A": 100.0},
    )

    result = await agent.run(
        SelectionResult(
            selected=[Ticker(symbol="A", isin="ISIN1")],
            scores={"A": 1.0},
            rationale={},
        )
    )

    assert result.selected == []
    assert result.skipped == ["A"]
    assert result.skipped_reasons["A"] == "no candidates in configured maturity/strike range"


def test_warrant_selection_scoring_tracks_active_maturity_range():
    today = date.today()
    near_mid = {
        "market_data": {"spread_percent": 1.0},
        "analytics": {"leverage": 5.0, "delta": 0.5},
        "reference_data": {"maturity_date": (today + timedelta(days=330)).isoformat()},
    }
    longer = {
        "market_data": {"spread_percent": 1.0},
        "analytics": {"leverage": 5.0, "delta": 0.5},
        "reference_data": {"maturity_date": (today + timedelta(days=450)).isoformat()},
    }

    shorter_window_agent = WarrantSelectionAgent(
        finhub=None,
        prices={},
        min_days_to_expiry=270,
        max_days_to_expiry=450,
    )
    wider_window_agent = WarrantSelectionAgent(
        finhub=None,
        prices={},
        min_days_to_expiry=270,
        max_days_to_expiry=540,
    )

    assert shorter_window_agent._score(near_mid, today) > shorter_window_agent._score(longer, today)
    assert wider_window_agent._score(longer, today) > wider_window_agent._score(near_mid, today)
