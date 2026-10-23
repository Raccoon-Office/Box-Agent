"""Offline PST access, keeping archive traversal separate from MIME decoding."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Iterator

import pypff

from ..base import check_tool_cancellation
from .email_parser import MAX_EMAIL_BYTES, EmailAttachment, ParsedEmail, _HtmlText, _decode


@contextmanager
def open_pst(source: Path, expected_sha256: str | None) -> Iterator[tuple[Any, str]]:
    # 分块计算身份，不将整个邮箱读入内存；分页和附件提取仍绑定同一份源文件。
    with source.open('rb') as stream:
        initial = os.fstat(stream.fileno())
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            check_tool_cancellation()
            digest.update(chunk)
        identity = digest.hexdigest()
        if expected_sha256 is not None and expected_sha256 != identity:
            raise ValueError('Email source changed; read it again before continuing or extracting attachments.')
        stream.seek(0)
        archive = pypff.file()
        try:
            archive.open_file_object(stream)
            yield archive, identity
            current = source.stat()
            if (initial.st_size, initial.st_mtime_ns, initial.st_ino) != (
                current.st_size, current.st_mtime_ns, current.st_ino
            ):
                raise ValueError('PST source changed while reading; retry from the first page.')
        finally:
            archive.close()


def _folders(archive):
    stack = [(archive.get_root_folder(), (), '')]
    seen = set()
    while stack:
        check_tool_cancellation()
        folder, route, parent = stack.pop()
        if folder is None:
            continue
        if len(route) > 64 or folder.identifier in seen:
            raise ValueError('PST folder tree is too deep or contains a cycle.')
        seen.add(folder.identifier)
        display = '/'.join(filter(None, (parent, folder.name)))
        yield folder, route, display
        for index in reversed(range(folder.number_of_sub_folders)):
            stack.append((folder.get_sub_folder(index), (*route, index), display))


def list_pst_messages(
    source: Path, offset: int, limit: int, expected_sha256: str | None,
) -> dict[str, Any]:
    offset, limit = max(1, offset), max(1, min(limit, 100))
    with open_pst(source, expected_sha256) as (archive, digest):
        rows, skipped, more = [], 0, False
        for folder, route, display in _folders(archive):
            if skipped + folder.number_of_sub_messages < offset:
                skipped += folder.number_of_sub_messages
                continue
            for index in range(max(0, offset - 1 - skipped), folder.number_of_sub_messages):
                check_tool_cancellation()
                if len(rows) == limit:
                    more = True
                    break
                message = folder.get_sub_message(index)
                rows.append({
                    'message_id': '/'.join(map(str, route)) + f':{index}',
                    'folder': display, 'subject': message.subject or '',
                    'sender': message.sender_name or '',
                    'date': str(message.delivery_time or message.client_submit_time or ''),
                    'attachment_count': message.number_of_attachments,
                })
            if more:
                break
            skipped += folder.number_of_sub_messages
        return {'source_sha256': digest, 'messages': rows, 'offset': offset,
                'has_more': more, 'next_offset': offset + len(rows) if more else None}


def _message(archive, message_id: str):
    if not re.fullmatch(r'(?:\d+(?:/\d+)*)?:\d+', message_id) or len(message_id) > 1024:
        raise ValueError('Invalid message_id; use an ID returned by read_email for this PST.')
    route, index = message_id.split(':')
    folder = archive.get_root_folder()
    indices = route.split('/') if route else []
    if len(indices) > 64:
        raise ValueError('PST folder tree is too deep.')
    for component in indices:
        folder = folder.get_sub_folder(int(component))
    return folder.get_sub_message(int(index))


def _property(item, tag, kind="string"):
    for record in item.record_sets:
        for entry in record.entries:
            if entry.entry_type == tag:
                return entry.data_as_integer if kind == "integer" else entry.data_as_string
    return None


def read_pst_message(
    source: Path, message_id: str, expected_sha256: str | None,
    attachment_index: int | None = None,
) -> tuple[ParsedEmail, str]:
    with open_pst(source, expected_sha256) as (archive, digest):
        message = _message(archive, message_id)
        warnings = []
        raw_headers = message.transport_headers or b''
        if isinstance(raw_headers, str):
            raw_headers = raw_headers.encode('utf-8')
        headers = BytesParser(policy=policy.default).parsebytes(raw_headers, headersonly=True)
        values = {name: str(headers.get(name, '')) for name in
                  ('subject', 'from', 'to', 'cc', 'bcc', 'date', 'message-id')}
        values['subject'] = values['subject'] or message.subject or ''
        values['from'] = values['from'] or message.sender_name or ''
        values['date'] = values['date'] or str(message.delivery_time or message.client_submit_time or '')
        codepage = _property(message, 0x3fde, 'integer')
        charset = f'cp{codepage}' if codepage else None
        body = message.plain_text_body or b''
        if not body.strip():
            html = message.html_body or b''
            if html:
                parser = _HtmlText()
                parser.feed(_decode(html, charset, warnings) if isinstance(html, bytes) else html)
                body = ''.join(parser.chunks)
            elif message.rtf_body:
                warnings.append('This message only has an RTF body; text decoding is unavailable.')
        if isinstance(body, bytes):
            body = _decode(body, charset, warnings)
        attachments = []
        for index in range(message.number_of_attachments):
            check_tool_cancellation()
            item = message.get_attachment(index)
            filename = item.long_filename or _property(item, 0x3704) or f'attachment-{index + 1}'
            size = item.size
            embedded = _property(item, 0x3705, 'integer') == 5
            if embedded:
                warnings.append(f'Attachment {index + 1} is an embedded Outlook item; extraction is unavailable.')
            data = b''
            # 浏览正文只返回附件元数据；只有明确提取某个附件时才读取它的字节。
            if attachment_index == index + 1:
                if embedded:
                    raise ValueError("Embedded Outlook item extraction is not supported by this PST reader.")
                if size > MAX_EMAIL_BYTES:
                    raise ValueError(f'Attachment exceeds the {MAX_EMAIL_BYTES}-byte extraction limit.')
                data = item.read_buffer(size)
                if len(data) != size:
                    raise ValueError('PST attachment is incomplete.')
            attachments.append(EmailAttachment(
                filename, _property(item, 0x370e) or mimetypes.guess_type(filename)[0]
                or 'application/octet-stream', _property(item, 0x3712) or '', data, size,
            ))
        return ParsedEmail(values, body.strip(), attachments, warnings), digest
