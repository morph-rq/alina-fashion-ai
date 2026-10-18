"""Turn an RFC 822 message into stored evidence.

The original bytes are stored unchanged (``message/rfc822``). Headers, a plain
text body and attachments are extracted locally with the standard ``email``
package, which handles MIME encodings, multipart structure and inline images.
HTML is reduced to text: no link is followed and no remote image or tracking
pixel is loaded. Unsupported attachments are recorded as ignored with a reason;
undecodable ones as failed, so nothing silently disappears.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from app.google_api import SUPPORTED_DOWNLOADS

ATTACHMENT_TYPES = SUPPORTED_DOWNLOADS | {"image/gif", "image/heic", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
MAX_BODY_CHARS = 50000


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML still yields what was read
        pass
    lines = [" ".join(line.split()) for line in "".join(extractor.parts).splitlines()]
    return "\n".join(line for line in lines if line)


@dataclass
class Attachment:
    filename: str
    media_type: str
    content: bytes | None
    inline: bool
    content_id: str | None
    problem: str | None = None


@dataclass
class ParsedMessage:
    subject: str
    sender: str
    recipients: list[str]
    date: str | None
    message_id_header: str | None
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)


def parse_message(raw: bytes) -> ParsedMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    def header(name):
        try:
            return str(message.get(name, "") or "")
        except Exception:  # noqa: BLE001 - malformed header
            return ""
    recipients = [address for _name, address in getaddresses([header("To"), header("Cc")]) if address]
    try:
        date = parsedate_to_datetime(header("Date")).isoformat() if header("Date") else None
    except (TypeError, ValueError):
        date = None
    plain, html = [], []
    attachments: list[Attachment] = []
    for index, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        media_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition is None and not filename and media_type in {"text/plain", "text/html"}:
            try:
                text = part.get_content()
            except (LookupError, UnicodeDecodeError):
                text = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
            (plain if media_type == "text/plain" else html).append(text)
            continue
        name = filename or f"part-{index}{'.' + media_type.split('/')[-1] if '/' in media_type else ''}"
        try:
            content = part.get_payload(decode=True)
            problem = None if content else "Empty or undecodable attachment"
        except Exception:  # noqa: BLE001 - corrupt transfer encoding
            content, problem = None, "Attachment could not be decoded"
        attachments.append(Attachment(filename=name[:300], media_type=media_type, content=content or None,
                                      inline=disposition == "inline" or bool(part.get("Content-ID")),
                                      content_id=(part.get("Content-ID") or "").strip("<>") or None, problem=problem))
    body = "\n\n".join(plain) if plain else "\n\n".join(html_to_text(item) for item in html)
    return ParsedMessage(subject=header("Subject")[:500], sender=header("From")[:500], recipients=recipients[:50], date=date,
                         message_id_header=header("Message-ID")[:300] or None, body_text=body[:MAX_BODY_CHARS], attachments=attachments)
