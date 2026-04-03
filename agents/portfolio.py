import logging
from decimal import Decimal

from agents.base import Agent
from models.market import Position
from models.signals import PortfolioProposal, SelectionResult

logger = logging.getLogger(__name__)


class PortfolioConstructionAgent(Agent[SelectionResult, PortfolioProposal]):
    name = "portfolio"

    def __init__(
        self,
        capital_eur: float,
        sizing_method: str = "equal",
        max_position_weight: float = 0.10,
    ) -> None:
        self._capital = capital_eur
        self._sizing_method = sizing_method
        self._max_weight = max_position_weight

    async def run(self, input: SelectionResult) -> PortfolioProposal:
        if not input.selected:
            return PortfolioProposal(positions=[], target_weights={})

        weights = self._compute_weights(input)
        positions: list[Position] = []
        target_weights: dict[str, float] = {}

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

        logger.info("Portfolio constructed: %d positions", len(positions))
        return PortfolioProposal(positions=positions, target_weights=target_weights)

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
