# Agent Spec: Monitoring Agent

## Responsibility

Reconcile current depot holdings with screening signals and warrant health before new entries are selected. Determine which held positions should be kept, sold, or rolled, and which screening candidates are eligible for entry. This is the fourth pipeline stage.

## Input

`MonitoringInput` (defined in `app/agents/monitoring.py`):

```python
class MonitoringInput(BaseModel):
    candidates: list[Ticker]               # from SelectionResult.selected (top-N ranked)
    scores: dict[str, float]               # underlying_symbol → score
    trend_signals: dict[str, str | None]   # underlying_symbol → "NEW" | "HOLD" | "BREAK" | None
    underlying_names: dict[str, str]       # underlying_symbol -> display name
    current_holdings: list[Position]       # depot warrant positions (isin + wkn in ticker); zero-qty skipped
    warrant_underlying_map: dict[str, str] # warrant_isin / warrant_wkn -> underlying_symbol
    held_since_map: dict[str, date]        # warrant_wkn → most recent BUY date
    warrant_snapshots: dict[str, WarrantSnapshot]  # held warrant health snapshots
    break_confirmed_symbols: set[str]      # symbols with candle-confirmed BREAK
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
- `warrant_snapshots` from FinHub warrant detail via `_fetch_warrant_snapshots()`
- `break_confirmed_symbols` via `_break_confirmed_symbols()` — derived from `SelectionResult.first_break_candle_dates` (see below)
- `max_positions` resolved from execution `config_overrides.portfolio.max_positions`, or falls back to `settings.portfolio.max_positions`

## Output

```python
class MonitoringResult(BaseModel):
    positions_to_sell: list[PositionReview]  # SELL decisions
    positions_to_keep: list[PositionReview]  # HOLD decisions
    positions_to_roll: list[PositionReview]  # ROLL candidates (classification only)
    entry_candidates: list[Ticker]           # filtered and capped to free_positions
    free_positions: int                      # max_positions − len(current_holdings)
    excluded_symbols: list[str]              # all held underlyings (blocked from entry)
    # Metadata for warrant selection integration:
    keep_existing_isins: list[str]
    roll_underlyings: list[str]              # symbols classified as roll candidates
    roll_keep_underlyings: list[str]
