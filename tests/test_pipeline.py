from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pytest

from app.agents.execution import TradeExecutionAgent
from app.agents.monitoring import MonitoringAgent, MonitoringInput, WarrantSnapshot
from app.agents.portfolio import PortfolioConstructionAgent
from app.agents.risk import RiskAgent
from app.agents.screening import SecuritySelectionAgent
from app.agents.warrant_selection import WarrantSelectionAgent
from app.config import MonitoringSettings, RiskSettings
from app.models.market import OHLCV, Position, Ticker
from app.models.signals import (
    MarketRegime,
    PlannedPosition,
    PortfolioAccountSnapshot,
    PortfolioHoldingValue,
    PortfolioProposal,
    ResearchResult,
    RollCandidate,
    RollReplacement,
    SelectedWarrant,
    SelectionResult,
    WarrantSelectionResult,
)


@pytest.mark.asyncio
async def test_screening_filters_low_market_cap():
    from app.config import ScreeningSettings
    agent = SecuritySelectionAgent(ScreeningSettings(top_n=10, min_market_cap_eur=1_000_000_000))
    ticker = Ticker(symbol="SMALL")
    result = await agent.run(
        ResearchResult(
            tickers=[ticker],
            bars={"SMALL": []},
            fundamentals={"SMALL": {"marketCap": 100_000}},
        )
    )
    assert ticker not in result.selected
    assert "SMALL" in result.rationale


@pytest.mark.asyncio
async def test_screening_selected_preserves_score_order():
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings(top_n=2))
    tickers = [Ticker(symbol="LOW"), Ticker(symbol="HIGH")]
    bars = {
        ticker.symbol: _make_synthetic_bars(ticker, [100.0 + i for i in range(70)])
        for ticker in tickers
    }

    agent._trend_quality = lambda ticker_bars, _lookback: (
        0.5 if ticker_bars[0].ticker.symbol == "LOW" else 1.0
    )
    agent._tsi = lambda _bars: 0.0
    agent._evaluate_policies = lambda _bars: {
        "supertrend": True,
        "supertrend_bearish": False,
        "ema20_rising": True,
        "ema20_falling": False,
        "adx_above": True,
        "adx_below": False,
        "adx_rising": True,
        "adx_falling": False,
        "price_above_ema50": True,
        "price_below_ema50": False,
        "tq60_above": True,
        "tq20_above": True,
        "tsi_above": True,
        "tsi_below": False,
    }
    agent._trend_signal = lambda *args, **kwargs: None
    agent._last_break_age = lambda *args, **kwargs: None

    result = await agent.run(
        ResearchResult(
            tickers=tickers,
            bars=bars,
            fundamentals={symbol: {"marketCap": 1_000_000_000} for symbol in ("LOW", "HIGH")},
        )
    )

    assert [ticker.symbol for ticker in result.selected] == ["HIGH", "LOW"]


@pytest.mark.asyncio
async def test_screening_exposes_atr_normalized_extension_without_changing_selection():
    from app.config import ScreeningSettings

    ticker = Ticker(symbol="EXT")
    bars = _make_synthetic_bars(ticker, [100.0 + i for i in range(70)])
    agent = SecuritySelectionAgent(ScreeningSettings(top_n=1))
    agent._evaluate_policies = lambda _bars: {
        "supertrend": True,
        "supertrend_bearish": False,
        "ema20_rising": True,
        "ema20_falling": False,
        "adx_above": True,
        "adx_below": False,
        "adx_rising": True,
        "adx_falling": False,
        "price_above_ema50": True,
        "price_below_ema50": False,
        "tq60_above": True,
        "tq20_above": True,
        "tsi_above": True,
        "tsi_below": False,
    }
    agent._trend_signal = lambda *args, **kwargs: None
    agent._last_break_age = lambda *args, **kwargs: None

    result = await agent.run(ResearchResult(
        tickers=[ticker],
        bars={ticker.symbol: bars},
        fundamentals={ticker.symbol: {"marketCap": 1_000_000_000}},
    ))

    assert result.selected == [ticker]
    assert result.extension_scores[ticker.symbol] > 0
    assert result.weekly_confirmed[ticker.symbol] is False


def test_market_regime_advisory_action_differentiates_by_status():
    green = MarketRegime(symbol="^NDX", tq60=0.05, tq20=0.02, status="green")
    yellow = MarketRegime(symbol="^NDX", tq60=0.00, tq20=0.00, status="yellow")
    red = MarketRegime(symbol="^NDX", tq60=-0.05, tq20=-0.02, status="red")

    assert green.advisory_action == "normal review"
    assert yellow.advisory_action == "stricter timing review; no automatic early entries"
    assert red.advisory_action == "no new entries recommended; review existing positions"


def test_screening_downgrades_green_regime_when_breadth_is_narrow():
    agent = SecuritySelectionAgent()
    regime = MarketRegime(symbol="^NDX", display_name="Nasdaq 100", tq60=0.05, tq20=0.02, status="green")
    policy_results = {
        "A": {"supertrend": False, "ema20_rising": False, "adx_above": False},
        "B": {"supertrend": False, "ema20_rising": False, "adx_above": False},
        "C": {"supertrend": True, "ema20_rising": True, "adx_above": False},
        "D": {"supertrend": False, "ema20_rising": False, "adx_above": False},
    }

    result = agent._enrich_regime(regime, policy_results)

    assert result is regime
    assert result.status == "yellow"
    assert result.breadth_score == pytest.approx(0.188)
    assert result.breadth_components["pct_supertrend_long"] == pytest.approx(0.25)
    assert result.advisory_action == "stricter timing review; no automatic early entries"


@pytest.mark.asyncio
async def test_portfolio_equal_weights():
    tickers = [Ticker(symbol=s) for s in ["A", "B", "C", "D"]]
    agent = PortfolioConstructionAgent(capital_eur=10_000, sizing_method="equal")
    result = await agent.run(
        SelectionResult(
            selected=tickers,
            scores={"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0},
            rationale={},
        )
    )
    assert len(result.positions) == 4
    for w in result.target_weights.values():
        assert abs(w - 0.25) < 1e-9


@pytest.mark.asyncio
async def test_portfolio_account_equal_sizing_uses_available_buy_slots():
    tickers = [Ticker(symbol="A", isin="A-ISIN"), Ticker(symbol="B", isin="B-ISIN")]
    agent = PortfolioConstructionAgent(
        capital_eur=100_000,
        max_positions=6,
        slippage_bps=0.0,
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("45000"),
            nav_eur=Decimal("100000"),
        ),
    )

    result = await agent.run(SelectionResult(selected=tickers, scores={}, rationale={}))

    assert result.standard_buy_amount_eur == Decimal("7492.12")
    assert [position.notional_eur for position in result.new_positions] == [
        Decimal("7492.12"),
        Decimal("7492.12"),
    ]
    assert result.cost_reserve_eur == Decimal("47.26")
    assert result.target_weights == {"A": 0.0749212, "B": 0.0749212}


