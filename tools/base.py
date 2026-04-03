from abc import ABC, abstractmethod


class Tool(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def __aenter__(self) -> "Tool":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
