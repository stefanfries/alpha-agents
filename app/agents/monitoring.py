import logging
from datetime import date
from typing import Literal

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
    break_confirmed_symbols: set[str] = Field(default_factory=set)
    max_positions: int = 15


class MonitoringAgent(Agent[MonitoringInput, MonitoringResult]):
    name = "monitoring"

    def __init__(self, settings: MonitoringSettings, max_positions: int = 15) -> None:
        self._min_holding_days = settings.min_holding_days
        self._warrant_health = settings.warrant_health
        self._max_positions = max_positions

    def _check_warrant_health(
        self,
        warrant_isin: str,
        snapshot: WarrantSnapshot,
    ) -> tuple[bool, str | None]:
        """Evaluate held warrant against health thresholds."""
        if not self._warrant_health.enabled:
            return False, None

        reasons: list[str] = []

        if snapshot.spread_pct is not None and snapshot.spread_pct > self._warrant_health.spread_max_pct:
            reasons.append(f"spread too wide: {snapshot.spread_pct:.2f}%")

        if snapshot.leverage is not None:
            if snapshot.leverage < self._warrant_health.leverage_min:
                reasons.append(f"leverage too low: {snapshot.leverage:.2f}")
            elif snapshot.leverage > self._warrant_health.leverage_max:
                reasons.append(f"leverage too high: {snapshot.leverage:.2f}")

        if (
            snapshot.days_to_maturity is not None
            and snapshot.days_to_maturity < self._warrant_health.min_days_to_maturity
        ):
            reasons.append(f"maturity too short: {snapshot.days_to_maturity} days")

        if snapshot.delta is not None:
            if snapshot.delta < self._warrant_health.delta_min:
                reasons.append(f"delta too low: {snapshot.delta:.3f}")
            elif snapshot.delta > self._warrant_health.delta_max:
                reasons.append(f"delta too high: {snapshot.delta:.3f}")

        is_degraded = len(reasons) > 0
        detail = " | ".join(reasons) if reasons else None
        if is_degraded:
            logger.info("Monitoring: degraded warrant %s (%s)", warrant_isin, detail)
        return is_degraded, detail

    def _monitoring_score(self, snapshot: WarrantSnapshot | None) -> float | None:
        """Compute a 0..1 health score for held warrants from available metrics."""
        if snapshot is None:
            return None

        components: list[float] = []

        # Spread: lower is better (1.0 at 0%, decay to 0.0 at max spread)
        if snapshot.spread_pct is not None:
            max_spread = max(self._warrant_health.spread_max_pct, 0.001)
            components.append(max(0.0, 1.0 - (snapshot.spread_pct / max_spread)))

        # Leverage: peak at midpoint, decay towards min/max
        if snapshot.leverage is not None:
            lev_min = self._warrant_health.leverage_min
            lev_max = self._warrant_health.leverage_max
            lev_mid = (lev_min + lev_max) / 2.0
            lev_half = max((lev_max - lev_min) / 2.0, 0.001)
            components.append(max(0.0, 1.0 - abs(snapshot.leverage - lev_mid) / lev_half))

        # Maturity: scaled by min threshold (higher better, floors at min days)
        if snapshot.days_to_maturity is not None:
            min_days = max(self._warrant_health.min_days_to_maturity, 1)
            components.append(min(1.0, max(0.0, snapshot.days_to_maturity / min_days)))

        # Delta: peak at midpoint, decay towards min/max
        if snapshot.delta is not None:
            d_min = self._warrant_health.delta_min
            d_max = self._warrant_health.delta_max
            d_mid = (d_min + d_max) / 2.0
            d_half = max((d_max - d_min) / 2.0, 0.001)
            components.append(max(0.0, 1.0 - abs(snapshot.delta - d_mid) / d_half))

        if not components:
            return None
        return round(sum(components) / len(components), 3)

    @staticmethod
    def _trend_status(
        *,
        has_trend_signal: bool,
        trend_signal: str | None,
        is_break_confirmed: bool,
    ) -> str:
        if not has_trend_signal:
            return "no screening signal"
        if trend_signal is None:
            return "BREAK confirmed earlier"
        if trend_signal == "BREAK":
            return "BREAK confirmed" if is_break_confirmed else "BREAK pending"
        if trend_signal in {"NEW", "HOLD"}:
            return trend_signal
        return "no signal"

    @staticmethod
    def _decide_action(
        *,
        has_exit_signal: bool,
        is_break_confirmed: bool,
        is_degraded: bool,
        holding_days: int,
        min_holding_days: int,
    ) -> tuple[Literal["sell", "roll", "keep"], str | None]:
        """Resolve action with trend-first priority, then warrant health.

        Priority order:
        1) Trend check (from screening):
           - Confirmed BREAK -> SELL
           - Unconfirmed BREAK -> KEEP (with reason: "break signal, not confirmed yet")
        2) Warrant health check (only if trend is intact):
           - Degraded + grace met -> ROLL
           - Otherwise -> KEEP
        """
        # Step 1: Check trend status first
        if has_exit_signal:
            if is_break_confirmed:
                return "sell", "trend break confirmed"
            else:
                # Unconfirmed BREAK: hold and wait for confirmation
                return "keep", "break signal, not confirmed yet"
        
        # Step 2: Check warrant health only if trend is intact
        if is_degraded and holding_days >= min_holding_days:
            return "roll", None  # reason will be set from degrade_detail
        
        return "keep", None

    @staticmethod
    def _log_sell_decision(
        *,
        underlying_symbol: str,
        is_degraded: bool,
        holding_days: int,
        trend_signal: str | None,
        is_break_confirmed: bool,
        degrade_detail: str | None,
        warrant_wkn: str,
    ) -> None:
        logger.info(
            "Monitoring: exit for %s (degraded=%s, held=%d days, signal=%s, confirmed=%s, detail=%s) -> SELL %s",
            underlying_symbol,
            is_degraded,
            holding_days,
            trend_signal,
            is_break_confirmed,
            degrade_detail,
            warrant_wkn,
        )

    async def run(self, input: MonitoringInput) -> MonitoringResult:
        today = date.today()
        positions_to_sell: list[PositionReview] = []
        positions_to_keep: list[PositionReview] = []
        positions_to_roll: list[PositionReview] = []
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
                warrant_snapshot = input.warrant_snapshots.get(warrant_isin)
                positions_to_keep.append(PositionReview(
                    underlying_symbol="",
                    underlying_name=None,
                    warrant_isin=warrant_isin,
                    warrant_wkn=warrant_wkn,
                    held_since=input.held_since_map.get(warrant_wkn),
                    spread_pct=warrant_snapshot.spread_pct if warrant_snapshot else None,
                    leverage=warrant_snapshot.leverage if warrant_snapshot else None,
                    delta=warrant_snapshot.delta if warrant_snapshot else None,
                    days_to_maturity=warrant_snapshot.days_to_maturity if warrant_snapshot else None,
                    monitoring_score=self._monitoring_score(warrant_snapshot) if warrant_snapshot else None,
                    decision_reason="underlying mapping unresolved",
                ))
                logger.debug("Monitoring: no underlying mapping for warrant %s — keeping", warrant_isin or warrant_wkn)
                continue

            all_held_underlyings.add(underlying_sym)

            # Check holding period (used as grace period for ROLL recommendations)
            held_since = input.held_since_map.get(warrant_wkn)
            holding_days = (today - held_since).days if held_since else 9999

            # Check exit signal from screening:
            # - BREAK: requires candle confirmation across runs
            # - None: BREAK happened earlier and the signal already aged out
            has_trend_signal = underlying_sym in input.trend_signals
            trend_signal = input.trend_signals.get(underlying_sym)
            if not has_trend_signal:
                logger.warning(
                    "Monitoring: mapped underlying %s has no screening signal entry (warrant=%s)",
                    underlying_sym,
                    warrant_wkn,
                )
            has_exit_signal = trend_signal == "BREAK" or (has_trend_signal and trend_signal is None)
            is_break_confirmed = (has_trend_signal and trend_signal is None) or underlying_sym in input.break_confirmed_symbols
            warrant_snapshot = input.warrant_snapshots.get(warrant_isin)
            is_degraded, degrade_detail = False, None
            if warrant_snapshot:
                is_degraded, degrade_detail = self._check_warrant_health(warrant_isin, warrant_snapshot)
            monitoring_score = self._monitoring_score(warrant_snapshot)

            review = PositionReview(
                underlying_symbol=underlying_sym,
                underlying_name=underlying_name,
                warrant_isin=warrant_isin,
                warrant_wkn=warrant_wkn,
                held_since=held_since,
                spread_pct=warrant_snapshot.spread_pct if warrant_snapshot else None,
                leverage=warrant_snapshot.leverage if warrant_snapshot else None,
                delta=warrant_snapshot.delta if warrant_snapshot else None,
                days_to_maturity=warrant_snapshot.days_to_maturity if warrant_snapshot else None,
                monitoring_score=monitoring_score,
                screening_signal=trend_signal,
                screening_signal_present=has_trend_signal,
                trend_status=self._trend_status(
                    has_trend_signal=has_trend_signal,
                    trend_signal=trend_signal,
                    is_break_confirmed=is_break_confirmed,
                ),
                warrant_health_status=(
                    "unknown"
                    if warrant_snapshot is None
                    else ("degraded" if is_degraded else "healthy")
                ),
                warrant_health_reason=degrade_detail,
            )

            action, decision_reason = self._decide_action(
                has_exit_signal=has_exit_signal,
                is_break_confirmed=is_break_confirmed,
                is_degraded=is_degraded,
                holding_days=holding_days,
                min_holding_days=self._min_holding_days,
            )

            match action:
                case "sell":
                    review.sell_reason = "exit_signal"
                    review.decision_reason = (
                        "break signal, confirmed earlier"
                        if trend_signal is None
                        else decision_reason
                    )
                    positions_to_sell.append(review)
                    self._log_sell_decision(
                        underlying_symbol=underlying_sym,
                        is_degraded=is_degraded,
                        holding_days=holding_days,
                        trend_signal=trend_signal,
                        is_break_confirmed=is_break_confirmed,
                        degrade_detail=degrade_detail,
                        warrant_wkn=warrant_wkn,
                    )
                case "roll":
                    review.decision_reason = degrade_detail or "warrant degraded, replacement available"
                    positions_to_roll.append(review)
                    logger.info(
                        "Monitoring: warrant degraded for %s (detail=%s) -> ROLL %s",
                        underlying_sym,
                        degrade_detail,
                        warrant_wkn,
                    )
                case "keep":
                    if decision_reason:
                        review.decision_reason = decision_reason
                    elif is_degraded:
                        review.decision_reason = "degraded but within grace period"
                    elif trend_signal in {"NEW", "HOLD"}:
                        review.decision_reason = "warrant healthy, trend intact"
                    elif trend_signal == "BREAK":
                        # Defensive fallback: keep this explicit if signal semantics ever change.
                        review.decision_reason = "break signal, not confirmed yet"
                    else:
                        review.decision_reason = "no signal"
                    positions_to_keep.append(review)
                    logger.debug(
                        "Monitoring: keeping %s (held %d days, signal=%s)",
                        underlying_sym,
                        holding_days,
                        trend_signal,
                    )
                case _:
                    raise ValueError(f"Unknown monitoring action: {action}")

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
            "Monitoring complete: %d keep, %d roll, %d sell, %d free slots, %d entry candidates",
            len(positions_to_keep), len(positions_to_roll), len(positions_to_sell), free_positions, len(entry_candidates),
        )
        return MonitoringResult(
            positions_to_sell=positions_to_sell,
            positions_to_keep=positions_to_keep,
            positions_to_roll=positions_to_roll,
            entry_candidates=entry_candidates,
            free_positions=free_positions,
            excluded_symbols=excluded_symbols,
        )
