from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.agents.risk import RiskAgent
from app.config import RiskSettings
from app.models.market import Position, Ticker
from app.models.signals import (
    PlannedPosition,
    PortfolioAccountSnapshot,
    PortfolioHoldingValue,
    PortfolioProposal,
)


def test_risk_sector_cap_defaults_to_thirty_percent():
    assert RiskSettings().max_sector_weight == 0.3


@pytest.mark.asyncio
async def test_run_risk_uses_persisted_risk_and_position_count_overrides(monkeypatch):
    from app import orchestrator as orchestrator_module

    captured: dict = {}

    class FakeRiskAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, proposal):
            captured["proposal"] = proposal
            return proposal

    monkeypatch.setattr(orchestrator_module, "RiskAgent", FakeRiskAgent)
    proposal = PortfolioProposal(positions=[], target_weights={})
    run = {
        "config_overrides": {
            "risk": {"max_position_multiple": 2.5, "max_sector_weight": 0.3},
            "portfolio": {"max_positions": 7},
        },
        "stages": {"portfolio": {"result": proposal.model_dump(mode="json")}},
    }

    result = await orchestrator_module.Pipeline()._run_risk(run)

    assert result == proposal
    assert captured["max_position_multiple"] == 2.5
    assert captured["max_sector_weight"] == 0.3
    assert captured["max_positions"] == 7