def test_risk_sector_cap_defaults_to_eighty_percent():
    assert RiskSettings().max_sector_weight == 0.8


@pytest.mark.asyncio
async def test_portfolio_entry_retains_ask_price_for_quantity_estimate():
    ticker = Ticker(symbol="WKN1", isin="ISIN1", name="Example Corp")
    agent = PortfolioConstructionAgent(
        capital_eur=10_000,
        max_positions=1,
        slippage_bps=0.0,
        planned_metadata_by_isin={"ISIN1": {"ask": 2.45}},
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("10000"),
            nav_eur=Decimal("10000"),
        ),
    )

    result = await agent.run(SelectionResult(selected=[ticker], scores={}, rationale={}))

    assert result.new_positions[0].buy_price_eur == Decimal("2.45")


@pytest.mark.asyncio
async def test_portfolio_account_sizing_fills_available_slots_after_costs():
    held_positions = [
        Position(
            ticker=Ticker(symbol=f"HELD{i}", isin=f"HELD-{i}"),
            quantity=Decimal("1"),
            avg_cost=Decimal("100"),
        )
        for i in range(8)
    ]
    holdings = [
        PortfolioHoldingValue(position=position, market_value_eur=Decimal("1000"))
        for position in held_positions
    ]
    new_tickers = [Ticker(symbol=f"NEW{i}", isin=f"NEW-{i}") for i in range(7)]
    agent = PortfolioConstructionAgent(
        capital_eur=100_000,
        current_holdings=held_positions,
        max_positions=15,
        slippage_bps=25.0,
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("458136.40"),
            nav_eur=Decimal("466136.40"),
            holdings=holdings,
        ),
    )

    result = await agent.run(SelectionResult(
        selected=[position.ticker for position in held_positions] + new_tickers,
        scores={},
        rationale={},
    ))

    assert result.standard_buy_amount_eur == Decimal("65225.09")
    assert result.cost_reserve_eur == Decimal("1560.72")
    assert len(result.new_positions) == 7
    assert result.standard_buy_amount_eur * 7 + result.cost_reserve_eur == Decimal("458136.35")


@pytest.mark.asyncio
async def test_portfolio_blocks_buy_sizing_until_cost_assumptions_are_configured():
    agent = PortfolioConstructionAgent(
        capital_eur=100_000,
        max_positions=6,
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("45000"),
            nav_eur=Decimal("100000"),
        ),
    )

    result = await agent.run(SelectionResult(
        selected=[Ticker(symbol="A", isin="A-ISIN")],
        scores={},
        rationale={},
    ))

    assert result.positions == []
    assert result.standard_buy_amount_eur is None
    assert "Configure slippage basis points" in result.sizing_blocked_reason


@pytest.mark.asyncio
async def test_portfolio_buy_uses_no_fee_issuer_action_flag():
    ticker = Ticker(symbol="FREE", isin="FREE-ISIN")
    agent = PortfolioConstructionAgent(
        capital_eur=20_000,
        max_positions=2,
        slippage_bps=0.0,
        planned_metadata_by_isin={
            "FREE-ISIN": {
                "issuer_action": True,
                "issuer_no_fee_action": True,
            },
        },
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("20000"),
            nav_eur=Decimal("20000"),
        ),
    )

    result = await agent.run(SelectionResult(selected=[ticker], scores={}, rationale={}))

    assert result.standard_buy_amount_eur == Decimal("10000.00")
    assert result.cost_reserve_eur == Decimal("0.00")
    assert result.new_positions[0].issuer_no_fee_action is True


@pytest.mark.asyncio
async def test_portfolio_account_sizing_includes_planned_sell_proceeds():
    incumbent = Position(
        ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
        quantity=Decimal("100"),
        avg_cost=Decimal("550"),
    )
    tickers = [Ticker(symbol="A", isin="A-ISIN"), Ticker(symbol="B", isin="B-ISIN")]
    agent = PortfolioConstructionAgent(
        capital_eur=100_000,
        current_holdings=[incumbent],
        max_positions=6,
        slippage_bps=0.0,
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("45000"),
            nav_eur=Decimal("100000"),
            holdings=[PortfolioHoldingValue(
                position=incumbent,
                market_value_eur=Decimal("55000"),
            )],
        ),
    )

    result = await agent.run(SelectionResult(selected=tickers, scores={}, rationale={}))

    assert result.close_positions == [incumbent]
    assert result.expected_net_sell_proceeds_eur == Decimal("54940.10")
    assert result.standard_buy_amount_eur == Decimal("16641.18")


@pytest.mark.asyncio
async def test_portfolio_roll_replacement_uses_standard_buy_size_not_full_sale_proceeds():
    incumbent = Position(
        ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
        quantity=Decimal("100"),
        avg_cost=Decimal("550"),
    )
    replacement = SelectedWarrant(
        underlying=Ticker(symbol="A", isin="A-ISIN", name="Alpha"),
        warrant_isin="NEW-ISIN",
        warrant_wkn="NEW",
        score=0.9,
        rationale="replacement",
    )
    agent = PortfolioConstructionAgent(
        capital_eur=155_000,
        current_holdings=[incumbent],
        max_positions=6,
        roll_replacements=[replacement],
        roll_incumbent_isins={"A": "OLD-ISIN"},
        slippage_bps=0.0,
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("45000"),
            nav_eur=Decimal("155000"),
            holdings=[PortfolioHoldingValue(
                position=incumbent,
                underlying_isin="A-ISIN",
                market_value_eur=Decimal("110000"),
            )],
        ),
    )

    result = await agent.run(SelectionResult(selected=[], scores={}, rationale={}))

    assert result.expected_net_sell_proceeds_eur == Decimal("109940.10")
    assert result.standard_buy_amount_eur == Decimal("25813.36")
    assert result.roll_trades[0].replacement.notional_eur == result.standard_buy_amount_eur
    assert result.roll_trades[0].replacement.notional_eur < incumbent.quantity * Decimal("1100")


