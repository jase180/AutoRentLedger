# AutoRentLedger Operational Runbook

This runbook answers “what do I do when X happens?” It assumes the virtual environment is active,
the package is installed, and the default database is `data/autorentledger.db`.

All names, units, dates, IDs, and amounts below are synthetic examples. Add `--database PATH` to a
database-backed command when using a non-default database.

## Contents

- [Before risky work](#before-risky-work)
- [Routine sync](#routine-sync)
- [Scheduled daily operation](#scheduled-daily-operation)
- [Local read-only web view](#local-read-only-web-view)
- [Monthly setup](#monthly-setup)
- [Add a new payer](#add-a-new-payer)
- [Add a unit, rent account, association, and schedule](#add-a-unit-rent-account-association-and-schedule)
- [Payment is unresolved](#payment-is-unresolved)
- [Payment is unallocated](#payment-is-unallocated)
- [Partial payment](#partial-payment)
- [One payment covers multiple months](#one-payment-covers-multiple-months)
- [Someone else pays the rent](#someone-else-pays-the-rent)
- [Rent amount changes](#rent-amount-changes)
- [Tenant or account ends](#tenant-or-account-ends)
- [Wrong payer or account configuration](#wrong-payer-or-account-configuration)
- [Missing obligation](#missing-obligation)
- [Parser improved](#parser-improved)
- [Parser now fails on an old email](#parser-now-fails-on-an-old-email)
- [Database health](#database-health)
- [If the database is unhealthy](#if-the-database-is-unhealthy)
- [Backup](#backup)
- [Restore](#restore)
- [Gmail OAuth problems](#gmail-oauth-problems)
- [Sync finds parse failures](#sync-finds-parse-failures)
- [Review and overview](#review-and-overview)
- [Monthly reporting and CSV](#monthly-reporting-and-csv)
- [Privacy and Git safety](#privacy-and-git-safety)

## Before risky work

Before installing a change that may require a schema upgrade, making a significant manual
configuration change, or starting a major parser/rebuild task, create a recovery point while the
database is still current:

```powershell
autorentledger db check
autorentledger db backup
```

Do not proceed if `db check` reports an unhealthy database. Note the verified backup path printed
by `db backup`. After the work, run `autorentledger db check` again.

If newer code is already installed and reports the database as outdated, `db check` and normal
`db backup` will reject it because both require the current schema. Run `autorentledger db upgrade`;
the schema lifecycle creates a timestamped sibling backup before changing an existing database.

AutoRentLedger does not upload or catalog backups. Its `daily` command creates a verified backup
before sync and, only after sync succeeds, retains a bounded number of recognizable daily backups.
An external scheduler still decides when that command runs.

## Routine sync

Run:

```powershell
autorentledger sync
```

`sync`:

1. requires the current schema;
2. searches Gmail and idempotently stores new raw evidence;
3. processes raw emails that do not yet have payment events;
4. shows the canonical current-attention counts; and
5. shows conservative, actionable allocation suggestions.

It may write only new `raw_emails` and `payment_events`. It does **not** generate obligations,
create allocations, rebuild existing payment events, add aliases, or change payer/rental
configuration.

Useful options:

```powershell
autorentledger sync `
  --query "subject:zelle" `
  --max-results 100 `
  --database data/autorentledger.db `
  --credentials credentials.json `
  --token token.json
```

Running sync repeatedly is safe: existing Gmail messages and payment events are not duplicated.
“Nothing new” does not mean there is nothing requiring attention; the review and suggestion
summaries still reflect current ledger state.

## Scheduled daily operation

Run one automation-safe operation:

```powershell
autorentledger daily
```

The command requires the current schema, creates and verifies a timestamped backup under
`backups/`, and only then authenticates to Gmail and runs the existing sync pipeline. It prints
sync, current-attention, and actionable-suggestion counts. An attention item or suggestion makes
the final `STATUS` section say `Needs attention` but still exits `0`; only an operational failure
exits `1`.

Useful options mirror `sync`, with backup-directory and retention options:

```powershell
autorentledger daily `
  --database data/autorentledger.db `
  --backup-dir backups `
  --keep-backups 30 `
  --query "subject:zelle" `
  --max-results 100 `
  --credentials credentials.json `
  --token token.json
```

If backup creation fails, Gmail authentication, sync, and retention are not attempted. If sync
fails, the verified pre-run backup remains available and retention does not run. After a fully
successful sync, retention keeps the newest 30 files matching the dedicated daily-backup naming
pattern by default. `--keep-backups` accepts another positive integer. Unrelated files, manual
backup names, subdirectories, and symlinks are not eligible. A retention failure exits `1` but does
not undo the completed sync or remove the current verified backup.

Repeated runs are safe because existing Gmail evidence and payment events are idempotent. Exit `0`
means the operation, including retention, completed; `Needs attention` is still an exit-`0` ledger
status. Exit `1` means readiness, backup, Gmail access, sync, or retention failed.

`daily` does not create allocations or aliases, generate obligations, rebuild payments, or change
payers, rent accounts, or schedules. It does not install or configure a schedule.

### Windows Task Scheduler example

In Windows Task Scheduler, create a basic daily task (for example, at 6:00 AM) and configure the
action using paths appropriate for the local checkout:

```text
Program/script: C:\path\to\AutoRentLedger\.venv\Scripts\autorentledger.exe
Arguments:      daily --database data\autorentledger.db --backup-dir backups --keep-backups 30 --credentials credentials.json --token token.json
Start in:       C:\path\to\AutoRentLedger
```

Enable **Run task as soon as possible after a scheduled start is missed** if that behavior is
useful. The time is only an example: Task Scheduler owns the schedule, while `autorentledger daily`
always performs exactly one run. Keep the task's working directory and OAuth files private. On
cron or another scheduler, invoke the same command from the project directory with equivalent
local paths.

## Local read-only web view

Configure single-owner authentication in the PowerShell session that will start the server. These
commands prompt without echoing the password and generate a random Flask signing key:

```powershell
$env:AUTORENTLEDGER_WEB_PASSWORD_HASH = python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass('AutoRentLedger password: ')))"
$env:AUTORENTLEDGER_WEB_SECRET_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep both values private and outside the repository. AutoRentLedger stores neither value in
SQLite and refuses to start the web server when either is absent.

Start the local server:

```powershell
autorentledger web `
  --database data/autorentledger.db `
  --host 127.0.0.1 `
  --port 8000
```

Open `http://127.0.0.1:8000/`. The root redirects to the current local month; use the month picker
or navigate directly to `http://127.0.0.1:8000/overview?period=2026-09`.

Use `http://127.0.0.1:8000/attention` for the browser equivalent of the derived review queue. It is
global/current rather than month-scoped, so older unresolved payments, open obligations, and
unparsed notifications remain visible.

Use `http://127.0.0.1:8000/payments` to inspect normalized payments, their current exact-alias payer
interpretation, and allocated/unallocated totals. The optional read-only views
`/payments?unallocated=1` and `/payments?unresolved=1` filter current results; combine both query
parameters to require both conditions.

Use `http://127.0.0.1:8000/obligations?period=2026-09` to inspect actual obligations and their
canonical PAID, PARTIAL, or UNPAID reconciliation for one month. This view is read-only and does
not treat schedules as debt: missing scheduled obligations remain warnings on Overview and never
increase obligation totals until an actual obligation exists.

The pages require the single owner password and otherwise retain their existing read-only
semantics. Authentication uses a signed Flask session cookie; it does not create users, roles,
login history, or database-backed sessions. The only POST routes are login and logout. The web UI
cannot sync Gmail, generate obligations, allocate payments, or otherwise mutate the ledger.

The CLI permits only `127.0.0.1`, `localhost`, and `::1`. It still rejects LAN addresses,
Tailscale IPs, `0.0.0.0`, and public addresses.

For private phone/laptop access, keep AutoRentLedger on `127.0.0.1:8000` and configure
[Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) on the same machine to proxy
that loopback service into your tailnet. For current Tailscale versions, `tailscale serve 8000`
proxies `http://127.0.0.1:8000`; verify the displayed target before using its private HTTPS URL.
Tailscale remains external to AutoRentLedger—there is no Python dependency or API integration.

Do not use router port forwarding, Tailscale Funnel, or direct `0.0.0.0` binding for this app.

## Monthly setup

At the beginning of a month:

```powershell
autorentledger obligations generate --period 2026-09 --dry-run
autorentledger obligations generate --period 2026-09
autorentledger overview --period 2026-09
```

Always inspect the dry-run first. Generation is explicit and transactional. It creates obligations
only from schedules applicable to that month and skips an account/month when any obligation already
exists. Running generation again is idempotent.

A schedule that overlaps any part of the requested month creates a full monthly obligation; there
is no proration. Changing or ending a schedule never changes an obligation that already exists.
Manual obligations remain supported and take precedence by causing generation to skip that month.

## Add a new payer

Create the stable payer identity, then attach the exact observed sender spelling as an alias:

```powershell
autorentledger payer add "Alex Example"
autorentledger payer alias-add 1 "ALEX Q EXAMPLE"
autorentledger payer aliases 1
```

Use the payer ID printed by `payer add`. Alias normalization trims surrounding whitespace,
collapses repeated whitespace, and performs Unicode-aware case normalization. Resolution remains
exact after normalization: there is no fuzzy, nickname, phonetic, memo, or AI matching.

Useful inspection commands:

```powershell
autorentledger payers
autorentledger unresolved-payers
```

Adding an alias changes current interpretation dynamically; it does not edit historical payment
rows.

## Add a unit, rent account, association, and schedule

Create each domain object explicitly and use the IDs printed by the preceding commands:

```powershell
autorentledger unit add "Unit A"
autorentledger rent-account add `
  --unit 1 `
  --name "Synthetic Household" `
  --active-from 2026-05-01
autorentledger rent-account add-payer --account 1 --payer 1
autorentledger rent-schedule add `
  --account 1 `
  --amount 1450.00 `
  --due-day 1 `
  --active-from 2026-05-01
```

Inspect the result:

```powershell
autorentledger units
autorentledger rent-accounts
autorentledger rent-account show 1
autorentledger rent-schedules --account 1
```

A unit is the physical local unit. A rent account is the household/account that will owe rent. A
payer is someone who may send money. These are not interchangeable. One account may have several
payers, and one payer may be associated with several accounts; the latter intentionally prevents
high-confidence automatic suggestions.

Schedules must be contained within the rent account’s active dates, use due days 1–28, and cannot
overlap another schedule for the same account.

## Payment is unresolved

Inspect the exception and normalized sender:

```powershell
autorentledger review
autorentledger payments
autorentledger unresolved-payers
autorentledger payers
autorentledger payer aliases 1
```

If the sender belongs to an existing payer, add its observed spelling as an alias:

```powershell
autorentledger payer alias-add 1 "ALEX Q EXAMPLE"
```

If it is a genuinely new payer, create the payer first as described in
[Add a new payer](#add-a-new-payer).

The payment event remains intact. Alias resolution is derived each time, so adding an alias can
resolve historical payment interpretation without editing payment evidence.

## Payment is unallocated

Inspect the payment, obligations, and advisory suggestion:

```powershell
autorentledger payments
autorentledger obligations
autorentledger allocation suggestions --payment 42
```

Suggestions are read-only and deliberately conservative. They appear only when exact alias
resolution leads to one associated rent account with one outstanding obligation. Multiple accounts
or multiple outstanding obligations are treated as ambiguous even when an amount matches.

Apply the accounting decision explicitly through the existing allocation command:

```powershell
autorentledger allocation add --payment 42 --obligation 8 --amount 675.00
autorentledger obligation show 8
```

No suggestion is auto-applied. Payment money may remain unallocated when that is the truthful state.

## Partial payment

Allocate only the amount actually intended for the obligation:

```powershell
autorentledger allocation add --payment 42 --obligation 8 --amount 675.00
autorentledger obligation show 8
```

If the obligation is larger, reconciliation becomes `PARTIAL`. A later payment can add another
allocation to the same obligation:

```powershell
autorentledger allocation add --payment 43 --obligation 8 --amount 775.00
```

Multiple payments may fund one obligation. One payment may also be only partially allocated. The
application rejects allocations that exceed either the payment’s remaining amount or the
obligation’s remaining amount.

## One payment covers multiple months

Create one explicit allocation per intended obligation:

```powershell
autorentledger allocation add --payment 42 --obligation 8 --amount 1450.00
autorentledger allocation add --payment 42 --obligation 9 --amount 150.00
autorentledger allocations --payment 42
```

The payment occurrence month and obligation period answer different questions. A September payment
can satisfy an August obligation, a September obligation, or both. Monthly payment intake groups
money by `payment_events.occurred_on`; monthly rent reconciliation groups allocations by obligation
period. The totals are not required to match.

## Someone else pays the rent

Do not rename the observed payment sender into the tenant or household. Preserve the payer identity,
then associate that payer with the relevant rent account:

```powershell
autorentledger payer add "Morgan Example"
autorentledger payer alias-add 2 "MORGAN EXAMPLE"
autorentledger rent-account add-payer --account 1 --payer 2
autorentledger rent-account show 1
```

The payer/account association is supporting interpretation, not proof that every payment from that
payer satisfies that account. Each payment allocation remains explicit.

## Rent amount changes

Represent the change with effective-dated schedules. End the old schedule, then create the new one:

```powershell
autorentledger rent-schedule end 4 --active-to 2026-08-31
autorentledger rent-schedule add `
  --account 1 `
  --amount 1500.00 `
  --due-day 1 `
  --active-from 2026-09-01
```

Inspect and generate explicitly:

```powershell
autorentledger rent-schedules --account 1
autorentledger obligations generate --period 2026-09 --dry-run
autorentledger obligations generate --period 2026-09
```

Historical obligations remain unchanged. The new schedule affects only later explicit generation;
it does not rewrite already generated or manually created obligations.

## Tenant or account ends

End schedules first when they extend beyond the proposed account end, then end the account:

```powershell
autorentledger rent-schedule end 4 --active-to 2027-04-30
autorentledger rent-account end 1 --active-to 2027-04-30
```

`rent-account end` refuses to silently truncate an open-ended schedule or one ending after the
requested account date. Ending either record preserves payer associations, obligations,
allocations, and historical schedule facts.

## Wrong payer or account configuration

Use the narrow maintenance commands that match the mistake:

```powershell
autorentledger payer rename 1 "Alex Example"
autorentledger payer alias-remove 1 "ALEX Q EXAMPLE"
autorentledger rent-account rename 1 "Example Household"
autorentledger rent-account remove-payer --account 1 --payer 2
autorentledger rent-schedule end 4 --active-to 2026-08-31
autorentledger rent-account end 1 --active-to 2027-04-30
```

These commands correct display metadata, alias interpretation, payer/account membership, or
effective end dates. They do not casually rewrite raw evidence, payment fields, obligations, or
allocations. To correct an allocation, remove it and add the intended one:

```powershell
autorentledger allocation remove 12
autorentledger allocation add --payment 42 --obligation 8 --amount 675.00
```

Alias removal may make old payments unresolved. Removing a payer/account association may make a
suggestion disappear. That is expected dynamic behavior; the payment event itself is unchanged.

## Missing obligation

The monthly `overview` compares applicable schedules with actual obligations and displays a warning
when a scheduled account/month has no obligation. It never creates the missing row and never adds
the scheduled amount to rent owed.

Preview, then explicitly generate:

```powershell
autorentledger overview --period 2026-09
autorentledger obligations generate --period 2026-09 --dry-run
autorentledger obligations generate --period 2026-09
autorentledger overview --period 2026-09
```

Any existing manual or generated obligation suppresses the warning, even when its amount or due date
differs from the schedule. The obligation is authoritative for that month; there is no automatic
schedule mismatch correction.

## Parser improved

Preview all existing payment events against the current deterministic parser:

```powershell
autorentledger payments rebuild --dry-run
```

Or inspect one payment:

```powershell
autorentledger payments rebuild --payment 42 --dry-run
```

If the differences are expected, apply the explicit rebuild:

```powershell
autorentledger payments rebuild
```

Rebuild reparses the immutable source email and updates the existing derived payment row in place.
The payment ID and raw-email link remain stable, and allocations are not edited. A candidate payment
amount below the amount already allocated is rejected. Sender or occurrence-date changes may
naturally change identity resolution, suggestions, review, and monthly payment intake.

`sync` processes only raw emails without payment events; it never auto-rebuilds old events.

## Parser now fails on an old email

`payments rebuild --dry-run` reports `PARSE_FAILED` with a safe reason. A real rebuild also leaves
the existing payment event unchanged. It does not delete the payment, remove allocations, or turn
the source back into an unparsed notification.

Keep the raw evidence and existing event intact. Treat the result as a parser regression to address
in deterministic parser code, then preview the rebuild again.

## Database health

Run:

```powershell
autorentledger db check --database data/autorentledger.db
```

The read-only check verifies:

- the database exists and matches the current schema;
- `PRAGMA integrity_check` returns `ok`;
- `PRAGMA foreign_key_check` returns no violations; and
- allocations do not exceed either their source payment or destination obligation.

Health checking detects problems only. It does not migrate, repair, delete, or rewrite anything.
An outdated database should be upgraded explicitly with:

```powershell
autorentledger db status --database data/autorentledger.db
autorentledger db upgrade --database data/autorentledger.db
```

Run `db check` after the upgrade.

## If the database is unhealthy

Use this recovery sequence rather than editing SQLite rows manually:

1. Stop the local web server and disable or pause the external `daily` task so the active database
   is not changing.
2. Run `autorentledger db check --database data/autorentledger.db` and retain its safe diagnostic.
3. Identify the latest known-good verified backup in `backups/`. Daily backups use the
   `autorentledger-daily-...db` name; explicitly created and pre-restore backups may have other
   names.
4. Restore the selected candidate:

   ```powershell
   autorentledger db restore backups/SELECTED-BACKUP.db `
     --database data/autorentledger.db
   ```

5. Run `autorentledger db check --database data/autorentledger.db` again.
6. Run `autorentledger sync` to catch up immutable Gmail evidence received after that recovery
   point.
7. Verify `autorentledger overview --period YYYY-MM` and `autorentledger review`, then resume the
   external task and web server.

Restore accepts only a healthy current-schema candidate, preserves the active database when one
exists, stages the replacement, validates it, and rolls back after a failed final validation. It
does not repair or migrate the selected backup.

## Backup

Create a timestamped verified backup under the ignored `backups/` directory:

```powershell
autorentledger db backup --database data/autorentledger.db
```

Or choose a destination:

```powershell
autorentledger db backup `
  --database data/autorentledger.db `
  --output backups/manual-before-change.db
```

The source must be current and healthy. Backup uses SQLite’s backup API, includes committed WAL
content, refuses to overwrite an existing destination, closes both databases, and independently
health-checks the result. The backup is a standalone SQLite snapshot; later source changes do not
change it.

Backups contain private ledger and raw email data. Keep them local and out of Git.

## Restore

Check the active database first when possible, then restore an explicit candidate:

```powershell
autorentledger db check --database data/autorentledger.db
autorentledger db restore backups/manual-before-change.db `
  --database data/autorentledger.db
```

Restore follows this safety sequence:

```text
validate candidate
       |
       v
stage and validate a temporary database beside the active database
       |
       v
preserve and verify the current active database
       |
       v
atomically replace the active database
       |
       v
validate the restored active database; roll back if final validation fails
```

The candidate is immutable input and remains unchanged. It must already use the current schema
version and pass schema, integrity, foreign-key, and ledger checks. Restore never upgrades a backup.
When an active database exists, the command prints the verified pre-restore backup path.

If candidate validation fails, the active database is untouched. If final validation unexpectedly
fails after replacement, AutoRentLedger restores the verified pre-restore database when available
and returns a failure.

## Gmail OAuth problems

Authentication applies to `search`, `ingest`, and `sync`. Confirm that:

1. the Gmail API is enabled in the Google Cloud project;
2. the OAuth client is a Desktop app;
3. the intended Google account may use the consent configuration (for an External testing app, it
   is listed as a test user);
4. `credentials.json` exists at the configured `--credentials` path; and
5. the local browser can complete the temporary localhost consent redirect.

The application requests only `https://www.googleapis.com/auth/gmail.readonly`. It loads
`token.json`, refreshes an expired token when a refresh token is available, and otherwise starts a
new local-server authorization flow.

If the token is invalid, belongs to the wrong account, or must be re-consented after a scope/client
change, move `token.json` to another private local location (or delete it if no longer needed), then
rerun `autorentledger sync`. Do not commit or paste either OAuth file into an issue or log.

For discovery without writing to the database:

```powershell
autorentledger search --query "subject:zelle" --max-results 25
```

## Sync finds parse failures

An individual parse failure is an expected domain outcome, not an infrastructure failure. `sync`
continues processing other messages and can exit successfully while reporting a failure count and
safe reason.

The raw email remains durable. It appears as an unparsed item in `review` and in the overview’s
global `CURRENT ATTENTION` count, preserving evidence for a later deterministic parser improvement.
Do not edit the raw MIME or manually create/change payment fields to hide the failure.

Authentication, Gmail/network, database, or structural invariant failures are command failures.
If Gmail ingestion completed before a later structural processing failure, already stored evidence
may remain. Fix the structural issue and rerun sync; ingestion is idempotent.

## Review and overview

Use the focused exception list when working through operational problems:

```powershell
autorentledger review
```

It derives unresolved payers, unallocated payments, partial obligations, unpaid obligations, and
unparsed emails across the current ledger.

Use the monthly owner dashboard for the consolidated picture:

```powershell
autorentledger overview --period 2026-09
```

Its monthly rent and payment-intake sections are period-based. `CURRENT ATTENTION` remains
global/current and can include older items. The overview also shows actionable suggestions and
missing scheduled-obligation warnings. It is strictly read-only: it does not sync Gmail, process
emails, generate obligations, or create allocations.

## Monthly reporting and CSV

Display the read-only monthly report:

```powershell
autorentledger report --period 2026-09
```

Export one row per actual obligation with exact integer-cent values:

```powershell
autorentledger report --period 2026-09 --csv reports/2026-09.csv
```

The command still prints the terminal report and refuses to overwrite an existing CSV. Reports and
CSV files are derived projections, not sources of truth. Obligation totals use obligation period;
payment intake uses payment occurrence date and includes allocations from those payments even when
they target another month.

## Privacy and Git safety

Never commit operational data or secrets:

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

Real payer names, aliases, unit labels, account names, payment/rent amounts, Gmail IDs, raw MIME,
memos, reports, databases, and backups are private. Tests and documentation must use synthetic data.

Before committing:

```powershell
git status
git diff --cached
```

Confirm every staged file is expected source, test, or privacy-safe documentation. Do not use manual
SQLite edits as a normal correction workflow; use the explicit domain commands in this runbook.
