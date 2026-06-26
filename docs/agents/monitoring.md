# Agent Spec: Monitoring Agent

## Responsibility

Reconcile the current depot state with the latest screening results before any new warrant is selected or bought. Determine which held positions should be kept, which should be sold, and which screening candidates are eligible for new entry. This is the fourth pipeline stage.

## Input

`MonitoringInput` (defined in `app/agents/monitoring.py`):

```python
class MonitoringInput(BaseModel):
    candidates: list[Ticker]               # from SelectionResult.selected (top-N ranked)
    scores: dict[str, float]               # underlying_symbol → TQ score
    trend_signals: dict[str, str | None]   # underlying_symbol → "NEW" | "HOLD" | "BREAK" | None
  underlying_names: dict[str, str]       # underlying_symbol -> display name
    current_holdings: list[Position]       # depot warrant positions (isin + wkn in ticker); zero-qty skipped
  warrant_underlying_map: dict[str, str] # warrant_isin / warrant_wkn -> underlying_symbol
    held_since_map: dict[str, date]        # warrant_wkn → most recent BUY date
    max_positions: int                     # from execution config override or PortfolioSettings
```

The orchestrator builds `MonitoringInput` from:

- `SelectionResult` (stages.screening.result)
- `underlying_names` from screening universe (`all_tickers` fallback `selected`), then enriched for held rows
- Current depot snapshot via `_fetch_holdings()` — **excludes zero-quantity positions**
- `warrant_underlying_map` from layered resolver via `_fetch_warrant_underlying_map()`:
  - last approved `WarrantSelectionResult`
  - persisted `warrant_underlying_map` cache
  - FinHub `/v1/instruments` fallback resolution (ISIN first, WKN fallback)
- `held_since_map` from `virtual_depot_transactions` via `_fetch_held_since()`
- `max_positions` resolved from execution `config_overrides.portfolio.max_positions`, or falls back to `settings.portfolio.max_positions`

## Output

```python
class MonitoringResult(BaseModel):
    positions_to_sell: list[PositionReview]  # exit signal confirmed → SELL
    positions_to_keep: list[PositionReview]  # no exit trigger → KEEP
    entry_candidates: list[Ticker]           # filtered and capped to free_positions
    free_positions: int                      # max_positions − len(current_holdings)
    excluded_symbols: list[str]              # all held underlyings (blocked from entry)
```

`PositionReview` fields: `underlying_symbol`, `underlying_name`, `warrant_isin`, `warrant_wkn`, `held_since`, `sell_reason` (`"exit_signal"` or `None`).

## Tools used

None — operates on data provided by the orchestrator.

## Behaviour

### For each held position

1. **Resolve underlying symbol** from `warrant_underlying_map[warrant_isin]`, fallback `warrant_underlying_map[warrant_wkn]`.
   - If not found (no mapping available): mark as KEEP with `underlying_symbol=""` (safe default).
2. **Resolve underlying display name** from `underlying_names[underlying_symbol]` when available.
  - Name precedence is enforced by orchestrator:
    - universe name via underlying ISIN
    - cached fallback name
3. **Check holding period**: `holding_days = (today - held_since_map[wkn]).days`. If `held_since` is unknown, `holding_days` defaults to 9999 (never blocks an exit).
4. **Check exit signal**: `trend_signals[underlying_symbol] == "BREAK"`.
5. Decision:
   - `has_exit_signal AND holding_days >= min_holding_days` → **SELL** (`sell_reason="exit_signal"`)
   - Otherwise → **KEEP**

### Entry candidate selection

- **Max positions resolution**: `max_positions` is first resolved from execution `config_overrides.portfolio.max_positions` (if set); otherwise defaults to global `settings.portfolio.max_positions`. This allows per-execution tuning.
- `free_positions = max(0, max_positions − len(current_holdings))`
  - When `current_holdings` is empty, `free_positions = max_positions` (full capacity available)
  - Note: Holdings with quantity ≤ 0 are excluded from the count by `_fetch_holdings()`
- Capital freed by sells is **not** recycled within the same run (deferred approach — prevents same-run whipsawing).
- `excluded_symbols` = all held underlying symbols (kept + selling).
- `entry_candidates` = `[t for t in candidates if t.symbol not in excluded_symbols][:free_positions]`

### Asymmetric entry / exit logic

| Dimension | Entry | Exit |
| --------- | ----- | ---- |
| Criterion | ALL policies must pass (enforced in Screening) | ANY BREAK policy fires |
| Grace period | n/a | `min_holding_days` (default 5) |
| Same-run re-entry | Excluded for any sold underlying | — |

## Configuration (`MonitoringSettings`)

| Key | Default | Notes |
| --- | ------- | ----- |
| `min_holding_days` | `5` | Grace period — exit signal ignored if position held fewer days |
| `re_entry_prevention_days` | `10` | Intended for future re-entry prevention from transaction history (not yet implemented) |

Set via `.env` with `MONITORING__` prefix, e.g. `MONITORING__MIN_HOLDING_DAYS=7`.

## Bug fixes (2026-06-22)

**Issue:** Monitoring reported incorrect free slot counts and incorrectly counted zero-quantity positions as active holdings.

**Root causes:**

1. No-holdings path computed `free_positions = min(candidates, max_positions)` instead of `max_positions`
2. Holdings loader included positions with quantity ≤ 0, inflating the held count
3. Portfolio max positions was hardcoded to global settings, ignoring execution config overrides

**Resolution:**

- `_portfolio_max_positions()` resolver in orchestrator honors execution-level `config_overrides.portfolio.max_positions`
- Holdings fetch skips all zero-quantity or negative-quantity positions
- No-holdings case now correctly returns `free_positions = max_positions`
- All 3 fixes validated by new integration tests

## Known limitations / TODO

- **Warrant health checks** (spread %, days to maturity) are not yet implemented. ADR-011 plans a `warrant_degraded` sell reason; currently only `exit_signal` is supported.
- **Re-entry prevention from history**: `re_entry_prevention_days` is stored but the agent does not yet query transaction history to exclude recently-sold underlyings from entry. Currently only currently-held symbols are excluded.
- **Real depot held-since**: `_fetch_held_since` only reads `virtual_depot_transactions`. For real Comdirect depots the purchase date is not tracked.
