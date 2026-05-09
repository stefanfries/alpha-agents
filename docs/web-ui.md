# Web UI Spec — MITL Review Interface

## Purpose

The web UI is the man-in-the-loop (MITL) interface for pipeline runs. It lets the user start a run, inspect each stage's output, and decide to approve (advance to the next stage) or restart from any earlier stage with optionally adjusted parameters. It is implemented as FastAPI + Jinja2 + HTMX (see ADR-008).

---

## Application layout

Every page shares a base template with:

- **Top navbar**: app name, link to run list, current run ID (if on a run page)
- **Left sidebar**: pipeline progress indicator showing all 7 stages with status badges (pending / running / awaiting review / approved / error). Clicking a completed stage navigates to its review page.
- **Main content area**: stage-specific review panel

---

## Pages

### 1. Run List — `GET /runs`

Lists all pipeline runs in reverse chronological order.

**Content:**

| Column | Description |
|--------|-------------|
| Run ID | Short UUID, links to `GET /runs/{run_id}` |
| Started | Timestamp |
| Indices | e.g. `DAX, MDAX` |
| Status | `running` / `awaiting review` / `complete` / `error` |
| Current stage | Name of the stage currently awaiting review or running |

**Actions:**

- **New run** button → opens the run-creation form (inline via HTMX or separate page)

---

### 2. New Run Form — `POST /runs`

A simple form for configuring a new pipeline run. Submitted via standard form POST; on success, redirects to `GET /runs/{run_id}`.

**Fields:**

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| Indices | Multi-checkbox | DAX checked | Select one or more: DAX, MDAX, SDAX, TecDAX |
| Capital (EUR) | Number input | `portfolio_capital_eur` from config | Total capital to deploy |
| MITL mode | Toggle | On | If off, all checkpoints are auto-approved |

---

### 3. Pipeline Run — `GET /runs/{run_id}`

Redirects to the review page for the current stage (`GET /runs/{run_id}/stages/{current_stage}`).

---

### 4. Stage Review Pages — `GET /runs/{run_id}/stages/{stage_name}`

One page per pipeline stage. Each page follows the same structure:

1. **Stage header**: stage name, status badge, timestamp
2. **Summary panel**: stage-specific data table or key metrics (rendered server-side)
3. **Chart panel**: Plotly chart(s), loaded on demand via HTMX
4. **Action bar**: Approve button + Restart dropdown (always at the bottom)

Stage-specific content is described below.

---

#### 4.1 Universe — `/stages/universe`

**Summary table**: one row per resolved index.

| Column | Description |
| ------ | ----------- |
| Index | e.g. `DAX` |
| Tickers resolved | Count |
| Tickers (sample) | First 5 symbols |
| Resolution source | `FastAPI` or `Wikipedia fallback` |
| Failures | Count of symbols that could not be resolved |

No charts.

**User actions at approve:** none (no overrides at this stage).

---

#### 4.2 Research — `/stages/research`

**Summary table**: one row per ticker.

| Column | Description |
| ------ | ----------- |
| Ticker | Symbol |
| Bars fetched | Number of OHLCV bars |
| Date range | First → last bar date |
| Status | `ok` / `partial` / `missing` |

Tickers with `missing` data are highlighted. No charts (universe is too large to chart per-ticker at this stage).

**User actions at approve:** none.

---

#### 4.3 Stock Selection (Screening) — `/stages/screening`

**Summary**: `{N} stocks selected from {universe_size} universe` — `{established}` established, `{starting}` starting uptrends.

**Selection table**: one row per selected stock, sorted by score descending.

| Column | Description |
| ------ | ----------- |
| Ticker | Symbol |
| Name | Company name |
| Trend status | `ESTABLISHED` / `STARTING` badge |
| Score | `0.0 – 1.0` |
| Rationale | Human-readable reason string from `StockSelectionResult.rationale` |
| Remove | Checkbox — user can deselect before approving |

Clicking a ticker row triggers `hx-get="/runs/{run_id}/charts/screening/{ticker}"` and loads the stock chart into the chart panel.

**Stock chart** (loaded on demand):

- Candlestick (last 200 bars)
- SMA20 (blue), SMA50 (orange), SMA200 (red) overlaid on price
- ADX subplot below price
- Higher-high / higher-low swing points marked

**User actions at approve:** the selection table checkboxes determine which tickers advance. Deselected tickers are excluded from the `WarrantSelectionAgent` input.

---

#### 4.4 Warrant Selection — `/stages/warrant_selection`

**Summary**: `{N} warrants selected for {M} underlyings. {K} underlyings had no suitable warrant.`

