# ADR-011 — Portfolio Monitoring Stage: Position Review Before New Entries

**Date:** 2026-06-08  
**Status:** Implemented — 2026-06-19  

---

## Context

The existing pipeline covers Research → Screening → Portfolio Construction → Risk → Execution, but has no step that reconciles the *current depot state* with the screening results before generating orders. Without this reconciliation:

- Positions may be bought and sold in rapid succession (whipsawing)
- Exits are not checked before entries, leading to over-allocation
- No distinction is made between "still good, hold" and "degraded, replace"

The user additionally wants entry and exit criteria to be asymmetric (entry requires all criteria, exit triggers on any single failure), and wants a minimum holding period to prevent same-day roundtrips.

---

## Decision

### New pipeline stage: Monitoring

Insert a **Monitoring** stage between Screening and Portfolio Construction (or between Risk and Execution — TBD during implementation based on where depot state is most naturally available).

The stage reads the depot, audits every open position against warrant and underlying criteria, derives how many free slots are available, and passes a structured recommendation to downstream stages.

---

## Processing sequence

```text
1. Load depot positions (real Comdirect depot or virtual depot per QuantSystem config)

2. For each open position:
   a. Check warrant acceptability:
      - Spread % still within threshold
      - Days to maturity above minimum
      - Sufficient liquidity / volume
      → If any warrant check fails: mark for SELL (reason: warrant_degraded)

   b. Check underlying exit criteria (ANY single failure triggers exit):
      - SuperTrend flipped bearish
      - Price closed below EMA20 (fast) or EMA50 (slow) — configurable
      - TSI crossed below its signal line
      - ADX turned down sharply from high level
      - TQ-60 dropped below zero
      → If any exit criterion fires AND holding period >= min_holding_days: mark for SELL (reason: exit_signal)

   c. Incumbent preference — if NEITHER a nor b triggered: keep the position (no action)

3. Derive capacity:
   free_positions = max_positions - len(current_positions) + len(positions_to_sell)
   (sells free up slots that can be filled in the same run)

4. Exclude from entry candidates:
   - Underlyings already held (and not being sold)
   - Underlyings sold in the last N days (re-entry prevention window, configurable)

5. Select top free_positions from Screening results that meet ALL entry criteria:
   - SuperTrend bullish
   - EMA20 rising
   - ADX > threshold and rising
   - Price above EMA50
   - TQ-60 > 0 (positive trend quality)

6. For each selected underlying, run Warrant Selection to find best warrant

7. Pass to Execution:
   - SELL list with reasons
   - BUY list with selected warrants
```

---

## Entry vs. exit criteria asymmetry

| Dimension | Entry | Exit |
| --------- | ----- | ---- |
| Logic | ALL criteria must pass | ANY single criterion fires |
| Strictness | Strict gate | Lenient trigger (prevents holding losers) |
| Grace period | n/a | Yes — only exit if held >= `min_holding_days` |

This asymmetry creates a natural holding bias without needing complex logic.

---

## Key configuration parameters

| Parameter | Suggested default | Notes |
| --------- | ----------------- | ----- |
| `min_holding_days` | 5 | Prevents same-day roundtrip; applies to exit signal check only (not warrant degradation) |
| `re_entry_prevention_days` | 10 | Days after selling an underlying before it can be re-entered |
| `exit_on_any` | `["supertrend", "ema20"]` | Which criteria trigger exit; configurable subset |
| `max_positions` | from QuantSystem config | Already exists |
| `warrant_min_days_to_maturity` | 30 | Below this: mark warrant for SELL |
| `warrant_max_spread_pct` | 1.5 | Above this: mark warrant for SELL |

---

## Stage placement

Preferred: insert between **Screening** and **Warrant Selection**.

```text
Research → Screening → [Monitoring] → Warrant Selection → Portfolio → Risk → Execution
```

The Monitoring stage consumes `SelectionResult` (from Screening) and the depot state, and produces a `MonitoringResult` containing:

- `positions_to_sell: list[PositionReview]` — each with symbol, warrant, reason
- `entry_candidates: list[Ticker]` — filtered and capped to `free_positions`
- `free_positions: int`

Downstream stages (Warrant Selection, Portfolio) operate only on `entry_candidates`, not the full screening list.

---

## Data model (implemented)

```python
class PositionReview(BaseModel):
    underlying_symbol: str              # yfinance symbol; "" if mapping unavailable
    warrant_isin: str
    warrant_wkn: str
    held_since: date | None             # most recent BUY date from virtual_depot_transactions
    sell_reason: Literal["exit_signal"] | None  # None = keep

class MonitoringResult(BaseModel):
    positions_to_sell: list[PositionReview]
    positions_to_keep: list[PositionReview]
    entry_candidates: list[Ticker]      # top-N filtered, capped to free_positions
    free_positions: int                 # max_positions − len(current_holdings)
    excluded_symbols: list[str]         # all held underlyings (kept + selling)
```

### Deviations from the draft sketch

| Draft | Implemented | Reason |
| ----- | ----------- | ------ |
| `sell_reason` includes `"warrant_degraded"` | Only `"exit_signal"` supported | Warrant health checks require FinHub calls; deferred |
| `positions_to_keep: list[str]` (symbols) | `list[PositionReview]` | Richer — carries warrant ISIN for downstream Portfolio use |
| Capital recycling within run | Deferred (free_positions = max − current) | Simpler; avoids same-run whipsawing |
| Re-entry prevention from history | Not yet implemented | Requires transaction history join; `re_entry_prevention_days` stored for future use |

---

## Open questions (to resolve during implementation)

1. **Virtual depot state**: does the virtual depot already track `held_since` dates per position? If not, this needs to be added to the depot data model.
2. **Exit signal computation**: the Monitoring stage needs price/indicator data for each *currently held* underlying. Does the existing `YFinanceTool` + `indicators.py` cover this, or does it need a dedicated lightweight fetch?
3. **Grace period on warrant degradation**: should `min_holding_days` also apply when the warrant (not the underlying) degrades? Probably not — a bad spread should trigger sell immediately.
4. **Partial exits**: for now, assume all-or-nothing per position (sell full position). Partial sizing can be added later.
5. **Capital recycling within same run**: decide whether freed capital from sells is immediately available for buys in the same execution, or whether it is deferred to the next run. Simplest: defer to next run (avoids same-day roundtrip naturally).