def test_risk_template_renders_underlying_details_and_projected_charts():
    import re

    from app.routes.pipeline import templates

    html = templates.env.get_template("stages/risk.html").render(
        execution={"execution_id": "exec1", "stages": {}},
        qs_id="qs1",
        stage_status="awaiting_review",
        stage_result={
            "approved_positions": [],
            "rejected_positions": [],
            "approved_roll_trades": [],
            "rejected_roll_trades": [],
            "risk_notes": {},
            "portfolio_warnings": [],
        },
        risk_cfg={"max_position_multiple": 3.0, "max_sector_weight": 0.3},
        risk_view={
            "review_rows": [{
                "underlying_symbol": "ACME",
                "underlying_name": "Acme Corp",
                "warrant_wkn": "WKN123",
                "action": "BUY",
                "status": "approved",
                "notional_eur": 1000.0,
                "note": "",
            }],
            "available": True,
            "nav_eur": 10000.0,
            "cash_shortfall_eur": 0.0,
            "sector_rows": [{
                "sector": "Cash", "value_eur": 9000.0, "weight_pct": 90.0, "color": "#89939e",
                "tooltip": "Cash", "dash_array": "237.504 26.390", "dash_offset": "0.000",
            }, {
                "sector": "Technology", "value_eur": 1000.0, "weight_pct": 10.0,
                "color": "#e76f51", "tooltip": "Technology\nAcme Corp (ACME) · WKN123",
                "dash_array": "26.390 237.504", "dash_offset": "-237.504",
            }],
            "sector_gradient": "#89939e 0% 100%",
            "position_rows": [{
                "underlying_symbol": "Cash", "underlying_name": "Cash", "warrant_wkn": "—",
                "weight_pct": 90.0, "bar_width_pct": 90.0, "color": "#89939e", "is_cash": True,
            }, {
                "underlying_symbol": "ACME", "underlying_name": "Acme Corp", "warrant_wkn": "WKN123",
                "weight_pct": 10.0, "bar_width_pct": 10.0, "color": "#e76f51", "is_cash": False,
            }],
            "position_limit_pct": 20.0,
            "position_limit_marker_pct": 20.0,
        },
        current_stage="risk",
        stages=["risk"],
        stage_labels={"risk": "Risk"},
    )

    assert "<th>Underlying</th><th>Name</th>" in html
    assert "Warrant WKN" in html
    assert "Acme Corp" in html
    assert "WKN123" in html
    assert "Projected sector allocation" in html
    assert "Projected position weights" in html
    assert html.count('class="risk-donut-segment"') == 2
    assert "stroke-dasharray=\"237.504 26.390\"" in html
    assert "data-risk-sector-tooltip" in html
    assert "data-bs-title=" in html
    assert "Acme Corp (ACME) · WKN123" in html
    assert ".risk-donut-segment { fill:none; stroke-width:24;" in html
    assert "width:62%; aspect-ratio:1; border-radius:50%; background:var(--bs-body-bg)" in html
    segment_markup = re.findall(r"<circle\b[^>]*>", html)
    assert len(segment_markup) == 2
    assert all(
        "data-bs-title=" in segment
        and 'data-bs-toggle="tooltip"' not in segment
        and re.search(r"(?<![\w-])title=", segment) is None
        for segment in segment_markup
    )


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
@pytest.mark.parametrize(
    ("notional", "expected_approved"),
    [(Decimal("50000"), True), (Decimal("50000.01"), False)],
)
async def test_risk_position_cap_boundary(notional, expected_approved):
    planned = PlannedPosition(
        ticker=Ticker(symbol="BOUNDARY"),
        notional_eur=notional,
        target_weight=0.5,
        sector="Technology",
    )
    result = await RiskAgent(
        max_position_multiple=3.0,
        max_positions=6,
        max_sector_weight=0.6,
    ).run(PortfolioProposal(
        positions=[planned],
        target_weights={"BOUNDARY": 0.5},
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("100000"),
            nav_eur=Decimal("100000"),
        ),
    ))

    assert (planned in result.approved_positions) is expected_approved
    assert (planned in result.rejected_positions) is not expected_approved
    if not expected_approved:
        assert "position cap" in result.risk_notes["BOUNDARY"]


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
@pytest.mark.parametrize(
    ("notional", "expected_approved"),
    [(Decimal("10000"), True), (Decimal("10000.01"), False)],
)
async def test_risk_sector_cap_boundary(notional, expected_approved):
    held = Position(
        ticker=Ticker(symbol="HELD", isin="HELD-ISIN"),
        quantity=Decimal("100"),
        avg_cost=Decimal("200"),
    )
    planned = PlannedPosition(
        ticker=Ticker(symbol="BOUNDARY"),
        notional_eur=notional,
        target_weight=float(notional / Decimal("100000")),
        sector="Technology",
    )
    result = await RiskAgent(
        max_position_multiple=3.0,
        max_positions=2,
        max_sector_weight=0.3,
    ).run(PortfolioProposal(
        positions=[planned],
        target_weights={"BOUNDARY": planned.target_weight},
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("80000"),
            nav_eur=Decimal("100000"),
            holdings=[PortfolioHoldingValue(
                position=held,
                underlying_isin="HELD-UNDERLYING",
                sector="Technology",
                bid_price_eur=Decimal("200"),
                market_value_eur=Decimal("20000"),
                quote_timestamp_utc=datetime.now(timezone.utc),
            )],
        ),
    ))

    assert (planned in result.approved_positions) is expected_approved
    assert (planned in result.rejected_positions) is not expected_approved
    if not expected_approved:
        assert "sector cap" in result.risk_notes["BOUNDARY"]


@pytest.mark.asyncio
async def test_risk_blocks_new_position_when_held_sector_is_unknown():
    held = Position(
        ticker=Ticker(symbol="UNKNOWN", isin="UNKNOWN-ISIN"),
        quantity=Decimal("100"),
        avg_cost=Decimal("200"),
    )
    planned = PlannedPosition(
        ticker=Ticker(symbol="BUY"),
        notional_eur=Decimal("1000"),
        target_weight=0.01,
        sector="Technology",
    )
    result = await RiskAgent(max_positions=6).run(PortfolioProposal(
        positions=[planned],
        target_weights={"BUY": 0.01},
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("80000"),
            nav_eur=Decimal("100000"),
            holdings=[PortfolioHoldingValue(
                position=held,
                underlying_isin="UNKNOWN-UNDERLYING",
                sector=None,
                bid_price_eur=Decimal("200"),
                market_value_eur=Decimal("20000"),
                quote_timestamp_utc=datetime.now(timezone.utc),
            )],
        ),
    ))

    assert result.approved_positions == []
    assert result.rejected_positions == [planned]
    assert "unknown sector" in result.risk_notes["BUY"]


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
