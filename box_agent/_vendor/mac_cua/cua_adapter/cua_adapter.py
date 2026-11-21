"""Guest CUA adapter entry point with native and managed browser backends."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

_evidence_spec = importlib.util.spec_from_file_location(
    'cua_evidence', Path(__file__).with_name('cua_evidence.py'))
evidence_contract = importlib.util.module_from_spec(_evidence_spec)
_evidence_spec.loader.exec_module(evidence_contract)


from .cua_actions import ActionExecutor
from .cua_driver import ActionError, DriverSession, data
from .cua_mcp import DRIVER_TIMEOUT_SECONDS, PROTOCOL, McpClient
from .cua_observation import (ObservationEngine, active_menu_elements, app_elements,
    application_menu_elements, application_menu_tree, attached_sheet_elements, ax_line,
    clear_image, compact_rows, menu_entries, rectangle, top_window, window_stack_sort_key,
    with_text_labels)
from .cua_service import ReplService as _ReplService, serve as _serve
from .cua_windows import WindowContext, native_window_inventory

APP_LAUNCH_TIMEOUT_SECONDS = 60
APP_LAUNCH_WINDOW_TIMEOUT_SECONDS = 30
APP_LAUNCH_POLL_SECONDS = 0.25
APP_LAUNCH_STABLE_SAMPLES = 2
INSTRUCTIONS = 'UI automation through the persistent cua_repl JavaScript session.'
JS_DESCRIPTION = """Call cua_repl.js and cua_repl.js_reset directly, never through functions.exec.
Use js for GUI actions; functions.exec is for non-CUA tools, not shell/osascript GUI work.
On the first GUI call, and after reset, execute only await cua.getState(),
let TextEdit = await cua.getApp("TextEdit"), or let browser = await cua.getBrowser("chrome").
For an App, use the requested app's name, bundle ID, or path instead of the example.
Read the returned operation manual and initial state before continuing.
Use await cua.rewriteDocumentation() to retrieve the manual again when needed.
JavaScript bindings persist across js calls. Await every CUA operation.
Text and images return directly as tool results; no outer forwarding wrapper is needed.
The timeout covers the whole cell. Reset clears JS bindings, not application changes."""
TOOLS = [
    {'name': 'js', 'description': JS_DESCRIPTION, 'inputSchema': {
        'type': 'object', 'properties': {'code': {'type': 'string'},
        'title': {'type': 'string'}, 'timeout_ms': {'type': 'integer', 'minimum': 1, 'maximum': 120000,
            'default': 60000, 'description': 'Total cell budget, including internal driver calls. '
            'Prefer omitting (60000 ms); GUI actions need at least 30000 ms, combined actions 60000 ms or more.'}},
        'required': ['code'], 'additionalProperties': False}},
    {'name': 'js_reset', 'description': 'Discard all JS bindings and observations, leaving apps and files open. '
     'Next js call must initialize again.', 'inputSchema': {'type': 'object', 'properties': {},
     'additionalProperties': False}},
]


class Adapter:
    """Internal native API. Model IDs are positions in the latest app snapshot."""

    def __init__(self, driver, evidence=None, *, native_command=None):
        self.native_command = native_command or [
            sys.executable, str(Path(__file__).with_name('cua_native.py'))]
        self.driver = DriverSession(
            driver, evidence_contract, evidence, timeout_seconds=DRIVER_TIMEOUT_SECONDS)
        self.handles = {}
        self.window_context = WindowContext(
            self.driver, self.call, self.invalidate,
            native_inventory=lambda pid: native_window_inventory(pid))
        self.observation = ObservationEngine(
            self.driver, self.window_context, self.call, self.invalidate)
        self.actions = ActionExecutor(
            self.driver, self.window_context, self.observation, self.call, self.invalidate,
            lambda: self.handles.values(), action_error=ActionError, result_data=data,
            clear_image=clear_image, menu_entries=menu_entries, rectangle=rectangle,
            top_window=top_window)

    @property
    def session(self):
        return self.driver.session

    @property
    def action_app_inventory(self):
        return self.window_context.action_app_inventory

    @action_app_inventory.setter
    def action_app_inventory(self, value):
        self.window_context.action_app_inventory = value

    @property
    def pending_window_action(self):
        return self.observation.pending_window_action

    @pending_window_action.setter
    def pending_window_action(self, value):
        self.observation.pending_window_action = value

    @property
    def pending_popup_action(self):
        return self.observation.pending_popup_action

    @pending_popup_action.setter
    def pending_popup_action(self, value):
        self.observation.pending_popup_action = value

    def set_context(self, *, evidence=None, deadline=None, cell=None, invocation_id=None):
        self.driver.set_context(
            evidence=evidence, deadline=deadline, cell=cell, invocation_id=invocation_id)

    def counts(self, cell):
        return self.driver.counts(cell)

    def ensure_started(self):
        self.driver.ensure_started()

    def window_state_args(self, args, *, native_resolution=False, attachment_context=False):
        return self.driver.window_state_args(
            args, native_resolution=native_resolution, attachment_context=attachment_context)

    def validate(self, name, args):
        return self.driver.validate(name, args)

    def call(self, name, args, *, recover_session=True):
        if name == 'guest_get_window_context':
            return self.native_window_context(args)
        if name not in {'list_apps', 'list_windows', 'get_window_state',
                        'get_screen_size', 'clipboard_write'}:
            self.clear_action_app_inventory()
        return self.driver.call(name, args, recover_session=recover_session)

    def record(self, value):
        self.driver.record(value)

    def invalidate(self):
        for state in self.handles.values():
            state['fresh'] = False

    def installed_app(self, requested):
        apps = data(self.call('list_apps', {})).get('apps', [])
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
        self.window_context.clear_action_app_inventory()

    def visible_windows(self, state):
        return self.window_context.visible_windows(state)

    @staticmethod
    def native_window_key(window):
        return WindowContext.native_window_key(window)

    def verify_native_window(self, state, window):
        return self.window_context.verify_native_window(state, window)

    def interaction_windows(self, state, verify_native=False):
        return self.window_context.interaction_windows(state, verify_native)

    def retain_live_overlays(self, state, rows):
        self.window_context.retain_live_overlays(state, rows)

    def windows(self, state):
        return self.window_context.windows(state)

    def selected_window(self, state, rows):
        return self.window_context.selected_window(state, rows)

    def transient_overlay_parent(self, state, current, rows):
        return self.window_context.transient_overlay_parent(state, current, rows)
    @staticmethod
    def current_window(rows):
        return WindowContext.current_window(rows)

    @staticmethod
    def frame(window):
        return WindowContext.frame(window)

    @staticmethod
    def interaction_pid(state, window):
        return WindowContext.interaction_pid(state, window)

    def sync_live_window(self, state, current):
        return self.window_context.sync_live_window(state, current)

    def attachment_chain(self, state, window_id, rows, path=()):
        return self.window_context.attachment_chain(state, window_id, rows, path)

    @staticmethod
    def attachment_role(state, window_id):
        return WindowContext.attachment_role(state, window_id)

    def build_attachments(self, state, rows):
        return self.window_context.build_attachments(state, rows)


    def focus_window(self, state, active_window, keyboard=False, observation_fresh=False,
                     foreground_window_id=None):
        return self.window_context.focus_window(
            state, active_window, keyboard, observation_fresh, foreground_window_id)

    def verify_frame(self, state, window):
        return self.window_context.verify_frame(state, window)

    def inspect_window_relationships(self, state, current, rows):
        return self.window_context.inspect_window_relationships(state, current, rows)

    def verify_relationship_frames(self, state, rows):
        return self.window_context.verify_relationship_frames(state, rows)

    def refresh_keyboard_focus(self, state, window):
        return self.window_context.refresh_keyboard_focus(state, window)

    def observe(self, state, method, disable_diff=False):
        return self.observation.observe(state, method, disable_diff)

    def _observe_once(self, state, method, disable_diff=False):
        return self.observation._observe_once(state, method, disable_diff)

    def validate_fallback_origin(self, state, current, rows):
        return self.window_context.validate_fallback_origin(state, current, rows)


    def focused_window_id(self, state):
        return self.window_context.focused_window_id(state)

    def popup_focus_rows(self, state, relation, screen_point=None):
        return self.window_context.popup_focus_rows(state, relation, screen_point)

    def refresh_popup_focus_relation(self, state, rows, current):
        return self.window_context.refresh_popup_focus_relation(state, rows, current)

    def retry_popup_click(self, state, relation, identity, params, screen_point, error):
        return self.actions.retry_popup_click(
            state, relation, identity, params, screen_point, error)

    def observation_window(self, state, current, rows):
        return self.window_context.observation_window(state, current, rows)


    def supplement_application_menu(self, state, rows, current, elements, targets):
        return self.observation.supplement_application_menu(
            state, rows, current, elements, targets)

    def render_menu_context(self, state, rows=None):
        return self.observation.render_menu_context(state, rows)

    def deliver_background(self, name, arguments, send, foreground, *, pid,
                           active_window_id, source_window_id, allow_fallback=True):
        return self.actions.deliver_background(
            name, arguments, send, foreground, pid=pid, active_window_id=active_window_id,
            source_window_id=source_window_id, allow_fallback=allow_fallback)

    def click_application_menu(self, state, method, args):
        return self.actions.click_application_menu(state, method, args)

    def describe_coordinates(self, state, rows, observation):
        return self.observation.describe_coordinates(state, rows, observation)

    def observe_window(self, state, method, window, disable_diff=False, offset=0,
                       sheet_bounds=None, display_window=None, scope=None,
                       native_resolution=False, attachment_chain=None,
                       attachment_root=None, attachment_bounds=None):
        return self.observation.observe_window(
            state, method, window, disable_diff, offset, sheet_bounds, display_window, scope,
            native_resolution, attachment_chain, attachment_root, attachment_bounds)

    @staticmethod
    def point(self, value):
        return self.actions.point(value)

    def target(self, state, target):
        return self.actions.target(state, target)

    def element_point(self, state, element):
        return self.actions.element_point(state, element)

    def screenshot_screen_point(self, state, window, params):
        return self.actions.screenshot_screen_point(state, window, params)

    def pixel_click_route(self, state, window, params, *, screenshot_coordinates=False):
        return self.actions.pixel_click_route(
            state, window, params, screenshot_coordinates=screenshot_coordinates)

    def native_window_context(self, args):
        """Read-only guest capability used to select an app's AX window."""
        if set(args) != {'pid'} or type(args['pid']) is not int or not 0 < args['pid'] < 2**31:
            raise ValueError('guest_get_window_context requires a positive signed 32-bit pid')
        timeout = 5.0
        if self.driver.deadline is not None:
            timeout = min(timeout, self.driver.deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError('JS deadline exceeded')
        entry = {'tool': 'guest_get_window_context', 'arguments': args, 'source': 'guest_native_ax'}
        self.record({'phase': 'dispatch', **entry})
        try:
            process = subprocess.run(
                [*self.native_command, '--pid', str(args['pid'])],
                capture_output=True, text=True, timeout=timeout, check=True)
            context = json.loads(process.stdout)
            if not isinstance(context, dict) or context.get('pid') != args['pid'] or context.get('source') != 'guest_native_ax':
                raise ValueError('Native AX response identity mismatch')
        except subprocess.TimeoutExpired:
            context = {'source': 'guest_native_ax', 'pid': args['pid'], 'status': 'timeout'}
        except (subprocess.CalledProcessError, OSError, ValueError) as error:
            context = {'source': 'guest_native_ax', 'pid': args['pid'],
                       'status': 'helper_failed', 'detail': str(error)}
        result = {'content': [], 'structuredContent': context}
        self.record({'phase': 'result', **entry, 'result': result})
        return result

    def control(self, method, handle=None, args=None):
        # Cache application enumeration only within one action. Geometry and AX
        # stay live; dispatch/activation and every subsequent action invalidate it.
        previous = self.action_app_inventory
        self.action_app_inventory = {} if method in {
            'click', 'drag', 'scroll', 'setValue', 'typeText', 'paste', 'pressKey'} else None
        try:
            return self._control(method, handle, args)
        finally:
            self.action_app_inventory = previous


    def _control(self, method, handle=None, args=None):
        args = args or {}
        if method in {'selectText', 'performSecondaryAction'}:
            raise ValueError(f'UnsupportedCapability: {method} requires a driver extension')
        self.ensure_started()
        if method == 'listApps':
            rows = data(self.call('list_apps', {})).get('apps', [])
            return [{'id': a.get('bundle_id') or a.get('launch_path') or a.get('name'),
                     **({'displayName': a['name']} if a.get('name') else {}),
                     **({'isRunning': a['running']} if type(a.get('running')) is bool else {})} for a in rows]
        if method == 'getApp':
            self.pending_window_action = None
            self.pending_popup_action = None
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
                self.call('launch_app', {'bundle_id': bundle_id, 'ensure_visible': True})
                opened = self.stable_launched_app(bundle_id)
            state = {'pid': opened['pid'], 'bundle_id': bundle_id, 'fresh': False}
            windowless = launched or not self.visible_windows(state)
            if windowless and not launched:
                self.call('launch_app', {'bundle_id': bundle_id, 'ensure_visible': True})
                opened = self.stable_launched_app(bundle_id)
                state['pid'] = opened['pid']
            if windowless and not self.wait_for_launch_window(state):
                state['launch_window_timeout'] = APP_LAUNCH_WINDOW_TIMEOUT_SECONDS
            observation = self.observe(state, 'getAXState')
            handle = uuid.uuid4().hex
            self.handles[handle] = state
            return {'handle': handle, 'state': observation['state']}
        if handle not in self.handles:
            raise ValueError('Invalid app reference; acquire it again with cua.getApp')
        state = self.handles[handle]
        if method == 'listWindows':
            rows = self.interaction_windows(state, verify_native=True)
            if not rows:
                self.sync_live_window(state, None)
                self.pending_window_action = None
                if self.pending_popup_action and self.pending_popup_action[0] is state:
                    self.pending_popup_action = None
                state.pop('fallback_origin', None)
                return []
            active = self.selected_window(state, rows)
            self.sync_live_window(state, active)
            continuing = False
            if self.pending_window_action:
                owner, action = self.pending_window_action
                before = action['windows']
                after = {row['window_id']: self.frame(row) for row in rows}
                added = after.keys() - before.keys()
                origin = state.get('fallback_origin')
                continuing = bool(owner is state and origin and action['source'] == origin['surface']
                                  and active['window_id'] == origin['surface'][0]
                                  and before.keys() == after.keys())
                if (owner is not state or state.get('app_active') is not True
                        or any(after.get(wid) != frame for wid, frame in before.items()
                               if not continuing or wid != active['window_id'])
                        or (added and added != {active['window_id']})
                        or (not added and not continuing and self.frame(active) != action['source'])):
                    self.pending_window_action = None
                    continuing = False
            if not continuing:
                self.validate_fallback_origin(state, active, rows)
            return [{'id': w['window_id'], 'title': w.get('title', ''), 'bounds': w.get('bounds'),
                     'layer': w.get('layer', 0), 'isActive': self.frame(w) == self.frame(active),
                     **({'isFocused': w['is_focused']} if type(w.get('is_focused')) is bool else {})}
                    for w in sorted(rows, key=lambda w: w['window_id'])]
        if method in {'getAXState', 'getScreenshot', 'getAXStateAndScreenshot'}:
            return self.observe(state, method, args.get('disableDiffing', False))
        return self.actions.execute(state, method, args)

    def close(self):
        self.handles.clear()
        self.driver.close()


class ReplService(_ReplService):
    """Compatibility export with adapter-owned dependencies bound explicitly."""

    def __init__(self, driver_factory, node, script, evidence_dir=None, *, browser_config=None,
                 browser_client_factory=None, documentation=None):
        super().__init__(driver_factory, node, script, evidence_dir,
            browser_config=browser_config, browser_client_factory=browser_client_factory,
            documentation=documentation,
            adapter_factory=Adapter, action_error=ActionError, result_data=data,
            evidence_contract=evidence_contract, mcp_client=McpClient,
            driver_timeout_seconds=DRIVER_TIMEOUT_SECONDS)


def serve(service, source=sys.stdin, sink=sys.stdout):
    return _serve(service, source, sink, protocol=PROTOCOL, instructions=INSTRUCTIONS, tools=TOOLS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--driver', required=True)
    parser.add_argument('--node', default='/usr/local/bin/node')
    parser.add_argument('--script', default=str(Path(__file__).with_name('cua_repl.mjs')))
    parser.add_argument('--evidence-dir')
    parser.add_argument('--browser-config')
    args = parser.parse_args()
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    browser_config = json.loads(Path(args.browser_config).read_text()) if args.browser_config else None
    service = ReplService(lambda timeout: McpClient([args.driver, 'mcp'], timeout=timeout),
                          args.node, args.script, args.evidence_dir, browser_config=browser_config)
    try:
        serve(service)
    finally:
        service.close()


if __name__ == '__main__':
    main()
