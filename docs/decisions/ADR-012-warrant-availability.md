# ADR-012 — Warrant Availability Caching and Manual ISIN Overrides for ADRs

**Date:** 2026-06-15  
**Status:** Accepted — implemented  

---

## Context

The universe stage resolves index members to tradable tickers. The Warrant Selection
stage then searches comdirect (via the FinHub API) for uncapped CALL warrants on each
selected underlying, keyed by **ISIN**.

Two problems surfaced:

1. **ADRs were silently skipped.** The universe agent dropped every member whose
   `security_type == "ADR"`, on the assumption that comdirect carries no warrants for
   ADRs. This is wrong: warrants exist for many ADRs (e.g. Arm Holdings
   `US0420682058`, Pinduoduo `US7223041028`). Only some ADRs lack warrants
   (e.g. the ASML ADR `USN070592100`, whose underlying stock `NL0010273215` does have
   warrants).

2. **Availability was only observable at the top-20.** `WarrantSelectionResult` only
   carries the selected shortlist, so an underlying with no warrants would only become
   visible the first time it entered the top-20 — potentially many runs later. There was
   no way to know warrant availability for the *whole* universe up front, nor to fix a
   missing-warrant case in a way that persists across executions.

There is **no reliable automatic ADR → underlying-stock mapping**. FinHub instrument
details do not consistently expose the underlying ISIN, so a manual override is required
for the minority of ADRs that need one.

---

## Decision

### 1. Stop skipping ADRs

ADRs are kept in the universe like any other instrument. Their yfinance `symbol`
(and therefore price candles) is unchanged. An `INFO` log records that a member is an
ADR for traceability.

### 2. New pipeline-owned collection `warrant_availability`

A dedicated MongoDB collection caches uncapped-CALL-warrant availability per underlying,
**global across all quant systems and executions**. This is distinct from
`instrument_master` (ADR-007), which is **read-only reference data owned by the FinHub
API** — pipeline-derived availability and manual overrides must not be written there.

**Document `_id`**: underlying ISIN.

```json
{
  "_id": "USN070592100",
  "symbol": "ASML",
  "name": "ASML Holding ADR",
  "has_uncapped_call": false,        // result of the auto scan
  "checked_at": "2026-06-15T...Z",
  "source": "auto",
  "override_isin": "NL0010273215",   // manual underlying ISIN for warrant lookup only
  "override_has_uncapped_call": true,
  "override_checked_at": "2026-06-15T...Z"
}
```

### 3. ADR-only availability scan (incremental)

After the universe stage resolves tickers, **only ADR members** are checked for an
uncapped CALL warrant and the result is persisted. Regular stocks reliably have warrants
at comdirect, so they are not scanned (no API cost, no badge). The universe agent already
detects ADRs via `security_type == "ADR"` and reports their ISINs in
`UniverseResult.adr_isins`.

The scan is **incremental**: only ADR ISINs that are unknown or whose `checked_at` is
older than **30 days** are re-queried. Progress is surfaced on the universe stage page.

**Availability definition:** at least one **uncapped** CALL warrant exists. The full FinHub
maturity range (`Range_NOW` … `Range_ENDLESS`) is used — availability does not depend on the
narrower 9–12 month maturity window applied later in Warrant Selection. (A maturity range
must be supplied because FinHub `/v1/warrants` returns no results without one.) Any strike is
accepted. The capped flag only appears in warrant *detail*, so the scan lists CALL candidates
and inspects up to **K = 10** details; available if ≥ 1 is uncapped.

**Robustness (false-negative fix):** a *failed* detail fetch is distinguished from a
genuinely *capped* warrant. If every sampled detail fetch fails (e.g. transient FinHub
rate-limiting), availability is treated as **unknown** — `_has_uncapped_call` raises and the
scan leaves the entry uncached for retry on the next run — rather than caching a false
"none". Detail fetches are also throttled (3 per underlying) to avoid the rate-limiting that
caused the original false negative (Arm Holdings `US0420682058` was wrongly cached as having
no warrants).

### 4. Manual ISIN override (warrant lookup only)

For an underlying showing no warrants, the universe page offers an inline input to set the
**underlying ISIN** to use for warrant lookup (e.g. ASML stock for the ASML ADR). On
submit, the override is persisted and its availability re-checked immediately. The
override is applied **only** to `get_warrants` / `get_warrant_detail` in Warrant
Selection; the instrument's yfinance `symbol`, name, and price candles always stay on the
original instrument.

Overrides are stored by ISIN in the same global collection, so they are **reusable across
all future executions** of every quant system.

### 5. Override currency handling (strike band + chart)

The override underlying is often denominated in a different currency than the ADR (e.g. the
ASML ADR trades in USD, but its EUR-listed stock carries EUR-strike warrants). Warrant
selection therefore, when an override is active:

- derives the **strike band** from the override underlying's live **native-currency quote**
  fetched from the FinHub `/quotes` endpoint — **never** the ADR's USD `currentPrice`, and
  **no FX conversion**. If the quote payload has no explicit last/current field, warrant
  selection derives the band from the bid/ask midprice instead of leaving the search
  unbounded; and
- sets `SelectedWarrant.chart_symbol` to the override underlying's yfinance symbol
  (e.g. `ASML.AS`), so the warrant chart plots candles in the strike currency and the
  strike line aligns with the price.

This keeps the override surgical: the ADR stays the analyzed instrument (research,
screening, fundamentals); only warrant sourcing and the strike chart follow the override.
See `docs/agents/warrant_selection.md`.

---

## Consequences

- **Positive:** ADRs are no longer lost, and warrant availability for the (few) ADRs in a
  universe is known after the first run. Missing-warrant ADRs are fixable once, globally,
  via a persisted override. Regular stocks incur no extra API calls. Candle history is
  never affected by an override, and override warrants are sized and charted in the correct
  (strike) currency with no FX conversion.
- **Negative:** The uncapped heuristic (first 10 candidates) can rarely be inaccurate. ADR
  underlyings without warrants still require one-time manual ISIN mapping.

---

## Related

- ADR-006 — Instrument Focus (warrants on equity underlyings)
- ADR-007 — Instrument Master Data (read-only reference data; why availability lives in a
  separate collection)
