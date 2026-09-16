"""Technical delivery does not silently claim missing or stale visual review."""

import pytest

from tests.test_presentation_delivery import adapter, deck, fake_steps, finish, module


@pytest.mark.asyncio
@pytest.mark.parametrize("review", ["missing", "current", "inputs_changed"])
async def test_delivery_discloses_missing_or_stale_review(module, deck, monkeypatch, review):
    if review != "missing":
        (deck / "_trace").mkdir()
        for name in ("review-issues.md", "content-fidelity.md"):
            (deck / "_trace" / name).write_text("Fixture review report")
    (deck / "speech.md").write_text("Page 1\nPage 2")
    obj = adapter(module, deck)
    fake_steps(monkeypatch, obj, deck)
    if review == "inputs_changed":
        original_run = obj._run

        async def build_changes_input(argv, **kwargs):
            result = await original_run(argv, **kwargs)
            if argv[2] == "build":
                (deck / "base.css").write_text(".slide {width:1600px;height:900px;color:navy}")
            return result

        monkeypatch.setattr(obj, "_run", build_changes_input)
    receipt = await finish(obj, deck, ("html",))
    assert receipt["status"] == "complete"
    warnings = "\n".join(receipt.get("warnings", []))
    if review == "missing":
        assert "Review" in warnings and "缺少" in warnings
    elif review == "inputs_changed":
        assert "Review" in warnings and "更新" in warnings
    else:
        assert not warnings
