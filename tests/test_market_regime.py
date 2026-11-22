from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.agents.research import ResearchAgent, ResearchInput
from app.agents.screening import SecuritySelectionAgent
from app.config import ResearchSettings, ScreeningSettings, settings
from app.models.market import OHLCV, Ticker
from app.models.signals import MarketGate, MarketRegime, MonitoringResult, ResearchResult
from app.routes.pipeline import STAGES, STAGE_LABELS, _stage_ctx, templates


def _benchmark_bars(direction: int, count: int = 80) -> list[OHLCV]:
    ticker = Ticker(symbol="^NDX")
    return [
        OHLCV(
            ticker=ticker,
            date=date(2025, 1, 1) + timedelta(days=index),
            open=Decimal(200 + direction * index),
            high=Decimal(201 + direction * index),
            low=Decimal(199 + direction * index),
            close=Decimal(200 + direction * index),
            volume=1,
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "expected"),
    [(1, "open"), (-1, "blocked")],
)
async def test_market_gate_uses_benchmark_supertrend(direction, expected):
    bars = _benchmark_bars(direction)
    result = await SecuritySelectionAgent().run(ResearchResult(
        tickers=[], bars={}, fundamentals={}, benchmark_symbol="^NDX", benchmark_bars=bars,
    ))
    assert result.market_gate.status == expected
    assert result.market_gate.supertrend_bullish is (direction == 1)
    assert result.market_gate.benchmark_date == bars[-1].date


@pytest.mark.asyncio
async def test_market_gate_blocks_missing_or_insufficient_data_and_can_be_disabled():
    for bars in ([], _benchmark_bars(1, count=3)):
        result = await SecuritySelectionAgent().run(ResearchResult(
            tickers=[], bars={}, fundamentals={}, benchmark_symbol="^NDX", benchmark_bars=bars,
        ))
        assert result.market_gate.status == "blocked"
        assert result.market_gate.reasons == ["Benchmark SuperTrend unavailable"]
    result = await SecuritySelectionAgent(ScreeningSettings(
        market_gate_supertrend=False,
    )).run(ResearchResult(tickers=[], bars={}, fundamentals={}))
    assert result.market_gate.status == "disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("regime_status", "red_rule", "expected"),
    [("red", False, "open"), ("red", True, "blocked"), ("yellow", True, "open"), (None, True, "blocked")],
)
async def test_optional_red_regime_gate(regime_status, red_rule, expected):
    regime = (
        MarketRegime(symbol="^NDX", tq60=0, tq20=0, status=regime_status)
        if regime_status else None
    )
    result = await SecuritySelectionAgent(ScreeningSettings(
        market_gate_red_regime=red_rule,
    )).run(ResearchResult(
        tickers=[], bars={}, fundamentals={}, benchmark_symbol="^NDX",
        benchmark_bars=_benchmark_bars(1), market_regime=regime,
    ))
    assert result.market_gate.status == expected
    if regime_status is None:
        assert result.market_gate.reasons == ["Market regime unavailable"]


@pytest.mark.asyncio
async def test_benchmark_history_survives_research_to_screening(monkeypatch):
    from app import orchestrator

    bars = _benchmark_bars(-1)

    class FakeYFinance:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def fetch_ohlcv_batch(self, tickers, lookback_days):
            return {}

    class FakeResearch:
        def __init__(self, **kwargs):
            pass

        async def run(self, input):
            return ResearchResult(
                tickers=[], bars={}, fundamentals={}, benchmark_symbol="^NDX", benchmark_bars=bars,
            )

    monkeypatch.setattr(orchestrator, "YFinanceTool", FakeYFinance)
    monkeypatch.setattr(orchestrator, "ResearchAgent", FakeResearch)
    pipeline = orchestrator.Pipeline()
    run = {
        "execution_id": "test",
        "stages": {"universe": {"result": {
            "tickers": [], "source": {}, "missing_isin": [], "unresolved_indices": [],
        }}},
    }
    research = await pipeline._run_research(run)
    assert research.benchmark_bars == bars
    run["stages"]["research"] = {"result": research.model_dump(mode="json")}
    screening = await pipeline._run_screening(run)
    assert screening.market_gate.status == "blocked"
    assert screening.market_gate.reasons == ["Benchmark SuperTrend bearish"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tq60", "tq20", "expected_status"),
    [
        (0.03, 0.01, "green"),
        (0.0299, 0.01, "yellow"),
        (-0.03, -0.01, "red"),
        (-0.03, -0.0099, "yellow"),
    ],
)
async def test_research_classifies_market_regime_at_thresholds(tq60, tq20, expected_status):
    benchmark = Ticker(symbol="^NDX")
    benchmark_bars = [
        OHLCV(
            ticker=benchmark,
            date=date(2025, 1, 1) + timedelta(days=index),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1,
        )
        for index in range(60)
    ]

    class StubYFinanceTool:
        async def fetch_ohlcv_batch(self, tickers, lookback_days):
            if tickers and tickers[0].symbol == benchmark.symbol:
                return {benchmark.symbol: benchmark_bars}
            return {}

    agent = ResearchAgent(StubYFinanceTool(), research_settings=ResearchSettings())
    agent._trend_quality = lambda _bars, lookback: {60: tq60, 20: tq20}[lookback]

    result = await agent.run(ResearchInput(
        tickers=[],
        universe_source={"member": "NASDAQ100"},
    ))

    assert result.market_regime is not None
    assert result.market_regime.status == expected_status
    assert result.market_regime.tq60 == round(tq60, 4)
    assert result.market_regime.tq20 == round(tq20, 4)


