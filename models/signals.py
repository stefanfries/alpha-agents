from pydantic import BaseModel

from models.market import OHLCV, Order, Position, Ticker


class ResearchResult(BaseModel):
    tickers: list[Ticker]
    bars: dict[str, list[OHLCV]]
    fundamentals: dict[str, dict]


class SelectionResult(BaseModel):
    selected: list[Ticker]
    scores: dict[str, float]
    rationale: dict[str, str]


class PortfolioProposal(BaseModel):
    positions: list[Position]
    target_weights: dict[str, float]


class RiskAssessment(BaseModel):
    approved_positions: list[Position]
    rejected_positions: list[Position]
    risk_notes: dict[str, str]


class ExecutionPlan(BaseModel):
    orders: list[Order]
    skipped: list[Position]