```

`PositionReview` fields:

- Identifiers: `underlying_symbol`, `underlying_name`, `warrant_isin`, `warrant_wkn`
- Holding context: `held_since` (date), `sell_reason` (`"exit_signal"` or `"warrant_degraded"`)
- Warrant metrics: `spread_pct`, `leverage`, `delta`, `days_to_maturity` (all optional float)
- Screening diagnostics: `screening_signal` (`"NEW"|"HOLD"|"BREAK"|None`) and `screening_signal_present` (bool)
- Trend status: `trend_status` (UI-ready status label)
- Health assessment: `monitoring_score` (0-1 health score), `warrant_health_status`, `warrant_health_reason`, `decision_reason`

## Tools used

None directly.

## Behaviour

### For each held position

- Resolve underlying symbol from `warrant_underlying_map[warrant_isin]`, fallback `warrant_underlying_map[warrant_wkn]`. If not found (no mapping available), mark as KEEP with `underlying_symbol=""` (safe default).
- Resolve underlying display name from `underlying_names[underlying_symbol]` when available. Name precedence is enforced by orchestrator: universe name via underlying ISIN, then cached fallback name.
- Check holding period: `holding_days = (today - held_since_map[wkn]).days`. If `held_since` is unknown, `holding_days` defaults to 9999 (never blocks an exit).
- Check exit signal from screening:
  - `BREAK` = active break signal (still in the short confirmation window)
  - `None` (`--` in screening UI) = break happened earlier and signal aged out; treated as confirmed earlier break
  - Missing key in `trend_signals` = no signal available for mapped underlying symbol (not auto-sold)
- Evaluate warrant health via `_check_warrant_health()` using available snapshot metrics (`spread_pct`, `leverage`, `days_to_maturity`, `delta`).
- Compute health score via `_monitoring_score()`: weighted 4-component normalization (spread, leverage, maturity, delta)
- **Resolve action via `_decide_action()` with trend-first priority**:
  
  **Step 1: Trend check (exit signal)**
  - `trend_signal is None` (`--`) => **SELL** with reason `"break signal, confirmed earlier"`
  - `trend_signal == BREAK` and confirmed (`is_break_confirmed`) => **SELL** with reason `"trend break confirmed"`
  - `trend_signal == BREAK` and not confirmed => **KEEP** with reason `"break signal, not confirmed yet"` (wait for confirmation)
  
  **Step 2: Warrant health check (only if trend is intact)**
  - Degraded warrant + grace met (`is_degraded AND holding_days >= min_holding_days`) => **ROLL**
  - Otherwise => **KEEP**

**Confirmed BREAK** fires when `SelectionResult.latest_candle_dates[symbol] > SelectionResult.first_break_candle_dates[symbol]` — i.e., at least one new candle has closed since the first BREAK was observed. `first_break_candle_dates` is computed in the Screening stage and persisted to MongoDB (see Orchestrator internals). This mechanism is robust across same-day reruns, public holidays, and weekend gaps: the first-break date is carried forward from prior runs regardless of how many runs were executed between the first BREAK and the confirmation. Same-day confirmation is structurally impossible because `latest > first_break` is `False` when both equal the same date.

**Unconfirmed BREAK** takes precedence: if a BREAK signal fires but hasn't yet confirmed on a second candle, the position is held (not rolled) while waiting for trend confirmation.

### Responsibility boundary

Monitoring is classification-only:

- It determines `SELL` / `KEEP` / `ROLL` candidate status from trend + warrant health.
- It does **not** select replacement warrants.
- Replacement selection happens in the `warrant_selection` stage.

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
| Grace period | n/a | Used only as ROLL grace (not SELL grace) |
| Same-run re-entry | Excluded for any sold underlying | — |

### Three-state decision matrix (trend-first priority)

#### Step 1: Exit signal (trend)

| Confirmed BREAK | Trend Signal | Action | Reason |
| --- | --- | --- | --- |
| ✓ (implicit) | `None` (`--`) with key present | **SELL** | "break signal, confirmed earlier" |
| ✓ | BREAK | **SELL** | "trend break confirmed" |
| ✗ | BREAK | **KEEP** | "break signal, not confirmed yet" (wait for 2nd candle) |
| — | key missing / NEW / HOLD | → Step 2 | Continue to warrant health check |

#### Step 2: Warrant health (only if trend is intact)

| Warrant Degraded | Grace Met (holding_days ≥ min_holding_days) | Action | Reason |
| --- | --- | --- | --- |
| ✓ | ✓ | **ROLL** | `"<health_detail>"` (e.g., "leverage too low: 2.45") |
| ✓ | ✗ | **KEEP** | "degraded but within grace period" |
| ✗ | any | **KEEP** | "warrant healthy, trend intact" |

### Trend reason semantics (KEEP rows)

Reason precedence for KEEP rows:

1. If `_decide_action()` returns a reason, use it (e.g., `"break signal, not confirmed yet"`).
2. If warrant is degraded but still within grace period, use `"degraded but within grace period"`.
3. If trend signal is `NEW` or `HOLD`, use `"warrant healthy, trend intact"`.
4. Otherwise use `"no signal"`.

### Signal state diagnostics

Monitoring exposes two underlying screening diagnostics for each reviewed position:

- `screening_signal_present`: whether the mapped underlying symbol exists in `SelectionResult.trend_signals`
- `screening_signal`: resolved signal value for that symbol (or `None`)

The stage UI derives two user-facing columns from those diagnostics and monitoring checks:

- `Trend status`: `BREAK pending`, `BREAK confirmed`, `BREAK confirmed earlier`, `NEW`, `HOLD`, or `no screening signal`
- `Warrant health`: `healthy`, `degraded` (with detail), or `unknown`
- `Decision rationale`: only non-redundant action context (e.g. `break signal, not confirmed yet`), while duplicate degradation text is shown only once under warrant health

## Configuration (`MonitoringSettings`)

Warrant health thresholds can be adjusted directly in the monitoring stage UI ("Warrant health thresholds" panel above the action bar) without editing `.env`. UI-selected values are persisted to `config_overrides.monitoring.warrant_health` in the execution document and take precedence over the `.env` defaults for that execution.

| Key | Default | Notes |
| --- | ------- | ----- |
| `min_holding_days` | `5` | Grace period before degraded warrants are eligible for ROLL; prevents roll churn on temporary fluctuations |
| `re_entry_prevention_days` | `10` | Intended for future re-entry prevention from transaction history (not yet implemented) |
| `warrant_health.enabled` | `True` | Master switch for warrant health checks |
| `warrant_health.spread_max_pct` | `2.5` | Bid-ask spread threshold for degradation (tighter than entry screening) |
| `warrant_health.leverage_min` | `3.0` | Minimum acceptable leverage |
| `warrant_health.leverage_max` | `8.0` | Maximum acceptable leverage |
| `warrant_health.min_days_to_maturity` | `60` | Minimum days until expiry |
| `warrant_health.delta_min` | `0.3` | Minimum acceptable delta (directional sensitivity) |
| `warrant_health.delta_max` | `0.7` | Maximum acceptable delta |

`MonitoringSettings` also includes `warrant_health` thresholds:

- `enabled` (default `True`)
- `spread_max_pct` (`2.5`)
- `leverage_min` (`3.0`), `leverage_max` (`8.0`)
- `min_days_to_maturity` (`60`)
- `delta_min` (`0.3`), `delta_max` (`0.7`)

Set via `.env` with `MONITORING__` prefix, e.g. `MONITORING__MIN_HOLDING_DAYS=7`, or via the stage UI for per-execution overrides.

### Threshold tuning guide

Use holding thresholds to control roll-candidate cadence independently from entry scoring.

| Variable | Lower value tends to | Higher value tends to |
| --- | --- | --- |
| `MONITORING__MIN_HOLDING_DAYS` | Roll sooner | Roll later (less churn) |
| `MONITORING__WARRANT_HEALTH__SPREAD_MAX_PCT` | Trigger more rolls/sells on spread | Tolerate wider spreads |
| `MONITORING__WARRANT_HEALTH__LEVERAGE_MIN` | Tolerate lower leverage | Trigger earlier on decayed leverage |
| `MONITORING__WARRANT_HEALTH__LEVERAGE_MAX` | Tolerate high leverage | Trigger earlier on over-levered instruments |
| `MONITORING__WARRANT_HEALTH__MIN_DAYS_TO_MATURITY` | Keep shorter-dated warrants longer | Roll earlier to extend maturity |
| `MONITORING__WARRANT_HEALTH__DELTA_MIN` | Tolerate lower directional sensitivity | Trigger earlier on low-delta drift |
| `MONITORING__WARRANT_HEALTH__DELTA_MAX` | Tolerate higher directional sensitivity | Trigger earlier on high-delta drift |

Trend-following baseline profile:

- `MONITORING__MIN_HOLDING_DAYS=7`
- `MONITORING__WARRANT_HEALTH__SPREAD_MAX_PCT=2.5`
- `MONITORING__WARRANT_HEALTH__LEVERAGE_MIN=2.8`
- `MONITORING__WARRANT_HEALTH__LEVERAGE_MAX=8.5`
- `MONITORING__WARRANT_HEALTH__MIN_DAYS_TO_MATURITY=90`
- `MONITORING__WARRANT_HEALTH__DELTA_MIN=0.25`
- `MONITORING__WARRANT_HEALTH__DELTA_MAX=0.80`

This profile is tuned to reduce premature roll churn in persistent trends while still protecting against severe warrant quality drift.

Example:

```dotenv
MONITORING__MIN_HOLDING_DAYS=7
MONITORING__WARRANT_HEALTH__SPREAD_MAX_PCT=2.5
MONITORING__WARRANT_HEALTH__LEVERAGE_MIN=2.8
MONITORING__WARRANT_HEALTH__LEVERAGE_MAX=8.5
MONITORING__WARRANT_HEALTH__MIN_DAYS_TO_MATURITY=90
MONITORING__WARRANT_HEALTH__DELTA_MIN=0.25
MONITORING__WARRANT_HEALTH__DELTA_MAX=0.80
```

### Monitoring KPIs (recommended)

Track these KPIs per approved monitoring run to evaluate parameter fit for trend following:

| KPI | Formula | Why it matters |
| --- | --- | --- |
| `roll_rate` | `len(positions_to_roll) / current_holdings_count` | Detects replacement churn while trend remains intact |
| `sell_rate` | `len(positions_to_sell) / current_holdings_count` | Captures hard exits and trend-break intensity |
| `avg_holding_days_roll` | Mean holding days for rolled positions | Indicates whether rolls happen too early |
| `avg_holding_days_sell` | Mean holding days for sold positions | Distinguishes normal exits from premature exits |

Guardrail suggestions for the baseline profile:

- `roll_rate > 0.20` for 3 consecutive runs -> churn likely too high

### Tuning runbook (one knob at a time)

- Freeze parameters for at least 3-4 weeks (or a statistically meaningful number of runs).
- Review KPIs weekly and compare against guardrails.
- If churn is too high, first increase `MONITORING__WARRANT_HEALTH__DELTA_MAX` by `+0.05` (cap at `0.90`).
- If churn remains elevated after the prior step, increase `MONITORING__MIN_HOLDING_DAYS` by `+2`.
- If risk is too high (positions held too long in degraded state), decrease `MONITORING__WARRANT_HEALTH__DELTA_MAX` by `-0.05`.
- If degraded warrants are held too close to expiry, increase `MONITORING__WARRANT_HEALTH__MIN_DAYS_TO_MATURITY` by `+15`.
- After each single change, run at least 2 weeks before further adjustments.
- Record each change with timestamp, rationale, and expected KPI impact.

## ROLL workflow and manual approval

- Monitoring emits `positions_to_roll` when action resolves to ROLL candidate.
- Stage page shows trend status and warrant health separately for each row.
- In HITL mode, no replacement trade is executed before human approval.
- Replacement warrant discovery and final roll selection happen downstream in `warrant_selection`.

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

- **Re-entry prevention from history**: `re_entry_prevention_days` is stored but the agent does not yet query transaction history to exclude recently-sold underlyings from entry. Currently only currently-held symbols are excluded.
- **Real depot held-since**: `_fetch_held_since` only reads `virtual_depot_transactions`. For real Comdirect depots the purchase date is not tracked.
- **Roll approval controls**: ROLL recommendations are approved at stage level (no per-row accept/reject yet).
