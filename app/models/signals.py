from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.models.market import OHLCV, Order, Position, Ticker


class MarketRegime(BaseModel):
    symbol: str
    display_name: str = ""  # human-readable index name, e.g. "NASDAQ 100"
    tq60: float
    tq20: float
    status: Literal["green", "yellow", "red"]
    breadth_score: float | None = None
    breadth_components: dict[str, float] = {}

    @property
    def advisory_action(self) -> str:
        if self.status == "green":
            return "normal review"
        if self.status == "yellow":
            return "stricter timing review; no automatic early entries"
        return "no new entries recommended; review existing positions"


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
    adr_isins: list[str] = []   # ISINs flagged as ADRs (warrant availability is checked only for these)


class ResearchResult(BaseModel):
    tickers: list[Ticker]
    bars: dict[str, list[OHLCV]]
    fundamentals: dict[str, dict]
    benchmark_symbol: str = ""
    benchmark_bars: list[OHLCV] = []
    market_regime: MarketRegime | None = None


class SelectionResult(BaseModel):
    selected: list[Ticker]
    all_tickers: list[Ticker] = []
    scores: dict[str, float]
    rationale: dict[str, str]
    tq_short: dict[str, float] = {}
    tsi: dict[str, float] = {}
    extension_scores: dict[str, float] = {}  # sym → (close - EMA20) / ATR20
    weekly_confirmed: dict[str, bool] = {}  # sym → completed-week close above rising EMA20
    policy_results: dict[str, dict[str, bool]] = {}
    rank_changes: dict[str, list[int | None]] = {}  # sym → [delta_1w, delta_2w]
    history_labels: list[str] = []
    trend_signals: dict[str, str | None] = {}  # sym → "NEW" | "HOLD" | "BREAK" | None
    last_break_age_bars: dict[str, int] = {}  # sym → bars since most recent BREAK event
    latest_candle_dates: dict[str, date] = {}
    previous_candle_dates: dict[str, date] = {}
    market_regime: MarketRegime | None = None


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
    issuer_action: bool = False
    issuer_no_fee_action: bool = False
    chart_symbol: str | None = None   # yfinance symbol matching the strike currency (override underlying)


class WarrantSelectionResult(BaseModel):
    selected: list[SelectedWarrant]
    skipped: list[str]
    skipped_reasons: dict[str, str] = Field(default_factory=dict)            # symbol → skip reason
    skipped_names: dict[str, str] = Field(default_factory=dict)              # symbol → underlying display name
    top3: dict[str, list[SelectedWarrant]] = Field(default_factory=dict)       # symbol → up to 3 warrants by score
    analyzed_count: dict[str, int] = Field(default_factory=dict)               # symbol → total candidates evaluated
    # Metadata for monitoring integration
    sell_existing_isins: list[str] = Field(default_factory=list)               # incumbent ISINs recommended for SELL (no better replacement)
    roll_underlyings: list[str] = Field(default_factory=list)                  # symbols where valid replacement found
    roll_sell_underlyings: list[str] = Field(default_factory=list)             # symbols recommended for SELL (no better replacement)
    roll_selected: list["SelectedWarrant"] = Field(default_factory=list)       # chosen replacement warrants (rolls)
    roll_incumbents: dict[str, "RollReplacement"] = Field(default_factory=dict) # symbol → incumbent snapshot (re-scored)


class RollReplacement(BaseModel):
    warrant_isin: str
    warrant_wkn: str
    strike: float | None = None
    maturity_date: date | None = None
    spread_pct: float | None = None
    leverage: float | None = None
    delta: float | None = None
    score: float | None = None
    rationale: str | None = None


class RollCandidate(BaseModel):
    """A held position classified for ROLL, passed to warrant selection for replacement search."""
    underlying: Ticker
    warrant_isin: str
    warrant_wkn: str
    spread_pct: float | None = None
    leverage: float | None = None
    delta: float | None = None
    days_to_maturity: int | None = None
    strike: float | None = None
    maturity_date: date | None = None


