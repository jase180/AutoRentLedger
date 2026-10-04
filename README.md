# AutoRentLedger

AutoRentLedger turns messy Zelle notification evidence into a trustworthy, auditable answer to:
**who paid what rent?**

It is a small, local Python application. It reads payment notifications from Gmail with read-only
permission, preserves the original email evidence in SQLite, accepts explicit evidence for
legitimate historical payments that have no email, normalizes both sources into payment events,
and connects money to monthly rent only through explicit allocations. It is deliberately not a
general property-management system.

## What it does

- Searches Gmail for candidate payment notifications without modifying messages or labels.
- Stores immutable raw MIME evidence locally and ingests it idempotently.
- Stores explicit manual evidence for historical payments that did not originate in Gmail.
- Preserves append-only correction and void history for manual payment evidence.
- Deterministically parses supported Chase and U.S. Bank Zelle notifications into payment events.
- Resolves observed sender names through explicitly managed payer aliases.
- Models properties, units, household-style rent accounts, recurring schedules, and monthly obligations.
- Requires an explicit allocation before payment money satisfies an obligation.
- Derives reconciliation, review items, conservative allocation suggestions, reports, and a
  monthly owner overview without persisting those projections.
- Supports explicit parser rebuilds, schema upgrades, database health checks, verified backups,
  and conservative restores.

AutoRentLedger does not automatically identify people, generate obligations during sync, allocate
payments, edit Gmail, or infer rent intent from payment dates, amounts, or memos.

## Core architecture

```text
Gmail notification                         explicit manual evidence
       |                                             |
       v                                             |
immutable raw email evidence                         |
       |                                             |
       +-----------------------> normalized payment event ---------> payer identity / aliases
       |                                      |
       |                                      v
       |                         rent account ------> unit ------> property
       |                                      |
       |       explicit allocation            v
       +--------------------------------> monthly obligation
                                              ^
                                              |
                                  explicit generation from schedule
                                              |
                                              v
                         reconciliation / review / suggestions / overview
```

The important distinctions are:

| Concept | Meaning |
| --- | --- |
| Payment evidence | Either immutable Gmail/raw-email evidence or an explicit manual record of an observed historical payment. |
| Payment event | A normalized payment observation with exactly one evidence source. It is not a tenant. |
| Payer | The identity that sent money. A payer is not a rent account. |
| Property | The rental property that contains one or more units; unit labels are unique only within a Property. |
| Property expense | An explicit owner-recorded cash outflow for one Property, optionally attributed to one of its Units. |
| Rent account | The household/account associated with a unit and eventual rent responsibility. |
| Obligation | The authoritative fact that a specific account owed an amount for a month. |
| Rent allocation | The explicit accounting link saying part of a payment satisfies a rent obligation. |
| Late-fee allocation | A separate explicit link saying part of a payment satisfies a late-fee charge. |

Schedules are instructions for explicitly creating future obligations; they are not debt. Reports,
review items, suggestions, and the owner overview are read-only projections of existing facts.
All user-facing rental reads include Property context, normally as `Property Name / Unit Label`,
so identical unit labels at different Properties remain unambiguous.

## Start the app: complete first-run flow

Requirements:

- Python 3.11 or newer
- A Google account and Google Cloud Desktop OAuth client only if you want to sync Gmail evidence

The application runs directly on Windows PowerShell; WSL is not required. The web UI is local and
read-only. These steps use the default database at `data/autorentledger.db`.

### 1. Open the repository

```powershell
cd C:\path\to\AutoRentLedger
py -3.11 --version
```

### 2. Create the virtual environment and install the app

PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

autorentledger db upgrade
autorentledger db check
```

macOS or Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

autorentledger db upgrade
autorentledger db check
```

`db upgrade` initializes the default database at `data/autorentledger.db` when it does not exist
and explicitly migrates an older database. Normal commands never upgrade the schema implicitly.

If GNU Make is installed, the entire environment/install/database sequence is:

```powershell
make setup
```

Make is optional and is not bundled with Windows. The explicit PowerShell commands above remain
the supported fallback.

### 3. Configure the local web login

The web server refuses to start without a password hash and Flask signing key. Set both in the
same PowerShell window that will run the app:

