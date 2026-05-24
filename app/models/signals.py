from datetime import date

from pydantic import BaseModel

from app.models.market import OHLCV, Order, Position, Ticker


class UniverseResult(BaseModel):
    tickers: list[Ticker]
    source: dict[str, str]      # ISIN (or symbol) → originating index name
    missing_isin: list[str]     # symbols for which no ISIN was resolved
    unresolved_indices: list[str]


class ResearchResult(BaseModel):
    tickers: list[Ticker]
    bars: dict[str, list[OHLCV]]
    fundamentals: dict[str, dict]


class SelectionResult(BaseModel):
    selected: list[Ticker]
    scores: dict[str, float]
    rationale: dict[str, str]


class SelectedWarrant(BaseModel):
    underlying: Ticker
    warrant_isin: str
    warrant_wkn: str
    strike: float | None = None
    maturity_date: date | None = None
    spread_pct: float | None = None
    leverage: float | None = None
    delta: float | None = None
    bid: float | None = None
    ask: float | None = None
    score: float
    rationale: str


class WarrantSelectionResult(BaseModel):
    selected: list[SelectedWarrant]
    skipped: list[str]


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
