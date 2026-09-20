import logging

from app.agents.base import Agent
from app.models.market import Position
from app.models.signals import PortfolioProposal, RiskAssessment, RollTrade

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
        approved_rolls: list[RollTrade] = []
        rejected_rolls: list[RollTrade] = []
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

        for roll in input.roll_trades:
            symbol = roll.replacement.ticker.symbol
            if roll.target_weight > self._max_position_weight:
                rejected_rolls.append(roll)
                notes[symbol] = (
                    f"Weight {roll.target_weight:.1%} exceeds max {self._max_position_weight:.1%}"
                )
                continue
            approved_rolls.append(roll)

        logger.info(
            "Risk check: %d positions and %d rolls approved, %d positions and %d rolls rejected",
            len(approved),
            len(approved_rolls),
            len(rejected),
            len(rejected_rolls),
        )
        return RiskAssessment(
            approved_positions=approved,
            rejected_positions=rejected,
            risk_notes=notes,
            close_positions=input.close_positions,
            approved_roll_trades=approved_rolls,
            rejected_roll_trades=rejected_rolls,
        )