@pytest.mark.asyncio
async def test_portfolio_constructs_paired_roll_without_closing_incumbent_early():
    incumbent = Position(
        ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
        quantity=Decimal("3"),
        avg_cost=Decimal("100"),
    )
    replacement = SelectedWarrant(
        underlying=Ticker(symbol="A", name="Alpha"),
        warrant_isin="NEW-ISIN",
        warrant_wkn="NEW",
        score=0.9,
        rationale="better roll candidate",
    )
    agent = PortfolioConstructionAgent(
        capital_eur=10_000,
        current_holdings=[incumbent],
        roll_replacements=[replacement],
        roll_incumbent_isins={"A": "OLD-ISIN"},
    )

    result = await agent.run(SelectionResult(selected=[], scores={}, rationale={}))

    assert result.close_positions == []
    assert len(result.roll_trades) == 1
    assert result.roll_trades[0].incumbent == incumbent
    assert result.roll_trades[0].replacement.ticker.isin == "NEW-ISIN"
    assert result.roll_trades[0].replacement.notional_eur == Decimal("300")
    assert result.roll_trades[0].target_weight == 0.03


@pytest.mark.asyncio
async def test_run_portfolio_forwards_selected_roll_as_paired_trade(monkeypatch):
    from app.models.signals import PortfolioAccountSnapshot, PortfolioHoldingValue
    from app.orchestrator import Pipeline

    incumbent = Position(
        ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
        quantity=Decimal("3"),
        avg_cost=Decimal("100"),
    )
    replacement = SelectedWarrant(
        underlying=Ticker(symbol="A", name="Alpha"),
        warrant_isin="NEW-ISIN",
        warrant_wkn="NEW",
        score=0.9,
        rationale="better roll candidate",
    )
    warrant_result = WarrantSelectionResult(
        selected=[],
        skipped=[],
        roll_underlyings=["A"],
        roll_selected=[replacement],
        roll_incumbents={
            "A": RollReplacement(warrant_isin="OLD-ISIN", warrant_wkn="OLD"),
        },
    )
    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict) -> list[Position]:
        return [incumbent]

    async def fake_account_snapshot(*_args):
        return PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("10000"),
            nav_eur=Decimal("10300"),
            holdings=[PortfolioHoldingValue(
                position=incumbent,
                underlying_isin="A-ISIN",
                market_value_eur=Decimal("300"),
            )],
        )

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_portfolio_account_snapshot", fake_account_snapshot)

    result = await pipeline._run_portfolio({
        "capital_eur": 10_000,
        "config_overrides": {
            "portfolio": {
                "slippage_bps": 0.0,
            },
        },
        "stages": {
            "warrant_selection": {"result": warrant_result.model_dump(mode="json")},
        },
    })

    assert result.close_positions == []
    assert len(result.roll_trades) == 1
    assert result.roll_trades[0].incumbent.ticker.isin == "OLD-ISIN"
    assert result.roll_trades[0].replacement.ticker.isin == "NEW-ISIN"


@pytest.mark.asyncio
async def test_run_portfolio_closes_roll_candidate_without_replacement(monkeypatch):
    from app.models.signals import PortfolioAccountSnapshot
    from app.orchestrator import Pipeline

    incumbent = Position(
        ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
        quantity=Decimal("3"),
        avg_cost=Decimal("100"),
    )
    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict) -> list[Position]:
        return [incumbent]

    async def fake_account_snapshot(*_args):
        return PortfolioAccountSnapshot(source="virtual", available_cash_eur=Decimal("10000"))

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_portfolio_account_snapshot", fake_account_snapshot)

    result = await pipeline._run_portfolio({
        "capital_eur": 10_000,
        "stages": {
            "warrant_selection": {
                "result": WarrantSelectionResult(
                    selected=[],
                    skipped=[],
                    sell_existing_isins=["OLD-ISIN"],
                    roll_sell_underlyings=["A"],
                ).model_dump(mode="json"),
            },
            "monitoring": {
                "result": {
                    "positions_to_sell": [],
                    "positions_to_keep": [],
                    "positions_to_roll": [{
                        "underlying_symbol": "A",
                        "warrant_isin": "OLD-ISIN",
                        "warrant_wkn": "OLD",
                    }],
                    "entry_candidates": [],
                    "free_positions": 0,
                    "excluded_symbols": ["A"],
                },
            },
        },
    })

    assert result.roll_trades == []
    assert result.close_positions == [incumbent]


@pytest.mark.asyncio
async def test_risk_rejects_oversized_position():
    ticker = Ticker(symbol="BIG")
    agent = RiskAgent(max_position_multiple=3.0, max_positions=30)
    result = await agent.run(
        PortfolioProposal(
            positions=[PlannedPosition(
                ticker=ticker,
                notional_eur=Decimal("50000"),
                target_weight=0.5,
                sector="Technology",
            )],
            target_weights={"BIG": 0.50},
            account_snapshot=PortfolioAccountSnapshot(
                source="virtual",
                available_cash_eur=Decimal("100000"),
                nav_eur=Decimal("100000"),
            ),
        )
    )
    assert ticker in [p.ticker for p in result.rejected_positions]
    assert "BIG" in result.risk_notes


@pytest.mark.asyncio
async def test_risk_rejects_sector_overflow_but_approves_other_sector():
    held = Position(
        ticker=Ticker(symbol="HELD", isin="HELD-ISIN"),
        quantity=Decimal("100"),
        avg_cost=Decimal("200"),
    )
    tech = PlannedPosition(
        ticker=Ticker(symbol="TECH"),
        notional_eur=Decimal("15000"),
        target_weight=0.15,
        sector="Technology",
    )
    health = PlannedPosition(
        ticker=Ticker(symbol="HEALTH"),
        notional_eur=Decimal("4000"),
        target_weight=0.04,
        sector="Healthcare",
    )
    snapshot_time = datetime.now(timezone.utc)

    result = await RiskAgent(max_positions=6).run(PortfolioProposal(
        positions=[tech, health],
        target_weights={"TECH": 0.15, "HEALTH": 0.04},
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("30000"),
            nav_eur=Decimal("100000"),
            holdings=[PortfolioHoldingValue(
                position=held,
                underlying_isin="HELD-UNDERLYING",
                sector="Technology",
                bid_price_eur=Decimal("700"),
                market_value_eur=Decimal("70000"),
                quote_timestamp_utc=snapshot_time,
            )],
        ),
    ))

    assert result.approved_positions == [health]
    assert result.rejected_positions == [tech]
    assert "sector cap" in result.risk_notes["TECH"]


