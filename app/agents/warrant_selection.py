import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.agents.base import Agent
from app.config import settings
from app.models.market import Ticker
from app.models.signals import (
    RollCandidate,
    RollReplacement,
    SelectedWarrant,
    SelectionResult,
    WarrantSelectionResult,
)
from app.policies.warrant_scoring import (
    WarrantScoringConfig,
    build_warrant_rationale,
    compute_warrant_score,
)
from app.tools.finhub import FinHubTool
from app.tools.retry import retry_call

logger = logging.getLogger(__name__)

# Approximate sensitivity of call-warrant delta to strike factor.
# At ATM (factor=1.0) delta≈0.5; each 0.10 move in strike factor shifts
# the expected delta by ~0.15 (derived from Black-Scholes with σ≈30%, T≈1yr).
_STRIKE_DELTA_SENSITIVITY: float = 1.5


@dataclass
class _RollOutcome:
    selected: list[SelectedWarrant] = field(default_factory=list)
    incumbents: dict[str, RollReplacement] = field(default_factory=dict)
    underlyings: list[str] = field(default_factory=list)
    sell_underlyings: list[str] = field(default_factory=list)
    sell_existing_isins: list[str] = field(default_factory=list)
    top3: dict[str, list[SelectedWarrant]] = field(default_factory=dict)
    analyzed_count: dict[str, int] = field(default_factory=dict)


