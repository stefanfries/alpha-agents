from math import isfinite
from typing import TypedDict

from app.models.signals import SelectionResult


class ScreeningRank(TypedDict):
    rank: int
    value: float


def build_screening_ranks(
    screening: SelectionResult, ranking_metric: str,
) -> dict[str, ScreeningRank]:
    metric_values = {
        "tq60": screening.scores,
        "tq20": screening.tq_short,
        "tsi": screening.tsi,
    }[ranking_metric]
    ranked_symbols = sorted(
        (symbol for symbol in screening.policy_results
         if symbol in metric_values and isfinite(metric_values[symbol])),
        key=lambda symbol: (-metric_values[symbol], symbol),
    )
    ranks: dict[str, ScreeningRank] = {}
    rank = 0
    previous_value = None
    for index, symbol in enumerate(ranked_symbols, start=1):
        value = metric_values[symbol]
        if value != previous_value:
            rank = index
        ranks[symbol] = {"rank": rank, "value": value}
        previous_value = value
    return ranks
