# Monitoring Agent Enhancement Plan — Warrant Degradation Health Checks

**Status:** M0 implemented; M1 planned  
**Date Created:** 2026-06-22  
**Priority:** High (risk management for trend-following strategy)

---

## Motivation

Currently, the monitoring agent only sells a warrant when the underlying triggers a BREAK signal. However, trend-following is vulnerable to warrant quality degradation — a warrant can become unsuitable while the underlying trend remains intact:

- Spread widens → slippage erodes small gains
- Leverage decays → position size becomes too small
- Maturity shortens → liquidity disappears, exercise risk rises
- Delta drifts → warrant no longer tracks underlying efficiently

**Solution:** Extend monitoring to score held warrants and enforce exits when quality drops below holding thresholds. When a warrant degrades but the underlying trend remains strong, **roll** the warrant: sell the degraded warrant and buy a new one with adjusted strike price and maturity date for the same underlying.

**Three-state model:**

- **HOLD** → Keep warrant (healthy, trend intact)
- **SELL** → Exit position (warrant degraded AND trend broken)
- **ROLL** → Replace warrant (warrant degraded BUT trend still strong) → sell old, buy new for same underlying

---

## Key Difference: Entry vs. Holding Thresholds

Entry selection (Warrant Selection stage) and holding evaluation (Monitoring stage) must use **separate scoring parameters**:

| Component | Entry Selection | Holding Evaluation |
| --------- | --------------- | ------------------ |
| Spread | 0%–3% acceptable | > 2.5% triggers SELL |
| Leverage | Peak at 5x (Gaussian) | < 3x or > 8x triggers SELL |
| Days to Maturity | 9–15 months ideal (target = midpoint of selected range) | < 60 days triggers SELL |
| Delta | Peak at 0.5 (linear) | < 0.3 or > 0.7 triggers SELL |

**Rationale:** Entry is selective (filter 100 candidates → 20); holding is defensive (keep alive positions → sell only if degraded).

---

## Architecture

### 0. First-Run Mapping Robustness (Prerequisite) — Implemented

Before health checks/ROLL logic can work reliably, monitoring must resolve held
warrants to underlying symbols even when no prior approved warrant-selection
run exists.

**Problem addressed:** Monitoring previously relied on a prebuilt map from the last approved
`warrant_selection` stage. On first run (or when holdings missed ISIN), underlyings
could remain unresolved and BREAK/SELL could not be applied to those holdings.

**Implemented fallback chain (layered):**

1. Use prebuilt map from last approved warrant selection (existing behavior)
2. Use persisted cache map (`warrant_underlying_map` collection)
3. Resolve missing rows via FinHub `GET /v1/instruments/{identifier}`
     - identifier preference: `isin` -> `wkn`
     - if instrument is a warrant and underlying metadata is present, extract underlying symbol
4. Persist resolved mapping for subsequent runs (ISIN and WKN keys)

**Data model (new collection):**

```json
{
    "_id": "<warrant_isin_or_wkn>",
    "warrant_isin": "DE000...",
    "warrant_wkn": "PM3ZQF",
    "underlying_symbol": "WDC",
    "underlying_isin": "US9581021055",
    "underlying_name": "Western Digital",
    "source": "finhub_instruments_fallback",
    "resolved_from": "isin|wkn",
    "checked_at": "2026-06-26T...Z"
}
```

**Monitoring behavior for unresolved rows:**

- Keep safe default (do not force SELL without resolved underlying)
- Show explicit warning block in monitoring UI:
    -- unresolved count
    -- identifiers (WKN/ISIN)
    -- note that BREAK/SELL evaluation was skipped

**Name resolution implemented for monitoring UI:**

- Resolve held-warrant underlying ISIN via FinHub `/instruments`
- Prefer shorter universe names by matching underlying ISIN to screening universe
- Fallback to cached warrant-derived `underlying_name` only when universe name is unavailable

**Rationale:**

