import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.agents.base import Agent
from app.formatting import format_eur
from app.models.signals import (
    PlannedPosition,
    PortfolioAccountSnapshot,
    PortfolioProposal,
    RiskAssessment,
    RollTrade,
)

logger = logging.getLogger(__name__)


class RiskAgent(Agent[PortfolioProposal, RiskAssessment]):
    name = "risk"

    def __init__(
        self,
        max_position_multiple: float = 3.0,
        max_sector_weight: float = 0.8,
        max_positions: int = 15,
        quote_max_age_hours: int = 72,
    ) -> None:
        self._max_position_multiple = Decimal(str(max_position_multiple))
        self._max_sector_weight = Decimal(str(max_sector_weight))
        self._max_positions = max_positions
        self._quote_max_age_hours = quote_max_age_hours

    async def run(self, input: PortfolioProposal) -> RiskAssessment:
        approved: list[PlannedPosition] = []
        rejected: list[PlannedPosition] = []
        approved_rolls: list[RollTrade] = []
        rejected_rolls: list[RollTrade] = []
        notes: dict[str, str] = {}
        warnings: list[str] = []
        snapshot = input.account_snapshot
        block_reason = self._account_block_reason(
            snapshot,
            datetime.now(timezone.utc),
            self._quote_max_age_hours,
        )

        if block_reason:
            for position in input.positions:
                rejected.append(position)
                notes[position.ticker.symbol] = block_reason
            for roll in input.roll_trades:
                rejected_rolls.append(roll)
                notes[roll.replacement.ticker.symbol] = block_reason
            return self._result(input, approved, rejected, approved_rolls, rejected_rolls, notes, warnings)

        assert snapshot is not None and snapshot.nav_eur is not None
        position_limit = (
            snapshot.nav_eur * self._max_position_multiple / Decimal(self._max_positions)
            if self._max_positions > 0
            else Decimal("0")
        )
        sector_limit = snapshot.nav_eur * self._max_sector_weight
        closing_isins = {position.ticker.isin for position in input.close_positions if position.ticker.isin}
        holdings_by_isin = {
            holding.position.ticker.isin: holding
            for holding in snapshot.holdings
            if holding.position.ticker.isin
        }
        sector_exposure: dict[str, Decimal] = {}
        unknown_sector_holdings: list[str] = []

        for holding in snapshot.holdings:
            isin = holding.position.ticker.isin or holding.position.ticker.symbol
            if isin in closing_isins:
                continue
            value = holding.market_value_eur or Decimal("0")
            if value > position_limit:
                warnings.append(
                    f"Existing position {isin} is above the position cap "
                    f"({format_eur(value)} > {format_eur(position_limit)})"
                )
            if not holding.sector:
                unknown_sector_holdings.append(isin)
                continue
            sector_exposure[holding.sector] = sector_exposure.get(holding.sector, Decimal("0")) + value

        for sector, value in sector_exposure.items():
            if value > sector_limit:
                warnings.append(
                    f"Existing {sector} exposure is above the sector cap "
                    f"({format_eur(value)} > {format_eur(sector_limit)})"
                )

        for roll in input.roll_trades:
            replacement = roll.replacement
            symbol = replacement.ticker.symbol
            incumbent_isin = roll.incumbent.ticker.isin or ""
            incumbent_value = holdings_by_isin.get(incumbent_isin)
            if unknown_sector_holdings:
                rejected_rolls.append(roll)
                notes[symbol] = "Cannot verify sector limit while a held position has unknown sector"
                continue
            if incumbent_value is None or not incumbent_value.sector:
                rejected_rolls.append(roll)
                notes[symbol] = "Roll incumbent has no mapped sector/value"
                continue
            if not replacement.sector:
                rejected_rolls.append(roll)
                notes[symbol] = "Replacement underlying has no mapped sector"
                continue
            if replacement.notional_eur > position_limit:
                rejected_rolls.append(roll)
                notes[symbol] = (
                    f"Notional {format_eur(replacement.notional_eur)} exceeds position cap "
                    f"{format_eur(position_limit)}"
                )
                continue

            trial_exposure = dict(sector_exposure)
            trial_exposure[incumbent_value.sector] = (
                trial_exposure.get(incumbent_value.sector, Decimal("0"))
                - (incumbent_value.market_value_eur or Decimal("0"))
            )
            trial_exposure[replacement.sector] = (
                trial_exposure.get(replacement.sector, Decimal("0")) + replacement.notional_eur
            )
            if trial_exposure[replacement.sector] > sector_limit:
                rejected_rolls.append(roll)
                notes[symbol] = (
                    f"{replacement.sector} exposure would exceed sector cap "
                    f"{format_eur(sector_limit)}"
                )
                continue
            sector_exposure = trial_exposure
            approved_rolls.append(roll)

        active_position_count = len(snapshot.holdings) - len(closing_isins)
        free_slots = max(0, self._max_positions - active_position_count)

        for position in input.positions:
            symbol = position.ticker.symbol
            if unknown_sector_holdings:
                rejected.append(position)
                notes[symbol] = "Cannot verify sector limit while a held position has unknown sector"
                continue
            if not position.sector:
                rejected.append(position)
                notes[symbol] = "Underlying has no mapped sector"
                continue
            if position.notional_eur > position_limit:
                rejected.append(position)
                notes[symbol] = (
                    f"Notional {format_eur(position.notional_eur)} exceeds position cap "
                    f"{format_eur(position_limit)}"
                )
                continue
            if free_slots <= 0:
                rejected.append(position)
                notes[symbol] = f"Max position count ({self._max_positions}) reached"
                continue
            projected_sector = sector_exposure.get(position.sector, Decimal("0")) + position.notional_eur
            if projected_sector > sector_limit:
                rejected.append(position)
                notes[symbol] = (
                    f"{position.sector} exposure would exceed sector cap {format_eur(sector_limit)}"
                )
                continue
            approved.append(position)
            sector_exposure[position.sector] = projected_sector
            free_slots -= 1

        logger.info(
            "Risk check: %d positions and %d rolls approved, %d positions and %d rolls rejected",
            len(approved), len(approved_rolls), len(rejected), len(rejected_rolls),
        )
        return self._result(input, approved, rejected, approved_rolls, rejected_rolls, notes, warnings)

    @staticmethod
    def _account_block_reason(
        snapshot: PortfolioAccountSnapshot | None,
        now: datetime,
        quote_max_age_hours: int = 72,
    ) -> str | None:
        if snapshot is None or snapshot.nav_eur is None:
            return "Current NAV is unavailable; risk-increasing orders are blocked"
        if snapshot.valuation_errors:
            return "Account valuation is incomplete; risk-increasing orders are blocked"
        for holding in snapshot.holdings:
            quote_time = holding.quote_timestamp_utc
            if (
                holding.market_value_eur is None
                or holding.bid_price_eur is None
                or quote_time is None
                or quote_time.tzinfo is None
                or quote_time.utcoffset() is None
            ):
                return "Held-position quote data is incomplete; risk-increasing orders are blocked"
            age = now - quote_time.astimezone(timezone.utc)
            if age.total_seconds() < 0 or age > timedelta(hours=quote_max_age_hours):
                return "Held-position quote is stale; risk-increasing orders are blocked"
        return None

    @staticmethod
    def _result(
        input: PortfolioProposal,
        approved: list[PlannedPosition],
        rejected: list[PlannedPosition],
        approved_rolls: list[RollTrade],
        rejected_rolls: list[RollTrade],
        notes: dict[str, str],
        warnings: list[str],
    ) -> RiskAssessment:
        return RiskAssessment(
            approved_positions=approved,
            rejected_positions=rejected,
            risk_notes=notes,
            close_positions=input.close_positions,
            approved_roll_trades=approved_rolls,
            rejected_roll_trades=rejected_rolls,
            portfolio_warnings=warnings,
        )