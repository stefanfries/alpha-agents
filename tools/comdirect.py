from models.market import Order
from tools.base import Tool


class ComdirectTool(Tool):
    """Stub — wire up via the comdirect_api sibling project."""

    async def connect(self) -> None:
        raise NotImplementedError("ComdirectTool is not yet implemented")

    async def close(self) -> None:
        pass

    async def submit_order(self, order: Order) -> str:
        raise NotImplementedError("ComdirectTool is not yet implemented")
