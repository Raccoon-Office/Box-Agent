"""MIME and character-encoding regressions from real email shapes."""

import json
from email.message import EmailMessage

import pytest

from box_agent.tools.file import email_parser
from box_agent.tools.file.email_parser import parse_email


def test_raw_utf8_headers_are_decoded_and_transport_safe():
    parsed = parse_email('Subject: 中文主题\r\nFrom: 张三 <sender@example.invalid>\r\n\r\nbody'.encode())
    assert parsed.headers['subject'] == '中文主题'
    assert parsed.headers['from'].startswith('张三')
    assert json.dumps(parsed.headers, ensure_ascii=False).encode('utf-8')


@pytest.mark.parametrize('plain', ['', ' \t\n'])
def test_empty_plain_alternative_falls_back_to_nonempty_html(plain):
    message = EmailMessage()
    message.set_content(plain)
    message.add_alternative('<p>实际业务正文</p>', subtype='html')
    parsed = parse_email(message.as_bytes())
    assert parsed.body == '实际业务正文'
    assert not any('No readable body' in warning for warning in parsed.warnings)


def test_omitted_head_end_tag_does_not_hide_the_body():
    message = EmailMessage()
    message.set_content('<html><head><title>隐藏标题</title>'
                        '<style>隐藏样式</style><body><p>业务正文</p></body></html>', subtype='html')
    assert parse_email(message.as_bytes()).body == '业务正文'


def test_html_related_alternative_keeps_its_inline_attachment():
    message = EmailMessage()
    message.set_content('')
    message.add_alternative('<p>业务正文</p><img src="cid:logo">', subtype='html')
    message.get_payload()[1].add_related(b'image', maintype='image', subtype='png', cid='<logo>')
    parsed = parse_email(message.as_bytes())
    assert parsed.body == '业务正文'
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].data == b'image'


def test_attached_email_depth_is_checked_before_serialization(monkeypatch):
    monkeypatch.setattr(email_parser, 'MAX_EMAIL_DEPTH', 3)
    message = EmailMessage()
    message.set_content('inner')
    for _ in range(4):
        outer = EmailMessage()
        outer.set_content('outer')
        outer.add_attachment(message, filename='forwarded.eml')
        message = outer
    with pytest.raises(ValueError, match='nesting'):
        parse_email(message.as_bytes())


def test_multipart_attachment_children_count_toward_parse_limit(monkeypatch):
    attachment = EmailMessage()
    attachment.set_type('multipart/mixed')
    attachment['Content-Disposition'] = 'attachment; filename="bundle.mime"'
    for text in ('one', 'two', 'three'):
        child = EmailMessage()
        child.set_content(text)
        attachment.attach(child)
    monkeypatch.setattr(email_parser, 'MAX_EMAIL_PARTS', 3)
    with pytest.raises(ValueError, match='part count'):
        parse_email(attachment.as_bytes())
