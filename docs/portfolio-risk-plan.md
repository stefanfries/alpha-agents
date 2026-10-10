# Portfolio Construction and Risk — Implementation Plan

**Status:** Account-backed equal sizing, typed EUR notionals, NAV-based Risk limits,
SELL-before-BUY execution, and documentation are implemented. The Comdirect fee schedule and
25 bps global slippage default are implemented; slippage is overridable per Quant System. The
full test suite and lint pass.
**Scope:** Portfolio sizing, current-account data, roll capital allocation, and hard portfolio risk checks.
**Owner:** Strategy / pipeline

## Purpose

Make Portfolio Construction and Risk operate on the same explicit capital, position-value,
and exposure definitions. Preserve the existing human-review and dry-run workflow. This is
the foundation for later underlying-volatility leverage and warrant-convexity scoring; those
scoring changes are out of scope here.

## Verified current state

- `PortfolioConstructionAgent` receives a `SelectionResult` assembled by the orchestrator
  from selected warrants, plus holdings loaded from a depot snapshot.
- `_fetch_holdings()` retains instrument quantity and average purchase price. The separate
  account-snapshot adapter combines held positions with fresh FinHub bid marks and source cash.
- Depot position schemas already include `current_price`, `current_value`, and
  `average_purchase_price`. Price values can include a unit and `price_datetime`.
- Virtual depot snapshots include `current_cash`. Real-depot capital is already calculated
  by `/quant-systems/depot-capital/{depot_id}` as marked position values plus the newest
  balance for each of Girokonto, Tagesgeld PLUS-Konto, and Verrechnungskonto.
- The Quant System's configured `capital_eur` remains stored for reference; account-backed
  sizing and Risk use NAV/cash from the current depot snapshot.
- Monitoring fetches a FinHub warrant snapshot and retains bid, ask, `market_data.prev_close`,
  currency, and timezone-aware quote timestamp; the midpoint metric remains unchanged.
  `pct_change_from_prev_close` is calculated from the midpoint and `prev_close` from the same
  `/v1/warrants/{identifier}` response, so no additional `/v1/quotes/{identifier}` request is
  needed. The snapshot fields and percentage are copied into persisted `PositionReview` results.
- `_fetch_portfolio_account_snapshot()` now reads real-depot cash by summing the latest EUR
  balance per supported cash account type, or virtual cash from the latest snapshot (falling back to starting capital before
  the first snapshot), fetches FinHub held-warrant bids, applies the 72-hour/EUR checks,
  and computes NAV only when all required values are valid. It attaches the typed snapshot to
  `PortfolioProposal`, which Portfolio sizing and Risk now consume.
- `PortfolioHoldingValue` now carries both `underlying_isin` and optional
  `underlying_symbol`. Sector lookup uses the underlying ISIN as its key; the symbol remains
  supporting metadata.
- `TradeExecutionAgent` now emits every approved close/roll SELL before any approved BUY.
  Roll approval remains paired, and execution skips both legs when the replacement is too
  small.
- Live FinHub responses have been checked: `/v1/quotes/{identifier}` returns top-level
  `timestamp_utc`, while `/v1/warrants/{identifier}` returns
  `market_data.timestamp_utc`; both include quote currency. The new fields are additive, and
  current callers consume raw dictionaries while reading only known keys.
- `Position.quantity` is now reserved for held instrument units. `PlannedPosition.notional_eur`
  holds proposed BUY allocations, and `Order.notional_eur` represents an Execution BUY; SELL
  orders use `Order.quantity` in instrument units.
- `portfolio.max_positions` is now the shared target/slot count for Monitoring, Portfolio, and
  Risk. The 10% caps were removed; Risk uses the 3× position multiple and one-third sector cap.
- Portfolio equal sizing divides available funds across BUY-slot capacity after planned closes,
  including roll replacements, rather than across configured `max_positions` when fewer BUY
  slots are open.
- The Portfolio review uses action-ordered SELL, BUY, and KEEP tables. BUY unit and spend values
  are estimates from the selected warrant ask; held values and weights use fresh EUR bid marks.
  SELL/KEEP reasons combine Monitoring trend and warrant-health context; BUY reasons carry the
  Warrant Selection rationale.