@pytest.mark.asyncio
async def test_risk_blocks_new_position_when_account_quote_is_stale_but_preserves_sell():
    held = Position(
        ticker=Ticker(symbol="SELL", isin="SELL-ISIN"),
        quantity=Decimal("100"),
        avg_cost=Decimal("2"),
    )
    planned = PlannedPosition(
        ticker=Ticker(symbol="BUY", isin="BUY-ISIN"),
        notional_eur=Decimal("5000"),
        target_weight=0.05,
        sector="Technology",
    )
    snapshot_time = datetime.now(timezone.utc) - timedelta(hours=73)

    result = await RiskAgent(max_positions=6).run(PortfolioProposal(
        positions=[planned],
        target_weights={"BUY": 0.05},
        close_positions=[held],
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("80000"),
            nav_eur=Decimal("100000"),
            holdings=[PortfolioHoldingValue(
                position=held,
                underlying_isin="SELL-UNDERLYING",
                sector="Healthcare",
                bid_price_eur=Decimal("200"),
                market_value_eur=Decimal("20000"),
                quote_timestamp_utc=snapshot_time,
            )],
        ),
    ))

    assert result.approved_positions == []
    assert result.rejected_positions == [planned]
    assert result.close_positions == [held]
    assert "stale" in result.risk_notes["BUY"]


@pytest.mark.asyncio
async def test_risk_reports_existing_over_limit_positions_without_forced_sell():
    held = Position(
        ticker=Ticker(symbol="GROWN", isin="GROWN-ISIN"),
        quantity=Decimal("100"),
        avg_cost=Decimal("400"),
    )

    result = await RiskAgent(max_positions=6).run(PortfolioProposal(
        positions=[],
        target_weights={},
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("10000"),
            nav_eur=Decimal("100000"),
            holdings=[PortfolioHoldingValue(
                position=held,
                underlying_isin="GROWN-UNDERLYING",
                sector="Technology",
                bid_price_eur=Decimal("900"),
                market_value_eur=Decimal("90000"),
                quote_timestamp_utc=datetime.now(timezone.utc),
            )],
        ),
    ))

    assert result.close_positions == []
    assert any("above the position cap" in warning for warning in result.portfolio_warnings)
    assert any("above the sector cap" in warning for warning in result.portfolio_warnings)


@pytest.mark.asyncio
async def test_execution_dry_run_does_not_raise():
    from app.models.signals import RiskAssessment

    ticker = Ticker(symbol="AAPL")
    agent = TradeExecutionAgent(dry_run=True, min_trade_eur=100.0, order_type="limit")
    result = await agent.run(
        RiskAssessment(
            approved_positions=[
                PlannedPosition(
                    ticker=ticker,
                    notional_eur=Decimal("500"),
                    target_weight=0.05,
                )
            ],
            rejected_positions=[],
            risk_notes={},
        )
    )
    assert len(result.orders) == 1
    assert result.orders[0].side == "buy"


def test_screening_policy_group_defaults_match_legacy_behavior():
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings())
    values = {"a": True, "b": False, "c": True}

    # NEW default: all selected must pass
    assert agent._passes_policy_group(
        values,
        {"a": True, "b": True, "c": False},
        min_true=None,
    ) is False

    # BREAK default: all selected must pass (same semantics as NEW).
    assert agent._passes_policy_group(
        values,
        {"a": False, "b": True, "c": True},
        min_true=None,
    ) is False


def test_screening_policy_group_k_of_n_and_clamp():
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings())
    values = {"a": True, "b": True, "c": False}
    enabled = {"a": True, "b": True, "c": True}

    assert agent._passes_policy_group(values, enabled, min_true=2) is True
    assert agent._passes_policy_group(values, enabled, min_true=3) is False

    # Configured min_true above selected policy count is clamped down.
    assert agent._passes_policy_group(values, enabled, min_true=9) is False


def test_screening_policy_group_no_selected_policy_fails():
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings())
    values = {"a": True}
    assert agent._passes_policy_group(
        values,
        {"a": False},
        min_true=1,
    ) is False


def _make_synthetic_bars(ticker: Ticker, closes: list[float]) -> list[OHLCV]:
    start = date(2025, 1, 1)
    bars: list[OHLCV] = []
    prev = closes[0]
    for i, close in enumerate(closes):
        open_ = prev
        high = max(open_, close) + 1.0
        low = min(open_, close) - 1.0
        bars.append(
            OHLCV(
                ticker=ticker,
                date=start + timedelta(days=i),
                open=Decimal(str(round(open_, 4))),
                high=Decimal(str(round(high, 4))),
                low=Decimal(str(round(low, 4))),
                close=Decimal(str(round(close, 4))),
                volume=1_000_000,
            )
        )
        prev = close
    return bars


def test_trend_signal_k2_emits_new_before_break_phase():
    from app.config import ScreeningSettings

    # Keep ADX intentionally hard to satisfy, so NEW relies on the other policies.
    agent = SecuritySelectionAgent(
        ScreeningSettings(min_adx=90, new_min_true=2, break_min_true=2)
    )
    ticker = Ticker(symbol="SYN")

    closes = [100.0 + 0.05 * i for i in range(80)] + [104.0 + 1.8 * i for i in range(8)]
    bars = _make_synthetic_bars(ticker, closes)

    new_enabled = {
        "ema20_rising": True,
        "price_above_ema50": True,
        "adx_above": True,
    }
    break_enabled = {
        "ema20_falling": True,
        "price_below_ema50": True,
        "adx_below": True,
    }

    signal = agent._trend_signal(
        bars,
        new_enabled,
        break_enabled,
        new_min_true=2,
        break_min_true=2,
    )

    assert signal in {"NEW", "HOLD"}


def test_trend_signal_k2_keeps_break_visible_for_five_bars_total():
    from app.config import ScreeningSettings

    # k=2 for both NEW and BREAK. BREAK remains visible for five bars total.
    agent = SecuritySelectionAgent(
        ScreeningSettings(min_adx=90, new_min_true=2, break_min_true=2)
    )
    ticker = Ticker(symbol="SYN")

    closes = (
        [100.0 + 0.05 * i for i in range(80)]
        + [104.0 + 1.8 * i for i in range(8)]
        + [118.0 - 3.2 * i for i in range(8)]
    )
    bars = _make_synthetic_bars(ticker, closes)

    new_enabled = {
        "ema20_rising": True,
        "price_above_ema50": True,
        "adx_above": True,
    }
    break_enabled = {
        "ema20_falling": True,
        "price_below_ema50": True,
        "adx_below": True,
    }

    signal = agent._trend_signal(
        bars,
        new_enabled,
        break_enabled,
        new_min_true=2,
        break_min_true=2,
    )

    assert signal == "BREAK"


