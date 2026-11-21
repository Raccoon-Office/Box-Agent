"""CuaDriver session, schema, deadline, recovery, and evidence ownership."""
from __future__ import annotations

import copy
import json
import re
import time
import uuid


class ActionError(Exception):
    def __init__(self, result):
        self.result = result


def data(result: dict) -> dict:
    return result.get('structuredContent') or {}


def failed(result: dict) -> bool:
    state = data(result)
    return bool(result.get('isError') or result.get('is_error') or state.get('is_error')
                or state.get('refusal')
                or state.get('effect') in {
                    'refused', 'failed', 'delivery_failed', 'partial', 'suspected_noop'}
                or state.get('status') in {'refused', 'failed'})


class DriverSession:
    """Validated access to one serial CuaDriver transport."""

    def __init__(self, client, evidence_contract, evidence=None, *, timeout_seconds=60):
        self.client = client
        self.tools = client.tools
        self.evidence_contract = evidence_contract
        self.timeout_seconds = timeout_seconds
        window_props = self.tools.get(
            'get_window_state', {}).get('inputSchema', {}).get('properties', {})
        list_window_props = self.tools.get(
            'list_windows', {}).get('inputSchema', {}).get('properties', {})
        click_props = self.tools.get(
            'click', {}).get('inputSchema', {}).get('properties', {})
        self.window_tree_format = 'both' if 'tree_format' in window_props else None
        self.window_full_output = 'full_output' in window_props
        self.window_native_resolution = 'max_image_dimension' in window_props
        self.window_verbose = 'verbose' in window_props
        self.window_attachment_context = 'attachment_context' in window_props
        self.list_windows_on_screen_only = 'on_screen_only' in list_window_props
        self.click_activation_owner = 'activation_window_id' in click_props
        self.version = None
        if 'get_config' in self.tools:
            try:
                self.version = data(client.call('get_config', {})).get('version')
            except Exception:
                # The permission gate can reject config reads before first-run TCC setup.
                pass
        self.session = 'repl-' + uuid.uuid4().hex
        self.started = False
        self.evidence = evidence
        self.deadline = None
        self.cell = None
        self.invocation_id = None
        self.record_counts = {}

    def set_context(self, *, evidence=None, deadline=None, cell=None, invocation_id=None):
        self.evidence = evidence
        self.deadline = deadline
        self.cell = cell
        self.invocation_id = invocation_id

    def counts(self, cell):
        return self.record_counts.get(cell, {})

    def ensure_started(self):
        if not self.started:
            self.call('start_session', {'session': self.session})
            self.started = True

    def window_state_args(self, args, *, native_resolution=False, attachment_context=False):
        args = dict(args)
        if args.get('include_accessibility_tree') is not False and self.window_tree_format:
            args['tree_format'] = self.window_tree_format
        screenshot = args.get('include_screenshot') is True
        if (native_resolution or screenshot) and args.get('include_screenshot') is not False:
            if self.window_native_resolution:
                args['max_image_dimension'] = 0
            if self.window_verbose:
                args['verbose'] = True
        if screenshot and self.window_attachment_context:
            args['attachment_context'] = 'auto'
        elif attachment_context:
            if not self.window_attachment_context:
                raise ValueError(
                    'UnsupportedCapability: driver lacks attachment_context screenshots')
            args['attachment_context'] = 'auto'
        return args

    def validate(self, name, args):
        tool = self.tools.get(name)
        if not tool:
            raise ValueError(f'UnsupportedCapability: driver does not expose {name}')
        schema = tool['inputSchema']
        props = schema.get('properties', {})
        args = dict(args)
        if 'session' in props:
            if name in {'start_session', 'end_session'}:
                args.setdefault('session', self.session)
            else:
                args['session'] = self.session
        if args.keys() - props.keys() or set(schema.get('required', [])) - args.keys():
            raise ValueError(f'UnsupportedCapability: incompatible driver schema for {name}')
        for key, value in args.items():
            field = props[key]
            if 'enum' in field and value not in field['enum']:
                raise ValueError(f'UnsupportedCapability: {name}.{key}={value!r}')
        return args

    def call(self, name, args, *, recover_session=True):
        # 0.38 defaults to compact Markdown; observation paths consume the
        # structured elements and complete screenshot/input metadata.
        if name == 'get_window_state':
            args = self.window_state_args(args)
            if self.window_full_output:
                args = {**args, 'full_output': True}
        args = self.validate(name, args)
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('JS deadline exceeded; outcome unknown')
            self.client.timeout = min(self.timeout_seconds, remaining)
            self.client.deadline = self.deadline
        entry = {'tool': name, 'arguments': args}
        self.record({'phase': 'dispatch', **entry})
        try:
            result = self.client.call(name, args)
            entry['result'] = result
            self.record({'phase': 'result', **entry})
        except Exception as error:
            entry['error'] = str(error)
            self.record({'phase': 'error', **entry})
            raise
        if failed(result):
            # The pinned Driver reports this AX reply as text, even when a modal opened.
            if (name == 'click' and 'element_token' in args and args.get('count', 1) == 1
                    and args.get('action', 'press') == 'press' and args.get('button', 'left') == 'left'
                    and any(item.get('type') == 'text' and item.get('text', '').strip()
                            == 'AX action failed: AXUIElementPerformAction(AXPress) returned -25204'
                            for item in result.get('content', []))):
                raise ActionError({
                    **result, 'structuredContent': {**data(result), 'effect': 'unverifiable'},
                    'content': [*result['content'], {'type': 'text', 'text':
                        '动作可能已生效，先读回界面。请在下一次调用中执行 await app.getAXState() '
                        '或 await app.getAXStateAndScreenshot()，确认界面变化后再决定是否需要点击。'}],
                })
            # Only this exact daemon rejection proves the action never ran.
            # Restore the reported identity: sessionless tools use the MCP id.
            if recover_session and name not in {'start_session', 'end_session'}:
                for content in result.get('content', []):
                    match = re.fullmatch(
                        r"session has ended; tool call '" + re.escape(name)
                        + r"' was rejected\. Call start_session with session '([^'\n]+)' "
                          r"to start it again, or use a new session label\.",
                        content.get('text', ''),
                    ) or re.fullmatch(
                        r"session '([^'\n]+)' has ended; tool call '" + re.escape(name)
                        + r"' was rejected\. Call start_session with this id to revive it "
                          r"before issuing further actions, or use a new session id\.",
                        content.get('text', ''),
                    )
                    if match and ('session' not in args or match[1] == args['session']):
                        self.call('start_session', {'session': match[1]}, recover_session=False)
                        return self.call(name, args, recover_session=False)
            if (name == 'get_window_state'
                    and data(result).get('reason')
                    == 'screen_capture_kit_pixels_conflicted_with_display_crop'):
                result = copy.deepcopy(result)
                result['content'] = [{'type': 'text', 'text':
                    'Screenshot capture route changed during verification. No screenshot or snapshot was published. '
                    'Call getScreenshot() again on the same App reference for a fresh screenshot before using coordinates.'}]
            raise ActionError(result)
        return result

    def record(self, value):
        if self.evidence:
            row = {'invocation_id': self.invocation_id, 'cell': self.cell,
                   'driver_session_id': self.session, 'backend': 'native', **value}
            self.evidence_contract.validate_phase(row, self.evidence_contract.FORMAT_VERSION)
            self.evidence.write(json.dumps(row, ensure_ascii=False) + '\n')
            self.evidence.flush()
            counts = self.record_counts.setdefault(
                self.cell, {'dispatches': 0, 'errors': 0})
            if value.get('phase') == 'dispatch':
                counts['dispatches'] += 1
            elif value.get('phase') == 'error':
                counts['errors'] += 1

    def close(self):
        self.deadline = None
        self.client.deadline = None
        try:
            if self.started and 'end_session' in self.tools:
                self.client.timeout = 1
                self.call('end_session', {})
        finally:
            self.client.close()
            self.started = False