**No-warrant list**: compact list of tickers where no warrant passed the hard filters, with the filter reason.

**Warrant table**: one row per selected warrant, sorted by underlying ticker.

| Column | Description |
| ------ | ----------- |
| Underlying | Ticker + company name |
| Warrant | WKN — Issuer, Strike, Maturity |
| Score | `0.0 – 10.0` |
| Delta | Value + score bracket |
| Leverage | Value + score bracket |
| Spread % | Value + score bracket |
| Premium p.a. % | Value + score bracket |
| Remaining (months) | Value + score bracket |
| Remove | Checkbox — user can reject this warrant |

Score brackets use colour coding: green (10), yellow (7), orange (4), red (0).

Clicking a warrant row triggers `hx-get="/runs/{run_id}/charts/warrant_selection/{isin}"` and loads the warrant scoring chart into the chart panel.

**Warrant scoring chart** (loaded on demand):

- Horizontal bar chart — one bar per scoring criterion
- X-axis: points awarded (0–10)
- Bar colour reflects score bracket (green/yellow/orange/red)
- Weighted contribution shown as a secondary axis or annotation

**User actions at approve:** checked warrants advance; unchecked warrants are excluded from portfolio construction. Their underlyings are implicitly removed too.

---

#### 4.5 Portfolio Construction — `/stages/portfolio`

**Summary**: `{N} positions. {new} new, {existing} unchanged, {close} to close. Total capital: {EUR}.`

**Three-section layout:**

1. **New positions** table:

| Column | Description |
| ------ | ----------- |
| Underlying | Ticker |
| Warrant | WKN — Strike, Maturity |
| Weight % | Proposed allocation |
| Capital (EUR) | Allocated amount |

2. **Existing positions** table (no trade needed):

| Column | Description |
| ------ | ----------- |
| Underlying | Ticker |
| Warrant | WKN |
| Current weight % | In current portfolio |

3. **Positions to close** table:

| Column | Description |
| ------ | ----------- |
| Underlying | Ticker |
| Warrant | WKN |
| Current value (EUR) | Approximate proceeds |
| Reason | `not in shortlist` |

**Portfolio weight chart** (rendered on page load, not on-demand):

- Donut chart: each new position as a segment, labelled by underlying ticker
- "Existing" and "Close" shown as summary segments for context

**User actions at approve:** none (no position-level overrides at this stage — use the warrant stage to remove positions).

---

#### 4.6 Risk — `/stages/risk`

**Summary**: `{approved} positions approved. {rejected} positions rejected by risk rules.`

**Risk rule table**: one row per configured rule.

| Column | Description |
| ------ | ----------- |
| Rule | e.g. `Max position weight 10%` |
| Status | `PASS` / `FAIL` badge |
| Details | e.g. `All positions within limit` or `2 positions exceeded` |

**Rejected positions table** (if any):

| Column | Description |
| ------ | ----------- |
| Underlying | Ticker |
| Warrant | WKN |
| Violated rule | Rule name |
| Reason | `risk_notes` value from `RiskAssessment` |

**Approved positions** weight bar chart:

- Horizontal bar chart of approved positions by weight %
- Dashed vertical line at `risk_max_position_weight` limit

**User actions at approve:** none (risk rules are hard constraints; the user cannot override rejections here — they must restart from an earlier stage with adjusted parameters or config).

---

#### 4.7 Execution — `/stages/execution`

**Summary**: `{buys} buy orders, {sells} sell orders. Estimated total: {EUR} deployed.`

A `DRY RUN` badge is shown prominently if `execution_dry_run=True`.

**Order table**: one row per order.

| Column | Description |
| ------ | ----------- |
| Action | `BUY` (green) / `SELL` (red) badge |
| Instrument | Name, WKN, ISIN |
| Quantity | Number of units |
| Limit price (EUR) | Suggested limit based on last ask (buy) or bid (sell) |
| Estimated value (EUR) | Quantity × limit price |
| Exchange | Venue for placement |

**Skipped positions** (below main table): compact list of positions already at target (no trade needed).

**Footer totals**: total buy value, total sell value, net capital change.

**User actions at approve:**

- Button label: **"Mark as placed"** (not "Approve") — explicitly signals the user has manually placed the orders in Comdirect
- On confirm: pipeline run is marked `complete` in MongoDB Atlas

---

## MITL interaction model

### Approve flow

```
User clicks "Approve" →
  POST /runs/{run_id}/stages/{stage_name}/approve
  → orchestrator advances pipeline to next stage
  → next stage runs synchronously (or queued if long)
  → redirect to GET /runs/{run_id}/stages/{next_stage}
```