def test_active_break_after_new_transition_emits_break_without_new_edge(monkeypatch):
    from app.agents import screening as screening_module
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings())
    ticker = Ticker(symbol="SYN")
    bars = _make_synthetic_bars(ticker, [100.0 + 0.1 * i for i in range(70)])

    monkeypatch.setattr(screening_module, "build_trend_indicator_series", lambda *args, **kwargs: object())

    def fake_bar_indicator_values(idx, *args, **kwargs):
        return {"new": idx == len(bars) - 2, "break": idx >= 0}

    monkeypatch.setattr(screening_module, "bar_indicator_values", fake_bar_indicator_values)

    signal = agent._trend_signal(
        bars,
        {"new": True},
        {"break": True},
        new_min_true=None,
        break_min_true=None,
    )

    assert signal == "BREAK"


def test_recent_new_downgrades_to_hold_when_current_bar_fails_selected_policy(monkeypatch):
    from app.agents import screening as screening_module
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings())
    ticker = Ticker(symbol="SYN")
    bars = _make_synthetic_bars(ticker, [100.0 + 0.1 * i for i in range(75)])

    ema20 = np.array([0.0] * 70 + [0.0, 1.0, 2.0, 2.0, 0.0])
    ema50 = np.zeros(75, dtype=float)
    adx = np.full(75, np.nan, dtype=float)
    atr = np.ones(75, dtype=float)
    upper = np.full(75, np.nan, dtype=float)
    lower = np.full(75, np.nan, dtype=float)

    def fake_ema(close: np.ndarray, timeperiod: int) -> np.ndarray:
        if timeperiod == 20:
            return ema20
        if timeperiod == 50:
            return ema50
        if timeperiod in (13, 25):
            return np.zeros(len(close), dtype=float)
        raise AssertionError(f"unexpected EMA period {timeperiod}")

    monkeypatch.setattr(screening_module.talib, "EMA", fake_ema)
    monkeypatch.setattr(screening_module.talib, "ADX", lambda *args, **kwargs: adx)
    monkeypatch.setattr(screening_module.talib, "ATR", lambda *args, **kwargs: atr)
    monkeypatch.setattr(screening_module, "supertrend_bands", lambda *args, **kwargs: (upper, lower))

    signal = agent._trend_signal(
        bars,
        {"ema20_rising": True},
        {},
        new_min_true=None,
        break_min_true=None,
    )

    assert signal == "HOLD"


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
async def test_restart_stage_persists_screening_policy_form_values(monkeypatch):
    from app.routes import pipeline as pipeline_module

    class FakeCollection:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        async def update_one(self, selector: dict, update: dict) -> None:
            self.calls.append((selector, update))

    class FakePipeline:
        async def run_stage(self, execution_id: str, from_stage: str) -> None:
            return None

    fake_collection = FakeCollection()

    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: fake_collection)
    monkeypatch.setattr(pipeline_module, "get_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_fire", lambda coro: coro.close())

    response = await pipeline_module.restart_stage(
        qs_id="qs1",
        execution_id="exec1",
        stage="screening",
        from_stage="screening",
        policies_submitted="1",
        policy_supertrend="on",
        policy_ema20_rising="on",
        policy_adx_above=None,
        policy_adx_rising="on",
        policy_price_above_ema50="on",
        policy_tq60_above="on",
        policy_tq20_above=None,
        policy_tq60_min="0.07",
        policy_tq20_min="0.02",
        policy_tsi_above="on",
        policy_tsi_new_min="30",
        new_min_true="99",
        policy_supertrend_break="on",
        policy_ema20_falling_break=None,
        policy_adx_below_break="on",
        policy_adx_falling_break=None,
        policy_price_below_ema50_break=None,
        policy_tsi_below_break=None,
        policy_tsi_break_max="15",
        break_min_true="0",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/quant-systems/qs1/executions/exec1/stages/screening"

    assert len(fake_collection.calls) == 1
    selector, update = fake_collection.calls[0]
    assert selector == {"execution_id": "exec1"}

    screening_cfg = update["$set"]["config_overrides.screening"]
    assert screening_cfg == {
        "policy_supertrend": True,
        "policy_ema20_rising": True,
        "policy_adx_above": False,
        "policy_adx_rising": True,
        "policy_price_above_ema50": True,
        "policy_tq60_above": True,
        "policy_tq20_above": False,
        "policy_tq60_min": 0.07,
        "policy_tq20_min": 0.02,
        "policy_tsi_above": True,
        "policy_tsi_new_min": 30.0,
        "new_min_true": 6,
        "policy_supertrend_break": True,
        "policy_ema20_falling_break": False,
        "policy_adx_below_break": True,
        "policy_adx_falling_break": False,
        "policy_price_below_ema50_break": False,
        "policy_tsi_below_break": False,
        "policy_tsi_break_max": 15.0,
        "break_min_true": 1,
    }


@pytest.mark.asyncio
async def test_restart_stage_clamps_tq_thresholds_and_handles_invalid(monkeypatch):
    from app.routes import pipeline as pipeline_module

    class FakeCollection:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        async def update_one(self, selector: dict, update: dict) -> None:
            self.calls.append((selector, update))

    class FakePipeline:
        async def run_stage(self, execution_id: str, from_stage: str) -> None:
            return None

    fake_collection = FakeCollection()

    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: fake_collection)
    monkeypatch.setattr(pipeline_module, "get_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_fire", lambda coro: coro.close())

    await pipeline_module.restart_stage(
        qs_id="qs1",
        execution_id="exec2",
        stage="screening",
        from_stage="screening",
        policies_submitted="1",
        policy_supertrend="on",
        policy_ema20_rising="on",
        policy_adx_above="on",
        policy_adx_rising="on",
        policy_price_above_ema50="on",
        policy_tq60_above="on",
        policy_tq20_above="on",
        policy_tq60_min="9.9",
        policy_tq20_min="invalid",
        policy_tsi_above="on",
        policy_tsi_new_min="invalid",
        new_min_true="2",
        policy_supertrend_break="on",
        policy_ema20_falling_break="on",
        policy_adx_below_break="on",
        policy_adx_falling_break="on",
        policy_price_below_ema50_break="on",
        policy_tsi_below_break="on",
        policy_tsi_break_max="invalid",
        break_min_true="2",
    )

    _, update = fake_collection.calls[0]
    screening_cfg = update["$set"]["config_overrides.screening"]

    assert screening_cfg["policy_tq60_min"] == 1.0
    assert screening_cfg["policy_tq20_min"] == 0.0


