import asyncio
import logging
from typing import Any

from app.models.market import Ticker
from app.tools.base import Tool

logger = logging.getLogger(__name__)

_WIKI_BASE = "https://en.wikipedia.org/wiki/{article}"

# symbol_cols: ordered candidates — first match wins
# Symbols in DAX/EuroStoxx50 already carry exchange suffixes (.DE, .AS, .PA) — suffix = ""
# MDAX/SDAX/TecDAX: Wikipedia no longer publishes a ticker column — omitted (FinHub is primary)
_INDEX_CONFIG: dict[str, dict[str, Any]] = {
    "DAX": {
        "article": "DAX",
        "symbol_cols": ["Ticker"],
        "name_cols": ["Company"],
        "isin_col": None,
        "suffix": "",
    },
    "EuroStoxx50": {
        "article": "EURO_STOXX_50",
        "symbol_cols": ["Ticker"],
        "name_cols": ["Company"],
        "isin_col": None,
        "suffix": "",
    },
    "NASDAQ100": {
        "article": "Nasdaq-100",
        "symbol_cols": ["Ticker", "Symbol"],
        "name_cols": ["Company"],
        "isin_col": None,
        "suffix": "",
    },
    "SP500": {
        "article": "List_of_S%26P_500_companies",
        "symbol_cols": ["Symbol"],
        "name_cols": ["Security"],
        "isin_col": None,
        "suffix": "",
    },
    "FTSE100": {
        "article": "FTSE_100_Index",
        "symbol_cols": ["Ticker"],
        "name_cols": ["Company", "Security"],
        "isin_col": None,
        "suffix": ".L",
    },
}

_ALIASES: dict[str, str] = {
    "EURO STOXX 50": "EuroStoxx50",
    "EUROSTOXX50": "EuroStoxx50",
    "EUROSTOXX 50": "EuroStoxx50",
    "NASDAQ-100": "NASDAQ100",
    "NASDAQ 100": "NASDAQ100",
    "S&P 500": "SP500",
    "SP 500": "SP500",
    "S&P500": "SP500",
    "FTSE 100": "FTSE100",
    "FTSE-100": "FTSE100",
}


class WikipediaIndexTool(Tool):
    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_index_constituents(self, index_name: str) -> list[Ticker]:
        canonical = _ALIASES.get(index_name.upper(), index_name)
        config = _INDEX_CONFIG.get(canonical)
        if config is None:
            logger.warning("Wikipedia: no config for index %r", index_name)
            return []
        return await asyncio.to_thread(self._fetch_sync, config)

    def _fetch_sync(self, config: dict[str, Any]) -> list[Ticker]:
        try:
            import pandas as pd
        except ImportError:
            logger.error("pandas not available — Wikipedia fallback disabled")
            return []

        url = _WIKI_BASE.format(article=config["article"])
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AlphaAgents/1.0)"}
        try:
            tables = pd.read_html(url, flavor="lxml", storage_options=headers)
        except Exception:
            try:
                tables = pd.read_html(url, storage_options=headers)
            except Exception:
                logger.warning(
                    "Wikipedia: failed to fetch %r", config["article"], exc_info=True
                )
                return []

        symbol_col: str | None = None
        isin_col: str | None = config.get("isin_col")
        suffix: str = config.get("suffix", "")
        best_df = None

        for df in tables:
            cols = set(df.columns.tolist())
            matched = next((c for c in config["symbol_cols"] if c in cols), None)
            if matched and (isin_col is None or isin_col in cols):
                best_df = df
                symbol_col = matched
                break

        if best_df is None or symbol_col is None:
            logger.warning("Wikipedia: constituent table not found for %r", config["article"])
            return []

        cols = set(best_df.columns.tolist())
        name_col: str | None = next((c for c in config.get("name_cols", []) if c in cols), None)

        tickers: list[Ticker] = []
        for _, row in best_df.iterrows():
            raw_symbol = str(row[symbol_col]).strip()
            if not raw_symbol or raw_symbol == "nan":
                continue
            symbol = raw_symbol if (not suffix or raw_symbol.endswith(suffix)) else raw_symbol + suffix
            isin: str | None = None
            if isin_col and isin_col in best_df.columns:
                raw_isin = str(row[isin_col]).strip()
                if raw_isin and raw_isin != "nan" and len(raw_isin) == 12:
                    isin = raw_isin
            name: str | None = None
            if name_col:
                raw_name = str(row[name_col]).strip()
                if raw_name and raw_name != "nan":
                    name = raw_name
            tickers.append(Ticker(symbol=symbol, isin=isin, name=name))

        logger.info("Wikipedia: %d tickers from %r", len(tickers), config["article"])
        return tickers
