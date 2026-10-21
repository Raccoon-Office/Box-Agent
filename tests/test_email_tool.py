"""Offline email decoding and permission-preserving attachment extraction."""

import json
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.tools.base import ToolResult
from box_agent.tools.file import ReadTool
from box_agent.tools.file import email_parser
from box_agent.tools.file.email_parser import parse_email
from box_agent.tools.file.email_tool import ExtractEmailAttachmentTool, ReadEmailTool
from box_agent.tools.file_change_receipts import FileWriteInvocationContext
from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine


@pytest.fixture
def mail(tmp_path):
    message = EmailMessage()
    message['Subject'] = '十月销售报告'
    message['From'] = '张三 <sender@example.invalid>'
    message['To'] = 'recipient@example.invalid'
    message.set_content('请分析附件销售额。', cte='base64')
    message.add_alternative('<html><body>重复的 HTML 正文</body></html>', subtype='html')
    message.add_attachment('项目,金额\n甲,100\n乙,200\n'.encode(),
                           maintype='text', subtype='csv', filename='销售.csv')
    path = tmp_path / 'report.EML'
    path.write_bytes(message.as_bytes())
    return path


async def test_decodes_chinese_headers_and_body_and_lists_attachments_without_writing(mail):
    before = set(mail.parent.iterdir())
    result = await ReadEmailTool(workspace_dir=str(mail.parent)).invoke({'path': mail.name})
    assert result.success, result.error
    decoded = json.loads(result.content)
    assert decoded['headers']['subject'] == '十月销售报告'
    assert '张三' in decoded['headers']['from']
    assert decoded['body'] == '请分析附件销售额。'
    assert decoded['attachments'] == [{
        'index': 1, 'filename': '销售.csv', 'content_type': 'text/csv',
        'content_id': '', 'size_bytes': len('项目,金额\n甲,100\n乙,200\n'.encode()),
    }]
    assert set(mail.parent.iterdir()) == before


@pytest.mark.parametrize('charset', ['gb18030', 'unknown-mail-charset'])
def test_decodes_legacy_and_unknown_charset_bodies(charset):
    data = (f'Subject: =?utf-8?b?5rWL6K+V?=\r\n'
            f'Content-Type: text/plain; charset={charset}\r\n\r\n').encode()
    parsed = parse_email(data + '中文正文'.encode('gb18030'))
    assert parsed.headers['subject'] == '测试'
    assert parsed.body == '中文正文'
    if charset.startswith('unknown'):
        assert any('charset' in warning for warning in parsed.warnings)


def test_html_only_mail_is_readable_without_scripts_styles_or_remote_content():
    message = EmailMessage()
    message['Subject'] = 'HTML mail'
    message.set_content('<html><head><style>hidden style</style></head><body>'
                        '<p>正文 &amp; 明细</p><a href="https://example.invalid/detail">详情</a>'
                        '<script>alert("hidden script")</script>'
                        '<img src="https://example.invalid/tracker">结束</body></html>', subtype='html',
                        cte='quoted-printable')
    parsed = parse_email(message.as_bytes())
    assert '正文 & 明细' in parsed.body
    assert '结束' in parsed.body
    assert '详情 (https://example.invalid/detail)' in parsed.body
    assert 'hidden' not in parsed.body
    assert 'tracker' not in parsed.body


async def test_extracts_decoded_csv_for_followup_analysis_and_preserves_original(mail):
    original = mail.read_bytes()
    result = await ExtractEmailAttachmentTool(workspace_dir=str(mail.parent)).invoke({
        'path': str(mail), 'attachment_index': 1, 'output_path': 'attachments/sales.csv',
    })
    assert result.success, result.error
    target = Path(result.raw_output['extracted_path'])
    assert target.read_text(encoding='utf-8') == '项目,金额\n甲,100\n乙,200\n'
    read = await ReadTool(workspace_dir=str(mail.parent)).execute(str(target))
    assert read.success and '甲,100' in read.content
    assert mail.read_bytes() == original
    assert result.raw_output['file_changes'][0]['path'] == 'attachments/sales.csv'


async def test_attached_email_is_listed_separately_and_can_be_read_after_extraction(tmp_path):
    inner = EmailMessage()
    inner['Subject'] = '转发内容'
    inner.set_content('内层正文')
    outer = EmailMessage()
    outer['Subject'] = '外层主题'
    outer.set_content('外层正文')
    outer.add_attachment(inner, filename='forwarded.eml')
    mail = tmp_path / 'outer.eml'
    mail.write_bytes(outer.as_bytes())
    tool = ReadEmailTool(workspace_dir=str(tmp_path))
    result = await ExtractEmailAttachmentTool(workspace_dir=str(tmp_path)).execute(
        str(mail), attachment_index=1, output_path='forwarded.eml',
    )
    assert result.success, result.error
    assert 'body' not in result.raw_output
    forwarded = await tool.execute('forwarded.eml')
    assert forwarded.success, forwarded.error
    assert forwarded.raw_output['headers']['subject'] == '转发内容'
    assert forwarded.raw_output['body'] == '内层正文'


