import logging
from decimal import Decimal

from app.agents.base import Agent
from app.models.market import Position, Ticker
from app.models.signals import PortfolioProposal, RollTrade, SelectedWarrant, SelectionResult

logger = logging.getLogger(__name__)


class PortfolioConstructionAgent(Agent[SelectionResult, PortfolioProposal]):
    name = "portfolio"

    def __init__(
        self,
        capital_eur: float,
        current_holdings: list[Position] | None = None,
        sizing_method: str = "equal",
        max_position_weight: float = 0.10,
        kept_warrant_isins: set[str] | None = None,
        roll_replacements: list[SelectedWarrant] | None = None,
        roll_incumbent_isins: dict[str, str] | None = None,
    ) -> None:
        self._capital = capital_eur
        self._holdings = {p.ticker.isin for p in (current_holdings or []) if p.ticker.isin}
        self._holding_positions = {p.ticker.isin: p for p in (current_holdings or []) if p.ticker.isin}
        self._sizing_method = sizing_method
        self._max_weight = max_position_weight
        # Warrant ISINs that monitoring determined should be kept — excluded from close_positions
        self._kept_isins: set[str] = kept_warrant_isins or set()
        self._roll_replacements = roll_replacements or []
        self._roll_incumbent_isins = roll_incumbent_isins or {}

    async def run(self, input: SelectionResult) -> PortfolioProposal:
        if not input.selected and not self._roll_replacements:
            close = list(self._holding_positions.values())
            return PortfolioProposal(positions=[], target_weights={}, close_positions=close)

        weights = self._compute_weights(input)
        positions: list[Position] = []
        target_weights: dict[str, float] = {}
        new_positions: list[Position] = []
        existing_positions: list[Position] = []
        roll_trades = self._build_roll_trades()

        selected_isins = {t.isin for t in input.selected if t.isin}

        for ticker in input.selected:
            symbol = ticker.symbol
            weight = weights.get(symbol, 0.0)
            if weight <= 0:
                continue
            capital_allocated = self._capital * weight
            position = Position(
                ticker=ticker,
                quantity=Decimal(str(round(capital_allocated, 2))),
                avg_cost=Decimal("0"),
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
        )

    def _build_roll_trades(self) -> list[RollTrade]:
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

            allocated_eur = incumbent.quantity * incumbent.avg_cost
            if allocated_eur <= 0:
                logger.warning(
                    "Skipping roll into %s: incumbent %s has no positive cost basis",
                    replacement.warrant_wkn,
                    incumbent.ticker.symbol,
                )
                continue

            rolls.append(RollTrade(
                incumbent=incumbent,
                replacement=Position(
                    ticker=Ticker(
                        symbol=replacement.warrant_wkn or replacement.warrant_isin,
                        isin=replacement.warrant_isin,
                        name=replacement.underlying.name,
                    ),
                    quantity=allocated_eur,
                    avg_cost=Decimal("0"),
                ),
                target_weight=float(allocated_eur / Decimal(str(self._capital))),
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

        capped = {k: min(v, self._max_weight) for k, v in raw.items()}
        total = sum(capped.values())
        return {k: v / total for k, v in capped.items()} if total > 0 else capped
