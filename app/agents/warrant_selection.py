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

logger = logging.getLogger(__name__)


class WarrantSelectionAgent(Agent[SelectionResult, WarrantSelectionResult]):
    name = "warrant_selection"

    def __init__(
        self,
        finhub: FinHubTool,
        prices: dict[str, float],
        min_days_to_expiry: int = 270,
        max_days_to_expiry: int = 365,
        atm_band: float = 0.02,
        on_progress: Callable[[int, int, list[str]], Awaitable[None]] | None = None,
    ) -> None:
        self._finhub = finhub
        self._prices = prices
        self._min_days = min_days_to_expiry
        self._max_days = max_days_to_expiry
        self._atm_band = atm_band
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

        async def select_one(ticker: Ticker) -> SelectedWarrant | None:
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
        for ticker, result in zip(input.selected, results):
            if isinstance(result, BaseException):
                logger.warning("Warrant lookup failed for %s: %s", ticker.symbol, result)
                skipped.append(ticker.symbol)
            elif result is None:
                skipped.append(ticker.symbol)
            else:
                selected.append(result)

        logger.info("Warrant selection: %d selected, %d skipped", len(selected), len(skipped))
        return WarrantSelectionResult(selected=selected, skipped=skipped)

    async def _pick_best(
        self,
        ticker: Ticker,
        maturity_from: str,
        maturity_to: str,
        detail_sem: asyncio.Semaphore,
    ) -> SelectedWarrant | None:
        if not ticker.isin:
            logger.warning("No ISIN for %s — skipping", ticker.symbol)
            return None

        price = self._prices.get(ticker.symbol)
        strike_min = round(price * (1 - self._atm_band), 4) if price else None
        strike_max = round(price * (1 + self._atm_band), 4) if price else None

        candidates = await self._finhub.get_warrants(
            underlying=ticker.isin,
            preselection="CALL",
            maturity_from=maturity_from,
            maturity_to=maturity_to,
            strike_min=strike_min,
            strike_max=strike_max,
        )
        if not candidates:
            logger.info(
                "No warrants found for %s (%s) strike %.2f–%.2f",
                ticker.symbol, ticker.isin, strike_min or 0, strike_max or 0,
            )
            return None

        logger.info(
            "%s: %d warrant candidates — fetching all details", ticker.symbol, len(candidates)
        )

        today = date.today()

        async def fetch_detail(isin: str) -> dict[str, Any] | None:
            async with detail_sem:
                try:
                    return await self._finhub.get_warrant_detail(isin)
                except Exception:
                    logger.warning("Failed to fetch detail for warrant %s", isin)
                    return None

        raw = await asyncio.gather(*[
            fetch_detail(c["isin"]) for c in candidates if c.get("isin")
        ])
        details = [d for d in raw if d]

        if not details:
            return None

        best = max(details, key=lambda d: self._score(d, today))
        return self._build(ticker, best, today)

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

    def _build(self, underlying: Ticker, detail: dict, today: date) -> SelectedWarrant:
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
        )
