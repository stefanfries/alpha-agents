"""Persistent uncapped-CALL-warrant availability per ADR underlying ISIN.

Regular stocks reliably have warrants at comdirect, so availability is only
uncertain — and only checked — for **ADRs**. Results are cached in the
pipeline-owned ``warrant_availability`` collection (keyed by underlying ISIN)
and refreshed incrementally: the universe stage only (re)checks ADR ISINs that
are unknown or older than ``STALE_DAYS``.

An entry may carry a manual ``override_isin`` — the ISIN to use for warrant
lookup instead of the member's own ISIN (e.g. an ADR whose underlying stock
carries the warrants). The override affects warrant lookup only; the yfinance
symbol and price candles always stay on the original instrument.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import warrant_availability_collection
from app.tools.finhub import FinHubTool
from app.tools.retry import retry_call

logger = logging.getLogger(__name__)

STALE_DAYS = 30          # re-check a cached result older than this
DETAIL_SAMPLE_K = 10     # max warrant details fetched to confirm an uncapped CALL
DETAIL_CONCURRENCY = 3   # parallel detail fetches per underlying (avoid rate limits)
SCAN_CONCURRENCY = 5     # underlyings checked in parallel


async def _has_uncapped_call(finhub: FinHubTool, isin: str) -> bool:
    """True if the underlying has at least one uncapped CALL warrant.

    The full FinHub maturity range (``Range_NOW`` … ``Range_ENDLESS``) is used —
    availability does not depend on the narrower maturity window applied later in
    warrant selection. A maturity range must be supplied because the FinHub
    ``/v1/warrants`` endpoint returns no results without one. Any strike is
    accepted. The capped flag only appears in warrant detail, so up to
    ``DETAIL_SAMPLE_K`` candidates are inspected.

    Raises if availability cannot be determined (e.g. every detail fetch failed),
    so the caller does not cache a false "none".
    """
    try:
        candidates = await retry_call(
            finhub.get_warrants,
            underlying=isin, preselection="CALL",
            maturity_from="Range_NOW", maturity_to="Range_ENDLESS",
        )
    except Exception:
        logger.warning("availability: get_warrants failed for %s after retry", isin)
        raise

    sample = [c["isin"] for c in candidates if c.get("isin")][:DETAIL_SAMPLE_K]
    if not sample:
        return False  # genuinely no CALL warrants for this underlying

    detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def fetch_detail(wisin: str) -> dict | None:
        async with detail_sem:
            try:
                return await retry_call(finhub.get_warrant_detail, wisin)
            except Exception:
                return None

    details = await asyncio.gather(*[fetch_detail(i) for i in sample])
    fetched = [d for d in details if isinstance(d, dict)]
    if not fetched:
        # All detail fetches failed — availability is unknown, not "none".
        raise RuntimeError(f"availability: all {len(sample)} detail fetches failed for {isin}")
    return any(not (d.get("reference_data") or {}).get("is_capped") for d in fetched)


def _is_stale(doc: dict | None, field: str = "checked_at") -> bool:
    if not doc or doc.get(field) is None:
        return True
    checked_at = doc[field]
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return checked_at < datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)


async def scan(
    finhub: FinHubTool,
    tickers: list[Any],
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> None:
    """Check uncapped-CALL availability for every ticker ISIN not already fresh.

    Incremental: only ISINs that are unknown or older than ``STALE_DAYS`` are
    queried. Results are upserted without disturbing any manual override fields.
    """
    coll = warrant_availability_collection()
    members = [t for t in tickers if getattr(t, "isin", None)]
    existing = {
        d["_id"]: d
        async for d in coll.find({"_id": {"$in": [t.isin for t in members]}})
    }
    stale = [t for t in members if _is_stale(existing.get(t.isin))]
    total = len(stale)
    if total == 0:
        if on_progress:
            await on_progress(0, 0)
        return

    logger.info("warrant availability: checking %d/%d ISINs", total, len(members))
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    done = [0]

    async def check_one(ticker: Any) -> None:
        async with sem:
            try:
                available = await _has_uncapped_call(finhub, ticker.isin)
            except Exception:
                done[0] += 1
                if on_progress:
                    await on_progress(done[0], total)
                return
            await coll.update_one(
                {"_id": ticker.isin},
                {"$set": {
                    "symbol": ticker.symbol,
                    "name": ticker.name,
                    "has_uncapped_call": available,
                    "checked_at": datetime.now(timezone.utc),
                    "source": "auto",
                }},
                upsert=True,
            )
        done[0] += 1
        if on_progress:
            await on_progress(done[0], total)

    if on_progress:
        await on_progress(0, total)
    await asyncio.gather(*[check_one(t) for t in stale])
    logger.info("warrant availability: %d ISINs checked", total)


async def availability_map(isins: list[str]) -> dict[str, dict]:
    """Return {isin: availability_doc} for the given ISINs."""
    coll = warrant_availability_collection()
    return {d["_id"]: d async for d in coll.find({"_id": {"$in": isins}})}


async def overrides_map() -> dict[str, str]:
    """Return {original_isin: override_isin} for every entry with an override."""
    coll = warrant_availability_collection()
    return {
        d["_id"]: d["override_isin"]
        async for d in coll.find({"override_isin": {"$ne": None}})
    }


async def set_override(
    finhub: FinHubTool, original_isin: str, override_isin: str
) -> dict:
    """Persist a manual override ISIN and re-check its availability immediately."""
    try:
        available = await _has_uncapped_call(finhub, override_isin)
    except Exception:
        available = False
    await warrant_availability_collection().update_one(
        {"_id": original_isin},
        {"$set": {
            "override_isin": override_isin,
            "override_has_uncapped_call": available,
            "override_checked_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return {"override_isin": override_isin, "override_has_uncapped_call": available}


async def clear_override(original_isin: str) -> None:
    await warrant_availability_collection().update_one(
        {"_id": original_isin},
        {"$set": {"override_isin": None, "override_has_uncapped_call": None, "override_checked_at": None}},
    )
