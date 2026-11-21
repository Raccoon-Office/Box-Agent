"""Native observation state machine and shaping helpers for the guest CUA adapter."""
from __future__ import annotations

import copy
import json
import math
import re

from .cua_driver import ActionError, data

ATTACHED_WINDOW_ROLES = {'AXSheet', 'AXPopover', 'AXMenu', 'AXWindow'}


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
    compact = []
    position = 0
    while position < len(elements):
        row = elements[position]
        depth = row.get('depth', 0)
        end = position + 1
        while end < len(elements) and elements[end].get('depth', 0) > depth:
            end += 1
        children = [item for item in elements[position + 1:end]
                    if item.get('depth', 0) == depth + 1]
        if (row.get('role') == 'AXRow' and not row.get('label') and len(children) == 1
                and children[0].get('role') == 'AXCell' and children[0].get('element_token')):
            for original in elements[position + 1:end]:
                item = {**original, 'depth': original.get('depth', 0) - 1}
                if original is children[0]:
                    item.update({flag: True for flag in ('selected', 'focused')
                                 if row.get(flag) is True})
                compact.append(item)
            position = end
            continue
        compact.append(row)
        position += 1
    return compact


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


def window_stack_key(window):
    """Order comparable on-screen surfaces by level, then within-level rank."""
    layer, z_index = window.get('layer'), window.get('z_index')
    return (layer, z_index) if type(layer) is int and type(z_index) is int else None


def window_stack_sort_key(window):
    return window_stack_key(window) or (-math.inf, -math.inf)


def top_window(windows):
    """Return one proven top surface, or None when ordering is unknown/ambiguous."""
    ranked = [(window_stack_key(window), window) for window in windows]
    if not ranked or any(key is None for key, _ in ranked):
        return None
    top = max(key for key, _ in ranked)
    winners = [window for key, window in ranked if key == top]
    return winners[0] if len(winners) == 1 else None


def clear_image(state):
    for name in ('image_frame', 'image_window', 'image_size', 'image_bounds',
                 'image_background_input', 'image_capture_id', 'image_attachment_chain',
                 'image_attachment_root_window_id', 'coordinate_frame'):
        state.pop(name, None)


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


def application_menu_tree(elements: list[dict]) -> list[dict]:
    """Keep the complete app-menu tree, including currently hidden descendants."""
    roots = [i for i, e in enumerate(elements) if e.get('role') == 'AXMenuBar']
    if len(roots) != 1:
        return []
    start = roots[0]
    depth = elements[start].get('depth', 0)
    end = start + 1
    while end < len(elements) and elements[end].get('depth', 0) > depth:
        end += 1
    result = []
    skip_depth = None
    for element in elements[start:end]:
        if skip_depth is not None:
            if element.get('depth', 0) > skip_depth:
                continue
            skip_depth = None
        if element.get('role') == 'AXMenuBarItem' and element.get('label') in {'Apple', '\uf8ff'}:
            skip_depth = element.get('depth', 0)
            continue
        result.append(element)
    return result


def application_menu_elements(elements: list[dict]) -> list[dict]:
    visible = app_elements(application_menu_tree(elements))
    return active_menu_elements(visible) or visible


def menu_entries(elements: list[dict]) -> list[tuple[dict, tuple]]:
    """Give actionable menu nodes a semantic path; AXMenu containers are transparent."""
    entries, parents = [], []
    for element in application_menu_tree(elements):
        if element.get('role') not in {'AXMenuBarItem', 'AXMenuItem'}:
            continue
        depth = element.get('depth', 0)
        while parents and parents[-1][0] >= depth:
            parents.pop()
        key = (element.get('role'), element.get('label'), element.get('identifier'))
        path = tuple(p[1] for p in parents) + (key,)
        entries.append((element, path))
        parents.append((depth, key))
    return entries


