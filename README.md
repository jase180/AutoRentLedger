# AutoRentLedger

AutoRentLedger Milestone 17 is a small, read-only Gmail ingestion and deterministic payment-event
pipeline with explicit payer identities, rent accounts, monthly obligations, and manual payment
allocations. It derives each obligation's current reconciliation state without persisting status.
It also derives one operational review list from unresolved identities, unallocated money,
incomplete obligations, and unparsed raw emails. It stores complete original raw MIME bytes in
local SQLite while keeping observed payments and debt records unchanged. A built-in, explicit,
versioned schema upgrade path keeps long-lived local databases compatible.

It does not modify messages or labels, print message bodies, match tenants, assign units or rent
periods automatically, match payments automatically, or calculate late/overdue state.

## Requirements

- Python 3.11 or newer
- A Google account with Gmail
- A Google Cloud project with a Desktop app OAuth client

## Google OAuth setup

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create or select a
   project.
2. In **APIs & Services > Library**, enable **Gmail API**.
3. In **Google Auth platform**, configure **Branding**, **Audience**, and **Data Access**. For a
   personal/testing app, choose **External**, keep the app in testing, and add your Gmail address
   as a test user.
4. In **Google Auth platform > Clients**, select **Create Client** and choose **Desktop app**.
5. Download the client JSON, place it in the project root, and rename it `credentials.json`.

Both `credentials.json` and the generated `token.json` are ignored by Git. Never commit either
file. The app requests only this scope:

```text
https://www.googleapis.com/auth/gmail.readonly
```

On the first run, a browser opens for consent. After approval, Google redirects to a temporary
local server and the app stores a refreshable OAuth token in `token.json`. Later runs reuse it.

## Install and run

PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
autorentledger db status
autorentledger db upgrade
autorentledger search
autorentledger ingest
autorentledger sync
autorentledger parse
autorentledger process
autorentledger payments
autorentledger payments rebuild --dry-run
autorentledger payments rebuild
autorentledger payer add "Alex Example"
autorentledger payer alias-add 1 "ALEX Q EXAMPLE"
autorentledger payer aliases 1
autorentledger payer rename 1 "Morgan Example"
autorentledger payer alias-remove 1 "ALEX Q EXAMPLE"
autorentledger payers
autorentledger unresolved-payers
autorentledger unit add "Unit A"
autorentledger units
autorentledger rent-account add --unit 1 --name "Synthetic Household"
autorentledger rent-account add-payer --account 1 --payer 1
autorentledger rent-accounts
autorentledger rent-account show 1
autorentledger rent-account rename 1 "Example Household"
autorentledger rent-account remove-payer --account 1 --payer 1
autorentledger rent-schedule end 1 --active-to 2026-08-31
autorentledger rent-account end 1 --active-to 2026-08-31
autorentledger obligation add --account 1 --period 2026-08 --amount 1234.56 --due-date 2026-08-01
autorentledger obligations
autorentledger obligation show 1
autorentledger rent-schedule add --account 1 --amount 1450.00 --due-day 1 --active-from 2026-09-01
autorentledger obligations generate --period 2026-09 --dry-run
autorentledger obligations generate --period 2026-09
autorentledger allocation add --payment 1 --obligation 1 --amount 675.00
autorentledger allocations
autorentledger allocation suggestions
autorentledger allocation remove 1
autorentledger reconcile --period 2026-08
autorentledger review
autorentledger overview --period 2026-09
```

macOS or Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
autorentledger db status
autorentledger db upgrade
autorentledger search
autorentledger ingest
autorentledger sync
autorentledger parse
autorentledger process
autorentledger payments
autorentledger payments rebuild --dry-run
autorentledger payments rebuild
autorentledger payer add 'Alex Example'
autorentledger payer alias-add 1 'ALEX Q EXAMPLE'
autorentledger payer aliases 1
autorentledger payer rename 1 'Morgan Example'
autorentledger payer alias-remove 1 'ALEX Q EXAMPLE'
autorentledger payers
autorentledger unresolved-payers
autorentledger unit add 'Unit A'
autorentledger units
autorentledger rent-account add --unit 1 --name 'Synthetic Household'
autorentledger rent-account add-payer --account 1 --payer 1
autorentledger rent-accounts
autorentledger rent-account show 1
autorentledger rent-account rename 1 'Example Household'
autorentledger rent-account remove-payer --account 1 --payer 1
autorentledger rent-schedule end 1 --active-to 2026-08-31
autorentledger rent-account end 1 --active-to 2026-08-31
autorentledger obligation add --account 1 --period 2026-08 --amount 1234.56 --due-date 2026-08-01
autorentledger obligations
autorentledger obligation show 1
autorentledger allocation add --payment 1 --obligation 1 --amount 675.00
autorentledger allocations
autorentledger allocation remove 1
autorentledger reconcile --period 2026-08
autorentledger review
autorentledger overview --period 2026-09
```

