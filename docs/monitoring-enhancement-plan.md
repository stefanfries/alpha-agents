# Monitoring Agent Enhancement Plan — Warrant Degradation Health Checks

**Status:** Planned for Phase M1 (next session)  
**Date Created:** 2026-06-22  
**Priority:** High (risk management for trend-following strategy)

---

## Motivation

Currently, the monitoring agent only sells a warrant when the underlying triggers a BREAK signal. However, trend-following is vulnerable to warrant quality degradation — a warrant can become unsuitable while the underlying trend remains intact:

- Spread widens → slippage erodes small gains
- Leverage decays → position size becomes too small
- Maturity shortens → liquidity disappears, exercise risk rises
- Delta drifts → warrant no longer tracks underlying efficiently

**Solution:** Extend monitoring to score held warrants and enforce sells when quality drops below holding thresholds.

---

## Key Difference: Entry vs. Holding Thresholds

Entry selection (Warrant Selection stage) and holding evaluation (Monitoring stage) must use **separate scoring parameters**:

| Component | Entry Selection | Holding Evaluation |
| --------- | --------------- | ------------------ |
| Spread | 0%–3% acceptable | > 2.5% triggers SELL |
| Leverage | Peak at 5x (Gaussian) | < 3x or > 8x triggers SELL |
| Days to Maturity | 9–12 months ideal | < 60 days triggers SELL |
| Delta | Peak at 0.5 (linear) | < 0.3 or > 0.7 triggers SELL |

**Rationale:** Entry is selective (filter 100 candidates → 20); holding is defensive (keep alive positions → sell only if degraded).

---

## Architecture

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
```
MONITORING__WARRANT_HEALTH__ENABLED=true
MONITORING__WARRANT_HEALTH__SPREAD_MAX_PCT=2.5
MONITORING__WARRANT_HEALTH__LEVERAGE_MIN=3.0
MONITORING__WARRANT_HEALTH__LEVERAGE_MAX=8.0
MONITORING__WARRANT_HEALTH__MIN_DAYS_TO_MATURITY=60
MONITORING__WARRANT_HEALTH__DELTA_MIN=0.3
MONITORING__WARRANT_HEALTH__DELTA_MAX=0.7
MONITORING__WARRANT_HEALTH__MIN_WARRANT_SCORE=0.5
```

### 2. New Sell Reason: `warrant_degraded`

**File:** `app/models/signals.py`

```python
class PositionReview(BaseModel):
    underlying_symbol: str
    warrant_isin: str
    warrant_wkn: str
    held_since: date | None = None
    sell_reason: Literal["exit_signal", "warrant_degraded", None] = None
    degrade_details: str | None = None  # e.g., "spread_too_wide", "maturity_too_short"
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
    review.sell_reason = "warrant_degraded"
    review.degrade_details = degrade_detail
    positions_to_sell.append(review)
    logger.info("Monitoring: warrant degraded %s (%s) → SELL", warrant_isin, degrade_detail)
elif has_exit_signal and holding_days >= self._min_holding_days:
    review.sell_reason = "exit_signal"
    positions_to_sell.append(review)
    logger.info("Monitoring: exit signal %s → SELL", underlying_sym)
else:
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

New helper method:
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
```

---

## Implementation Roadmap

### Phase M1.1: Configuration & Model Updates
- [ ] Add `MonitoringWarrantHealthSettings` to `app/config.py`
- [ ] Add `warrant_degraded` sell reason to `PositionReview` model
- [ ] Add `WarrantSnapshot` input model to `MonitoringInput`
- [ ] Update `.env` with new warrant health config params

### Phase M1.2: Agent Logic
- [ ] Implement `_check_warrant_health()` method in `MonitoringAgent`
- [ ] Integrate health checks into `run()` decision tree
- [ ] Update logging to include degradation details

### Phase M1.3: Orchestrator Data Flow
- [ ] Implement `_fetch_warrant_snapshots()` helper
- [ ] Wire snapshots into `MonitoringInput` in `_run_monitoring()`
- [ ] Handle missing/stale quote data gracefully

### Phase M1.4: Testing
- [ ] Add unit tests for `_check_warrant_health()` — boundary cases (spread at threshold, maturity edge)
- [ ] Add integration tests for combined decision logic (exit_signal vs. warrant_degraded priority)
- [ ] Add regression tests to ensure non-degraded warrants are kept when no exit signal

### Phase M1.5: UI & Documentation
- [ ] Update [docs/agents/monitoring.md](docs/agents/monitoring.md) with warrant health check details
- [ ] Extend monitoring results table to show degrade_details column
- [ ] Add configuration guide to [docs/agents/monitoring.md](docs/agents/monitoring.md)

---

## Decision Priority

**When both conditions are true:**

```
if is_degraded:
    → SELL (warrant_degraded)
elif has_exit_signal AND holding_days >= min_holding_days:
    → SELL (exit_signal)
else:
    → KEEP
```

**Rationale:** Warrant health is a hard floor (non-negotiable). Trend signal is softer (respects grace period).

---

## Edge Cases & Fallbacks

| Scenario | Behavior |
| -------- | -------- |
| Warrant snapshot missing (YFinance unavailable) | Log warning, skip health check, keep position (err on side of caution) |
| Partial snapshot (e.g., spread available but delta missing) | Check only available components |
| Days to maturity = 0 (expired) | Immediate SELL (hard error, not soft threshold) |
| Leverage or spread = None in tool response | Treat as "data unavailable", don't trigger sell |

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
- ✅ Health checks don't break existing exit_signal logic
- ✅ Dry-run mode allows testing without live trades
- ✅ All 80 existing tests still pass; 10+ new tests added
- ✅ Monitoring results display degrade_details for transparency
