# Agent Spec: Portfolio Construction Agent

## Responsibility

Construct the target warrant portfolio from selected entry warrants, current holdings,
account cash, and confirmed roll replacements. Current holdings are valued at fresh FinHub
bid prices. Portfolio emits explicit EUR notionals for planned BUYs; held instrument units
remain represented separately as `Position.quantity`.

## Inputs

- Selected warrants, converted to a `SelectionResult` for the agent contract.
- Current held warrant positions from the linked real or virtual depot.
- Real-depot free cash is the sum of the newest EUR balances for Girokonto, Tagesgeld
  PLUS-Konto, and Verrechnungskonto; account balances are selected per account type because an
  unchanged balance may not receive a new timestamp.
- `PortfolioAccountSnapshot` with current EUR cash, held positions' EUR bid values, quote
  timestamps, underlying ISINs/symbols, sectors, and valuation issues.
- Confirmed roll replacements and incumbent ISINs from Warrant Selection.
- Shared `max_positions` and configurable slippage-basis-point setting. Per-order fees follow
  the Comdirect schedule in `app/policies/transaction_costs.py`.

## Output

`PortfolioProposal` contains:

- `positions`, `new_positions`, and `existing_positions` as `PlannedPosition` values with
  `notional_eur`, target weight, underlying ISIN/symbol, and sector.
- `close_positions` as held `Position` values; their `quantity` is instrument units.
- `roll_trades` as atomic incumbent `Position` / replacement `PlannedPosition` pairs.
- The account snapshot, standard BUY amount, expected net SELL proceeds, cost reserve, and an
  explicit sizing-block reason when inputs are incomplete or cost assumptions are unset.

## Sizing

For `equal` sizing, calculate BUY-slot capacity as vacant slots after planned close SELLs plus
planned roll replacements. The standard per-BUY amount allocates opening cash plus expected
net proceeds from every planned SELL across that capacity, reserving transaction fees and
slippage for planned BUYs. It is not divided by configured `max_positions` when fewer slots
are available. Only vacant slots after planned SELLs are available to ordinary entries; rolls
replace occupied slots but their proceeds join the same run-wide BUY funding pool.

When fewer entry candidates are selected than the available slot capacity, keep the same
per-slot amount and leave the unallocated balance in cash. Do not increase individual BUYs to
consume cash reserved for unfilled slots. Risk-rejected allocations also remain cash.

BUYs are not sized unless NAV, cash, quote validity, and slippage input are available. Slippage
defaults to 25 bps and can be overridden per Quant System. Fees follow the Comdirect schedule
and issuer-action flags.
Unused or risk-rejected allocations remain cash; they are not redistributed.

## Configuration

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `portfolio.max_positions` | `15` | Shared target breadth and position-slot limit |
| `portfolio.slippage_bps` | `25` | Global slippage allowance in basis points; overridable per Quant System |
| `portfolio.quote_max_age_hours` | `72` | Maximum age of a held-warrant quote for NAV/risk validation |
| `portfolio.sizing_method` | `equal` | Equal sizing is the account-backed v1 method |

Per-order fees are computed by `app/policies/transaction_costs.py`:

- Standard order: €4.90 Grundentgelt + 0.25% Orderprovision, with a €9.90 minimum and €59.90
  maximum; Börsenplatzentgelt is €0.00.
- `issuer_action`: €3.90 total fee.
- `issuer_no_fee_action`: €0.00 total fee; this takes precedence if both flags are true.

## Roll behavior

- Sell the full incumbent and buy the replacement at the standard per-BUY amount.
- Include expected net incumbent sale proceeds in the shared run-wide cash pool; do not size
  the replacement directly to consume the incumbent's full value.
- Risk approves/rejects both legs together. Execution lists all approved SELLs before any
  BUYs and omits both roll legs if the replacement is rejected or undersized.

## Quote and identity rules

- Use FinHub bid for held-warrant liquidation value.
- A risk-increasing plan requires a timezone-aware UTC quote no older than
  `portfolio.quote_max_age_hours` (72 hours by default) and EUR
  currency. Missing, stale, invalid, or non-EUR quotes block BUY/ROLL sizing; SELLs remain
  available.
- `underlying_isin` is the canonical join key for sector and exposure aggregation.
  `underlying_symbol` is optional supporting/display metadata.

## Notes

- Planned SELL proceeds are estimates because broker fills are manual. The operator must
  verify the SELLs filled and actual cash is sufficient before placing BUYs.
- The Portfolio stage does not submit orders. Execution remains dry-run/manual.
