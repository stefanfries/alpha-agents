import logging

from app.agents.base import Agent
from app.models.signals import ResearchResult, SelectionResult

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

        rank_changes, history_labels = self._rank_changes(input, scores)

        logger.info("Screening complete: %d/%d tickers selected", len(selected), len(input.tickers))
        return SelectionResult(
            selected=selected,
            scores=scores,
            rationale=rationale,
            rank_changes=rank_changes,
            history_labels=history_labels,
        )

    def _rank_changes(
        self, input: ResearchResult, current_scores: dict[str, float]
    ) -> tuple[dict[str, list[int | None]], list[str]]:
        current_ranks = {
            sym: i + 1
            for i, (sym, _) in enumerate(
                sorted(current_scores.items(), key=lambda x: x[1], reverse=True)
            )
        }
        hist_ranks_list: list[dict[str, int]] = []
        valid_labels: list[str] = []
        for offset, label in [(5, "1W"), (10, "2W")]:
            hist_scores = {
                sym: self._score(input.fundamentals.get(sym, {}), input.bars.get(sym, []), offset)
                for sym in current_ranks
                if len(input.bars.get(sym, [])) > offset + 20
            }
            if not hist_scores:
                continue
            hist_ranks = {
                sym: i + 1
                for i, (sym, _) in enumerate(
                    sorted(hist_scores.items(), key=lambda x: x[1], reverse=True)
                )
            }
            hist_ranks_list.append(hist_ranks)
            valid_labels.append(label)

        rank_changes = {
            sym: [
                (hist_ranks[sym] - cur_rank if sym in hist_ranks else None)
                for hist_ranks in hist_ranks_list
            ]
            for sym, cur_rank in current_ranks.items()
        }
        return rank_changes, valid_labels

    def _score(self, fundamentals: dict, bars: list, offset: int = 0) -> float:
        score = 0.0

        pe = fundamentals.get("trailingPE")
        if pe and 0 < pe < 25:
            score += 1.0

        ref = len(bars) - 1 - offset
        if ref >= 20:
            recent_close = float(bars[ref].close)
            older_close = float(bars[ref - 20].close)
            if older_close > 0:
                momentum = (recent_close - older_close) / older_close
                score += max(0.0, momentum * 10)

        return score
