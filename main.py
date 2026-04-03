import asyncio
import logging

from orchestrator import Pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "BRK-B", "JPM", "JNJ"]


async def main() -> None:
    pipeline = Pipeline()
    plan = await pipeline.run(DEFAULT_UNIVERSE)

    print(f"\n{'='*50}")
    print(f"Execution Plan ({len(plan.orders)} orders)")
    print("="*50)
    for order in plan.orders:
        print(f"  {order.side.upper():4s}  {order.ticker.symbol:<10s}  EUR {order.quantity}")
    if plan.skipped:
        print(f"\nSkipped ({len(plan.skipped)}): {[p.ticker.symbol for p in plan.skipped]}")


if __name__ == "__main__":
    asyncio.run(main())