def attached_sheet_elements(elements: list[dict], bounds: dict | None = None,
                            role: str = 'sheet') -> list[dict]:
    """Return a sheet/popover subtree, optionally requiring matching bounds."""
    target = rectangle(bounds) if bounds is not None else None
    if bounds is not None and target is None:
        return []
    for start, element in enumerate(elements):
        if str(element.get('role', '')).removeprefix('AX').lower() != role:
            continue
        frame = rectangle(element.get('frame'))
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


class ObservationEngine:
    """Own native observation state, snapshots, and model-visible rendering."""

    def __init__(self, driver, windows, call, invalidate):
        self.driver = driver
        self.windows = windows
        self.call = call
        self.invalidate = invalidate
        self.pending_window_action = None
        self.pending_popup_action = None

    def observe(self, state, method, disable_diff=False):
        # Window transitions can race a read. Retry only the observation, never input.
        for attempt in range(3):
            try:
                return self._observe_once(state, method, disable_diff)
            except ValueError as error:
                if not str(error).startswith('WindowChanged:'):
                    raise
                state['fresh'] = False
                clear_image(state)
                self.invalidate()
                if attempt == 2:
                    raise
                disable_diff = True

    def _observe_once(self, state, method, disable_diff=False):
        pending, self.pending_window_action = self.pending_window_action, None
        popup_pending = self.pending_popup_action
        if popup_pending is not None and popup_pending[0] is state:
            self.pending_popup_action = None
        else:
            popup_pending = None
        state.update(attachments={}, attachment_ax={}, attachment_focused=set(),
                     transient_overlays={},
                     unresolved_windows=set(state.get('unresolved_windows', ())) if method == 'getScreenshot' else set())
        if method != 'getScreenshot':
            self.invalidate()
            state['fresh'] = False
            clear_image(state)
        rows = sorted(self.windows.interaction_windows(state, verify_native=True), key=lambda w: w['window_id'])
        try:
            current = self.windows.selected_window(state, rows)
        except ValueError:
            if method == 'getScreenshot':
                raise
            current = None
        self.windows.sync_live_window(state, current)
        if pending is not None:
            owner, action = pending
            before = dict(action['windows'])
            after = {row['window_id']: self.windows.frame(row) for row in rows}
            added = after.keys() - before.keys()
            previous = state.get('fallback_origin')
            if (owner is state and previous and current is not None
                    and action['source'] == previous['surface'] and before == previous['windows']
                    and current['window_id'] == previous['surface'][0] and before.keys() == after.keys()
                    and all(after[wid] == frame for wid, frame in before.items()
                            if wid != current['window_id'])):
                # Typing can resize its own editor. Re-observe it without changing
                # the creating parent; all other frames must remain unchanged.
                state['fallback_origin'] = {**previous, 'surface': copy.deepcopy(self.windows.frame(current)),
                                            'windows': copy.deepcopy(after)}
            else:
                state.pop('fallback_origin', None)
            if (owner is state and current is not None and state.get('app_active') is True
                    and added == {current['window_id']}
                    and all(after.get(wid) == frame for wid, frame in before.items())):
                state['fallback_origin'] = {
                    'source': action['source'], 'surface': copy.deepcopy(self.windows.frame(current)),
                    'windows': copy.deepcopy(after)}
        self.windows.refresh_popup_focus_relation(state, rows, current)
        if popup_pending is not None:
            owner, action = popup_pending
            before = dict(action['windows'])
            after = {row['window_id']: self.windows.frame(row) for row in rows}
            added = after.keys() - before.keys()
            source_id = action['source'][0]
            candidate = current if current is not None and added == {current['window_id']} else None
            try:
                focused_id = self.windows.focused_window_id(state)
            except ActionError:
                focused_id = None
            if (owner is state and candidate is not None and state.get('app_active') is True
                    and state.get('frontmost_window_id') == candidate['window_id']
                    and candidate.get('pid', state['pid']) == state['pid']
                    and focused_id == source_id
                    and after.get(source_id) == action['source']
                    and all(after.get(wid) == frame for wid, frame in before.items())):
                relation = {
                    'surface': copy.deepcopy(self.windows.frame(candidate)),
                    'focus_owner': copy.deepcopy(action['source']),
                    'windows': copy.deepcopy(after),
                    'status': 'active',
                }
                state['popup_focus_relation'] = relation
                self.driver.record({'phase': 'popup_focus_relation_created',
                             'surface_window_id': candidate['window_id'],
                             'focus_owner_window_id': source_id,
                             'windows': list(sorted(after))})
            else:
                self.driver.record({'phase': 'popup_focus_candidate_rejected',
                             'source_window_id': source_id,
                             'added_window_ids': list(sorted(added)),
                             'focused_window_id': focused_id})
        self.windows.validate_fallback_origin(state, current, rows)
        if current is not None and len(rows) > 1:
            clear_image(state)
            current = self.windows.inspect_window_relationships(state, current, rows)
        observed_window = self.windows.observation_window(state, current, rows) if current else None
        attached = bool(current and current['window_id'] in state.get('attachments', {}))
        chain = None
        root = None
        contextual_attachment = False
        if attached:
            chain = self.windows.attachment_chain(state, current['window_id'], rows)
            root = next(row for row in rows if row['window_id'] == chain[-1])
            contextual_attachment = self.driver.window_attachment_context and all(
                state['attachments'][child][0].get('role') in ATTACHED_WINDOW_ROLES
                for child in chain[:-1])
        attachment_bounds = (current if attached and self.windows.attachment_role(
            state, current['window_id']) == 'AXMenu' else root)
        capture_window = current if contextual_attachment else (root if attached else observed_window)
        exact_capture = contextual_attachment or bool(
            capture_window and capture_window.get('native_inventory') is True
            and capture_window.get('native_verified') is True)
        if method == 'getScreenshot':
            observation = self.observe_window(
                state, method, capture_window, native_resolution=exact_capture,
                attachment_chain=chain if contextual_attachment else None,
                attachment_root=root if contextual_attachment else None,
                attachment_bounds=attachment_bounds if contextual_attachment else None)
            state['window'] = current
            if observation.get('image'):
                state['image_frame'] = self.windows.frame(current)
            if attached:
                self.windows.verify_relationship_frames(state, rows)
            self.windows.verify_frame(state, current)
            return self.describe_coordinates(state, rows, observation)
        snapshots = state.get('window_states', {})
        elements, targets = {}, {}
        application_menu_paths = {}
        application_menu_only = False
        image = None
        source = None
        rendered = ''
        updated = {}
        if current is not None:
            wid = observed_window['window_id']
            child = dict(snapshots.get(wid, {}))
            child.update(pid=state['pid'], bundle_id=state.get('bundle_id'),
                         native_inventory_error=state.get('native_inventory_error'))
            child_method = ('getAXState' if attached and method == 'getAXStateAndScreenshot'
                            else 'getAXStateAndScreenshot' if method == 'getAXStateAndScreenshot'
                            else 'getAXState')
            observation = self.observe_window(
                child, child_method,
                observed_window, disable_diff, scope=('window', wid),
                native_resolution=bool(
                    observed_window.get('native_inventory') is True
                    and observed_window.get('native_verified') is True))
            state['attachment_ax'][wid] = child.get('raw_elements', [])
            image = observation['image']
            if image:
                image_geometry = {name: child[name] for name in (
                    'image_frame', 'image_window', 'image_size', 'image_bounds',
                    'image_background_input', 'image_capture_id', 'image_attachment_chain',
                    'image_attachment_root_window_id')
                    if name in child}
                image_geometry['image_frame'] = self.windows.frame(current)
            if child['elements']:
                source, rendered, updated[wid] = observed_window, observation['state'], child
            else:
                for candidate in sorted((w for w in rows if w['window_id'] != wid),
                                        key=window_stack_sort_key, reverse=True):
                    candidate_id = candidate['window_id']
                    candidate_state = dict(snapshots.get(candidate_id, {}))
                    candidate_state.update(pid=state['pid'], bundle_id=state.get('bundle_id'))
                    sheet = self.observe_window(
                        candidate_state, 'getAXState', candidate, disable_diff,
                        sheet_bounds=current.get('bounds'), display_window=current)
                    if candidate_state['elements']:
                        source, rendered = candidate, sheet['state']
                        updated[candidate_id] = candidate_state
                        break
            if attached and method == 'getAXStateAndScreenshot':
                capture_state = {**snapshots.get(capture_window['window_id'], {}),
                                 'pid': state['pid'], 'bundle_id': state.get('bundle_id')}
                captured = self.observe_window(
                    capture_state, 'getScreenshot', capture_window,
                    native_resolution=contextual_attachment,
                    attachment_chain=chain if contextual_attachment else None,
                    attachment_root=root if contextual_attachment else None,
                    attachment_bounds=attachment_bounds if contextual_attachment else None)
                image = captured['image']
                if image:
                    image_geometry = {name: capture_state[name] for name in (
                        'image_frame', 'image_window', 'image_size', 'image_bounds',
                        'image_background_input', 'image_capture_id', 'image_attachment_chain',
                        'image_attachment_root_window_id') if name in capture_state}
                    image_geometry['image_frame'] = self.windows.frame(current)
        if current is None and 'get_application_menu_state' in self.driver.tools:
            result = self.call('get_application_menu_state', {'pid': state['pid']})
            raw = data(result)
            if raw.get('pid') != state['pid'] or raw.get('surface', {}).get('kind') != 'application_menu':
                raise ValueError('ApplicationChanged: application-menu observation identity mismatch')
            if raw.get('visible_window_count') not in {None, 0}:
                raise ValueError('WindowChanged: a visible window appeared during application-menu observation')
            menu_raw = compact_rows(with_text_labels(raw.get('elements', []), result))
            menu_tree = application_menu_tree(menu_raw)
            if menu_tree and raw.get('app_active') is True and state.get('app_active') is True:
                application_menu_only = True
                state['menu_sources'] = {'application': menu_raw}
                visible = application_menu_elements(menu_raw)
                entries = menu_entries(menu_raw)
                for index, element in enumerate(visible):
                    elements[index] = {**element, 'depth': max(0, element.get('depth', 0))}
                    matches = [path for raw_element, path in entries
                               if raw_element is element or raw_element == element]
                    if len(matches) == 1:
                        application_menu_paths[index] = matches[0]
                app_name = raw.get('app_name') or state.get('bundle_id') or state['pid']
                rendered = (f'Application: "{app_name}", PID: {state["pid"]}\n'
                            'Window: none — application has no visible windows.\n'
                            'Application menu bar:\n'
                            + '\n'.join(ax_line(i, elements[i]) for i in sorted(elements)))
        if source is not None:
            elements.update(updated[source['window_id']]['elements'])
            targets.update({index: source for index in elements})
        for wid, child_state in updated.items():
            state['attachment_ax'][wid] = child_state.get('raw_elements', [])
        if not application_menu_only:
            state['menu_sources'] = {wid: child.get('raw_elements', []) for wid, child in updated.items()}
        menu_indices, menu_text = self.supplement_application_menu(state, rows, current, elements, targets)
        if menu_text:
            rendered = '\n'.join(filter(None, (rendered, menu_text)))
        if application_menu_only:
            menu_indices.update(application_menu_paths)
        else:
            menu_tokens = {wid: {e.get('element_token') for e, _ in menu_entries(raw)
                                 if e.get('element_token')}
                           for wid, raw in state['menu_sources'].items()}
            menu_indices.update(i for i, e in elements.items()
                                if e.get('element_token') in menu_tokens.get(
                                    targets[i]['window_id'], set()))
        self.windows.verify_relationship_frames(state, rows)
        self.windows.build_attachments(state, rows)
        surfaces = {}
        if not application_menu_only:
            for index, element in elements.items():
                owners = state['attachment_tokens'].get(
                    (targets[index]['window_id'], element.get('element_token')), set())
                if len(owners) == 1:
                    surfaces[index] = next(w for w in rows if w['window_id'] in owners)
        if image:
            self.windows.verify_frame(state, current)
            clear_image(state)
            state.update(image_geometry)
        state.update(window=current, window_states=updated, elements=elements,
                     element_windows=targets, element_surfaces=surfaces,
                     application_menu_indices=menu_indices,
                     application_menu_paths=application_menu_paths, fresh=True)
        if state.get('bundle_id') == 'com.apple.finder':
            # Remember the editor the model saw, across a screenshot-only cell.
            raw = state['attachment_ax'].get(current['window_id'], []) if current else []
            state['finder_editor'] = self.windows.finder_editor(state, current, rows, raw)
        if state.get('menu_context'):
            rendered = self.render_menu_context(state, rows)
        launch_window_timeout = state.pop('launch_window_timeout', None)
        if application_menu_only and launch_window_timeout is not None:
            rendered = (f'Application launched successfully, but no visible window appeared within '
                        f'{launch_window_timeout:g} seconds. Its application menu is available.\n'
                        + rendered)
        if not elements:
            rendered = ((f'Application launched successfully, but no visible window appeared within '
                         f'{launch_window_timeout:g} seconds.')
                        if not rows and launch_window_timeout is not None else
                        'Application is running but has no visible windows.' if not rows else
                        'App AX unavailable: no accessible elements in the current window.')
        return self.describe_coordinates(state, rows, {'state': rendered, 'image': image})


    def supplement_application_menu(self, state, rows, current, elements, targets):
        """Read app menus through driver snapshots, preserving their source tokens."""
        if current is None or any(e.get('role') in {'AXMenuBar', 'AXMenuBarItem', 'AXMenuItem'}
                                  for e in elements.values()):
            return set(), ''
        main = state.get('native_window_context', {}).get('main_window', {})
        main_id = main.get('window_id') if main.get('status') == 'ok' else None
        used = {current['window_id'], *(w['window_id'] for w in targets.values())}
        candidates = [w for w in rows if w['window_id'] not in used
                      and w.get('pid', state['pid']) == state['pid']
                      and w.get('is_on_screen') is True and w.get('on_current_space') is not False
                      and (w['window_id'] == main_id or w.get('layer', 0) == 0)
                      and not (w.get('native_inventory') and w.get('native_verified') is not True)]
        candidates.sort(key=lambda w: (w['window_id'] != main_id, -w.get('z_index', -1), w['window_id']))
        # Bound extra reads; never reread a source whose tokens are already displayed.
        for source in candidates[:3]:
            result = self.call('get_window_state', {
                'pid': state['pid'], 'window_id': source['window_id'],
                'include_accessibility_tree': True, 'include_screenshot': False})
            raw = data(result)
            raw_elements = compact_rows(with_text_labels(raw.get('elements', []), result))
            menus = application_menu_elements(raw_elements)
            if not menus:
                continue
            if raw.get('pid') != state['pid'] or raw.get('window_id') != source['window_id']:
                raise ValueError('WindowChanged: menu snapshot identity mismatch')
            state.setdefault('menu_sources', {})[source['window_id']] = raw_elements
            indices = set()
            for index, element in enumerate(menus, start=max(elements, default=-1) + 1):
                elements[index] = element
                targets[index] = source
                indices.add(index)
            text = f"Application menu bar (AX source window ID: {source['window_id']}):\n"
            return indices, text + '\n'.join(ax_line(i, elements[i]) for i in sorted(indices))
        return set(), ''

    def render_menu_context(self, state, rows=None):
        """Expose one logical menu level without opening an on-screen menu."""
        context = state['menu_context']
        source = context['source']
        application_only = context.get('application_only') is True
        if application_only:
            live_rows = self.windows.interaction_windows(state) if rows is None else rows
            if live_rows or state.get('app_active') is not True or state.get('window') is not None:
                state.pop('menu_context', None)
                state['window_states'] = {}
                state['fresh'] = False
                raise ValueError('WindowChanged: application-menu surface changed; observe again')
            raw_menu = state.get('menu_sources', {}).get('application', [])
        else:
            if rows is not None and not any(
                    self.windows.frame(w) == self.windows.frame(source) for w in rows):
                state.pop('menu_context', None)
                state['window_states'] = {}
                state['fresh'] = False
                raise ValueError('WindowChanged: menu source changed; observe again')
            raw_menu = state.get('menu_sources', {}).get(source['window_id'], [])
        entries = menu_entries(raw_menu)
        matches = [(e, path) for e, path in entries if path == context['path']]
        window_changed = (not application_only
                          and self.windows.frame(state['window']) != context['current_frame'])
        if window_changed or len(matches) != 1:
            state.pop('menu_context', None)
            state['window_states'] = {}
            state['fresh'] = False
            # A vanished semantic menu path is not a transient window race.
            # Do not let observation retry silently replace it with a window tree.
            if len(matches) != 1:
                raise ValueError('StaleElement: menu context missing or ambiguous; observe again')
            raise ValueError('WindowChanged: menu window changed; observe again')
        path = context['path']
        children = [(e, p) for e, p in entries if len(p) == len(path) + 1 and p[:-1] == path]
        if not children:
            state.pop('menu_context', None)
            state['window_states'] = {}
            state['fresh'] = False
            raise ValueError('UnsupportedCapability: menu has no accessible options; no menu action sent')
        visible = [(matches[0][0], path), *children]
        # A driver reread can produce different indices; keep its raw tokens and rebase display IDs.
        state['elements'] = {i: {**e, 'depth': 0 if i == 0 else 1}
                             for i, (e, _) in enumerate(visible)}
        state['element_windows'] = ({} if application_only else
                                    {i: source for i in state['elements']})
        state['element_surfaces'] = {}
        state['application_menu_indices'] = set(state['elements'])
        state['application_menu_paths'] = ({i: item_path for i, (_, item_path) in enumerate(visible)}
                                           if application_only else {})
        state['fresh'] = True
        title = ' > '.join(str(part[1] or part[2] or 'unnamed') for part in path)
        surface = ('Window: none — application menu surface. '
                   if application_only else f"AX source window ID: {source['window_id']}. ")
        return (f"Menu context: {title}, App: {state.get('bundle_id') or state['pid']}, "
                + surface + "Options only; no on-screen menu opened. Press Escape to return.\n"
                + '\n'.join(ax_line(i, e) for i, e in state['elements'].items()))


    def describe_coordinates(self, state, rows, observation):
        """Keep the captured surface separate from the attached click coordinate origin."""
        if observation.get('image'):
            observation['image_dimensions'] = state.get('image_size')
        state.pop('coordinate_frame', None)
        current = state.get('window')
        if not observation.get('image') or not current or current['window_id'] not in state.get('attachments', {}):
            return observation
        try:
            chain = self.windows.attachment_chain(state, current['window_id'], rows)
        except ValueError:
            # Ambiguous relationships can still be observed, but cannot authorize input.
            return observation
        root = next(row for row in rows if row['window_id'] == chain[-1])
        menu_coordinates = self.windows.attachment_role(state, current['window_id']) == 'AXMenu'
        coordinate_window = current if menu_coordinates else root
        bounds = rectangle(coordinate_window.get('bounds'))
        if not bounds or any(not math.isfinite(v) for v in bounds) or min(bounds[2:]) <= 0:
            return observation
        state['coordinate_frame'] = (coordinate_window['window_id'], dict(coordinate_window['bounds']))
        coordinate_kind = 'menu' if menu_coordinates else 'root'
        notice = (f"Coordinate clicks: {coordinate_kind} window ID {coordinate_window['window_id']}, "
                  f"bounds {json.dumps(coordinate_window['bounds'])}; use original window-local coordinates, "
                  f"not screenshot pixels. Screenshot window ID: {state.get('image_window', self.windows.frame(current))[0]}.")
        observation['state'] = '\n'.join(filter(None, (observation.get('state'), notice)))
        return observation

    def observe_window(self, state, method, window, disable_diff=False, offset=0,
                       sheet_bounds=None, display_window=None, scope=None,
                       native_resolution=False, attachment_chain=None,
                       attachment_root=None, attachment_bounds=None):
        previous = state.get('window')
        if previous and self.windows.frame(previous) != self.windows.frame(window):
            clear_image(state)
        state['window'] = window
        identity = {'pid': self.windows.interaction_pid(state, window),
                    'window_id': window['window_id']}
        tree = method != 'getScreenshot'
        result = self.call('get_window_state', self.driver.window_state_args({
            **identity, 'include_screenshot': method != 'getAXState',
            'include_accessibility_tree': tree}, native_resolution=native_resolution,
            attachment_context=bool(attachment_chain)))
        raw = data(result)
        if any(key in raw and raw[key] != value for key, value in identity.items()):
            self.invalidate()
            raise ValueError('WindowChanged: driver observation does not belong to the requested app/window')
        if attachment_chain:
            expected_chain = list(attachment_chain)
            expected_root = attachment_root or {}
            expected_bounds = attachment_bounds or expected_root
            if (raw.get('attachment_chain') != expected_chain
                    or raw.get('attachment_root_window_id') != expected_chain[-1]
                    or rectangle(raw.get('window_bounds')) != rectangle(window.get('bounds'))
                    or rectangle(raw.get('screenshot_bounds')) != rectangle(expected_bounds.get('bounds'))):
                self.invalidate()
                raise ValueError('AttachmentUnverified: driver screenshot relationship disagrees with AX evidence')
        image = next((c for c in result.get('content', []) if c.get('type') == 'image'), None)
        if image:
            state['image_frame'] = self.windows.frame(window)
            # image_frame can bind freshness to an attached child; retain the actual
            # capture source separately for screenshot geometry and diagnostics.
            state['image_window'] = self.windows.frame(window)
            state['image_capture_id'] = raw.get('capture_id')
            state['image_size'] = (raw.get('screenshot_width'), raw.get('screenshot_height'))
            bounds = rectangle(raw.get('screenshot_bounds') or raw.get('window_bounds'))
            if raw.get('screenshot_frame_valid') is True and bounds:
                state['image_bounds'] = bounds
                state['image_background_input'] = raw.get('background_input')
                chain = raw.get('attachment_chain')
                root_id = raw.get('attachment_root_window_id')
                if (isinstance(chain, list) and len(chain) > 1
                        and all(type(window_id) is int for window_id in chain)
                        and len(set(chain)) == len(chain)
                        and chain[0] == window['window_id'] and root_id == chain[-1]):
                    state['image_attachment_chain'] = chain
                    state['image_attachment_root_window_id'] = root_id
                else:
                    state.pop('image_attachment_chain', None)
                    state.pop('image_attachment_root_window_id', None)
            else:
                state.pop('image_bounds', None)
                state.pop('image_background_input', None)
                state.pop('image_attachment_chain', None)
                state.pop('image_attachment_root_window_id', None)
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
            title = ('Attached sheet: ' if sheet else 'Window: ')
            shown_title = (shown.get('title', '') if display_window and not popover else
                           raw.get('window_title', shown.get('title', '')))
            title += json.dumps(shown_title, ensure_ascii=False)
            title += ', App: ' + json.dumps(raw.get('app_name') or state.get('bundle_id') or state['pid'], ensure_ascii=False)
            notices = []
            if raw.get('degraded_reason') and sheet_bounds is None:
                notices.append('AX unavailable: ' + str(raw['degraded_reason']))
            if state.get('native_inventory_error'):
                notices.append('Native window inventory unavailable: ' + state['native_inventory_error'])
            title += ', Window ID: ' + str(shown['window_id'])
            if display_window and popover:
                title += ', Popover window ID: ' + str(display_window['window_id'])
            elif display_window and sheet:
                title += ', AX source window ID: ' + str(window['window_id'])
            root = (('menu', menu[0].get('label')) if menu else
                    ('sheet' if sheet else 'popover', shown['window_id'], self.windows.frame(shown)) if surface else scope)
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
                         tree_context=context, fresh=True)
            output['state'] = rendered
        return output
