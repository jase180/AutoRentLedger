# AutoRentLedger

AutoRentLedger Milestone 1 is a small, read-only Gmail ingestion proof of concept. It searches
for candidate Zelle or payment-notification emails and prints only their Gmail message ID,
received date, sender, and subject.

It does not modify messages or labels. It does not download message bodies, parse amounts,
store data, match tenants, reconcile rent, or implement accounting logic.

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
```

macOS or Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
autorentledger search
```

The default Gmail query is `{zelle "payment notification"}`. Gmail braces mean OR, so this
finds messages containing either `zelle` or the phrase `payment notification`. Override it when
your bank uses different wording:

```powershell
autorentledger search --query 'from:alerts@bank.example (Zelle OR "payment received")' --max-results 25
```

Use non-default credential locations if desired:

```powershell
autorentledger search --credentials C:\secure\gmail-client.json --token C:\secure\gmail-token.json
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
    gmail.py        # Gmail OAuth, search, and metadata mapping adapter
  cli.py            # search command and output formatting
tests/              # adapter and CLI unit tests with no live Gmail access
pyproject.toml      # package, command, dependencies, and tool configuration
```

The CLI depends on the provider-neutral `EmailSource` interface. Google SDK response objects
remain inside the Gmail adapter, allowing another input adapter to be introduced later.

## Intentionally out of scope

- Databases or durable payment storage
- Message-body or payment-amount parsing
- Tenant matching, rent obligations, reconciliation, or accounting
- Gmail writes, label creation, or label changes
- Web UI, visualization, cloud deployment, or background processing
