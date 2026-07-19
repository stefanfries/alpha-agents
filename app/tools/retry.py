"""Shared retry policy for transient external-API failures.

All external data sources (finhub, yfinance) occasionally fail or throttle.
This module centralises the retry parameters so they are tuned in one place
instead of being hand-rolled at each call site.
"""

import logging

from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

ATTEMPTS = 3  # 1 original call + 2 retries
WAIT_SECONDS = 2.0  # fixed wait, used by yfinance throttle retry
WAIT_MIN_SECONDS = 2.0  # exponential backoff floor: ~2s, then ~4s
WAIT_MAX_SECONDS = 8.0


async def retry_call(
    fn,
    *args,
    non_retry_exceptions: tuple[type[BaseException], ...] = (),
    **kwargs,
):
    """Call an async external-API function, retrying on transient failure.

    Retries on any ``Exception`` with exponential backoff (~2s, ~4s) and re-raises
    the final exception so callers can apply their own fallback::

        candidates = await retry_call(finhub.get_warrants, underlying=isin, ...)
    """
    retry_policy = (
        retry_if_exception(
            lambda exc: isinstance(exc, Exception) and not isinstance(exc, non_retry_exceptions)
        )
        if non_retry_exceptions
        else retry_if_exception_type(Exception)
    )

    return await AsyncRetrying(
        stop=stop_after_attempt(ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=WAIT_MIN_SECONDS, max=WAIT_MAX_SECONDS),
        retry=retry_policy,
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(fn, *args, **kwargs)