```powershell
$env:AUTORENTLEDGER_WEB_PASSWORD_HASH = python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass('AutoRentLedger password: ')))"
$env:AUTORENTLEDGER_WEB_SECRET_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The first command prompts for the owner password without echoing it. Remember that password for
the login screen. These environment variables live only in the current PowerShell session; do not
put their values in Git, SQLite, the README, or shell history.

### 4. Start the read-only web app

With the virtual environment activated:

```powershell
autorentledger web --database data/autorentledger.db --host 127.0.0.1 --port 8000
```

Or, with GNU Make:

```powershell
make web
```

Leave that terminal running. Open `http://127.0.0.1:8000/` and sign in with the password chosen in
step 3. The server intentionally accepts loopback hosts only. Stop it with `Ctrl+C`.

### 5. Start it again later

For each new PowerShell session:

```powershell
cd C:\path\to\AutoRentLedger
.venv\Scripts\Activate.ps1
$env:AUTORENTLEDGER_WEB_PASSWORD_HASH = python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass('AutoRentLedger password: ')))"
$env:AUTORENTLEDGER_WEB_SECRET_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
autorentledger db check
autorentledger web --database data/autorentledger.db --host 127.0.0.1 --port 8000
```

Generating a new hash and signing key at startup is safe; use the password entered during that
startup. If you deliberately persist the environment variables outside Git, you can reuse the
same password and sessions instead.

### Optional: connect Gmail evidence

The web app and manually entered ledger data do not require Gmail authorization. To sync Gmail:

1. Create or select a Google Cloud project and enable the Gmail API.
2. Configure the Google Auth Platform consent screen. For a personal Gmail account, an External app
   can remain in testing with that Gmail address added as a test user.
3. Create an OAuth client with application type **Desktop app**.
4. Download its JSON file into the project root as `credentials.json`.
5. Run `autorentledger sync`. The first run opens a browser for consent and writes `token.json`.

The implementation requests only:

```text
https://www.googleapis.com/auth/gmail.readonly
```

Google maintains a current [Gmail Python OAuth quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python).
Both `credentials.json` and `token.json` stay local and are Git-ignored. Never commit either file.

After authorization:

```powershell
autorentledger sync
autorentledger overview --period 2026-09
```

The default Gmail query is `subject:zelle`. Override it when necessary with `--query` and limit
the search with `--max-results`.

## Normal operating workflow

First-time configuration is preview-first and establishes the unit, rent account, payer identity,
aliases, association, optional explicit first-month obligation, and recurring rent schedule in one
reviewed operation:

```powershell
autorentledger setup tenancy ...
autorentledger setup tenancy ... --apply
```

After that, ordinary month-to-month operation is one command:

```powershell
autorentledger daily
```

The schedule remains the recurring rule and each obligation remains a durable monthly charge.
`daily` ensures exactly the current host-local month; it never pre-generates future months.

## Evidence-only workflow

Normal evidence refresh:

```powershell
autorentledger sync
autorentledger overview --period 2026-09
```

For one externally scheduled run with a verified pre-run backup:

```powershell
autorentledger daily
```

`daily` performs one run only: backup, sync, ensure current-month obligations from applicable
schedules, refresh attention, then retain the newest 30 recognizable daily backups. Change the
positive retention limit with `--keep-backups`. Windows Task Scheduler, cron, or another external
scheduler decides when it runs; AutoRentLedger contains no scheduler or daemon. Repeated runs are
idempotent, and only the host-local current month is generated.

Advanced historical/backfill obligation generation remains available explicitly:

```powershell
autorentledger obligations generate --period 2026-09 --dry-run
autorentledger obligations generate --period 2026-09
autorentledger overview --period 2026-09
```

`sync` alone may add raw emails and new payment events but does not generate obligations. `daily`
adds only current-month obligation generation; it never creates allocations or late fees, rebuilds
old payments, or changes identity/rental configuration. Use `--skip-obligations` only as a
one-run recovery/debugging escape hatch.

### Discover historical payment evidence

Before configuring a newly bootstrapped ledger, inspect the evidence already collected:

```powershell
autorentledger discovery payments
```

The read-only report inventories exact observed sender spellings, current exact-alias resolution,
active payment counts and totals, possible same-sender/date/amount notification duplicates, and
unparsed Gmail subjects. Possible duplicates are warnings only: the command never collapses,
voids, or changes payments. It does not infer tenants, units, rent accounts, associations, or
aliases and writes no configuration or accounting state. Review the evidence first, then use
`setup tenancy` for confirmed configuration and explicit obligation/allocation commands for
accounting interpretation. Current-month rent is maintained by `daily` after setup.

