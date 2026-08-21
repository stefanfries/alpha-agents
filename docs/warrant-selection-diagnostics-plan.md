# Warrant Selection Diagnostics Plan

## Status: PROPOSED

**Created:** 2026-08-21  
**Purpose:** Make warrant-selection rejections transparent enough for strategy development, debugging, and configuration tuning.

## Goal

Explain why an underlying has no eligible call warrant without collapsing all causes into a generic skip message.

Target diagnosis:

```text
GEHC - no eligible call warrant found

Universe: 127 leveraged products
Rejected:
  119 product_type = KNOCK_OUT
    8 classic warrants
    6 maturity < minimum
    2 strike outside configured range
Eligible: 0
Analyzed: 0
Qualified: 0
```

The diagnosis must distinguish product availability from later detail, scoring, and qualification failures.

## Current state

The existing warrant-selection flow already provides:

- `WarrantSelectionResult.skipped`
- `WarrantSelectionResult.skipped_reasons`
- `WarrantSelectionResult.analyzed_count`
- a skipped-underlyings section in `app/templates/stages/warrant_selection.html`

However, `_pick_best()` currently calls FinHub with `preselection="CALL"`, maturity bounds, and strike bounds. Products excluded by that API query are invisible to Alpha Agents. The application therefore cannot currently count Knock-Out products, classic warrants, or out-of-range products unless FinHub returns them or exposes aggregate counts.

## Design principles

- Store structured diagnostics, not only a formatted string.
- Keep `skipped_reasons` as a concise human-readable compatibility field.
- Give every rejected product one primary rejection category for additive counting.
- Keep `Universe`, `Eligible`, `Analyzed`, `Qualified`, and `Selected` as separate concepts.
- Fetch detailed warrant data only for products that pass the inexpensive discovery filters.
- Do not change the scoring formula or ranking behavior in this feature.
- Preserve existing ADR override, maturity, strike-band, retry, and concurrency behavior unless explicitly required by the API strategy.

## Proposed data contract

Add a per-underlying diagnostic model in `app/models/signals.py`:

```python
class WarrantSelectionDiagnostics(BaseModel):
    universe_count: int = 0
    rejected_by_reason: dict[str, int] = {}
    eligible_count: int = 0
    analyzed_count: int = 0
    qualified_count: int = 0
    best_score: float | None = None
```

Add it to `WarrantSelectionResult`:

```python
diagnostics: dict[str, WarrantSelectionDiagnostics] = {}
```

Suggested stable rejection keys:

- `product_type_knock_out`
- `product_type_classic_warrant`
- `non_call_product`
- `maturity_below_minimum`
- `maturity_above_maximum`
- `strike_below_minimum`
- `strike_above_maximum`
- `missing_detail`
- `score_below_minimum`
- `lookup_failed`
- `unknown_product_type`

The exact product-type labels must be confirmed against real FinHub payloads before implementation. The names above are application-level categories, not assumed API field values.

## Required API investigation

Before coding the filter counters, inspect the actual FinHub response contract and answer:

1. Can `GET /v1/warrants` return an unfiltered or broad product universe?
2. Which field identifies Knock-Out products versus classic warrants?
3. Which field identifies call versus put direction?
4. Are maturity and strike fields present in discovery responses, or only detail responses?
5. Does the endpoint provide `total`, category counts, or pagination metadata?
6. Are the current query filters applied server-side before pagination?
7. Can one broad request safely return the complete relevant universe for one underlying?

This determines the implementation route.

## Implementation options

### Option A: Broad discovery, local classification (preferred if feasible)

1. Request the broadest practical product universe for the underlying.
2. Classify product type and call/put locally.
3. Apply maturity and strike filters locally in a deterministic order.
4. Record one primary rejection reason per product.
5. Fetch details only for eligible products.
6. Score eligible details and record `qualified_count` and `best_score`.
7. Preserve the existing adaptive strike-search behavior only where it remains useful for candidate volume.

**Benefit:** Full per-product diagnostics.  
**Risk:** Larger API responses and possible pagination/rate-limit impact.

### Option B: FinHub aggregate diagnostics (preferred if available)

Keep the current filtered search for performance and extend FinHub to return counts such as:

```json
{
  "items": [],
  "total": 127,
  "counts": {
    "KNOCK_OUT": 119,
    "CLASSIC_WARRANT": 8
  }
}
```

**Benefit:** Lower payload and detail-fetch cost.  
**Risk:** Requires a FinHub API contract change and less control over filter-order diagnostics.

### Option C: Two-stage hybrid