@pytest.mark.asyncio
async def test_restart_stage_persists_warrant_maturity_range(monkeypatch):
    from app.routes import pipeline as pipeline_module

    class FakeCollection:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        async def update_one(self, selector: dict, update: dict) -> None:
            self.calls.append((selector, update))

    class FakePipeline:
        async def run_stage(self, execution_id: str, from_stage: str) -> None:
            return None

    fake_collection = FakeCollection()

    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: fake_collection)
    monkeypatch.setattr(pipeline_module, "get_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_fire", lambda coro: coro.close())

    await pipeline_module.restart_stage(
        qs_id="qs1",
        execution_id="exec3",
        stage="warrant_selection",
        from_stage="warrant_selection",
        maturity_range_submitted="1",
        ws_min_months="9",
        ws_max_months="15",
        ws_strike_min_factor="0.95",
        ws_strike_max_factor="1.00",
        ws_min_score="0.62",
        ws_spread_max_pct="2.25",
    )

    _, update = fake_collection.calls[0]
    ws_cfg = update["$set"]["config_overrides.warrant_selection"]

    assert ws_cfg == {
        "min_days_to_expiry": 270,
        "max_days_to_expiry": 450,
        "strike_min_factor": 0.95,
        "strike_max_factor": 1.0,
        "min_score": 0.62,
        "spread_max_pct": 2.25,
    }


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


@pytest.mark.asyncio
async def test_monitoring_no_holdings_reports_full_free_slots_from_config_override(monkeypatch):
    from app.models.signals import PortfolioAccountSnapshot
    from app.orchestrator import Pipeline

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict) -> list[Position]:
        return []

    async def fake_account_snapshot(*_args, **_kwargs):
        return PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("99636.35"),
            nav_eur=Decimal("99636.35"),
        )

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_portfolio_account_snapshot", fake_account_snapshot)

    screening = SelectionResult(
        selected=[Ticker(symbol="A"), Ticker(symbol="B"), Ticker(symbol="C")],
        scores={"A": 1.0, "B": 0.9, "C": 0.8},
        rationale={},
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 20}},
    }

    result = await pipeline._run_monitoring(run)

    assert result.free_positions == 20
    assert len(result.entry_candidates) == 3
    assert result.positions_to_keep == []
    assert result.positions_to_sell == []
    assert result.nav_eur == Decimal("99636.35")
    assert result.available_cash_eur == Decimal("99636.35")
    assert result.valuation_errors == []


@pytest.mark.asyncio
async def test_monitoring_uses_portfolio_max_positions_override_with_holdings(monkeypatch):
    from app.orchestrator import Pipeline

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict) -> list[Position]:
        return [
            Position(
                ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                quantity=Decimal("1"),
                avg_cost=Decimal("0"),
            )
        ]

    async def fake_warrant_underlying_map(_run: dict, _current_holdings: list[Position] | None = None) -> dict[str, str]:
        return {"ISIN1": "A"}

    async def fake_held_since(_run: dict) -> dict[str, date]:
        return {"WKN1": date.today() - timedelta(days=30)}

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_warrant_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_held_since", fake_held_since)

    screening = SelectionResult(
        selected=[Ticker(symbol="A"), Ticker(symbol="B"), Ticker(symbol="C")],
        scores={"A": 1.0, "B": 0.9, "C": 0.8},
        rationale={},
        trend_signals={"A": "HOLD", "B": "NEW", "C": "NEW"},
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 5}},
    }

    result = await pipeline._run_monitoring(run)

    assert result.free_positions == 4
    assert len(result.positions_to_keep) == 1
    assert len(result.positions_to_sell) == 0
    assert [t.symbol for t in result.entry_candidates] == ["B", "C"]


@pytest.mark.asyncio
async def test_monitoring_resolves_underlying_via_isin_and_sells_on_break(monkeypatch):
    from app.orchestrator import Pipeline

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict) -> list[Position]:
        return [
            Position(
                ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                quantity=Decimal("1"),
                avg_cost=Decimal("0"),
            )
        ]

    async def fake_warrant_underlying_map(_run: dict, _current_holdings: list[Position] | None = None) -> dict[str, str]:
        return {"ISIN1": "A"}

    async def fake_held_since(_run: dict) -> dict[str, date]:
        return {"WKN1": date.today() - timedelta(days=30)}

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_warrant_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_held_since", fake_held_since)

    screening = SelectionResult(
        selected=[Ticker(symbol="A"), Ticker(symbol="B")],
        scores={"A": 1.0, "B": 0.9},
        rationale={},
        trend_signals={"A": "BREAK", "B": "NEW"},
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 5}},
    }

    result = await pipeline._run_monitoring(run)

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].underlying_symbol == "A"
    assert result.positions_to_sell[0].sell_reason == "exit_signal"
    assert result.free_positions == 5
    assert [t.symbol for t in result.entry_candidates] == ["B"]