For the screening stage, the POST body includes the list of tickers to carry forward (from the checkboxes). Same for warrant selection.

### Restart flow

The action bar includes a **"Restart from..."** dropdown listing all earlier stages. Selecting a stage and clicking Restart shows a config override panel (HTMX-swapped inline) where the user can adjust parameters before restarting.

```
User selects "Restart from: screening" →
  HTMX loads config override form fragment
  User optionally adjusts parameters (see table below)
  User clicks "Restart" →
  POST /runs/{run_id}/stages/{stage_name}/restart
  Body: { from_stage: "screening", config_overrides: {...} }
  → orchestrator rewinds pipeline to the named stage, applies overrides, re-runs
  → redirect to GET /runs/{run_id}/stages/screening
```

Config overrides are stored per-run in MongoDB Atlas under the run document and do not affect global defaults (`.env` remains the source of truth).

### Config override parameters per stage

Each restart form shows only the parameters relevant to the stage being re-run. All other config values are carried forward unchanged from the original run.

| Stage | Editable parameters |
| ----- | ------------------- |
| Universe | Indices (multi-checkbox: DAX, MDAX, SDAX, TecDAX) |
| Screening | `stock_selection_top_n`, `stock_selection_min_adx`, `stock_selection_allow_starting_trends` |
| Warrant selection | `warrant_min_remaining_days`, `warrant_max_leverage`, `warrant_max_spread_pct`, `warrant_min_score`, `warrant_scoring_weights` (7 fields, validated to sum to 1.0) |
| Portfolio | `portfolio_capital_eur`, `portfolio_sizing_method`, `portfolio_max_position_weight` |
| Risk | `risk_max_position_weight`, `risk_max_sector_weight`, `risk_max_positions` |
| Execution | `execution_dry_run`, `execution_min_trade_eur`, `execution_order_type` |

The Research stage has no editable parameters — restarting from Research simply re-fetches OHLCV data with the same universe.

Config overrides are merged with the current run config and stored in MongoDB Atlas under the run document. They do not modify the global config.

---

## Route table

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/runs` | Run list page |
| `POST` | `/runs` | Create and start a new run |
| `GET` | `/runs/{run_id}` | Redirect to current stage review |
| `GET` | `/runs/{run_id}/stages/{stage}` | Stage review page |
| `POST` | `/runs/{run_id}/stages/{stage}/approve` | Approve stage; advance pipeline |
| `POST` | `/runs/{run_id}/stages/{stage}/restart` | Restart from a named stage |
| `GET` | `/runs/{run_id}/charts/screening/{ticker}` | Plotly candlestick fragment for a stock |
| `GET` | `/runs/{run_id}/charts/warrant_selection/{isin}` | Plotly scoring chart fragment for a warrant |
| `GET` | `/runs/{run_id}/charts/portfolio` | Plotly donut chart fragment for portfolio weights |
| `GET` | `/runs/{run_id}/charts/risk` | Plotly weight bar chart fragment |

All `/charts/` endpoints return a Plotly HTML fragment (`full_html=False`) for HTMX insertion. They read data from MongoDB Atlas using `run_id` and the stage name.

---

## Charts reference

| Stage | Chart type | Library | Trigger |
| ----- | ---------- | ------- | ------- |
| Screening | Candlestick + SMA20/50/200 + ADX subplot | Plotly | Click ticker row |
| Warrant selection | Horizontal bar (criterion scores) | Plotly | Click warrant row |
| Portfolio | Donut (position weights) | Plotly | Page load |
| Risk | Horizontal bar (position weights + limit line) | Plotly | Page load |

All charts use `plotly.graph_objects.Figure.to_html(full_html=False)` and are inserted into a `<div id="chart-panel">` via `hx-swap="innerHTML"`.

---

## Template structure

```text
templates/
  base.html              ← navbar + sidebar + content block
  runs/
    list.html            ← run list table + new run button
    new_run_form.html    ← new run form (may render as HTMX fragment)
  stages/
    universe.html
    research.html
    screening.html
    warrant_selection.html
    portfolio.html
    risk.html
    execution.html
  partials/
    stage_progress.html  ← sidebar pipeline progress indicator
    action_bar.html      ← approve button + restart dropdown (shared)
    config_override.html ← config override form (HTMX fragment, loaded on restart)
    chart_panel.html     ← chart container div (target for HTMX chart swaps)
```

Stage pages extend `base.html` and include `partials/action_bar.html`. Chart routes return raw Plotly HTML (no template — just `HTMLResponse`).
