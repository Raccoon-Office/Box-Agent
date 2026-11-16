"""Native macOS App adapter for CuaDriver 0.38.

Driver owns window enumeration and input delivery. The adapter retains PR225
focus verification, attachment checks, pixel routing and foreground fallback.
A bounded remote file-panel key helper supplements Driver delivery; unknown or
partial effects are not replayed.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from box_agent.mcp_servers.cua_panel_keyboard import (
    PanelFocusSession,
    deliver_panel_key,
    focus_file_panel,
    resolve_panel_geometry,
    verify_file_panel_front,
)


PROTOCOL = '2024-11-05'
DRIVER_TIMEOUT_SECONDS = 60
APP_LAUNCH_TIMEOUT_SECONDS = 60
APP_LAUNCH_WINDOW_TIMEOUT_SECONDS = 30
APP_LAUNCH_POLL_SECONDS = 0.25
APP_LAUNCH_STABLE_SAMPLES = 2
FLOATING_TOOLBAR_NOTICE = (
    'Floating toolbar controls are observation-only: their AX tokens belong to another window. '
    'Its Driver capture may contain a scaled document instead of the toolbar; pixel input is unavailable. '
    'Use unaddressed press_key/type_text/paste with delivery_mode=foreground only while this exact '
    'toolbar is focused (for example Escape to close it), then observe again.')
# 0.32: Box-Agent re-encodes images above this long edge before the model sees
# them. Ask the driver for at most this size so model pixels == driver pixels.
MAX_IMAGE_DIMENSION = 1568


class McpClient:
    """One serial MCP connection. A timeout poisons it: never replay mutations."""

    def __init__(self, command: list[str], timeout: float = DRIVER_TIMEOUT_SECONDS, *, env=None):
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.serial = 0
        self.inbox = queue.Queue()
        env = {k: v for k, v in (os.environ if env is None else env).items()
               if k not in {'OPENAI_API_KEY', 'OPENAI_BASE_URL'}}
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=sys.stderr, text=True, env=env)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                         'clientInfo': {'name': 'mac-cua-adapter', 'version': '1'}})
            self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            self.tools = {}
            cursor = None
            seen = set()
            while True:
                page = self.request('tools/list', {'cursor': cursor} if cursor else {})
                self.tools.update({tool['name']: tool for tool in page['tools']})
                cursor = page.get('nextCursor')
                if not cursor:
                    break
                if cursor in seen:
                    raise RuntimeError('Driver repeated tools/list cursor')
                seen.add(cursor)
            self.deadline = None
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.inbox.put(json.loads(line))
        except (ValueError, OSError) as error:
            self.inbox.put(error)
        finally:
            self.inbox.put(EOFError('Driver MCP connection closed'))

    def send(self, value: dict):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.serial += 1
        request_id = self.serial
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        deadline = min(time.monotonic() + self.timeout, self.deadline or float("inf"))
        while True:
            try:
                value = self.inbox.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                self.close()
                raise TimeoutError('Driver timed out; outcome unknown. Connection closed; do not replay.')
            if isinstance(value, Exception):
                self.close()
                raise RuntimeError('Driver connection failed; outcome unknown') from value
            if 'method' in value:
                if 'id' in value:
                    reply = {'result': {}} if value['method'] == 'ping' else {
                        'error': {'code': -32601, 'message': 'Client method not supported'}}
                    self.send({'jsonrpc': '2.0', 'id': value['id'], **reply})
                continue
            if value.get('id') != request_id:
                continue
            if 'error' in value:
                raise RuntimeError('Driver RPC error: ' + json.dumps(value['error']))
            return value['result']

    def call(self, name: str, args: dict) -> dict:
        return self.request('tools/call', {'name': name, 'arguments': args})

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


class ActionError(Exception):
    def __init__(self, result):
        self.result = result


def data(result: dict) -> dict:
    return result.get('structuredContent') or {}


def with_text_labels(elements: list[dict], result: dict) -> list[dict]:
    """Fill unlabeled addressable nodes from their display-only AXStaticText children."""
    labels = {}
    for content in result.get('content', []):
        if content.get('type') != 'text':
            continue
        parents = []
        for line in content.get('text', '').splitlines():
            indent = len(line) - len(line.lstrip())
            while parents and parents[-1][0] >= indent:
                parents.pop()
            indexed = re.match(r'\s*- \[(\d+)\] ', line)
            if indexed:
                parents.append((indent, int(indexed.group(1))))
                continue
            static = re.match(r'\s*- AXStaticText = ("(?:[^"\\]|\\.)*")', line)
            if static and parents:
                try:
                    labels.setdefault(parents[-1][1], json.loads(static.group(1)))
                except json.JSONDecodeError:
                    pass
    return [{**element, 'label': labels[element['element_index']]}
            if not element.get('label') and element.get('element_index') in labels else element
            for element in elements]


def compact_rows(elements: list[dict]) -> list[dict]:
    """Drop unlabeled single-cell row wrappers while preserving useful state."""
    # Index candidate rows in one pass instead of rescanning every node's
    # descendants. Only depth+1 entries count as direct children.
    rows, stack = {}, []
    for index, element in enumerate(elements):
        depth = element.get('depth', 0)
        while stack and stack[-1][1] >= depth:
            rows[stack.pop()[0]]['end'] = index
        if stack and stack[-1][1] + 1 == depth:
            parent = rows[stack[-1][0]]
            parent['count'] += 1
            if parent['count'] == 1:
                parent['child'] = element
        if element.get('role') == 'AXRow' and not element.get('label'):
            rows[index] = {'end': len(elements), 'count': 0, 'child': None}
            stack.append((index, depth))
    compact = []
    position = 0
    while position < len(elements):
        row = elements[position]
        info = rows.get(position)
        child = info['child'] if info else None
        if (info and info['count'] == 1 and child.get('role') == 'AXCell'
                and child.get('element_token')):
            for original in elements[position + 1:info['end']]:
                item = {**original, 'depth': original.get('depth', 0) - 1}
                if original is child:
                    item.update({flag: True for flag in ('selected', 'focused')
                                 if row.get(flag) is True})
                compact.append(item)
            position = info['end']
            continue
        compact.append(row)
        position += 1
    return compact


def failed(result: dict) -> bool:
    state = data(result)
    return bool(result.get('isError') or result.get('is_error') or state.get('is_error') or state.get('refusal')
                or state.get('effect') in {'refused', 'failed', 'delivery_failed', 'partial', 'suspected_noop'}
                or state.get('status') in {'refused', 'failed'})


def ended_session(name: str, text: str) -> dict | None:
    """Return start_session args when text proves the call was rejected unrun."""
    # Named-session refusal in CuaDriver 0.38 (cua-driver/src/serve.rs).
    match = re.fullmatch(
        r"session has ended; tool call '" + re.escape(name)
        + r"' was rejected\. Call start_session with session '([^'\n]+)' to start it again, "
          r"or use a new session label\.", text)
    if match:
        return {'session': match[1]}
    return None


def rectangle(value):
    if not isinstance(value, dict):
        return None
    try:
        output = tuple(float(value[name]) for name in ('x', 'y')) + (
            float(value['width'] if 'width' in value else value['w']),
            float(value['height'] if 'height' in value else value['h']),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return output if all(math.isfinite(number) for number in output) else None


def clear_image(state):
    for name in ('image_frame', 'image_size', 'image_bounds', 'image_background_input', 'image_source_window'):
        state.pop(name, None)


def native_focused_window_id(pid: int, *, require_nonminimized=False) -> int | None:
    """Read application-local AX focus; never activate or raise the app."""
    if sys.platform != 'darwin':
        return None
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    ax = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    cf.CFBooleanGetValue.restype = ctypes.c_bool
    cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
    cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
    cf.CFGetTypeID.restype = ctypes.c_ulong
    ax.AXUIElementGetTypeID.restype = ctypes.c_ulong
    ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
    ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
    ax.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_void_p)]
    ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
    ax._AXUIElementGetWindow.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    ax._AXUIElementGetWindow.restype = ctypes.c_int32
    app = ax.AXUIElementCreateApplication(pid)
    key = cf.CFStringCreateWithCString(None, b'AXFocusedWindow', 0x08000100)
    focused = ctypes.c_void_p()
    try:
        if (not app or not key or ax.AXUIElementCopyAttributeValue(app, key, ctypes.byref(focused)) != 0
                or not focused.value or cf.CFGetTypeID(focused) != ax.AXUIElementGetTypeID()):
            return None
        if require_nonminimized:
            minimized_key = cf.CFStringCreateWithCString(None, b'AXMinimized', 0x08000100)
            minimized = ctypes.c_void_p()
            try:
                if (ax.AXUIElementCopyAttributeValue(focused, minimized_key, ctypes.byref(minimized)) != 0
                        or not minimized.value
                        or cf.CFGetTypeID(minimized) != cf.CFBooleanGetTypeID()
                        or cf.CFBooleanGetValue(minimized)):
                    return None
            finally:
                for value in (minimized.value, minimized_key):
                    if value:
                        cf.CFRelease(value)
        window_id = ctypes.c_uint32()
        if ax._AXUIElementGetWindow(focused, ctypes.byref(window_id)) == 0 and window_id.value:
            return window_id.value
        return None
    finally:
        for value in (focused.value, key, app):
            if value:
                cf.CFRelease(value)


def app_elements(elements: list[dict]) -> list[dict]:
    """Keep app UI and app menus; omit the global Apple menu subtree."""
    visible = []
    position = 0
    while position < len(elements):
        element = elements[position]
        position += 1
        if element.get('role') == 'AXMenuBarItem':
            end = position
            while end < len(elements) and elements[end].get('depth', 0) > element.get('depth', 0):
                end += 1
            if element.get('label') in {'Apple', '\uf8ff'}:
                position = end
                continue
            # Unknown menu state stays expanded. Never use labels to infer visibility.
            if element.get('selected') is False and not any(
                    e.get(flag) is True for e in elements[position - 1:end]
                    for flag in ('selected', 'expanded', 'focused')):
                position = end
        visible.append(element)
    return visible


def active_menu_elements(elements: list[dict]) -> list[dict]:
    """Return the unique selected menu-bar item subtree, rebased as the active root."""
    selected = [(position, element) for position, element in enumerate(elements)
                if (element.get('role') == 'AXMenuBarItem'
                    and element.get('label') not in {'Apple', '\uf8ff'}
                    and element.get('selected') is True)]
    if len(selected) != 1:
        return []
    start, root = selected[0]
    depth = root.get('depth', 0)
    end = start + 1
    while end < len(elements) and elements[end].get('depth', 0) > depth:
        end += 1
    return [{**item, 'depth': max(0, item.get('depth', 0) - depth)}
            for item in elements[start:end]]


def attached_sheet_elements(elements: list[dict], bounds: dict | None = None,
                            role: str = 'sheet') -> list[dict]:
    """Resolve a sheet or an exact, top-level floating toolbar from a parent tree."""
    target = rectangle(bounds) if bounds is not None else None
    if bounds is not None and target is None:
        return []
    for start, element in enumerate(elements):
        actual_role = str(element.get('role', '')).removeprefix('AX').lower()
        floating = (role == 'sheet' and actual_role == 'toolbar'
                    and element.get('depth', 0) == 0 and target is not None)
        if actual_role != role and not floating:
            continue
        frame = rectangle(element.get('frame'))
        # Match the attached surface to its subtree in the parent AX snapshot.
        if target is not None and frame != target:
            continue
        depth = element.get('depth', 0)
        end = start + 1
        while end < len(elements) and elements[end].get('depth', 0) > depth:
            end += 1
        return [{**item, 'depth': max(0, item.get('depth', 0) - depth)}
                for item in elements[start:end]]
    return []


def ax_line(index: int, element: dict) -> str:
    """One compact, single-line model-visible node; no driver snapshot identity."""
    readable = lambda name: re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name.removeprefix('AX')).lower()
    role = readable(element.get('role', 'element'))
    flags = [name for name in ('selected', 'focused', 'settable') if element.get(name) is True]
    if element.get('enabled') is False:
        flags.insert(0, 'disabled')
    if type(element.get('expanded')) is bool:
        flags.append('expanded' if element['expanded'] else 'collapsed')
    line = f'{index} {role}' + (f' ({", ".join(flags)})' if flags else '')
    label = element.get('label')
    if label and not str(label).startswith('_NS:'):
        line += ' ' + json.dumps(label, ensure_ascii=False)
    for key in ('value', 'description', 'help', 'identifier', 'url', 'min', 'max'):
        if key in element and element[key] is not None:
            line += f', {key.title()}: ' + json.dumps(element[key], ensure_ascii=False)
    actions = [readable(action) for action in element.get('actions', [])
               if action not in {'AXPress', 'AXPick', 'AXCancel'}]
    if actions:
        line += ', Actions: ' + ', '.join(actions)
    return '\t' * min(50, max(0, element.get('depth', 0))) + line


class Adapter:
    """Internal native API. Model IDs are positions in the latest app snapshot."""

    def __init__(self, driver, evidence=None):
        self.driver = driver
        self.panel_focus = PanelFocusSession()
        # A cua-driver session belongs to this transport, not the REPL service.
        self.session = 'repl-' + uuid.uuid4().hex
        self.handles = {}
        self.started = False
        self.cell = None
        self.invocation_id = None
        self.evidence = evidence
        self.deadline = None
        self.record_counts = {}
        self.screen_ratio = None
        self.event_sink = None
        self.action_app_inventory = None
        self.observation_max_depth = None

    def validate(self, name, args):
        tool = self.driver.tools.get(name)
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
        if name == 'get_window_state':
            # Internal consumers need elements, metadata and the full menu tree.
            args = {**args, 'full_output': True}
        args = self.validate(name, args)
        # Re-read app identity/activity after any attempted UI mutation, even
        # when the Driver refuses it and the existing foreground fallback runs.
        if name not in {'list_apps', 'list_windows', 'get_window_state',
                        'get_screen_size', 'clipboard_write'}:
            self.clear_action_app_inventory()
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('JS deadline exceeded; outcome unknown')
            self.driver.timeout = min(DRIVER_TIMEOUT_SECONDS, remaining)
            self.driver.deadline = self.deadline
        entry = {'tool': name, 'arguments': args}
        self.record({'phase': 'dispatch', **entry})
        try:
            result = self.driver.call(name, args)
            entry['result'] = result
            self.record({'phase': 'result', **entry})
        except Exception as error:
            entry['error'] = str(error)
            self.record({'phase': 'error', **entry})
            raise
        if failed(result) and name == 'bring_to_front':
            report = data(result)
            pid, wid = args.get('pid'), args.get('window_id')
            if (type(pid) is int and type(wid) is int
                    and report.get('pid') == pid and report.get('window_id') == wid
                    and report.get('code') in {'bring_to_front_exact_window_unverified',
                                               'bring_to_front_window_not_ordinary'}):
                # Driver 0.38 excludes layer-8 file panels from its postcondition.
                # Do not activate again or trust a stale success flag: independently
                # prove the exact live panel. Failure preserves the original error.
                try:
                    proof = verify_file_panel_front(pid, wid, timeout=self.driver.timeout)
                    self.record({'phase': 'file_panel_front_verification',
                                 'pid': pid, 'window_id': wid, 'result': proof})
                except Exception as error:
                    self.record({'phase': 'file_panel_front_verification_failed',
                                 'pid': pid, 'window_id': wid, 'reason': str(error)})
                    proof = None
                if (isinstance(proof, dict) and proof.get('effect') == 'confirmed'
                        and proof.get('pid') == pid and proof.get('window_id') == wid
                        and proof.get('code') == 'file_panel_front_verified'):
                    corrected = {**report, 'status': 'activated', 'activated': True,
                                 'code': 'bring_to_front_exact_window_verified',
                                 'process_activated': True,
                                 'exact_window_effect': {
                                     **report.get('exact_window_effect', {}),
                                     'focused': True, 'front_in_process_on_display': True,
                                     'verified': True},
                                 'observed': {**report.get('observed', {}),
                                              'focused_window_id': wid,
                                              'process_front_window_on_display': wid},
                                 'verification_source': 'adaptor_file_panel',
                                 'verification': proof, 'driver_result': report}
                    result = {'content': [{'type': 'text', 'text':
                              'Exact file panel verified in front by adaptor after Driver layer-filter refusal.'}],
                              'structuredContent': corrected}
                    self.record({'phase': 'file_panel_front_corrected', **entry,
                                 'result': result})
        if failed(result):
            # Only this exact daemon rejection proves the action never ran.
            # Restore the reported identity: sessionless tools use the MCP id.
            if recover_session and name not in {'start_session', 'end_session'}:
                for content in result.get('content', []):
                    revive = ended_session(name, content.get('text', ''))
                    if revive is not None:
                        self.call('start_session', revive, recover_session=False)
                        return self.call(name, args, recover_session=False)
            raise ActionError(result)
        return result

    def record(self, value):
        if self.event_sink is not None:
            self.event_sink(value)
        if self.evidence:
            self.evidence.write(json.dumps({'timestamp': time.time(),
                                            'invocation_id': self.invocation_id, 'cell': self.cell,
                                            'driver_session_id': self.session, 'backend': 'native', **value}, ensure_ascii=False) + '\n')
            self.evidence.flush()
            counts = self.record_counts.setdefault(self.cell, {'dispatches': 0, 'errors': 0})
            if value.get('phase') == 'dispatch':
                counts['dispatches'] += 1
            elif value.get('phase') == 'error':
                counts['errors'] += 1

    def invalidate(self):
        for state in self.handles.values():
            state['fresh'] = False
            state.pop('floating_images', None)

    def installed_app(self, requested):
        apps = data(self.call('list_apps', {})).get('apps', [])
        if self.action_app_inventory is not None:
            self.action_app_inventory['apps'] = apps
        if requested.startswith('/'):
            matches = [app for app in apps if app.get('launch_path') == requested]
        else:
            matches = [app for app in apps if app.get('bundle_id') == requested]
            if not matches:
                matches = [app for app in apps if app.get('name') == requested]
        if not matches:
            raise ValueError(f'ApplicationNotFound: {requested}')
        if len(matches) != 1:
            raise ValueError(f'ApplicationAmbiguous: {requested}')
        return matches[0]

    def stable_launched_app(self, bundle_id, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + APP_LAUNCH_TIMEOUT_SECONDS
        previous_pid = None
        stable_samples = 0
        while True:
            apps = data(self.call('list_apps', {})).get('apps', [])
            running = [app for app in apps if app.get('bundle_id') == bundle_id
                       and app.get('running') is True
                       and type(app.get('pid')) is int and app['pid'] > 0]
            if len(running) > 1:
                raise ValueError(f'ApplicationAmbiguous: {bundle_id}')
            pid = running[0]['pid'] if running else None
            if pid is not None and pid == previous_pid:
                stable_samples += 1
            else:
                previous_pid = pid
                stable_samples = 1 if pid is not None else 0
            if stable_samples >= APP_LAUNCH_STABLE_SAMPLES:
                return running[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(
                    f'ApplicationLaunchUnstable: {bundle_id} did not keep a stable PID')
            time.sleep(min(APP_LAUNCH_POLL_SECONDS, remaining))

    def wait_for_launch_window(self, state):
        deadline = time.monotonic() + APP_LAUNCH_WINDOW_TIMEOUT_SECONDS
        while True:
            try:
                if self.visible_windows(state):
                    return True
            except ValueError as error:
                if str(error) != 'ApplicationChanged: process identity expired; call cua.getApp again':
                    raise
                opened = self.stable_launched_app(state['bundle_id'], deadline)
                state['pid'] = opened['pid']
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(APP_LAUNCH_POLL_SECONDS, remaining))

    def clear_action_app_inventory(self):
        if self.action_app_inventory is not None:
            self.action_app_inventory.clear()

    def visible_windows(self, state, *, focus_result=None):
        if state.get('bundle_id'):
            inventory = self.action_app_inventory
            if inventory is None or 'apps' not in inventory:
                apps = data(self.call('list_apps', {})).get('apps', [])
                if inventory is not None:
                    inventory['apps'] = apps
            else:
                apps = inventory['apps']
            live = [app for app in apps if app.get('bundle_id') == state['bundle_id']
                    and app.get('pid') == state['pid'] and app.get('running') is not False]
            if len(live) != 1:
                raise ValueError('ApplicationChanged: process identity expired; call cua.getApp again')
            state['app_active'] = live[0].get('active') is True
        result = self.call('list_windows', {'pid': state['pid']})
        candidates = [w for w in data(result).get('windows', [])
                      if w.get('pid', state['pid']) == state['pid']]
        # WindowServer visibility is not window existence. An occluded app can
        # retain a live, non-minimized AX-focused window on the current Space.
        offscreen = [w for w in candidates if w.get('is_on_screen') is not True
                     and w.get('on_current_space') is True]
        try:
            focused = native_focused_window_id(state['pid'], require_nonminimized=True) if offscreen else None
        except (OSError, AttributeError):
            focused = None
        if focused is not None and focus_result is not None:
            focus_result['window_id'] = focused
        return [w for w in candidates if w.get('is_on_screen') is True
                or (w in offscreen and w['window_id'] == focused)]


    def interaction_windows(self, state):
        """Use Driver's all-layer, filtered inventory and read app-local focus."""
        focus_result = {}
        rows = self.visible_windows(state, focus_result=focus_result)
        try:
            # The visibility check may already have proved this exact AX focus
            # and non-minimized state. No UI action occurred between the reads.
            focused_id = (focus_result['window_id'] if 'window_id' in focus_result
                          else native_focused_window_id(state['pid']))
        except (OSError, AttributeError):
            focused_id = None
        if focused_id is not None and any(w['window_id'] == focused_id for w in rows):
            rows = [{**w, 'is_focused': w['window_id'] == focused_id} for w in rows]
        return rows

    def windows(self, state):
        return self.selected_window(state, self.interaction_windows(state))

    def selected_window(self, state, rows):
        if any(w.get('is_focused') is True for w in rows):
            state.pop('focus_hint', None)
            return self.current_window(rows)
        hint = state.get('focus_hint')
        frames = [self.frame(w) for w in sorted(rows, key=lambda w: w['window_id'])]
        if hint and hint['frames'] == frames:
            matches = [w for w in rows if w['window_id'] == hint['window_id']]
            if len(matches) == 1:
                return matches[0]
        state.pop('focus_hint', None)
        return self.current_window(rows)

    @staticmethod
    def current_window(rows):
        focused = [w for w in rows if w.get('is_focused') is True]
        if len(focused) == 1:
            return focused[0]
        if len(rows) == 1:
            return rows[0]
        ranked = [w for w in rows if type(w.get('z_index')) is int]
        if len(ranked) == len(rows) and ranked:
            # Driver reverses CGWindowList order: the largest z_index is frontmost.
            top = max(w['z_index'] for w in ranked)
            winners = [w for w in ranked if w['z_index'] == top]
            if len(winners) == 1:
                return winners[0]
        raise ValueError('WindowAmbiguous: no proven current window. Inspect app/window UI; '
                         'cross-process panels cannot be guessed. Candidates: ' + json.dumps(rows))

    @staticmethod
    def frame(window):
        return (window['window_id'], window.get('bounds'))


    def focus_window(self, state, active_window, allow_correction=False,
                     keyboard=False, observation_fresh=False, activate=False):
        try:
            return self._focus_window(state, active_window, allow_correction,
                                      keyboard, observation_fresh, activate)
        except (ValueError, ActionError) as error:
            self.record({'phase': 'attachment_rejected', 'window_id': active_window['window_id'],
                         'reason': str(error)})
            raise

    def current_file_panel(self, state, window):
        """Fresh, exact Driver AX root proof; never infer a panel from its title."""
        properties = self.driver.tools.get('get_window_state', {}).get('inputSchema', {}).get('properties', {})
        # This predicate consumes only depth-zero roots and their identifiers.
        # Walking the file list cannot add evidence to this identity check.
        root_options = {'max_depth': 1} if 'max_depth' in properties else {}
        result = self.call('get_window_state', {
            'pid': state['pid'], 'window_id': window['window_id'],
            'include_accessibility_tree': True, 'include_screenshot': False, **root_options})
        roots = [e for e in data(result).get('elements', [])
                 if e.get('depth') == 0 and e.get('role') in {'AXWindow', 'AXSheet'}]
        if len(roots) != 1:
            return False
        # Driver 0.38 omits AXIdentifier from structured elements but includes
        # it in the AX root's textual representation. Match only that root.
        for item in result.get('content', []):
            if item.get('type') != 'text':
                continue
            for line in item.get('text', '').splitlines():
                if re.match(r'^- \[0\] AX(?:Window|Sheet) ', line):
                    return re.search(r'\[id=(?:open-panel|save-panel|GoToWindow)(?: |\])', line) is not None
        return False

    def verified_nonordinary_window(self, state, window):
        """PR225 supplemental-window proof, using Driver 0.38's own inventory."""
        if type(window.get('layer')) is not int or window['layer'] == 0:
            return False
        observed = state.get('window_states', {}).get(window['window_id'], {})
        roots = [e for e in observed.get('raw_elements', [])
                 if e.get('depth', 0) == 0 and e.get('role') == 'AXWindow']
        return (observed.get('pid') == state['pid'] and len(roots) == 1
                and observed.get('window') is not None
                and self.frame(observed['window']) == self.frame(window))


    def verify_frame(self, state, window, *, rows=None):
        try:
            if rows is None:
                rows = self.interaction_windows(state)
            current = self.selected_window(state, rows)
            if self.frame(current) != self.frame(window):
                raise ValueError('WindowChanged: observe the current target again')
            return rows
        except Exception:
            clear_image(state)
            self.invalidate()
            raise

    def attachment_chain(self, state, window_id, rows, path=()):
        """Follow only unique AX-proven edges; reject cycles, stale frames and deep graphs."""
        if window_id in path or len(path) >= 16:
            raise ValueError('AttachmentUnverified: cyclic or excessively deep window relationship')
        if window_id in state.get('attachment_conflicts', set()):
            raise ValueError('AttachmentUnverified: multiple windows match an AX surface')
        graph = state.get('attachments', {})
        edges = graph.get(window_id, [])
        if not edges:
            return [window_id]
        if len(edges) != 1:
            raise ValueError('AttachmentUnverified: multiple AX parents match the window')
        edge = edges[0]
        frames = {row['window_id']: self.frame(row) for row in rows}
        if (frames.get(window_id) != edge['surface']
                or frames.get(edge['source'][0]) != edge['source']):
            raise ValueError('WindowChanged: attachment or ancestor moved/closed; observe again')
        return [window_id] + self.attachment_chain(state, edge['source'][0], rows, (*path, window_id))


    def build_attachments(self, state, rows):
        """Match nested AX surfaces to unique same-app windows, never proximity."""
        windows = {row['window_id']: row for row in rows
                   if row.get('pid', state['pid']) == state['pid']}
        window_bounds = {wid: rectangle(row.get('bounds')) for wid, row in windows.items()}
        windows_by_bounds = {}
        for wid, bounds in window_bounds.items():
            if bounds is not None:
                windows_by_bounds.setdefault(bounds, []).append(wid)
        graph, focused, token_owners, conflicts = {}, set(), {}, set()
        focus_count = 0
        focus_reported = False
        surface_roles = {'AXSheet', 'AXPopover', 'AXMenu', 'AXWindow'}
        for source, elements in state.get('attachment_ax', {}).items():
            if source not in windows:
                continue
            stack = []
            window_depths = [e.get('depth', 0) for e in elements if e.get('role') == 'AXWindow']
            rootless = not window_depths
            unique_root = bool(window_depths) and window_depths.count(min(window_depths)) == 1
            for element in elements:
                depth = element.get('depth', 0)
                while stack and stack[-1][0] >= depth:
                    stack.pop()
                owner = stack[-1][1] if stack else None
                role = element.get('role')
                bounds = rectangle(element.get('frame')) if role in surface_roles else None
                anchor = role == 'AXWindow' and owner is None
                if anchor:
                    # Only the source root anchors ownership; siblings are not children.
                    if ((bounds is None and unique_root)
                            or bounds == window_bounds[source]):
                        owner = source
                if (owner is None and role in {'AXSheet', 'AXPopover'}
                        and rootless):
                    # Older snapshots omit the source window root for sheets/popovers.
                    owner = source
                if role in surface_roles and owner is not None and not anchor:
                    matches = [wid for wid in windows_by_bounds.get(bounds, ()) if wid != owner]
                    if len(matches) == 1:
                        child = matches[0]
                        edge = {'surface': self.frame(windows[child]),
                                'source': self.frame(windows[owner]), 'role': role}
                        edges = graph.setdefault(child, [])
                        if not any(e['source'] == edge['source'] and e['surface'] == edge['surface']
                                   for e in edges):
                            edges.append(edge)
                        owner = child
                    elif len(matches) > 1:
                        conflicts.update(matches)
                        owner = None
                    elif role == 'AXWindow':
                        owner = None
                token = element.get('element_token')
                if token and owner is not None:
                    token_owners.setdefault((source, token), set()).add(owner)
                if role not in surface_roles and type(element.get('focused')) is bool:
                    focus_reported = True
                    if element['focused']:
                        focused.add(owner)
                        focus_count += 1
                stack.append((depth, owner))
        state.update(attachments=graph, attachment_focused=focused,
                     attachment_focus_count=focus_count, attachment_focus_reported=focus_reported,
                     attachment_tokens=token_owners,
                     attachment_conflicts=conflicts)


    def inspect_window_relationships(self, state, current, rows, *, source_ids=None, max_depth=None):
        """Capture multi-window AX evidence before activation can alter a transient UI."""
        self.invalidate()
        state['fresh'] = False
        clear_image(state)
        sources = []
        state.update(attachment_ax={}, unresolved_windows=set())
        candidates = [current] + sorted(
            (row for row in rows if row['window_id'] != current['window_id']
             and (source_ids is None or row['window_id'] in source_ids)),
            key=lambda row: row.get('z_index', -1), reverse=True)
        for window in candidates:
            identity = {'pid': state['pid'], 'window_id': window['window_id']}
            try:
                snapshot = self.call('get_window_state', {
                    **identity, 'include_screenshot': False, 'include_accessibility_tree': True,
                    **({'max_depth': max_depth} if max_depth is not None else {})})
            except ActionError:
                # The raw rejection is already in evidence; it proves no relationship.
                continue
            raw = data(snapshot)
            if any(key in raw and raw[key] != value for key, value in identity.items()):
                raise ValueError('WindowChanged: relationship observation belongs to another app/window')
            elements = raw.get('elements', [])
            state['attachment_ax'][window['window_id']] = elements
            if str(raw.get('degraded_reason', '')).startswith('ax_window_unresolved'):
                state['unresolved_windows'].add(window['window_id'])
            menu = active_menu_elements(elements)
            sheet = attached_sheet_elements(elements, current.get('bounds')) if current.get('bounds') else []
            popover = attached_sheet_elements(elements, current.get('bounds'), 'popover') if current.get('bounds') else []
            sources.append({
                'window_id': window['window_id'],
                'active_menu': bool(menu),
                'matching_sheet': bool(sheet),
                'matching_popover': bool(popover),
            })
        verified_rows = self.verify_relationship_frames(state, rows)
        self.build_attachments(state, rows)
        self.record({'phase': 'window_relationships', 'pid': state['pid'],
                     'current_window_id': current['window_id'], 'windows': rows,
                     'ax_sources': sources, 'attachments': state['attachments'],
                     'conflicts': sorted(state['attachment_conflicts'])})
        # Read-only evidence is not permission to send input; validate the chain per action.
        self.verify_frame(state, current, rows=verified_rows)
        return verified_rows


    def verify_relationship_frames(self, state, rows):
        current = self.interaction_windows(state)
        if ({w['window_id']: self.frame(w) for w in rows}
                != {w['window_id']: self.frame(w) for w in current}):
            state.update(attachments={}, attachment_ax={}, attachment_tokens={},
                         attachment_conflicts=set(), fresh=False)
            clear_image(state)
            self.invalidate()
            self.record({'phase': 'attachment_rejected',
                         'reason': 'WindowChanged: window inventory changed during observation'})
            raise ValueError('WindowChanged: window inventory changed during observation; observe again')
        return current


    def refresh_focused_file_sheet(self, state, window, rows, chain):
        """Refresh a known GoTo sheet through direct AX ancestry, not a file-tree walk.

        The native reader accepts only AXFocusedWindow=GoToWindow under an
        open/save panel and matches both AX frames and CGWindow ownership.
        No element tokens or old AX nodes are retained as fresh evidence.
        """
        if (len(chain) < 2
                or window['window_id'] in state.get('attachment_conflicts', set())):
            return None
        edges = state.get('attachments', {}).get(window['window_id'], [])
        parent = next((row for row in rows if row['window_id'] == chain[1]), None)
        if len(edges) != 1 or edges[0].get('role') != 'AXSheet' or parent is None:
            return None
        try:
            report = self.verify_panel_geometry(state, window, parent, rows=rows)
        except ValueError:
            return None  # Ordinary AX validation remains the fallback; no input sent.
        verified = self.verify_relationship_frames(state, rows)
        self.verify_frame(state, window, rows=verified)
        frames = {row['window_id']: self.frame(row) for row in verified}
        state.update(attachments={child: [{
            'surface': frames[child], 'source': frames[parent_id],
            'role': 'AXSheet', 'proof': 'native_focused_ax_ancestry'}]
            for child, parent_id in zip(chain, chain[1:])},
            attachment_ax={}, attachment_tokens={}, attachment_conflicts=set(),
            attachment_focus_reported=False, attachment_focus_count=0,
            attachment_focused=set(), fresh=False)
        self.record({'phase': 'focused_file_sheet_verified', 'chain': chain, 'report': report})
        return verified

    def refresh_keyboard_focus(self, state, window):
        """Refresh evidence, tolerating one transient inventory change before input."""
        wid = window['window_id']
        ordinary = (window.get('layer', 0) == 0
                    and wid not in state.get('attachments', {})
                    and wid not in state.get('attachment_conflicts', set())
                    and wid not in state.get('unresolved_windows', set()))
        try:
            return self._refresh_keyboard_focus(state, window)
        except ValueError as error:
            # Photoshop can create/dismiss a contextual toolbar during this
            # read-only check. No input or clipboard write has occurred yet.
            # Keep the original target: the fresh pass must prove its frame,
            # active identity and control focus again. Never replay delivery.
            if (not ordinary or not str(error).startswith(
                    'WindowChanged: window inventory changed during observation')
                    or (self.deadline is not None and time.monotonic() >= self.deadline)):
                raise
            self.clear_action_app_inventory()
            self.record({'phase': 'keyboard_focus_retry', 'window_id': wid,
                         'reason': str(error)})
            return self._refresh_keyboard_focus(state, window)

    def _refresh_keyboard_focus(self, state, window):
        """Refresh input evidence without replacing the model's displayed AX snapshot."""
        self.invalidate()
        clear_image(state)
        try:
            rows = self.verify_frame(state, window)
            previous_chain = self.attachment_chain(state, window['window_id'], rows)
            direct_rows = self.refresh_focused_file_sheet(state, window, rows, previous_chain)
            if direct_rows is not None:
                return direct_rows
            depth = state.get('relationship_max_depth') if len(previous_chain) > 1 else None
            verified_rows = self.inspect_window_relationships(
                state, window, rows, source_ids=previous_chain, max_depth=depth)
            if depth is not None:
                try:
                    scoped_chain = self.attachment_chain(state, window['window_id'], verified_rows)
                except ValueError:
                    scoped_chain = None
                explicit_focus = (state.get('attachment_focus_reported')
                                  and state.get('attachment_focus_count') == 1
                                  and state.get('attachment_focused') == {window['window_id']})
                native_focus = (not state.get('attachment_focus_reported')
                                and any(row['window_id'] == window['window_id']
                                        and row.get('is_focused') is True for row in verified_rows))
                if scoped_chain != previous_chain or not (explicit_focus or native_focus):
                    # Driver may omit AXFocused on controls entirely. The fresh
                    # app-local AXFocusedWindow is then sufficient surface proof;
                    # explicit conflicting control focus must never be overridden.
                    verified_rows = self.inspect_window_relationships(
                        state, window, verified_rows, source_ids=previous_chain)
            chain = self.attachment_chain(state, window['window_id'], rows)
            if chain != previous_chain:
                raise ValueError('WindowChanged: keyboard attachment chain changed; observe again')
            if window['window_id'] not in state.get('attachment_ax', {}):
                raise ValueError('AttachmentUnverified: keyboard AX refresh failed; input not sent')
            if len(chain) == 1 and not state['attachment_ax'][window['window_id']]:
                raise ValueError('AttachmentUnverified: no proven AX parent or accessible target; input not sent')
            if (state.get('attachment_focus_reported')
                    and (state.get('attachment_focus_count') != 1
                         or state.get('attachment_focused') != {window['window_id']})):
                raise ValueError('AttachmentUnverified: focused-control evidence conflicts with target; input not sent')
            self.record({'phase': 'keyboard_focus_refreshed', 'window_id': window['window_id'],
                         'chain': chain, 'focus_reported': state.get('attachment_focus_reported'),
                         'focus_count': state.get('attachment_focus_count')})
            return verified_rows
        except Exception as error:
            self.record({'phase': 'keyboard_focus_rejected', 'window_id': window['window_id'],
                         'reason': str(error)})
            raise


    def native_file_panel_front(self, state, window):
        """Fresh CG order + exact AX focus, independent of Driver z_index."""
        report = verify_file_panel_front(state['pid'], window['window_id'], timeout=self.driver.timeout)
        self.record({'phase': 'file_panel_front_verification', 'result': report})
        return (isinstance(report, dict) and report.get('effect') == 'confirmed'
                and report.get('read_only') is True
                and report.get('pid') == state['pid']
                and report.get('window_id') == window['window_id']
                and report.get('focused_window_id') == window['window_id']
                and report.get('frontmost_pid') == state['pid']
                and tuple(report.get('frame', [])) == rectangle(window.get('bounds')))

    def verify_unresolved_foreground(self, state, window, rows):
        """An empty AX tree is not a missing window: prove exact live focus.

        Photoshop's New Layer dialog exposes an AXLayoutArea rather than an
        AXWindow. Do not invent an attachment or permit background delivery.
        Only explicit, untargeted foreground input may use Driver's exact-window
        route after OS focus and the observed CG frame agree.
        """
        wid = window['window_id']
        self.verify_frame(state, window, rows=rows)
        if (wid in state.get('attachments', {})
                or wid in state.get('attachment_conflicts', set())
                or state.get('app_active') is not True
                or native_focused_window_id(state['pid']) != wid):
            raise ValueError('AttachmentUnverified: unresolved foreground target lacks exact OS focus; input not sent')
        self.record({'phase': 'unresolved_foreground_verified', 'pid': state['pid'],
                     'window_id': wid, 'frame': window.get('bounds')})

    def focus_file_panel(self, state, window):
        report = focus_file_panel(state['pid'], window['window_id'], timeout=self.driver.timeout)
        self.record({'phase': 'file_panel_foreground_focus', 'report': report})
        if (report.get('effect') != 'confirmed'
                or report.get('pid') != state['pid']
                or report.get('window_id') != window['window_id']
                or report.get('focused_window_id') != window['window_id']
                or report.get('frontmost_pid') != state['pid']):
            raise ValueError('AttachmentUnverified: native panel focus not verified; input not sent')

    def _focus_window(self, state, active_window, allow_correction=False,
                      keyboard=False, observation_fresh=False, activate=False):
        wid = active_window['window_id']
        if (wid in state.get('attachments', {}) or wid in state.get('unresolved_windows', set())
                or wid in state.get('attachment_conflicts', set())):
            rows = self.interaction_windows(state)
            chain = self.attachment_chain(state, wid, rows)
            if len(chain) < 2:
                raise ValueError('AttachmentUnverified: no proven AX parent; input not sent')
            chain_frames = {row['window_id']: self.frame(row) for row in rows
                            if row['window_id'] in chain}
            ranked = [row for row in rows if type(row.get('z_index')) is int]
            front = ([row['window_id'] for row in ranked
                      if row['z_index'] == max(item['z_index'] for item in ranked)]
                     if ranked and len(ranked) == len(rows) else [])
            blocked = (len(front) == 1
                       and wid not in self.attachment_chain(state, front[0], rows))
            if activate and (state.get('app_active') is not True or blocked):
                # Office sheets may be layer 0. Identify the exact native file
                # panel from live AX evidence, never from layer or window title.
                native_panel = self.refresh_focused_file_sheet(state, active_window, rows, chain) is not None
                if not native_panel:
                    native_panel = self.current_file_panel(state, active_window)
                try:
                    if native_panel:
                        self.focus_file_panel(state, active_window)
                    else:
                        self.call('bring_to_front', {'pid': state['pid'], 'window_id': chain[-1]})
                except ActionError as error:
                    # A sheet may retain AX focus when its owning document is
                    # activated. Re-prove foreground ownership and the complete
                    # attachment chain below; no input has been sent yet.
                    if data(error.result).get('code') != 'bring_to_front_exact_window_unverified':
                        raise
                rows = self.interaction_windows(state)
                direct_rows = self.refresh_focused_file_sheet(state, active_window, rows, chain)
                if direct_rows is None:
                    self.inspect_window_relationships(state, active_window, rows)
                else:
                    rows = direct_rows
                if (self.attachment_chain(state, wid, rows) != chain
                        or {row['window_id']: self.frame(row) for row in rows
                            if row['window_id'] in chain} != chain_frames):
                    raise ValueError('WindowChanged: attachment changed during activation; input not sent')
                observation_fresh = True
            if state.get('app_active') is not True:
                raise ValueError('AttachmentUnverified: owning app is not confirmed foreground')
            ranked = [row for row in rows if type(row.get('z_index')) is int]
            if len(ranked) != len(rows):
                raise ValueError('AttachmentUnverified: frontmost surface is unknown')
            top = max(row['z_index'] for row in ranked)
            front = [row['window_id'] for row in ranked if row['z_index'] == top]
            if len(front) != 1:
                raise ValueError('AttachmentUnverified: ambiguous frontmost surface')
            front_chain = self.attachment_chain(state, front[0], rows)
            if wid not in front_chain:
                # Driver 0.38 can report an ancestor above its focused Office
                # sheet even when fresh CGWindowList reports the sheet first.
                # Accept only an independent exact file-panel focus/frame/order
                # proof; arbitrary popovers and unknown blockers stay rejected.
                if not self.native_file_panel_front(state, active_window):
                    raise ValueError('AttachmentUnverified: another surface blocks the target')
                front, front_chain = [wid], chain
            focus_evidence = 'not_required'
            if keyboard:
                if not observation_fresh:
                    raise ValueError('AttachmentUnverified: fresh focused-control observation is missing; observe again')
                if state.get('attachment_focus_reported'):
                    if (state.get('attachment_focus_count') != 1
                            or state.get('attachment_focused') != {wid}):
                        raise ValueError('AttachmentUnverified: focused-control evidence conflicts with target; input not sent')
                    focus_evidence = 'explicit'
                else:
                    # Missing AX focus is unknown, not a negative report. Let macOS
                    # deliver keys only when this verified surface is itself frontmost.
                    if front[0] != wid:
                        raise ValueError('AttachmentUnverified: keyboard target is not the frontmost surface')
                    focus_evidence = 'unreported_frontmost_surface'
            self.record({'phase': 'attachment_verified', 'window_id': wid, 'chain': chain,
                         'front_chain': front_chain, 'keyboard': keyboard, 'focus_evidence': focus_evidence,
                         'edges': [state['attachments'][child] for child in chain[:-1]]})
            return active_window
        if self.verified_nonordinary_window(state, active_window):
            if state.get('app_active') is not True:
                raise ValueError('Window focus was not verified; input not sent. Observe again.')
            return active_window
        if keyboard and activate:
            # Raising an already-focused document can dismiss its custom editor
            # or font menu. Prove the current app/window afresh before preserving
            # that focus; missing or conflicting evidence keeps the old route.
            self.clear_action_app_inventory()
            rows = self.interaction_windows(state)
            self.verify_frame(state, active_window, rows=rows)
            focused = [row for row in rows if row.get('is_focused') is True]
            if (state.get('app_active') is True and len(focused) == 1
                    and self.frame(focused[0]) == self.frame(active_window)):
                self.record({'phase': 'foreground_focus_preserved',
                             'pid': state['pid'], 'window_id': wid})
                return active_window
        try:
            focus = self.call('bring_to_front', {'pid': state['pid'], 'window_id': active_window['window_id']})
        except ActionError as error:
            focus = error.result
        if data(focus).get('exact_window_effect', {}).get('verified') is not True:
            report = data(focus)
            observed = report.get('observed', {})
            wid = observed.get('focused_window_id')
            # One bounded correction before input; never reinterpret screenshot coordinates.
            if (not allow_correction
                    or report.get('code') != 'bring_to_front_exact_window_unverified'
                    or report.get('pid') != state['pid']
                    or report.get('window_id') != active_window['window_id']
                    or type(wid) is not int or wid == active_window['window_id']
                    or observed.get('front_process_matches_target') is not True
                    or observed.get('frontmost_pid') != state['pid']
                    or observed.get('workspace_frontmost_pid') != state['pid']
                    or observed.get('frontmost_ordinary_window_id') != wid):
                if failed(focus):
                    raise ActionError(focus)
                raise ValueError('Window focus was not verified; input not sent. Observe again.')
            rows = self.interaction_windows(state)
            matches = [w for w in rows if w['window_id'] == wid]
            if len(matches) != 1:
                raise ValueError('WindowChanged: reported focus is not a visible app window; input not sent')
            corrected = matches[0]
            focus = self.call('bring_to_front', {'pid': state['pid'], 'window_id': wid})
            if data(focus).get('exact_window_effect', {}).get('verified') is not True:
                raise ValueError('Window focus correction was not verified; input not sent')
            active_window = corrected
            state.update(window=corrected, focus_hint={
                'window_id': wid,
                'frames': [self.frame(w) for w in sorted(rows, key=lambda w: w['window_id'])]})
            clear_image(state)
        return active_window


    def validate_observation_depth(self, method, max_depth):
        if max_depth is not None:
            if type(max_depth) is not int or max_depth < 1 or method == 'getScreenshot':
                raise ValueError('max_depth requires AX observation and a positive integer')
            props = self.driver.tools.get('get_window_state', {}).get('inputSchema', {}).get('properties', {})
            if 'max_depth' not in props:
                raise ValueError('UnsupportedCapability: driver does not expose max_depth')

    def floating_context(self, state, observation, *, screenshot):
        """Show overlapping app-owned floating windows without retargeting input."""
        state.pop('floating_images', None)
        captured = {}
        current = state.get('window')
        if current is None or current.get('layer', 0) != 0:
            return observation
        rows = self.interaction_windows(state)
        self.verify_frame(state, current, rows=rows)
        base = rectangle(current.get('bounds'))
        if base is None:
            return observation
        floating = []
        for row in rows:
            rect = rectangle(row.get('bounds'))
            if (row['window_id'] != current['window_id'] and row.get('layer', 0) > 0
                    and row.get('is_on_screen') is True and rect is not None
                    and rect[0] < base[0] + base[2] and base[0] < rect[0] + rect[2]
                    and rect[1] < base[1] + base[3] and base[1] < rect[1] + rect[3]):
                floating.append(row)
        if not floating:
            return observation
        context = []
        for row in floating:
            caption = (f"Additional floating window {row['window_id']} "
                       f"{row.get('title', '')!r}, screen bounds {row.get('bounds')}. "
                       "It may contain a menu absent from the main-window image. "
                       "Its pixel coordinates are NOT the main-window coordinates; "
                       "use click with this window_id and delivery_mode=foreground.")
            context.append({'type': 'text', 'text': caption})
            if screenshot:
                # Screenshot-only reads do not replace the model's AX tokens.
                result = self.call('get_window_state', {
                    'pid': state['pid'], 'window_id': row['window_id'],
                    'include_screenshot': True, 'include_accessibility_tree': False,
                    **self.image_limit()})
                raw = data(result)
                if raw.get('pid', state['pid']) != state['pid'] or raw.get('window_id') != row['window_id']:
                    raise ValueError('WindowChanged: floating capture belongs to another window')
                picture = next((c for c in result.get('content', []) if c.get('type') == 'image'), None)
                if picture is not None:
                    context.append(picture)
                    size = (raw.get('screenshot_width'), raw.get('screenshot_height'))
                    if (raw.get('screenshot_frame_valid') is True
                            and rectangle(raw.get('window_bounds')) == rectangle(row.get('bounds'))
                            and all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in size)):
                        captured[row['window_id']] = {'window': dict(row), 'size': size}
                else:
                    context.append({'type': 'text', 'text': 'Floating-window screenshot unavailable.'})
        self.verify_relationship_frames(state, rows)
        state['floating_images'] = captured
        observation['context_content'] = context
        return observation

    def click_floating(self, state, args):
        """Click a separately captured palette without raising its document."""
        wid, point = args.get('windowId'), args.get('target')
        if (type(wid) is not int or not isinstance(point, (list, tuple)) or len(point) != 2
                or not all(type(v) in (int, float) and math.isfinite(v) for v in point)):
            raise ValueError('Floating click requires window_id and a finite [x, y] point')
        if args.get('deliveryMode') != 'foreground':
            raise ValueError('Floating click requires explicit foreground delivery; no input sent')
        capture = state.get('floating_images', {}).get(wid)
        if capture is None:
            raise ValueError('Floating click requires a current verified screenshot of this window')
        window, size = capture['window'], capture['size']
        if not (0 <= point[0] < size[0] and 0 <= point[1] < size[1]):
            raise ValueError('Floating click is outside its screenshot')
        self.clear_action_app_inventory()
        rows = self.interaction_windows(state)
        matches = [row for row in rows if row['window_id'] == wid]
        if (len(matches) != 1 or self.frame(matches[0]) != self.frame(window)
                or matches[0].get('layer', 0) <= 0):
            state.pop('floating_images', None)
            raise ValueError('WindowChanged: floating window moved or disappeared; no input sent')
        # Never activate here: doing so may dismiss the very menu being clicked.
        if state.get('app_active') is not True:
            raise ValueError('Floating app is not foreground; no input sent. Observe again.')
        x0, y0, width, height = rectangle(window['bounds'])
        x, y = x0 + point[0] * width / size[0], y0 + point[1] * height / size[1]
        rank = matches[0].get('z_index')
        if type(rank) is not int:
            raise ValueError('Floating window stacking order is unknown; no input sent')
        for row in rows:
            if row['window_id'] == wid:
                continue
            b = rectangle(row.get('bounds'))
            if b and b[0] <= x < b[0] + b[2] and b[1] <= y < b[1] + b[3]:
                if (type(row.get('z_index')) is not int or type(row.get('layer', 0)) is not int
                        or (row.get('layer', 0), row['z_index']) >= (matches[0]['layer'], rank)):
                    raise ValueError('Floating click is obscured by another app surface; no input sent')
        ratio = self.desktop_ratio()
        params = {'scope': 'desktop', 'x': x * ratio, 'y': y * ratio,
                  'button': 'left', 'count': 1, 'delivery_mode': 'foreground'}
        self.validate('click', params)
        self.invalidate()
        return data(self.call('click', params))

    def observe(self, state, method, disable_diff=False, max_depth=None):
        self.validate_observation_depth(method, max_depth)
        # Reuse app identity only during this observation. Window/frame checks
        # remain live, and call() clears the inventory on any UI mutation.
        previous = self.action_app_inventory
        previous_depth = self.observation_max_depth
        self.observation_max_depth = max_depth
        self.action_app_inventory = previous if previous is not None else {}
        try:
            try:
                result = self._observe(state, method, disable_diff)
                return self.floating_context(state, result, screenshot=True) if method != 'getAXState' else result
            except (ValueError, ActionError) as error:
                changed = (isinstance(error, ActionError)
                           and data(error.result).get('code') == 'window_id_not_found')
                changed = changed or (not isinstance(error, ActionError)
                                      and str(error).startswith('WindowChanged:'))
                if not changed or (self.deadline is not None and time.monotonic() >= self.deadline):
                    raise
                self.invalidate()
                state['fresh'] = False
                clear_image(state)
                self.clear_action_app_inventory()
                self.record({'phase': 'observation_retry', 'reason': str(error),
                             'pid': state['pid'], 'method': method})
                # Retry only the read, once, under the original deadline. A
                # second transition propagates; no input action is replayed.
                result = self._observe(state, method, True)
                return self.floating_context(state, result, screenshot=True) if method != 'getAXState' else result
        finally:
            self.action_app_inventory = previous
            self.observation_max_depth = previous_depth

    def _observe(self, state, method, disable_diff=False):
        previous_attachments = state.get('attachments', {})
        previous_unresolved = state.get('unresolved_windows', set())
        if method != 'getScreenshot':
            state.pop('floating_toolbar_source', None)
            state.update(attachments={}, attachment_ax={}, attachment_focused=set(), unresolved_windows=set())
            self.invalidate()
            state['fresh'] = False
            clear_image(state)
        rows = sorted(self.interaction_windows(state), key=lambda w: w['window_id'])
        try:
            current = self.selected_window(state, rows)
        except ValueError:
            if method == 'getScreenshot':
                raise
            current = None
        if current is not None and method != 'getScreenshot':
            # A previously detected ambiguity is not a usable parent hint.
            # Re-read every competing source before choosing a current route;
            # ordinary observations still avoid unrelated document scans.
            competing = previous_attachments.get(current['window_id'], [])
            if len(competing) > 1:
                self.inspect_window_relationships(
                    state, current, rows,
                    source_ids={edge['source'][0] for edge in competing})
        if method == 'getScreenshot':
            if state.get('floating_toolbar_source') is not None:
                self.verify_frame(state, state['window'], rows=rows)
                clear_image(state)
                return {'state': FLOATING_TOOLBAR_NOTICE, 'image': None}
            if (current is not None and current['window_id'] in state.get('attachments', {})
                    and any(e.get('role') == 'AXSheet' and e.get('label') == 'GoToWindow'
                            for e in state.get('elements', {}).values())):
                return self.observe(state, 'getAXStateAndScreenshot', disable_diff)
            observation = self.observe_window(state, method, current)
            self.verify_frame(state, current)
            return observation
        snapshots = state.get('window_states', {})
        observation_frames = [self.frame(w) for w in rows]
        elements, targets = {}, {}
        image = None
        source = None
        rendered = ''
        updated = {}
        scope_notices = set()
        empty_sources = set()
        parent_capture_needed = False
        parent_capture_hint = (method == 'getAXStateAndScreenshot'
                               and any(e.get('role') == 'AXSheet' and e.get('label') == 'GoToWindow'
                                       for e in state.get('elements', {}).values()))
        if (current is not None and (method == 'getAXState' or parent_capture_hint)
                and current['window_id'] in previous_unresolved
                and state.get('relationship_max_depth') == self.observation_max_depth
                and state.get('relationship_frames') == observation_frames):
            edges = previous_attachments.get(current['window_id'], [])
            parent = (next((row for row in rows if self.frame(row) == edges[0]['source']), None)
                      if len(edges) == 1 else None)
            if parent is not None:
                parent_id = parent['window_id']
                parent_state = dict(snapshots.get(parent_id, {}))
                parent_state.update(pid=state['pid'], bundle_id=state.get('bundle_id'))
                observation = self.observe_window(
                    parent_state, 'getAXState', parent, disable_diff,
                    sheet_bounds=current.get('bounds'), display_window=current)
                if observation.get('scope_notice'):
                    scope_notices.add(observation['scope_notice'])
                if not parent_state['elements']:
                    # This source was just read with the same target bounds and
                    # depth. Reuse negative evidence within this read only;
                    # there are no action tokens here to become stale.
                    empty_sources.add(parent_id)
                    state['attachment_ax'][parent_id] = parent_state.get('raw_elements', [])
                    if parent_state.get('ax_unresolved'):
                        state['unresolved_windows'].add(parent_id)
                role = edges[0]['role'].removeprefix('AX').lower()
                goto_panel = any(e.get('role') == 'AXSheet' and e.get('label') == 'GoToWindow'
                                 for e in parent_state['elements'].values())
                if (parent_state['elements'] and (not parent_capture_hint or goto_panel)
                        and attached_sheet_elements(
                            parent_state.get('raw_elements', []), current.get('bounds'), role)):
                    parent_capture_needed = parent_capture_hint
                    source, rendered = parent, observation['state']
                    updated[parent_id] = parent_state
                    state['unresolved_windows'].add(current['window_id'])
        if current is not None and source is None:
            wid = current['window_id']
            child = dict(snapshots.get(wid, {}))
            child.update(pid=state['pid'], bundle_id=state.get('bundle_id'))
            observation = self.observe_window(
                child, 'getAXStateAndScreenshot' if method == 'getAXStateAndScreenshot' else 'getAXState',
                current, disable_diff, scope=('window', wid))
            if observation.get('scope_notice'):
                scope_notices.add(observation['scope_notice'])
            state['attachment_ax'][wid] = child.get('raw_elements', [])
            if child.get('ax_unresolved'):
                state['unresolved_windows'].add(wid)
            image = observation['image']
            if image:
                image_geometry = {name: child[name] for name in (
                    'image_frame', 'image_size', 'image_bounds', 'image_background_input')
                    if name in child}
            if child['elements']:
                source, rendered, updated[wid] = current, observation['state'], child
            else:
                prior_edges = previous_attachments.get(wid, [])
                known_parent = (prior_edges[0]['source'][0] if len(prior_edges) == 1
                                and state.get('relationship_max_depth') == self.observation_max_depth
                                and state.get('relationship_frames') == observation_frames else None)
                ambiguity_confirmed = len(state.get('attachments', {}).get(wid, [])) > 1
                last_candidate_id = None
                for candidate in sorted((w for w in rows if w['window_id'] != wid),
                                        key=lambda w: (w['window_id'] in snapshots
                                                       and snapshots[w['window_id']].get('window') is not None
                                                       and self.frame(snapshots[w['window_id']]['window']) == self.frame(w),
                                                       w.get('z_index', -1)), reverse=True):
                    candidate_id = candidate['window_id']
                    if candidate_id in empty_sources:
                        continue
                    last_candidate_id = candidate_id
                    candidate_state = dict(snapshots.get(candidate_id, {}))
                    candidate_state.update(pid=state['pid'], bundle_id=state.get('bundle_id'))
                    sheet = self.observe_window(
                        candidate_state, 'getAXState', candidate, disable_diff,
                        sheet_bounds=current.get('bounds'), display_window=current)
                    if sheet.get('scope_notice'):
                        scope_notices.add(sheet['scope_notice'])
                    state['attachment_ax'][candidate_id] = candidate_state.get('raw_elements', [])
                    if candidate_state.get('ax_unresolved'):
                        state['unresolved_windows'].add(candidate_id)
                    if candidate_state['elements'] and source is None:
                        source, rendered = candidate, sheet['state']
                        updated[candidate_id] = candidate_state
                        if candidate_id == known_parent or ambiguity_confirmed:
                            break
                # Cold discovery checks remaining sources for competing edges.
                # Those reads invalidate tokens, so refresh only the chosen
                # source afterwards, never replay its old action tokens.
                if source is not None and last_candidate_id != source['window_id']:
                    chosen = updated[source['window_id']]
                    refreshed = self.observe_window(
                        chosen, 'getAXState', source, True,
                        sheet_bounds=current.get('bounds'), display_window=current)
                    if not chosen['elements']:
                        raise ValueError('WindowChanged: attached surface vanished during discovery; observe again')
                    rendered = refreshed['state']
        if source is not None:
            elements.update(updated[source['window_id']]['elements'])
            targets.update({index: source for index in elements})
        for wid, child_state in updated.items():
            state['attachment_ax'][wid] = child_state.get('raw_elements', [])
        verified_rows = self.verify_relationship_frames(state, rows)
        if current is not None:
            self.verify_frame(state, current, rows=verified_rows)
        self.build_attachments(state, rows)
        surfaces = {}
        for index, element in elements.items():
            owners = state['attachment_tokens'].get(
                (targets[index]['window_id'], element.get('element_token')), set())
            if len(owners) == 1:
                surfaces[index] = next(w for w in rows if w['window_id'] in owners)
        if image:
            clear_image(state)
            state.update(image_geometry)
        state.update(window=current, window_states=updated, elements=elements,
                     element_windows=targets, element_surfaces=surfaces, fresh=True,
                     relationship_frames=observation_frames)
        state['relationship_max_depth'] = self.observation_max_depth
        if (current is not None and source is not None
                and source['window_id'] != current['window_id']
                and any(e.get('role') == 'AXToolbar' and e.get('depth', 0) == 0
                        and rectangle(e.get('frame')) == rectangle(current.get('bounds'))
                        for e in elements.values())):
            # Word Find is a top-level AXToolbar returned in a document's tree,
            # not an AX child of that document. Neither its tokens nor Driver
            # 0.38's scaled-parent capture can safely target the toolbar.
            state['floating_toolbar_source'] = source['window_id']
            state['unresolved_windows'].add(current['window_id'])
            clear_image(state)
            image = None
            rendered += '\n' + FLOATING_TOOLBAR_NOTICE
        if ((image or parent_capture_needed) and source is not None
                and source['window_id'] != current['window_id']
                and any(e.get('role') == 'AXSheet' and e.get('label') == 'GoToWindow'
                        for e in elements.values())):
            # Driver 0.38's unresolved child capture can contain a scaled parent
            # composite despite child bounds in metadata. Keep the proven parent
            # image and its own transform; do not pretend it is a child crop.
            chain = self.attachment_chain(state, current['window_id'], rows)
            if source['window_id'] not in chain:
                raise ValueError('AttachmentUnverified: screenshot source is outside attachment chain')
            geometry = self.verify_panel_geometry(state, current, source)
            parent_state = dict(updated[source['window_id']])
            captured = self.observe_window(parent_state, 'getScreenshot', source)
            exact = (parent_state.get('image_background_input') or {}).get('exact_window', {})
            if (exact.get('pid') != state['pid'] or exact.get('window_id') != source['window_id']
                    or exact.get('status') != 'matched'
                    or parent_state.get('image_bounds') != rectangle(source.get('bounds'))):
                clear_image(state)
                raise ValueError('AttachmentUnverified: parent screenshot geometry is not proven')
            self.verify_relationship_frames(state, rows)
            self.verify_panel_geometry(state, current, source)
            clear_image(state)
            state.update({key: parent_state[key] for key in (
                'image_size', 'image_bounds', 'image_background_input')})
            state.update(image_frame=self.frame(current), image_source_window=self.frame(source))
            image = captured['image']
            rendered += '\nScreenshot includes the parent window; coordinate targets use this full image.'
            if geometry.get('sheet_contained_in_parent') is False:
                rendered += (' The attached sheet extends beyond the parent; its outer edges may be clipped. '
                             'Use AX controls or keyboard for controls outside this image.')
        launch_window_timeout = state.pop('launch_window_timeout', None)
        if not elements:
            if (image and current is not None
                    and current['window_id'] in state.get('unresolved_windows', set())
                    and current['window_id'] not in state.get('attachments', {})
                    and current['window_id'] not in state.get('attachment_conflicts', set())):
                # Driver 0.38's combined unresolved AX/image read is display-only
                # and publishes no pixel context. Its documented screenshot-only
                # read establishes that context without inventing AX controls.
                # Return THIS new image, so later clicks use the displayed frame.
                captured = self.observe_window(state, 'getScreenshot', current)
                if state.get('image_bounds') != rectangle(current.get('bounds')):
                    clear_image(state)
                    scope_notices.add('Screenshot is observation-only: pixel geometry is not proven.')
                self.verify_relationship_frames(state, rows)
                image = captured['image']
            rendered = ((f'Application launched successfully, but no visible window appeared within '
                         f'{launch_window_timeout:g} seconds.')
                        if not rows and launch_window_timeout is not None else
                        'Application is running but has no visible windows.' if not rows else
                        'App AX unavailable: no accessible elements in the current window.')
            if current is not None and current['window_id'] in state.get('unresolved_windows', set()):
                rendered += (f" Window ID: {current['window_id']}, title: {current.get('title', '')!r}. "
                             'Use screenshot coordinates or unaddressed keyboard input with '
                             'delivery_mode=foreground only while this exact window is focused; '
                             'background input remains unavailable.')
            if self.observation_max_depth is not None:
                rendered += f' AX scope: max_depth={self.observation_max_depth}; deeper controls may be omitted.'
            if scope_notices:
                rendered += '\n' + '\n'.join(sorted(scope_notices))
        return {'state': rendered, 'image': image}


    def observe_window(self, state, method, window, disable_diff=False, offset=0,
                       sheet_bounds=None, display_window=None, scope=None):
        previous = state.get('window')
        if previous and self.frame(previous) != self.frame(window):
            clear_image(state)
            state.pop('ax_retry_timeout_ms', None)
        state['window'] = window
        identity = {'pid': state['pid'], 'window_id': window['window_id']}
        tree = method != 'getScreenshot'
        depth_options = ({'max_depth': self.observation_max_depth}
                         if tree and self.observation_max_depth is not None else {})
        properties = self.driver.tools.get('get_window_state', {}).get('inputSchema', {}).get('properties', {})
        retry_budget = ({'timeout_ms': 5000} if tree and previous is not None
                        and state.get('ax_retry_timeout_ms') == 5000 and 'timeout_ms' in properties else {})
        result = self.call('get_window_state', {**identity, 'include_screenshot': method != 'getAXState',
                                                'include_accessibility_tree': tree,
                                                **self.image_limit(), **retry_budget, **depth_options})
        raw = data(result)
        automatic_shallow = False
        if (tree and raw.get('truncated') is True and raw.get('truncation_reason') == 'timeout'
                and 'timeout_ms' in properties and not retry_budget):
            # Driver's documented retry for its default 1s partial AX walk.
            # A missing sheet in a truncated parent tree is not evidence of absence.
            state['ax_retry_timeout_ms'] = 5000
            result = self.call('get_window_state', {
                **identity, 'include_screenshot': method != 'getAXState',
                'include_accessibility_tree': True, 'timeout_ms': 5000, **self.image_limit(), **depth_options})
            raw = data(result)
        if (tree and sheet_bounds is not None and raw.get('truncated') is True
                and raw.get('truncation_reason') == 'timeout' and 'max_depth' in properties
                and not depth_options
                and not attached_sheet_elements(raw.get('elements', []), sheet_bounds)):
            automatic_shallow = True
            # A large file browser can consume the whole depth-first walk before
            # its sibling GoTo sheet is reached. Recover shallow modal controls
            # without walking every file row; retain exact sheet-frame matching.
            result = self.call('get_window_state', {
                **identity, 'include_screenshot': method != 'getAXState',
                'include_accessibility_tree': True, 'timeout_ms': 5000,
                'max_depth': 3, **self.image_limit()})
            raw = data(result)
        if any(key in raw and raw[key] != value for key, value in identity.items()):
            self.invalidate()
            raise ValueError('WindowChanged: driver observation does not belong to the requested app/window')
        image = next((c for c in result.get('content', []) if c.get('type') == 'image'), None)
        if image:
            state['image_frame'] = self.frame(window)
            state['image_size'] = (raw.get('screenshot_width'), raw.get('screenshot_height'))
            bounds = rectangle(raw.get('window_bounds'))
            if raw.get('screenshot_frame_valid') is True and bounds:
                state['image_bounds'] = bounds
                state['image_background_input'] = raw.get('background_input')
            else:
                state.pop('image_bounds', None)
                state.pop('image_background_input', None)
        else:
            clear_image(state)
        if method == 'getScreenshot' and not image:
            raise ValueError('Screenshot unavailable; inspect state before choosing coordinates')
        output = {'image': image}
        if tree:
            # A new driver snapshot invalidates any sibling handle's element tokens.
            self.invalidate()
            raw_elements = compact_rows(with_text_labels(raw.get('elements', []), result))
            menu = active_menu_elements(raw_elements)
            sheet = (attached_sheet_elements(raw_elements, sheet_bounds)
                     if not menu and sheet_bounds is not None else [])
            popover = (attached_sheet_elements(raw_elements, sheet_bounds, 'popover')
                       if not menu and not sheet and sheet_bounds is not None else [])
            nested = []
            if not menu and not sheet and not popover and sheet_bounds is not None:
                for role in ('menu', 'window'):
                    nested = attached_sheet_elements(raw_elements, sheet_bounds, role)
                    if nested:
                        break
            surface = sheet or popover or nested
            visible = menu or (app_elements(raw_elements) if popover else
                               surface if surface or sheet_bounds is not None else app_elements(raw_elements))
            elements = dict(enumerate(visible, start=offset))
            lines = {index: ax_line(index, element) for index, element in elements.items()}
            shown = window if popover else display_window or window
            title = ('Floating toolbar: ' if sheet and sheet[0].get('role') == 'AXToolbar'
                     else 'Attached sheet: ' if sheet else 'Window: ')
            shown_title = (shown.get('title', '') if display_window and not popover else
                           raw.get('window_title', shown.get('title', '')))
            title += json.dumps(shown_title, ensure_ascii=False)
            title += ', App: ' + json.dumps(raw.get('app_name') or state.get('bundle_id') or state['pid'], ensure_ascii=False)
            notices = []
            if automatic_shallow:
                output['scope_notice'] = ('AX scope: max_depth=3 after full traversal timed out; '
                                          'deeper controls may be omitted.')
                notices.append(output['scope_notice'])
            if depth_options:
                notices.append(f'AX scope: max_depth={self.observation_max_depth}; deeper controls may be omitted. '
                               'Omit max_depth for the default traversal.')
            if raw.get('truncated') is True:
                notices.append('AX tree incomplete: ' + str(raw.get('truncation_reason', 'truncated')))
            if raw.get('degraded_reason') and sheet_bounds is None:
                notices.append('AX unavailable: ' + str(raw['degraded_reason']))
            title += ', Window ID: ' + str(shown['window_id'])
            if display_window and popover:
                title += ', Popover window ID: ' + str(display_window['window_id'])
            elif display_window and sheet:
                title += ', AX source window ID: ' + str(window['window_id'])
            root = (('menu', menu[0].get('label')) if menu else
                    ('sheet' if sheet else 'popover', shown['window_id'], self.frame(shown)) if surface else scope)
            context = (title, notices, offset, root)
            same_context = state.get('tree_window') == window['window_id'] and state.get('tree_context') == context
            previous_lines = state.get('lines', {})
            if not disable_diff and same_context and lines == previous_lines:
                rendered = 'There has been no change in the accessibility tree for ' + title
            elif not disable_diff and same_context:
                changes = [('~ ' if k in previous_lines else '+ ') + v for k, v in lines.items()
                           if previous_lines.get(k) != v]
                removed = previous_lines.keys() - lines.keys()
                if removed:
                    changes.append('Removed element IDs: ' + ', '.join(map(str, sorted(removed))))
                rendered = title + '\nAX changes (IDs are positions in the latest snapshot):\n' + '\n'.join(changes)
                if not lines:
                    rendered += '\nNo accessible elements'
            else:
                rendered = title + '\n' + ('\n'.join(lines.values()) or 'No accessible elements')
            if notices:
                rendered += '\n' + '\n'.join(notices)
            # Refresh action tokens even when the rendered snapshot is unchanged.
            state.update(elements=elements, raw_elements=raw.get('elements', []), lines=lines, tree_window=window['window_id'],
                         tree_context=context, fresh=True,
                         ax_unresolved=str(raw.get('degraded_reason', '')).startswith('ax_window_unresolved'))
            output['state'] = rendered
        return output

    @staticmethod
    def point(value):
        if not isinstance(value, list) or len(value) != 2 or any(
                type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in value):
            raise ValueError('Expected [x,y] with finite nonnegative screenshot coordinates')
        return value

    def target(self, state, target):
        if type(target) is int:
            element = state.get('elements', {}).get(target)
            if not state.get('fresh') or not element or not element.get('element_token'):
                raise ValueError('StaleElement: observe again and use an ID from that observation')
            return {'element_token': element['element_token']}
        x, y = self.point(target)
        if state.get('image_frame') != self.frame(state['window']):
            raise ValueError('Coordinates require a current screenshot of this window')
        width, height = state.get('image_size', (None, None))
        if (width is not None and x >= width) or (height is not None and y >= height):
            raise ValueError('Coordinates fall outside the observed screenshot')
        return {'x': x, 'y': y}


    def element_point(self, state, element):
        if state.get('image_frame') != self.frame(state['window']):
            raise ValueError('UnsupportedCapability: indexed pixel fallback requires a current screenshot')
        bounds = state.get('image_bounds')
        frame = rectangle(element.get('frame'))
        size = state.get('image_size')
        if (not bounds or not frame or not isinstance(size, tuple) or len(size) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (*size, bounds[2], bounds[3], frame[2], frame[3]))):
            raise ValueError('UnsupportedCapability: element lacks verified screenshot geometry')
        left, top = max(frame[0], bounds[0]), max(frame[1], bounds[1])
        right = min(frame[0] + frame[2], bounds[0] + bounds[2])
        bottom = min(frame[1] + frame[3], bounds[1] + bounds[3])
        if right <= left or bottom <= top:
            raise ValueError('UnsupportedCapability: element is outside the current screenshot')
        x = ((left + right) / 2 - bounds[0]) * size[0] / bounds[2]
        y = ((top + bottom) / 2 - bounds[1]) * size[1] / bounds[3]
        return {'x': round(x, 2), 'y': round(y, 2)}


    def verify_panel_geometry(self, state, window, parent, *, rows=None):
        report = resolve_panel_geometry(state['pid'], window['window_id'], timeout=self.driver.timeout)
        self.record({'phase': 'panel_geometry', 'report': report})
        ancestry = report.get('ancestry')
        if ancestry is not None:
            # Office Save -> GoTo can sit below a document or Print window.
            # Verify the entire fresh native chain, including the actual capture
            # ancestor; the immediate file panel and AX source need not coincide.
            rows = rows if rows is not None else self.interaction_windows(state)
            chain = self.attachment_chain(state, window['window_id'], rows)
            frames = {row['window_id']: rectangle(row.get('bounds')) for row in rows}
            valid = (isinstance(ancestry, list) and len(ancestry) == len(chain)
                     and all(isinstance(item, dict) and item.get('window_id') == wid
                             and tuple(item.get('frame', [])) == frames.get(wid)
                             for item, wid in zip(ancestry, chain))
                     and parent['window_id'] in chain[1:]
                     and report.get('parent_window_id') in chain[1:]
                     and tuple(report.get('parent_frame', []))
                         == frames.get(report.get('parent_window_id')))
            parent_proven = valid
        else:
            # Older two-surface reports cannot authorize a deeper chain.
            parent_proven = (report.get('parent_window_id') == parent['window_id']
                             and tuple(report.get('parent_frame', [])) == rectangle(parent.get('bounds'))
                             and not state.get('attachments', {}).get(parent['window_id']))
        if (report.get('effect') != 'confirmed' or report.get('read_only') is not True
                or report.get('pid') != state['pid'] or report.get('window_id') != window['window_id']
                or not parent_proven
                or tuple(report.get('sheet_frame', [])) != rectangle(window.get('bounds'))
                ):
            clear_image(state)
            raise ValueError('AttachmentUnverified: native panel geometry is not proven')
        return report

    def pixel_click_route(self, state, window, params, *, foreground=False):
        attached = window['window_id'] in state.get('attachments', {})
        if not attached and not self.verified_nonordinary_window(state, window):
            return ({'pid': state['pid'], 'window_id': window['window_id']},
                    {**params, 'delivery_mode': 'foreground' if foreground else 'background'})
        report = state.get('image_background_input') or {}
        exact = report.get('exact_window') or {}
        bounds = state.get('image_bounds')
        size = state.get('image_size')
        source_frame = state.get('image_source_window')
        expected_image_window = source_frame[0] if source_frame else window['window_id']
        if (exact.get('pid') != state['pid'] or exact.get('window_id') != expected_image_window
                or exact.get('status') not in ({'matched', 'ax_unresolved'} if attached else {'matched'})
                or not bounds or not isinstance(size, tuple) or len(size) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (*size, bounds[2], bounds[3]))):
            raise ValueError('UnsupportedCapability: supplemental window lacks exact screenshot geometry')
        if not foreground:
            # A supplemental window is still an exact window target. Only the
            # foreground fallback needs the legacy desktop-coordinate mapping.
            return ({'pid': state['pid'], 'window_id': window['window_id']},
                    {**params, 'delivery_mode': 'background'})
        screen_x = bounds[0] + params['x'] * bounds[2] / size[0]
        screen_y = bounds[1] + params['y'] * bounds[3] / size[1]
        # Desktop-scope x/y are native desktop-screenshot pixels, which
        # the driver divides by the display backing scale. Pre-multiply so the
        # logical screen point computed above is the point actually clicked.
        ratio = self.desktop_ratio()
        return ({'scope': 'desktop'},
                {**params, 'x': round(screen_x * ratio, 2), 'y': round(screen_y * ratio, 2),
                 'delivery_mode': 'foreground'})

    def verify_attached_point(self, state, window, params, rows, *, desktop=False):
        """Upstream chain/occlusion checks using Box-Agent screenshot pixels."""
        chain = self.attachment_chain(state, window['window_id'], rows)
        bounds, size = state.get('image_bounds'), state.get('image_size')
        source_frame = state.get('image_source_window')
        if source_frame and (source_frame[0] not in chain
                             or not any(self.frame(row) == source_frame for row in rows)):
            raise ValueError('WindowChanged: screenshot source moved; observe again')
        if source_frame:
            self.verify_panel_geometry(state, window, next(row for row in rows if self.frame(row) == source_frame))
        if (state.get('image_frame') != self.frame(window) or not bounds or not size
                or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                       for v in (*size, bounds[2], bounds[3]))):
            raise ValueError('AttachmentUnverified: pixel input requires verified screenshot geometry')
        if desktop:
            ratio = self.desktop_ratio()
            x, y = params['x'] / ratio, params['y'] / ratio
        else:
            x = bounds[0] + params['x'] * bounds[2] / size[0]
            y = bounds[1] + params['y'] * bounds[3] / size[1]
        containing = [row for row in rows if row['window_id'] in chain
                      and (b := rectangle(row.get('bounds')))
                      and b[0] <= x < b[0] + b[2] and b[1] <= y < b[1] + b[3]]
        if not containing or any(type(row.get('z_index')) is not int for row in containing):
            raise ValueError('AttachmentUnverified: pixel target surface is unknown')
        rank = max(row['z_index'] for row in containing)
        for row in rows:
            if row['window_id'] in chain:
                continue
            b = rectangle(row.get('bounds'))
            if type(row.get('z_index')) is not int:
                raise ValueError('AttachmentUnverified: pixel occlusion order is unknown')
            if (row['z_index'] >= rank and b and b[0] <= x < b[0] + b[2]
                    and b[1] <= y < b[1] + b[3]):
                raise ValueError('AttachmentUnverified: pixel target is obscured by another surface')

    def control(self, method, handle=None, args=None):
        # Share only the expensive app enumeration within one input action.
        # Window geometry/order and AX reads remain live. Never reuse this
        # inventory across actions, observations, errors, or activation.
        previous = self.action_app_inventory
        self.action_app_inventory = {} if method in {
            'getApp', 'click', 'drag', 'scroll', 'setValue', 'typeText', 'paste', 'pressKey'} else None
        try:
            return self._control(method, handle, args)
        except BaseException:
            self.panel_focus.close()
            raise
        finally:
            self.action_app_inventory = previous

    def _control(self, method, handle=None, args=None):
        args = args or {}
        if method in {'selectText', 'performSecondaryAction'}:
            raise ValueError(f'UnsupportedCapability: {method} requires a driver extension')
        if not self.started:
            self.call('start_session', {'session': self.session})
            self.started = True
        if method == 'listApps':
            rows = data(self.call('list_apps', {})).get('apps', [])
            return [{'id': a.get('bundle_id') or a.get('launch_path') or a.get('name'),
                     **({'displayName': a['name']} if a.get('name') else {}),
                     **({'isRunning': a['running']} if type(a.get('running')) is bool else {})} for a in rows]
        if method == 'getApp':
            self.validate_observation_depth('getAXState', args.get('maxDepth'))
            app = args.get('app')
            if not isinstance(app, str) or not app.strip():
                raise ValueError('getApp requires a name, bundle ID or absolute .app path')
            installed = self.installed_app(app)
            bundle_id = installed.get('bundle_id')
            if not bundle_id:
                raise ValueError(f'ApplicationNotFound: {app} has no bundle ID')
            launched = installed.get('running') is not True
            if not launched:
                if type(installed.get('pid')) is not int or installed['pid'] <= 0:
                    raise ValueError(f'ApplicationUnavailable: {bundle_id} has no running PID')
                opened = installed
            else:
                # Launch polling must sample fresh process identity each time;
                # only the already-running getApp path shares its first lookup.
                self.action_app_inventory = None
                self.call('launch_app', {'bundle_id': bundle_id})
                opened = self.stable_launched_app(bundle_id)
            state = {'pid': opened['pid'], 'bundle_id': bundle_id, 'fresh': False}
            if launched and not self.wait_for_launch_window(state):
                state['launch_window_timeout'] = APP_LAUNCH_WINDOW_TIMEOUT_SECONDS
            observation = self.observe(state, 'getAXState', max_depth=args.get('maxDepth'))
            handle = uuid.uuid4().hex
            self.handles[handle] = state
            return {'handle': handle, 'state': observation['state']}
        if handle not in self.handles:
            raise ValueError('Invalid app reference; acquire it again with cua.getApp')
        state = self.handles[handle]
        if method == 'clickFloating':
            return self.click_floating(state, args)
        if self.panel_focus.identity is not None and self.panel_focus.identity[0] != state['pid']:
            self.panel_focus.close()
        if method == 'listWindows':
            rows = self.interaction_windows(state)
            if not rows:
                return []
            active = self.selected_window(state, rows)
            return [{'id': w['window_id'], 'title': w.get('title', ''), 'bounds': w.get('bounds'),
                     'layer': w.get('layer', 0), 'isActive': self.frame(w) == self.frame(active),
                     **({'isFocused': w['is_focused']} if type(w.get('is_focused')) is bool else {})}
                    for w in sorted(rows, key=lambda w: w['window_id'])]
        if method in {'getAXState', 'getScreenshot', 'getAXStateAndScreenshot'}:
            return self.observe(state, method, args.get('disableDiffing', False), args.get('maxDepth'))
        active_window = state.get('window')
        target_window = active_window
        targeted_input = method in {'typeText', 'pressKey', 'paste'} and 'target' in args
        if targeted_input and (type(args['target']) is not int or args['target'] < 0):
            raise ValueError('Input target must be a current AX element ID')
        indexed = (method in {'click', 'scroll', 'setValue'} or targeted_input) and type(args.get('target')) is int
        if state.get('floating_toolbar_source') is not None and (
                indexed or method in {'click', 'drag', 'scroll'}):
            raise ValueError(FLOATING_TOOLBAR_NOTICE + ' No input sent.')
        if indexed:
            self.target(state, args['target'])  # Validate freshness before selecting its window.
            target_window = state['element_windows'][args['target']]
            owners = state.get('attachment_tokens', {}).get(
                (target_window['window_id'], state['elements'][args['target']]['element_token']), set())
            if len(owners) > 1:
                raise ValueError('AttachmentUnverified: AX token belongs to multiple surfaces')
        if active_window is None:
            raise ValueError('WindowUnavailable: no current window for keyboard or coordinate input; observe again')
        identity = {'pid': state['pid'], 'window_id': target_window['window_id']}
        name, params = '', {}
        pixel_fallback = False
        if method == 'click':
            opts = args.get('options', {})
            if not isinstance(opts, dict) or opts.keys() - {'mouseButton', 'clickCount', 'action'}:
                raise ValueError('Unsupported click options')
            button = opts.get('mouseButton', 'left')
            button = {'l': 'left', 'r': 'right', 'm': 'middle'}.get(button, button)
            count = opts.get('clickCount', 1)
            if button not in {'left', 'right', 'middle'} or type(count) is not int or count < 1:
                raise ValueError('Invalid mouse button/click count')
            params = self.target(state, args.get('target'))
            action = opts.get('action', 'press')
            ax_actions = {'press': 'AXPress', 'show_menu': 'AXShowMenu', 'pick': 'AXPick',
                          'confirm': 'AXConfirm', 'cancel': 'AXCancel', 'open': 'AXOpen'}
            if action not in ax_actions:
                raise ValueError('Unsupported AX action')
            if action != 'press':
                if not indexed or button != 'left' or count != 1:
                    raise ValueError('AX actions require an element ID and a single left click')
                if ax_actions[action] not in state['elements'][args['target']].get('actions', []):
                    raise ValueError('AX action not advertised by this element; observe again')
                params['action'] = action
            if (action == 'press' and 'element_token' in params and count == 1 and button == 'left'
                    and 'AXPress' not in state['elements'][args['target']].get('actions', [])):
                element = state['elements'][args['target']]
                self.observe(state, 'getScreenshot')
                active_window = state['window']
                target_window = active_window
                identity['window_id'] = active_window['window_id']
                params = self.element_point(state, element)
                pixel_fallback = True
            if 'element_token' in params and count != 1:
                if count != 2 or button != 'left':
                    raise ValueError('UnsupportedCapability: indexed clicks support one click or left double click')
                name = 'double_click'
            else:
                name = 'click'
                params.update(button=button)
                if 'x' in params:
                    params['count'] = count
            if 'x' in params:
                identity, params = self.pixel_click_route(state, target_window, params)
            else:
                params['delivery_mode'] = 'background'
        elif method == 'drag':
            start = self.target(state, args.get('from'))
            end = self.target(state, args.get('to'))
            if 'x' not in end:
                raise ValueError('drag requires two coordinate pairs')
            if 'x' not in start:
                raise ValueError('drag requires two coordinate pairs')
            name = 'drag'
            params = dict(from_x=start['x'], from_y=start['y'], to_x=end['x'], to_y=end['y'], delivery_mode='foreground')
        elif method == 'scroll':
            direction = args.get('direction')
            direction = {'u': 'up', 'd': 'down', 'l': 'left', 'r': 'right'}.get(direction, direction)
            pages = args.get('pages', 1)
            if direction not in {'up', 'down', 'left', 'right'} or type(pages) is not int or not 1 <= pages <= 50:
                raise ValueError('scroll needs a direction and 1–50 integer pages (driver page steps)')
            name = 'scroll'
            params = {**self.target(state, args.get('target')), 'direction': direction, 'by': 'page',
                      'amount': pages, 'delivery_mode': 'background'}
        elif method == 'setValue':
            if type(args.get('target')) is not int or not isinstance(args.get('value'), str):
                raise ValueError('setValue requires a current AX element index and string value')
            name = 'set_value'
            params = {**self.target(state, args['target']), 'value': args['value']}
        elif method == 'typeText':
            if not isinstance(args.get('text'), str):
                raise ValueError('typeText requires a string')
            name, params = 'type_text', {'text': args['text'], 'delivery_mode': 'background'}
        elif method == 'paste':
            opts = args.get('options', {})
            if (not isinstance(args.get('text'), str) or not isinstance(opts, dict)
                    or opts.keys() - {'format'} or opts.get('format', 'text') != 'text'):
                raise ValueError('paste requires a string and optional {format: "text"}; rich text is unsupported')
            self.validate('clipboard_write', {'text': args['text']})
            name, params = 'hotkey', {'keys': ['cmd', 'v'], 'delivery_mode': 'background'}
        elif method == 'pressKey':
            key = args.get('key')
            if not isinstance(key, str) or not key or any(not k for k in key.split('+')):
                raise ValueError('pressKey requires a key string, e.g. super+s')
            aliases = {'super': 'cmd', 'control': 'ctrl', 'alt': 'option', 'enter': 'return',
                       'backspace': 'delete', 'esc': 'escape', 'prior': 'pageup', 'next': 'pagedown'}
            keys = [aliases.get(k.lower(), k.lower()) for k in key.split('+')]
            if any(k not in {'cmd', 'ctrl', 'shift', 'option', 'fn'} for k in keys[:-1]):
                raise ValueError('pressKey requires modifiers followed by one key')
            name = 'hotkey' if len(keys) > 1 else 'press_key'
            params = {**({'keys': keys} if len(keys) > 1 else {'key': keys[0]}),
                      'delivery_mode': 'background'}
        else:
            raise ValueError('Unknown native App method: ' + method)
        if targeted_input:
            params.update(self.target(state, args['target']))
        if 'deliveryMode' in args:
            mode = args['deliveryMode']
            if mode not in {'background', 'foreground'} or method not in {'click', 'drag', 'scroll', 'typeText', 'pressKey', 'paste'}:
                raise ValueError('Invalid delivery mode for this action')
            params['delivery_mode'] = mode
        # An explicitly addressed text editor requests editing keys, not the
        # document's similarly named menu command. Driver hotkey can translate
        # background Cmd+A into Photoshop Select > All before delivering keys.
        # press_key with modifiers preserves the exact field token and routes
        # through Driver's focus/identity checks without menu substitution.
        editing_keys = ['cmd', 'v'] if method == 'paste' else keys if method == 'pressKey' else []
        if (method in {'pressKey', 'paste'} and targeted_input and len(editing_keys) > 1
                and editing_keys[-1] in {'a', 'c', 'x', 'v', 'z'}
                and editing_keys[:-1] in (['cmd'], ['cmd', 'shift'])
                and state['elements'][args['target']].get('role') in {'AXTextField', 'AXTextArea'}
                and target_window['window_id'] == active_window['window_id']
                and state.get('element_surfaces', {}).get(args['target'], target_window)['window_id']
                    == target_window['window_id']):
            name = 'press_key'
            params.pop('keys', None)
            params.update(key=editing_keys[-1], modifiers=editing_keys[:-1])
        panel_confirm_frame = None
        if method == 'pressKey' and targeted_input and keys == ['return']:
            element = state['elements'][args['target']]
            surface = state.get('element_surfaces', {}).get(args['target'], target_window)
            if element.get('role') == 'AXTextField' and surface['window_id'] == active_window['window_id']:
                panel_confirm_frame = rectangle(element.get('frame'))
        # A token belongs to the AX snapshot's source window. Driver 0.38
        # derives keyboard delivery's window from that token, even if another
        # window_id is supplied. Never send parent-scoped keys to an attachment.
        if targeted_input:
            surface = state.get('element_surfaces', {}).get(args['target'])
            if (target_window['window_id'] != active_window['window_id']
                    or (surface is not None and surface['window_id'] != target_window['window_id'])):
                element = state['elements'][args['target']]
                if (method == 'pressKey' and keys == ['return']
                        and params['delivery_mode'] == 'background'
                        and surface is not None
                        and surface['window_id'] == active_window['window_id']
                        and element.get('role') == 'AXTextField'
                        and 'AXConfirm' in element.get('actions', [])):
                    # Confirm the specific field through its retained AX element;
                    # no keyboard focus preparation, activation or token rewriting.
                    name, params = 'click', {
                        **self.target(state, args['target']), 'action': 'confirm',
                        'button': 'left', 'delivery_mode': 'background'}
                else:
                    raise ValueError('AttachmentKeyboardUnsupported: element token belongs to the '
                                     'AX source window, not the active input surface. Observe again '
                                     'and use an advertised semantic action; no input sent.')
        # Validate all arguments before input delivery.
        self.validate(name, {**identity, **params})
        unresolved_foreground = (
            args.get('deliveryMode') == 'foreground' and not indexed
            and method in {'click', 'pressKey', 'typeText', 'paste'}
            and active_window['window_id'] in state.get('unresolved_windows', set())
            and active_window['window_id'] not in state.get('attachments', {})
            and active_window['window_id'] not in state.get('attachment_conflicts', set()))
        refreshed_rows = None
        if method in {'pressKey', 'typeText', 'paste'} and not indexed and not unresolved_foreground:
            refreshed_rows = self.refresh_keyboard_focus(state, active_window)
        def check_windows(rows=None):
            if rows is None:
                rows = self.interaction_windows(state)
            active = self.selected_window(state, rows)
            source = [w for w in rows if w['window_id'] == target_window['window_id']]
            if (self.frame(active) != self.frame(active_window) or len(source) != 1
                    or self.frame(source[0]) != self.frame(target_window)):
                self.invalidate()
                clear_image(state)
                raise ValueError('WindowChanged: active or AX source window changed; observe again')

            if (active_window['window_id'] in state.get('attachments', {})
                    or active_window['window_id'] in state.get('attachment_conflicts', set())):
                self.attachment_chain(state, active_window['window_id'], rows)
                if method == 'drag' or (method == 'scroll' and 'x' in params):
                    raise ValueError('UnsupportedCapability: attached-surface drag/coordinate scroll needs occlusion proof')
                if 'x' in params:
                    self.verify_attached_point(state, active_window, params, rows,
                                               desktop=identity.get('scope') == 'desktop')
            if (active_window['window_id'] in state.get('unresolved_windows', set())
                    and active_window['window_id'] not in state.get('attachments', {})):
                if not unresolved_foreground:
                    raise ValueError('AttachmentUnverified: no proven AX parent; input not sent')
                self.verify_unresolved_foreground(state, active_window, rows)
            if indexed:
                surface = state.get('element_surfaces', {}).get(args['target'], active_window)
                if surface['window_id'] != active_window['window_id']:
                    self.attachment_chain(state, surface['window_id'], rows)
                    if surface['window_id'] not in state.get('attachments', {}):
                        raise ValueError('AttachmentUnverified: indexed target is outside the active surface')

        check_windows(refreshed_rows)
        self.invalidate()
        def foreground_delivery(reason):
            nonlocal active_window, target_window, identity, params
            self.panel_focus.close()
            self.clear_action_app_inventory()
            self.record({'phase': 'foreground_delivery', 'tool': name, 'reason': reason,
                         'pid': state['pid'], 'window_id': target_window['window_id']})
            if unresolved_foreground:
                # Preserve exact PID/window targeting. Driver owns foreground
                # delivery; never turn this into an unbound desktop key/click.
                check_windows()
                return self.call(name, {**identity, **params, 'delivery_mode': 'foreground'})
            # Retain the original adapter's verified focus and desktop-key route.
            focus_target = (state.get('element_surfaces', {}).get(args.get('target'), active_window)
                            if indexed and not pixel_fallback else active_window)
            source_frame = state.get('image_source_window')
            if method == 'click' and 'x' in params and source_frame:
                rows = self.interaction_windows(state)
                parent = next((row for row in rows if self.frame(row) == source_frame), None)
                if parent is None:
                    raise ValueError('WindowChanged: panel capture parent moved')
                self.verify_panel_geometry(state, active_window, parent)
                self.focus_file_panel(state, active_window)
                if not self.native_file_panel_front(state, active_window):
                    raise ValueError('AttachmentUnverified: panel is not foreground or is obscured')
                # Driver can omit this sheet while refreshing parent AX ancestry.
                # Re-prove exact native identity and foreground ownership instead
                # of accepting its unverified result or relaxing the chain check.
                report = self.verify_panel_geometry(state, active_window, parent)
                if report.get('frontmost_pid') != state['pid']:
                    raise ValueError('AttachmentUnverified: panel app is not foreground')
                corrected = active_window
            else:
                corrected = self.focus_window(
                    state, focus_target,
                    not targeted_input and ((indexed and not pixel_fallback)
                                            or method in {'pressKey', 'typeText', 'paste'}),
                    keyboard=method in {'pressKey', 'typeText', 'paste'}, observation_fresh=True, activate=True)
            if corrected['window_id'] != focus_target['window_id']:
                active_window = corrected
                if not indexed:
                    target_window = corrected
                    if 'window_id' in identity:
                        identity['window_id'] = corrected['window_id']
            if method == 'click' and 'x' in params and state.get('image_frame') != self.frame(active_window):
                # Activation may invalidate the earlier capture. Obtain geometry
                # after activation instead of dispatching with a cleared image.
                self.observe(state, 'getScreenshot')
                if self.frame(state['window']) != self.frame(active_window):
                    raise ValueError('WindowChanged: click target changed during activation; observe again')
                if pixel_fallback:
                    params = {**self.element_point(state, element), 'button': button, 'count': count,
                              'delivery_mode': 'background'}
                    identity = {'pid': state['pid'], 'window_id': active_window['window_id']}
            check_windows()
            # Explicit fields keep their snapshot token, PID and source window;
            # Driver resolves/focuses that field and rejects stale identities.
            # Desktop keys are reserved for unaddressed keyboard input.
            if method in {'pressKey', 'paste'} and not targeted_input:
                chord = ['cmd', 'v'] if method == 'paste' else keys
                return self.call('hotkey' if len(chord) > 1 else 'press_key', {
                    'scope': 'desktop', 'delivery_mode': 'foreground',
                    **({'keys': chord} if len(chord) > 1 else {'key': chord[0]})})
            if method == 'click' and 'x' in params:
                click_identity, click_params = self.pixel_click_route(
                    state, target_window, params, foreground=True)
                return self.call(name, {**click_identity, **click_params})
            return self.call(name, {**identity, **params, 'delivery_mode': 'foreground'})

        def deliver():
            if params.get('delivery_mode') == 'foreground':
                return foreground_delivery('explicit_request')
            if method == 'click' and 'x' in params and state.get('image_source_window'):
                if args.get('deliveryMode') == 'background':
                    raise ActionError({'isError': True, 'structuredContent': {
                        'effect': 'not_sent', 'code': 'background_unavailable',
                        'reason': 'panel_coordinate_click_requires_foreground'},
                        'content': [{'type': 'text', 'text': 'Panel coordinate clicks require foreground delivery.'}]})
                return foreground_delivery('panel_coordinate_click_requires_foreground')
            # Remote AppKit panels receive keys in a separate system process.
            # Driver 0.38 still posts these keys to the logical host PID. Resolve
            # the live panel receiver before delivery; never retry an unknown post.
            # Indexed Return is eligible only when the native helper can prove
            # this exact frame is the currently focused GoToWindow PathTextField.
            # Other indexed keys retain the Driver's element contract unchanged.
            if method == 'pressKey' and (not targeted_input or panel_confirm_frame is not None):
                report = deliver_panel_key(
                    state['pid'], active_window['window_id'], keys[-1], keys[:-1],
                    timeout=self.driver.timeout, session=self.panel_focus,
                    field_frame=panel_confirm_frame)
                if report is not None:
                    self.record({'phase': 'panel_keyboard', 'tool': name, **report})
                    if report['effect'] != 'not_sent':
                        return {'structuredContent': report}
                    if (not targeted_input and report.get('code') in {
                            'panel_focus_unavailable', 'panel_lease_unavailable',
                            'native_helper_permission_unavailable', 'native_helper_build_failed',
                            'native_helper_unavailable', 'native_helper_launch_failed'}
                            and self.current_file_panel(state, active_window)):
                        check_windows()
                        self.record({'phase': 'foreground_fallback', 'tool': name,
                                     'reason': report['code'], 'source': 'panel_helper',
                                     'pid': state['pid'], 'window_id': active_window['window_id']})
                        return foreground_delivery(report['code'])
            if method in {'pressKey', 'typeText', 'paste'}:
                self.panel_focus.close()
            # Prefer Driver background delivery before the verified foreground route.
            try:
                return self.call(name, {**identity, **params})
            except ActionError as error:
                report = data(error.result)
                # AX can report these errors after the application has already acted.
                # Preserve uncertainty instead of encouraging a foreground replay.
                ax_messages = [item.get('text', '') for item in error.result.get('content', [])
                               if item.get('type') == 'text']
                if (method == 'click' and 'element_token' in params
                        and any(re.fullmatch(
                            r'AX action failed: AXUIElementPerformAction\(AXPress\) returned -(25204|25205)',
                            message.strip()) for message in ax_messages)):
                    self.record({'phase': 'ax_action_uncertain', 'tool': name,
                                 'pid': state['pid'], 'error': error.result})
                    return {'structuredContent': {'effect': 'unverifiable'}}
                # Only a definite pre-delivery refusal can enter another route.
                # Stale targets, transport failures and partial/unknown effects stop.
                if (params.get('delivery_mode') != 'background'
                        or not (report.get('code') == 'background_unavailable'
                                or (report.get('code') in {'same_pid_keyboard_ambiguity',
                                                          'off_space_or_ax_unresolved'}
                                    and report.get('effect') == 'refused'))
                        or report.get('escalation', {}).get('recommended') not in {None, 'foreground'}
                        or target_window['window_id'] != active_window['window_id']
                        or report.get('effect') not in {None, 'not_sent', 'refused'}):
                    raise
                check_windows()
                self.record({'phase': 'foreground_fallback', 'tool': name,
                             'reason': report.get('code'), 'pid': state['pid'],
                             'active_window_id': active_window['window_id'],
                             'source_window_id': target_window['window_id'],
                             'element_targeted': 'element_token' in params})
                return foreground_delivery(report.get('code'))

        if method == 'paste':
            try:
                # Clipboard contents intentionally remain changed; arbitrary formats cannot be restored.
                written = self.call('clipboard_write', {'text': args['text']})
                if data(written).get('supported') is not True or data(written).get('written_type') != 'text':
                    raise ValueError('Clipboard text write was not confirmed')
                check_windows()
                result = deliver()
            except Exception as error:
                if isinstance(error, ActionError):
                    report = dict(error.result)
                    report['structuredContent'] = {**data(error.result), 'clipboard_may_have_changed': True}
                    report['content'] = [*error.result.get('content', []), {'type': 'text', 'text':
                        'Clipboard may have changed; paste is unconfirmed. Observe before continuing; do not replay.'}]
                    raise ActionError(report) from error
                detail = str(error)
                message = 'Clipboard may have changed; paste is unconfirmed. Observe before continuing; do not replay. ' + detail
                if isinstance(error, (TimeoutError, EOFError, OSError, RuntimeError)):
                    raise type(error)(message) from error
                raise ValueError(message) from error
        else:
            result = deliver()
        report = data(result)
        output = {key: report[key] for key in (
            'effect', 'code', 'path', 'delivery_mode', 'delivered_chars', 'hint', 'reason', 'escalation')
            if key in report}
        messages = [item['text'] for item in result.get('content', [])
                    if item.get('type') == 'text' and item.get('text')]
        if messages:
            output['messages'] = messages
        if method == 'setValue':
            # AXValue readback only proves the control's edit buffer. Photoshop
            # can revert it on Return (campaign cells 54/60); it does not prove
            # that the application committed the font/size/document change.
            output['effect_scope'] = 'control_value'
            output['application_commit'] = 'unverified'
            output['hint'] = (
                'Only the control value was written; application commit is unverified. '
                'Observe the resulting application state before continuing. If the value reverted or '
                'did not apply, click the field and use keyboard editing, then verify again. '
                'Do not assume an untargeted Return commits this field or repeat input blindly.')
        return output

    def image_limit(self):
        props = self.driver.tools.get('get_window_state', {}).get('inputSchema', {}).get('properties', {})
        return {'max_image_dimension': MAX_IMAGE_DIMENSION} if 'max_image_dimension' in props else {}


    def desktop_ratio(self):
        if self.screen_ratio is None:
            screen = data(self.call('get_screen_size', {})) if 'get_screen_size' in self.driver.tools else {}
            scale = screen.get('scale_factor')
            self.screen_ratio = (float(scale) if type(scale) in (int, float)
                                 and math.isfinite(scale) and scale >= 1 else 1.0)
        return self.screen_ratio


    def close(self):
        self.panel_focus.close()
        self.deadline = None
        self.driver.deadline = None
        if self.started and 'end_session' in self.driver.tools:
            self.driver.timeout = 1
            self.call('end_session', {})
        self.handles.clear()
        self.started = False