## Database schema lifecycle

Initialize a new database or explicitly upgrade an older one before running normal commands:

```powershell
autorentledger db status
autorentledger db upgrade
```

AutoRentLedger uses monotonic `PRAGMA user_version` values and eight built-in schema migrations for
the existing persisted schema epochs: raw emails, payment events, payer identities, rental
accounts, obligations, allocations, rent schedules, and payment-parser provenance. This is a small
application-specific mechanism, not a general migration framework.

Legacy databases with `user_version = 0` are inspected conservatively using known table and column
signatures. Ambiguous or inconsistent schemas are rejected rather than guessed. All required
migrations run in one `BEGIN IMMEDIATE` transaction, followed by foreign-key and integrity checks;
any failure rolls back both schema changes and `user_version`.

Before mutating an existing database, upgrade creates a sibling SQLite backup such as:

```text
data/autorentledger.db.bak-20260823T220000Z
```

Backups, databases, OAuth files, and downloaded email data remain local and are ignored by Git.
A database already at the current version is a no-op and does not create another backup.

Read-only commands never upgrade automatically. Normal read and write commands require the current
schema and provide a clean `autorentledger db upgrade` instruction for missing or outdated
databases. Repository constructors reuse the same centralized table definitions for isolated test
fixtures, while the CLI upgrade command is the authoritative production lifecycle path.

The historical `payment_events.amount_cents` definition does not yet carry the positive-value
database check used by obligations and allocations. Adding it requires a carefully validated SQLite
table rebuild and is intentionally deferred rather than increasing migration risk in this hardening
milestone.

The default Gmail query is `subject:zelle`. Override it when your bank uses different wording:

```powershell
autorentledger search --query 'from:alerts@bank.example (Zelle OR "payment received")' --max-results 25
```

Use non-default credential locations if desired:

```powershell
autorentledger search --credentials C:\secure\gmail-client.json --token C:\secure\gmail-token.json
```

## Raw email ingestion

Refresh normal evidence and immediately summarize current attention with one command:

```powershell
autorentledger sync
```

`sync` first requires the current database schema, then uses the existing Gmail ingestion and
payment-processing services before deriving the canonical review list and conservative actionable
allocation suggestions. It accepts the same practical options as ingestion:

```powershell
autorentledger sync `
  --query "subject:zelle" `
  --max-results 100 `
  --database data/autorentledger.db `
  --credentials credentials.json `
  --token token.json
