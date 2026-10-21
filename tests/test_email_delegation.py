"""Exercise email capabilities through actual child resolution and execution."""

import json
from email.message import EmailMessage
from unittest.mock import AsyncMock

import pytest

from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from box_agent.tools.file.email_tool import ExtractEmailAttachmentTool, ReadEmailTool
from box_agent.tools.sub_agent_capabilities import (
    CapabilityFailure, CapabilityResolver, DelegationSpec, parse_delegation_spec,
)
from box_agent.tools.sub_agent_tool import SubAgentTool


@pytest.fixture
def email_tools(tmp_path):
    message = EmailMessage()
    message['Subject'] = '委派邮件'
    message.set_content('需要分析的正文')
    message.add_attachment(b'amount\n100\n200\n', maintype='text', subtype='csv', filename='sales.csv')
    (tmp_path / 'source.eml').write_bytes(message.as_bytes())
    return {
        tool.name: tool for tool in (
            ReadEmailTool(workspace_dir=str(tmp_path)),
            ExtractEmailAttachmentTool(workspace_dir=str(tmp_path)),
        )
    }


def test_read_only_child_gets_email_reader_but_no_extractor(email_tools):
    spec = parse_delegation_spec(task='Read the email', default_required_tools=tuple(email_tools))
    assert isinstance(spec, DelegationSpec)
    bundle = CapabilityResolver().resolve(spec, parent_tools=email_tools)
    assert not isinstance(bundle, CapabilityFailure)
    assert 'read_email' in bundle.tools
    assert 'extract_email_attachment' not in bundle.tools


def test_extraction_requires_a_declared_write_scope():
    spec = parse_delegation_spec(task='Extract', required_tools=['extract_email_attachment'])
    assert isinstance(spec, CapabilityFailure)
    assert 'write_scope' in spec.invalid_fields


def test_one_step_email_defaults_keep_decoder_in_general_loop():
    spec = parse_delegation_spec(
        task='Read email', files=['source.EML'], budget={'max_steps': 1},
        default_required_tools=('read_file', 'read_email'),
    )
    assert isinstance(spec, DelegationSpec)
    assert spec.strategy == 'general_loop'
    assert 'read_email' in spec.required_tools


async def _run_child_call(tmp_path, email_tools, name, arguments, **options):
    observed = []

    async def stream(*, messages, **kwargs):
        if not observed:
            observed.append(None)
            yield StreamEvent(type='finish', finish_reason='tool_use', tool_calls=[
                ToolCall(id='email-call', type='function', function=FunctionCall(name=name, arguments=arguments)),
            ])
        else:
            observed.extend(message for message in messages if message.role == 'tool')
            yield StreamEvent(type='text', delta='Email task completed')
            yield StreamEvent(type='finish', finish_reason='stop', tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = stream
    child = SubAgentTool(llm=llm, parent_tools=email_tools, workspace_dir=str(tmp_path))
    result = await child.execute(task='Inspect the provided email', required_tools=[name], **options)
    assert result.success, result.error
    assert len(observed) == 2
    return observed[-1]


async def test_child_can_decode_email_through_live_tool_loop(tmp_path, email_tools):
    message = await _run_child_call(tmp_path, email_tools, 'read_email', {'path': 'source.eml'})
    assert json.loads(message.content)['body'] == '需要分析的正文'
    assert not (tmp_path / 'sales.csv').exists()


@pytest.mark.parametrize('output_path,allowed', [
    ('attachments/sales.csv', True), ('elsewhere/sales.csv', False),
])
async def test_child_checks_attachment_output_not_source_path(tmp_path, email_tools, output_path, allowed):
    message = await _run_child_call(
        tmp_path, email_tools, 'extract_email_attachment',
        {'path': 'source.eml', 'attachment_index': 1, 'output_path': output_path},
        write_scope=['attachments'],
    )
    target = tmp_path / output_path
    assert target.exists() is allowed
    if allowed:
        assert target.read_bytes() == b'amount\n100\n200\n'
        assert json.loads(message.content)['extracted_path'] == str(target)
    else:
        assert 'WRITE_SCOPE_VIOLATION' in message.content
