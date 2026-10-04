# AutoRentLedger Architecture and Maintenance Notes

AutoRentLedger is a local rent-payment ledger, not a general property-management or accounting
platform. Its purpose is to turn immutable Gmail payment evidence and explicit manual payment
evidence into an explicit, reviewable answer to “who paid what rent?”

## Data flow

```text
Gmail notifications (read-only)       explicit manual evidence
        |                                      |
        v                                      |
immutable raw email evidence                  |
        |                                      |
        v                                      |
deterministic, versioned parser                |
        |                                      |
        +-------------> normalized payment events
        |
        v
identity and rent-account interpretation
        |
        +----------------------+
        v                      v
actual obligations <---- explicit allocations
        |
        v
reconciliation / review / suggestions
        |
        v
CLI and read-only web projections
```

SQLite is the durable local store. Gmail and explicit manual records supply evidence; neither
source decides its payer, rent account, or obligation meaning. Each payment event has exactly one
source: a raw email or a manual evidence row. Reports, review items, suggestions, and owner
overviews are recomputed read models rather than persisted workflow state.

The owner overview combines canonical obligation reconciliation with two explicit read-only facts:
rent-account/payer associations and payment-to-rent allocations. Its latest-payment context is the
most recent non-voided payment with an actual rent allocation to that account; it never infers a
payment from sender identity and never includes late-fee allocations in the displayed rent
contribution. Dated contributions outrank undated ones, with payment-event ID providing the stable
tie-breaker.

## Storage organization

SQLite persistence adapters are grouped by existing domain concern under
`src/autorentledger/storage/`: payments and evidence, manual and Gmail audit history, identity,
rentals, obligations, schedules, allocations, reconciliation, reporting, review, discovery,
suggestions, and allocation planning. Rental-lifecycle persistence has four focused owners:

- `storage/rentals.py` owns structural Unit, Rent Account, and payer-association persistence.
- `storage/tenancy_setup.py` owns the checked atomic guided-tenancy setup transaction.
- `storage/schedules.py` owns recurring schedule persistence and obligation generation.
- `storage/rent_operations.py` owns checked rent-change and tenancy-end lifecycle mutations.

`storage/db.py` contains only the shared connection setup for row access, foreign-key enforcement,
and read-only URI handling.

`storage/__init__.py` remains the compatibility facade for established imports. Schema lifecycle
and historical upgrades remain centralized in `storage/migrations.py`; repository modules must not
duplicate schema definitions or move authoritative write checks outside their transactions.

The rental hierarchy is `Property -> Unit -> Rent Account`. Every Unit belongs to exactly one
Property, and a Unit label is unique only within that Property. Property ID is authoritative;
duplicate Property display names are allowed. The v14 migration materializes one `Default
Property` only when legacy Units exist and preserves every Unit ID and downstream accounting
relationship.

`UnitRecord` is the persisted unit entity with `id`, `property_id`, `label`, and `created_at`.
`UnitContext` is the reusable read-model identity (`property_id`, `property_name`, `unit_id`, and
`unit_label`) used
when account, obligation, allocation, schedule, planning, reconciliation, suggestion, and late-fee
queries project a unit. Unit labels are only unique within a Property, so every user-facing read
path carries Property context and renders `Property Name / Unit Label` where ambiguity matters.
Property names remain live joined presentation metadata; Property IDs do not change the meaning of
rent accounts, obligations, payments, or allocations and are not copied into accounting tables.

The expense hierarchy is separate from rent accounting:

```text
Property -> Expense
             `-> optional Unit from the same Property
