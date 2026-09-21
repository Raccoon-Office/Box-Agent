"""Disabled Cua vision leaves the model client and image path untouched."""

from box_agent.plugins.cua.config import CuaConfig
from box_agent.plugins.cua.wiring import build_cua_bindings


def test_disabled_vision_keeps_original_llm_and_stream():
    llm = object()
    bindings = build_cua_bindings(llm=llm, config=CuaConfig(feed_screenshots=False))
    assert not bindings.active
    assert bindings.llm is llm

    async def events():
        yield "event"

    stream = events()
    bound = bindings.bind_run(stream)
    assert bound is not stream
    bindings.close()
