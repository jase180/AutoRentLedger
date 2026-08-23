# AutoRentLedger

AutoRentLedger Milestone 3 is a small, read-only Gmail ingestion and deterministic parsing tool.
It stores complete original raw MIME bytes in local SQLite, then converts supported Zelle
notifications into source-neutral payment notifications without persisting parsed payments.

It does not modify messages or labels, print message bodies, match tenants, persist payment
records, reconcile rent, or implement accounting logic.

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
    sqlite.py       # SQLite schema and raw email repository
  parsing/
    mime.py         # source-neutral MIME text decoding
    models.py       # normalized result and structured parse failure
    parser.py       # provider identification and dispatch
    chase.py        # Chase-specific deterministic parser
    us_bank.py      # U.S. Bank-specific deterministic parser
    values.py       # shared exact value normalization
  ingestion.py      # source-to-repository idempotent workflow
  cli.py            # search, ingest, and local parse commands
tests/              # synthetic adapter, storage, service, and CLI tests
pyproject.toml      # package, command, dependencies, and tool configuration
```

The ingestion service depends on the provider-neutral `EmailSource` interface and raw email
repository. Parsing accepts raw MIME bytes and has no Gmail dependency. Google SDK response
objects remain inside the Gmail adapter, and SQLite has no provider-specific parsing knowledge.

## Intentionally out of scope

- Persisting parsed payments or introducing a payment ledger
- Tenant matching, rent obligations, reconciliation, or accounting
- Gmail writes, label creation, or label changes
- Web UI, visualization, cloud deployment, or background processing
- Schema migration tooling; Milestone 2 initializes its single table directly
