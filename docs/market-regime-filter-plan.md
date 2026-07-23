# Market Regime Filter — Implementation Plan

## Goal

Add a market regime traffic light (🟢 / 🟡 / 🔴) to the Screening stage that:

- Computes TQ-100 on the index that matches the quant system's investment universe
- Classifies the current market into Green (uptrend), Yellow (sideways), Red (downtrend)
- Displays the signal prominently in the Screening UI
- **Phase 1**: visual / informational only — human still controls Approve
- **Phase 2** (future): feed regime into the Monitoring agent to tighten trailing stops

---

## Index → Yahoo Finance symbol mapping

| System index name | Yahoo Finance symbol |
| --- | --- |
| DAX | `^GDAXI` |
| MDAX | `^MDAXI` |
| SDAX | `^SDAXI` |
| TecDAX | `^TECDAX` |
| EuroStoxx50 | `^STOXX50E` |
| NASDAQ100 | `^NDX` |
| SP500 | `^GSPC` |
| FTSE100 | `^FTSE` |

This mapping lives in a small dict in `app/agents/research.py` (or a shared constant module).

### Mixed universes

When a quant system uses multiple indices (e.g. DAX + NASDAQ100), choose the benchmark
with the **most constituent tickers** in the resolved universe. If tied, use the first one.

---

## TQ window and thresholds

### Why TQ-100, not TQ-60

TQ-60 (~3 months) is calibrated for individual stock selection — it reacts quickly to
short-term momentum. For a *market regime* filter the goal is a stable, slow-moving signal
that does not flip on normal 4–6 week corrections. TQ-100 (~5 months) is the right balance.

### Threshold defaults (configurable)

| Status | Condition | Meaning |
| ------ | --------- | ------- |
| 🟢 Green | TQ-100 > 0.03 | Market in uptrend — normal operation |
| 🟡 Yellow | −0.03 ≤ TQ-100 ≤ 0.03 | Sideways / no clear direction — caution |
| 🔴 Red | TQ-100 < −0.03 | Downtrend — pause new entries |

These thresholds are starting points. Validate by inspecting TQ-100 values on ^NDX history
for known periods:

- 2022 bear market → should read Red
- 2023 recovery → should transition Yellow → Green
- Current sideways (Jul 2026) → should read Yellow

---

## What each state means for the trader (Phase 1: manual)

| State | New BUYs | Existing positions |
| ----- | -------- | ------------------ |
| 🟢 Green | Normal | Normal stops |
| 🟡 Yellow | Pause | Manually consider tightening stops |
| 🔴 Red | Block (warning) | Manually consider closing weakest positions |

Phase 1 is advisory only — the UI warns but does not prevent the human from clicking Approve.

---

## Implementation steps

### Step 1 — `app/models/signals.py`

Add a new model:

```python
from typing import Literal

class MarketRegime(BaseModel):
    symbol: str                              # e.g. "^NDX"
    tq100: float                             # TQ-100 value
    tq60: float                              # TQ-60 value (secondary / early-warning)
    status: Literal["green", "yellow", "red"]
```

Extend `ResearchResult`:

```python
class ResearchResult(BaseModel):
    tickers: list[Ticker]
    bars: dict[str, list[OHLCV]]
    fundamentals: dict[str, dict]
    benchmark_symbol: str = ""               # NEW — e.g. "^NDX"
    benchmark_bars: list[OHLCV] = []         # NEW — OHLCV for the benchmark index
```

Extend `SelectionResult`:

```python
class SelectionResult(BaseModel):
    ...
    market_regime: MarketRegime | None = None   # NEW
```

---

### Step 2 — `app/config.py`

Add to `ScreeningSettings`:

```python
market_regime_lookback: int = 100          # TQ window for regime (TQ-100)
market_regime_tq_green: float = 0.03       # TQ >= this → green
market_regime_tq_red: float = -0.03        # TQ <= this → red
```

Add to `ResearchSettings`:

```python
market_regime_symbols: dict[str, str] = {  # index name → Yahoo symbol
    "DAX":         "^GDAXI",
    "MDAX":        "^MDAXI",
    "SDAX":        "^SDAXI",
    "TecDAX":      "^TECDAX",
    "EuroStoxx50": "^STOXX50E",
    "NASDAQ100":   "^NDX",
    "SP500":       "^GSPC",
    "FTSE100":     "^FTSE",
}
```

---

### Step 3 — `app/agents/research.py`

`ResearchAgent` receives the quant system's `indices: list[str]` (already available via
`UniverseResult.source` or passed explicitly). Add logic:

