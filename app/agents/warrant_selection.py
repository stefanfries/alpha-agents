import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from app.agents.base import Agent
from app.config import settings
from app.models.market import Ticker
from app.models.signals import SelectedWarrant, SelectionResult, WarrantSelectionResult
from app.policies.warrant_scoring import (
    WarrantScoringConfig,
    build_warrant_rationale,
    compute_warrant_score,
)
from app.tools.finhub import FinHubTool
from app.tools.retry import retry_call

logger = logging.getLogger(__name__)


class WarrantSelectionAgent(Agent[SelectionResult, WarrantSelectionResult]):
    name = "warrant_selection"

    @staticmethod
    def _range_adjusted_scoring_config(
        base_config: WarrantScoringConfig,
        min_days_to_expiry: int,
        max_days_to_expiry: int,
    ) -> WarrantScoringConfig:
        if max_days_to_expiry <= min_days_to_expiry:
            return base_config

        return WarrantScoringConfig(
            spread_weight=base_config.spread_weight,
            spread_cutoff_pct=base_config.spread_cutoff_pct,
            leverage_weight=base_config.leverage_weight,
            leverage_mean=base_config.leverage_mean,
            leverage_sigma=base_config.leverage_sigma,
            days_weight=base_config.days_weight,
            days_mean=(min_days_to_expiry + max_days_to_expiry) / 2.0,
            days_sigma=max(base_config.days_sigma, (max_days_to_expiry - min_days_to_expiry) / 4.0),
            delta_weight=base_config.delta_weight,
            delta_peak=base_config.delta_peak,
            delta_half_width=base_config.delta_half_width,
        )

    def __init__(
        self,
        finhub: FinHubTool,
        prices: dict[str, float],
        min_days_to_expiry: int = 270,
        max_days_to_expiry: int = 450,
        atm_band: float = 0.02,
        atm_band_fallback: float = 0.10,
        isin_overrides: dict[str, str] | None = None,
        on_progress: Callable[[int, int, list[str]], Awaitable[None]] | None = None,
        scoring_config: WarrantScoringConfig | None = None,
    ) -> None:
        self._finhub = finhub
        self._prices = prices
        self._min_days = min_days_to_expiry
        self._max_days = max_days_to_expiry
        self._atm_band = atm_band
        self._atm_band_fallback = atm_band_fallback
        self._isin_overrides = isin_overrides or {}
        self._on_progress = on_progress
        # Keep the scoring peak aligned with the active maturity search window.
        base_scoring_config = scoring_config or WarrantScoringConfig.from_settings(settings.warrant_scoring)
        self._scoring_config = self._range_adjusted_scoring_config(
            base_scoring_config,
            min_days_to_expiry,
            max_days_to_expiry,
        )

    async def run(self, input: SelectionResult) -> WarrantSelectionResult:
        today = date.today()
        maturity_from = (today + timedelta(days=self._min_days)).isoformat()
        maturity_to = (today + timedelta(days=self._max_days)).isoformat()

        underlying_sem = asyncio.Semaphore(5)   # max 5 underlyings in parallel
        detail_sem = asyncio.Semaphore(10)       # max 10 concurrent detail fetches total
        total = len(input.selected)
        done_count = [0]
        active: set[str] = set()

        async def select_one(ticker: Ticker) -> tuple[SelectedWarrant | None, list[SelectedWarrant], int]:
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
        top3: dict[str, list[SelectedWarrant]] = {}
        analyzed_count: dict[str, int] = {}
        for ticker, result in zip(input.selected, results):
            if isinstance(result, BaseException):
                logger.warning("Warrant lookup failed for %s: %s", ticker.symbol, result)
                skipped.append(ticker.symbol)
            elif result is None:
                skipped.append(ticker.symbol)
            else:
                best, candidates_top3, count = result
                selected.append(best)
                top3[ticker.symbol] = candidates_top3
                analyzed_count[ticker.symbol] = count

        logger.info("Warrant selection: %d selected, %d skipped", len(selected), len(skipped))
        return WarrantSelectionResult(selected=selected, skipped=skipped, top3=top3, analyzed_count=analyzed_count)

    async def _pick_best(
        self,
        ticker: Ticker,
        maturity_from: str,
        maturity_to: str,
        detail_sem: asyncio.Semaphore,
    ) -> tuple[SelectedWarrant, list[SelectedWarrant], int] | None:
        if not ticker.isin:
            logger.warning("No ISIN for %s — skipping", ticker.symbol)
            return None

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
        strike_min = round(price * (1 - self._atm_band), 4) if price else None
        strike_max = round(price * (1 + self._atm_band), 4) if price else None

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

        candidates = await fetch_warrants(strike_min, strike_max)
        if candidates is None:
            return None

        if not candidates and price:
            wide_min = round(price * (1 - self._atm_band_fallback), 4)
            wide_max = round(price * (1 + self._atm_band_fallback), 4)
            logger.info(
                "%s: no warrants at ±%.0f%% — widening to ±%.0f%% (%.2f–%.2f)",
                ticker.symbol, self._atm_band * 100, self._atm_band_fallback * 100,
                wide_min, wide_max,
            )
            candidates = await fetch_warrants(wide_min, wide_max)
            if candidates is None:
                return None

        if not candidates:
            logger.info(
                "No warrants found for %s (%s) even after widening strike band",
                ticker.symbol, ticker.isin,
            )
            return None

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
        details = [d for d in details if not (d.get("reference_data") or {}).get("is_capped")]

        if not details:
            logger.warning("%s: all %d detail fetches failed or were capped — skipping", ticker.symbol, len(candidates))
            return None

        scored = sorted(details, key=lambda d: self._score(d, today), reverse=True)
        best_detail = scored[0]
        top3_details = scored[:3]

        best = self._build(ticker, best_detail, today, chart_symbol)
        top3 = [self._build(ticker, d, today, chart_symbol) for d in top3_details]
        return best, top3, len(details)

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
