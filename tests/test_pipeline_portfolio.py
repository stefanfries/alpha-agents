from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.agents.monitoring import WarrantSnapshot
from app.agents.portfolio import PortfolioConstructionAgent
from app.models.market import Position, Ticker
from app.models.signals import (
    PortfolioAccountSnapshot,
    PortfolioHoldingValue,
    RollReplacement,
    SelectedWarrant,
    SelectionResult,
    WarrantSelectionResult,
)


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
@pytest.mark.parametrize("with_challenger", [False, True])
async def test_run_portfolio_forwards_selected_roll_as_paired_trade(monkeypatch, with_challenger):
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
        challenger_selected=[SelectedWarrant(
            underlying=Ticker(symbol="CHALLENGER"),
            warrant_isin="CHALLENGER-ISIN", warrant_wkn="CHALLENGER",
            score=0.99, rationale="advisory only",
        )] if with_challenger else [],
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
    assert result.new_positions == []
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
@pytest.mark.parametrize("sizing_method", ["equal", "score_weighted"])
async def test_empty_entry_pool_preserves_kept_positions_in_portfolio(sizing_method):
    from app.models.signals import PortfolioAccountSnapshot

    kept = Position(ticker=Ticker(symbol="KEEP", isin="IK"), quantity=Decimal("1"), avg_cost=Decimal("1"))
    sold = Position(ticker=Ticker(symbol="SELL", isin="IS"), quantity=Decimal("1"), avg_cost=Decimal("1"))
    agent = PortfolioConstructionAgent(
        capital_eur=10000,
        current_holdings=[kept, sold],
        kept_warrant_isins={"IK"},
        sizing_method=sizing_method,
        account_snapshot=PortfolioAccountSnapshot(source="virtual"),
    )
    result = await agent.run(SelectionResult(selected=[], scores={}, rationale={}))
    assert result.new_positions == []
    assert result.close_positions == [sold]


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
@pytest.mark.parametrize(
    "cash_value, other_cash, bid, expected_cash, expected_nav, expected_errors",
    [
        ("101000.00", "106.06", 10.0, "101106.06", "101206.06", []),
        ("-55.62", "0.00", 10.0, "-55.62", "44.38", []),
        ("-55.62", "100.00", 10.0, "44.38", "144.38", []),
        (
            "invalid", "0.00", 10.0, None, None,
            [
                "Cash amount for Verrechnungskonto is invalid",
                "Current EUR cash balance is unavailable",
            ],
        ),
        (
            "-55.62", "0.00", None, "-55.62", None,
            [
                "ISIN1: missing or invalid bid quote",
                "NAV is incomplete because one or more held-position quotes are invalid",
            ],
        ),
    ],
)
async def test_portfolio_account_snapshot_uses_latest_real_eur_cash(
    monkeypatch, cash_value, other_cash, bid, expected_cash, expected_nav, expected_errors,
):
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
                {
                    "account_type": "Verrechnungskonto",
                    "balance": {"value": "101865.98", "unit": "EUR"},
                    "available_cash_amount": {"value": cash_value, "unit": "EUR"},
                },
                {"account_type": "Tagesgeld PLUS-Konto", "balance": {"value": "0.00", "unit": "EUR"}},
                {"account_type": "Girokonto", "balance": {"value": other_cash, "unit": "EUR"}},
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
        [Position(
            ticker=Ticker(symbol="WKN1", isin="ISIN1"), quantity=Decimal("10"), avg_cost=Decimal("2.5"),
        )],
        {},
        {},
        underlying_map={},
        warrant_quotes={
            "ISIN1": WarrantSnapshot(
                warrant_isin="ISIN1", bid=bid, currency="EUR", timestamp_utc=datetime.now(timezone.utc),
            ),
        },
    )

    assert snapshot.source == "real"
    assert snapshot.available_cash_eur == (Decimal(expected_cash) if expected_cash is not None else None)
    assert snapshot.nav_eur == (Decimal(expected_nav) if expected_nav is not None else None)
    assert snapshot.valuation_errors == expected_errors


def test_portfolio_quote_validation_rejects_stale_and_non_eur_quotes():
    from app.portfolio_valuation import portfolio_quote_error

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

    assert portfolio_quote_error(stale, now) == "quote is older than 72 hours"
    weekend_quote = WarrantSnapshot(
        warrant_isin="WEEKEND",
        bid=2.0,
        currency="EUR",
        timestamp_utc=now - timedelta(hours=72),
    )
    assert portfolio_quote_error(weekend_quote, now) is None
    assert portfolio_quote_error(foreign, now) == "quote currency is not EUR"


def test_portfolio_valuation_preserves_valid_values_when_one_quote_is_missing():
    from app.portfolio_valuation import build_portfolio_account_snapshot

    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    holdings = [
        Position(ticker=Ticker(symbol="GOOD", isin="ISIN_GOOD"), quantity=Decimal("2"), avg_cost=Decimal("1")),
        Position(ticker=Ticker(symbol="MISSING", isin="ISIN_MISSING"), quantity=Decimal("1"), avg_cost=Decimal("1")),
    ]
    errors = ["Cash warning", "Cash warning"]
    snapshot = build_portfolio_account_snapshot(
        source="virtual",
        cash_eur=Decimal("100"),
        holdings=holdings,
        underlying_map={"ISIN_GOOD": "AAPL"},
        warrant_quotes={
            "ISIN_GOOD": WarrantSnapshot(
                warrant_isin="ISIN_GOOD", bid=5.0, currency="EUR", timestamp_utc=now,
            ),
        },
        sectors_by_underlying_isin={"US0378331005": "Technology"},
        underlying_isins_by_symbol={"AAPL": "US0378331005"},
        underlying_names_by_symbol={"AAPL": "Apple Inc."},
        errors=errors,
        now=now,
        quote_max_age_hours=72,
    )

    assert snapshot.holdings[0].market_value_eur == Decimal("10")
    assert snapshot.holdings[0].underlying_name == "Apple Inc."
    assert snapshot.holdings[0].sector == "Technology"
    assert snapshot.holdings[1].market_value_eur is None
    assert snapshot.nav_eur is None
    assert snapshot.valuation_errors == [
        "Cash warning",
        "ISIN_MISSING: missing or invalid bid quote",
        "NAV is incomplete because one or more held-position quotes are invalid",
    ]
    assert errors == ["Cash warning", "Cash warning"]