```

The summary reports new and previously stored email counts, newly created payment events, safe
parse-failure reasons, each current review-kind count, and compact actionable suggestions. A parse
failure for one notification is a reported domain outcome: other messages continue processing and
the command succeeds. Authentication, network, database, or ledger-invariant failures return a
nonzero status.

The stages keep their existing independent durability guarantees. If ingestion succeeds and a
later stage fails structurally, the raw evidence remains stored; rerunning `sync` skips that raw
email download and safely resumes processing. A second unchanged run creates neither duplicate raw
emails nor duplicate payment events, while still showing existing attention and suggestions.

`sync` writes only through the existing `raw_emails` and `payment_events` evidence pipeline. It
does not generate obligations, create or accept allocations, change payer identity or rental
configuration, run maintenance commands, persist review/suggestion results, or create sync history.
Obligation generation and every accounting/configuration decision remain explicit commands.

Run ingestion with the defaults:

```powershell
autorentledger ingest
```

This searches for `subject:zelle` in the current database initialized by `db upgrade`. It never
initializes or upgrades the schema implicitly. Output is limited to safe counts:

```text
Found: 3
Inserted: 3
Already present: 0
```

Running the same command again keeps the database at three messages and reports all three as
already present. Existing Gmail message IDs are checked before their raw MIME is downloaded.

Override the query, limit, or database location:

```powershell
autorentledger ingest --query "subject:zelle newer_than:1y" --max-results 50 --database data/local.db
```

The SQLite database contains private raw email and must remain local. `data/`, SQLite database
files and sidecars, OAuth credentials/tokens, `.eml`, `.mbox`, and Gmail download directories are
all ignored by Git. Tests use synthetic messages only.

## Parse stored notifications

Parse the locally stored messages without writing normalized payments back to SQLite:

```powershell
autorentledger parse
```

Use another local database if needed:

```powershell
autorentledger parse --database data/local.db
```

The command prints normalized fields or a safe failure reason. It never prints raw MIME or full
message bodies. Currency is represented internally as integer cents. Notification dates are
represented as calendar dates because the observed messages do not provide a transaction time or
timezone.

### Privacy-safe format observations

- Chase format: forwarded multipart email with plain-text and HTML alternatives. Sender, amount,
  payment date, and optional memo appear as separate labeled detail lines.
- U.S. Bank format A: forwarded multipart email where sender and amount share a payment summary
  sentence, followed by a labeled received date. A memo is not always present.
- U.S. Bank format B: forwarded multipart email with sender, received date, and memo, but no
  amount in either MIME alternative. The parser rejects this format as `missing_required_amount`
  instead of guessing.

Current local validation: three stored messages, two parsed successfully, and one rejected for a
missing required amount. No real values are recorded in this repository.

## Persist and inspect payment events

Create derived payment events from all unprocessed raw emails:

```powershell
autorentledger process
```

The command skips raw emails that already have a payment event. Parse failures remain only in
`raw_emails` and are retried on later runs so improved parsers can process them in the future.
Output contains counts and safe aggregated failure reasons, never raw message bodies.

List the normalized payment events stored locally:

```powershell
autorentledger payments
```

Both commands accept `--database` to select another local SQLite file. The additive
`payment_events` schema is initialized automatically with `CREATE TABLE IF NOT EXISTS`; existing
raw email rows are unchanged.

```text
payment_events
  id                integer primary key
  raw_email_id      unique foreign key -> raw_emails.id
  provider          text
  sender_name       text
  amount_cents      integer
  occurred_on       nullable ISO date
  memo              nullable text
  parsed_at         UTC ISO timestamp
  parser_version    deterministic parser-contract identifier