def _stage_context(stage: str, stage_result: dict) -> dict:
    execution = {
        "execution_id": "exec1",
        "quant_system_id": "qs1",
        "config_overrides": {},
    }
    return {
        "execution": execution,
        "execution_id": execution["execution_id"],
        "qs_id": execution["quant_system_id"],
        "current_stage": stage,
        "stages": STAGES,
        "stage_labels": STAGE_LABELS,
        "stage_status": "awaiting_review",
        "stage_result": stage_result,
        "stage_progress": None,
        "screening_cfg": settings.screening.model_dump(),
        "currency_locale": "de_DE",
        "market_gate_benchmark_name": "NASDAQ-100",
    }


@pytest.mark.parametrize("stage", ["screening", "monitoring"])
def test_market_gate_benchmark_name_for_existing_execution(stage):
    gate = {"status": "blocked", "benchmark_symbol": "^NDX"}
    execution = {
        "execution_id": "exec1",
        "quant_system_id": "qs1",
        "stages": {
            "screening": {"result": {
                "market_gate": gate,
                "market_regime": {"symbol": "^NDX", "display_name": "Nasdaq 100"},
            }},
            "monitoring": {"result": {"market_gate": gate}},
        },
    }
    assert _stage_ctx(execution, stage)["market_gate_benchmark_name"] == "NASDAQ-100"


def test_market_gate_benchmark_name_falls_back_to_unknown_symbol():
    execution = {
        "execution_id": "exec1",
        "quant_system_id": "qs1",
        "stages": {"screening": {"result": {
            "market_gate": {"status": "blocked", "benchmark_symbol": "^UNKNOWN"},
        }}},
    }
    assert _stage_ctx(execution, "screening")["market_gate_benchmark_name"] == "^UNKNOWN"


def test_research_stage_renders_market_regime_status_and_values():
    regime = MarketRegime(
        symbol="^NDX",
        display_name="Nasdaq 100",
        tq60=-0.0356,
        tq20=-0.0123,
        status="red",
    )

    html = templates.env.get_template("stages/research.html").render(
        **_stage_context("research", {
            "tickers": [],
            "fundamentals": {},
            "market_regime": regime,
        })
    )

    assert "alert-danger" in html
    assert "Nasdaq 100" in html
    assert "TQ-60&nbsp;=&nbsp;<code>-0.036</code>" in html
    assert "Downtrend" in html


def test_screening_stage_renders_breadth_and_downgraded_regime():
    regime = MarketRegime(
        symbol="^NDX",
        display_name="Nasdaq 100",
        tq60=0.05,
        tq20=0.02,
        breadth_score=0.375,
        breadth_components={
            "pct_supertrend_long": 0.25,
            "pct_ema20_above_ema50": 0.5,
            "pct_adx_above_20": 0.25,
        },
        status="yellow",
    )

    html = templates.env.get_template("stages/screening.html").render(
        **_stage_context("screening", {
            "market_regime": regime,
            "market_gate": {
                "status": "blocked",
                "benchmark_symbol": "^NDX",
                "benchmark_date": "2025-01-03",
                "supertrend_bullish": False,
                "reasons": ["Benchmark SuperTrend bearish"],
            },
            "selected": [],
            "scores": {},
            "history_labels": [],
            "rank_changes": {},
            "policy_results": {},
            "all_tickers": [],
            "tq_short": {},
            "tsi": {},
            "weekly_confirmed": {},
            "trend_signals": {},
        })
    )

    assert "alert-warning" in html
    assert "Sideways" in html
    assert "Bullish breadth&nbsp;=&nbsp;<code>38%</code>" in html
    assert "SuperTrend Long: <strong>25%</strong>" in html
    assert "EMA20 &gt; EMA50: <strong>50%</strong>" in html
    assert "ADX &gt; 20 (direction-neutral): <strong>25%</strong>" in html
    assert "Market gate: blocked" in html
    assert "Benchmark SuperTrend bearish" in html
    assert "latest bar 2025-01-03" in html
    gate_html = html.split("<strong>Market gate: blocked</strong>", 1)[1].split("</div>", 1)[0]
    assert "NASDAQ-100" in gate_html
    assert "^NDX" not in gate_html
    assert gate_html.count("SuperTrend bearish") == 1
    assert 'name="market_gate_supertrend" id="market_gate_supertrend"\n                       checked' in html
    assert 'name="market_gate_red_regime" id="market_gate_red_regime"\n                       checked' not in html


def test_monitoring_gate_displays_benchmark_name():
    result = MonitoringResult(
        positions_to_keep=[], positions_to_sell=[], entry_candidates=[],
        free_positions=0, excluded_symbols=[],
        market_gate=MarketGate(
            status="blocked", benchmark_symbol="^NDX",
            reasons=["Benchmark SuperTrend bearish"],
        ),
    )
    html = templates.env.get_template("stages/monitoring.html").render(
        **_stage_context("monitoring", result.model_dump(mode="json"))
    )
    gate_html = html.split("<strong>Market gate: blocked</strong>", 1)[1].split("</div>", 1)[0]
    assert "NASDAQ-100" in gate_html
    assert "^NDX" not in gate_html
    assert "Benchmark SuperTrend bearish" in gate_html
