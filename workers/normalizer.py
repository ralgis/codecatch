"""Parse RFC822 messages into a clean dataclass shape extractors can consume."""
from __future__ import annotations

import email
import email.policy
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True)
class NormalizedMessage:
    sender: str
    recipient: str           # primary To: address (first one)
    subject: str
    body_text: str           # plain text part (or fallback from html)
    message_id: str | None
    raw_to_header: str       # original To: header (for routing in groups)


def parse_rfc822(raw_bytes: bytes) -> NormalizedMessage:
    msg: EmailMessage = email.message_from_bytes(raw_bytes, policy=email.policy.default)  # type: ignore[assignment]

    sender = (msg.get("From") or "").strip()
    raw_to = (msg.get("To") or "").strip()
    subject = (msg.get("Subject") or "").strip()
    message_id = msg.get("Message-ID")
    if message_id:
        message_id = message_id.strip().strip("<>")

    recipient = _extract_address(raw_to)

    body_text = _extract_body(msg)

    return NormalizedMessage(
        sender=sender,
        recipient=recipient,
        subject=subject,
        body_text=body_text,
        message_id=message_id,
        raw_to_header=raw_to,
    )


def _extract_address(header_value: str) -> str:
    """Pick the first email address from a To/From/Delivered-To header."""
    if not header_value:
        return ""
    addrs = email.utils.getaddresses([header_value])
    if not addrs:
        return ""
    return addrs[0][1].strip().lower()


def _extract_body(msg: EmailMessage) -> str:
    """Get text content, falling back from text/plain to stripped text/html."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    return part.get_content()
                except (LookupError, ValueError):
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(errors="replace")
        # fallback to HTML, stripped
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_content()
                except (LookupError, ValueError):
                    payload = part.get_payload(decode=True) or b""
                    html = payload.decode(errors="replace")
                return _strip_html(html)
        return ""
    try:
        content = msg.get_content()
    except (LookupError, ValueError):
        payload = msg.get_payload(decode=True) or b""
        content = payload.decode(errors="replace")
    if msg.get_content_type() == "text/html":
        return _strip_html(content)
    return content


def _strip_html(s: str) -> str:
    """Cheap tag-stripping — good enough for code extraction."""
    import re
    return re.sub(r"<[^>]+>", " ", s)