### Bootstrap one tenancy

Preview a new unit, rent account, payer, aliases, association, and recurring schedule in one
deterministic plan:

```powershell
autorentledger setup tenancy `
  --property 1 `
  --unit-label "2F" `
  --account-name "Synthetic Household" `
  --active-from 2026-05-01 `
  --payer-name "Synthetic Tenant" `
  --alias "SYNTHETIC TENANT" `
  --rent 1450.00 `
  --due-day 1
```

Preview is the default and writes nothing. Add `--apply` only after reviewing each CREATE or REUSE
action. The command is a convenience wrapper over the existing primitives; it creates no tenant
model, payments, or allocations. It creates an obligation only when `--first-month-rent` is
explicitly supplied. Payers remain distinct from rent accounts, and
sender resolution remains exact after conservative alias normalization. See the runbook for new
and reused-record examples. After setup, `daily` creates missing current-month rent automatically.

#### Mid-month tenancy starts

Tenancy `active_from` records the actual relationship start. Recurring rent starts separately on a
first-of-month boundary. AutoRentLedger never calculates proration; record the exact first-month
rent that was agreed:

```powershell
autorentledger setup tenancy `
  --property 1 `
  --unit-label "2F" `
  --account-name "Synthetic Household" `
  --active-from 2026-10-21 `
  --payer-name "Synthetic Tenant" `
  --first-month-rent 460.00 `
  --first-month-due 2026-10-21 `
  --rent 1300.00 `
  --rent-effective 2026-11-01 `
  --due-day 1
```

Omit `--first-month-due` to use `active_from`. For a free/no-charge partial month, omit
`--first-month-rent` but still provide the next first-of-month `--rent-effective`. For a normal
first-of-month tenancy, `--rent-effective` may be omitted and defaults to `active_from`. A
mid-month tenancy with recurring rent and no `--rent-effective` is rejected as ambiguous.

A schedule overlapping any part of a month can generate a full monthly obligation. Normal setup
avoids that existing behavior by starting recurring rent after the partial first month. The
first-month charge is an ordinary durable rent obligation and participates automatically in
reconciliation, allocation, reporting, Overview, and Property Cash.

### Local read-only web view

Serve the same canonical owner overview in a local browser:

```powershell
autorentledger web --database data/autorentledger.db --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000/`. The local browser includes **Overview**, **Attention**,
**Payments**, **Obligations**, **Expenses**, **Property Cash**, and **Allocation Plan**. Attention is the global/current derived review queue. Payments
shows normalized payments with current exact-alias payer interpretation and payment-centric
allocated/unallocated amounts, and payment IDs open provenance, audit, and allocation details.
Obligations shows month-scoped canonical reconciliation for actual obligations only; account links
open associated payers, monthly obligations, and contributing payments. Allocation Plan renders
the exact M26 CLI preview—including every proposed link and blocking issue—but cannot apply it.
Schedules and missing-obligation warnings remain separate on Overview. Rental rows show live
Property context alongside Unit identity. Every screen is read-only;
none query Gmail, sync, generate obligations, create allocations, or expose ledger write routes.
The UI requires one owner password configured through
`AUTORENTLEDGER_WEB_PASSWORD_HASH` and `AUTORENTLEDGER_WEB_SECRET_KEY`; neither value belongs in
Git or SQLite. Flask still accepts only `127.0.0.1`, `localhost`, or `::1` and rejects direct
LAN, Tailscale-IP, and public binding. See the runbook for private Tailscale Serve access.

## Common commands

