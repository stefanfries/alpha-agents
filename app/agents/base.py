from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

AgentInputT = TypeVar("AgentInputT", bound=BaseModel)
AgentOutputT = TypeVar("AgentOutputT", bound=BaseModel)


class Agent(ABC, Generic[AgentInputT, AgentOutputT]):
    name: str

    @abstractmethod
    async def run(self, input: AgentInputT) -> AgentOutputT: ...