1. Determine benchmark Yahoo symbol: iterate `input.tickers`, count how many came from each
   index (using `UniverseResult.source`), pick the index with the most tickers, map to Yahoo
   symbol via `ResearchSettings.market_regime_symbols`.
2. Fetch 200 days of OHLCV for that symbol via `self._tool.fetch_ohlcv_batch` (single ticker,
   reuse existing batch method — it accepts a list).
3. Store in `ResearchResult.benchmark_symbol` and `ResearchResult.benchmark_bars`.
4. Failure is non-fatal: log a warning, leave fields empty.

**Note:** `ResearchInput` needs a new optional field:

```python
class ResearchInput(BaseModel):
    tickers: list[Ticker]
    lookback_days: int = 365
    universe_source: dict[str, str] = {}    # ISIN/symbol → index name (from UniverseResult.source)
```

Pass `UniverseResult.source` into `ResearchInput` in `app/orchestrator.py`.

---

### Step 4 — `app/agents/screening.py`

At the **start** of `SecuritySelectionAgent.run()`, before the per-ticker loop:

```python
market_regime: MarketRegime | None = None
if input.benchmark_bars and len(input.benchmark_bars) >= cfg.market_regime_lookback:
    tq100 = self._trend_quality(input.benchmark_bars, cfg.market_regime_lookback)
    tq60  = self._trend_quality(input.benchmark_bars, 60)
    if tq100 >= cfg.market_regime_tq_green:
        status = "green"
    elif tq100 <= cfg.market_regime_tq_red:
        status = "red"
    else:
        status = "yellow"
    market_regime = MarketRegime(
        symbol=input.benchmark_symbol,
        tq100=tq100,
        tq60=tq60,
        status=status,
    )
```

Include `market_regime` in the returned `SelectionResult`.

`ScreeningSettings` needs the new config fields plumbed through (like the existing policy fields).

---

### Step 5 — `app/templates/stages/screening.html`

Add a traffic light badge in the `awaiting_review` block, just above the filter bar row.
Show it only if `r.market_regime` is not None.

```html
{% if r.market_regime %}
  {% set regime = r.market_regime %}
  {% if regime.status == "green" %}
    {% set regime_color = "success" %}
    {% set regime_icon  = "🟢" %}
    {% set regime_label = "Uptrend — new entries enabled" %}
  {% elif regime.status == "yellow" %}
    {% set regime_color = "warning" %}
    {% set regime_icon  = "🟡" %}
    {% set regime_label = "Sideways — caution, consider pausing new entries" %}
  {% else %}
    {% set regime_color = "danger" %}
    {% set regime_icon  = "🔴" %}
    {% set regime_label = "Downtrend — no new entries recommended" %}
  {% endif %}
  <div class="alert alert-{{ regime_color }} py-2 d-flex align-items-center gap-2 mb-2">
    <span>{{ regime_icon }}</span>
    <strong>Market ({{ regime.symbol }})</strong>
    <span class="text-muted small">TQ-100 = {{ "%.3f"|format(regime.tq100) }} &nbsp;|&nbsp; TQ-60 = {{ "%.3f"|format(regime.tq60) }}</span>
    <span class="ms-2">{{ regime_label }}</span>
  </div>
{% endif %}
```

---

### Step 6 — `app/orchestrator.py`

Pass `UniverseResult.source` into `ResearchInput`:

```python
research_input = ResearchInput(
    tickers=universe_result.tickers,
    lookback_days=settings.research.lookback_days,
    universe_source=universe_result.source,   # NEW
)
```

---

## Out of scope for Phase 1

- Automatic suppression of the Approve button when Red
- Feeding `market_regime` into the Monitoring agent to auto-tighten stops (Phase 2)
- VIX as a secondary signal
- Persisting regime history across pipeline runs

---

## Phase 2 sketch (future)

Pass `market_regime` from `SelectionResult` into the Monitoring agent input. When
`status == "yellow"`, reduce the drawdown tolerance (e.g. stop at −8% instead of −12%).
When `status == "red"`, flag all existing HOLD positions for review / tighter stop.

---

## Files to change (summary)

| File | Change |
| ---- | ------ |
| `app/models/signals.py` | Add `MarketRegime`; extend `ResearchResult`, `SelectionResult` |
| `app/config.py` | Add regime settings to `ScreeningSettings` and `ResearchSettings` |
| `app/agents/research.py` | Fetch benchmark OHLCV; extend `ResearchInput` |
| `app/agents/screening.py` | Compute TQ-100/60 on benchmark; classify; attach to result |
| `app/orchestrator.py` | Pass `universe_source` into `ResearchInput` |
| `app/templates/stages/screening.html` | Render traffic light badge |

No new dependencies required — yfinance, talib, and numpy are already in use.