def test_lists_inline_image_with_its_content_id():
    message = EmailMessage()
    message['Subject'] = 'Inline'
    message.set_content('<p>图片</p><img src="cid:logo">', subtype='html')
    message.add_related(b'\x89PNG\r\n', maintype='image', subtype='png', cid='<logo>')
    parsed = parse_email(message.as_bytes())
    assert parsed.body == '图片'
    assert parsed.attachments[0].content_id == '<logo>'
    assert parsed.attachments[0].data == b'\x89PNG\r\n'


async def test_body_pagination_does_not_drop_content(tmp_path):
    message = EmailMessage()
    message['Subject'] = 'Long mail'
    message.set_content('0123456789' * 5)
    mail = tmp_path / 'long.eml'
    mail.write_bytes(message.as_bytes())
    tool = ReadEmailTool(workspace_dir=str(tmp_path))
    offset, chunks = 1, []
    while offset is not None:
        result = await tool.invoke({'path': str(mail), 'offset': offset, 'limit': 7})
        assert result.success, result.error
        chunks.append(result.raw_output['body'])
        offset = result.raw_output['next_offset']
    assert ''.join(chunks) == '0123456789' * 5


async def test_read_file_preserves_email_source_and_guides_analysis_to_decoder(mail):
    result = await ReadTool(workspace_dir=str(mail.parent)).execute(str(mail))
    assert result.success
    assert 'read_email' in result.content
    assert 'Content-Transfer-Encoding: base64' in result.content


async def test_denies_outside_read_before_parsing(mail, monkeypatch):
    from box_agent.tools.file import email_tool
    def unexpected_parse(_):
        pytest.fail('Denied email must not be parsed')
    monkeypatch.setattr(email_tool, 'parse_email', unexpected_parse)
    workspace = mail.parent / 'workspace'
    workspace.mkdir()
    tool = ReadEmailTool(workspace_dir=str(workspace),
                         permission_engine=PermissionEngine(CapabilityPolicy(), workspace))
    result = await tool.execute(str(mail))
    assert not result.success
    assert 'Access denied' in result.error


@pytest.mark.parametrize('destination', ['../outside.csv', 'existing.csv', 'link/escaped.csv'])
async def test_extraction_cannot_escape_workspace_or_overwrite_files(mail, destination):
    existing = mail.parent / 'existing.csv'
    existing.write_text('preserve')
    outside = mail.parent.parent / f'{mail.parent.name}-outside'
    outside.mkdir()
    if destination.startswith('link/'):
        try:
            (mail.parent / 'link').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip('Directory symlinks are unavailable on this host')
    tool = ExtractEmailAttachmentTool(workspace_dir=str(mail.parent))
    result = await tool.execute(str(mail), attachment_index=1, output_path=destination)
    assert not result.success
    assert existing.read_text() == 'preserve'
    assert list(outside.iterdir()) == []
    assert not (mail.parent.parent / 'outside.csv').exists()


async def test_extraction_checks_write_permission_and_delegated_write_scope(mail):
    engine = SimpleNamespace(check=lambda **kw: SimpleNamespace(
        allowed=kw['capability'] == 'filesystem.read', reason='write denied',
        permission_request={'path': kw['resource']['path'], 'scope': 'filesystem'},
    ))
    tool = ExtractEmailAttachmentTool(workspace_dir=str(mail.parent), permission_engine=engine)
    result = await tool.invoke({'path': str(mail), 'attachment_index': 1, 'output_path': 'denied.csv'})
    assert not result.success and result.permission_request is not None
    assert not (mail.parent / 'denied.csv').exists()
    tool = ExtractEmailAttachmentTool(workspace_dir=str(mail.parent))
    result = await tool.invoke(
        {'path': str(mail), 'attachment_index': 1, 'output_path': 'denied.csv'},
        context=FileWriteInvocationContext(
            validate_write_target=lambda _: ToolResult(success=False, error='outside child write scope'),
        ),
    )
    assert not result.success and 'child write scope' in result.error
    assert not (mail.parent / 'denied.csv').exists()


@pytest.mark.parametrize('kwargs', [
    {'attachment_index': 1}, {'output_path': 'x.csv'},
    {'attachment_index': 2, 'output_path': 'x.csv'},
])
async def test_invalid_extraction_requests_create_no_files(mail, kwargs):
    result = await ExtractEmailAttachmentTool(workspace_dir=str(mail.parent)).invoke({'path': str(mail), **kwargs})
    assert not result.success
    assert not (mail.parent / 'x.csv').exists()


def test_rejects_excessive_mime_structure(monkeypatch):
    monkeypatch.setattr(email_parser, 'MAX_EMAIL_PARTS', 2)
    message = EmailMessage()
    message.set_content('body')
    message.add_attachment(b'one', maintype='text', subtype='plain')
    with pytest.raises(ValueError, match='part count'):
        parse_email(message.as_bytes())


async def test_rejects_oversized_email_before_loading_payload(mail, monkeypatch):
    from box_agent.tools.file import email_tool
    monkeypatch.setattr(email_tool, 'MAX_EMAIL_BYTES', 10)
    result = await ReadEmailTool(workspace_dir=str(mail.parent)).execute(str(mail))
    assert not result.success and 'byte limit' in result.error