| Task | Command |
| --- | --- |
| Refresh evidence and current attention | `autorentledger sync` |
| Back up, sync, ensure current obligations, and summarize attention | `autorentledger daily` |
| Open the local read-only browser view | `autorentledger web` |
| Inventory historical payment evidence before setup | `autorentledger discovery payments` |
| Preview one guided tenancy setup | `autorentledger setup tenancy ...` |
| Change recurring rent from a future month | `autorentledger rent change --account ID --amount 1350.00 --effective 2026-11-01` |
| End recurring rent without deleting history | `autorentledger tenancy end --account ID --active-to 2026-11-30` |
| Inspect the owner dashboard | `autorentledger overview --period YYYY-MM` |
| Inspect focused exceptions | `autorentledger review` |
| List normalized payments | `autorentledger payments` |
| Record historical/manual payment evidence | `autorentledger payment manual-add --sender "Synthetic Tenant" --amount 1450.00 --date 2026-05-03` |
| Correct a manual payment without replacing its original evidence | `autorentledger payment manual-correct ID --date 2026-05-04 --reason "Date entered incorrectly"` |
| Void an erroneous unallocated manual payment | `autorentledger payment manual-void ID --reason "Duplicate historical entry"` |
| Inspect a manual payment's audit history | `autorentledger payment manual-history ID` |
| Inspect a Gmail-derived payment's audit state | `autorentledger payment gmail-history ID` |
| Void a confirmed invalid Gmail-derived payment | `autorentledger payment gmail-void ID --reason "Duplicate forwarded notification"` |
| Preview conservative allocation suggestions | `autorentledger allocation suggestions` |
| Allocate payment money explicitly | `autorentledger allocation add --payment ID --obligation ID --amount 675.00` |
| Preview historical allocations | `autorentledger allocation plan --from 2026-05 --to 2026-08` |
| Apply a fully reviewed historical plan | `autorentledger allocation plan --from 2026-05 --to 2026-08 --apply` |
| Advanced/backfill monthly rent preview | `autorentledger obligations generate --period YYYY-MM --dry-run` |
| Show monthly reconciliation | `autorentledger reconcile --period YYYY-MM` |
| Show/export a monthly report | `autorentledger report --period YYYY-MM --csv reports/YYYY-MM.csv` |
| Check database health | `autorentledger db check` |
| Create a verified backup | `autorentledger db backup` |

Most database-backed commands accept `--database`; the default is `data/autorentledger.db`.
Detailed setup, corrections, troubleshooting, rebuild, and recovery procedures are in the
[operational runbook](docs/RUNBOOK.md).

## Make command shortcuts

The repository `Makefile` wraps the same Python CLI; it does not introduce a second execution
path. Override `DATABASE`, `PORT`, `PERIOD`, or `PROPERTY` at invocation time as needed.

| Task | Make command |
| --- | --- |
| Show available targets | `make help` |
| Create `.venv`, install development dependencies, upgrade and check the database | `make setup` |
| Start the local read-only web app | `make web` |
| Start on another port | `make web PORT=8080` |
| Show, upgrade, or check schema health | `make db-status`, `make db-upgrade`, `make db-check` |
| Create a verified database backup | `make backup` |
| Sync Gmail evidence | `make sync` |
| Run the normal backed-up daily workflow | `make daily` |
| Show monthly owner overview | `make overview PERIOD=2026-10` |
| Show all-Property monthly cash | `make property-cash PERIOD=2026-10` |
| Show one Property's monthly cash | `make property-cash PERIOD=2026-10 PROPERTY=2` |
| List active expenses | `make expenses` |
| Run Ruff, tests, or both | `make lint`, `make test`, `make check` |
| Apply Ruff's safe fixes | `make format` |

The Makefile defaults to `.venv/Scripts/python.exe` on Windows and `.venv/bin/python` elsewhere.
You can override either interpreter, for example `make test PYTHON=python`.

## Safety and privacy

The repository must never contain real operational data. These stay local and are covered by
`.gitignore`:

```text
credentials.json
token.json
data/
backups/
reports/
*.db
*.sqlite
*.sqlite3
*.eml
*.mbox
```

Real payer names, sender aliases, rent amounts, unit labels, raw emails, reports, databases, and
backups are private even when their file type is ignored. Inspect `git status` and staged content
before every commit. Tests and committed documentation use synthetic examples only.

Observed evidence and historical accounting are intentionally protected:

- Gmail access is read-only.
- Raw email evidence is immutable.
- Gmail-derived payment events can only be re-derived through the explicit parser rebuild
  workflow; manual payment events are never parser-rebuild candidates.
- Manual payment corrections append a full effective-state revision while updating the same
  normalized payment projection. Voids deactivate the projection without deleting evidence or
  history; neither operation applies to Gmail-derived payments.
