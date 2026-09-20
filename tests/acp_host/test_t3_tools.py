"""Host-visible skills and tool results, using shared ACP transport."""
import json

import pytest

from .probe import collect_session_updates, tool_events_from_updates


@pytest.mark.asyncio
async def test_skills_catalog_contains_named_entries(connected_probe):
    probe, _ = connected_probe
    response = await probe.ext_request('list_skills', {})
    skills = response['skills']
    assert skills
    assert all(isinstance(skill.get('name'), str) and skill['name'].strip() for skill in skills)
    assert any(skill.get('description') for skill in skills)


@pytest.mark.asyncio
async def test_read_file_result_visible_in_tool_events(live_probe):
    probe, session_id = live_probe
    (probe.cwd / 'probe_readonly.txt').write_text('ACP_HOST_PROBE_READONLY_OK\n')
    await probe.session_prompt(session_id, 'Use read_file to read probe_readonly.txt and report its content.')
    events = tool_events_from_updates(collect_session_updates(probe.drain_notifications()))
    assert events
    assert 'ACP_HOST_PROBE_READONLY_OK' in json.dumps(events)


@pytest.mark.asyncio
async def test_missing_file_error_visible_in_tool_result(live_probe):
    probe, session_id = live_probe
    await probe.session_prompt(session_id,
        'Use read_file once to read definitely_missing_acp_probe_file_xyz.txt. '
        'Do not create it. Report the failure.')
    events = tool_events_from_updates(collect_session_updates(probe.drain_notifications()))
    assert any(event.get('status') == 'failed' for event in events)
