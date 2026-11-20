"""Finder icon-view inline editing uses the verified root without raising it."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from box_agent._vendor.mac_cua.cua_adapter.cua_adapter import Adapter


@pytest.fixture
def finder(monkeypatch):
    session = {"properties": {"pid": {}, "window_id": {}, "delivery_mode": {}, "text": {},
                              "modifiers": {}, "key": {}}}
    client = SimpleNamespace(tools={k: {"inputSchema": session} for k in ("type_text", "press_key")})
    adapter = Adapter(client)
    main = dict(window_id=1, pid=7, layer=0, current_space_id=1, is_on_screen=True,
                on_current_space=True, title="Folder", bounds=dict(x=0, y=0, width=600, height=400))
    surface = dict(main, window_id=2, title="", bounds=dict(x=28, y=118, width=90, height=22))
    field = dict(role="AXTextField", depth=0, enabled=True, label="Old.txt", value="Old.txt",
                 frame=dict(x=30, y=120, w=85, h=18))
    icon = dict(role="AXImage", selected=True, label="Old.txt", frame=dict(x=40, y=55, w=64, h=64))
    state = dict(bundle_id="com.apple.finder", window=main, pid=7, app_active=True, frontmost_window_id=2)
    raw = dict(pid=7, window_id=1, window_bounds=main["bounds"], truncated=False, elements=[field, icon])
    context = dict(trusted=True, main_window=dict(status="ok", window_id=1, minimized=False),
                   focused_window=dict(status="ax_error", ax_error=-25212))
    rows = [main, surface]
    def call(name, args):
        result = {"get_window_state": raw, "guest_get_window_context": context,
                  "type_text": {"effect": "unverifiable"}, "press_key": {}}[name]
        return {"structuredContent": result}
    calls = Mock(side_effect=call)
    windows = adapter.window_context
    monkeypatch.setattr(windows, "verify_frame", lambda *_: rows)
    monkeypatch.setattr(windows, "call", calls)
    monkeypatch.setattr(adapter.actions, "call", calls)
    def observe():
        state["finder_editor"] = windows.finder_editor(state, main, rows, raw["elements"])
    observe()
    return adapter.actions, state, rows, raw, context, calls, observe


def test_type_and_commit_keep_verified_finder_editor_focused(finder):
    actions, state, rows, raw, _, calls, observe = finder
    actions.execute(state, "typeText", {"text": "New"})
    field = raw["elements"][0]
    field.update(label="New.txt", value="New.txt")
    rows[1]["bounds"]["width"] += 12
    observe()
    actions.execute(state, "pressKey", {"key": "return"})
    assert [c.args[0] for c in calls.call_args_list] == [
        "get_window_state", "guest_get_window_context", "type_text",
        "get_window_state", "guest_get_window_context", "press_key"]
    for c in (calls.call_args_list[2], calls.call_args_list[5]):
        assert c.args[1]["window_id"] == 1 and c.args[1]["delivery_mode"] == "foreground"


@pytest.mark.parametrize("change", ["field", "selection", "surface", "space", "foreground", "focus", "permission"])
def test_changed_finder_editor_stops_before_input(finder, change):
    actions, state, rows, raw, context, calls, _ = finder
    if change == "field":
        raw["elements"].pop(0)
    elif change == "selection":
        raw["elements"][1]["label"] = "Other.txt"
    elif change == "surface":
        rows.pop()
    elif change == "space":
        rows[1]["current_space_id"] = 2
    elif change == "foreground":
        state["app_active"] = False
    elif change == "focus":
        context["focused_window"] = dict(status="ok", window_id=99)
    else:
        context["trusted"] = False
    with pytest.raises(ValueError, match="no input sent"):
        actions.execute(state, "typeText", {"text": "New"})
    assert [c.args[0] for c in calls.call_args_list] == ["get_window_state", "guest_get_window_context"]


def test_finder_editor_disappearing_before_submit_refuses_return(finder):
    actions, state, _, raw, _, calls, observe = finder
    actions.execute(state, "typeText", {"text": "New"})
    raw["elements"].pop(0)
    observe()
    calls.reset_mock()
    with pytest.raises(ValueError, match="no input sent"):
        actions.execute(state, "pressKey", {"key": "return"})
    calls.assert_not_called()