- Ensures first-run monitoring can still resolve holdings and apply trend exits
- Avoids silent false negatives in SELL detection
- Reduces API calls after first resolution via persisted cache

### 1. New Config Class: `MonitoringWarrantHealthSettings`

**File:** `app/config.py`

```python
class MonitoringWarrantHealthSettings(BaseModel):
    """Warrant health checks for held positions (independent of entry thresholds)."""
    
    enabled: bool = True  # Feature flag
    
    # Spread (%)
    spread_max_pct: float = 2.5
    
    # Leverage (ratio)
    leverage_min: float = 3.0
    leverage_max: float = 8.0
    
    # Days to maturity (absolute floor)
    min_days_to_maturity: int = 60
    
    # Delta (0.0–1.0)
    delta_min: float = 0.3
    delta_max: float = 0.7
    
    # Warrant score (0.0–1.0, optional; if set, overrides component thresholds)
    min_warrant_score: float | None = None


class MonitoringSettings(BaseModel):
    # Existing fields
    min_holding_days: int = 5
    re_entry_prevention_days: int = 10
    
    # New fields
    warrant_health: MonitoringWarrantHealthSettings = Field(
        default_factory=MonitoringWarrantHealthSettings
    )
```

**Environment variables (`.env`):**

```text
MONITORING__WARRANT_HEALTH__ENABLED=true
MONITORING__WARRANT_HEALTH__SPREAD_MAX_PCT=2.5
MONITORING__WARRANT_HEALTH__LEVERAGE_MIN=3.0
MONITORING__WARRANT_HEALTH__LEVERAGE_MAX=8.0
MONITORING__WARRANT_HEALTH__MIN_DAYS_TO_MATURITY=60
MONITORING__WARRANT_HEALTH__DELTA_MIN=0.3
MONITORING__WARRANT_HEALTH__DELTA_MAX=0.7
MONITORING__WARRANT_HEALTH__MIN_WARRANT_SCORE=0.5
```

### 2. Extended Action Types: HOLD, SELL, ROLL

**File:** `app/models/signals.py`

```python
class PositionReview(BaseModel):
    underlying_symbol: str
    warrant_isin: str
    warrant_wkn: str
    held_since: date | None = None
    action: Literal["hold", "sell", "roll"] = "hold"  # Monitoring recommendation
    exit_reason: Literal["exit_signal", "warrant_degraded"] | None = None
    degrade_details: str | None = None  # e.g., "spread_too_wide:2.8%", "maturity_too_short:45d"
    roll_replacement: dict | None = None  # NEW: Suggested warrant replacement (isin, strike, maturity)
```

### 3. Extended MonitoringInput with Warrant Snapshots

**File:** `app/agents/monitoring.py`

```python
class WarrantSnapshot(BaseModel):
    """Current warrant quote for health check evaluation."""
    warrant_isin: str
    spread_pct: float | None = None
    leverage: float | None = None
    days_to_maturity: int | None = None
    delta: float | None = None
    bid_ask_midprice: float | None = None  # Fallback if TQ quote missing


class MonitoringInput(BaseModel):
    candidates: list[Ticker]
    scores: dict[str, float]
    trend_signals: dict[str, str | None]
    current_holdings: list[Position]
    warrant_underlying_map: dict[str, str]
    held_since_map: dict[str, date]
    warrant_snapshots: dict[str, WarrantSnapshot]  # NEW: warrant_isin → snapshot
    max_positions: int = 20
```

### 4. Warrant Health Check Logic

**File:** `app/agents/monitoring.py` (new method)

