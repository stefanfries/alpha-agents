# Web UI Spec — HITL Review Interface

## Purpose

The web UI is the human-in-the-loop (HITL) interface for the Alpha Agents investment pipeline. It covers two areas:

1. **Quant System management** — create, configure, edit, and delete named investment strategies, each associated with a depot.
2. **Execution management** — start a pipeline run for a Quant System, inspect each stage's output, and decide to approve (advance) or restart from any earlier stage.

It is implemented as FastAPI + Jinja2 + HTMX + Bootstrap 5 (see ADR-008, ADR-010).

---

## Application layout

Every page shares a base template with:

- **Top navbar**: app name, link to Quant Systems list, current execution ID (if on an execution page), **FinHub API status dot** (see below), light/dark theme toggle
- **Left sidebar** (execution pages): pipeline progress indicator showing all 8 stages with status badges (pending / running / awaiting review / approved / error). Clicking a completed stage navigates to its review page.
- **Main content area**: page-specific content

---

## Quant System management

### QS List — `GET /quant-systems`

Lists all Quant Systems in reverse creation order with name, depot, indices, capital, and status badge.

### New QS — `GET /quant-systems/new` / `POST /quant-systems`

Wizard form with the following fields (in order):

| Field | Type | Notes |
| ----- | ---- | ----- |
| Name | Text | User-defined strategy name |
| Indices | Multi-checkbox | DAX, MDAX, SDAX, TecDAX, EuroStoxx50, NASDAQ100, SP500, FTSE100 |
| Depot | Select | Real Comdirect depots (from `finance.depot_snapshots`) or virtual paper-trading depots |
| Capital (EUR) | Number | `min=10000`, `step=1`; **auto-populated** when a real depot is selected (see below) |

**Depot capital auto-calculation**: selecting a real depot triggers `GET /quant-systems/depot-capital/{depot_id}` via JavaScript `fetch()`. The endpoint returns `{"capital_eur": <total>}` where total = sum of position `current_value` fields from the latest `finance.depot_snapshots` + latest `balance` from `finance.account_balances` (joined via `account_name`). The result is populated into the capital input with a "(auto-calculated from depot)" hint; the value remains editable.

A virtual depot can be created inline via HTMX without leaving the form.

### Edit QS — `GET /quant-systems/{qs_id}/edit` / `POST /quant-systems/{qs_id}`

Same fields as the New form plus a **Status** select (`draft`, `active`, `paused`, `archived`). The depot capital auto-fetch also applies here when changing the depot selection.

### Delete QS — `POST /quant-systems/{qs_id}/delete`

Confirmation dialog via `onsubmit`. Redirects to the list on success.

---

## Pages

### 1. Run List — `GET /runs`

Lists all pipeline runs in reverse chronological order.

**Content:**

| Column | Description |
| ------ | ----------- |
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
| HITL mode | Toggle | On | If off, all checkpoints are auto-approved |

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

**ADR warrant availability** (see ADR-012): for members flagged as ADRs, an **ADR Warrants** badge shows whether comdirect carries an uncapped CALL warrant — `available` (green) / `none` (yellow) / `override ✓` (a manual ISIN override is active). Non-ADR rows show `—`. The scan runs incrementally after universe resolution (only ADR ISINs that are unknown or > 30 days old), with progress shown on the page. Rows with `none` expose an inline input to set an **override underlying ISIN** (e.g. the EUR-listed stock for a USD ADR) used for warrant lookup only; overrides persist globally by ISIN.

**User actions at approve:** optionally set or clear ADR ISIN overrides (persisted globally; see ADR-012).

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

**Summary**: `{N} stocks selected from {universe_size} universe` with scoring details.

**All-stocks table**: every ticker that passed the pre-filter is shown, sorted by TQ descending. Selected tickers are highlighted. Columns:

| Column | Description |
| ------ | ----------- |
| `#` | Rank among selected tickers only (`—` for non-selected) |
| Symbol | yfinance symbol |
| Name | Company name |
| ISIN | Identifier |
| TQ | Primary Trend Quality score (60-bar R² × slope / ATR) |
| TQ-20 | Short-window Trend Quality (20-bar) |
| TSI | True Strength Index value |
| ST / E20 / ADX / E50 | Per-policy pass/fail badges (green ✓ / red ✗) |
| Signal | Trend signal vs 5 trading days ago: **NEW** (green) / **HOLD** (yellow) / **BREAK** (red) / — |
| 1W / 2W / 4W | Rank delta vs prior runs (▲/▼/▶/—) |

