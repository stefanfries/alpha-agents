from datetime import date
from enum import Enum

from pydantic import BaseModel

from app.models.market import OHLCV, Order, Position, Ticker


class TrendStatus(str, Enum):
    ESTABLISHED_UP   = "established_up"    # Gate 1 bullish + Gate 2 confirmed
    STARTING_UP      = "starting_up"       # Gate 1 bullish, Gate 2 not yet confirmed
    SIDEWAYS         = "sideways"          # No directional Gate 1 majority
    STARTING_DOWN    = "starting_down"     # Gate 1 bearish, Gate 2 not yet confirmed
    ESTABLISHED_DOWN = "established_down"  # Gate 1 bearish + Gate 2 confirmed


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
    all_tickers: list[Ticker] = []
    scores: dict[str, float]
    rationale: dict[str, str]
    tq_short: dict[str, float] = {}
    tsi: dict[str, float] = {}
    policy_results: dict[str, dict[str, bool]] = {}
    rank_changes: dict[str, list[int | None]] = {}  # sym → [delta_1w, delta_2w]
    history_labels: list[str] = []


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
    top3: dict[str, list[SelectedWarrant]] = {}       # symbol → up to 3 warrants by score
    analyzed_count: dict[str, int] = {}               # symbol → total candidates evaluated


class PortfolioProposal(BaseModel):
    positions: list[Position]           # all target positions
    target_weights: dict[str, float]
    new_positions: list[Position] = []       # not currently held → buy
    existing_positions: list[Position] = []  # already held → no trade needed
    close_positions: list[Position] = []     # held but not on shortlist → sell


class RiskAssessment(BaseModel):
    approved_positions: list[Position]
    rejected_positions: list[Position]
    risk_notes: dict[str, str]


class ExecutionPlan(BaseModel):
    orders: list[Order]
    skipped: list[Position]
