import logging

import numpy as np
import talib

from app.agents.base import Agent
from app.config import ScreeningSettings
from app.indicators import supertrend_bullish
from app.models.market import OHLCV
from app.models.signals import ResearchResult, SelectionResult

logger = logging.getLogger(__name__)

_MIN_BARS = 70  # 60-bar regression + ATR/EMA warmup


class SecuritySelectionAgent(Agent[ResearchResult, SelectionResult]):
    name = "screening"

    def __init__(self, settings: ScreeningSettings | None = None) -> None:
        cfg = settings or ScreeningSettings()
        self._top_n = cfg.top_n
        self._min_market_cap_eur = cfg.min_market_cap_eur
        self._min_adx = cfg.min_adx
        self._lookback_regression = cfg.lookback_regression
        self._lookback_regression_short = cfg.lookback_regression_short
        self._supertrend_period = cfg.supertrend_period
        self._supertrend_multiplier = cfg.supertrend_multiplier
        self._tsi_fast = cfg.tsi_fast
        self._tsi_slow = cfg.tsi_slow
        self._policy_supertrend = cfg.policy_supertrend
        self._policy_ema20_rising = cfg.policy_ema20_rising
        self._policy_adx = cfg.policy_adx
        self._policy_price_above_ema50 = cfg.policy_price_above_ema50

    async def run(self, input: ResearchResult) -> SelectionResult:
        scores: dict[str, float] = {}
        tq_short: dict[str, float] = {}
        tsi_vals: dict[str, float] = {}
        rationale: dict[str, str] = {}
        policy_results: dict[str, dict[str, bool]] = {}
        trend_signals: dict[str, str | None] = {}
        candidate_symbols: set[str] = set()

        for ticker in input.tickers:
            symbol = ticker.symbol
            fund = input.fundamentals.get(symbol, {})
            bars = input.bars.get(symbol, [])

            market_cap = fund.get("marketCap", 0)
            if market_cap < self._min_market_cap_eur:
                rationale[symbol] = f"Skipped: market cap {market_cap:,.0f} below minimum"
                scores[symbol] = 0.0
                continue

            if len(bars) < _MIN_BARS:
                rationale[symbol] = f"Skipped: only {len(bars)} bars (minimum {_MIN_BARS})"
                scores[symbol] = 0.0
                continue

            # --- Score ---
            tq = self._trend_quality(bars, self._lookback_regression)
            tqs = self._trend_quality(bars, self._lookback_regression_short)
            tsi = self._tsi(bars)
            scores[symbol] = tq
            tq_short[symbol] = tqs
            tsi_vals[symbol] = tsi

            # --- Policies ---
            policies = self._evaluate_policies(bars)
            policy_results[symbol] = policies

            enabled = {
                "supertrend": self._policy_supertrend,
                "ema20_rising": self._policy_ema20_rising,
                "adx": self._policy_adx,
                "price_above_ema50": self._policy_price_above_ema50,
            }
            passes = all(policies[p] for p, on in enabled.items() if on)

            # Trend signal: compare current policies to 5 bars ago
            if len(bars) > 5 + _MIN_BARS:
                policies_5d = self._evaluate_policies(bars[:-5])
                passes_5d = all(policies_5d[p] for p, on in enabled.items() if on)
            else:
                passes_5d = None  # insufficient history

            if passes and passes_5d is False:
                trend_signals[symbol] = "NEW"
            elif passes and passes_5d is True:
                trend_signals[symbol] = "HOLD"
            elif not passes and passes_5d is True:
                trend_signals[symbol] = "BREAK"
            # else: consistently out → None (no signal)

            policy_detail = " | ".join(
                f"{k}={'✓' if v else '✗'}" for k, v in policies.items()
            )
            rationale[symbol] = f"TQ={tq:.3f} TQ20={tqs:.3f} TSI={tsi:.1f} | {policy_detail}"

            if passes:
                candidate_symbols.add(symbol)

        ranked_symbols = sorted(
            candidate_symbols, key=lambda s: scores.get(s, 0.0), reverse=True
        )[: self._top_n]
        ranked_set = set(ranked_symbols)
        selected = [t for t in input.tickers if t.symbol in ranked_set]

        rank_changes, history_labels = self._rank_changes(input, scores, ranked_set)

        logger.info(
            "Screening complete: %d/%d selected (policies: ST=%s EMA20=%s ADX=%s EMA50=%s)",
            len(selected),
            len(input.tickers),
            self._policy_supertrend,
            self._policy_ema20_rising,
            self._policy_adx,
            self._policy_price_above_ema50,
        )
        return SelectionResult(
            selected=selected,
            all_tickers=input.tickers,
            scores=scores,
            tq_short=tq_short,
            tsi=tsi_vals,
            rationale=rationale,
            policy_results=policy_results,
            rank_changes=rank_changes,
            history_labels=history_labels,
            trend_signals=trend_signals,
        )

    # ------------------------------------------------------------------ #
    # Scoring                                                              #
    # ------------------------------------------------------------------ #

    def _trend_quality(self, bars: list[OHLCV], lookback: int) -> float:
        """R²_lb × (Slope_lb / ATR_20). Returns 0.0 on insufficient data."""
        close = np.array([float(b.close) for b in bars])
        high = np.array([float(b.high) for b in bars])
        low = np.array([float(b.low) for b in bars])

        if len(close) < lookback:
            return 0.0

        atr = talib.ATR(high, low, close, timeperiod=20)
        atr_val = float(atr[-1])
        if np.isnan(atr_val) or atr_val <= 0:
            return 0.0

        segment = close[-lookback:]
        x = np.arange(lookback, dtype=float)
        slope, intercept = np.polyfit(x, segment, 1)
        fitted = slope * x + intercept
        ss_res = float(np.sum((segment - fitted) ** 2))
        ss_tot = float(np.sum((segment - segment.mean()) ** 2))
        r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0

        return r2 * (slope / atr_val)

    def _tsi(self, bars: list[OHLCV]) -> float:
        """True Strength Index on last bar. Returns 0.0 on insufficient data."""
        close = np.array([float(b.close) for b in bars])
        min_len = self._tsi_fast + self._tsi_slow + 2
        if len(close) < min_len:
            return 0.0

        pc = np.diff(close).astype(float)
        ds_pc = talib.EMA(talib.EMA(pc, timeperiod=self._tsi_fast), timeperiod=self._tsi_slow)
        ds_abs = talib.EMA(talib.EMA(np.abs(pc), timeperiod=self._tsi_fast), timeperiod=self._tsi_slow)

        denom = float(ds_abs[-1])
        if np.isnan(denom) or denom == 0.0 or np.isnan(ds_pc[-1]):
            return 0.0
        return float(100.0 * ds_pc[-1] / denom)

    # ------------------------------------------------------------------ #
    # Policy evaluation                                                    #
    # ------------------------------------------------------------------ #

    def _evaluate_policies(self, bars: list[OHLCV]) -> dict[str, bool]:
        close = np.array([float(b.close) for b in bars])
        high = np.array([float(b.high) for b in bars])
        low = np.array([float(b.low) for b in bars])

        # SuperTrend bullish
        st_bull = bool(supertrend_bullish(high, low, close, self._supertrend_period, self._supertrend_multiplier))

        # EMA20 rising (compare last bar to 5 bars ago)
        ema20 = talib.EMA(close, timeperiod=20)
        ema20_rising = (
            bool(ema20[-1] > ema20[-6])
            if not (np.isnan(ema20[-1]) or np.isnan(ema20[-6]))
            else False
        )

        # ADX above threshold and rising (slope of last 5 values > 0)
        adx = talib.ADX(high, low, close, timeperiod=14)
        adx_segment = adx[-5:]
        adx_ok = (
            not np.any(np.isnan(adx_segment))
            and float(adx[-1]) > self._min_adx
            and float(np.polyfit(np.arange(5, dtype=float), adx_segment, 1)[0]) > 0
        )

        # Price above EMA50
        ema50 = talib.EMA(close, timeperiod=50)
        price_above_ema50 = bool(not np.isnan(ema50[-1]) and float(close[-1]) > float(ema50[-1]))

        return {
            "supertrend": st_bull,
            "ema20_rising": ema20_rising,
            "adx": bool(adx_ok),
            "price_above_ema50": price_above_ema50,
        }

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _rank_changes(
        self,
        input: ResearchResult,
        current_scores: dict[str, float],
        ranked_set: set[str],
    ) -> tuple[dict[str, list[int | None]], list[str]]:
        current_ranks = {
            sym: i + 1
            for i, (sym, _) in enumerate(
                sorted(current_scores.items(), key=lambda x: x[1], reverse=True)
            )
        }
        hist_ranks_list: list[dict[str, int]] = []
        valid_labels: list[str] = []
        for offset, label in [(5, "1W"), (10, "2W"), (20, "4W")]:
            hist_scores: dict[str, float] = {}
            for sym in ranked_set:
                bars = input.bars.get(sym, [])
                if len(bars) > offset + _MIN_BARS:
                    hist_scores[sym] = self._trend_quality(bars[: len(bars) - offset], self._lookback_regression)
            if not hist_scores:
                continue
            hist_ranks = {
                sym: i + 1
                for i, (sym, _) in enumerate(
                    sorted(hist_scores.items(), key=lambda x: x[1], reverse=True)
                )
            }
            hist_ranks_list.append(hist_ranks)
            valid_labels.append(label)

        rank_changes = {
            sym: [
                (hist_ranks[sym] - cur_rank if sym in hist_ranks else None)
                for hist_ranks in hist_ranks_list
            ]
            for sym, cur_rank in current_ranks.items()
        }
        return rank_changes, valid_labels
