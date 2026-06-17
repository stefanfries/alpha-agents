import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from app.agents.base import Agent
from app.models.market import Ticker
from app.models.signals import SelectedWarrant, SelectionResult, WarrantSelectionResult
from app.tools.finhub import FinHubTool
from app.tools.retry import retry_call

logger = logging.getLogger(__name__)


class WarrantSelectionAgent(Agent[SelectionResult, WarrantSelectionResult]):
    name = "warrant_selection"

    def __init__(
        self,
        finhub: FinHubTool,
        prices: dict[str, float],
        min_days_to_expiry: int = 270,
        max_days_to_expiry: int = 456,
        atm_band: float = 0.02,
        atm_band_fallback: float = 0.10,
        isin_overrides: dict[str, str] | None = None,
        on_progress: Callable[[int, int, list[str]], Awaitable[None]] | None = None,
    ) -> None:
        self._finhub = finhub
        self._prices = prices
        self._min_days = min_days_to_expiry
        self._max_days = max_days_to_expiry
        self._atm_band = atm_band
        self._atm_band_fallback = atm_band_fallback
        self._isin_overrides = isin_overrides or {}
        self._on_progress = on_progress

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
        # underlying's own native-currency price instead — no FX conversion.
        chart_symbol: str | None = None
        if lookup_isin != ticker.isin:
            price = await self._override_underlying_price(lookup_isin, maturity_from, maturity_to)
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

    async def _override_underlying_price(
        self, lookup_isin: str, maturity_from: str, maturity_to: str
    ) -> float | None:
        """Native-currency price of an override underlying for the strike band.

        Read from a warrant's ``reference_data.underlying_price`` (same currency
        as ``strike``), so the band needs no FX conversion. Returns None if no
        warrant or price is available, in which case the band is left unbounded.
        """
        try:
            candidates = await self._finhub.get_warrants(
                underlying=lookup_isin,
                preselection="CALL",
                maturity_from=maturity_from,
                maturity_to=maturity_to,
            )
        except Exception:
            logger.warning("override price: get_warrants failed for %s", lookup_isin)
            return None
        for c in candidates:
            if not c.get("isin"):
                continue
            try:
                detail = await self._finhub.get_warrant_detail(c["isin"])
            except Exception:
                continue
            price = (detail or {}).get("reference_data", {}).get("underlying_price")
            if price:
                return float(price)
        return None

    def _score(self, detail: dict, today: date) -> float:
        md = detail.get("market_data") or {}
        an = detail.get("analytics") or {}
        rd = detail.get("reference_data") or {}

        score = 0.0

        # Spread: lower is better; 0% → 1.0, 3% → 0.0
        spread_pct = md.get("spread_percent")
        if spread_pct is not None:
            score += 0.40 * max(0.0, 1.0 - spread_pct / 3.0)

        # Leverage: sweet spot 3–8×; peak at 5×
        leverage = an.get("leverage")
        if leverage is not None and leverage > 0:
            score += 0.25 * math.exp(-0.5 * ((leverage - 5.0) / 3.0) ** 2)

        # Days to expiry: peak at midpoint of the 9–12 month window (~315 days)
        maturity_raw = rd.get("maturity_date")
        if maturity_raw:
            try:
                days = (date.fromisoformat(str(maturity_raw)) - today).days
                if days > 0:
                    score += 0.20 * math.exp(-0.5 * ((days - 315) / 45.0) ** 2)
            except (ValueError, TypeError):
                pass

        # Delta: ATM calls ideally around 0.5
        delta = an.get("delta")
        if delta is not None:
            score += 0.15 * max(0.0, 1.0 - abs(delta - 0.5) / 0.5)

        return score

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

        days_to_expiry: int | None = None
        if maturity_raw:
            try:
                days_to_expiry = (date.fromisoformat(str(maturity_raw)) - today).days
            except (ValueError, TypeError):
                pass

        parts = []
        if spread_pct is not None:
            parts.append(f"spread {spread_pct:.1f}%")
        if leverage is not None:
            parts.append(f"leverage {leverage:.1f}×")
        if days_to_expiry is not None:
            parts.append(f"{days_to_expiry}d to expiry")
        if delta is not None:
            parts.append(f"δ={delta:.2f}")

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
            rationale=", ".join(parts) if parts else "—",
            issuer_action=bool(rd.get("issuer_action")),
            issuer_no_fee_action=bool(rd.get("issuer_no_fee_action")),
            chart_symbol=chart_symbol,
        )
