# Market Regime Filter — Implementation Plan

## Goal

Add a market regime traffic light (🟢 / 🟡 / 🔴) to the Screening stage that:

- Computes TQ-60 on the index that matches the quant system's investment universe (TQ-20 shown as secondary early-warning)
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

This mapping now lives in `ResearchSettings.market_regime_symbols` in `app/config.py`.

### Mixed universes

When a quant system uses multiple indices (e.g. DAX + NASDAQ100), choose the benchmark
with the **most constituent tickers** in the resolved universe. If tied, use the first one.

---

## TQ window and thresholds

### Why TQ-60 (revised from initial TQ-100 proposal)

Initial reasoning suggested TQ-100 (~5 months) for stability. This was wrong for this use case.

**The key insight:** TQ = R² × slope/ATR. The R² component already acts as a natural
"confidence" filter — when the market goes sideways, R² drops even on short windows, pulling
TQ toward zero without needing a longer lookback. This makes TQ self-dampening on choppy
markets in a way that a plain moving average is not.

**Validation against the Jul 2026 NASDAQ-100 chart:**

- TQ-100 today (Jul 23): looks back to mid-March, still captures the full Apr–May rally
  (~23k → 30.6k). That surge dominates the regression → TQ-100 reads **green** even though
  the market has been sideways for 7 weeks. ❌ Wrong.
- TQ-60 today (Jul 23): looks back to late April, captures the peak + the Jun–Jul plateau +
  recent slight decline. Low R² (choppy fit) + flat/negative slope → TQ-60 reads near zero
  → **yellow**. ✓ Correct.
- TQ-60 in May (during uptrend): strong slope, high R² → reads clearly **green**. ✓ Correct.

TQ-100 is too slow to detect a regime shift that has been underway for 7 weeks.
TQ-60 is the right primary window. TQ-20 is used as a secondary confirmation window for
status classification.

### Threshold defaults (configurable)

| Status | Condition | Meaning |
| ------ | --------- | ------- |
| 🟢 Green | TQ-60 ≥ 0.03 **and** TQ-20 ≥ 0.01 | Market in uptrend — normal operation |
| 🟡 Yellow | otherwise | Sideways / no clear direction — caution |
| 🔴 Red | TQ-60 ≤ −0.03 **and** TQ-20 ≤ −0.01 | Downtrend — pause new entries |

These thresholds are starting points. Validate by inspecting TQ-60 values on ^NDX history
for known periods:

- 2022 bear market → should read Red
- 2023 recovery → should transition Yellow → Green
- Current sideways (Jul 2026) → should read Yellow

---

## What each state means for the trader (Phase 1: advisory)

| State | New BUYs | Existing positions |
| ----- | -------- | ------------------ |
| 🟢 Green | Normal | Normal stops |
| 🟡 Yellow | Advisory: consider pausing | Manually consider tightening stops |
| 🔴 Red | Advisory: no new entries recommended | Manually consider closing weakest positions |

All decisions remain with the human — the regime badge informs but never blocks any action.

---

## Implementation steps

### Step 1 — `app/models/signals.py`

Add a new model:

```python
from typing import Literal

class MarketRegime(BaseModel):
    symbol: str                              # e.g. "^NDX"
    tq60: float                              # TQ-60 value (primary — used for status)
    tq20: float                              # TQ-20 value (secondary / early-warning)
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
    market_regime: MarketRegime | None = None  # NEW — partial regime (TQ-60 + TQ-20 only)
```

Extend `SelectionResult`:

```python
class SelectionResult(BaseModel):
    ...
    market_regime: MarketRegime | None = None   # NEW
```

---

### Step 2 — `app/config.py`

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

market_regime_display_names: dict[str, str] = {
  "DAX": "DAX",
  "MDAX": "MDAX",
  "SDAX": "SDAX",
  "TecDAX": "TecDAX",
  "EuroStoxx50": "EuroStoxx50",
  "NASDAQ100": "Nasdaq 100",
  "SP500": "S&P 500",
  "FTSE100": "FTSE 100",
}

market_regime_tq_green: float = 0.03
market_regime_tq_red: float = -0.03
market_regime_tq20_green: float = 0.01
market_regime_tq20_red: float = -0.01
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

Also compute a **partial regime** (TQ-60 + TQ-20, no Breadth yet — that requires screening results)
and store it in `ResearchResult.market_regime`:

```python
if result.benchmark_bars and len(result.benchmark_bars) >= 60:
  tq60 = self._trend_quality(result.benchmark_bars, 60)
  tq20 = self._trend_quality(result.benchmark_bars, 20)
  if tq60 >= settings.research.market_regime_tq_green and tq20 >= settings.research.market_regime_tq20_green:
    status = "green"
  elif tq60 <= settings.research.market_regime_tq_red and tq20 <= settings.research.market_regime_tq20_red:
    status = "red"
  else:
    status = "yellow"
    result.market_regime = MarketRegime(
        symbol=result.benchmark_symbol,
    display_name=settings.research.market_regime_display_names.get(dominant_index, result.benchmark_symbol),
        tq60=tq60,
        tq20=tq20,
        status=status,
    )
```