def test_malformed_mail_and_undecodable_bytes_are_reported():
    parsed = parse_email(b'Content-Type: text/plain; charset=utf-8\r\n\r\n\xff')
    assert parsed.warnings
    assert '\ufffd' in parsed.body


def test_malformed_encoded_header_does_not_hide_the_body():
    parsed = parse_email(b'Subject: =?utf-8?b?A?=\r\n\r\nreadable body')
    assert parsed.body == 'readable body'
    assert parsed.headers['subject'] == '=?utf-8?b?A?='
    assert any('Malformed' in warning for warning in parsed.warnings)


async def test_read_tool_rejects_extraction_arguments_and_writes_nothing(mail):
    result = await ReadEmailTool(workspace_dir=str(mail.parent)).invoke({
        'path': str(mail), 'attachment_index': 1, 'output_path': 'unexpected.csv',
    })
    assert not result.success
    assert not (mail.parent / 'unexpected.csv').exists()


async def test_binary_transfer_attachment_can_be_read_and_extracted(tmp_path):
    path = tmp_path / 'binary.eml'
    data = b'binary\x00payload\xff'
    path.write_bytes(
        b'Subject: Binary mail\r\nContent-Type: multipart/mixed; boundary=BOUNDARY\r\n\r\n'
        b'--BOUNDARY\r\nContent-Type: text/plain\r\n\r\nbody\r\n'
        b'--BOUNDARY\r\nContent-Type: application/octet-stream\r\n'
        b'Content-Disposition: attachment; filename="blob.bin"\r\n'
        b'Content-Transfer-Encoding: binary\r\n\r\n' + data + b'\r\n--BOUNDARY--\r\n'
    )
    read = await ReadEmailTool(workspace_dir=str(tmp_path)).invoke({'path': str(path)})
    assert read.success, read.error
    assert read.raw_output['body'] == 'body'
    extracted = await ExtractEmailAttachmentTool(workspace_dir=str(tmp_path)).invoke({
        'path': str(path), 'attachment_index': 1, 'output_path': 'blob.bin',
        'expected_sha256': read.raw_output['source_sha256'],
    })
    assert extracted.success, extracted.error
    assert (tmp_path / 'blob.bin').read_bytes() == data


async def test_changed_source_is_rejected_before_continuation_or_extraction(mail):
    reader = ReadEmailTool(workspace_dir=str(mail.parent))
    first = await reader.execute(str(mail), limit=2)
    mail.write_bytes(mail.read_bytes().replace(b'Subject:', b'X-Subject:', 1))
    digest = first.raw_output['source_sha256']
    continued = await reader.invoke({'path': str(mail), 'offset': 3, 'expected_sha256': digest})
    extracted = await ExtractEmailAttachmentTool(workspace_dir=str(mail.parent)).invoke({
        'path': str(mail), 'attachment_index': 1, 'output_path': 'changed.csv', 'expected_sha256': digest,
    })
    assert not continued.success and 'source changed' in continued.error
    assert not extracted.success and 'source changed' in extracted.error
    assert not (mail.parent / 'changed.csv').exists()


@pytest.mark.parametrize('limit', [20_000, 50_000])
async def test_email_page_survives_shared_per_result_storage(tmp_path, limit):
    from box_agent.schema import Message
    from box_agent.tool_result_storage import ToolResultStorage

    message = EmailMessage()
    message.set_content('a' * 9_000 + 'MIDDLE_EVIDENCE' + 'b' * 42_000)
    path = tmp_path / 'long.eml'
    path.write_bytes(message.as_bytes())
    reader = ReadEmailTool(workspace_dir=str(tmp_path))
    page = await reader.invoke({'path': str(path), 'limit': limit})
    storage = ToolResultStorage(tmp_path / 'results')
    storage.set_context_token_limit(50_000)
    original = Message(role='tool', name='read_email', tool_call_id='email-page', content=page.content)
    projected = storage.process_message(original, tool=reader)
    assert json.loads(projected.content)['body'] == page.raw_output['body']
    assert 'MIDDLE_EVIDENCE' in projected.content
    assert json.loads(projected.content)['next_offset'] == limit + 1


async def test_aggregate_budget_preview_requires_recovering_page_before_advancing(mail):
    from box_agent.schema import Message
    from box_agent.tool_result_storage import ToolResultStorage

    message = EmailMessage()
    message.set_content('a' * 20_000)
    mail.write_bytes(message.as_bytes())
    reader = ReadEmailTool(workspace_dir=str(mail.parent))
    page = await reader.invoke({'path': str(mail)})
    storage = ToolResultStorage(mail.parent / 'results', aggregate_budget=5_000)
    original = Message(role='tool', name='read_email', tool_call_id='email-page', content=page.content)
    projected = storage.process_message(original, tool=reader)
    messages = [projected]
    storage.enforce_fresh_budget(messages, tools={'read_email': reader}, session_id='email')
    assert 'Do not advance next_offset' in messages[0].content
    persisted = mail.parent / 'results' / 'email' / 'tool-results' / 'email-page.txt'
    assert persisted.read_text() == page.content
