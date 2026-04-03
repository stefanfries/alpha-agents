import logging

from agents.base import Agent
from models.market import Position
from models.signals import PortfolioProposal, RiskAssessment

logger = logging.getLogger(__name__)


class RiskAgent(Agent[PortfolioProposal, RiskAssessment]):
    name = "risk"

    def __init__(
        self,
        max_position_weight: float = 0.10,
        max_positions: int = 30,
    ) -> None:
        self._max_position_weight = max_position_weight
        self._max_positions = max_positions

    async def run(self, input: PortfolioProposal) -> RiskAssessment:
        approved: list[Position] = []
        rejected: list[Position] = []
        notes: dict[str, str] = {}

        for position in input.positions:
            symbol = position.ticker.symbol
            weight = input.target_weights.get(symbol, 0.0)

            if weight > self._max_position_weight:
                rejected.append(position)
                notes[symbol] = (
                    f"Weight {weight:.1%} exceeds max {self._max_position_weight:.1%}"
                )
                continue

            if len(approved) >= self._max_positions:
                rejected.append(position)
                notes[symbol] = f"Max position count ({self._max_positions}) reached"
                continue

            approved.append(position)

        logger.info(
            "Risk check: %d approved, %d rejected",
            len(approved),
            len(rejected),
        )
        return RiskAssessment(
            approved_positions=approved,
            rejected_positions=rejected,
            risk_notes=notes,
        )
