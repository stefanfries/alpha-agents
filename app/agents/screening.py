import logging
from datetime import date

import numpy as np
import talib

from app.agents.base import Agent
from app.config import ScreeningSettings
from app.indicators import supertrend_bands
from app.models.market import OHLCV
from app.models.signals import ResearchResult, SelectionResult
from app.policies.trend_detection import (
    TrendDetectionPolicyConfig,
    bar_indicator_values,
    build_trend_indicator_series,
    passes_rule_group,
)

logger = logging.getLogger(__name__)

_MIN_BARS = 70  # 60-bar regression + ATR/EMA warmup


class SecuritySelectionAgent(Agent[ResearchResult, SelectionResult]):
    name = "screening"

    def __init__(self, settings: ScreeningSettings | None = None) -> None:
        cfg = settings or ScreeningSettings()
        self._top_n = cfg.top_n
        self._min_market_cap_eur = cfg.min_market_cap_eur
        self._lookback_regression = cfg.lookback_regression
        self._lookback_regression_short = cfg.lookback_regression_short
        self._tsi_fast = cfg.tsi_fast
        self._tsi_slow = cfg.tsi_slow
        self._trend_policy = TrendDetectionPolicyConfig(
            min_adx=cfg.min_adx,
            policy_supertrend=cfg.policy_supertrend,
            policy_ema20_rising=cfg.policy_ema20_rising,
            policy_adx_above=cfg.policy_adx_above,
            policy_adx_rising=cfg.policy_adx_rising,
            policy_price_above_ema50=cfg.policy_price_above_ema50,
            policy_tq60_above=cfg.policy_tq60_above,
            policy_tq20_above=cfg.policy_tq20_above,
            policy_tq60_min=cfg.policy_tq60_min,
            policy_tq20_min=cfg.policy_tq20_min,
            new_min_true=cfg.new_min_true,
            policy_supertrend_break=cfg.policy_supertrend_break,
            policy_ema20_falling_break=cfg.policy_ema20_falling_break,
            policy_adx_below_break=cfg.policy_adx_below_break,
            policy_adx_falling_break=cfg.policy_adx_falling_break,
            policy_price_below_ema50_break=cfg.policy_price_below_ema50_break,
            break_min_true=cfg.break_min_true,
            supertrend_period=cfg.supertrend_period,
            supertrend_multiplier=cfg.supertrend_multiplier,
        )

        # Keep existing attributes for compatibility with logs/tests during transition.
        self._min_adx = self._trend_policy.min_adx
        self._supertrend_period = self._trend_policy.supertrend_period
        self._supertrend_multiplier = self._trend_policy.supertrend_multiplier
        self._policy_supertrend = self._trend_policy.policy_supertrend
        self._policy_ema20_rising = self._trend_policy.policy_ema20_rising
        self._policy_adx_above = self._trend_policy.policy_adx_above
        self._policy_adx_rising = self._trend_policy.policy_adx_rising
        self._policy_price_above_ema50 = self._trend_policy.policy_price_above_ema50
        self._policy_tq60_above = self._trend_policy.policy_tq60_above
        self._policy_tq20_above = self._trend_policy.policy_tq20_above
        self._policy_tq60_min = self._trend_policy.policy_tq60_min
        self._policy_tq20_min = self._trend_policy.policy_tq20_min
        self._policy_ema20_falling_break = self._trend_policy.policy_ema20_falling_break
        self._policy_supertrend_break = self._trend_policy.policy_supertrend_break
        self._policy_adx_below_break = self._trend_policy.policy_adx_below_break
        self._policy_adx_falling_break = self._trend_policy.policy_adx_falling_break
        self._policy_price_below_ema50_break = self._trend_policy.policy_price_below_ema50_break
        self._new_min_true = self._trend_policy.new_min_true
        self._break_min_true = self._trend_policy.break_min_true

    async def run(self, input: ResearchResult) -> SelectionResult:
        scores: dict[str, float] = {}
        tq_short: dict[str, float] = {}
        tsi_vals: dict[str, float] = {}
        rationale: dict[str, str] = {}
        policy_results: dict[str, dict[str, bool]] = {}
        trend_signals: dict[str, str | None] = {}
        latest_candle_dates: dict[str, date] = {}
        previous_candle_dates: dict[str, date] = {}
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

            latest_candle_dates[symbol] = bars[-1].date
            if len(bars) >= 2:
                previous_candle_dates[symbol] = bars[-2].date

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

            new_enabled = self._trend_policy.entry_enabled_rules()
            break_enabled = self._trend_policy.exit_enabled_rules()
            passes_new = self._passes_policy_group(
                policies,
                new_enabled,
                min_true=self._new_min_true,
            )

            trend_signals[symbol] = self._trend_signal(
                bars,
                new_enabled,
                break_enabled,
                new_min_true=self._new_min_true,
                break_min_true=self._break_min_true,
            )

            policy_detail = " | ".join(
                f"{k}={'✓' if v else '✗'}" for k, v in policies.items()
            )
            rationale[symbol] = f"TQ={tq:.3f} TQ20={tqs:.3f} TSI={tsi:.1f} | {policy_detail}"

            if passes_new:
                candidate_symbols.add(symbol)

        ranked_symbols = sorted(
            candidate_symbols, key=lambda s: scores.get(s, 0.0), reverse=True
        )[: self._top_n]
        ranked_set = set(ranked_symbols)
        selected = [t for t in input.tickers if t.symbol in ranked_set]

        rank_changes, history_labels = self._rank_changes(input, scores)

        logger.info(
            "Screening complete: %d/%d selected "
            "(new: ST=%s EMA20=%s ADX>=%s ADX↑=%s EMA50=%s TQ60>%s TQ20>%s k=%s | "
            "break: ST=%s EMA20↓=%s ADX<=%s ADX↓=%s EMA50<=%s)",
            len(selected),
            len(input.tickers),
            self._policy_supertrend,
            self._policy_ema20_rising,
            self._policy_adx_above,
            self._policy_adx_rising,
            self._policy_price_above_ema50,
            self._policy_tq60_above,
            self._policy_tq20_above,
            self._new_min_true,
            self._policy_supertrend_break,
            self._policy_ema20_falling_break,
            self._policy_adx_below_break,
            self._policy_adx_falling_break,
            self._policy_price_below_ema50_break,
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
            latest_candle_dates=latest_candle_dates,
            previous_candle_dates=previous_candle_dates,
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

    def _trend_signal(
        self,
        bars: list[OHLCV],
        new_enabled: dict[str, bool],
        break_enabled: dict[str, bool],
        new_min_true: int | None,
        break_min_true: int | None,
    ) -> str | None:
        """State machine over full bar history → NEW / BREAK / HOLD / None.

        Transitions: OUT -[NEW]-> IN_TREND -[BREAK]-> OUT.
        Consecutive same-direction signals are impossible by construction.
        """
        n = len(bars)
        if n < _MIN_BARS:
            return None
        series = build_trend_indicator_series(
            bars,
            self._trend_policy,
            supertrend_fn=supertrend_bands,
        )

        def _bar_passes(i: int) -> tuple[bool, bool]:
            c = bar_indicator_values(
                idx=i,
                series=series,
                policy_cfg=self._trend_policy,
                lookback_regression=self._lookback_regression,
                lookback_regression_short=self._lookback_regression_short,
            )
            return (
                self._passes_policy_group(c, new_enabled, new_min_true),
                self._passes_policy_group(c, break_enabled, break_min_true),
            )

        prev_pn, prev_pb = _bar_passes(0)
        state = "OUT"
        last_signal: str | None = None
        last_signal_bar = -1

        for i in range(1, n):
            pn, pb = _bar_passes(i)
            if state == "OUT" and pn and not prev_pn:
                state = "IN_TREND"
                last_signal = "NEW"
                last_signal_bar = i
            elif state == "IN_TREND" and pb and not prev_pb:
                state = "OUT"
                last_signal = "BREAK"
                last_signal_bar = i
            prev_pn, prev_pb = pn, pb

        age = n - 1 - last_signal_bar  # 0 = fired on current (last) bar
        current_passes_new = prev_pn
        if last_signal == "NEW" and age <= 5 and current_passes_new:
            return "NEW"
        if last_signal == "BREAK" and age <= 5:
            return "BREAK"
        if state == "IN_TREND":
            return "HOLD"
        return None

    def _evaluate_policies(self, bars: list[OHLCV]) -> dict[str, bool]:
        series = build_trend_indicator_series(
            bars,
            self._trend_policy,
            supertrend_fn=supertrend_bands,
        )
        return bar_indicator_values(
            idx=len(bars) - 1,
            series=series,
            policy_cfg=self._trend_policy,
            lookback_regression=self._lookback_regression,
            lookback_regression_short=self._lookback_regression_short,
        )

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _passes_policy_group(
        self,
        policy_results: dict[str, bool],
        enabled_policies: dict[str, bool],
        min_true: int | None,
    ) -> bool:
        return passes_rule_group(policy_results, enabled_policies, min_true)

    def _rank_changes(
        self,
        input: ResearchResult,
        current_scores: dict[str, float],
    ) -> tuple[dict[str, list[int | None]], list[str]]:
        # Deterministic tie-breaker keeps rank movements stable for equal TQ values.
        def rank_items(score_map: dict[str, float]) -> list[tuple[str, float]]:
            return sorted(score_map.items(), key=lambda x: (-x[1], x[0]))
        current_ranks = {
            sym: i + 1
            for i, (sym, _) in enumerate(
                rank_items(current_scores)
            )
        }
        hist_ranks_list: list[dict[str, int]] = []
        valid_labels: list[str] = []
        for offset, label in [(5, "1W"), (10, "2W"), (20, "4W")]:
            hist_scores: dict[str, float] = {}
            for sym in current_scores:
                bars = input.bars.get(sym, [])
                if len(bars) > offset + _MIN_BARS:
                    hist_scores[sym] = self._trend_quality(bars[: len(bars) - offset], self._lookback_regression)
            if not hist_scores:
                continue
            hist_ranks = {
                sym: i + 1
                for i, (sym, _) in enumerate(
                    rank_items(hist_scores)
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