```

The unique foreign key enforces at most one payment event per raw email at the database level.
Payment events represent only observed notification facts, not tenant identity or rent status.

### Rebuild derived payments after parser improvements

Raw email MIME remains immutable source evidence. A payment event is a normalized observation
derived by the deterministic parser, so parser improvements are applied to existing events only
through this explicit workflow:

```powershell
autorentledger payments rebuild --dry-run
autorentledger payments rebuild
autorentledger payments rebuild --payment 12 --dry-run
autorentledger payments rebuild --payment 12
```

Dry-run reparses the original raw MIME, compares provider, sender, amount, occurrence date, memo,
and parser provenance, and performs no database writes. Normal output never displays raw MIME or
memo values. A real rebuild updates the existing row in place, preserving both its payment-event
ID and raw-email ID, and refreshes `parsed_at` and `parser_version`.

The current deterministic parser contract is identified by a centralized parser version. Rows
migrated from schema v7 receive the honest marker `legacy-unversioned`; migration does not parse or
rebuild them. Newly processed events record the current parser version. An otherwise identical
legacy row is explicitly updated to current provenance when rebuilt, while an identical already-
current row is left untouched.

Every payment is rebuilt in its own checked transaction. A candidate amount below the money
already allocated from that payment is rejected without changing either the event or allocations.
If the current parser can no longer parse the source, the existing event is retained and the safe
failure reason is reported. One expected rejection or parse regression does not block unrelated
events in the same batch; structural corruption still fails the command.

Rebuild never changes raw email, aliases, rental configuration, schedules, obligations, or
allocations. Sender and occurrence-date corrections can naturally change current identity
resolution, suggestions, review, and occurrence-month reporting because those projections derive
from payment facts. `sync` continues to process only raw emails without an event and never invokes
rebuild automatically. No rebuild history, parser-run history, event-version, or audit table is
stored.

## Manage payer identities

Create and inspect canonical payer identities locally:

```powershell
autorentledger payer add "Alex Example"
autorentledger payers
```

Assign and inspect explicitly chosen sender aliases:

```powershell
autorentledger payer alias-add 1 "ALEX Q EXAMPLE"
autorentledger payer aliases 1
```

Aliases are normalized conservatively by trimming surrounding whitespace, collapsing internal
whitespace, and applying case-insensitive `casefold()` normalization. The entered alias is also
preserved unchanged. A normalized alias can belong to only one payer, and conflicts are rejected
instead of silently reassigned. No payer is created automatically.

Show distinct observed payment senders that do not currently resolve, including their event
counts:

```powershell
autorentledger unresolved-payers
```

Resolution is dynamic. `payment_events.sender_name` remains the original parsed bank evidence;
adding or correcting an alias changes its current payer interpretation without rewriting the
payment event.

```text
payers
  id                integer primary key
  display_name      text (not unique)
  created_at        UTC ISO timestamp

payer_aliases
  id                integer primary key
  payer_id          foreign key -> payers.id ON DELETE RESTRICT
  alias             original entered text
  normalized_alias  unique normalized lookup key
  created_at        UTC ISO timestamp
```

## Manage units and rent accounts

Create and list the local unit inventory:

```powershell
autorentledger unit add "Unit A"
autorentledger units
```

Create a rent account for an existing unit, with optional ISO calendar dates:

```powershell
autorentledger rent-account add `
  --unit 1 `
  --name "Synthetic Household" `
  --active-from 2026-05-01
autorentledger rent-accounts
```

Associate existing payer identities and inspect the account:

```powershell
autorentledger rent-account add-payer --account 1 --payer 1
autorentledger rent-account show 1
```

The association is domain interpretation only. It does not add a rent-account ID to a payment
event, allocate a payment, automatically create an obligation, or calculate a balance.

```text
units
  id                integer primary key
  label             unique nonempty text
  created_at        UTC ISO timestamp

rent_accounts
  id                integer primary key
  unit_id           foreign key -> units.id ON DELETE RESTRICT
  display_name      nonempty text
  active_from       nullable ISO date
  active_to         nullable ISO date, not before active_from
  created_at        UTC ISO timestamp

rent_account_payers
  rent_account_id   foreign key -> rent_accounts.id ON DELETE RESTRICT
  payer_id          foreign key -> payers.id ON DELETE RESTRICT
  created_at        UTC ISO timestamp
  unique            (rent_account_id, payer_id)
```

## Manage monthly rent obligations

Create one explicit obligation for one account and calendar month:

```powershell
autorentledger obligation add `
  --account 1 `
  --period 2026-08 `
  --amount 1234.56 `
  --due-date 2026-08-01
```

List all obligations, optionally filtered to one rent account, and inspect one obligation:

