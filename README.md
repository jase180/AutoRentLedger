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
- Models units, household-style rent accounts, recurring schedules, and monthly obligations.
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
       |                                rent account ------> unit
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
| Rent account | The household/account associated with a unit and eventual rent responsibility. |
| Obligation | The authoritative fact that a specific account owed an amount for a month. |
| Allocation | The explicit accounting link saying part of a payment satisfies an obligation. |

Schedules are instructions for explicitly creating future obligations; they are not debt. Reports,
review items, suggestions, and the owner overview are read-only projections of existing facts.

## Quick start

Requirements:

- Python 3.11 or newer
- A Google account with Gmail
- A Google Cloud project with the Gmail API and a Desktop OAuth client

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

### Gmail OAuth setup

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

## Routine workflow

Normal evidence refresh:

```powershell
autorentledger sync
autorentledger overview --period 2026-09
```

For one externally scheduled run with a verified pre-sync backup:

```powershell
autorentledger daily
```

`daily` performs one run only. It retains the newest 30 recognizable daily backups by default;
change that positive limit with `--keep-backups`. Windows Task Scheduler, cron, or another external
scheduler decides when it runs; AutoRentLedger contains no scheduler or daemon.

At the beginning of a month, preview and explicitly create scheduled obligations first:

```powershell
autorentledger obligations generate --period 2026-09 --dry-run
autorentledger obligations generate --period 2026-09
autorentledger overview --period 2026-09
```

`sync` may add raw emails and new payment events. It does not generate obligations, create
allocations, rebuild old payment events, or change identity/rental configuration.

### Bootstrap one tenancy

Preview a new unit, rent account, payer, aliases, association, and optional schedule in one
deterministic plan:

```powershell
autorentledger setup tenancy `
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
model, obligations, payments, or allocations. Payers remain distinct from rent accounts, and
sender resolution remains exact after conservative alias normalization. See the runbook for new
and reused-record examples.

### Local read-only web view

Serve the same canonical owner overview in a local browser:

```powershell
autorentledger web --database data/autorentledger.db --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000/`. The local browser includes **Overview**, **Attention**,
**Payments**, and **Obligations**. Attention is the global/current derived review queue. Payments
shows normalized payments with current exact-alias payer interpretation and payment-centric
allocated/unallocated amounts. Obligations shows month-scoped canonical reconciliation for actual
obligations only; schedules and missing-obligation warnings remain separate on Overview. Every
screen is read-only; none query Gmail, sync, generate obligations, create
allocations, or expose ledger write routes. The UI requires one owner password configured through
`AUTORENTLEDGER_WEB_PASSWORD_HASH` and `AUTORENTLEDGER_WEB_SECRET_KEY`; neither value belongs in
Git or SQLite. Flask still accepts only `127.0.0.1`, `localhost`, or `::1` and rejects direct
LAN, Tailscale-IP, and public binding. See the runbook for private Tailscale Serve access.

## Common commands

| Task | Command |
| --- | --- |
| Refresh evidence and current attention | `autorentledger sync` |
| Create a verified backup, sync, and summarize attention | `autorentledger daily` |
| Open the local read-only browser view | `autorentledger web` |
| Preview one guided tenancy setup | `autorentledger setup tenancy ...` |
| Inspect the owner dashboard | `autorentledger overview --period YYYY-MM` |
| Inspect focused exceptions | `autorentledger review` |
| List normalized payments | `autorentledger payments` |
| Record historical/manual payment evidence | `autorentledger payment manual-add --sender "Synthetic Tenant" --amount 1450.00 --date 2026-05-03` |
| Correct a manual payment without replacing its original evidence | `autorentledger payment manual-correct ID --date 2026-05-04 --reason "Date entered incorrectly"` |
| Void an erroneous unallocated manual payment | `autorentledger payment manual-void ID --reason "Duplicate historical entry"` |
| Inspect a manual payment's audit history | `autorentledger payment manual-history ID` |
| Preview conservative allocation suggestions | `autorentledger allocation suggestions` |
| Allocate payment money explicitly | `autorentledger allocation add --payment ID --obligation ID --amount 675.00` |
| Preview monthly obligation generation | `autorentledger obligations generate --period YYYY-MM --dry-run` |
| Show monthly reconciliation | `autorentledger reconcile --period YYYY-MM` |
| Show/export a monthly report | `autorentledger report --period YYYY-MM --csv reports/YYYY-MM.csv` |
| Check database health | `autorentledger db check` |
| Create a verified backup | `autorentledger db backup` |

Most database-backed commands accept `--database`; the default is `data/autorentledger.db`.
Detailed setup, corrections, troubleshooting, rebuild, and recovery procedures are in the
[operational runbook](docs/RUNBOOK.md).

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
- Allocations are created and removed explicitly; suggestions never apply themselves.
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
The current SQLite schema version is 10.

The application uses Python, standard-library `sqlite3`, and a small service/repository structure
under `src/autorentledger/`. Gmail remains behind an email-source adapter; domain and read-model
services do not depend on Google SDK objects.

## Documentation

- [Operational runbook](docs/RUNBOOK.md): normal operation, common scenarios, troubleshooting,
  parser rebuild, and database recovery.
- [Architecture and maintenance notes](docs/ARCHITECTURE.md): source-of-truth boundaries,
  invariants, and dependency-update procedure.

## Explicit non-goals

AutoRentLedger is not a lease manager, tenant balance system, general ledger, or full
property-management platform. It does not model security deposits, late fees, credits, expenses,
NOI, double-entry bookkeeping, or AI/fuzzy payment matching. It has no public/write-capable web UI,
internal scheduler, background jobs, cloud backup, or automatic accounting policy.