class PositionReview(BaseModel):
    underlying_symbol: str
    underlying_name: str | None = None
    warrant_isin: str
    warrant_wkn: str
    quantity: Decimal | None = None
    held_since: date | None = None
    buy_price: float | None = None
    current_price: float | None = None
    bid_price: float | None = None
    quote_currency: str | None = None
    quote_timestamp_utc: datetime | None = None
    performance_pct: float | None = None
    pct_change_from_prev_close: float | None = None
    # Health snapshot (from current warrant, if available)
    spread_pct: float | None = None
    leverage: float | None = None
    delta: float | None = None
    days_to_maturity: int | None = None
    strike: float | None = None
    maturity_date: date | None = None
    monitoring_score: float | None = None  # 0–1 health score
    screening_signal: str | None = None
    screening_signal_present: bool | None = None
    trend_status: str | None = None         # derived UI status (NEW/HOLD/BREAK pending/confirmed/...)
    trend_status_detail: str | None = None  # detailed tooltip text (all active BREAK reasons)
    warrant_health_status: str | None = None  # healthy/degraded/unknown
    warrant_health_reason: str | None = None  # degradation detail, if any
    # Decision info
    sell_reason: Literal["exit_signal", "warrant_degraded"] | None = None  # None = keep
    decision_reason: str | None = None  # human-readable reason
    roll_replacement: RollReplacement | None = None


class MonitoringResult(BaseModel):
    positions_to_sell: list[PositionReview]
    positions_to_keep: list[PositionReview]
    positions_to_roll: list[PositionReview] = Field(default_factory=list)
    entry_candidates: list[Ticker]   # filtered and capped to free_positions
    free_positions: int
    excluded_symbols: list[str]      # already held (kept or selling) → blocked from entry
    nav_eur: Decimal | None = None
    available_cash_eur: Decimal | None = None
    valuation_errors: list[str] = Field(default_factory=list)
    # Metadata for warrant selection integration
    keep_existing_isins: list[str] = Field(default_factory=list)  # ISINs where replacement was worse
    roll_underlyings: list[str] = Field(default_factory=list)  # symbols with valid replacement
    roll_keep_underlyings: list[str] = Field(default_factory=list)  # symbols downgraded to KEEP


class PlannedPosition(BaseModel):
    ticker: Ticker
    notional_eur: Decimal
    target_weight: float
    buy_price_eur: Decimal | None = None
    reason: str | None = None
    underlying_isin: str | None = None
    underlying_symbol: str | None = None
    sector: str | None = None
    issuer_action: bool = False
    issuer_no_fee_action: bool = False


class RollTrade(BaseModel):
    """Paired replacement of a held warrant with a newly selected warrant."""

    incumbent: Position
    replacement: PlannedPosition
    target_weight: float


class PortfolioHoldingValue(BaseModel):
    position: Position
    underlying_isin: str | None = None
    underlying_symbol: str | None = None
    underlying_name: str | None = None
    sector: str | None = None
    bid_price_eur: Decimal | None = None
    market_value_eur: Decimal | None = None
    quote_timestamp_utc: datetime | None = None
    issuer_action: bool = False
    issuer_no_fee_action: bool = False
    quote_error: str | None = None


class PortfolioAccountSnapshot(BaseModel):
    source: Literal["real", "virtual"]
    available_cash_eur: Decimal | None = None
    holdings: list[PortfolioHoldingValue] = Field(default_factory=list)
    nav_eur: Decimal | None = None
    recorded_at_utc: datetime | None = None
    valuation_errors: list[str] = Field(default_factory=list)


class PortfolioProposal(BaseModel):
    positions: list[PlannedPosition]    # target warrant notionals in EUR
    target_weights: dict[str, float]
    new_positions: list[PlannedPosition] = []  # not currently held → buy
    existing_positions: list[PlannedPosition] = []  # already held → no trade needed
    close_positions: list[Position] = []     # held but not on shortlist → sell
    roll_trades: list[RollTrade] = []
    account_snapshot: PortfolioAccountSnapshot | None = None
    standard_buy_amount_eur: Decimal | None = None
    expected_net_sell_proceeds_eur: Decimal | None = None
    cost_reserve_eur: Decimal | None = None
    sizing_blocked_reason: str | None = None


class RiskAssessment(BaseModel):
    approved_positions: list[PlannedPosition]
    rejected_positions: list[PlannedPosition]
    risk_notes: dict[str, str]
    close_positions: list[Position] = Field(default_factory=list)
    approved_roll_trades: list[RollTrade] = Field(default_factory=list)
    rejected_roll_trades: list[RollTrade] = Field(default_factory=list)
    portfolio_warnings: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    orders: list[Order]
    skipped: list[PlannedPosition]