class WarrantSelectionAgent(Agent[SelectionResult, WarrantSelectionResult]):
    name = "warrant_selection"

    @staticmethod
    def _range_adjusted_scoring_config(
        base_config: WarrantScoringConfig,
        min_days_to_expiry: int,
        max_days_to_expiry: int,
        strike_min_factor: float | None = None,
        strike_max_factor: float | None = None,
    ) -> WarrantScoringConfig:
        # Align days scoring with the active maturity search window.
        if max_days_to_expiry > min_days_to_expiry:
            days_mean = (min_days_to_expiry + max_days_to_expiry) / 2.0
            days_sigma = max(base_config.days_sigma, (max_days_to_expiry - min_days_to_expiry) / 4.0)
        else:
            days_mean = base_config.days_mean
            days_sigma = base_config.days_sigma

        # Align delta scoring peak with the midpoint of the strike band.
        # A call delta decreases as strike moves OTM (factor > 1.0) and increases
        # as strike moves ITM (factor < 1.0). _STRIKE_DELTA_SENSITIVITY approximates
        # this relationship for medium-term European calls.
        if strike_min_factor is not None and strike_max_factor is not None:
            strike_target = (strike_min_factor + strike_max_factor) / 2.0
            raw_peak = 0.5 - (strike_target - 1.0) * _STRIKE_DELTA_SENSITIVITY
            delta_peak = max(0.1, min(0.9, raw_peak))
        else:
            delta_peak = base_config.delta_peak

        return WarrantScoringConfig(
            spread_weight=base_config.spread_weight,
            spread_cutoff_pct=base_config.spread_cutoff_pct,
            leverage_weight=base_config.leverage_weight,
            leverage_mean=base_config.leverage_mean,
            leverage_sigma=base_config.leverage_sigma,
            days_weight=base_config.days_weight,
            days_mean=days_mean,
            days_sigma=days_sigma,
            delta_weight=base_config.delta_weight,
            delta_peak=delta_peak,
            delta_half_width=base_config.delta_half_width,
        )

    def __init__(
        self,
        finhub: FinHubTool,
        prices: dict[str, float],
        min_days_to_expiry: int = 270,
        max_days_to_expiry: int = 450,
        strike_min_factor: float = 0.95,
        strike_max_factor: float = 1.00,
        min_score: float = 0.0,
        spread_max_pct: float | None = None,
        atm_band_fallback: float = 0.10,
        max_selected: int | None = None,
        isin_overrides: dict[str, str] | None = None,
        on_progress: Callable[[int, int, list[str]], Awaitable[None]] | None = None,
        scoring_config: WarrantScoringConfig | None = None,
        roll_candidates: list[RollCandidate] | None = None,
        roll_min_improvement: float = 0.10,
    ) -> None:
        self._finhub = finhub
        self._prices = prices
        self._min_days = min_days_to_expiry
        self._max_days = max_days_to_expiry
        self._strike_min_factor = strike_min_factor
        self._strike_max_factor = strike_max_factor
        self._min_score = min_score
        self._spread_max_pct = spread_max_pct
        self._atm_band_fallback = atm_band_fallback
        self._max_selected = max_selected
        self._isin_overrides = isin_overrides or {}
        self._on_progress = on_progress
        self._roll_candidates = roll_candidates or []
        self._roll_min_improvement = roll_min_improvement
        # Keep the scoring peak aligned with the active maturity search window.
        base_scoring_config = scoring_config or WarrantScoringConfig.from_settings(settings.warrant_scoring)
        self._scoring_config = self._range_adjusted_scoring_config(
            base_scoring_config,
            min_days_to_expiry,
            max_days_to_expiry,
            strike_min_factor,
            strike_max_factor,
        )

    async def run(self, input: SelectionResult) -> WarrantSelectionResult:
        today = date.today()
        maturity_from = (today + timedelta(days=self._min_days)).isoformat()
        maturity_to = (today + timedelta(days=self._max_days)).isoformat()

        underlying_sem = asyncio.Semaphore(5)   # max 5 underlyings in parallel
        detail_sem = asyncio.Semaphore(5)        # max 5 concurrent detail fetches total (reduced from 10 to avoid Comdirect rate limiting)
        total = len(input.selected)
        done_count = [0]
        active: set[str] = set()

        async def select_one(
            ticker: Ticker,
        ) -> tuple[SelectedWarrant | None, list[SelectedWarrant], int, str | None] | None:
            async with underlying_sem:
                active.add(ticker.symbol)
                if self._on_progress:
                    await self._on_progress(done_count[0], total, sorted(active))
                result = await self._pick_best(ticker, maturity_from, maturity_to, detail_sem)
                active.discard(ticker.symbol)
            done_count[0] += 1
            if self._on_progress:
                await self._on_progress(done_count[0], total, sorted(active))
            return result

        results = await asyncio.gather(
            *[select_one(t) for t in input.selected],
            return_exceptions=True,
        )

        selected: list[SelectedWarrant] = []
        skipped: list[str] = []
        skipped_reasons: dict[str, str] = {}
        skipped_names: dict[str, str] = {}
        top3: dict[str, list[SelectedWarrant]] = {}
        analyzed_count: dict[str, int] = {}
        for ticker, result in zip(input.selected, results):
            if isinstance(result, BaseException):
                logger.warning("Warrant lookup failed for %s: %s", ticker.symbol, result)
                skipped.append(ticker.symbol)
                skipped_reasons[ticker.symbol] = "lookup failed"
            elif result is None:
                skipped.append(ticker.symbol)
            else:
                best, candidates_top3, count, skip_reason = result
                if best is None:
                    skipped.append(ticker.symbol)
                    if skip_reason:
                        skipped_reasons[ticker.symbol] = skip_reason
                    if count:
                        analyzed_count[ticker.symbol] = count
                elif self._max_selected is not None and len(selected) >= self._max_selected:
                    # Warrant found but all free position slots are filled — not entered.
                    analyzed_count[ticker.symbol] = count
                else:
                    selected.append(best)
                    top3[ticker.symbol] = candidates_top3
                    analyzed_count[ticker.symbol] = count

        for ticker in input.selected:
            if ticker.symbol in skipped and ticker.name:
                skipped_names[ticker.symbol] = ticker.name

        roll = await self._select_rolls(today, maturity_from, maturity_to, detail_sem)
        top3.update(roll.top3)
        analyzed_count.update(roll.analyzed_count)

        logger.info("Warrant selection: %d selected, %d skipped, %d rolls, %d roll/sell",
                    len(selected), len(skipped), len(roll.underlyings), len(roll.sell_underlyings))
        return WarrantSelectionResult(
            selected=selected,
            skipped=skipped,
            skipped_reasons=skipped_reasons,
            skipped_names=skipped_names,
            top3=top3,
            analyzed_count=analyzed_count,
            roll_underlyings=roll.underlyings,
            roll_sell_underlyings=roll.sell_underlyings,
            sell_existing_isins=roll.sell_existing_isins,
            roll_selected=roll.selected,
            roll_incumbents=roll.incumbents,
        )

    async def _select_rolls(
        self,
        today: date,
        maturity_from: str,
        maturity_to: str,
        detail_sem: asyncio.Semaphore,
    ) -> "_RollOutcome":
        outcome = _RollOutcome()
        if not self._roll_candidates:
            return outcome

        results = await asyncio.gather(
            *[self._pick_best(rc.underlying, maturity_from, maturity_to, detail_sem)
              for rc in self._roll_candidates],
            return_exceptions=True,
        )
        for rc, result in zip(self._roll_candidates, results):
            sym = rc.underlying.symbol
            maturity_iso = (
                rc.maturity_date.isoformat() if rc.maturity_date is not None
                else (today + timedelta(days=rc.days_to_maturity)).isoformat()
                if rc.days_to_maturity is not None else None
            )
            incumbent_score = compute_warrant_score(
                rc.spread_pct, rc.leverage, maturity_iso, rc.delta, today, self._scoring_config
            )
            outcome.incumbents[sym] = RollReplacement(
                warrant_isin=rc.warrant_isin,
                warrant_wkn=rc.warrant_wkn,
                strike=rc.strike,
                maturity_date=rc.maturity_date,
                spread_pct=rc.spread_pct,
                leverage=rc.leverage,
                delta=rc.delta,
                score=incumbent_score,
            )
            best: SelectedWarrant | None = None
            if isinstance(result, BaseException):
                logger.warning("Roll replacement lookup failed for %s: %s", sym, result)
            elif result is not None:
                best, candidates_top3, count, _skip_reason = result
                # Always expose the searched alternatives, even when the incumbent is kept,
                # so the UI can show that all candidates scored worse.
                outcome.top3[sym] = candidates_top3
                outcome.analyzed_count[sym] = count

            if best is not None and best.score >= incumbent_score + self._roll_min_improvement:
                outcome.selected.append(best)
                outcome.underlyings.append(sym)
            else:
                # No replacement clears the score margin — the incumbent is degraded
                # (it only reaches this loop via a roll classification), so recommend
                # closing the position outright rather than keeping a known-degraded warrant.
                outcome.sell_underlyings.append(sym)
                outcome.sell_existing_isins.append(rc.warrant_isin)
        return outcome

    async def _pick_best(
        self,
        ticker: Ticker,
        maturity_from: str,
        maturity_to: str,
        detail_sem: asyncio.Semaphore,
    ) -> tuple[SelectedWarrant | None, list[SelectedWarrant], int, str | None] | None:
        if not ticker.isin:
            logger.warning("No ISIN for %s — skipping", ticker.symbol)
            return None, [], 0, "missing ISIN"

        # Warrant lookup may use a manual override ISIN (e.g. an ADR whose
        # underlying stock carries the warrants). Display + price stay on `ticker`.
        lookup_isin = self._isin_overrides.get(ticker.isin, ticker.isin)
        if lookup_isin != ticker.isin:
            logger.info("%s: using override ISIN %s for warrant lookup", ticker.symbol, lookup_isin)

        # The strike band must be expressed in the warrant's strike currency. For
        # an override the ADR's `currentPrice` is in the wrong currency (e.g. USD
        # ADR vs EUR-denominated underlying), so derive the band from the override
        # underlying's live quote price instead — no FX conversion.
        chart_symbol: str | None = None
        if lookup_isin != ticker.isin:
            price = await self._override_underlying_price(lookup_isin)
            # Chart the override underlying (matching the strike currency) instead
            # of the ADR, so candles and the strike line share one currency.
            chart_symbol = await self._override_chart_symbol(lookup_isin)
        else:
            price = self._prices.get(ticker.symbol)
        factor_min = min(self._strike_min_factor, self._strike_max_factor)
        factor_max = max(self._strike_min_factor, self._strike_max_factor)

        async def fetch_warrants(s_min: float | None, s_max: float | None) -> list[dict[str, Any]] | None:
            try:
                return await retry_call(
                    self._finhub.get_warrants,
                    underlying=lookup_isin,
                    preselection="CALL",
                    maturity_from=maturity_from,
                    maturity_to=maturity_to,
                    strike_min=s_min,
                    strike_max=s_max,
                )
            except Exception:
                logger.warning("get_warrants failed for %s after retry", ticker.symbol)
                return None

        candidates: list[dict[str, Any]] = []
        adjustment_count = 0
        widen_count = 0
        shrink_count = 0
        if price:
            center_factor = (factor_min + factor_max) / 2.0
            half_width = (factor_max - factor_min) / 2.0

            while adjustment_count < 4:
                strike_min = round(price * max(0.0, center_factor - half_width), 4)
                strike_max = round(price * max(0.0, center_factor + half_width), 4)
                if strike_min > strike_max:
                    strike_min, strike_max = strike_max, strike_min

                candidates = await fetch_warrants(strike_min, strike_max)
                if candidates is None:
                    return None, [], 0, "lookup failed"

                count = len(candidates)
                if count < 5 and widen_count < 2 and half_width > 0:
                    widen_count += 1
                    adjustment_count += 1
                    half_width *= 2.0
                    logger.info(
                        "%s: %d candidates (<5) in strike factors %.3f–%.3f, widening interval (pass %d)",
                        ticker.symbol,
                        count,
                        center_factor - (half_width / 2.0),
                        center_factor + (half_width / 2.0),
                        widen_count,
                    )
                    continue

                if count > 50 and shrink_count < 2 and half_width > 0:
                    shrink_count += 1
                    adjustment_count += 1
                    half_width /= 2.0
                    logger.info(
                        "%s: %d candidates (>50) in strike factors %.3f–%.3f, narrowing interval (pass %d)",
                        ticker.symbol,
                        count,
                        center_factor - (half_width * 2.0),
                        center_factor + (half_width * 2.0),
                        shrink_count,
                    )
                    continue

                break
        else:
            candidates = await fetch_warrants(None, None)
            if candidates is None:
                return None, [], 0, "lookup failed"

        if not candidates and price:
            wide_min = round(price * (1 - self._atm_band_fallback), 4)
            wide_max = round(price * (1 + self._atm_band_fallback), 4)
            logger.info(
                "%s: no warrants in strike-factor range %.3f–%.3f — widening to ±%.0f%% (%.2f–%.2f)",
                ticker.symbol,
                factor_min,
                factor_max,
                self._atm_band_fallback * 100,
                wide_min,
                wide_max,
            )
            candidates = await fetch_warrants(wide_min, wide_max)
            if candidates is None:
                return None, [], 0, "lookup failed"

        if not candidates:
            logger.info(
                "No warrants found for %s (%s) even after widening strike band",
                ticker.symbol, ticker.isin,
            )
            return None, [], 0, "no candidates in configured maturity/strike range"

        logger.info(
            "%s: %d warrant candidates — fetching all details", ticker.symbol, len(candidates)
        )

        today = date.today()

        async def fetch_detail(isin: str) -> dict[str, Any] | None:
            async with detail_sem:
                try:
                    return await retry_call(self._finhub.get_warrant_detail, isin)
                except Exception as exc:
                    logger.warning("Failed to fetch detail for %s: %s", isin, exc)
                    return None

        raw = await asyncio.gather(*[
            fetch_detail(c["isin"]) for c in candidates if c.get("isin")
        ])
        details = [d for d in raw if d]
        fetched_count = len(details)
        capped_rejected = sum(
            1 for d in details if (d.get("reference_data") or {}).get("is_capped")
        )
        details = [d for d in details if not (d.get("reference_data") or {}).get("is_capped")]

        spread_rejected = 0
        if self._spread_max_pct is not None:
            spread_eligible_details: list[dict[str, Any]] = []
            for detail in details:
                spread = self._as_float((detail.get("market_data") or {}).get("spread_percent"))
                if spread is not None and spread > self._spread_max_pct:
                    spread_rejected += 1
                    continue
                spread_eligible_details.append(detail)
            details = spread_eligible_details

        if not details:
            if spread_rejected > 0:
                logger.info(
                    "%s: all %d eligible details rejected by spread cap %.2f%%",
                    ticker.symbol,
                    spread_rejected,
                    self._spread_max_pct,
                )
                return None, [], 0, "all candidates above configured spread cap"

            if capped_rejected > 0 and capped_rejected == fetched_count:
                logger.info("%s: all %d call warrants are capped — skipping", ticker.symbol, capped_rejected)
                return None, [], 0, "only capped call warrants available"

            logger.warning("%s: all %d detail fetches failed — skipping", ticker.symbol, len(candidates))
            return None, [], 0, "all detail fetches failed"

        scored_with_values = [
            (detail, self._score(detail, today))
            for detail in details
        ]
        scored_with_values.sort(key=lambda pair: pair[1], reverse=True)
        qualified = [pair for pair in scored_with_values if pair[1] > self._min_score]
        if not qualified:
            top_score = scored_with_values[0][1]
            logger.info(
                "%s: %d candidates analyzed, top score %.3f did not exceed min_score %.3f",
                ticker.symbol,
                len(scored_with_values),
                top_score,
                self._min_score,
            )
            return (
                None,
                [],
                len(scored_with_values),
                f"no candidate exceeded min score {self._min_score:.2f} (best {top_score:.2f})",
            )

        best_detail, _best_score = qualified[0]
        top3_details = [detail for detail, _score in qualified[:3]]

        best = self._build(ticker, best_detail, today, chart_symbol)
        top3 = [self._build(ticker, d, today, chart_symbol) for d in top3_details]
        return best, top3, len(scored_with_values), None

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def _override_chart_symbol(self, lookup_isin: str) -> str | None:
        """yfinance symbol of an override underlying (native-currency price series)."""
        try:
            inst = await self._finhub.get_instrument(lookup_isin)
        except Exception:
            logger.warning("override chart: get_instrument failed for %s", lookup_isin)
            return None
        return (inst or {}).get("global_identifiers", {}).get("symbol_yfinance")

    async def _override_underlying_price(self, lookup_isin: str) -> float | None:
        """Live native-currency price of an override underlying for the strike band.

        This uses the FinHub /quotes endpoint so the strike window is anchored to
        the current underlying quote, not to a warrant detail snapshot.
        """
        try:
            quote = await self._finhub.get_quote(lookup_isin)
        except Exception:
            logger.warning("override price: get_quote failed for %s", lookup_isin)
            return None
        return self._extract_quote_price(quote)

    @staticmethod
    def _extract_quote_price(quote: dict[str, Any] | None) -> float | None:
        if not quote:
            return None
        for key in ("currentPrice", "price", "lastPrice", "last", "close"):
            value = quote.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        bid = quote.get("bid")
        ask = quote.get("ask")
        if bid is not None and ask is not None:
            try:
                return (float(bid) + float(ask)) / 2.0
            except (TypeError, ValueError):
                return None
        for key in ("bid", "ask"):
            value = quote.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        for key in ("data", "quote", "result"):
            nested = quote.get(key)
            if isinstance(nested, dict):
                price = WarrantSelectionAgent._extract_quote_price(nested)
                if price is not None:
                    return price
        return None

    def _score(self, detail: dict, today: date) -> float:
        md = detail.get("market_data") or {}
        an = detail.get("analytics") or {}
        rd = detail.get("reference_data") or {}

        spread_pct = md.get("spread_percent")
        leverage = an.get("leverage")
        maturity_date = rd.get("maturity_date")
        delta = an.get("delta")

        return compute_warrant_score(spread_pct, leverage, maturity_date, delta, today, self._scoring_config)

    def _build(
        self, underlying: Ticker, detail: dict, today: date, chart_symbol: str | None = None
    ) -> SelectedWarrant:
        md = detail.get("market_data") or {}
        an = detail.get("analytics") or {}
        rd = detail.get("reference_data") or {}

        spread_pct = md.get("spread_percent")
        leverage = an.get("leverage")
        delta = an.get("delta")
        maturity_raw = rd.get("maturity_date")

        return SelectedWarrant(
            underlying=underlying,
            warrant_isin=detail.get("isin", ""),
            warrant_wkn=detail.get("wkn", ""),
            strike=rd.get("strike"),
            maturity_date=maturity_raw,
            spread_pct=spread_pct,
            leverage=leverage,
            delta=delta,
            bid=md.get("bid"),
            ask=md.get("ask"),
            score=self._score(detail, today),
            rationale=build_warrant_rationale(spread_pct, leverage, maturity_raw, delta, today),
            issuer_action=bool(rd.get("issuer_action")),
            issuer_no_fee_action=bool(rd.get("issuer_no_fee_action")),
            chart_symbol=chart_symbol,
        )
