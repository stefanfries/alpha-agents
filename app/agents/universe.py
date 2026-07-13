import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel

from app.agents.base import Agent
from app.config import settings
from app.models.market import Ticker
from app.models.signals import UniverseResult
from app.tools.finhub import FinHubTool
from app.tools.retry import retry_call
from app.tools.wikipedia import WikipediaIndexTool

logger = logging.getLogger(__name__)


class UniverseInput(BaseModel):
    indices: list[str]
    extra_tickers: list[Ticker] = []
    exclude_tickers: list[Ticker] = []


class UniverseAgent(Agent[UniverseInput, UniverseResult]):
    name = "universe"

    def __init__(
        self,
        finhub: FinHubTool,
        wikipedia: WikipediaIndexTool,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        self._finhub = finhub
        self._wikipedia = wikipedia
        self._on_progress = on_progress

    async def run(self, input: UniverseInput) -> UniverseResult:
        all_entries: list[tuple[Ticker, str]] = []  # (ticker, source_index_name)
        unresolved: list[str] = []
        self._adr_isins: set[str] = set()

        for index_name in input.indices:
            tickers = await self._resolve_index(index_name)
            if tickers is None:
                unresolved.append(index_name)
            else:
                all_entries.extend((t, index_name) for t in tickers)

        # Deduplicate — ISIN is primary key, symbol is fallback
        seen_isins: set[str] = set()
        seen_symbols: set[str] = set()
        source: dict[str, str] = {}
        deduped: list[Ticker] = []

        for ticker, index_name in all_entries:
            if ticker.isin:
                if ticker.isin in seen_isins:
                    continue
                seen_isins.add(ticker.isin)
                source[ticker.isin] = index_name
            else:
                if ticker.symbol in seen_symbols:
                    continue
                seen_symbols.add(ticker.symbol)
                source[ticker.symbol] = index_name
            deduped.append(ticker)

        # Apply extra_tickers
        for ticker in input.extra_tickers:
            if ticker.isin and ticker.isin in seen_isins:
                continue
            if not ticker.isin and ticker.symbol in seen_symbols:
                continue
            deduped.append(ticker)
            if ticker.isin:
                seen_isins.add(ticker.isin)
                source[ticker.isin] = "extra"
            else:
                seen_symbols.add(ticker.symbol)
                source[ticker.symbol] = "extra"

        # Apply exclude_tickers
        exclude_isins = {t.isin for t in input.exclude_tickers if t.isin}
        exclude_symbols = {t.symbol for t in input.exclude_tickers}
        final = [
            t for t in deduped
            if t.isin not in exclude_isins and t.symbol not in exclude_symbols
        ]

        missing_isin = [t.symbol for t in final if t.isin is None]
        adr_isins = [t.isin for t in final if t.isin in self._adr_isins]

        logger.info(
            "Universe resolved: %d tickers, %d missing ISIN, %d unresolved indices, %d ADRs",
            len(final), len(missing_isin), len(unresolved), len(adr_isins),
        )
        return UniverseResult(
            tickers=final,
            source=source,
            missing_isin=missing_isin,
            unresolved_indices=unresolved,
            adr_isins=adr_isins,
        )

    async def _resolve_index(self, index_name: str) -> list[Ticker] | None:
        try:
            tickers = await self._resolve_via_finhub(index_name)
            if tickers:
                return tickers
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning(
                "FinHub failed for %r (%s) — falling back to Wikipedia", index_name, exc
            )

        tickers = await self._wikipedia.get_index_constituents(index_name)
        if tickers:
            return tickers

        logger.error("Both FinHub and Wikipedia failed for index %r", index_name)
        return None

    async def _resolve_via_finhub(self, index_name: str) -> list[Ticker]:
        try:
            await self._finhub.ping()
        except Exception as exc:
            logger.warning("FinHub ping failed (%s) — proceeding anyway", exc)

        members = await self._finhub.get_index_constituents(index_name)
        if not members:
            return []

        sem = asyncio.Semaphore(max(1, settings.finhub.instrument_lookup_concurrency))
        total = len(members)
        done = 0
        last_reported = 0
        report_every = max(1, total // 25)
        progress_lock = asyncio.Lock()

        async def mark_progress() -> None:
            nonlocal done, last_reported
            to_report: tuple[int, int] | None = None
            async with progress_lock:
                done += 1
                if (
                    self._on_progress is not None
                    and (done - last_reported >= report_every or done == total)
                ):
                    last_reported = done
                    to_report = (done, total)
            if to_report is not None:
                await self._on_progress(*to_report)

        if self._on_progress is not None:
            await self._on_progress(0, total)

        async def fetch_ticker(member: dict) -> Ticker | None:
            try:
                isin = member.get("isin")
                if not isin:
                    return None
                async with sem:
                    try:
                        instrument = await retry_call(self._finhub.get_instrument, isin)
                    except Exception:
                        logger.warning("Failed to fetch instrument for ISIN %s", isin)
                        return None
                if instrument is None:
                    logger.warning("No instrument found for ISIN %s", isin)
                    return None
                security_type = (instrument.get("details") or {}).get("security_type", "")
                if security_type == "ADR":
                    self._adr_isins.add(isin)
                    logger.info("ADR included %s (%s) — verify warrant availability at comdirect", isin, member.get("name"))
                identifiers = instrument.get("global_identifiers") or {}
                symbol = identifiers.get("symbol_yfinance") or identifiers.get("symbol_comdirect")
                if not symbol:
                    logger.warning("No yfinance symbol for ISIN %s", isin)
                    return None
                return Ticker(symbol=symbol, isin=isin, name=member.get("name"))
            finally:
                await mark_progress()

        results = await asyncio.gather(*[fetch_ticker(m) for m in members], return_exceptions=True)
        tickers: list[Ticker] = []
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("Instrument fetch error: %s", r)
            elif r is not None:
                tickers.append(r)

        logger.info("FinHub: %d/%d tickers resolved for %r", len(tickers), len(members), index_name)
        return tickers