- Sector metadata is joined and aggregated by underlying ISIN; symbols are supporting metadata.
- Focused tests cover account snapshot serialization, virtual NAV from cash plus bid value,
  real EUR cash extraction, stale/non-EUR quote rejection, equal sizing, planned SELL proceeds,
  roll sizing, position/sector limits, existing-breach warnings, and grouped
  SELL-before-BUY order generation. The Comdirect fee schedule and 25 bps default slippage are
  implemented; fill-based slippage calibration remains a future refinement.

## Agreed design

### Capital and valuation

- **NAV** is the current EUR market value of held positions plus available EUR cash.
- Use the current FinHub bid, not average purchase cost or midpoint, to value held warrants;
  bid is the conservative liquidation price.
- FinHub quote timestamps must identify an absolute instant. The API should return RFC 3339
  timestamps in UTC (`Z`), converting the Comdirect `Europe/Berlin` source time at ingestion.
  The timestamp must represent quote time, not API retrieval time. If existing clients depend
  on the naive timestamp format, introduce the aware field compatibly before removing it.
- The verified quote response includes `timestamp_utc` at `/v1/quotes/{identifier}` and
  `market_data.timestamp_utc` at `/v1/warrants/{identifier}`.
- A quote is valid for risk-increasing approvals only if `timestamp_utc` is present, parseable,
  no more than `portfolio.quote_max_age_hours` (72 hours by default), and its currency is EUR. Non-EUR conversion is out of scope for
  v1; block new exposure when a required quote is non-EUR.
- Available cash is distinct from NAV and is the source for ordinary new-buy sizing. For real
  depots it is the sum of the newest `Girokonto`, `Tagesgeld PLUS-Konto`, and
  `Verrechnungskonto` records; unchanged balances remain valid even when their `recorded_at`
  timestamps are older than the other account records.
- Use one shared `max_positions` setting as the intended portfolio breadth and slot limit.
- Preserve current holdings not selected for sale; confirmed SELLs free position slots.
- A ROLL is a full SELL of the incumbent and a BUY of a replacement on the same underlying.
  It replaces one occupied slot and does not consume an entry slot.
- Expected net proceeds from all planned SELLs count toward same-run BUY sizing, including
  ordinary closes and incumbent SELLs from planned ROLLs. Calculate this from current bid
  marks less estimated sell costs/slippage.
- The execution plan must list all SELL orders first, followed by all BUY orders. A ROLL's
  two legs remain an atomic approval decision, even though its orders are grouped by side.
- A roll replacement uses the same standard BUY amount as every other BUY; it is not sized
  to consume the incumbent's full proceeds. Remaining proceeds stay as cash unless they
  increase the run-wide standard BUY amount under the sizing formula.
- Because execution is manual and fills are not confirmed by this app, show that the BUY
  budget depends on expected SELL proceeds. Before placing any BUYs, the operator must
  confirm the SELLs filled and actual cash is sufficient; otherwise adjust/withhold BUYs.

### Equal sizing

Let `S` be the BUY-slot capacity: vacant portfolio slots after planned close SELLs, plus
planned roll replacements. Include opening cash and expected net proceeds from all planned
SELLs, then reserve estimated costs and slippage for the BUYs:

```text
available_funds = opening_cash + expected_net_sell_proceeds
standard_buy_amount = max amount where (standard_buy_amount * S) + estimated_buy_costs <= available_funds
```

Each accepted ordinary BUY uses this standard amount. The number of ordinary new BUYs is
limited by vacant slots after planned SELLs. Unused cash remains cash; do not divide the full
cash balance only among the few candidates selected in a run. When there are enough entry
candidates to fill the available slots, the run allocates the available funds across those
slots, after estimated BUY costs. If fewer candidates are available, size each at the same
slot-based amount and leave the unallocated balance as cash.

For example, with €45,000 opening cash and six available BUY slots, size up to €7,500 gross per
slot before estimated BUY costs. If only two new positions are selected, allocate two such
BUYs and leave the remainder in cash. When planned SELLs exist, their expected net proceeds
are added to opening cash before sizing across the available BUY slots.

