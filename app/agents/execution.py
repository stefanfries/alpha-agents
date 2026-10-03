import logging
from decimal import Decimal

from app.agents.base import Agent
from app.models.market import Order
from app.models.signals import ExecutionPlan, PlannedPosition, RiskAssessment

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
        sell_orders: list[Order] = []
        buy_orders: list[Order] = []
        skipped: list[PlannedPosition] = []

        for position in input.close_positions:
            sell_orders.append(Order(
                ticker=position.ticker,
                side="sell",
                quantity=position.quantity,
                order_type=self._order_type,  # type: ignore[arg-type]
                limit_price=None,
            ))

        for roll in input.approved_roll_trades:
            allocated_eur = float(roll.replacement.notional_eur)
            if allocated_eur < self._min_trade_eur:
                skipped.append(roll.replacement)
                logger.debug(
                    "Skipping roll into %s: allocated %.2f EUR below minimum %.2f",
                    roll.replacement.ticker.symbol,
                    allocated_eur,
                    self._min_trade_eur,
                )
                continue

            sell_orders.append(Order(
                ticker=roll.incumbent.ticker,
                side="sell",
                quantity=roll.incumbent.quantity,
                order_type=self._order_type,  # type: ignore[arg-type]
                limit_price=None,
            ))
            buy_orders.append(Order(
                ticker=roll.replacement.ticker,
                side="buy",
                notional_eur=Decimal(str(round(allocated_eur, 2))),
                order_type=self._order_type,  # type: ignore[arg-type]
                limit_price=None,
            ))

        for position in input.approved_positions:
            allocated_eur = float(position.notional_eur)
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
                notional_eur=Decimal(str(round(allocated_eur, 2))),
                order_type=self._order_type,  # type: ignore[arg-type]
                limit_price=None,
            )
            buy_orders.append(order)

        orders = sell_orders + buy_orders

        if self._dry_run:
            logger.info("[DRY RUN] Would submit %d orders (not sent to broker)", len(orders))
        else:
            logger.info("Submitting %d orders to broker", len(orders))

        return ExecutionPlan(orders=orders, skipped=skipped)