```python
def _check_warrant_health(
    self,
    warrant_isin: str,
    snapshot: WarrantSnapshot,
) -> tuple[bool, str | None]:
    """
    Evaluate held warrant against health thresholds.
    Returns (is_degraded: bool, detail_reason: str | None).
    """
    if not self._warrant_health.enabled:
        return False, None
    
    reasons = []
    
    # Check spread
    if snapshot.spread_pct is not None:
        if snapshot.spread_pct > self._warrant_health.spread_max_pct:
            reasons.append(f"spread_too_wide:{snapshot.spread_pct:.2f}%")
    
    # Check leverage
    if snapshot.leverage is not None:
        if snapshot.leverage < self._warrant_health.leverage_min:
            reasons.append(f"leverage_too_low:{snapshot.leverage:.2f}x")
        elif snapshot.leverage > self._warrant_health.leverage_max:
            reasons.append(f"leverage_too_high:{snapshot.leverage:.2f}x")
    
    # Check maturity (hard floor)
    if snapshot.days_to_maturity is not None:
        if snapshot.days_to_maturity < self._warrant_health.min_days_to_maturity:
            reasons.append(f"maturity_too_short:{snapshot.days_to_maturity}d")
    
    # Check delta
    if snapshot.delta is not None:
        if snapshot.delta < self._warrant_health.delta_min:
            reasons.append(f"delta_too_low:{snapshot.delta:.3f}")
        elif snapshot.delta > self._warrant_health.delta_max:
            reasons.append(f"delta_too_high:{snapshot.delta:.3f}")
    
    is_degraded = len(reasons) > 0
    detail = " | ".join(reasons) if reasons else None
    return is_degraded, detail
```

### 5. Updated Monitoring Decision Logic

**File:** `app/agents/monitoring.py` (updated `run()` method)

```python
# In the position evaluation loop:
trend_signal = input.trend_signals.get(underlying_sym)
has_exit_signal = trend_signal == "BREAK"

# NEW: Check warrant health
warrant_snapshot = input.warrant_snapshots.get(warrant_isin)
is_degraded, degrade_detail = False, None
if warrant_snapshot:
    is_degraded, degrade_detail = self._check_warrant_health(warrant_isin, warrant_snapshot)

review = PositionReview(
    underlying_symbol=underlying_sym,
    warrant_isin=warrant_isin,
    warrant_wkn=warrant_wkn,
    held_since=held_since,
)

# Decision tree:
if is_degraded:
    if has_exit_signal and holding_days >= self._min_holding_days:
        # Trend broken + warrant degraded → SELL (exit completely)
        review.action = "sell"
        review.exit_reason = "exit_signal"
        review.degrade_details = degrade_detail
        positions_to_sell.append(review)
        logger.info("Monitoring: exit signal + degraded %s → SELL", underlying_sym)
    else:
        # Trend intact but warrant degraded → ROLL (replace warrant)
        review.action = "roll"
        review.exit_reason = "warrant_degraded"
        review.degrade_details = degrade_detail
        # NEW: Query Warrant Selection for replacement candidates
        replacement = await self._find_roll_replacement(underlying_sym, current_warrant_specs)
        review.roll_replacement = replacement
        positions_to_roll.append(review)  # NEW list
        logger.info("Monitoring: degraded but trend intact %s → ROLL", underlying_sym)
elif has_exit_signal and holding_days >= self._min_holding_days:
    # Warrant healthy but trend broken → SELL
    review.action = "sell"
    review.exit_reason = "exit_signal"
    positions_to_sell.append(review)
    logger.info("Monitoring: exit signal %s → SELL", underlying_sym)
else:
    # Warrant healthy, trend intact → HOLD
    review.action = "hold"
    positions_to_keep.append(review)
    logger.debug("Monitoring: keeping %s", underlying_sym)
```

### 6. Orchestrator Changes

**File:** `app/orchestrator.py`

`_run_monitoring()` must now fetch current warrant snapshots for all held ISINs:

```python
async def _run_monitoring(self, run: dict) -> MonitoringResult:
    current_holdings = await self._fetch_holdings(run)
    warrant_isins = [pos.ticker.isin for pos in current_holdings if pos.ticker.isin]
    
    # NEW: Fetch warrant snapshots for health checks
    warrant_snapshots = {}
    if warrant_isins:
        warrant_snapshots = await self._fetch_warrant_snapshots(warrant_isins)
    
    # ... rest of method
    monitoring_input = MonitoringInput(
        # ... existing fields
        warrant_snapshots=warrant_snapshots,  # NEW
    )
```