```powershell
autorentledger obligations
autorentledger obligations --account 1
autorentledger obligation show 1
```

Periods must use canonical `YYYY-MM`, amounts are parsed directly into positive integer cents
without binary floating point, and due dates must use `YYYY-MM-DD`. The account must overlap at
least one day of the requested month. Due dates are explicit and are not required to fall inside
the obligation month.

Obligations record only what was owed. They do not reference payment events or store amounts paid
or paid/partial/unpaid status. They may be created manually or by the explicit schedule-generation
command described below; both paths create the same authoritative obligation record.

```text
rent_obligations
  id                integer primary key
  rent_account_id   foreign key -> rent_accounts.id ON DELETE RESTRICT
  period            canonical YYYY-MM
  amount_cents      positive integer
  due_date          ISO date
  created_at        UTC ISO timestamp
  unique            (rent_account_id, period)
```

## Define recurring rent schedules and generate explicitly

A rent schedule is an effective-dated instruction for creating future obligations. It is not debt
and does not participate directly in reconciliation or reporting. Create and inspect schedules:

```powershell
autorentledger rent-schedule add `
  --account 1 `
  --amount 1450.00 `
  --due-day 1 `
  --active-from 2026-05-01

autorentledger rent-schedules
autorentledger rent-schedules --account 1
```

Preview and then explicitly generate one month:

```powershell
autorentledger obligations generate --period 2026-09 --dry-run
autorentledger obligations generate --period 2026-09
```

Dry-run uses the same deterministic plan as generation and performs no writes. Real generation
holds one SQLite `BEGIN IMMEDIATE` transaction while it replans and inserts every missing row, so
an unexpected failure rolls back the entire month. Repeating the command is idempotent: an existing
obligation causes `SKIP`, whether that obligation was generated previously or entered manually.
The existing amount and due date are never compared, corrected, or overwritten.

Schedule ranges for one account may be adjacent but cannot overlap. Rent changes are represented
by ending the old range and adding a new effective-dated schedule, preserving the historical
instruction. Schedule ranges must be contained within any bounds on the rent account. Due days are
limited to 1–28. A schedule active for any part of the requested month creates the full configured
obligation; there is no proration. Generation never runs during reporting, reconciliation, review,
ingestion, processing, or startup.

```text
rent_schedules
  id                integer primary key
  rent_account_id   foreign key -> rent_accounts.id ON DELETE RESTRICT
  amount_cents      positive integer
  due_day           1 through 28
  active_from       ISO date
  active_to         nullable ISO date, not before active_from
  created_at        UTC ISO timestamp
```

No generated-through marker, run history, or generation status is stored. Whether an account/month
has already been generated is derived solely from the unique obligation `(rent_account_id, period)`.

## Apply targeted corrections

Interpretation and configuration can be corrected with six narrow maintenance commands:

```powershell
autorentledger payer rename 1 "Morgan Example"
autorentledger payer alias-remove 1 "ALEX Q EXAMPLE"
autorentledger rent-account rename 3 "Example Household"
autorentledger rent-account remove-payer --account 3 --payer 7
autorentledger rent-schedule end 4 --active-to 2026-08-31
autorentledger rent-account end 3 --active-to 2027-04-30
```

Renaming preserves stable IDs and all relationships. Alias removal uses the same exact normalized
lookup as identity resolution and removes only the alias owned by the named payer. Removing a
payer/account association changes only that current interpretation. These changes naturally affect
derived review items, reports, reconciliation displays, and allocation suggestions without
rewriting a payment event or creating an allocation.

Ending a schedule or rent account changes only its `active_to` date. Existing obligations and
allocations remain historical facts. An account cannot be ended while one of its schedules is
open-ended or ends after the requested account date; end each affected schedule explicitly first.
Schedule ending also enforces account containment and non-overlap without changing adjacent
schedules.

There are deliberately no generic edit or delete commands. Payment evidence and obligation facts
are not manually editable. Allocation corrections continue to use `allocation remove` followed by
`allocation add`. No audit-log framework or maintenance-history table is introduced.

