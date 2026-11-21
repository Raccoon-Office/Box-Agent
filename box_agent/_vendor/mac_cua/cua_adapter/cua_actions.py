"""Model-action compilation and native input execution for the guest CUA adapter."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import math


_AX_OPEN_MAY_HAVE_ACTED = {-25200, -25204, -25205, -25206}


@dataclass(frozen=True)
class ActionPlan:
    """Validated action semantics before live target and window resolution."""

    tool: str
    target: object = None
    destination: object = None
    params: dict = field(default_factory=dict)
    action: str | None = None
    keyboard: bool = False
    clipboard_text: str | None = None
    allow_foreground_fallback: bool = True


def _keyboard_plan(params: dict, driver_tools: dict, **kwargs) -> ActionPlan:
    tool = 'press_key'
    if tool in driver_tools:
        properties = driver_tools[tool].get('inputSchema', {}).get('properties', {})
        if 'modifiers' not in properties:
            params = dict(params)
            modifiers = params.pop('modifiers')
            if modifiers:
                tool = 'hotkey'
                params['keys'] = [*modifiers, params.pop('key')]
    return ActionPlan(tool, params=params, keyboard=True, **kwargs)


def compile_action(method: str, args: dict, driver_tools: dict) -> ActionPlan:
    """Validate public App arguments and select the corresponding driver operation."""
    if method == 'click':
        options = args.get('options', {})
        if not isinstance(options, dict) or options.keys() - {'mouseButton', 'clickCount'}:
            raise ValueError('Unsupported click options')
        button = options.get('mouseButton', 'left')
        button = {'l': 'left', 'r': 'right', 'm': 'middle'}.get(button, button)
        count = options.get('clickCount', 1)
        if button not in {'left', 'right', 'middle'} or type(count) is not int or count < 1:
            raise ValueError('Invalid mouse button/click count')
        return ActionPlan('click', target=args.get('target'),
                          params={'button': button, 'count': count}, action='press')

    if method == 'drag':
        return ActionPlan('drag', target=args.get('from'), destination=args.get('to'),
                          params={'delivery_mode': 'foreground'},
                          allow_foreground_fallback=False)

    if method == 'scroll':
        direction = args.get('direction')
        direction = {'u': 'up', 'd': 'down', 'l': 'left', 'r': 'right'}.get(
            direction, direction)
        pages = args.get('pages', 1)
        if (direction not in {'up', 'down', 'left', 'right'}
                or type(pages) is not int or not 1 <= pages <= 50):
            raise ValueError('scroll needs a direction and 1–50 integer pages (driver page steps)')
        return ActionPlan('scroll', target=args.get('target'),
                          params={'direction': direction, 'by': 'page', 'amount': pages})

    if method == 'setValue':
        if type(args.get('target')) is not int or not isinstance(args.get('value'), str):
            raise ValueError('setValue requires a current AX element index and string value')
        return ActionPlan('set_value', target=args['target'], params={'value': args['value']})

    if method == 'typeText':
        if not isinstance(args.get('text'), str):
            raise ValueError('typeText requires a string')
        return ActionPlan('type_text', params={
            'text': args['text'], 'delivery_mode': 'foreground'}, keyboard=True)

    if method == 'paste':
        options = args.get('options', {})
        if (not isinstance(args.get('text'), str) or not isinstance(options, dict)
                or options.keys() - {'format'} or options.get('format', 'text') != 'text'):
            raise ValueError(
                'paste requires a string and optional {format: "text"}; rich text is unsupported')
        return _keyboard_plan({
            'key': 'v', 'modifiers': ['cmd'], 'delivery_mode': 'foreground'}, driver_tools,
            clipboard_text=args['text'])

    if method == 'pressKey':
        key = args.get('key')
        if not isinstance(key, str) or not key or any(not part for part in key.split('+')):
            raise ValueError('pressKey requires a key string, e.g. super+s')
        aliases = {'super': 'cmd', 'control': 'ctrl', 'alt': 'option', 'enter': 'return',
                   'backspace': 'delete', 'esc': 'escape', 'prior': 'pageup',
                   'next': 'pagedown'}
        keys = [aliases.get(part.lower(), part.lower()) for part in key.split('+')]
        if any(part not in {'cmd', 'ctrl', 'shift', 'option', 'fn'} for part in keys[:-1]):
            raise ValueError('pressKey requires modifiers followed by one key')
        return _keyboard_plan({
            'key': keys[-1], 'modifiers': keys[:-1], 'delivery_mode': 'foreground'},
            driver_tools)

    raise ValueError('Unknown native App method: ' + method)


class ActionExecutor:
    """Resolve live targets, verify delivery, and dispatch native input."""

    def __init__(self, driver, windows, observation, call, invalidate, states, *,
                 action_error, result_data, clear_image, menu_entries, rectangle, top_window):
        self.driver = driver
        self.windows = windows
        self.observation = observation
        self.call = call
        self.invalidate = invalidate
        self.states = states
        self.action_error = action_error
        self.result_data = result_data
        self.clear_image = clear_image
        self.menu_entries = menu_entries
        self.rectangle = rectangle
        self.top_window = top_window

    def uncertain_ax_open(self, error):
        """Describe AXOpen errors that AppKit may return after acting."""
        detail = '\n'.join(
            item.get('text', '') for item in error.result.get('content', [])
            if item.get('type') == 'text')
        code = next((code for code in _AX_OPEN_MAY_HAVE_ACTED
                     if f'AXOpen returned {code}' in detail), None)
        if code is None:
            return None
        return {
            'effect': 'unverifiable',
            'ax_error': code,
            'warning': (
                f'Warning: AXOpen returned {code}, but this macOS/AppKit error may occur '
                'after the action has already taken effect. Take a fresh getAXState() or '
                'getAXStateAndScreenshot() observation before deciding whether to retry; '
                'do not replay the click without observing.'),
        }

    def retry_popup_click(self, state, relation, identity, params, screen_point, error):
        report = self.result_data(error.result)
        surface_id = relation['surface'][0]
        owner_id = relation['focus_owner'][0]
        if (not self.driver.click_activation_owner
                or report.get('code') != 'foreground_unavailable'
                or report.get('reason') != 'exact_window_focus_failed'
                or report.get('effect') != 'not_sent'
                or report.get('target_window_id') != surface_id
                or report.get('focused_window_id') != owner_id
                or identity.get('window_id') != surface_id):
            raise error
        try:
            _, _, owner = self.windows.popup_focus_rows(state, relation, screen_point)
        except (ValueError, self.action_error) as rejected:
            state.pop('popup_focus_relation', None)
            self.driver.record({'phase': 'popup_focus_fallback_rejected',
                         'surface_window_id': surface_id,
                         'focus_owner_window_id': owner_id,
                         'reason': str(rejected)})
            raise error
        arguments = {**identity, **params, 'activation_window_id': owner_id,
                     'delivery_mode': 'foreground'}
        self.driver.validate('click', arguments)
        self.driver.record({'phase': 'popup_focus_fallback',
                     'surface_window_id': surface_id,
                     'focus_owner_window_id': owner['window_id'],
                     'screen_x': round(screen_point[0], 2),
                     'screen_y': round(screen_point[1], 2),
                     'button': params.get('button'), 'count': params.get('count')})
        result = self.call('click', arguments)
        self.driver.record({'phase': 'popup_focus_fallback_delivered',
                     'surface_window_id': surface_id,
                     'focus_owner_window_id': owner_id})
        return result


    def deliver_background(self, name, arguments, send, foreground, *, pid,
                           active_window_id, source_window_id, allow_fallback=True):
        """Escalate once, only after a driver refusal proves no input was sent."""
        try:
            return send()
        except self.action_error as error:
            report = self.result_data(error.result)
            if (not allow_fallback or arguments.get('delivery_mode') != 'background'
                    or report.get('code') not in {'background_unavailable',
                        'same_pid_keyboard_ambiguity', 'off_space_or_ax_unresolved'}
                    or report.get('effect') not in {'not_sent', 'refused'}):
                raise
            self.driver.record({'phase': 'foreground_fallback', 'tool': name,
                         'reason': report['code'], 'pid': pid,
                         'active_window_id': active_window_id,
                         'source_window_id': source_window_id})
            return foreground()

    def click_application_menu(self, state, method, args):
        """Navigate menu context locally; only leaf commands reach the driver."""
        element = state['elements'][args['target']]
        opts = args.get('options', {})
        if (method != 'click' or not isinstance(opts, dict)
                or opts.keys() - {'mouseButton', 'clickCount'}
                or opts.get('mouseButton', 'left') not in {'left', 'l'}
                or type(opts.get('clickCount', 1)) is not int or opts.get('clickCount', 1) != 1
                or element.get('role') not in {'AXMenuBarItem', 'AXMenuItem'}
                or element.get('enabled') is False):
            raise ValueError('UnsupportedCapability: application menus require an enabled single left click')
        application_only = (state.get('window') is None
                            and args['target'] in state.get('application_menu_paths', {}))
        if application_only:
            if state.get('fresh') is not True:
                raise ValueError('StaleElement: observe the application menu again before clicking')
            pid = state['pid']
            rows = self.windows.interaction_windows(state)
            if rows or state.get('app_active') is not True or state.get('window') is not None:
                state.pop('menu_context', None)
                state['fresh'] = False
                self.invalidate()
                raise ValueError('WindowChanged: application-menu surface changed; observe again')
            entries = self.menu_entries(state.get('menu_sources', {}).get('application', []))
            path = state['application_menu_paths'][args['target']]
            addressed = [(raw, candidate) for raw, candidate in entries if candidate == path]
            if len(addressed) != 1:
                raise ValueError('StaleElement: application-menu path changed; observe again')
            raw = addressed[0][0]
            actions = raw.get('actions', [])
            has_children = any(len(candidate) > len(path) and candidate[:len(path)] == path
                               for _, candidate in entries)
            submenu = (raw.get('role') == 'AXMenuBarItem' or has_children
                       or 'AXShowMenu' in actions)
            if submenu:
                state['menu_context'] = {'source': 'application', 'path': path,
                                         'application_only': True}
                self.observation.render_menu_context(state)
                state['fresh'] = False
                self.invalidate()
                return {}
            if not any(action in actions for action in ('AXPick', 'AXPress', 'AXConfirm')):
                raise ValueError(
                    'UnsupportedCapability: menu command has no usable native action')
            labels = [str(part[1] or part[2] or '') for part in path]
            state.pop('menu_context', None)
            state['window_states'] = {}
            state['fresh'] = False
            self.invalidate()
            result = self.call('invoke_application_menu', {'pid': pid, 'path': labels})
            effect = self.result_data(result).get('effect')
            return {'effect': effect} if effect and effect != 'confirmed' else {}
        params = self.target(state, args['target'])
        source = state['element_windows'][args['target']]
        pid = state['pid']
        current_frame = copy.deepcopy(self.windows.frame(state['window']))
        source_frame = copy.deepcopy(self.windows.frame(source))

        def check_menu_windows():
            if state['pid'] != pid:
                raise ValueError('ApplicationChanged: menu application changed; observe again')
            rows = self.windows.interaction_windows(state)
            current = self.windows.selected_window(state, rows)
            matches = [w for w in rows if w['window_id'] == source['window_id']
                       and w.get('pid', pid) == pid]
            if (self.windows.frame(current) != current_frame
                    or self.windows.frame(state['window']) != current_frame or len(matches) != 1
                    or self.windows.frame(matches[0]) != source_frame):
                state.pop('menu_context', None)
                state['fresh'] = False
                self.invalidate()
                raise ValueError('WindowChanged: application menu context changed; observe again')

        check_menu_windows()
        entries = self.menu_entries(state.get('menu_sources', {}).get(source['window_id'], []))
        addressed = [(e, p) for e, p in entries if e.get('element_token') == params['element_token']]
        if len(addressed) != 1:
            raise ValueError('StaleElement: menu node not in its source snapshot; observe again')
        raw, path = addressed[0]
        if sum(p == path for _, p in entries) != 1:
            raise ValueError('UnsupportedCapability: ambiguous menu path; no menu action sent')
        actions = raw.get('actions', [])
        has_children = any(len(p) > len(path) and p[:len(path)] == path for _, p in entries)
        has_container = type(raw.get('element_index')) is int and any(
                            e.get('role') == 'AXMenu' and e.get('parent_index') == raw['element_index']
                            for e in state['menu_sources'][source['window_id']])
        submenu = (raw.get('role') == 'AXMenuBarItem' or has_children or has_container
                   or 'AXShowMenu' in actions)
        if submenu:
            state['menu_context'] = {'source': source, 'path': path,
                                     'current_frame': current_frame}
            self.observation.render_menu_context(state)  # Refuse empty/lazy menus without a physical click.
            state['fresh'] = False
            self.invalidate()
            return {}
        action = 'pick' if 'AXPick' in actions else 'press' if 'AXPress' in actions else None
        if action is None:
            raise ValueError('UnsupportedCapability: menu command does not advertise AXPick or AXPress')
        state.pop('menu_context', None)
        state['window_states'] = {}
        state['fresh'] = False
        self.invalidate()
        arguments = {'pid': pid, 'window_id': source['window_id'], **params,
                     'action': action, 'button': 'left', 'delivery_mode': 'background'}

        def foreground_delivery():
            self.windows.clear_action_app_inventory()
            check_menu_windows()
            self.windows.focus_window(state, source, foreground_window_id=source['window_id'])
            check_menu_windows()
            # Inventory reads preserve the original AX snapshot. Do not reread
            # get_window_state or substitute another window, token or AX action.
            return self.call('click', {**arguments, 'delivery_mode': 'foreground'})

        result = self.deliver_background(
            'click', arguments, lambda: self.call('click', arguments), foreground_delivery,
            pid=pid, active_window_id=current_frame[0], source_window_id=source['window_id'])
        effect = self.result_data(result).get('effect')
        return {'effect': effect} if effect and effect != 'confirmed' else {}


    def point(self, value):
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
        if state.get('image_frame') != self.windows.frame(state['window']):
            raise ValueError('Coordinates require a current screenshot of this window')
        width, height = state.get('image_size', (None, None))
        if state['window']['window_id'] in state.get('attachments', {}):
            reference = state.get('coordinate_frame')
            if not reference:
                raise ValueError('AttachmentUnverified: root coordinate frame unavailable; observe again')
            _, _, width, height = self.rectangle(reference[1])
            if x >= width or y >= height:
                raise ValueError('Coordinates fall outside the observed root window')
            return {'x': x, 'y': y}
        if (width is not None and x >= width) or (height is not None and y >= height):
            raise ValueError('Coordinates fall outside the observed screenshot')
        return {'x': x, 'y': y}

    def element_point(self, state, element):
        if state.get('image_frame') != self.windows.frame(state['window']):
            raise ValueError('UnsupportedCapability: indexed pixel fallback requires a current screenshot')
        bounds = state.get('image_bounds')
        frame = self.rectangle(element.get('frame'))
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

    def screenshot_screen_point(self, state, window, params):
        bounds = state.get('image_bounds')
        size = state.get('image_size')
        if (state.get('image_frame') != self.windows.frame(window) or not bounds
                or not isinstance(size, tuple) or len(size) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (*size, bounds[2], bounds[3]))):
            raise ValueError('popup fallback requires exact screenshot geometry')
        return (
            bounds[0] + params['x'] * bounds[2] / size[0],
            bounds[1] + params['y'] * bounds[3] / size[1],
        )

    def pixel_click_route(self, state, window, params, *, screenshot_coordinates=False):
        attached = window['window_id'] in state.get('attachments', {})
        driver_chain = state.get('image_attachment_chain')
        contextual = isinstance(driver_chain, list) and len(driver_chain) > 1
        if not attached and not contextual and not (window.get('native_inventory') is True
                and window.get('native_verified') is True):
            return ({'pid': self.windows.interaction_pid(state, window),
                     'window_id': window['window_id']},
                    {**params, 'delivery_mode': 'foreground'})
        report = state.get('image_background_input') or {}
        exact = report.get('exact_window') or {}
        bounds = state.get('image_bounds')
        size = state.get('image_size')
        capture = state.get('image_window', self.windows.frame(window))
        if (capture != self.windows.frame(window)
                and (not attached or capture != state.get('coordinate_frame'))):
            raise ValueError('AttachmentUnverified: screenshot source is outside the root coordinate frame')
        if contextual and not attached:
            rows = {row['window_id']: row for row in self.windows.interaction_windows(state)}
            root = rows.get(driver_chain[-1])
            if (driver_chain[0] != window['window_id'] or len(rows.keys() & driver_chain) != len(driver_chain)
                    or not root or self.rectangle(root.get('bounds')) != bounds):
                raise ValueError(
                    'WindowChanged: driver attachment screenshot root moved or closed; observe again')
        if (exact.get('pid') != state['pid'] or exact.get('window_id') != capture[0]
                or exact.get('status') not in (
                    {'matched', 'ax_unresolved'} if attached or contextual else {'matched'})
                or not bounds or not isinstance(size, tuple) or len(size) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (*size, bounds[2], bounds[3]))):
            raise ValueError('UnsupportedCapability: supplemental window lacks exact screenshot geometry')
        screen_x = bounds[0] + params['x'] * bounds[2] / size[0]
        screen_y = bounds[1] + params['y'] * bounds[3] / size[1]
        if attached and not screenshot_coordinates:
            reference = state.get('coordinate_frame')
            if not reference:
                raise ValueError('AttachmentUnverified: root coordinate frame unavailable; observe again')
            root_bounds = self.rectangle(reference[1])
            screen_x, screen_y = root_bounds[0] + params['x'], root_bounds[1] + params['y']
        return ({'scope': 'desktop'},
                {**params, 'x': round(screen_x, 2), 'y': round(screen_y, 2),
                 'delivery_mode': 'foreground'})


    def dispatch_menu_input(self, state, menu, tool, params, *, screenshot_coordinates=False):
        """Keep the observed menu as the pixel surface and its live AX owner as key target."""
        wid = menu['window_id']
        self.windows.clear_action_app_inventory()
        rows = self.windows.verify_frame(state, menu)
        chain = self.windows.attachment_chain(state, wid, rows)
        if tool == 'click':
            bounds = self.rectangle(menu.get('bounds'))
            capture = state.get('image_capture_id')
            exact = (state.get('image_background_input') or {}).get('exact_window') or {}
            if (not bounds or state.get('coordinate_frame') != self.windows.frame(menu)
                    or state.get('image_frame') != self.windows.frame(menu)
                    or state.get('image_window') != self.windows.frame(menu)
                    or state.get('image_bounds') != bounds or state.get('image_attachment_chain') != chain
                    or not isinstance(capture, str) or not capture or exact.get('pid') != state['pid']
                    or exact.get('window_id') != wid or exact.get('status') not in {'matched', 'ax_unresolved'}):
                raise ValueError('WindowChanged: take a fresh screenshot of the attached menu before clicking')
            width, height = state['image_size']
            if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                   for v in (width, height, *bounds[2:])):
                raise ValueError('AttachmentUnverified: invalid menu screenshot geometry')
            x, y = params['x'], params['y']
            if not screenshot_coordinates:
                x, y = x * width / bounds[2], y * height / bounds[3]
            if not (0 <= x < width and 0 <= y < height):
                raise ValueError('AttachmentUnverified: click is outside the menu screenshot')
            params = {**params, 'x': x, 'y': y, 'capture_id': capture, 'delivery_mode': 'foreground'}
        else:
            self.windows.refresh_keyboard_focus(state, menu)

        self.windows.clear_action_app_inventory()
        rows = self.windows.verify_frame(state, menu)
        owner = self.windows.focused_window_id(state)
        context = state.get('native_window_context') or {}
        if (self.windows.attachment_role(state, wid) != 'AXMenu' or len(chain) < 2
                or self.windows.attachment_chain(state, wid, rows) != chain
                or state.get('app_active') is not True or state.get('frontmost_window_id') != wid
                or any(row.get('pid') != state['pid'] for row in rows if row['window_id'] in chain)
                or context.get('trusted') is not True or context.get('pid') != state['pid']
                or owner not in chain or state.get('driver_active_window_id') != owner):
            raise ValueError('WindowChanged: attached menu or its keyboard focus owner changed; observe again')
        arguments = {'pid': state['pid'], 'window_id': owner, **params}
        if tool == 'click':
            arguments.update(window_id=wid, activation_window_id=owner)
        self.driver.validate(tool, arguments)
        self.invalidate()
        self.clear_image(state)
        result = self.call(tool, arguments)
        effect = self.result_data(result).get('effect')
        return {'effect': effect} if effect and effect != 'confirmed' else {}

    def activate_keyboard_target(self, state, window):
        """Reconcile a z-order-only activation refusal with fresh semantic focus."""
        wid = window['window_id']
        try:
            return self.result_data(self.call('bring_to_front', {
                'pid': state['pid'], 'window_id': wid}))
        except self.action_error as error:
            report = self.result_data(error.result)
            exact = report.get('exact_window_effect') or {}
            if not (report.get('code') == 'bring_to_front_exact_window_unverified'
                    and report.get('status') == 'partial'
                    and report.get('pid') == state['pid']
                    and report.get('window_id') == wid
                    and report.get('process_activated') is True
                    and exact.get('focused') is True
                    and exact.get('front_in_process_on_display') is False):
                raise
            # Activation has not dispatched the requested key. Re-observe once;
            # reuse the existing semantic-focus gate, never replay activation/input.
            self.windows.clear_action_app_inventory()
            self.windows.refresh_keyboard_focus(state, window)
            if not (state.get('app_active') is True
                    and state.get('active_window_id') == wid
                    and state.get('ax_focused_window_id') == wid):
                raise
            self.driver.record({'phase': 'keyboard_activation_reconciled',
                                'window_id': wid, 'activation_report': report})
            return {'activated': True}

    def execute(self, state, method, args):
        # Any intervening action attempt invalidates an unobserved creation context.
        self.observation.pending_window_action = None
        self.observation.pending_popup_action = None
        for other in self.states():
            if other is not state:
                other.pop('fallback_origin', None)
        active_window = state.get('window')
        target_window = active_window
        popup_relation = state.get('popup_focus_relation')
        popup_screen_point = None
        indexed = method in {'click', 'scroll', 'setValue'} and type(args.get('target')) is int
        if state.get('menu_context') and method == 'pressKey' and args.get('key', '').lower() in {'esc', 'escape'}:
            state.pop('menu_context', None)
            state['window_states'] = {}
            state['fresh'] = False
            self.invalidate()
            return {}
        if indexed and (args['target'] in state.get('application_menu_indices', set())
                        or state.get('elements', {}).get(args['target'], {}).get('role') == 'AXMenuBarItem'):
            return self.click_application_menu(state, method, args)
        if state.pop('menu_context', None):
            state['window_states'] = {}
        if indexed:
            self.target(state, args['target'])  # Validate freshness before selecting its window.
            target_window = state['element_windows'][args['target']]
            owners = state.get('attachment_tokens', {}).get(
                (target_window['window_id'], state['elements'][args['target']]['element_token']), set())
            if len(owners) > 1:
                self.driver.record({'phase': 'attachment_rejected', 'window_id': target_window['window_id'],
                             'reason': 'ambiguous AX token surface ownership'})
                raise ValueError('AttachmentUnverified: AX token belongs to multiple surfaces')
        if active_window is None:
            raise ValueError('WindowAmbiguous: observe a current window before keyboard or coordinate input')
        identity = {'pid': self.windows.interaction_pid(state, target_window),
                    'window_id': target_window['window_id']}
        plan = compile_action(method, args, self.driver.tools)
        name, params = plan.tool, dict(plan.params)
        menu_surface = self.windows.attachment_role(state, active_window['window_id']) == 'AXMenu'
        if (menu_surface and method == 'pressKey' and not params.get('modifiers')
                and params.get('key') in {'up', 'down', 'left', 'right', 'home', 'end', 'return', 'escape'}):
            return self.dispatch_menu_input(state, active_window, name, params)
        pixel_fallback = False
        if method == 'click':
            button, count = params.pop('button'), params.pop('count')
            params = self.target(state, plan.target)
            action = plan.action
            element = state['elements'].get(args.get('target')) if indexed else None
            needs_pixel = (element is not None and
                           ((count == 1 and 'AXPress' not in element.get('actions', []))
                            or (count == 2 and 'AXOpen' not in element.get('actions', []))))
            if action == 'press' and button == 'left' and 'element_token' in params and needs_pixel:
                # Share screenshot geometry for single/double pointer clicks.
                # AXOpen remains a semantic double-click route; without it the
                # driver would otherwise use the unclipped whole-element center.
                element = state['elements'][args['target']]
                if (active_window['window_id'] in state.get('attachments', {})
                        and state.get('app_active') is not True):
                    expected = self.windows.frame(active_window)
                    activated = self.call('bring_to_front', {'pid': state['pid']})
                    if self.result_data(activated).get('activated') is not True:
                        raise ValueError(
                            'AttachmentUnverified: owning app could not be foregrounded; input not sent')
                    self.observation.observe(state, 'getScreenshot')
                    active_window = state.get('window')
                    if (state.get('app_active') is not True or active_window is None
                            or self.windows.frame(active_window) != expected):
                        self.clear_image(state)
                        raise ValueError(
                            'WindowChanged: attached surface changed while foregrounding; observe again')
                    self.driver.record({'phase': 'pixel_fallback_foregrounded',
                                 'window_id': active_window['window_id']})
                # Reuse this observation's screenshot; frame checks before/after focus
                # still reject movement or a changed active surface before input.
                if (state.get('image_frame') != self.windows.frame(active_window)
                        or not state.get('image_bounds') or not state.get('image_size')):
                    self.observation.observe(state, 'getScreenshot')
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
                if menu_surface:
                    return self.dispatch_menu_input(state, active_window, name, params,
                                                    screenshot_coordinates=pixel_fallback)
                if (popup_relation and popup_relation.get('status') == 'active'
                        and self.windows.frame(target_window) == popup_relation.get('surface')):
                    popup_screen_point = self.screenshot_screen_point(state, target_window, params)
                    popup_relation['status'] = 'suspended'
                identity, params = self.pixel_click_route(
                    state, target_window, params, screenshot_coordinates=pixel_fallback)
            else:
                params['delivery_mode'] = 'background'
        elif method == 'drag':
            start = self.target(state, plan.target)
            end = self.target(state, plan.destination)
            if 'x' not in end:
                raise ValueError('drag requires two coordinate pairs')
            if 'x' not in start:
                raise ValueError('drag requires two coordinate pairs')
            params.update(from_x=start['x'], from_y=start['y'],
                          to_x=end['x'], to_y=end['y'])
        elif method == 'scroll':
            params = {**self.target(state, plan.target), **params}
            params['delivery_mode'] = 'foreground' if 'x' in params else 'background'
        elif method == 'setValue':
            params = {**self.target(state, plan.target), **params}
        elif method == 'typeText':
            if active_window['window_id'] in state.get('attachments', {}):
                # AX-backed typing belongs to the source root, not the CG-only surface.
                identity['window_id'] = self.windows.attachment_chain(
                    state, active_window['window_id'], self.windows.interaction_windows(state))[-1]
        elif method == 'paste':
            self.driver.validate('clipboard_write', {'text': plan.clipboard_text})
        if popup_relation and popup_screen_point is None:
            state.pop('popup_focus_relation', None)
            self.driver.record({'phase': 'popup_focus_relation_rejected',
                         'surface_window_id': popup_relation['surface'][0],
                         'focus_owner_window_id': popup_relation['focus_owner'][0],
                         'reason': 'intervening non-popup input'})
        # All argument failures precede input delivery; indexed pixel fallback may take a screenshot.
        self.driver.validate(name, {**identity, **params})
        keyboard = plan.keyboard
        rename_input = method == 'typeText' or (method == 'pressKey' and params.get('key') == 'return'
                                                and not params.get('modifiers'))
        if state.get('bundle_id') == 'com.apple.finder':
            editor = state.get('finder_editor')
            pending = state.get('pending_finder_editor')
            if rename_input and (editor or pending):
                # The last AX read must describe the same editing session after typing.
                if pending and (not editor or pending != (editor['owner'], editor['surface'])):
                    state.pop('pending_finder_editor', None)
                    raise ValueError('WindowChanged: Finder rename editor changed; no input sent. Observe again.')
                if not pending and editor['value'] != editor['owner'][2]:
                    raise ValueError('TargetUnverified: Finder filename does not match the selected item; no input sent.')
                self.invalidate()
                self.windows.verify_finder_editor(state, active_window, editor)
                result = self.call(name, {**identity, **params})
                state['finder_editor'] = None
                if method == 'typeText':
                    state['pending_finder_editor'] = (editor['owner'], editor['surface'])
                else:
                    state.pop('pending_finder_editor', None)
                return {'effect': self.result_data(result).get('effect')}
            state.pop('finder_editor', None)
            state.pop('pending_finder_editor', None)
        if keyboard:
            wid = active_window['window_id']
            attached = (wid in state.get('attachments', {})
                        or wid in state.get('unresolved_windows', set())
                        or wid in state.get('attachment_conflicts', set()))
            verified_native = (active_window.get('native_inventory') is True
                               and active_window.get('native_verified') is True)
            verified_frontmost_nonordinary = (
                type(active_window.get('layer')) is int
                and active_window['layer'] != 0
                and state.get('app_active') is True
                and state.get('active_window_id') == wid
                and state.get('frontmost_window_id') == wid)
            verified_active_focus = (state.get('app_active') is True
                                     and state.get('active_window_id') == wid
                                     and state.get('ax_focused_window_id') == wid)
            if (not attached and not verified_native and not verified_frontmost_nonordinary
                    and not verified_active_focus):
                # TODO(cua-background): remove this foreground activation once exact-window
                # keyboard delivery is reliable end to end in background mode.
                self.windows.verify_frame(state, active_window)
                focused = self.activate_keyboard_target(state, active_window)
                if (focused.get('activated') is not True
                        and focused.get('exact_window_effect', {}).get('verified') is not True):
                    raise ValueError('Window focus was not verified; input not sent')
                self.windows.clear_action_app_inventory()
            self.windows.refresh_keyboard_focus(state, active_window)
        observation_fresh = keyboard or state.get('fresh') is True
        foreground_window_id = (identity.get('window_id')
                                if params.get('delivery_mode') == 'foreground' else None)
        def check_windows():
            rows = self.windows.interaction_windows(state)
            active = self.windows.selected_window(state, rows)
            source = [w for w in rows if w['window_id'] == target_window['window_id']]
            if (self.windows.frame(active) != self.windows.frame(active_window) or len(source) != 1
                    or self.windows.frame(source[0]) != self.windows.frame(target_window)):
                self.invalidate()
                self.clear_image(state)
                raise ValueError('WindowChanged: active or AX source window changed; observe again')
            if active_window['window_id'] in state.get('attachments', {}):
                chain = self.windows.attachment_chain(state, active_window['window_id'], rows)
                if method == 'drag' or (method == 'scroll' and 'x' in params):
                    raise ValueError('UnsupportedCapability: attached-surface drag/coordinate scroll needs occlusion proof')
                if 'x' in params:
                    if identity.get('scope') != 'desktop':
                        raise ValueError('AttachmentUnverified: pixel input requires verified desktop geometry')
                    if not pixel_fallback:
                        root = next(row for row in rows if row['window_id'] == chain[-1])
                        if self.windows.frame(root) != state.get('coordinate_frame'):
                            raise ValueError('WindowChanged: root coordinate frame changed; observe again')
                    # Outside the popover, a sibling above the root can cover the click
                    # even when that sibling is below the frontmost attached surface.
                    containing = [row for row in rows if row['window_id'] in chain
                                  and (b := self.rectangle(row.get('bounds')))
                                  and b[0] <= params['x'] < b[0] + b[2]
                                  and b[1] <= params['y'] < b[1] + b[3]]
                    if not containing:
                        raise ValueError('AttachmentUnverified: pixel target surface is unknown')
                    frontmost_id = state.get('frontmost_window_id')
                    if not any(row['window_id'] == frontmost_id for row in containing):
                        point_windows = [row for row in rows
                                         if (b := self.rectangle(row.get('bounds')))
                                         and b[0] <= params['x'] < b[0] + b[2]
                                         and b[1] <= params['y'] < b[1] + b[3]]
                        winner = self.top_window(point_windows)
                        if winner is None:
                            raise ValueError(
                                'AttachmentUnverified: pixel occlusion order is unknown')
                        if winner['window_id'] not in chain:
                            raise ValueError(
                                'AttachmentUnverified: pixel target is obscured by another surface')
            return rows

        check_windows()
        self.invalidate()
        # The sheet can be active while its elements come from the document's AX tree.
        # Validate the active surface; retain the original AX source/token.
        # Only foreground paths need to activate this surface.
        focus_target = (state.get('element_surfaces', {}).get(args.get('target'), active_window)
                        if indexed and not pixel_fallback else active_window)
        if focus_target['window_id'] != active_window['window_id']:
            # A background control is not authorized merely because a child is frontmost.
            if focus_target['window_id'] not in state.get('attachments', {}):
                raise ValueError('AttachmentUnverified: indexed target is outside the active surface')
        if method == 'setValue' or params.get('delivery_mode') == 'background':
            # Targeted background input does not require desktop activation. Keep
            # attachment identity checks without activating or raising windows.
            wid = focus_target['window_id']
            if (wid in state.get('attachments', {})
                    or wid in state.get('unresolved_windows', set())
                    or wid in state.get('attachment_conflicts', set())):
                chain = self.windows.attachment_chain(state, wid, self.windows.interaction_windows(state))
                if len(chain) < 2:
                    raise ValueError('AttachmentUnverified: no proven AX parent; input not sent')
        else:
            corrected = self.windows.focus_window(
                state, focus_target, keyboard=keyboard, observation_fresh=observation_fresh,
                foreground_window_id=foreground_window_id)
            if corrected['window_id'] != focus_target['window_id']:
                active_window = corrected
                if not indexed:
                    target_window = corrected
                    if method == 'typeText':
                        identity['window_id'] = corrected['window_id']
        def dispatch_action(mode=None):
            rows = check_windows()
            context = {'source': copy.deepcopy(self.windows.frame(active_window)),
                       'windows': {row['window_id']: copy.deepcopy(self.windows.frame(row)) for row in rows}}
            arguments = {**identity, **params}
            if mode is not None:
                arguments['delivery_mode'] = mode
            result = self.call(name, arguments)
            report = self.result_data(result)
            if report.get('effect') == 'confirmed':
                self.observation.pending_window_action = (state, context)
            delivery = report.get('delivery')
            foreground_global_input = (
                report.get('route') == 'global_input'
                and isinstance(delivery, dict)
                and delivery.get('mode') == 'foreground')
            if (method == 'click' and 'x' in params and type(identity.get('window_id')) is int
                    and (report.get('effect') == 'confirmed'
                         or report.get('path') == 'cgevent_fg'
                         or foreground_global_input)):
                self.observation.pending_popup_action = (state, context)
                self.driver.record({'phase': 'popup_focus_candidate',
                             'source_window_id': context['source'][0],
                             'delivery_path': report.get('path'),
                             'delivery_route': report.get('route'),
                             'delivery_mode': delivery.get('mode') if isinstance(delivery, dict) else None,
                             'effect': report.get('effect')})
            return result

        def foreground_delivery():
            self.windows.clear_action_app_inventory()
            check_windows()
            self.windows.focus_window(
                state, focus_target, keyboard=keyboard, observation_fresh=observation_fresh,
                foreground_window_id=identity.get('window_id'))
            # Retain the exact window target. The driver owns foreground
            # activation; never substitute a desktop-wide keyboard event.
            return dispatch_action('foreground')

        def deliver():
            return self.deliver_background(
                name, params, dispatch_action, foreground_delivery, pid=state['pid'],
                active_window_id=active_window['window_id'],
                source_window_id=target_window['window_id'],
                allow_fallback=plan.allow_foreground_fallback)

        if method == 'paste':
            try:
                # Clipboard contents intentionally remain changed; arbitrary formats cannot be restored.
                written = self.call('clipboard_write', {'text': plan.clipboard_text})
                if self.result_data(written).get('supported') is not True or self.result_data(written).get('written_type') != 'text':
                    raise ValueError('Clipboard text write was not confirmed')
                check_windows()
                result = deliver()
            except Exception as error:
                if isinstance(error, self.action_error):
                    report = dict(error.result)
                    report['structuredContent'] = {**self.result_data(error.result), 'clipboard_may_have_changed': True}
                    report['content'] = [*error.result.get('content', []), {'type': 'text', 'text':
                        'Clipboard may have changed; paste is unconfirmed. Observe before continuing; do not replay.'}]
                    raise self.action_error(report) from error
                detail = str(error)
                message = 'Clipboard may have changed; paste is unconfirmed. Observe before continuing; do not replay. ' + detail
                if isinstance(error, (TimeoutError, EOFError, OSError, RuntimeError)):
                    raise type(error)(message) from error
                raise ValueError(message) from error
        else:
            try:
                result = deliver()
            except self.action_error as error:
                uncertain = self.uncertain_ax_open(error) if name == 'double_click' else None
                if uncertain is not None:
                    return uncertain
                if method != 'click' or popup_relation is None or popup_screen_point is None:
                    raise
                result = self.retry_popup_click(
                    state, popup_relation, identity, params, popup_screen_point, error)
        effect = self.result_data(result).get('effect')
        if effect and effect != 'confirmed':
            return {'effect': effect}
        return {}
