# AutoRentLedger

AutoRentLedger Milestone 6 is a small, read-only Gmail ingestion and deterministic payment-event
pipeline with explicit payer identity and rent-account relationships. It stores complete original
raw MIME bytes in local SQLite, derives payment events, maps observed sender aliases to
user-managed payer identities, and records which units and rent accounts those payers are
associated with.

It does not modify messages or labels, print message bodies, match tenants, assign units or rent
periods, allocate payments, reconcile rent, or implement accounting logic.

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
autorentledger search
autorentledger ingest
autorentledger parse
autorentledger process
autorentledger payments
autorentledger payer add "Alex Example"
autorentledger payer alias-add 1 "ALEX Q EXAMPLE"
autorentledger payer aliases 1
autorentledger payers
autorentledger unresolved-payers
autorentledger unit add "Unit A"
autorentledger units
autorentledger rent-account add --unit 1 --name "Synthetic Household"
autorentledger rent-account add-payer --account 1 --payer 1
autorentledger rent-accounts
autorentledger rent-account show 1
```

macOS or Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
autorentledger search
autorentledger ingest
autorentledger parse
autorentledger process
autorentledger payments
autorentledger payer add 'Alex Example'
autorentledger payer alias-add 1 'ALEX Q EXAMPLE'
autorentledger payer aliases 1
autorentledger payers
autorentledger unresolved-payers
autorentledger unit add 'Unit A'
autorentledger units
autorentledger rent-account add --unit 1 --name 'Synthetic Household'
autorentledger rent-account add-payer --account 1 --payer 1
autorentledger rent-accounts
autorentledger rent-account show 1
```

The default Gmail query is `subject:zelle`. Override it when your bank uses different wording:

```powershell
autorentledger search --query 'from:alerts@bank.example (Zelle OR "payment received")' --max-results 25
```

Use non-default credential locations if desired:

```powershell
autorentledger search --credentials C:\secure\gmail-client.json --token C:\secure\gmail-token.json
```

## Raw email ingestion

Run ingestion with the defaults:

```powershell
autorentledger ingest
```

This searches for `subject:zelle` and creates `data/autorentledger.db` automatically. Output is
limited to safe counts:

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
```

The unique foreign key enforces at most one payment event per raw email at the database level.
Payment events represent only observed notification facts, not tenant identity or rent status.

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
event, allocate a payment, create an obligation, or calculate a balance.

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

## Development checks

```powershell
pytest
ruff check .
```

## Structure

```text
src/autorentledger/
  email/
    source.py       # provider-neutral message model and EmailSource protocol
    gmail.py        # Gmail OAuth, metadata search, and raw MIME adapter
  storage/
    sqlite.py       # SQLite schemas and repositories
  identity/
    normalization.py # conservative sender-alias normalization
    service.py       # read-time payer resolution and unresolved inspection
  rental/
    service.py       # unit, account, date, and association validation
  parsing/
    mime.py         # source-neutral MIME text decoding
    models.py       # normalized result and structured parse failure
    parser.py       # provider identification and dispatch
    chase.py        # Chase-specific deterministic parser
    us_bank.py      # U.S. Bank-specific deterministic parser
    values.py       # shared exact value normalization
  ingestion.py      # source-to-repository idempotent workflow
  processing.py     # idempotent raw-email to payment-event workflow
  cli.py            # ingestion, payment, identity, unit, and account commands
tests/              # synthetic adapter, storage, service, and CLI tests
pyproject.toml      # package, command, dependencies, and tool configuration
```

The ingestion service depends on the provider-neutral `EmailSource` interface and raw email
repository. Parsing accepts raw MIME bytes and has no Gmail dependency. Processing connects the
parser to the payment-event repository. Google SDK response objects remain inside the Gmail
adapter, and SQLite has no provider-specific parsing knowledge. Identity resolution depends only
on payment sender values and locally managed payer aliases. Rental-domain logic depends on payer
identities and rental storage, never Gmail, MIME parsing, or payment allocation.

## Intentionally out of scope

- Tenant legal status, formal leases, monthly rent obligations, deposits, payment allocation,
  balances, reconciliation, or paid/partial/unpaid status
- Fuzzy, phonetic, nickname, typo-correcting, or AI-assisted identity matching
- Gmail writes, label creation, or label changes
- Web UI, visualization, cloud deployment, or background processing
- Schema migration tooling; the current schemas use additive `CREATE TABLE IF NOT EXISTS`