Keep regime thresholds and display labels centralized in `ResearchSettings`.

---

### Step 3b — `app/templates/stages/research.html`

In the `awaiting_review` block, add the regime badge just above the ticker-count summary card.
This gives the human the macro signal as soon as Research completes — before Screening runs.

```html
{% if r.market_regime %}
  {% set regime = r.market_regime %}
  {% set color = "success" if regime.status == "green" else ("warning" if regime.status == "yellow" else "danger") %}
  {% set icon  = "🟢" if regime.status == "green" else ("🟡" if regime.status == "yellow" else "🔴") %}
  <div class="alert alert-{{ color }} py-2 px-3 d-flex align-items-center gap-2 mb-3">
    <span class="fs-6">{{ icon }}</span>
    <strong>{{ regime.symbol }} macro signal</strong>
    <span class="text-muted small ms-1">
      TQ-60 = <code>{{ "%.3f"|format(regime.tq60) }}</code>
      &nbsp;·&nbsp;
      TQ-20 = <code>{{ "%.3f"|format(regime.tq20) }}</code>
    </span>
    <span class="badge bg-{{ color }} bg-opacity-25 text-{{ color }} ms-2">
      {{ "Uptrend" if regime.status == "green" else ("Sideways" if regime.status == "yellow" else "Downtrend") }}
    </span>
    <span class="text-muted small ms-auto">Breadth available after Screening</span>
  </div>
{% endif %}
```

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

Screening consumes the forwarded `market_regime` for UI display and breadth enrichment.
Classification itself remains in Research.

Include `market_regime` in the returned `SelectionResult`.

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
    <span class="text-muted small">TQ-60 = {{ "%.3f"|format(regime.tq60) }} &nbsp;|&nbsp; TQ-20 = {{ "%.3f"|format(regime.tq20) }}</span>
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

- Regime-aware behaviour in Monitoring, Warrant Selection, and Portfolio stages (Phase 2)
- VIX as a secondary signal
- Persisting regime history across pipeline runs

---

## Implemented UI extensions (post-Phase-1)

The Screening stage now includes chart interactions for the regime banner:

- Clicking the market regime line loads an index candle chart in the right chart panel.
- For index charts opened from the regime line:
  - NEW/BREAK markers are hidden.
  - TQ linear regression overlays are available:
    - `TQ-20 LR`: regression line over the last 20 bars only
    - `TQ-60 LR`: regression line over the last 60 bars only
- For normal ticker row charts, these regression overlays are not shown.

This keeps policy signal visualization focused on stock-level screening charts while keeping index charts focused on macro trend context.

---

## Phase 1.5 — Breadth Score (future, low effort)

**Motivation:** The index TQ-60 can read Green while only a few mega-caps drive the index.
A breadth indicator measures whether many stocks actually participate in the trend — which
is what matters for a stock-selection strategy.

**No new data required.** The Screening agent already computes SuperTrend, EMA20, EMA50,
and ADX for every ticker. Breadth is just an aggregation of results already in memory.

Use a dedicated breadth ADX threshold that is independent from stock entry/exit policy
thresholds (`ScreeningSettings.market_regime_breadth_adx_threshold`, default `25.0`).

### Formula

For each ticker $i$ in the screened universe ($N$ stocks), compute a **trend-health score**:

```text
h_i = 0.40 × I(SuperTrend_i = Long)
    + 0.35 × I(EMA20_i > EMA50_i)
    + 0.25 × I(ADX_i > 25)
```

The **BreadthScore** is the mean across all tickers:

```text
BreadthScore = (1/N) × Σ h_i  ∈ [0, 1]
```

| BreadthScore | Meaning |
| --- | --- |
| > 0.60 | 🟢 broad-based uptrend — most stocks trending |
| 0.40 – 0.60 | 🟡 mixed — selective trends |
| < 0.40 | 🔴 few stocks in trend — headwind for stock picking |

### Combined regime logic

When both signals are available, the final status is:

| Index TQ-60 | BreadthScore | Combined Status |
| --- | --- | --- |
| 🟢 | ≥ 0.40 | 🟢 |
| 🟢 | < 0.40 | 🟡 narrow rally — downgrade |
| 🟡 | any | 🟡 |
| 🔴 | any | 🔴 |

The downgrade rule catches "narrow rallies" where the index is rising but only
a handful of mega-caps drive it — a poor environment for stock-selection strategies.

### Implementation

Extend `MarketRegime`:

