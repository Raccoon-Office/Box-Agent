"""Window inventory, identity, and selection for the guest CUA adapter."""
from __future__ import annotations

import ctypes
import json
import math
import sys

from .cua_driver import ActionError, data
from .cua_observation import (ATTACHED_WINDOW_ROLES, active_menu_elements,
    attached_sheet_elements, clear_image, rectangle, top_window, window_stack_key,
    window_stack_sort_key)

CF_NUMBER_SINT64_TYPE = 4
CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY = 1 << 0
CG_WINDOW_LIST_EXCLUDE_DESKTOP_ELEMENTS = 1 << 4
CG_NULL_WINDOW_ID = 0


class _CGPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [('width', ctypes.c_double), ('height', ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [('origin', _CGPoint), ('size', _CGSize)]


def native_window_inventory(pid: int) -> list[dict]:
    """Return same-process WindowServer records, including nonzero layers."""
    if sys.platform != 'darwin':
        return []
    core_foundation = ctypes.CDLL(
        '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    core_graphics = ctypes.CDLL(
        '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    core_graphics.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    core_graphics.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    core_graphics.CGRectMakeWithDictionaryRepresentation.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_CGRect)]
    core_graphics.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
    core_foundation.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    core_foundation.CFArrayGetCount.restype = ctypes.c_long
    core_foundation.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    core_foundation.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    core_foundation.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core_foundation.CFDictionaryGetValue.restype = ctypes.c_void_p
    core_foundation.CFNumberGetValue.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    core_foundation.CFNumberGetValue.restype = ctypes.c_bool
    core_foundation.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    core_foundation.CFBooleanGetValue.restype = ctypes.c_bool
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]

    keys = {name: ctypes.c_void_p.in_dll(core_graphics, name).value for name in (
        'kCGWindowNumber', 'kCGWindowOwnerPID', 'kCGWindowLayer',
        'kCGWindowBounds', 'kCGWindowIsOnscreen')}
    array = core_graphics.CGWindowListCopyWindowInfo(
        CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY | CG_WINDOW_LIST_EXCLUDE_DESKTOP_ELEMENTS,
        CG_NULL_WINDOW_ID)
    if not array:
        raise OSError('CGWindowListCopyWindowInfo returned no inventory')

    def value(record, name):
        return core_foundation.CFDictionaryGetValue(record, keys[name])

    def number(record, name):
        output = ctypes.c_longlong()
        raw = value(record, name)
        return (output.value if raw and core_foundation.CFNumberGetValue(
            raw, CF_NUMBER_SINT64_TYPE, ctypes.byref(output)) else None)

    try:
        total = core_foundation.CFArrayGetCount(array)
        rows = []
        for index in range(total):
            record = core_foundation.CFArrayGetValueAtIndex(array, index)
            owner = number(record, 'kCGWindowOwnerPID')
            window_id = number(record, 'kCGWindowNumber')
            layer = number(record, 'kCGWindowLayer')
            bounds = _CGRect()
            raw_bounds = value(record, 'kCGWindowBounds')
            if (owner != pid or window_id is None or layer is None or not raw_bounds
                    or not core_graphics.CGRectMakeWithDictionaryRepresentation(
                        raw_bounds, ctypes.byref(bounds))):
                continue
            raw_on_screen = value(record, 'kCGWindowIsOnscreen')
            rows.append({
                'pid': owner,
                'window_id': window_id,
                'title': '',
                'bounds': {'x': bounds.origin.x, 'y': bounds.origin.y,
                           'width': bounds.size.width, 'height': bounds.size.height},
                'layer': layer,
                'z_index': total - index,
                'is_on_screen': bool(raw_on_screen and
                                     core_foundation.CFBooleanGetValue(raw_on_screen)),
                'native_inventory': True,
            })
        return rows
    finally:
        core_foundation.CFRelease(array)


class WindowContext:
    """Own window inventory, identity, and current-window selection policy."""

    def __init__(self, driver, call, invalidate, native_inventory=native_window_inventory):
        self.driver = driver
        self.call = call
        self.invalidate = invalidate
        self.native_inventory = native_inventory
        self.action_app_inventory = None

    def clear_action_app_inventory(self):
        if self.action_app_inventory is not None:
            self.action_app_inventory.clear()

    def visible_windows(self, state):
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
        args = {'pid': state['pid']}
        if self.driver.list_windows_on_screen_only:
            args['on_screen_only'] = False
        result = data(self.call('list_windows', args))
        if type(result.get('app_active')) is bool:
            state['app_active'] = result['app_active']
        state['driver_active_window_id'] = result.get('active_window_id')
        state['active_window_id'] = state['driver_active_window_id']
        state['frontmost_window_id'] = result.get('frontmost_window_id')
        candidates = [w for w in result.get('windows', [])
                      if (w.get('pid', state['pid']) == state['pid']
                          or w.get('logical_owner_pid') == state['pid'])]
        offscreen = [w for w in candidates if w.get('is_on_screen') is not True
                     and w.get('on_current_space') is True]
        focused = {}
        state.pop('ax_focused_window_id', None)
        explicitly_focused = [w for w in candidates if w.get('is_focused') is True]
        active = [w for w in candidates if w['window_id'] == state.get('active_window_id')]
        frontmost = [w for w in candidates if w['window_id'] == state.get('frontmost_window_id')]
        same_layer_conflict = (len(active) == len(frontmost) == 1
                               and active[0]['window_id'] != frontmost[0]['window_id']
                               and active[0].get('layer', 0) == frontmost[0].get('layer', 0))
        if len(explicitly_focused) == 1:
            state['ax_focused_window_id'] = explicitly_focused[0]['window_id']
        elif offscreen or same_layer_conflict:
            context = data(self.call('guest_get_window_context', {'pid': state['pid']}))
            focused = context.get('focused_window') or {}
            if focused.get('status') == 'ok' and focused.get('minimized') is False:
                state['ax_focused_window_id'] = focused.get('window_id')
        return [w for w in candidates if w.get('is_on_screen') is True
                or (w in offscreen and focused.get('status') == 'ok'
                    and focused.get('minimized') is False
                    and focused.get('window_id') == w['window_id'])]

    @staticmethod
    def native_window_key(window):
        bounds = window.get('bounds') or {}
        return (window['window_id'], tuple(bounds.get(name) for name in
                                           ('x', 'y', 'width', 'height')))

    def verify_native_window(self, state, window):
        try:
            result = self.call('get_window_state', self.driver.window_state_args({
                'pid': state['pid'], 'window_id': window['window_id'],
                'include_screenshot': False, 'include_accessibility_tree': True}))
        except ActionError:
            return False
        raw = data(result)
        roots = [element for element in raw.get('elements', [])
                 if element.get('depth', 0) == 0 and element.get('role') == 'AXWindow']
        return (raw.get('pid') == state['pid']
                and raw.get('window_id') == window['window_id']
                and len(roots) == 1)

    def interaction_windows(self, state, verify_native=False):
        rows = self.visible_windows(state)
        try:
            native = self.native_inventory(state['pid'])
        except OSError as error:
            state['native_inventory_error'] = str(error)
            self.retain_live_overlays(state, rows)
            return rows
        state.pop('native_inventory_error', None)
        known = {window['window_id'] for window in rows}
        extra = [window for window in native
                 if window.get('pid') == state['pid']
                 and window['window_id'] not in known
                 and window.get('is_on_screen') is True
                 and window.get('bounds', {}).get('width', 0) > 0
                 and window.get('bounds', {}).get('height', 0) > 0]
        cache = state.setdefault('native_window_validation', {})
        live = {self.native_window_key(window) for window in extra}
        for key in cache.keys() - live:
            del cache[key]
        if verify_native:
            for window in sorted(extra, key=window_stack_sort_key, reverse=True):
                key = self.native_window_key(window)
                if cache.get(key) is not True:
                    self.invalidate()
                    state['fresh'] = False
                    cache[key] = self.verify_native_window(state, window)
        rows += [{**window, 'native_verified': cache.get(self.native_window_key(window))}
                 for window in extra if cache.get(self.native_window_key(window)) is not False]
        self.retain_live_overlays(state, rows)
        return rows

    @staticmethod
    def frame(window):
        return (window['window_id'], window.get('bounds'))

    @staticmethod
    def interaction_pid(state, window):
        return (window.get('pid', state['pid'])
                if window.get('logical_owner_pid') == state['pid'] else state['pid'])

    def retain_live_overlays(self, state, rows):
        live_frames = {w['window_id']: self.frame(w) for w in rows}
        state['transient_overlays'] = {
            window_id: frame for window_id, frame in state.get('transient_overlays', {}).items()
            if window_id != state.get('driver_active_window_id')
            and live_frames.get(window_id) == frame}

    def windows(self, state):
        return self.selected_window(state, self.interaction_windows(state, verify_native=True))

    def selected_window(self, state, rows):
        overlays = state.get('transient_overlays', {})
        rows = [w for w in rows if overlays.get(w['window_id']) != self.frame(w)]
        native = [w for w in rows if w.get('native_inventory') is True]
        if native:
            top = top_window(rows)
            if top is None or top.get('native_inventory') is True:
                return self.current_window(rows)
        active_window_id = state.get('active_window_id')
        if type(active_window_id) is int:
            matches = [w for w in rows if w['window_id'] == active_window_id]
            if len(matches) == 1:
                active = matches[0]
                frontmost_window_id = state.get('frontmost_window_id')
                frontmost = [w for w in rows if w['window_id'] == frontmost_window_id]
                if (len(frontmost) == 1 and active.get('is_focused') is not True
                        and frontmost[0].get('layer', 0) > active.get('layer', 0)):
                    state['active_window_id'] = frontmost_window_id
                    return frontmost[0]
                return active
            raise ValueError('WindowChanged: driver active window is not visible; observe again')
        try:
            self.current_window(rows)
        except ValueError as error:
            if str(error).startswith('WindowUnverified:'):
                raise
        context = data(self.call('guest_get_window_context', {'pid': state['pid']}))
        state['native_window_context'] = context
        for attribute in ('focused_window', 'main_window'):
            candidate = context.get(attribute, {})
            if candidate.get('status') != 'ok':
                continue
            matches = [w for w in rows if w['window_id'] == candidate.get('window_id')
                       and w.get('pid', state['pid']) == state['pid']
                       and not (w.get('native_inventory') is True
                                and w.get('native_verified') is not True)]
            if len(matches) == 1:
                return matches[0]
        return self.current_window(rows)

    @staticmethod
    def current_window(rows):
        ranked_verified = [w for w in rows
                           if not (w.get('native_inventory') is True
                                   and w.get('native_verified') is None)
                           and window_stack_key(w) is not None]
        verified_top = max((window_stack_key(w) for w in ranked_verified), default=None)
        unordered_unknown = [w for w in rows if w.get('native_inventory') is True
                             and w.get('native_verified') is None
                             and window_stack_key(w) is None]
        unknown = [w for w in rows if w.get('native_inventory') is True
                   and w.get('native_verified') is None
                   and window_stack_key(w) is not None
                   and (verified_top is None or window_stack_key(w) >= verified_top)]
        if unordered_unknown or unknown:
            raise ValueError('WindowUnverified: a new native surface must be observed before input')
        native = [w for w in rows if w.get('native_inventory') is True]
        if native:
            winner = top_window(rows)
            if winner is not None and winner.get('native_inventory') is True:
                return winner
            if winner is None:
                reason = ('native window order is unknown'
                          if any(window_stack_key(w) is None for w in rows)
                          else 'multiple native windows are frontmost')
                raise ValueError('WindowAmbiguous: ' + reason)
        focused = [w for w in rows if w.get('is_focused') is True]
        if len(focused) == 1:
            return focused[0]
        if len(rows) == 1:
            return rows[0]
        raise ValueError('WindowAmbiguous: no proven current window. Inspect app/window UI; '
                         'cross-process panels cannot be guessed. Candidates: ' + json.dumps(rows))

    def sync_live_window(self, state, current):
        previous = state.get('window')
        if ((current is None and previous is None)
                or (current is not None and previous is not None
                    and self.frame(current) == self.frame(previous))):
            return False
        state.update(window=current, window_states={}, elements={}, element_windows={},
                     element_surfaces={}, application_menu_indices=set(),
                     application_menu_paths={}, menu_sources={},
                     attachments={}, attachment_ax={}, attachment_tokens={},
                     attachment_focused=set(), attachment_conflicts=set(),
                     unresolved_windows=set(), fresh=False)
        state.pop('menu_context', None)
        clear_image(state)
        self.invalidate()
        return True

    def validate_fallback_origin(self, state, current, rows):
        origin = state.get('fallback_origin')
        if not origin:
            return
        if (current is None or state.get('app_active') is not True
                or self.frame(current) != origin['surface']
                or {row['window_id']: self.frame(row) for row in rows} != origin['windows']):
            state.pop('fallback_origin', None)
            return
        parent = rectangle(origin['source'][1])
        child = rectangle(origin['surface'][1])
        if (not parent or not child or not all(math.isfinite(v) for v in (*parent, *child))
                or min(parent[2:]) <= 0 or min(child[2:]) <= 0
                or not (parent[0] <= child[0] and parent[1] <= child[1]
                        and child[0] + child[2] <= parent[0] + parent[2]
                        and child[1] + child[3] <= parent[1] + parent[3])):
            state.pop('fallback_origin', None)

    def attachment_chain(self, state, window_id, rows, path=()):
        if window_id in path or len(path) >= 16:
            raise ValueError('AttachmentUnverified: cyclic or excessively deep window relationship')
        if window_id in state.get('attachment_conflicts', set()):
            raise ValueError('AttachmentUnverified: multiple windows match an AX surface')
        edges = state.get('attachments', {}).get(window_id, [])
        if not edges:
            return [window_id]
        if len(edges) != 1:
            raise ValueError('AttachmentUnverified: multiple AX parents match the window')
        edge = edges[0]
        if edge.get('role') == 'main_window_fallback':
            child = next((row for row in rows if row['window_id'] == window_id), None)
            self.validate_fallback_origin(state, child, rows)
            if not state.get('fallback_origin'):
                raise ValueError('AttachmentUnverified: creating-action context expired; input not sent')
        frames = {row['window_id']: self.frame(row) for row in rows}
        if (frames.get(window_id) != edge['surface']
                or frames.get(edge['source'][0]) != edge['source']):
            raise ValueError('WindowChanged: attachment or ancestor moved/closed; observe again')
        return [window_id] + self.attachment_chain(
            state, edge['source'][0], rows, (*path, window_id))

    @staticmethod
    def attachment_role(state, window_id):
        edges = state.get('attachments', {}).get(window_id, [])
        return edges[0].get('role') if len(edges) == 1 else None

    def observation_window(self, state, current, rows):
        snapshots = state.get('attachment_ax', {})
        wid = current['window_id']
        if any(edge.get('role') == 'main_window_fallback'
               for edge in state.get('attachments', {}).get(wid, [])):
            chain = self.attachment_chain(state, wid, rows)
            return next(row for row in rows if row['window_id'] == chain[-1])
        if (wid not in snapshots or snapshots[wid]
                or wid in state.get('attachments', {})
                or wid in state.get('attachment_conflicts', set())):
            return current
        for elements in snapshots.values():
            if active_menu_elements(elements) or any(
                    element.get('role') in ATTACHED_WINDOW_ROLES
                    and not (element.get('role') == 'AXWindow' and element.get('depth', 0) == 0)
                    and rectangle(element.get('frame')) is not None
                    for element in elements) or any(
                    attached_sheet_elements(
                        elements, current.get('bounds'), role.removeprefix('AX').lower())
                    for role in ATTACHED_WINDOW_ROLES):
                state.pop('fallback_origin', None)
                return current
        candidates = []
        for row in rows:
            if (row['window_id'] == wid or row.get('pid', state['pid']) != state['pid']
                    or row['window_id'] in state.get('attachments', {})):
                continue
            roots = [element for element in snapshots.get(row['window_id'], [])
                     if element.get('role') == 'AXWindow' and element.get('depth', 0) == 0]
            if len(roots) == 1 and (rectangle(roots[0].get('frame')) is None
                                   or rectangle(roots[0].get('frame'))
                                   == rectangle(row.get('bounds'))):
                candidates.append(row)
        origin = state.get('fallback_origin')
        candidates = [row for row in candidates
                      if origin and self.frame(row) == origin['source']]
        if len(candidates) != 1:
            state.pop('fallback_origin', None)
            return current
        main = candidates[0]
        self.driver.record({'phase': 'main_window_fallback', 'window_id': wid,
                            'source_window_id': main['window_id'],
                            'reason': 'action_new_empty_ax'})
        return main

    def build_attachments(self, state, rows):
        windows = {row['window_id']: row for row in rows
                   if row.get('pid', state['pid']) == state['pid']}
        graph, focused, token_owners, conflicts = {}, set(), {}, set()
        focus_count = 0
        focus_reported = False
        focus_sources = []
        for source, elements in state.get('attachment_ax', {}).items():
            if source not in windows:
                continue
            stack = []
            window_depths = [e.get('depth', 0) for e in elements if e.get('role') == 'AXWindow']
            rootless = not window_depths
            unique_root = bool(window_depths) and window_depths.count(min(window_depths)) == 1
            root_depth = min(window_depths) if window_depths else None
            source_bounds = rectangle(windows[source].get('bounds'))
            source_rooted = source_bounds is not None and sum(
                e.get('role') == 'AXWindow' and e.get('depth', 0) == root_depth
                and rectangle(e.get('frame')) == source_bounds for e in elements) == 1
            selected_menu_count = sum(
                e.get('role') == 'AXMenuBarItem' and e.get('selected') is True
                for e in elements)
            for element in elements:
                depth = element.get('depth', 0)
                while stack and stack[-1][0] >= depth:
                    stack.pop()
                owner = stack[-1][1] if stack else None
                parent_role = stack[-1][2] if stack else None
                role = element.get('role')
                bounds = rectangle(element.get('frame'))
                anchor = role == 'AXWindow' and owner is None
                if anchor and ((bounds is None and unique_root)
                               or bounds == rectangle(windows[source].get('bounds'))):
                    owner = source
                if owner is None and role in {'AXSheet', 'AXPopover'} and rootless:
                    owner = source
                if (owner is None and source_rooted and selected_menu_count == 1
                        and role == 'AXMenuBarItem' and element.get('selected') is True
                        and parent_role == 'AXMenuBar'):
                    owner = source
                if role in ATTACHED_WINDOW_ROLES and owner is not None and not anchor:
                    matches = [wid for wid, row in windows.items()
                               if wid != owner and bounds
                               and rectangle(row.get('bounds')) == bounds]
                    if len(matches) == 1:
                        child = matches[0]
                        edge = {'surface': self.frame(windows[child]),
                                'source': self.frame(windows[owner]), 'role': role}
                        edges = graph.setdefault(child, [])
                        if not any(e['source'] == edge['source']
                                   and e['surface'] == edge['surface'] for e in edges):
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
                if role not in ATTACHED_WINDOW_ROLES and type(element.get('focused')) is bool:
                    focus_reported = True
                    if element['focused']:
                        focused.add(owner)
                        focus_sources.append((source, owner))
                        focus_count += 1
                stack.append((depth, owner, role))
        state.update(attachments=graph, attachment_focused=focused,
                     attachment_focus_count=focus_count,
                     attachment_focus_reported=focus_reported,
                     attachment_tokens=token_owners, attachment_conflicts=conflicts)
        try:
            current = self.selected_window(state, list(windows.values()))
        except ValueError:
            state.pop('fallback_origin', None)
            return
        if (current['window_id'] in state.get('attachment_ax', {})
                and not state['attachment_ax'][current['window_id']]):
            state.setdefault('unresolved_windows', set()).add(current['window_id'])
        self.validate_fallback_origin(state, current, rows)
        if (current['window_id'] in graph or current['window_id'] in conflicts
                or current['window_id'] not in state.get('attachment_ax', {})
                or state['attachment_ax'][current['window_id']]):
            state.pop('fallback_origin', None)
        main = self.observation_window(state, current, rows)
        if main['window_id'] != current['window_id']:
            child, parent = current['window_id'], main['window_id']
            graph[child] = [{'surface': self.frame(current), 'source': self.frame(main),
                             'role': 'main_window_fallback'}]
            for element in state.get('attachment_ax', {}).get(parent, []):
                token = element.get('element_token')
                key = (parent, token)
                if token and token_owners.get(key, {parent}) == {parent}:
                    token_owners[key] = {child}
            state['attachment_focused'] = {
                child if source == parent and owner in (None, parent) else owner
                for source, owner in focus_sources}


    def transient_overlay_parent(self, state, current, rows):
        """Return the driver's active window for a proven noninteractive overlay."""
        active_id = state.get('driver_active_window_id')
        if (type(active_id) is not int or active_id == current['window_id']
                or current['window_id'] != state.get('frontmost_window_id')
                or current.get('title') not in (None, '')
                or type(current.get('layer')) is not int):
            return None
        active = next((row for row in rows if row['window_id'] == active_id), None)
        if (active is None or current.get('pid', state['pid']) != active.get('pid', state['pid'])
                or current['layer'] <= active.get('layer', 0)):
            return None
        overlay = rectangle(current.get('bounds'))
        parent = rectangle(active.get('bounds'))
        if (not overlay or not parent or min((*overlay[2:], *parent[2:])) <= 0
                or not (parent[0] <= overlay[0] and parent[1] <= overlay[1]
                        and overlay[0] + overlay[2] <= parent[0] + parent[2]
                        and overlay[1] + overlay[3] <= parent[1] + parent[3])
                or overlay[2] * overlay[3] * 100 > parent[2] * parent[3]):
            return None
        elements = state.get('attachment_ax', {}).get(current['window_id'])
        if (not isinstance(elements, list) or len(elements) != 1
                or elements[0].get('role') != 'AXWindow'
                or elements[0].get('depth', 0) != 0
                or set(elements[0].get('actions', ())) - {'AXRaise', 'raise'}):
            return None
        return active


    def focus_window(self, state, active_window, keyboard=False, observation_fresh=False,
                     foreground_window_id=None):
        try:
            return self._focus_window(
                state, active_window, keyboard, observation_fresh, foreground_window_id)
        except (ValueError, ActionError) as error:
            self.driver.record({'phase': 'attachment_rejected', 'window_id': active_window['window_id'],
                         'reason': str(error)})
            raise

    def _focus_window(self, state, active_window, keyboard=False, observation_fresh=False,
                      foreground_window_id=None):
        wid = active_window['window_id']
        if (wid in state.get('attachments', {}) or wid in state.get('unresolved_windows', set())
                or wid in state.get('attachment_conflicts', set())):
            rows = self.interaction_windows(state)
            chain = self.attachment_chain(state, wid, rows)
            if len(chain) < 2:
                raise ValueError('AttachmentUnverified: no proven AX parent; input not sent')
            foreground_can_activate = (
                foreground_window_id in chain and state.get('active_window_id') == wid)
            if state.get('app_active') is not True and not foreground_can_activate:
                raise ValueError('AttachmentUnverified: owning app is not confirmed foreground')
            front, front_chain = [wid], chain
            if not foreground_can_activate:
                frontmost_id = state.get('frontmost_window_id')
                frontmost = [row for row in rows if row['window_id'] == frontmost_id]
                if len(frontmost) != 1:
                    raise ValueError('AttachmentUnverified: frontmost surface is unknown')
                front = [frontmost_id]
                front_chain = self.attachment_chain(state, frontmost_id, rows)
                if wid not in front_chain:
                    raise ValueError('AttachmentUnverified: another surface blocks the target')
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
                    # deliver keys only when this verified surface is itself frontmost
                    # or an exact foreground action is authorized to activate it.
                    if not foreground_can_activate and front[0] != wid:
                        raise ValueError('AttachmentUnverified: keyboard target is not the frontmost surface')
                    focus_evidence = ('action_foreground' if foreground_can_activate
                                      else 'unreported_frontmost_surface')
            self.driver.record({'phase': 'attachment_verified', 'window_id': wid, 'chain': chain,
                         'front_chain': front_chain, 'keyboard': keyboard, 'focus_evidence': focus_evidence,
                         'activation': ('action_foreground' if foreground_can_activate
                                        else 'already_foreground'),
                         'edges': [state['attachments'][child] for child in chain[:-1]]})
            return active_window
        if (active_window.get('native_inventory') is True
                and active_window.get('native_verified') is True):
            if (state.get('app_active') is not True
                    and foreground_window_id != wid):
                raise ValueError('Window focus was not verified; input not sent. Observe again.')
            return active_window
        return active_window

    @staticmethod
    def finder_editor(state, root, rows, elements):
        """Identify Finder's standalone filename AX field inside its untitled editing window."""
        if root is None or root.get('layer') != 0:
            return None
        fields = [e for e in elements if e.get('depth') == 0 and e.get('role') == 'AXTextField'
                  and e.get('enabled') is True and e.get('value') and e.get('label') == e['value']]
        icons = [e for e in elements if e.get('role') == 'AXImage' and e.get('selected') is True]
        if len(fields) != 1 or len(icons) != 1 or not icons[0].get('label'):
            return None
        field = fields[0]
        surface_id = state.get('frontmost_window_id')
        surfaces = [w for w in rows if w['window_id'] == surface_id and surface_id != root['window_id']
                    and w.get('pid') == state['pid'] and w.get('title') == '' and w.get('layer') == 0
                    and w.get('is_on_screen') is True and w.get('on_current_space') is True
                    and w.get('current_space_id') == root.get('current_space_id')]

        def contains(outer, inner):
            a, b = rectangle(outer), rectangle(inner)
            return (a and b and 0 < b[2] <= a[2] and 0 < b[3] <= a[3]
                    and a[0] <= b[0] <= a[0] + a[2] - b[2] and a[1] <= b[1] <= a[1] + a[3] - b[3])

        surface = surfaces[0] if len(surfaces) == 1 else {}
        icon_box = rectangle(icons[0].get('frame'))
        field_box = rectangle(field.get('frame'))
        aligned = (icon_box and field_box and icon_box[2] > 0 and icon_box[3] > 0
                   and field_box[0] <= icon_box[0] + icon_box[2] / 2 <= field_box[0] + field_box[2]
                   and icon_box[1] <= field_box[1] <= icon_box[1] + 2 * icon_box[3])
        if not (aligned and contains(root.get('bounds'), surface.get('bounds'))
                and contains(surface.get('bounds'), field.get('frame'))):
            surface_id = None
        # An observed but unverified editor must refuse input, never activate the root.
        return {'owner': (root['window_id'], root.get('current_space_id'), icons[0]['label'], icon_box),
                'surface': surface_id, 'value': field['value']}

    def verify_finder_editor(self, state, root, expected):
        identity = {'pid': state['pid'], 'window_id': root['window_id']}
        raw = data(self.call('get_window_state', {**identity, 'include_screenshot': False,
                                                  'include_accessibility_tree': True}))
        context = data(self.call('guest_get_window_context', {'pid': state['pid']}))
        rows = self.verify_frame(state, root)
        observed = self.finder_editor(state, root, rows, raw.get('elements', []))
        main, focus = context.get('main_window', {}), context.get('focused_window', {})
        focus_matches = (focus.get('window_id') in (root['window_id'], expected['surface'])
                         if focus.get('status') == 'ok' else focus.get('ax_error') == -25212)
        if not (expected['surface'] is not None and root.get('pid') == state['pid']
                and root.get('is_on_screen') is True and root.get('on_current_space') is True
                and root.get('current_space_id') is not None and root.get('title')
                and state.get('driver_active_window_id') in (None, root['window_id'], expected['surface'])
                and observed == expected and raw.get('pid') == state['pid']
                and raw.get('window_id') == root['window_id'] and raw.get('window_bounds') == root['bounds']
                and raw.get('truncated') is False and state.get('app_active') is True
                and context.get('trusted') is True and main.get('window_id') == root['window_id']
                and main.get('status') == 'ok' and main.get('minimized') is False
                and focus_matches):
            raise ValueError('WindowChanged: Finder rename editor disappeared or changed; no input sent. Observe again.')

    def verify_frame(self, state, window):
        try:
            rows = self.interaction_windows(state)
            current = self.selected_window(state, rows)
            if self.frame(current) != self.frame(window):
                raise ValueError('WindowChanged: observe the current target again')
            return rows
        except Exception:
            clear_image(state)
            self.invalidate()
            raise

    def inspect_window_relationships(self, state, current, rows):
        """Capture multi-window AX evidence before activation can alter a transient UI."""
        self.invalidate()
        state['fresh'] = False
        clear_image(state)
        sources = []
        state.update(attachment_ax={}, unresolved_windows=set())
        candidates = [current] + sorted(
            (row for row in rows if row['window_id'] != current['window_id']),
            key=window_stack_sort_key, reverse=True)
        for window in candidates:
            identity = {'pid': state['pid'], 'window_id': window['window_id']}
            try:
                snapshot = self.call('get_window_state', self.driver.window_state_args({
                    **identity, 'include_screenshot': False, 'include_accessibility_tree': True}))
            except ActionError:
                # The raw rejection is already in evidence; it proves no relationship.
                continue
            raw = data(snapshot)
            if any(key in raw and raw[key] != value for key, value in identity.items()):
                raise ValueError('WindowChanged: relationship observation belongs to another app/window')
            elements = raw.get('elements', [])
            state['attachment_ax'][window['window_id']] = elements
            if not elements or str(raw.get('degraded_reason', '')).startswith('ax_window_unresolved'):
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
        self.verify_relationship_frames(state, rows)
        parent = self.transient_overlay_parent(state, current, rows)
        if parent is not None:
            state.setdefault('transient_overlays', {})[current['window_id']] = self.frame(current)
            state['active_window_id'] = parent['window_id']
            current = parent
        self.build_attachments(state, rows)
        self.driver.record({'phase': 'window_relationships', 'pid': state['pid'],
                     'current_window_id': current['window_id'], 'windows': rows,
                     'ax_sources': sources, 'attachments': state['attachments'],
                     'conflicts': sorted(state['attachment_conflicts'])})
        # Read-only evidence is not permission to send input; validate the chain per action.
        self.verify_frame(state, current)
        return current

    def verify_relationship_frames(self, state, rows):
        current = self.interaction_windows(state)
        if ({w['window_id']: self.frame(w) for w in rows}
                != {w['window_id']: self.frame(w) for w in current}):
            state.update(attachments={}, attachment_ax={}, attachment_tokens={},
                         attachment_conflicts=set(), fresh=False)
            clear_image(state)
            self.invalidate()
            self.driver.record({'phase': 'attachment_rejected',
                         'reason': 'WindowChanged: window inventory changed during observation'})
            raise ValueError('WindowChanged: window inventory changed during observation; observe again')

    def refresh_keyboard_focus(self, state, window):
        """Refresh input evidence without replacing the model's displayed AX snapshot."""
        self.invalidate()
        clear_image(state)
        try:
            rows = self.verify_frame(state, window)
            previous_chain = self.attachment_chain(state, window['window_id'], rows)
            self.inspect_window_relationships(state, window, rows)
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
            self.driver.record({'phase': 'keyboard_focus_refreshed', 'window_id': window['window_id'],
                         'chain': chain, 'focus_reported': state.get('attachment_focus_reported'),
                         'focus_count': state.get('attachment_focus_count')})
        except Exception as error:
            self.driver.record({'phase': 'keyboard_focus_rejected', 'window_id': window['window_id'],
                         'reason': str(error)})
            raise


    def focused_window_id(self, state):
        context = data(self.call('guest_get_window_context', {'pid': state['pid']}))
        state['native_window_context'] = context
        focused = context.get('focused_window') or {}
        if focused.get('status') != 'ok' or focused.get('minimized') is not False:
            return None
        return focused.get('window_id')

    def popup_focus_rows(self, state, relation, screen_point=None):
        """Revalidate a creation-proven popup and its unchanged focus owner."""
        self.clear_action_app_inventory()
        rows = self.interaction_windows(state)
        inventory = {row['window_id']: self.frame(row) for row in rows}
        if state.get('app_active') is not True or inventory != relation.get('windows'):
            raise ValueError('popup window inventory changed')
        surface_id = relation['surface'][0]
        owner_id = relation['focus_owner'][0]
        surface = next((row for row in rows if self.frame(row) == relation['surface']), None)
        owner = next((row for row in rows if self.frame(row) == relation['focus_owner']), None)
        if (surface is None or owner is None
                or surface.get('pid', state['pid']) != state['pid']
                or owner.get('pid', state['pid']) != state['pid']
                or state.get('frontmost_window_id') != surface_id
                or self.focused_window_id(state) != owner_id):
            raise ValueError('popup surface or focus owner changed')
        if screen_point is not None:
            x, y = screen_point
            containing = []
            for row in rows:
                bounds = rectangle(row.get('bounds'))
                if (bounds and bounds[0] <= x < bounds[0] + bounds[2]
                        and bounds[1] <= y < bounds[1] + bounds[3]):
                    containing.append(row)
            if not containing:
                raise ValueError('popup click point no longer hits a window')
            surface_bounds = rectangle(surface.get('bounds'))
            inside_frontmost_surface = bool(
                surface_bounds and surface_bounds[0] <= x < surface_bounds[0] + surface_bounds[2]
                and surface_bounds[1] <= y < surface_bounds[1] + surface_bounds[3])
            winner = surface if inside_frontmost_surface else top_window(containing)
            if winner is None:
                raise ValueError('popup point occlusion order is unknown')
            if winner['window_id'] != surface_id:
                raise ValueError('popup click point is obscured by another surface')
        return rows, surface, owner

    def refresh_popup_focus_relation(self, state, rows, current):
        relation = state.get('popup_focus_relation')
        if not relation:
            return
        try:
            self.popup_focus_rows(state, relation)
        except (ValueError, ActionError) as error:
            state.pop('popup_focus_relation', None)
            self.driver.record({'phase': 'popup_focus_relation_rejected',
                         'surface_window_id': relation['surface'][0],
                         'focus_owner_window_id': relation['focus_owner'][0],
                         'reason': str(error)})
            return
        relation['status'] = 'active'
        self.driver.record({'phase': 'popup_focus_relation_refreshed',
                     'surface_window_id': relation['surface'][0],
                     'focus_owner_window_id': relation['focus_owner'][0]})
