import logging
from decimal import Decimal

from agents.base import Agent
from models.market import Order, Position
from models.signals import ExecutionPlan, RiskAssessment

logger = logging.getLogger(__name__)


class TradeExecutionAgent(Agent[RiskAssessment, ExecutionPlan]):
    name = "execution"

    def __init__(
        self,
        dry_run: bool = True,
        min_trade_eur: float = 100.0,
        order_type: str = "limit",
    ) -> None:
        self._dry_run = dry_run
        self._min_trade_eur = min_trade_eur
        self._order_type = order_type

    async def run(self, input: RiskAssessment) -> ExecutionPlan:
        orders: list[Order] = []
        skipped: list[Position] = []

        for position in input.approved_positions:
            allocated_eur = float(position.quantity)
            if allocated_eur < self._min_trade_eur:
                skipped.append(position)
                logger.debug(
                    "Skipping %s: allocated %.2f EUR below minimum %.2f",
                    position.ticker.symbol,
                    allocated_eur,
                    self._min_trade_eur,
                )
                continue

            order = Order(
                ticker=position.ticker,
                side="buy",
                quantity=Decimal(str(round(allocated_eur, 2))),
                order_type=self._order_type,  # type: ignore[arg-type]
                limit_price=None,
            )
            orders.append(order)

        if self._dry_run:
            logger.info("[DRY RUN] Would submit %d orders (not sent to broker)", len(orders))
        else:
            logger.info("Submitting %d orders to broker", len(orders))

        return ExecutionPlan(orders=orders, skipped=skipped)