- Existing obligations are never overwritten by schedules.
- Rent and late-fee allocations are created and removed explicitly; suggestions never apply
  themselves. Their combined total may not exceed the payment amount.
- Reporting, review, reconciliation, suggestions, health checks, and overview are read-only.

## Database backup and recovery

Before installing a change that may require a schema upgrade, making significant configuration
changes, or doing parser work, create a recovery point while the database is still current:

```powershell
autorentledger db check
autorentledger db backup
```

Backups use SQLite's backup API, are independently health-checked, and default to the ignored
`backups/` directory. Restore validates a current-version candidate before touching the active
database, preserves the current database to a verified pre-restore backup, stages the replacement,
and rolls back if final validation fails.

If newly installed code already reports the database as outdated, its normal `db backup` command
will refuse the outdated source. Use `db upgrade`; that lifecycle path creates its own timestamped
sibling backup before mutating an existing database.

See [Database health](docs/RUNBOOK.md#database-health),
[Backup](docs/RUNBOOK.md#backup), and [Restore](docs/RUNBOOK.md#restore) for exact procedures.

## Development

Install development dependencies with `python -m pip install -e ".[dev]"`, then run:

```powershell
ruff check .
pytest
```

GitHub Actions runs the same checks on Python 3.11 for every push and pull request. Tests use
synthetic local fixtures and require no Gmail credentials, network access, or operational database.
The current SQLite schema version is 15.

## Property expenses

Record expenses explicitly; AutoRentLedger does not infer them from bank activity:

```powershell
autorentledger expense add --property 1 --unit 2 --date 2026-10-03 --amount 425.00 --category repairs_maintenance --vendor "Example Plumbing" --note "Synthetic repair"
autorentledger expenses
autorentledger expense show 1
autorentledger expense void 1 --reason "Entered twice"
```

Expense categories are controlled and discoverable through `expense categories` or the
`--category` CLI choices. Voiding is audited and retains the original row. The web **Expenses**
page is read-only, and expenses remain excluded from the rent Overview and monthly report.

## Property Cash

Show the derived monthly summary for every Property or drill into one Property:

```powershell
autorentledger property-cash --period 2026-10
autorentledger property-cash --period 2026-10 --property 1
```

The read-only **Property Cash** web page shows the same monthly facts with rent-account, expense
category, and active-expense breakdowns. Rent owed comes only from durable obligations; rent
collected comes from allocations to those obligations, regardless of when the payment occurred.
Operating expenses exclude Capital Improvement, which remains a separate line. Net cash before
debt is collected rent minus both expense groups. It is not profit, NOI, taxable income, or cash
after mortgage/debt service. No summary rows are persisted and the schema remains v15.

The application uses Python, standard-library `sqlite3`, and a small service/repository structure
under `src/autorentledger/`. Gmail remains behind an email-source adapter; domain and read-model
services do not depend on Google SDK objects.

## Documentation

- [Operational runbook](docs/RUNBOOK.md): normal operation, common scenarios, troubleshooting,
  parser rebuild, and database recovery.
- [Architecture and maintenance notes](docs/ARCHITECTURE.md): source-of-truth boundaries,
  invariants, and dependency-update procedure.

## Explicit late fees

`autorentledger late-fee assess`, `late-fee void`, `late-fee history`, and `late-fee list`
record and inspect owner-assessed charges separately from rent obligations. Explicit
`late-fee allocation add/remove` commands link payment money to those charges without changing
rent allocations. Assessments retain their original facts; waivers/voids append an audit record.
Payment and account web details display both allocation types read-only. See
[Late fees](docs/RUNBOOK.md#late-fees) for commands and duplicate protection.

Rent obligation != late-fee charge. Rent reconciliation and late-fee UNPAID/PARTIAL/PAID status
remain separate. A voided charge has primary state VOIDED. Existing late-fee allocations reduce
money available to the rent-only historical planner, but the planner never targets fees. The app
neither automatically assesses nor allocates fees and does not decide whether they are legally
permitted.

## Explicit non-goals

AutoRentLedger is not a lease manager, tenant balance system, general ledger, or full
property-management platform. It does not model security deposits, automatic late-fee policies,
credits, expense inference/imports, NOI, double-entry bookkeeping, or AI/fuzzy payment matching.
It has no public/write-capable web UI,
internal scheduler, background jobs, cloud backup, or automatic accounting policy.
