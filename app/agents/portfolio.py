import logging
from decimal import Decimal

from app.agents.base import Agent
from app.models.market import Position, Ticker
from app.models.signals import (
    PlannedPosition,
    PortfolioAccountSnapshot,
    PortfolioProposal,
    RollTrade,
    SelectedWarrant,
    SelectionResult,
)
from app.policies.transaction_costs import (
    calculate_equal_buy_amount_eur,
    calculate_order_fee_eur,
    calculate_slippage_eur,
)

logger = logging.getLogger(__name__)


class PortfolioConstructionAgent(Agent[SelectionResult, PortfolioProposal]):
    name = "portfolio"

    def __init__(
        self,
        capital_eur: float,
        current_holdings: list[Position] | None = None,
        sizing_method: str = "equal",
        kept_warrant_isins: set[str] | None = None,
        roll_replacements: list[SelectedWarrant] | None = None,
        roll_incumbent_isins: dict[str, str] | None = None,
        account_snapshot: PortfolioAccountSnapshot | None = None,
        max_positions: int = 15,
        slippage_bps: float | None = None,
        planned_metadata_by_isin: dict[str, dict[str, str | bool]] | None = None,
    ) -> None:
        self._capital = capital_eur
        self._holdings = {p.ticker.isin for p in (current_holdings or []) if p.ticker.isin}
        self._holding_positions = {p.ticker.isin: p for p in (current_holdings or []) if p.ticker.isin}
        self._sizing_method = sizing_method
        # Warrant ISINs that monitoring determined should be kept — excluded from close_positions
        self._kept_isins: set[str] = kept_warrant_isins or set()
        self._roll_replacements = roll_replacements or []
        self._roll_incumbent_isins = roll_incumbent_isins or {}
        self._account_snapshot = account_snapshot
        self._max_positions = max_positions
        self._slippage_bps = slippage_bps
        self._planned_metadata = planned_metadata_by_isin or {}

    async def run(self, input: SelectionResult) -> PortfolioProposal:
        if self._account_snapshot is not None and self._sizing_method == "equal":
            return self._run_equal_from_account(input)

        if not input.selected and not self._roll_replacements:
            close = list(self._holding_positions.values())
            return PortfolioProposal(
                positions=[],
                target_weights={},
                close_positions=close,
                account_snapshot=self._account_snapshot,
            )

        weights = self._compute_weights(input)
        positions: list[PlannedPosition] = []
        target_weights: dict[str, float] = {}
        new_positions: list[PlannedPosition] = []
        existing_positions: list[PlannedPosition] = []
        roll_trades = self._build_roll_trades()

        selected_isins = {t.isin for t in input.selected if t.isin}

        for ticker in input.selected:
            symbol = ticker.symbol
            weight = weights.get(symbol, 0.0)
            if weight <= 0:
                continue
            capital_allocated = self._capital * weight
            position = PlannedPosition(
                ticker=ticker,
                notional_eur=Decimal(str(round(capital_allocated, 2))),
                target_weight=weight,
            )
            positions.append(position)
            target_weights[symbol] = weight
            if ticker.isin in self._holdings:
                existing_positions.append(position)
            else:
                new_positions.append(position)

        close_positions = [
            p for isin, p in self._holding_positions.items()
            if isin not in selected_isins
            and isin not in self._kept_isins
            and isin not in self._roll_incumbent_isins.values()
        ]

        logger.info(
            "Portfolio constructed: %d positions (%d new, %d existing, %d rolls, %d to close)",
            len(positions), len(new_positions), len(existing_positions), len(roll_trades), len(close_positions),
        )
        return PortfolioProposal(
            positions=positions,
            target_weights=target_weights,
            new_positions=new_positions,
            existing_positions=existing_positions,
            close_positions=close_positions,
            roll_trades=roll_trades,
            account_snapshot=self._account_snapshot,
        )

    def _run_equal_from_account(self, input: SelectionResult) -> PortfolioProposal:
        snapshot = self._account_snapshot
        assert snapshot is not None
        close_positions = [
            position for isin, position in self._holding_positions.items()
            if isin not in {ticker.isin for ticker in input.selected if ticker.isin}
            and isin not in self._kept_isins
            and isin not in self._roll_incumbent_isins.values()
        ]
        valid_rolls = [
            replacement for replacement in self._roll_replacements
            if self._roll_incumbent_isins.get(replacement.underlying.symbol) in self._holding_positions
        ]
        new_tickers = [
            ticker for ticker in input.selected
            if ticker.isin not in self._holdings
        ]
        free_slots = max(
            0,
            self._max_positions - len(self._holding_positions) + len(close_positions),
        )
        new_tickers = new_tickers[:free_slots]
        buy_count = len(new_tickers) + len(valid_rolls)
        sell_positions = close_positions + [
            self._holding_positions[self._roll_incumbent_isins[item.underlying.symbol]]
            for item in valid_rolls
        ]
        sell_isins = {position.ticker.isin for position in sell_positions if position.ticker.isin}
        values_by_isin = {
            holding.position.ticker.isin: holding.market_value_eur
            for holding in snapshot.holdings
            if holding.position.ticker.isin
        }
        gross_sell_proceeds = sum(
            (values_by_isin.get(isin) or Decimal("0") for isin in sell_isins),
            start=Decimal("0"),
        )

        standard_buy_amount: Decimal | None = None
        expected_net_sell_proceeds: Decimal | None = None
        cost_reserve: Decimal | None = None
        blocked_reason: str | None = None

        if buy_count:
            if snapshot.valuation_errors or snapshot.nav_eur is None or snapshot.available_cash_eur is None:
                blocked_reason = "Account valuation is incomplete; risk-increasing orders are blocked"
            elif self._slippage_bps is None:
                blocked_reason = "Configure slippage basis points before sizing BUYs"
            elif self._max_positions <= 0:
                blocked_reason = "max_positions must be positive before sizing BUYs"
            else:
                holdings_by_isin = {
                    holding.position.ticker.isin: holding
                    for holding in snapshot.holdings
                    if holding.position.ticker.isin
                }
                sell_holdings = [holdings_by_isin[isin] for isin in sell_isins if isin in holdings_by_isin]
                sell_fees = sum((
                    calculate_order_fee_eur(
                        holding.market_value_eur or Decimal("0"),
                        issuer_action=holding.issuer_action,
                        issuer_no_fee_action=holding.issuer_no_fee_action,
                    )
                    for holding in sell_holdings
                ), start=Decimal("0"))
                sell_slippage = sum((
                    calculate_slippage_eur(holding.market_value_eur or Decimal("0"), self._slippage_bps)
                    or Decimal("0")
                    for holding in sell_holdings
                ), start=Decimal("0"))
                expected_net_sell_proceeds = gross_sell_proceeds - sell_fees - sell_slippage
                buy_metadata = [
                    self._planned_metadata.get(ticker.isin or "", {})
                    for ticker in new_tickers
                ] + [
                    self._planned_metadata.get(replacement.warrant_isin, {})
                    for replacement in valid_rolls
                ]
                standard_buy_amount, buy_cost_reserve = calculate_equal_buy_amount_eur(
                    snapshot.available_cash_eur + expected_net_sell_proceeds,
                    self._max_positions,
                    [
                        (
                            bool(metadata.get("issuer_action", False)),
                            bool(metadata.get("issuer_no_fee_action", False)),
                        )
                        for metadata in buy_metadata
                    ],
                    self._slippage_bps,
                )
                cost_reserve = sell_fees + sell_slippage + (buy_cost_reserve or Decimal("0"))
                if standard_buy_amount is None or standard_buy_amount <= 0:
                    standard_buy_amount = None
                    blocked_reason = "Available cash and expected net sale proceeds do not cover costs"

        positions: list[PlannedPosition] = []
        target_weights: dict[str, float] = {}
        new_positions: list[PlannedPosition] = []
        existing_positions: list[PlannedPosition] = []
        roll_trades: list[RollTrade] = []

        if standard_buy_amount is not None:
            for ticker in new_tickers:
                metadata = self._planned_metadata.get(ticker.isin or "", {})
                position = PlannedPosition(
                    ticker=ticker,
                    notional_eur=standard_buy_amount,
                    target_weight=float(standard_buy_amount / snapshot.nav_eur),
                    underlying_isin=metadata.get("underlying_isin"),
                    underlying_symbol=metadata.get("underlying_symbol"),
                    sector=metadata.get("sector"),
                    issuer_action=bool(metadata.get("issuer_action", False)),
                    issuer_no_fee_action=bool(metadata.get("issuer_no_fee_action", False)),
                )
                positions.append(position)
                new_positions.append(position)
                target_weights[ticker.symbol] = position.target_weight

            roll_trades = self._build_roll_trades(
                replacement_notional_eur=standard_buy_amount,
                nav_eur=snapshot.nav_eur,
            )

        logger.info(
            "Portfolio constructed from account snapshot: %d buys, %d rolls, %d to close",
            len(new_positions), len(roll_trades), len(close_positions),
        )
        return PortfolioProposal(
            positions=positions,
            target_weights=target_weights,
            new_positions=new_positions,
            existing_positions=existing_positions,
            close_positions=close_positions,
            roll_trades=roll_trades,
            account_snapshot=snapshot,
            standard_buy_amount_eur=standard_buy_amount,
            expected_net_sell_proceeds_eur=expected_net_sell_proceeds,
            cost_reserve_eur=cost_reserve,
            sizing_blocked_reason=blocked_reason,
        )

    def _build_roll_trades(
        self,
        replacement_notional_eur: Decimal | None = None,
        nav_eur: Decimal | None = None,
    ) -> list[RollTrade]:
        rolls: list[RollTrade] = []
        for replacement in self._roll_replacements:
            incumbent_isin = self._roll_incumbent_isins.get(replacement.underlying.symbol)
            incumbent = self._holding_positions.get(incumbent_isin)
            if incumbent is None:
                logger.warning(
                    "Skipping roll into %s: incumbent holding not found",
                    replacement.warrant_wkn,
                )
                continue

            allocated_eur = (
                replacement_notional_eur
                if replacement_notional_eur is not None
                else incumbent.quantity * incumbent.avg_cost
            )
            if allocated_eur <= 0:
                logger.warning(
                    "Skipping roll into %s: incumbent %s has no positive cost basis",
                    replacement.warrant_wkn,
                    incumbent.ticker.symbol,
                )
                continue

            metadata = self._planned_metadata.get(replacement.warrant_isin, {})
            rolls.append(RollTrade(
                incumbent=incumbent,
                replacement=PlannedPosition(
                    ticker=Ticker(
                        symbol=replacement.warrant_wkn or replacement.warrant_isin,
                        isin=replacement.warrant_isin,
                        name=replacement.underlying.name,
                    ),
                    notional_eur=allocated_eur,
                    target_weight=float(
                        allocated_eur / nav_eur
                        if nav_eur
                        else allocated_eur / Decimal(str(self._capital))
                    ),
                    underlying_isin=metadata.get("underlying_isin") or replacement.underlying.isin,
                    underlying_symbol=metadata.get("underlying_symbol") or replacement.underlying.symbol,
                    sector=metadata.get("sector"),
                    issuer_action=bool(metadata.get("issuer_action", replacement.issuer_action)),
                    issuer_no_fee_action=bool(
                        metadata.get("issuer_no_fee_action", replacement.issuer_no_fee_action)
                    ),
                ),
                target_weight=float(
                    allocated_eur / nav_eur
                    if nav_eur
                    else allocated_eur / Decimal(str(self._capital))
                ),
            ))
        return rolls

    def _compute_weights(self, input: SelectionResult) -> dict[str, float]:
        n = len(input.selected)
        if self._sizing_method == "equal" or not input.scores:
            raw = {t.symbol: 1.0 / n for t in input.selected}
        else:
            total_score = sum(input.scores.get(t.symbol, 0.0) for t in input.selected)
            if total_score == 0:
                raw = {t.symbol: 1.0 / n for t in input.selected}
            else:
                raw = {
                    t.symbol: input.scores.get(t.symbol, 0.0) / total_score
                    for t in input.selected
                }

        total = sum(raw.values())
        return {k: v / total for k, v in raw.items()} if total > 0 else raw