Use a broad lightweight discovery request for counts, then retain the current filtered request for eligible candidates and scoring.

**Benefit:** Separates diagnosis from selection and limits detail calls.  
**Risk:** Two discovery calls per underlying and possible count inconsistencies if data changes between calls.

## Proposed phases

### Phase D0: Contract and payload discovery

- Capture representative FinHub payloads for:
  - a successful selection
  - no products
  - only Knock-Out products
  - products outside maturity range
  - products outside strike range
  - mixed classic and Knock-Out products
- Confirm field names and pagination behavior.
- Decide between Option A, B, and C.
- Do not change application code in this phase.

**Exit criteria:** Product-type, call/put, maturity, and strike fields are mapped to real payload fields.

### Phase D1: Domain model and classification helpers

- Add `WarrantSelectionDiagnostics` to `app/models/signals.py`.
- Add small pure helpers under `app/policies/` or `app/agents/warrant_selection.py` for:
  - product-type classification
  - maturity extraction
  - strike extraction
  - primary rejection classification
  - diagnostic counter accumulation
- Keep helpers data-driven; do not create one class per rejection reason.

**Verification:** Unit tests cover each category and deterministic primary-reason precedence.

### Phase D2: Agent integration

- Extend `_pick_best()` to return diagnostics alongside selection data.
- Populate diagnostics for both successful and skipped underlyings.
- Preserve existing `selected`, `top3`, `analyzed_count`, and `skipped_reasons` behavior.
- Ensure API lookup failures remain distinct from “no eligible product”.
- Log compact structured diagnostics per underlying.

**Verification:** Existing warrant-selection tests pass; new tests assert counts for mixed product fixtures.

### Phase D3: UI presentation

Update `app/templates/stages/warrant_selection.html`:

- Show a compact one-line reason for each skipped underlying.
- Add expandable diagnostic details rather than making the main table wider.
- Display:
  - Universe
  - Rejected categories and counts
  - Eligible
  - Analyzed
  - Qualified
  - Best score, when available
- Keep the existing top-3 detail panel unchanged for selected warrants.

**Verification:** Template renders with missing diagnostics and with fully populated diagnostics.

### Phase D4: Documentation and operational validation

- Update `docs/agents/warrant_selection.md` with the data contract, filter order, and diagnostics semantics.
- Update `docs/data-models.md` with the new model and result field.
- Record the selected FinHub API strategy and payload assumptions.
- Run the full test suite and Ruff.
- Compare diagnostic output for several real underlyings before and after the change.

**Exit criteria:** Strategy users can distinguish “no products available” from “products rejected by filters” and “all eligible products scored below minimum”.

## Filter order and counting rules

The implementation must define and test a single primary-reason order. Recommended order:

1. Discovery/API failure
2. Product type not supported
3. Product direction is not CALL
4. Maturity below minimum
5. Maturity above maximum
6. Strike below minimum
7. Strike above maximum
8. Missing or invalid detail data
9. Score at or below `min_score`

A product that fails multiple checks is counted only under the first applicable reason. This makes category totals additive and prevents misleading double counting.

## Compatibility and migration

- Existing executions without `diagnostics` must render normally.
- `diagnostics` should default to an empty mapping.
- Existing `skipped_reasons` strings remain available for older UI/API consumers.
- No database migration is required if diagnostics are stored inside the existing execution result and defaults are applied on read.
- Historical executions will not gain retroactive diagnostics unless rerun.

## Test plan

### Unit tests

- Classify Knock-Out and classic warrant payloads.
- Classify call versus put products.
- Count maturity and strike rejections.
- Verify primary-reason precedence.
- Verify empty universe, API failure, missing fields, and invalid dates.
- Verify `eligible_count`, `analyzed_count`, `qualified_count`, and `best_score`.

### Agent tests

- Mixed discovery response produces additive diagnostics.
- No eligible products produces `selected=[]` and a useful `skipped_reasons` entry.
- Eligible products with no score above `min_score` report `Eligible > 0`, `Analyzed > 0`, `Qualified = 0`.
- Detail-fetch failures are not misreported as product-type or filter rejections.
- Existing strike widening/narrowing and ADR override tests remain valid.

### Regression checks

- Existing full suite remains green.
- No increase in detail requests for rejected products.
- Concurrent selection still respects current semaphores and retry behavior.

## First task for tomorrow

1. Capture or inspect real FinHub `/v1/warrants` discovery and detail payloads.
2. Map the actual product-type, direction, maturity, and strike fields.
3. Decide whether broad discovery is feasible or whether FinHub must expose aggregate counts.
4. Add the diagnostics model only after that contract decision is documented.