@pytest.mark.asyncio
async def test_fetch_holdings_ignores_zero_quantity_positions(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "d1", "depot_type": "virtual"}

    class FakeSnapshotsCollection:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {
                "positions": [
                    {"isin": "X1", "wkn": "W1", "quantity": {"value": "0", "unit": "ST"}},
                    {"isin": "X2", "wkn": "W2", "quantity": {"value": "0.0000", "unit": "ST"}},
                    {"isin": "X3", "wkn": "W3", "quantity": {"value": "2", "unit": "ST"}},
                ]
            }

    monkeypatch.setattr(orchestrator_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(orchestrator_module, "virtual_depot_snapshots_collection", lambda: FakeSnapshotsCollection())

    pipeline = Pipeline()
    holdings = await pipeline._fetch_holdings({"quant_system_id": "qs1"})

    assert len(holdings) == 1
    assert holdings[0].ticker.symbol == "W3"
    assert holdings[0].quantity == Decimal("2")


@pytest.mark.asyncio
async def test_fetch_holdings_maps_average_purchase_price_to_avg_cost(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "d1", "depot_type": "virtual"}

    class FakeSnapshotsCollection:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {
                "positions": [
                    {
                        "isin": "X3",
                        "wkn": "W3",
                        "instrument_name": "Test",
                        "quantity": {"value": "2", "unit": "ST"},
                        "average_purchase_price": {"value": "12.34", "unit": "EUR"},
                    }
                ]
            }

    monkeypatch.setattr(orchestrator_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(orchestrator_module, "virtual_depot_snapshots_collection", lambda: FakeSnapshotsCollection())

    pipeline = Pipeline()
    holdings = await pipeline._fetch_holdings({"quant_system_id": "qs1"})

    assert len(holdings) == 1
    assert holdings[0].avg_cost == Decimal("12.34")


@pytest.mark.asyncio
async def test_portfolio_account_snapshot_values_virtual_holding_at_fresh_bid(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "d1", "depot_type": "virtual"}

    class FakeSnapshotsCollection:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {"current_cash": 45_000.0}

    position = Position(
        ticker=Ticker(symbol="WKN1", isin="ISIN1"),
        quantity=Decimal("10"),
        avg_cost=Decimal("2.5"),
    )
    quote_time = datetime.now(timezone.utc)
    pipeline = Pipeline()
    monkeypatch.setattr(orchestrator_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(
        orchestrator_module,
        "virtual_depot_snapshots_collection",
        lambda: FakeSnapshotsCollection(),
    )

    async def fake_underlying_map(_run: dict, _holdings: list[Position]) -> dict[str, str]:
        return {"ISIN1": "AAPL"}

    async def fake_quotes(_isins: list[str]) -> dict[str, WarrantSnapshot]:
        return {
            "ISIN1": WarrantSnapshot(
                warrant_isin="ISIN1",
                bid=5.0,
                currency="EUR",
                timestamp_utc=quote_time,
                issuer_action=True,
                issuer_no_fee_action=True,
            )
        }

    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_warrant_snapshots", fake_quotes)

    snapshot = await pipeline._fetch_portfolio_account_snapshot(
        {"quant_system_id": "qs1"},
        [position],
        {"US0378331005": "Technology"},
        {"AAPL": "US0378331005"},
        underlying_names_by_symbol={"AAPL": "Apple Inc."},
    )

    assert snapshot.available_cash_eur == Decimal("45000.0")
    assert snapshot.holdings[0].bid_price_eur == Decimal("5.0")
    assert snapshot.holdings[0].market_value_eur == Decimal("50.0")
    assert snapshot.holdings[0].sector == "Technology"
    assert snapshot.holdings[0].underlying_isin == "US0378331005"
    assert snapshot.holdings[0].underlying_name == "Apple Inc."
    assert snapshot.holdings[0].issuer_action is True
    assert snapshot.holdings[0].issuer_no_fee_action is True
    assert snapshot.nav_eur == Decimal("45050.0")
    assert snapshot.valuation_errors == []


@pytest.mark.asyncio
async def test_portfolio_account_snapshot_uses_latest_real_eur_cash(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "real-1", "depot_type": "real"}

    class FakeRealSnapshots:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {"account_name": "account-1"}

    class FakeBalanceCursor:
        def sort(self, *_args):
            return self

        async def to_list(self, length=None):
            return [
                {"account_type": "Verrechnungskonto", "balance": {"value": "101865.98", "unit": "EUR"}},
                {"account_type": "Tagesgeld PLUS-Konto", "balance": {"value": "0.03", "unit": "EUR"}},
                {"account_type": "Girokonto", "balance": {"value": "106.03", "unit": "EUR"}},
            ]

    class FakeAccountBalances:
        def find(self, _query: dict, _projection: dict):
            return FakeBalanceCursor()

    class FakeFinanceDB:
        def __getitem__(self, collection: str):
            if collection == "depot_snapshots":
                return FakeRealSnapshots()
            if collection == "account_balances":
                return FakeAccountBalances()
            raise KeyError(collection)

    pipeline = Pipeline()
    monkeypatch.setattr(orchestrator_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(orchestrator_module, "finance_db", lambda: FakeFinanceDB())

    snapshot = await pipeline._fetch_portfolio_account_snapshot(
        {"quant_system_id": "qs1"},
        [],
        {},
        {},
    )

    assert snapshot.source == "real"
    assert snapshot.available_cash_eur == Decimal("101972.04")
    assert snapshot.nav_eur == Decimal("101972.04")
    assert snapshot.valuation_errors == []


def test_portfolio_quote_validation_rejects_stale_and_non_eur_quotes():
    from app.orchestrator import Pipeline

    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    stale = WarrantSnapshot(
        warrant_isin="STALE",
        bid=2.0,
        currency="EUR",
        timestamp_utc=now - timedelta(hours=73),
    )
    foreign = WarrantSnapshot(
        warrant_isin="USD",
        bid=2.0,
        currency="USD",
        timestamp_utc=now,
    )

    assert Pipeline._portfolio_quote_error(stale, now) == "quote is older than 72 hours"
    weekend_quote = WarrantSnapshot(
        warrant_isin="WEEKEND",
        bid=2.0,
        currency="EUR",
        timestamp_utc=now - timedelta(hours=72),
    )
    assert Pipeline._portfolio_quote_error(weekend_quote, now) is None
    assert Pipeline._portfolio_quote_error(foreign, now) == "quote currency is not EUR"


@pytest.mark.asyncio
async def test_fetch_holdings_fails_fast_on_legacy_position_fields(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "d1", "depot_type": "virtual"}

    class FakeSnapshotsCollection:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {
                "positions": [
                    {
                        "isin": "X3",
                        "wkn": "W3",
                        "quantity": {"value": "2", "unit": "ST"},
                        "purchase_price": {"value": "12.34", "unit": "EUR"},
                    }
                ]
            }

    monkeypatch.setattr(orchestrator_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(orchestrator_module, "virtual_depot_snapshots_collection", lambda: FakeSnapshotsCollection())

    pipeline = Pipeline()
    with pytest.raises(RuntimeError, match="Legacy position fields"):
        await pipeline._fetch_holdings({"quant_system_id": "qs1"})


@pytest.mark.asyncio
async def test_fetch_held_since_uses_snapshot_held_since_date(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "d1", "depot_type": "real"}

    class FakeDepotSnapshotsCollection:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {
                "positions": [
                    {"wkn": "W1", "held_since_date": "2026-01-03"},
                    {"wkn": "W2", "held_since_date": None},
                ]
            }

    class FakeFinanceDB:
        def __getitem__(self, name: str):
            if name == "depot_snapshots":
                return FakeDepotSnapshotsCollection()
            raise KeyError(name)

    monkeypatch.setattr(orchestrator_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(orchestrator_module, "finance_db", lambda: FakeFinanceDB())

    pipeline = Pipeline()
    held_since = await pipeline._fetch_held_since({"quant_system_id": "qs1"})

    assert held_since == {"W1": date(2026, 1, 3)}


def test_monitoring_warrant_health_check_flags_threshold_breaches():
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)

    degraded, detail = agent._check_warrant_health(
        warrant_isin="DE000TEST123",
        snapshot=WarrantSnapshot(
            warrant_isin="DE000TEST123",
            spread_pct=2.6,
            leverage=2.5,
            days_to_maturity=59,
            delta=0.75,
        ),
    )

    assert degraded is True
    assert detail is not None
    assert "spread too wide" in detail
    assert "leverage too low" in detail
    assert "maturity too short" in detail
    assert "delta too high" in detail


def test_monitoring_warrant_health_check_keeps_exact_threshold_values():
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)

    degraded, detail = agent._check_warrant_health(
        warrant_isin="DE000TEST123",
        snapshot=WarrantSnapshot(
            warrant_isin="DE000TEST123",
            spread_pct=2.5,
            leverage=3.0,
            days_to_maturity=60,
            delta=0.3,
        ),
    )
    assert degraded is False
    assert detail is None

    degraded, detail = agent._check_warrant_health(
        warrant_isin="DE000TEST123",
        snapshot=WarrantSnapshot(
            warrant_isin="DE000TEST123",
            spread_pct=2.5,
            leverage=8.0,
            days_to_maturity=60,
            delta=0.7,
        ),
    )
    assert degraded is False
    assert detail is None


@pytest.mark.asyncio
async def test_monitoring_rolls_degraded_warrant_without_break_signal():
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "HOLD"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=30)},
            warrant_snapshots={
                "ISIN1": WarrantSnapshot(
                    warrant_isin="ISIN1",
                    spread_pct=3.1,
                )
            },
            max_positions=5,
        )
    )

    assert len(result.positions_to_roll) == 1
    assert result.positions_to_roll[0].sell_reason is None
    assert len(result.positions_to_sell) == 0
    assert len(result.positions_to_keep) == 0