Clicking a ticker row calls `GET /runs/{run_id}/charts/screening/{ticker}` via `fetch()` and swaps the chart panel inline.

**Signal filter** (above the table): checkboxes to show/hide rows by trend signal — All / NEW / HOLD / BREAK / — (no signal). All are checked by default. Unchecking "All" activates per-signal filtering.

**Stock chart** (loaded on demand, powered by Lightweight Charts v4):

- Candlestick chart with up to 4 years of OHLCV data
- **Time range selector**: 3M / 6M / 1Y (default) / 3Y
- **Overlay indicators** (toggle buttons): EMA 20 (blue, default on), EMA 50 (orange), SMA 200 (purple), SuperTrend (green/red segments)
- **ADX sub-pane** (toggle, synchronized scroll/zoom): ADX line (gold), +DI (green), −DI (red), threshold line at `min_adx` (default 20, dashed, lineWidth=2)
- **Policy signal markers** on the price chart: green ▲ NEW at each bar where all enabled policies first pass; red ▼ BREAK at each bar where they stop passing. Computed server-side over the full 1460-bar history, respecting the current `config_overrides.screening` policy configuration.
- All indicators computed server-side with TA-Lib

**Screening policies panel** (above action bar):

- Four checkboxes: SuperTrend / EMA20 rising / ADX rising / Price > EMA50
- **Apply & Re-run** button — re-runs screening with updated policy config without creating a new run
- Policy state is persisted to `config_overrides.screening` in the run document

**User actions at approve:** the selection advances to monitoring.

---

#### 4.4 Monitoring — `/stages/monitoring`

**Summary**: `{keep} positions kept, {sell} positions to sell. {free} free slots. {n} entry candidates.`

**Three-section layout:**

1. **Positions to keep** table: incumbent holdings with no exit trigger.

| Column | Description |
| ------ | ----------- |
| Underlying | Symbol mapped from last run's warrant selection |
| Warrant WKN | Held warrant |
| Held since | Most recent BUY date (from virtual depot transactions) |
| Signal | Trend signal (`HOLD` / `NEW` / `—`) |

2. **Positions to sell** table: positions where a BREAK signal was detected and the minimum holding period has elapsed.

| Column | Description |
| ------ | ----------- |
| Underlying | Symbol |
| Warrant WKN | Held warrant |
| Held since | Date |
| Days held | Calendar days since most recent BUY |
| Reason | `exit_signal` |

3. **Entry candidates** table: filtered and ranked new-entry candidates, capped to `free_positions`.

| Column | Description |
| ------ | ----------- |
| # | Rank from Screening |
| Symbol | Underlying ticker |
| TQ | Trend Quality score |
| Signal | `NEW` / `HOLD` |

**User actions at approve:** entry candidates advance to warrant selection.

---

#### 4.5 Warrant Selection — `/stages/warrant_selection`

**Summary**: `{N} warrants selected. {K} underlyings skipped.`

**Split-panel layout:**

Left side (55% width) — **main warrant table**: one row per underlying, ordered by screening TQ rank.

| Column | Description |
| ------ | ----------- |
| `#` | Screening rank |
| Underlying | Symbol + company name |
| Analyzed | Number of warrant details fetched and scored |
| WKN | Best warrant WKN |
| ISIN | Best warrant ISIN |
| Strike | Strike price |
| Maturity | Maturity date |
| Spread | Bid-ask spread % |
| Lev | Leverage ratio |
| Delta | Option delta |
| Score | Composite score in [0, 1] |

Right side — two vertically stacked panels:

**Top-3 detail panel** (top-right, ~42% height):

- Shown when a row in the main table is clicked
- Displays a header: `{N} warrants analyzed — top {M} shown`
- Table with the top-3 warrants by score: WKN, ISIN, Strike, Maturity, Spread, Lev, Delta, Score
- Clicking a row in this panel triggers the stock chart

**Underlying stock chart** (bottom-right, remaining height):

- Candlestick chart powered by Lightweight Charts v4
- EMA 20/50, SuperTrend
- **Orange dashed horizontal line** at the selected warrant's strike price (labelled "Strike")
- **Orange arrow marker** at the maturity date (labelled "Expiry")
- Time range selector: 3M / 6M / 1Y (default) / 3Y
- Loaded via `GET /runs/{run_id}/charts/warrant_selection/{ticker}?strike={n}&maturity={date}&chart_symbol={sym}`. For ISIN-override underlyings (ADRs), `chart_symbol` plots the override underlying in its native currency so candles and the strike line share one currency (no FX); otherwise the underlying symbol is charted.

