"""Read-only Gmail implementation of the email source interface."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from autorentledger.email.source import EmailMessageSummary

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = (GMAIL_READONLY_SCOPE,)


class GmailSource:
    """Search Gmail without exposing Google SDK objects to the application."""

    def __init__(self, service: Any) -> None:
        self._service = service

    @classmethod
    def authenticate(
        cls,
        credentials_path: Path = Path("credentials.json"),
        token_path: Path = Path("token.json"),
    ) -> GmailSource:
        """Authenticate an installed application with read-only Gmail access."""
        credentials: Credentials | None = None
        if token_path.exists():
            credentials = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        elif not credentials or not credentials.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), GMAIL_SCOPES)
            credentials = flow.run_local_server(port=0)

        token_path.write_text(credentials.to_json(), encoding="utf-8")
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return cls(service)

    def search(self, query: str, max_results: int = 100) -> list[EmailMessageSummary]:
        """Search Gmail and return source-neutral message metadata."""
        if max_results < 1:
            raise ValueError("max_results must be at least 1")

        messages: list[EmailMessageSummary] = []
        page_token: str | None = None

        while len(messages) < max_results:
            page_size = min(100, max_results - len(messages))
            request = self._service.users().messages().list(
                userId="me",
                q=query,
                maxResults=page_size,
                pageToken=page_token,
            )
            response = request.execute()

            for item in response.get("messages", []):
                raw_message = (
                    self._service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=item["id"],
                        format="metadata",
                        metadataHeaders=["Date", "From", "Subject"],
                    )
                    .execute()
                )
                messages.append(_to_summary(raw_message))
                if len(messages) >= max_results:
                    break

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return messages


def _to_summary(message: dict[str, Any]) -> EmailMessageSummary:
    headers = {
        header["name"].lower(): header.get("value", "")
        for header in message.get("payload", {}).get("headers", [])
    }
    return EmailMessageSummary(
        message_id=message["id"],
        received_at=_received_at(message, headers.get("date")),
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
    )


def _received_at(message: dict[str, Any], date_header: str | None) -> datetime:
    internal_date = message.get("internalDate")
    if internal_date is not None:
        return datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC)
    if date_header:
        parsed = parsedate_to_datetime(date_header)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise ValueError(f"Gmail message {message.get('id', '<unknown>')} has no received date")
