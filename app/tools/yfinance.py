import asyncio
import logging
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf
from tenacity import retry, retry_if_result, stop_after_attempt, wait_fixed

from app.config import settings
from app.models.market import OHLCV, Ticker
from app.tools.base import Tool
from app.tools.retry import ATTEMPTS, WAIT_SECONDS

logger = logging.getLogger(__name__)


class YFinanceTool(Tool):
    def __init__(self) -> None:
        self._resolved_yf_symbols: dict[str, str] = {}

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def fetch_ohlcv_batch(self, tickers: list[Ticker], lookback_days: int) -> dict[str, list[OHLCV]]:
        if not tickers:
            return {}
        self._resolved_yf_symbols = {}
        end = date.today() + timedelta(days=1)
        start = end - timedelta(days=lookback_days) # yfinance's end date is exclusive, so we add one day to include today
        symbol_map = {t.symbol: t for t in tickers}
        symbols = list(symbol_map.keys())

        def _download_with_mapping(yf_to_original: dict[str, str]) -> dict[str, list[OHLCV]]:
            yf_symbols = list(yf_to_original.keys())

            def _missing(df) -> bool:
                # yfinance silently drops throttled symbols (no exception); a
                # missing column means the download should be retried.
                if df is None or df.empty:
                    return True
                present = set(df.columns.get_level_values(0))
                return any(s not in present for s in yf_symbols)

            @retry(
                stop=stop_after_attempt(ATTEMPTS),
                wait=wait_fixed(WAIT_SECONDS),
                retry=retry_if_result(_missing),
                retry_error_callback=lambda rs: rs.outcome.result(),  # keep partial data
            )
            def _fetch():
                return yf.download(
                    yf_symbols, start=start, end=end, progress=False, auto_adjust=True, group_by="ticker"
                )

            df = _fetch()
            result: dict[str, list[OHLCV]] = {}
            for yf_symbol, original_symbol in yf_to_original.items():
                ticker = symbol_map[original_symbol]
                try:
                    sub = df[yf_symbol]
                except KeyError:
                    continue
                sub = sub.dropna(subset=["Open", "High", "Low", "Close"])
                if sub.empty:
                    continue
                bars = [
                    OHLCV(
                        ticker=ticker,
                        date=row_date.date(),
                        open=Decimal(str(row["Open"].item())),
                        high=Decimal(str(row["High"].item())),
                        low=Decimal(str(row["Low"].item())),
                        close=Decimal(str(row["Close"].item())),
                        volume=int(row["Volume"].item()) if row["Volume"] == row["Volume"] else 0,
                    )
                    for row_date, row in sub.iterrows()
                ]
                if bars:
                    result[original_symbol] = bars
            return result

        # Primary pass using the provided symbols (with slash normalization for Yahoo)
        primary_map = {s.replace("/", "-"): s for s in symbols}
        result = await asyncio.to_thread(_download_with_mapping, primary_map)
        for original_symbol in result:
            self._resolved_yf_symbols[original_symbol] = original_symbol.replace("/", "-")

        missing_symbols = [s for s in symbols if s not in result]
        if not missing_symbols:
            return result

        fallback_map: dict[str, tuple[str, str]] = {}
        for original_symbol in missing_symbols:
            ticker = symbol_map[original_symbol]
            candidates: list[tuple[str, str]] = []

            symbol_override = settings.research.yfinance_symbol_overrides_by_symbol.get(original_symbol)
            if symbol_override:
                candidates.append((symbol_override, "symbol_override"))

            if ticker.isin:
                override = settings.research.yfinance_symbol_overrides_by_isin.get(ticker.isin)
                if override:
                    candidates.append((override, "isin_override"))

            # Try stripping exchange suffix (e.g. GRMN.SW -> GRMN) for unresolved symbols.
            if "." in original_symbol:
                candidates.append((original_symbol.split(".", 1)[0], "suffix_strip"))

            for candidate, source in candidates:
                yf_symbol = candidate.replace("/", "-")
                # Keep first candidate per Yahoo symbol to avoid ambiguous remapping.
                fallback_map.setdefault(yf_symbol, (original_symbol, source))
                break

        if fallback_map:
            fallback_download_map = {
                yf_symbol: original_symbol
                for yf_symbol, (original_symbol, _source) in fallback_map.items()
            }
            fallback_result = await asyncio.to_thread(_download_with_mapping, fallback_download_map)
            for original_symbol, bars in fallback_result.items():
                if original_symbol not in result:
                    result[original_symbol] = bars
                    source = "unknown"
                    for _yf_symbol, (mapped_original, mapped_source) in fallback_map.items():
                        if mapped_original == original_symbol:
                            source = mapped_source
                            self._resolved_yf_symbols[original_symbol] = _yf_symbol
                            break
                    logger.info("Recovered OHLCV for %s via %s fallback", original_symbol, source)

        return result

    async def fetch_fundamentals(self, ticker: Ticker) -> dict:
        candidates: list[str] = []

        resolved = self._resolved_yf_symbols.get(ticker.symbol)
        if resolved:
            candidates.append(resolved)

        symbol_override = settings.research.yfinance_symbol_overrides_by_symbol.get(ticker.symbol)
        if symbol_override:
            candidates.append(symbol_override.replace("/", "-"))

        if ticker.isin:
            isin_override = settings.research.yfinance_symbol_overrides_by_isin.get(ticker.isin)
            if isin_override:
                candidates.append(isin_override.replace("/", "-"))

        if "." in ticker.symbol:
            candidates.append(ticker.symbol.split(".", 1)[0].replace("/", "-"))

        candidates.append(ticker.symbol.replace("/", "-"))

        deduped_candidates: list[str] = []
        seen: set[str] = set()
        for c in candidates:
            if c and c not in seen:
                deduped_candidates.append(c)
                seen.add(c)

        def _info(symbol: str) -> dict:
            info = yf.Ticker(symbol).info
            # yfinance can return a near-empty stub dict without raising
            if len(info) <= 2:
                raise ValueError(f"Empty fundamentals stub for {symbol}")
            return info

        last_error: Exception | None = None
        for candidate in deduped_candidates:
            try:
                info = await asyncio.to_thread(_info, candidate)
                if candidate != ticker.symbol:
                    logger.info("Recovered fundamentals for %s via fallback symbol %s", ticker.symbol, candidate)
                return info
            except Exception as exc:
                last_error = exc

        if last_error is not None:
            raise last_error
        raise ValueError(f"Empty fundamentals stub for {ticker.symbol}")