**User actions at approve:** all selected warrants advance to portfolio construction.

---

#### 4.6 Portfolio Construction — `/stages/portfolio`

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

#### 4.7 Risk — `/stages/risk`

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

#### 4.8 Execution — `/stages/execution`

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

## HITL interaction model

### Approve flow

```text
User clicks "Approve" →
  POST /runs/{run_id}/stages/{stage_name}/approve
  → orchestrator advances pipeline to next stage
  → next stage runs synchronously (or queued if long)
  → redirect to GET /runs/{run_id}/stages/{next_stage}
```

For the screening stage, the POST body includes the list of tickers to carry forward (from the checkboxes). Same for warrant selection.

### Restart flow

The action bar includes a **"Restart from..."** dropdown listing all earlier stages. Selecting a stage and clicking Restart rewinds the pipeline to that stage and re-runs it.

```text
User selects "Restart from: screening" →
  User clicks "Restart" →
  POST /runs/{run_id}/stages/{stage_name}/restart
  Body: { from_stage: "screening" }
  → orchestrator rewinds pipeline to the named stage and re-runs
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
| `GET` | `/runs/{run_id}/charts/screening/{ticker}` | Lightweight Charts candlestick + indicator fragment |
| `GET` | `/runs/{run_id}/charts/warrant_selection/{isin}` | Warrant scoring chart fragment (stub) |
| `GET` | `/runs/{run_id}/charts/portfolio` | Portfolio weight chart fragment (stub) |
| `GET` | `/runs/{run_id}/charts/risk` | Risk weight chart fragment (stub) |

All `/charts/` endpoints return a Plotly HTML fragment (`full_html=False`) for HTMX insertion. They read data from MongoDB Atlas using `run_id` and the stage name.

---

## Charts reference

| Stage | Chart type | Library | Trigger |
| ----- | ---------- | ------- | ------- |
| Screening | Interactive candlestick + EMA/SMA overlays + SuperTrend + ADX sub-pane | Lightweight Charts v4 | Click ticker row (vanilla `fetch()`) |
| Warrant selection | Horizontal bar (criterion scores) | — (stub) | Click warrant row |
| Portfolio | Donut (position weights) | — (stub) | Page load |
| Risk | Horizontal bar (position weights + limit line) | — (stub) | Page load |

The screening chart is an HTML fragment returned by the `/charts/screening/{ticker}` endpoint. It embeds all indicator data as a `data-chart` JSON attribute and is initialised client-side by `initDataCharts()` in `base.html`. The Lightweight Charts library is loaded from CDN (`lightweight-charts@4.1.3`). All indicators (EMA, SMA, ADX, SuperTrend via ATR) are computed server-side using TA-Lib before the fragment is returned.

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

---

## FinHub API keepalive

The FinHub data API is deployed as an Azure Container App with **scale-to-zero**. A cold start takes 30–60 seconds; after 5 minutes of inactivity the ACA scales back down.

To keep the API warm while the browser client is open, `base.html` includes a keepalive mechanism:

### Server-side proxy — `GET /api/finhub/health`

`app/main.py` exposes a thin proxy that forwards to `settings.finhub.base_url + /health`. The browser calls this local endpoint instead of FinHub directly, which avoids CORS issues and keeps the API base URL server-side.

- Returns `200 {"status": "ok"}` on success
- Returns `503 {"status": "error"}` on any failure
- Hard-coded 10 s request timeout

### Client-side behaviour (JS IIFE in `base.html`)

| Phase | Behaviour |
| ----- | --------- |
| On `DOMContentLoaded` | Immediately pings `/api/finhub/health` to trigger cold-start |
| While waking up | Retries every **10 s** until a 200 response is received |
| Once online | Schedules a ping every **4 minutes** (below the 5-min ACA idle threshold) |
| On `beforeunload` | Clears all timers |

### Navbar status dot

A small 8px dot to the left of the theme toggle shows the current API state:

| State | Appearance | Meaning |
| ----- | ---------- | ------- |
| `unknown` | Grey | Page just loaded, not yet checked |
| `checking` | Amber, pulsing | Request in flight / waking up |
| `ok` | Green, glowing | API online |
| `error` | Red | API unreachable |

Hovering the dot shows a tooltip with the human-readable status text.