### Rolls

- Sell the full incumbent position.
- Size the replacement BUY to the same `standard_buy_amount` as an ordinary new BUY.
- Do not use the incumbent's cost basis to size the replacement.
- Include expected net proceeds from the incumbent SELL in the shared same-run sell-proceeds
  pool before calculating `standard_buy_amount`; do not directly size the replacement to
  consume all incumbent proceeds.
- Risk approves or rejects the paired SELL/BUY atomically. Execution must omit both legs if
  the replacement is rejected or cannot meet the minimum trade requirement.
- Execution output groups all approved SELLs before all approved BUYs, including roll legs.

### Risk limits

- **Single-position cap:** `3 × NAV / max_positions`.
- **Sector cap:** one-third of NAV per sector, measured using post-trade marked position
  values.
- The former 10% single-position caps are removed; the 3× rule is the sole position cap.
- Risk evaluates current holdings plus proposed trades, subtracting confirmed SELLs and
  replacing roll incumbents with their proposed replacements.
- A naturally grown existing position above the cap is reported as a breach; it does not
  trigger an automatic SELL in this first increment. Do not add to an over-limit position.
- A sector breach rejects the exposure-increasing BUY or ROLL pair; it does not force-close
  existing holdings.
- Recommended first-version rejection behavior: rejected allocations remain uninvested as
  cash. Do not redistribute them to other positions in the same run.
- If a held position cannot be assigned to a sector, report the missing classification and
  block additional exposure to that unknown sector. Do not silently omit it from sector
  totals.

## Agreed Policies and Setup

- FinHub bid is the held-warrant valuation mark. Quotes must have an aware UTC timestamp no
  older than `portfolio.quote_max_age_hours` (72 hours by default) and EUR currency; non-EUR conversion is deferred. Depot marks may be
  reconciled but do not silently replace invalid FinHub quotes.
- Missing/stale/invalid quotes block new BUY/ROLLs, never count as zero value, and are surfaced
  with the affected ISIN. Separately planned SELLs remain available.
- Expected net proceeds from ordinary SELLs and roll-incumbent SELLs fund same-run BUY sizing.
  Execution emits all SELLs before all BUYs; the operator verifies actual fills/cash before
  placing BUYs.
- Standard BUY sizing divides spendable funds across available BUY slots after planned closes,
  including roll replacements, rather than across configured `max_positions` when fewer slots
  are open.
- The Comdirect fee schedule is implemented in `app/policies/transaction_costs.py`: €4.90
  Grundentgelt + 0.25% Orderprovision, clamped to €9.90–€59.90, with €0.00 venue charge;
  issuer-action costs €3.90 and issuer-no-fee-action costs €0.00 (which wins if both flags
  are true). Slippage defaults to 25 basis points and is overridable per Quant System; this
  initial estimate should be calibrated from fill evidence. Slippage applies to trade
  notional, without double-counting the spread.
- Sector data comes from Research fundamentals joined by underlying ISIN. Missing held-sector
  mappings are reported and prevent unverified exposure increases.

## Implementation phases

### P0 — Confirm data and accounting contract — policy decisions complete

1. Inspect real and virtual snapshot examples and FinHub warrant-detail responses for quote
  price, currency, timestamps, current values, and cash fields. FinHub's additive
  `timestamp_utc` fields have been verified on both endpoints; the v1 quote-age and currency
  policies are agreed.
2. Use the 25 bps initial slippage estimate and verify sector mapping coverage for held
  underlyings; calibrate slippage from fill evidence later. Quote-age, EUR-only, and same-run
  sale-funding policies are agreed.
3. Define typed input/output fields with unambiguous units. `Position.quantity` is held units;
  `PlannedPosition.notional_eur` and BUY `Order.notional_eur` are EUR amounts.

**Verify:** contract tests for EUR-only quotes, quote-age handling, missing prices, and
consistent real/virtual cash extraction.

### P1 — Supply an account snapshot to Portfolio — implemented

