"""Exercise public PST bytes and the same read/write boundaries as EML."""

import gzip
import hashlib
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.tools.file import ReadTool
from box_agent.tools.file import pst_parser
from box_agent.tools.file.email_tool import ExtractEmailAttachmentTool, ReadEmailTool
from box_agent.tools.file_change_receipts import FileWriteInvocationContext
from box_agent.tools.base import ToolResult
from scripts import build_runtime


@pytest.fixture
def pst(tmp_path):
    data = gzip.decompress((Path(__file__).parent / 'fixtures/email/testPST.pst.gz').read_bytes())
    assert hashlib.sha256(data).hexdigest() == 'f2a6b1d2cad00f574e3d1c1211c4b1c854d6526caea77213adc3da92b7813ae3'
    source = tmp_path / 'archive.PST'
    source.write_bytes(data)
    return source


async def test_lists_every_message_once_and_reads_decoded_body_without_writing(pst):
    reader = ReadEmailTool(workspace_dir=str(pst.parent))
    before = set(pst.parent.iterdir())
    offset, rows, digest = 1, [], None
    while offset is not None:
        page = await reader.invoke({'path': pst.name, 'offset': offset, 'limit': 2,
                                    **({'expected_sha256': digest} if digest else {})})
        assert page.success, page.error
        rows.extend(page.raw_output['messages'])
        digest = page.raw_output['source_sha256']
        offset = page.raw_output['next_offset']
    assert len(rows) == len({row['message_id'] for row in rows}) == 7
    message = await reader.invoke({'path': pst.name, 'message_id': rows[0]['message_id'],
                                   'expected_sha256': digest})
    assert message.success, message.error
    assert message.raw_output['headers']['subject'] == 'Re: Feature Generators'
    assert 'PreviousMapFeatureGenerator' in message.raw_output['body']
    assert set(pst.parent.iterdir()) == before
    assert hashlib.sha256(pst.read_bytes()).hexdigest() == digest


async def test_pages_a_selected_message_and_can_compare_it_with_an_eml(pst):
    reader = ReadEmailTool(workspace_dir=str(pst.parent))
    whole = await reader.execute(pst.name, message_id='1:0')
    pieces, offset = [], 1
    while offset is not None:
        page = await reader.execute(pst.name, message_id='1:0', offset=offset, limit=97,
                                    expected_sha256=whole.raw_output['source_sha256'])
        assert page.success, page.error
        pieces.append(page.raw_output['body'])
        offset = page.raw_output['next_offset']
    assert ''.join(pieces) == whole.raw_output['body']
    eml = EmailMessage()
    eml['Subject'] = '对比材料'
    eml.set_content('PreviousMapFeatureGenerator: 补充说明')
    (pst.parent / 'reference.eml').write_bytes(eml.as_bytes())
    other = await reader.execute('reference.eml')
    assert other.success and 'PreviousMapFeatureGenerator' in other.raw_output['body']
    assert other.raw_output['source_sha256'] != whole.raw_output['source_sha256']


@pytest.mark.parametrize('message_id', ['../x:0', ':999999', '99:0'])
async def test_rejects_invalid_message_ids_without_writing(pst, message_id):
    result = await ReadEmailTool(workspace_dir=str(pst.parent)).execute(pst.name, message_id=message_id)
    assert not result.success
    assert result.error.startswith('EMAIL_READ_FAILED:')
    assert list(pst.parent.iterdir()) == [pst]


async def test_rejects_changed_and_corrupt_archives_and_redirects_text_reads(pst):
    reader = ReadEmailTool(workspace_dir=str(pst.parent))
    changed = await reader.execute(pst.name, expected_sha256='0' * 64)
    assert not changed.success and 'changed' in changed.error
    raw = await ReadTool(workspace_dir=str(pst.parent)).execute(pst.name)
    assert not raw.success and 'read_email' in raw.error
    pst.write_bytes(b'not a PST')
    corrupt = await reader.execute(pst.name)
    assert not corrupt.success and 'EMAIL_READ_FAILED' in corrupt.error


async def test_denies_pst_reads_before_opening_the_archive(pst, monkeypatch):
    workspace = pst.parent / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(pst_parser.pypff, 'file', lambda: pytest.fail('Must check permission first'))
    result = await ReadEmailTool(workspace_dir=str(workspace), allow_full_access=False).execute(str(pst))
    assert not result.success


async def test_lists_embedded_outlook_items_without_exporting_unreadable_bytes(pst):
    reader = ReadEmailTool(workspace_dir=str(pst.parent))
    result = await reader.execute(pst.name, message_id='1:6')
    assert result.success
    assert result.raw_output['attachments'][0]['size_bytes'] > 0
    assert any('embedded Outlook' in warning for warning in result.raw_output['warnings'])
    extraction = await ExtractEmailAttachmentTool(workspace_dir=str(pst.parent)).execute(
        pst.name, message_id='1:6', attachment_index=1, output_path='embedded.eml',
    )
    assert not extraction.success and 'Embedded Outlook' in extraction.error
    assert not (pst.parent / 'embedded.eml').exists()


async def test_reads_pst_attachment_bytes_only_for_explicit_permissioned_extraction(pst, monkeypatch):
    data = b'project,amount\nA,100\n'
    calls = []
    def read_buffer(size):
        calls.append(size)
        return data
    attachment = SimpleNamespace(long_filename='sales.csv', size=len(data), record_sets=[], read_buffer=read_buffer)
    message = SimpleNamespace(transport_headers=b'Subject: Sales\r\n', subject='Sales',
                              sender_name='Sender', delivery_time=None, client_submit_time=None,
                              record_sets=[], plain_text_body=b'Compare sales', number_of_attachments=1,
                              get_attachment=lambda index: attachment)
    monkeypatch.setattr(pst_parser, '_message', lambda archive, message_id: message)
    reader = ReadEmailTool(workspace_dir=str(pst.parent))
    read = await reader.execute(pst.name, message_id='1:0')
    assert read.success and calls == []
    assert read.raw_output['attachments'][0]['size_bytes'] == len(data)
    extractor = ExtractEmailAttachmentTool(workspace_dir=str(pst.parent))
    args = {'path': pst.name, 'message_id': '1:0', 'attachment_index': 1,
            'output_path': 'attachments/sales.csv', 'expected_sha256': read.raw_output['source_sha256']}
    denied = await extractor.invoke(args, context=FileWriteInvocationContext(
        validate_write_target=lambda path: ToolResult(success=False, error='WRITE_SCOPE_VIOLATION'),
    ))
    assert not denied.success and not (pst.parent / args['output_path']).exists()
    result = await extractor.invoke(args)
    assert result.success, result.error
    assert (pst.parent / args['output_path']).read_bytes() == data
    assert result.raw_output['file_changes'][0]['path'] == args['output_path']
    assert not (await extractor.invoke(args)).success  # never overwrite
    assert not (await extractor.invoke({**args, 'output_path': '../outside.csv'})).success


@pytest.mark.parametrize('external', [False, True])
def test_runtime_recipe_keeps_pst_reader_in_agent_process(external):
    assert 'pypff' in build_runtime.pyinstaller_hidden_imports(external_python_sandbox=external)
    assert 'pypff' not in build_runtime.EXTERNAL_PYTHON_SANDBOX_EXCLUDED_MODULES
