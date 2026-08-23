"""Decode useful text from raw MIME without provider-specific knowledge."""

from __future__ import annotations

import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import ClassVar


@dataclass(frozen=True)
class DecodedEmail:
    sender_header: str
    subject: str
    text: str


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS: ClassVar[frozenset[str]] = frozenset(
        {
            "br",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "p",
            "table",
            "td",
            "th",
            "tr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        self.fragments.append(data)

    def text(self) -> str:
        return "".join(self.fragments)


def decode_email(raw_mime: bytes) -> DecodedEmail:
    """Decode preferred plain text, falling back to HTML-only content."""
    message = BytesParser(policy=policy.default).parsebytes(raw_mime)
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        content = part.get_content()
        if not isinstance(content, str):
            continue
        if content_type == "text/plain":
            plain_parts.append(content)
        else:
            extractor = _HTMLTextExtractor()
            extractor.feed(content)
            html_parts.append(extractor.text())

    selected_parts = plain_parts if any(part.strip() for part in plain_parts) else html_parts
    return DecodedEmail(
        sender_header=str(message.get("From", "")),
        subject=str(message.get("Subject", "")),
        text=_normalize_text("\n".join(selected_parts)),
    )


def _normalize_text(text: str) -> str:
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        normalized = re.sub(r"[\t\v\f \u00a0\u202f]+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)