@pytest.mark.asyncio
async def test_monitoring_keeps_non_degraded_without_exit_signal():
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "HOLD"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=1)},
            warrant_snapshots={
                "ISIN1": WarrantSnapshot(
                    warrant_isin="ISIN1",
                    spread_pct=2.0,
                    leverage=4.5,
                    days_to_maturity=120,
                    delta=0.5,
                )
            },
            max_positions=5,
        )
    )

    assert len(result.positions_to_keep) == 1
    assert len(result.positions_to_sell) == 0


@pytest.mark.asyncio
async def test_monitoring_break_sells_immediately_regardless_of_warrant_degradation():
    """BREAK always triggers immediate SELL; warrant degradation does not change that."""
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "BREAK"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=30)},
            warrant_snapshots={
                "ISIN1": WarrantSnapshot(
                    warrant_isin="ISIN1",
                    spread_pct=3.1,
                )
            },
            max_positions=5,
        )
    )

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].sell_reason == "exit_signal"
    assert result.positions_to_sell[0].decision_reason == "trend break"
    assert len(result.positions_to_roll) == 0
    assert len(result.positions_to_keep) == 0


@pytest.mark.asyncio
async def test_monitoring_confirmed_break_sells_regardless_of_warrant_health_and_grace():
    agent = MonitoringAgent(settings=MonitoringSettings(min_holding_days=5), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "BREAK"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=1)},
            warrant_snapshots={
                "ISIN1": WarrantSnapshot(
                    warrant_isin="ISIN1",
                    spread_pct=3.2,
                )
            },
            max_positions=5,
        )
    )

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].sell_reason == "exit_signal"
    assert len(result.positions_to_roll) == 0


@pytest.mark.asyncio
async def test_monitoring_break_sells_immediately_within_grace_period():
    """Grace period only governs warrant-health ROLL; BREAK always sells immediately."""
    agent = MonitoringAgent(settings=MonitoringSettings(min_holding_days=5), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "BREAK"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=1)},
            max_positions=5,
        )
    )

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].sell_reason == "exit_signal"
    assert len(result.positions_to_keep) == 0


@pytest.mark.asyncio
async def test_monitoring_holds_degraded_before_roll_grace_period():
    agent = MonitoringAgent(settings=MonitoringSettings(min_holding_days=5), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "HOLD"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=1)},
            warrant_snapshots={
                "ISIN1": WarrantSnapshot(
                    warrant_isin="ISIN1",
                    spread_pct=3.2,
                )
            },
            max_positions=5,
        )
    )

    assert len(result.positions_to_keep) == 1
    assert len(result.positions_to_roll) == 0
    assert len(result.positions_to_sell) == 0


@pytest.mark.asyncio
async def test_monitoring_sells_break_during_grace_with_candle_confirmation():
    agent = MonitoringAgent(settings=MonitoringSettings(min_holding_days=5), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "BREAK"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=1)},
            max_positions=5,
        )
    )

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].sell_reason == "exit_signal"


@pytest.mark.asyncio
async def test_fetch_warrant_snapshots_extracts_metrics(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    today = date.today()
    quote_calls = []

    class FakeFinHubTool:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get_warrant_detail(self, isin: str) -> dict | None:
            if isin == "ISIN1":
                return {
                    "market_data": {
                        "spread_percent": 1.8,
                        "bid": 1.9,
                        "ask": 2.1,
                        "prev_close": 1.8,
                        "timestamp_utc": "2026-10-02T19:59:00Z",
                    },
                    "analytics": {"leverage": 4.2, "delta": 0.44},
                    "reference_data": {
                        "maturity_date": (today + timedelta(days=120)).isoformat(),
                        "currency": "EUR",
                        "issuer_action": True,
                        "issuer_no_fee_action": False,
                    },
                }
            if isin == "ISIN2":
                return {"market_data": {}, "analytics": {}, "reference_data": {}}
            return None

        async def get_quote(self, isin: str) -> dict | None:
            quote_calls.append(isin)
            return {"prev_close": 0.1}

    monkeypatch.setattr(orchestrator_module, "FinHubTool", FakeFinHubTool)

    pipeline = Pipeline()
    snapshots = await pipeline._fetch_warrant_snapshots(["ISIN1", "ISIN2", "ISIN3"])

    assert set(snapshots.keys()) == {"ISIN1"}
    snap = snapshots["ISIN1"]
    assert snap.spread_pct == 1.8
    assert snap.leverage == 4.2
    assert snap.delta == 0.44
    assert snap.days_to_maturity == 120
    assert snap.bid == 1.9
    assert snap.ask == 2.1
    assert snap.currency == "EUR"
    assert snap.timestamp_utc is not None
    assert snap.issuer_action is True
    assert snap.issuer_no_fee_action is False
    assert snap.timestamp_utc.isoformat() == "2026-10-02T19:59:00+00:00"
    assert snap.bid_ask_midprice == 2.0
    assert snap.prev_close == 1.8
    assert quote_calls == []
    assert Pipeline._parse_utc_timestamp("2026-10-02T19:59:00") is None
