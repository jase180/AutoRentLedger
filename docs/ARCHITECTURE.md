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
- A payer is not a rent account, a payment is not an allocation, and a schedule is not an
  obligation.
- Actual obligations state what was owed. Schedules can explicitly generate missing obligations
  but never overwrite existing ones.
- Allocations are the only authoritative link between payment money and obligations. Their totals
  may exceed neither the source payment nor the destination obligation.
- Exact aliases provide identity interpretation. No fuzzy, memo, or AI matching is authoritative.
- Suggestions are derived, conservative, and non-authoritative; users apply allocations explicitly.
- The CLI owns explicit mutations. The authenticated Flask UI remains read-only and loopback-only.
- `sync` and `daily` may refresh raw evidence and payment events only. They never create aliases,
  allocations, or obligations and never rebuild old payments automatically.
- Parser rebuild is explicit, applies only to Gmail-derived events, and cannot reduce a payment
  below its allocated total. Manual events are never reparsed.
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