```

Expenses are explicit owner-recorded cash outflows. They are not inferred from bank activity.
`property_expenses` stores stable original facts and an optional Unit reference;
`property_expense_voids` records one append-only void reason while the original expense remains.
Property and Unit names are joined dynamically for reads. Expense rows never alter obligations,
payments, allocations, reconciliation, or the rent-focused Overview and monthly report.

Expense categories are controlled machine values: `repairs_maintenance`, `utilities`, `insurance`,
`property_tax`, `management`, `cleaning`, `landscaping_snow`, `pest_control`,
`legal_professional`, `capital_improvement`, `supplies`, and `other`. Capital improvements remain
distinct from repairs and maintenance; no depreciation or tax interpretation is performed.

The tenancy lifecycle keeps accounting boundaries separate from actual relationship dates:

```text
START
    Tenancy active_from
        -> actual relationship start
    First-month obligation
        -> optional explicit one-off rent charge
    Recurring rent schedule
        -> starts on a first-of-month boundary
MIDDLE
    Recurring schedule
        -> normal durable monthly obligations
END
    Tenancy active_to
        -> actual move-out date
    Final-month obligation
        -> optional explicit one-off rent charge
    Recurring rent schedule
        -> stops before an explicit partial final month
```

For a mid-month tenancy, recurring rent requires an explicit first-of-month `rent_effective`.
Any agreed partial-month charge is stored as an ordinary `rent_obligations` row; no proration
formula or special accounting state exists. The setup preview is read-only, while apply inserts the
account, optional first-month obligation, and optional recurring schedule in the same checked
transaction. A first-of-month tenancy remains backward compatible: omitted `rent_effective`
defaults to `active_from`.

For a partial-month end, the business operation requires either an exact final-month amount or an
explicit no-charge choice. It stores `rent_accounts.active_to` as the actual move-out date while
ending recurring schedule applicability on the preceding month-end. An explicit amount becomes a
normal `rent_obligations` row for the final period. Account update, schedule shortening, and optional
obligation insert share one checked transaction. Existing final-period obligations are never
overwritten, and all older schedules, obligations, payments, and allocations remain historical facts.
End-of-month termination keeps the schedule applicable through the final full month.

The generic schedule rule is unchanged: a schedule overlapping any part of a month may generate a
full monthly obligation. Normal setup and high-level tenancy end prevent accidental partial-month
generation by placing recurring applicability outside the explicit first/final month. Existing
unique account/period obligation identity prevents replacement or duplication. No first- or
final-month proration formula exists.

Property Cash Summary is a derived, read-only model over the same canonical facts. It persists no
snapshots, cached totals, or rollups and leaves the schema at v15. For one Property and month:

- Rent owed is the sum of durable monthly rent obligations. Schedules are not debt.
- Rent collected is the sum of rent allocations to those obligations. Payment occurrence dates do
  not select the accounting month.
- Operating expenses are active expenses whose `occurred_on` is in the month, excluding
  `capital_improvement`.
- Capital improvements are active `capital_improvement` expenses for that month.
- Net cash before debt is collected rent minus operating expenses and capital improvements.

The read model joins Property and Unit names dynamically, retains Property isolation even when Unit
labels repeat, and exposes rent-account, expense-category, and active-expense breakdowns. It does
not include late fees, bank activity, security deposits, mortgages, debt service, depreciation, or
tax semantics. Property Cash is not profit, NOI, taxable income, or cash after debt service.

## Presentation and orchestration organization

Each domain module under `src/autorentledger/cli/` owns both parser registration and the thin
`argparse.Namespace` adapter that dispatches its commands. Identity commands live in
`cli/identity.py`, Property/Unit/rent-account structure lives in `cli/rentals.py`, and tenancy
setup/end lifecycle commands live in `cli/tenancy.py`. The stable `autorentledger.cli:main`
entrypoint explicitly assembles those modules, applies only the shared schema preflight, and invokes
the registered leaf handler. Command adapters continue to delegate validation, business rules, and
writes to existing services and repositories.

Web read composition and route registration are grouped by screen under
`src/autorentledger/web/composition/` and `src/autorentledger/web/routes/`. Focused route modules
register on the same `web` blueprint, preserving established URLs and endpoint names. Routes stay
thin and authenticated, and the web surface remains inspection-only.

## Invariants to preserve

- Evidence origin is not accounting meaning. Raw MIME stays immutable; manual evidence records a
  payment observed outside Gmail; both normalize into payment events.
- Manual evidence is append-audited. Corrections preserve the original evidence, append a full
  effective-state revision, and update the same normalized payment projection atomically. A void
  appends history and deactivates that projection without deleting either record.
- Gmail evidence is immutable. An explicit Gmail-payment void appends a separate audit record and
  deactivates the same normalized payment event without changing its ID, parsed facts, or raw email.
- A payer is not a rent account, a payment is not an allocation, and a schedule is not an
  obligation.
- Actual obligations state what was owed. Schedules describe recurring terms and can generate a
  missing obligation, but never count as debt or overwrite an existing obligation. `daily` invokes
  the canonical `ensure_monthly_rent` operation for the host-local current month only; manual
  generation remains available for explicit repair/backfill. Effective-dated rent changes create
  schedule history, and ending a tenancy stops future applicability without deleting history.
- Rent obligation != late-fee charge. Explicit assessments live in `late_fee_charges`, linked to
  an obligation for context only. Original assessment facts are retained; `late_fee_voids` records
  the waiver/void reason and timestamp atomically with the charge's `voided_at` projection.
  Active fees derive UNPAID/PARTIAL/PAID only from explicit `late_fee_allocations`; voided fees
  have primary state VOIDED. Fees never change rent reconciliation. No automatic assessment or
  legal-entitlement logic exists. The CLI owns assessment, void, and allocation; web detail is
  read-only.
- `payment_allocations` links payment money to rent obligations. `late_fee_allocations` separately
  links payment money to late-fee charges. Rent allocation plus late-fee allocation may not exceed
  the source payment, and neither destination may receive more than its own remaining balance.
  This is one combined payment-capacity invariant, not a polymorphic allocation model.
- Exact aliases provide identity interpretation. No fuzzy, memo, or AI matching is authoritative.
- Suggestions are derived, conservative, and non-authoritative; users apply allocations explicitly.
- Historical allocation plans are ephemeral and review-first. They require exact identity and an
  unambiguous explicit account association, then simulate oldest-outstanding-first. Chronology is
  only a deterministic planning heuristic, never evidence of which rent month a payment satisfies.
  The planner remains rent-only but subtracts existing late-fee allocations from payment capacity.
- The CLI owns explicit mutations. The authenticated Flask UI remains read-only and loopback-only;
  allocation-plan and drill-down pages compose the canonical planner, audit, allocation, and
  reconciliation services used by terminal workflows.
- `sync` refreshes raw evidence and payment events only. After a verified backup and successful
  sync, `daily` also ensures current-month rent obligations, then recomputes review/suggestions.
  Neither operation creates aliases, allocations, or late fees, and neither rebuilds old payments.
- Parser rebuild is explicit, applies only to Gmail-derived events, and cannot reduce a payment
  below its combined rent-and-fee allocated total. Manual events are never reparsed.
- Manual correction cannot reduce a payment below its combined allocated total, and either payment
  void path requires zero allocations of both kinds. A fee must likewise have zero fee allocations
  before void. None of these operations changes aliases, obligations, or allocation targets.
- Restore validates a current-schema candidate and never silently migrates it.

## Dependency maintenance

Direct dependencies in `pyproject.toml` use lower bounds plus major-version upper bounds. Do not
pin every transitive package or introduce another dependency manager without a concrete need.
For an intentional dependency update:

1. review and adjust the direct bound in `pyproject.toml`;
2. install the project into a fresh virtual environment;
3. run `pytest` and `ruff check .`;
4. exercise schema, Gmail-fake, backup/restore, and web tests through the full suite; and
5. push only after GitHub CI passes.

Keep fixtures synthetic and keep credentials, tokens, databases, backups, reports, and raw email
outside Git.