1. Add an account/portfolio snapshot contract containing EUR cash, held positions with
   instrument units and EUR marked values, valuation timestamp/source, and underlying/sector
   metadata where available.
2. Update the orchestrator adapter to preserve snapshot values and add FinHub marks according
   to the agreed quote policy.
3. Replace stale run-level capital as the sizing source with the current NAV/cash snapshot;
   retain configured capital only as an explicit fallback if agreed, never as silent live
   account truth.

**Verify:** virtual/real account snapshot tests, fresh quote valuation, and stale/non-EUR quote
rejection are implemented. More coverage for missing snapshots remains appropriate.

### P2 — Implement Portfolio sizing and roll allocation — implemented, costs require configuration

1. Use one shared `max_positions` value for monitoring capacity, Portfolio, and Risk.
2. Compute standard BUY size by allocating opening cash plus expected net proceeds from all
  planned SELLs across vacant slots after planned closes plus roll replacements, reserving
  estimated BUY costs and slippage. Limit ordinary entry count to vacant slots after planned
  SELLs.
3. Apply the same standard BUY size to roll replacements; sell the entire incumbent and keep
   any excess proceeds as cash.
4. Keep rejected/unused allocations as cash; do not enlarge other orders after Risk rejection.
5. Make all allocations and currency units explicit in the proposal and execution plan.

**Verify:** €100,000 NAV / €45,000 cash / six positions yields a fee- and slippage-adjusted
standard BUY; planned SELL proceeds, issuer fee exceptions, and roll sizing are covered.

### P3 — Implement Risk limits — implemented

1. Calculate the per-position limit as `3 × NAV / max_positions`.
2. Calculate sector totals from post-trade marked values, including held positions and
   approved buys, subtracting sells and replacing roll incumbents atomically.
3. Reject only exposure-increasing trades that breach limits; preserve existing positions and
   expose breach reasons.
4. Do not redistribute rejected allocations in this phase.

**Verify:** oversized positions, sector overflow, stale quotes, and existing-breach warnings
are covered. Add remaining boundary/unknown-sector and end-to-end roll-rejection coverage as
appropriate during final verification.

### P4 — Sync user-facing contracts and documentation — implemented

1. Update `docs/agents/portfolio.md`, `docs/agents/risk.md`, and `docs/data-models.md` to
   match runtime inputs, outputs, capital units, and rules.
2. Show NAV, cash, reserve, target BUY size, post-trade exposures, and risk rejection reasons
  in stage results/UI. Portfolio review presents the ordered SELL/BUY/KEEP action plan with
  estimated quantities, current prices, values, weights, and upstream reasons.
3. Update `docs/improvement-roadmap.md` only after focused tests and implementation status
   are complete.

**Verify:** full `uv run pytest tests/ -q` (199 passed), `uv run ruff check .`, and workspace
diagnostics pass.

## Acceptance criteria

- NAV and available cash use current, complete, EUR-denominated account data under an
  explicit quote freshness policy.
- New BUY sizing allocates opening cash plus expected net SELL proceeds across available BUY
  slots after planned closes, including roll replacements, less estimated Comdirect fees and
  slippage; slippage uses the 25 bps default or the Quant System override.
- Portfolio and Risk use the same maximum-position count.
- A roll sells the complete incumbent and buys only the standard per-BUY amount; its paired
  orders are approved/rejected together; all SELLs appear before all BUYs, and BUY sizing
  includes expected net proceeds from planned SELLs.
- Position and sector limits use post-trade marked exposure and cannot be bypassed by rolls.
- Missing quote/sector data is surfaced and handled by explicit policy, never silently treated
  as zero exposure.
- BUY orders express EUR via `notional_eur`; SELL orders express instrument units via
  `quantity`.
- Dry-run remains enabled; no live broker submission is introduced.

## Related plans

- [Improvement roadmap](improvement-roadmap.md)
- [Roll warrant selection](roll-warrant-selection-plan.md)
- [Underlying-volatility leverage](underlying-volatility-leverage-plan.md)
- [Warrant convexity scoring](warrant-convexity-scoring-plan.md)