## Allocate payments explicitly

Create one deliberate allocation from an observed payment event to a rent obligation:

```powershell
autorentledger allocation add `
  --payment 1 `
  --obligation 1 `
  --amount 675.00
```

List allocations with optional source or target filters:

```powershell
autorentledger allocations
autorentledger allocations --payment 1
autorentledger allocations --obligation 1
```

Remove an incorrect allocation before adding its replacement:

```powershell
autorentledger allocation remove 1
```

Allocation creation uses one SQLite `BEGIN IMMEDIATE` transaction to load the payment and
obligation, calculate both allocated totals, validate both remaining amounts, and insert. A failed
validation rolls back without inserting a row. Multiple payments may fund one obligation and one
payment may fund multiple obligations, but a payment/obligation pair has only one allocation row.

Remaining amounts are derived arithmetic only. Extra payment money does not create a credit,
prepayment, balance, or unapplied-funds record. Payer-to-account membership is not consulted and
no matching is automatic.

```text
payment_allocations
  id                  integer primary key
  payment_event_id    foreign key -> payment_events.id ON DELETE RESTRICT
  rent_obligation_id  foreign key -> rent_obligations.id ON DELETE RESTRICT
  amount_cents        positive integer
  created_at          UTC ISO timestamp
  unique              (payment_event_id, rent_obligation_id)
