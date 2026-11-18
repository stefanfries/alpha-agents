from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.agents.research import ResearchAgent, ResearchInput
from app.config import ResearchSettings, settings
from app.models.market import OHLCV, Ticker
from app.models.signals import MarketRegime
from app.routes.pipeline import STAGES, STAGE_LABELS, templates


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
    }


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