```python
class MarketRegime(BaseModel):
    symbol: str
    tq60: float
    tq20: float
    breadth_score: float | None = None          # NEW
    breadth_components: dict[str, float] = {}   # NEW — pct_supertrend_long, pct_ema_cross, pct_adx
    status: Literal["green", "yellow", "red"]
```

In `SecuritySelectionAgent.run()`, after the per-ticker loop, add:

```python
if market_regime and input.policy_results:
    n = len(input.policy_results)
    pct_st  = sum(1 for v in input.policy_results.values() if v.get("supertrend"))    / n
    pct_ema = sum(1 for v in input.policy_results.values() if v.get("ema20_rising"))  / n
  adx_threshold = settings.screening.market_regime_breadth_adx_threshold
  pct_adx = sum(1 for sym in input.policy_results if latest_adx(input.bars[sym]) > adx_threshold) / n
    breadth = 0.40 * pct_st + 0.35 * pct_ema + 0.25 * pct_adx
    market_regime.breadth_score = round(breadth, 3)
    market_regime.breadth_components = {
        "pct_supertrend_long": round(pct_st, 3),
        "pct_ema20_above_ema50": round(pct_ema, 3),
    "pct_adx_above_threshold": round(pct_adx, 3),
    "adx_threshold": adx_threshold,
    }
    # Narrow-rally downgrade
    if market_regime.status == "green" and breadth < 0.40:
        market_regime.status = "yellow"
```

Update the Screening UI badge to show breadth alongside TQ values:

```html
<span class="text-muted small">
  TQ-60 = {{ "%.3f"|format(regime.tq60) }}
  &nbsp;|&nbsp; TQ-20 = {{ "%.3f"|format(regime.tq20) }}
  {% if regime.breadth_score is not none %}
    &nbsp;|&nbsp; Breadth = {{ "%.0f"|format(regime.breadth_score * 100) }}%
  {% endif %}
</span>
```

---

## Phase 2 sketch (future)

### 2a — Feed regime into Monitoring agent

Pass `market_regime` from `SelectionResult` into the Monitoring agent input. When
`status == "yellow"`, reduce the drawdown tolerance (e.g. stop at −8% instead of −12%).
When `status == "red"`, flag all existing HOLD positions for review / tighter stop.

### 2b — Regime from winners ("investable universe quality")

Compute the average TQ-60 and percentage of SuperTrend-Long positions among the **top-N
selected tickers** (not all tickers). If even the best candidates have weak indicators,
that is a stronger warning than any index filter. This directly measures whether the
strategy's own pick pool has statistical tailwind.

```python
if result.selected:
    winner_tqs = [scores[t.symbol] for t in result.selected if t.symbol in scores]
    avg_winner_tq = sum(winner_tqs) / len(winner_tqs) if winner_tqs else 0.0
    # Store in MarketRegime.avg_winner_tq for UI display
```

### 2c — Regime-aware pipeline behaviour (no blocking)

**The Approve button is never disabled.** Blocking pipeline progression would prevent the
user from reaching Monitoring — which is the most critical stage during a Red regime,
because existing holdings need to be reviewed and potentially exited.

Instead, the regime status flows as a parameter into downstream stages and adjusts their
behaviour:

| Stage | Green | Yellow | Red |
| ----- | ----- | ------ | --- |
| Screening | Normal | 🟡 badge advisory | 🔴 badge advisory |
| Monitoring | Normal stops | Tighter stops (−8% instead of −12%) | All HOLD positions flagged for review |
| Warrant Selection | Normal | New BUYs shown with caution label | New BUYs shown with 🔴 regime warning; user can override per-position |
| Portfolio | Normal | Normal | New open positions de-prioritised; shown with regime warning |

The regime warning in Warrant Selection and Portfolio is informational — the human
reviews and decides. This keeps the human in control while making the risk visible at
every stage where it matters.

---

## Files to change (summary)

| File | Change |
| ---- | ------ |
| `app/models/signals.py` | Add `MarketRegime`; extend `ResearchResult` (add `market_regime`), `SelectionResult` |
| `app/config.py` | Add regime thresholds to `ResearchSettings`; move lookback + thresholds out of `ScreeningSettings` |
| `app/agents/research.py` | Fetch benchmark OHLCV; compute partial regime (TQ-60 + TQ-20); extend `ResearchInput` |
| `app/templates/stages/research.html` | Render early-warning regime badge (TQ-60 + TQ-20 only) |
| `app/agents/screening.py` | Extend regime with TQ-20 re-check + Breadth; classify; attach to result |
| `app/templates/stages/screening.html` | Render full regime badge (TQ-60 + TQ-20 + Breadth) |
| `app/orchestrator.py` | Pass `universe_source` into `ResearchInput` |

No new dependencies required — yfinance, talib, and numpy are already in use.