```

## Review explainable allocation suggestions

Show conservative suggestions for every payment with one clear destination, or inspect one payment:

```powershell
autorentledger allocation suggestions
autorentledger allocation suggestions --payment 12
```

Suggestions are derived, read-only interpretations—not allocations or accounting state. The service
uses only structured facts already in the ledger: exact payer aliases, payer-to-account
associations, current payment remainders, and canonical reconciliation of actual obligations. It
shows an actionable suggestion only when the sender resolves explicitly, that payer has exactly one
associated rent account, and that account has exactly one outstanding obligation. Multiple accounts
or multiple outstanding obligations are intentionally ambiguous, even when one amount matches.

Payment occurrence month does not determine obligation period: the only outstanding obligation may
be past or future. The suggested amount is the smaller of the current payment remainder and current
obligation remainder. Schedules do not create candidate obligations, and sender memos, raw email,
fuzzy matching, AI, ranking, oldest-first policy, and amount-based account selection are not used.

Every result explains its structured evidence and prints an ordinary command such as:

```powershell
autorentledger allocation add --payment 12 --obligation 8 --amount 1450.00
```

The user must run that command explicitly. It goes through the existing transactional allocation
validation. Suggestions are recomputed each time and therefore change or disappear only when the
authoritative ledger facts change. They are never persisted, accepted, or assigned suggestion IDs;
the existing review workflow remains unchanged until a real allocation is created.

## Reconcile existing obligations

Derive the state of every existing obligation in one canonical month:

```powershell
autorentledger reconcile --period 2026-08
```

`obligation show` uses the same derivation and includes owed, allocated, remaining, and status:

```powershell
autorentledger obligation show 1
```

The storage query groups obligations with a `LEFT JOIN` to allocations, so obligations with no
allocations are included. One canonical service derives exactly these states:

```text
UNPAID   allocated_cents == 0
PARTIAL  0 < allocated_cents < owed_cents
PAID     allocated_cents == owed_cents
```

No reconciliation table or status/balance columns exist. Removing an allocation therefore changes
the next derived result immediately. Due dates are displayed but do not affect classification,
missing obligations are not invented, and extra unallocated payment money does not create an
overpaid state or credit. An allocated total above the obligation amount raises a clear invariant
error rather than hiding corrupt data.

## Review items needing attention

Show the current derived review list:

```powershell
autorentledger review
```

The command reports five neutral categories:

```text
UNRESOLVED_PAYER      observed sender has no explicit payer alias
UNALLOCATED_PAYMENT   payment money remains explicitly unallocated
UNPAID_OBLIGATION     existing obligation has no allocations
PARTIAL_OBLIGATION    existing obligation is only partially allocated
UNPARSED_EMAIL        stored raw email has no payment event
```

These conditions are independent. For example, one payment may have both an unresolved sender and
unallocated money. Due dates do not turn obligation review items into late or overdue items, and
unallocated money is not labeled a credit, overpayment, or error.

Review state is never stored. Adding an alias, allocating remaining money, fully funding an
obligation, or creating a payment event makes the corresponding item disappear on the next run.
The review adapters open the existing SQLite database in read-only mode. Output ordering is stable,
and unparsed-email output is limited to its local raw record ID and subject; raw MIME, decoded body,
memo text, and parser internals are never displayed.

## Monthly reporting and CSV export

Show the owner-facing projection for one canonical month:

```powershell
autorentledger report --period 2026-08
```

Write the same obligation rows as an exact, machine-readable CSV while still printing the terminal
report:

```powershell
autorentledger report --period 2026-08 --csv reports/2026-08.csv
```

The CSV contains `period`, obligation/account/unit identifiers, due date, integer-cent amounts, and
the canonical reconciliation status. Parent directories are created as needed, but an existing
file is never overwritten. The local `reports/` directory is ignored by Git because exports may
contain private rental data.

The two report sections answer different questions:

- Rent totals are grouped by obligation period and reuse canonical reconciliation. Only obligations
  that actually exist for the requested month appear; missing obligations are not inferred.
- Payment intake is grouped by `payment_events.occurred_on`. It sums every allocation originating
  from those payments, including allocations to another month's obligation. Payments with no
  `occurred_on` date are excluded rather than assigned a guessed month.

Observed payment money is labeled neutrally. It becomes satisfied rent only through explicit
allocation to an obligation. Consequently, monthly obligation allocation and monthly payment-side
allocation need not equal one another. Report generation opens read adapters in SQLite read-only
mode, stores no result, changes no schema or ledger row, and treats the CSV as a projection rather
than a source of truth.

## Consolidated owner overview

Open one canonical, local monthly snapshot:

```powershell
autorentledger overview --period 2026-09
```

The overview is a structured Python read model with a terminal formatter. It composes the existing
monthly report, canonical review, conservative allocation suggestions, and read-only obligation
generation plan. This keeps the owner-facing meaning outside the CLI so a future interface can
render the same model without duplicating accounting rules.

Monthly rent totals and account rows include only actual obligations for the requested period and
reuse canonical `PAID`, `PARTIAL`, and `UNPAID` reconciliation. Payment intake separately uses the
existing occurrence-month semantics: all allocations originating from payments observed in that
month count there, even when their destination obligation belongs to another month. These two
sections answer different questions and are not forced to match.

Current attention is the complete operational review state and is not restricted to the requested
month. Actionable suggestions reuse the existing one-account/one-outstanding-obligation policy and
remain advisory only.

Applicable schedules without an actual account/period obligation appear as missing-obligation
warnings. They never create an obligation, and their scheduled amounts are not included in rent
owed. Any existing manual or generated obligation suppresses the warning without comparing its
amount or due date to the schedule. The explicit generation command is shown as the remedy.

Overview is strictly read-only. It does not access Gmail, run sync, ingest or process email,
generate obligations, create allocations, modify configuration, export files, or persist overview,
warning, cache, or dashboard state.

## Development checks

```powershell
pytest
ruff check .
```

## Continuous integration

Pushes and pull requests run the same full `ruff check .` and `pytest` checks on Python 3.11 in
GitHub Actions. CI uses only synthetic/local test fixtures and requires no Gmail credentials,
OAuth token, real email data, or other repository secrets.

## Structure

```text
src/autorentledger/
  email/
    source.py       # provider-neutral message model and EmailSource protocol
    gmail.py        # Gmail OAuth, metadata search, and raw MIME adapter
  storage/
    migrations.py   # schema versions, legacy detection, backup, and atomic upgrade
    sqlite.py       # SQLite schemas and repositories
  identity/
    normalization.py # conservative sender-alias normalization
    service.py       # read-time payer resolution and unresolved inspection
  rental/
    service.py       # unit, account, date, and association validation
  obligations/
    service.py       # period, currency, due-date, and active-range validation
  schedules/
    service.py       # effective rent instructions and explicit atomic generation
  suggestions/
    service.py       # conservative read-only payment destination explanations
  allocations/
    service.py       # explicit allocation validation and error translation
  reconciliation/
    service.py       # read-only amount and status derivation
  reporting/
    service.py       # monthly obligation totals and occurrence-dated payment intake
  review/
    service.py       # derived operational review items
  operations/
    sync.py          # evidence refresh orchestration and safe derived summaries
  overview/
    service.py       # consolidated owner-facing monthly read model
  rebuilding/
    service.py       # explicit parser comparison and checked per-payment rebuild
  parsing/
    mime.py         # source-neutral MIME text decoding
    models.py       # normalized result and structured parse failure
    parser.py       # provider identification and dispatch
    version.py      # centralized current and legacy parser provenance identifiers
    chase.py        # Chase-specific deterministic parser
    us_bank.py      # U.S. Bank-specific deterministic parser
    values.py       # shared exact value normalization
  ingestion.py      # source-to-repository idempotent workflow
  processing.py     # idempotent raw-email to payment-event workflow
  cli.py            # ingestion, ledger-domain, reconciliation, and review commands
