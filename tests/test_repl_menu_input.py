"""Attached Excel-style menus have a pixel surface distinct from their AX key owner."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from box_agent._vendor.mac_cua.cua_adapter.cua_adapter import Adapter
from box_agent._vendor.mac_cua.cua_adapter.cua_driver import ActionError


@pytest.fixture
def menu(monkeypatch):
    fields = "pid window_id activation_window_id delivery_mode key modifiers capture_id x y button count element_token"
    schema = {"inputSchema": {"properties": {field: {} for field in fields.split()}}}
    adapter = Adapter(SimpleNamespace(tools={"press_key": schema, "click": schema}))
    windows = adapter.window_context
    parent = dict(pid=7, window_id=10, bounds=dict(x=340, y=33, width=700, height=327))
    popup = dict(pid=7, window_id=12, bounds=dict(x=461, y=133, width=164, height=154))
    frame = windows.frame(popup)
    state = dict(pid=7, window=popup, app_active=True, frontmost_window_id=12,
                 driver_active_window_id=10, image_capture_id="menu-capture", image_size=(328, 308),
                 image_window=frame, image_frame=frame, image_bounds=(461, 133, 164, 154),
                 image_attachment_chain=[12, 10], coordinate_frame=frame,
                 image_background_input={"exact_window": dict(pid=7, window_id=12, status="ax_unresolved")},
                 attachments={12: [dict(role="AXMenu", surface=frame, source=windows.frame(parent))]})
    adapter.handles["excel"] = state
    context = dict(pid=7, trusted=True, focused_window=dict(status="ok", minimized=False, window_id=10))
    monkeypatch.setattr(windows, "call", lambda *_: {"structuredContent": context})
    monkeypatch.setattr(windows, "verify_frame", lambda *_: [popup, parent])
    monkeypatch.setattr(windows, "refresh_keyboard_focus", Mock())
    send = Mock(return_value={"structuredContent": {"effect": "confirmed"}})
    monkeypatch.setattr(adapter.actions, "call", send)
    return adapter.actions, state, send, context


@pytest.mark.parametrize("key", ["down", "end", "return", "escape"])
def test_menu_navigation_goes_once_to_focused_sort_dialog(menu, key):
    actions, state, send, _ = menu
    actions.execute(state, "pressKey", {"key": key})
    send.assert_called_once_with("press_key", dict(pid=7, window_id=10, key=key,
                                                  modifiers=[], delivery_mode="foreground"))


def test_menu_click_binds_retina_pixels_to_capture_and_focus_to_parent(menu):
    actions, state, send, _ = menu
    actions.execute(state, "click", {"target": [82, 115]})
    send.assert_called_once_with("click", dict(pid=7, window_id=12, activation_window_id=10,
                                              x=164, y=230, capture_id="menu-capture",
                                              button="left", count=1, delivery_mode="foreground"))
    with pytest.raises(ValueError, match="screenshot"):
        actions.execute(state, "click", {"target": [82, 115]})
    assert send.call_count == 1


@pytest.mark.parametrize("change", ["owner", "pid", "front", "trust", "capture", "root", "bounds", "chain"])
def test_changed_or_unverified_menu_never_sends_click(menu, change):
    actions, state, send, context = menu
    if change == "owner":
        context["focused_window"]["window_id"] = 17
    elif change == "pid":
        context["pid"] = 9
    elif change == "front":
        state["frontmost_window_id"] = 10
    elif change == "trust":
        context["trusted"] = False
    elif change == "capture":
        state.pop("image_capture_id")
    elif change == "root":
        state["coordinate_frame"] = state["attachments"][12][0]["source"]
    elif change == "bounds":
        state["image_bounds"] = (460, 133, 164, 154)
    else:
        state["image_attachment_chain"] = [12, 17]
    with pytest.raises(ValueError):
        actions.execute(state, "click", {"target": [82, 115]})
    send.assert_not_called()


def test_keyboard_owner_outside_attachment_chain_refuses_without_sending(menu):
    actions, state, send, context = menu
    context["focused_window"]["window_id"] = state["driver_active_window_id"] = 17
    with pytest.raises(ValueError, match="focus owner changed"):
        actions.execute(state, "pressKey", {"key": "return"})
    send.assert_not_called()


def test_semantic_menu_item_retains_token_source_and_propagates_ownership_refusal(menu, monkeypatch):
    actions, state, send, _ = menu
    windows = actions.windows
    rows = windows.verify_frame(state, state["window"])
    popup, dialog = rows
    state.update(fresh=True, active_window_id=popup["window_id"],
                 elements={5: dict(element_token="dialog:7", actions=["AXPress"])},
                 element_windows={5: dialog}, element_surfaces={5: popup})
    monkeypatch.setattr(windows, "interaction_windows", lambda *_: rows)
    refusal = ActionError({"structuredContent": {"code": "element_outside_target_window",
                                                 "effect": "not_sent"}})
    send.side_effect = refusal
    with pytest.raises(ActionError) as caught:
        actions.execute(state, "click", {"target": 5})
    assert caught.value is refusal
    send.assert_called_once_with("click", dict(pid=7, window_id=10, element_token="dialog:7",
                                              button="left", delivery_mode="background"))