New helper methods:

```python
async def _fetch_warrant_snapshots(
    self,
    warrant_isins: list[str],
) -> dict[str, WarrantSnapshot]:
    """Fetch current warrant quote data (spread, leverage, maturity, delta) for health checks."""
    # Implementation: Query YFinance tool (or Comdirect for real depots)
    # For each ISIN: parse bid, ask, expiry, underlying quote → compute spread %, leverage, etc.
    # Return dict of {isin → WarrantSnapshot}
    pass

async def _find_roll_replacement(
    self,
    underlying_symbol: str,
    current_warrant_specs: dict,  # {spread_pct, leverage, days_to_maturity, delta}
) -> dict | None:
    """Find a better warrant for the same underlying.
    Query from WarrantSelectionAgent with refresh filters.
    Return {isin, wkn, strike, maturity, projected_leverage, projected_spread} or None if none available.
    """
    # Delegate to WarrantSelectionAgent logic (wrapped as tool)
    pass
```

---

## Implementation Roadmap

### Phase M0: Mapping Fallback & Persistence (must ship first)

- [x] Add persistent `warrant_underlying_map` collection + index in `app/db.py`
- [x] Extend orchestrator mapping fetch to merge:
    -- prior approved run map
    -- persisted cache map
    -- FinHub `/instruments` fallback for unresolved holdings
- [x] Resolve by both identifiers when available (`isin`, `wkn`)
- [x] Persist resolved mappings with `checked_at`, `source`, `resolved_from`
- [x] Add monitoring UI warning panel for unresolved holdings
- [ ] Add tests:
    -- no prior approved warrant-selection run -> fallback resolves
    -- missing ISIN but valid WKN -> fallback resolves
    -- unresolved after fallback -> shown in UI and excluded from SELL logic

### Phase M1.1: Configuration & Model Updates

- [x] Add `MonitoringWarrantHealthSettings` to `app/config.py`
- [x] Add `warrant_degraded` sell reason to `PositionReview` model
- [x] Add `WarrantSnapshot` input model to `MonitoringInput`
- [x] Update `.env` with new warrant health config params

### Phase M1.2: Agent Logic

- [x] Implement `_check_warrant_health()` method in `MonitoringAgent`
- [x] Integrate health checks into `run()` decision tree
- [x] Update logging to include degradation details

### Phase M1.3: Orchestrator Data Flow

- [x] Implement `_fetch_warrant_snapshots()` helper
- [x] Wire snapshots into `MonitoringInput` in `_run_monitoring()`
- [x] Handle missing/stale quote data gracefully

### Phase M1.4: Testing

- [x] Add unit tests for `_check_warrant_health()` — boundary cases (spread at threshold, maturity edge)
- [x] Add integration tests for combined decision logic (exit_signal vs. warrant_degraded priority)
- [x] Add regression tests to ensure non-degraded warrants are kept when no exit signal

### Phase M1.5: Warrant Replacement Logic (ROLL)

- [ ] Implement `_find_roll_replacement()` helper to query WarrantSelectionAgent
- [ ] Add `positions_to_roll` output list to `MonitoringResult`
- [ ] Extend `PositionReview` with `roll_replacement` field (suggested warrant ISIN, strike, maturity)
- [ ] Add decision tree logic for ROLL vs. SELL based on trend signal presence
- [ ] Wire replacement warrant suggestions into monitoring result

### Phase M1.6: UI & Documentation

- [ ] Update [docs/agents/monitoring.md](docs/agents/monitoring.md) with three-state decision logic (HOLD, SELL, ROLL)
- [ ] Update monitoring results template to show action column (HOLD | SELL | ROLL) with color coding
- [ ] Add `roll_replacement` details panel (suggested ISIN, strike, maturity, projected improvement)
- [ ] Add configuration guide with holding warrant thresholds
- [ ] Document ROLL workflow and manual approval requirements

