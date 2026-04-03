import logging

from agents.base import Agent
from models.signals import ResearchResult, SelectionResult

logger = logging.getLogger(__name__)


class SecuritySelectionAgent(Agent[ResearchResult, SelectionResult]):
    name = "screening"

    def __init__(self, top_n: int = 20, min_market_cap_eur: int = 500_000_000) -> None:
        self._top_n = top_n
        self._min_market_cap_eur = min_market_cap_eur

    async def run(self, input: ResearchResult) -> SelectionResult:
        scores: dict[str, float] = {}
        rationale: dict[str, str] = {}

        for ticker in input.tickers:
            symbol = ticker.symbol
            fund = input.fundamentals.get(symbol, {})

            market_cap = fund.get("marketCap", 0)
            if market_cap < self._min_market_cap_eur:
                rationale[symbol] = f"Market cap {market_cap:,.0f} below minimum {self._min_market_cap_eur:,.0f}"
                continue

            score = self._score(fund, input.bars.get(symbol, []))
            scores[symbol] = score
            rationale[symbol] = f"Score: {score:.2f}"

        ranked = sorted(scores, key=lambda s: scores[s], reverse=True)[: self._top_n]
        selected = [t for t in input.tickers if t.symbol in ranked]

        logger.info("Screening complete: %d/%d tickers selected", len(selected), len(input.tickers))
        return SelectionResult(selected=selected, scores=scores, rationale=rationale)

    def _score(self, fundamentals: dict, bars: list) -> float:
        score = 0.0

        pe = fundamentals.get("trailingPE")
        if pe and 0 < pe < 25:
            score += 1.0

        if len(bars) >= 20:
            recent_close = float(bars[-1].close)
            older_close = float(bars[-20].close)
            if older_close > 0:
                momentum = (recent_close - older_close) / older_close
                score += max(0.0, momentum * 10)

        return score
