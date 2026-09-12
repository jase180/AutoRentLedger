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
  the same canonical generator for the host-local current month only; manual generation remains
  available for explicit historical or future periods.
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