---

## Decision Priority (Three-State Model)

**Evaluation order:**

1. **Warrant health first** — Is warrant degraded?
   - YES + Exit signal (trend broken) → **SELL** (exit completely)
   - YES + No exit signal (trend intact) → **ROLL** (replace warrant, stay in trade)
   - NO + Exit signal + grace period met → **SELL** (exit on trend)
   - NO + Exit signal + grace period not met → **HOLD** unless BREAK is candle-confirmed on two consecutive closed candles
   - NO + No exit signal → **HOLD** (keep position)

**Matrix:**

| Warrant Degraded | Exit Signal | Grace Period | Action |
| --- | --- | --- | --- |
| ✓ | ✓ | ✓ | **SELL** (trend broken) |
| ✓ | ✓ | ✗ | **SELL** (trend broken; do not preserve exposure) |
| ✓ | ✗ | — | **ROLL** (trend intact, just swap warrant) |
| ✗ | ✓ | ✓ | **SELL** (clean exit) |
| ✗ | ✓ | ✗ | **HOLD** (await grace period, unless candle-confirmed BREAK) |
| ✗ | ✗ | — | **HOLD** (no action) |

**Candle confirmation rule (anti-overtrading during development):**

- Same-day re-runs do not count as confirmation.
- Confirmed BREAK requires BREAK on two consecutive closed candles.
- Implementation compares the previous run's BREAK candle date with the current run's previous candle date.

**Rationale:**

- Warrant health is a **hard floor** (non-negotiable risk management)
- Trend signal is **softer** (respects grace period)
- ROLL preserves trend exposure while eliminating execution risk (spread, leverage, maturity)

---

## Edge Cases & Fallbacks

| Scenario | Behavior |
| -------- | -------- |
| Warrant snapshot missing (YFinance unavailable) | Log warning, skip health check, keep position (err on side of caution) |
| Partial snapshot (e.g., spread available but delta missing) | Check only available components |
| Days to maturity = 0 (expired) | Immediate SELL (hard error, not soft threshold) |
| Leverage or spread = None in tool response | Treat as "data unavailable", don't trigger sell |
| No replacement warrant found for ROLL | Downgrade to SELL (preserve capital rather than stay in degraded warrant) |
| Replacement warrant is worse than current | Keep current (HOLD) until health triggers again |
| User rejects ROLL suggestion | Position moves to manual queue (requires override approval) |

---

## Future Considerations (Post-M1)

- **Warrant list refresh:** If better warrants become available (lower spread, higher leverage), trigger "replacement opportunity" signal
- **Maturity ladder:** Stagger maturity ranges by portfolio slot to ensure continuous coverage
- **Historical tracking:** Store degradation events for analysis (e.g., "how often did spread widening predict poor fills?")
- **Alerts:** Real-time Slack/email alerts when warrant health degrades, allowing manual intervention before next monitoring run

---

## Success Criteria

- ✅ Held warrants are scored independently of entry thresholds
- ✅ Degradation triggers are configurable per component
- ✅ Three-state model (HOLD, SELL, ROLL) correctly routes decisions
- ✅ ROLL replacements suggest better warrants for same underlying
- ✅ Health checks don't break existing exit_signal logic
- ✅ Dry-run mode allows testing ROLL suggestions without live trades
- ✅ All 80 existing tests still pass; 15+ new tests added (health checks + ROLL logic)
- ✅ Monitoring results display action (HOLD/SELL/ROLL) with degradation details and replacement suggestions
- ✅ UI template shows three-state table with color coding (green=HOLD, red=SELL, blue=ROLL)

## Terminology

- **Roll** / **Rolling** — Replace an expiring or degraded warrant with a new one at extended maturity/adjusted strike for the same underlying (English: standard finance term; German: "rollen" or "durchrollen")
- **Roll forward** — Extend position to later expiration date
- **Warrant health** — Collective assessment of spread, leverage, maturity, delta against holding thresholds
