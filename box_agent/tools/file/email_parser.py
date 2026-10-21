"""Bounded, offline MIME decoding shared by email tool operations."""

from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.errors import HeaderParseError
from email.header import decode_header
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser

MAX_EMAIL_BYTES = 50 * 1024 * 1024
MAX_EMAIL_PARTS = 256
MAX_EMAIL_DEPTH = 30
MAX_EMAIL_ATTACHMENTS = 100


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.hidden = 0
        self.in_head = False
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "head":
            self.in_head = True
        elif tag == "body":
            # HTML permits an omitted </head> before the body starts.
            self.in_head = False
        if tag in {"script", "style"}:
            self.hidden += 1
        if not (self.hidden or self.in_head) and tag in {"br", "p", "div", "tr", "li", "hr"}:
            self.chunks.append("\n")
        if not (self.hidden or self.in_head) and tag == "a":
            self.links.append(next((value or "" for key, value in attrs if key == "href"), ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.in_head = False
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if not (self.hidden or self.in_head) and tag in {"p", "div", "tr", "li"}:
            self.chunks.append("\n")
        if not (self.hidden or self.in_head) and tag in {"td", "th"}:
            self.chunks.append("\t")
        if not (self.hidden or self.in_head) and tag == "a" and self.links:
            if href := self.links.pop():
                self.chunks.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if not (self.hidden or self.in_head):
            self.chunks.append(data)


def _decode(data: bytes, charset: str | None, warnings: list[str]) -> str:
    for encoding in dict.fromkeys([charset or "utf-8", "utf-8", "gb18030"]):
        try:
            decoded = data.decode(encoding)
            if charset and encoding != charset:
                warnings.append(f"Could not decode charset {charset[:80]}; used {encoding}.")
            return decoded
        except (LookupError, UnicodeError):
            continue
    warnings.append("Unrecognized text encoding; undecodable bytes replaced with U+FFFD.")
    return data.decode("utf-8", errors="replace")


def _header(value: str, warnings: list[str]) -> str:
    # raw_items() uses surrogateescape for unencoded non-ASCII header bytes.
    if any(0xDC80 <= ord(char) <= 0xDCFF for char in value):
        value = _decode(value.encode("utf-8", errors="surrogateescape"), None, warnings)
    try:
        chunks = decode_header(value)
    except HeaderParseError:
        warnings.append("Malformed encoded email header; original header retained.")
        return value
    return "".join(
        _decode(chunk, charset, warnings) if isinstance(chunk, bytes) else chunk
        for chunk, charset in chunks
    )


@dataclass
class EmailAttachment:
    filename: str
    content_type: str
    content_id: str
    data: bytes


@dataclass
class ParsedEmail:
    headers: dict[str, str]
    body: str
    attachments: list[EmailAttachment]
    warnings: list[str]


def parse_email(data: bytes) -> ParsedEmail:
    """Decode one EML without fetching links, executing HTML, or writing files."""
    if len(data) > MAX_EMAIL_BYTES:
        raise ValueError(f"Email exceeds the {MAX_EMAIL_BYTES}-byte limit.")
    part_count = 0

    def message_factory(*args, **kwargs):
        nonlocal part_count
        part_count += 1
        if part_count > MAX_EMAIL_PARTS:
            raise ValueError("Email MIME part count exceeds the safety limit.")
        return EmailMessage(*args, **kwargs)

    message = BytesParser(policy=policy.default.clone(message_factory=message_factory)).parsebytes(data)
    # Validate the complete tree, including attached messages and multipart
    # attachments, before body selection or attachment serialization.
    pending = [(message, 0)]
    while pending:
        part, depth = pending.pop()
        if depth > MAX_EMAIL_DEPTH:
            raise ValueError("Email MIME nesting exceeds the safety limit.")
        payload = part.get_payload()
        if isinstance(payload, list):
            pending.extend((child, depth + 1) for child in payload)
    warnings: list[str] = []
    headers: dict[str, str] = {}
    header_names = {"subject", "from", "to", "cc", "date", "message-id", "reply-to"}
    for key, value in message.raw_items():
        if key.lower() in header_names:
            decoded = _header(value, warnings)
            if len(decoded) > 2_000:
                warnings.append(f"Header {key} shortened to 2000 characters.")
            headers[key.lower()] = decoded[:2_000]
    if not headers:
        warnings.append("No common email headers found; the input may be malformed.")
    attachments: list[EmailAttachment] = []
    def visit(part: EmailMessage) -> tuple[str, str | None]:
        content_type = part.get_content_type()
        filename = part.get_filename()
        is_attachment = (
            filename is not None or part.get_content_disposition() == "attachment"
            or content_type == "message/rfc822"
        )
        if not is_attachment and part.is_multipart():
            bodies = [visit(child) for child in part.iter_parts()]
            if content_type == "multipart/alternative":
                for preferred in ("text/plain", "text/html"):
                    selected = next((item for item in bodies if item[0] and item[1] == preferred), None)
                    if selected is not None:
                        return selected
            readable = [(text, kind) for text, kind in bodies if text]
            kind = "text/plain" if any(kind == "text/plain" for _, kind in readable) else "text/html"
            return "\n\n".join(text for text, _ in readable), kind
        if not is_attachment and content_type in {"text/plain", "text/html"}:
            payload = part.get_payload(decode=True) or b""
            text = _decode(payload, part.get_content_charset(), warnings)
            if content_type == "text/html":
                parser = _HtmlText()
                parser.feed(text)
                parser.close()
                text = "".join(parser.chunks)
            warnings.extend(type(defect).__name__ for defect in part.defects)
            return text.strip(), content_type
        if len(attachments) >= MAX_EMAIL_ATTACHMENTS:
            raise ValueError("Email attachment count exceeds the safety limit.")
        if content_type == "message/rfc822" and isinstance(part.get_payload(), list):
            payload = b"\n".join(child.as_bytes() for child in part.get_payload())
        elif part.is_multipart():
            payload = part.as_bytes()
        else:
            payload = part.get_payload(decode=True) or b""
        warnings.extend(type(defect).__name__ for defect in part.defects)
        attachments.append(EmailAttachment(
            filename=_header(filename or ("attached-message.eml" if content_type == "message/rfc822"
                                          else f"attachment-{len(attachments) + 1}"), warnings)[:512],
            content_type=content_type[:128],
            content_id=_header(str(part.get("Content-ID", "")), warnings)[:512],
            data=payload,
        ))
        return "", None

    decoded_body, _ = visit(message)
    warnings.extend(type(defect).__name__ for defect in message.defects)
    if not decoded_body:
        warnings.append("No readable body found; encrypted content is not decrypted.")
    return ParsedEmail(headers, decoded_body, attachments, list(dict.fromkeys(warnings)))
