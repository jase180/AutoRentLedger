from datetime import UTC, datetime

import pytest

from autorentledger.email.gmail import GMAIL_SCOPES, GmailSource


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeMessages:
    def __init__(self):
        self.list_calls = []
        self.get_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if kwargs["pageToken"] is None:
            return FakeRequest({"messages": [{"id": "gmail-1"}], "nextPageToken": "page-2"})
        return FakeRequest({"messages": [{"id": "gmail-2"}]})

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        message_id = kwargs["id"]
        return FakeRequest(
            {
                "id": message_id,
                "internalDate": "1724335200000",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Zelle <notify@example.com>"},
                        {"name": "Subject", "value": f"Payment received: {message_id}"},
                        {"name": "Date", "value": "Thu, 22 Aug 2024 09:00:00 -0500"},
                    ]
                },
            }
        )


class FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self):
        self.messages_api = FakeMessages()

    def users(self):
        return FakeUsers(self.messages_api)


def test_scope_is_read_only():
    assert GMAIL_SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)


def test_search_paginates_and_maps_metadata():
    service = FakeService()

    messages = GmailSource(service).search("zelle", max_results=2)

    assert [message.message_id for message in messages] == ["gmail-1", "gmail-2"]
    assert messages[0].received_at == datetime(2024, 8, 22, 14, 0, tzinfo=UTC)
    assert messages[0].sender == "Zelle <notify@example.com>"
    assert messages[0].subject == "Payment received: gmail-1"
    assert service.messages_api.list_calls == [
        {"userId": "me", "q": "zelle", "maxResults": 2, "pageToken": None},
        {"userId": "me", "q": "zelle", "maxResults": 1, "pageToken": "page-2"},
    ]
    assert all(call["format"] == "metadata" for call in service.messages_api.get_calls)
    assert all(call["metadataHeaders"] == ["Date", "From", "Subject"] for call in service.messages_api.get_calls)


def test_search_rejects_non_positive_limit():
    with pytest.raises(ValueError, match="at least 1"):
        GmailSource(FakeService()).search("zelle", max_results=0)
