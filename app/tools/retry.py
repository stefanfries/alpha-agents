"""Shared retry policy for transient external-API failures.

All external data sources (finhub, yfinance) occasionally fail or throttle.
This module centralises the retry parameters so they are tuned in one place
instead of being hand-rolled at each call site.
"""

import logging

from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

logger = logging.getLogger(__name__)

ATTEMPTS = 2  # 1 original call + 1 retry
WAIT_SECONDS = 2.0


async def retry_call(fn, *args, **kwargs):
    """Call an async external-API function, retrying once on transient failure.

    Retries on any ``Exception`` with a fixed ``WAIT_SECONDS`` wait and re-raises
    the final exception so callers can apply their own fallback::

        candidates = await retry_call(finhub.get_warrants, underlying=isin, ...)
    """
    return await AsyncRetrying(
        stop=stop_after_attempt(ATTEMPTS),
        wait=wait_fixed(WAIT_SECONDS),
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(fn, *args, **kwargs)
