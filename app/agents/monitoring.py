import logging
from datetime import date

from pydantic import BaseModel, Field

from app.agents.base import Agent
from app.config import MonitoringSettings
from app.models.market import Position, Ticker
from app.models.signals import MonitoringResult, PositionReview

logger = logging.getLogger(__name__)


class WarrantSnapshot(BaseModel):
    """Current warrant quote snapshot used by health checks."""

    warrant_isin: str
    spread_pct: float | None = None
    leverage: float | None = None
    days_to_maturity: int | None = None
    delta: float | None = None
    bid_ask_midprice: float | None = None


class MonitoringInput(BaseModel):
    candidates: list[Ticker]                   # from SelectionResult.selected (top-N ranked)
    scores: dict[str, float]                   # underlying_symbol → score
    trend_signals: dict[str, str | None]       # underlying_symbol → "NEW"|"HOLD"|"BREAK"|None
    underlying_names: dict[str, str] = {}      # underlying_symbol → display name
    current_holdings: list[Position]           # depot warrant positions (isin+wkn in ticker)
    warrant_underlying_map: dict[str, str]     # warrant_isin → underlying_symbol
    held_since_map: dict[str, date]            # warrant_wkn → most recent BUY date
    warrant_snapshots: dict[str, WarrantSnapshot] = Field(default_factory=dict)
    max_positions: int = 15


class MonitoringAgent(Agent[MonitoringInput, MonitoringResult]):
    name = "monitoring"

    def __init__(self, settings: MonitoringSettings, max_positions: int = 15) -> None:
        self._min_holding_days = settings.min_holding_days
        self._max_positions = max_positions

    async def run(self, input: MonitoringInput) -> MonitoringResult:
        today = date.today()
        positions_to_sell: list[PositionReview] = []
        positions_to_keep: list[PositionReview] = []
        # Track all held underlyings (sell + keep) to block from entry
        all_held_underlyings: set[str] = set()

        for pos in input.current_holdings:
            warrant_isin = pos.ticker.isin or ""
            warrant_wkn = pos.ticker.symbol or ""

            # Resolve underlying symbol from mapping cache (ISIN first, WKN fallback).
            underlying_sym = input.warrant_underlying_map.get(warrant_isin) or input.warrant_underlying_map.get(warrant_wkn)
            underlying_name = input.underlying_names.get(underlying_sym) if underlying_sym else None
            if underlying_sym is None:
                # Can't map to underlying — keep as-is (safe default)
                positions_to_keep.append(PositionReview(
                    underlying_symbol="",
                    underlying_name=None,
                    warrant_isin=warrant_isin,
                    warrant_wkn=warrant_wkn,
                    held_since=input.held_since_map.get(warrant_wkn),
                    sell_reason=None,
                ))
                logger.debug("Monitoring: no underlying mapping for warrant %s — keeping", warrant_isin or warrant_wkn)
                continue

            all_held_underlyings.add(underlying_sym)

            # Check holding period (grace period applies only to exit signals)
            held_since = input.held_since_map.get(warrant_wkn)
            holding_days = (today - held_since).days if held_since else 9999

            # Check exit signal: ANY break criterion already evaluated by screening
            trend_signal = input.trend_signals.get(underlying_sym)
            has_exit_signal = trend_signal == "BREAK"

            review = PositionReview(
                underlying_symbol=underlying_sym,
                underlying_name=underlying_name,
                warrant_isin=warrant_isin,
                warrant_wkn=warrant_wkn,
                held_since=held_since,
            )

            if has_exit_signal and holding_days >= self._min_holding_days:
                review.sell_reason = "exit_signal"
                positions_to_sell.append(review)
                logger.info(
                    "Monitoring: exit signal for %s (held %d days, signal=%s) → SELL %s",
                    underlying_sym, holding_days, trend_signal, warrant_wkn,
                )
            else:
                positions_to_keep.append(review)
                logger.debug(
                    "Monitoring: keeping %s (held %d days, signal=%s)",
                    underlying_sym, holding_days, trend_signal,
                )

        # Free slots: capital is NOT recycled within the same run (deferred approach)
        # Positions being sold don't free up slots until the next run
        n_held = len(input.current_holdings)
        free_positions = max(0, self._max_positions - n_held)

        # Entry candidates: screening candidates not already held (kept or being sold)
        excluded_symbols = sorted(all_held_underlyings)
        entry_candidates = [
            t for t in input.candidates
            if t.symbol not in all_held_underlyings
        ][:free_positions]

        logger.info(
            "Monitoring complete: %d keep, %d sell, %d free slots, %d entry candidates",
            len(positions_to_keep), len(positions_to_sell), free_positions, len(entry_candidates),
        )
        return MonitoringResult(
            positions_to_sell=positions_to_sell,
            positions_to_keep=positions_to_keep,
            entry_candidates=entry_candidates,
            free_positions=free_positions,
            excluded_symbols=excluded_symbols,
        )