tests/              # synthetic adapter, storage, service, and CLI tests
pyproject.toml      # package, command, dependencies, and tool configuration
```

The ingestion service depends on the provider-neutral `EmailSource` interface and raw email
repository. Parsing accepts raw MIME bytes and has no Gmail dependency. Processing connects the
parser to the payment-event repository. Google SDK response objects remain inside the Gmail
adapter, and SQLite has no provider-specific parsing knowledge. Identity resolution depends only
on payment sender values and locally managed payer aliases. Rental-domain logic depends on payer
identities and rental storage, never Gmail, MIME parsing, or payment allocation.
Obligation logic depends only on rent-account records and obligation storage; it never interprets
or mutates payment events. Allocation creation is explicit and transactional; it never infers a
target from payer identity, dates, amounts, memos, or rent-account membership.
Reconciliation reads obligation and allocation totals without modifying either source and derives
all status values through one service path.
Review reuses canonical identity resolution and reconciliation, plus aggregate read-only queries;
it adds no workflow or review-state storage.
Reporting composes canonical reconciliation with one payment-side read query; it stores no totals,
statuses, or exports in SQLite.
Schedule generation uses effective-dated instructions to create ordinary obligations explicitly;
it never modifies an obligation that already exists.
Suggestions combine exact identity, account membership, payment remainder, and canonical
reconciliation without writing or bypassing allocation validation. Sync composes ingestion,
processing, review, and suggestions without adding accounting behavior or persisted run state.
Overview composes reporting, review, suggestions, and schedule planning into one read-only model;
it does not introduce a second accounting engine.
Rebuilding reparses immutable raw evidence through the canonical parser and updates only a checked,
stable payment-event row; it never edits accounting or configuration state.
Normal CLI operations require the current schema; only `db upgrade` changes schema version or
applies migrations.

## Intentionally out of scope

- Tenant legal status, formal leases, proration, deposits, late fees, automatic matching,
  credits/prepayments, tenant balances, overdue/late status, or persisted reconciliation/review
  state
- Fuzzy, phonetic, nickname, typo-correcting, or AI-assisted identity matching
- Gmail writes, label creation, or label changes
- Web UI, visualization, cloud deployment, HTTP/authentication, cron, scheduling, or background
  processing
- Review acknowledgement, dismissal, assignment, tickets, notifications, or scheduled jobs
- External or general-purpose migration frameworks
