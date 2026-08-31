"""Regression coverage for execute_code artifact-root write confinement."""

from __future__ import annotations

from pathlib import Path

import pytest

from box_agent.tools.jupyter_tool import JupyterSandboxTool, SandboxEnvironment


@pytest.mark.asyncio
async def test_execute_code_blocks_session_root_writes_and_allows_output_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def skip_unrelated_package_verification(
        _self: SandboxEnvironment,
        _on_progress=None,
    ) -> None:
        return None

    monkeypatch.setattr(
        SandboxEnvironment,
        "_verify_packages",
        skip_unrelated_package_verification,
    )
    session_root = tmp_path / "session"
    session_root.mkdir()
    (session_root / "input.txt").write_text("source", encoding="utf-8")
    allowed_tmp = tmp_path / "kernel-tmp"
    allowed_tmp.mkdir()
    monkeypatch.setattr(
        "box_agent.tools.jupyter_tool.tempfile.gettempdir",
        lambda: str(allowed_tmp),
    )
    tool = JupyterSandboxTool(
        workspace_dir=str(session_root),
        use_output_dir=True,
    )
    assert "durable writes are confined to the active" in tool.description
    session_id = "artifact-root-guard"

    try:
        blocked = await tool.execute(
            code=(
                "from pathlib import Path\n"
                "target = Path('../research/exact-page-reads.json')\n"
                "target.parent.mkdir(parents=True, exist_ok=True)\n"
                "target.write_text('{}', encoding='utf-8')"
            ),
            session_id=session_id,
        )

        assert blocked.success is False
        assert "EXECUTE_CODE_WRITE_OUTSIDE_ARTIFACT_ROOT" in (blocked.error or "")
        assert not (session_root / "research" / "exact-page-reads.json").exists()

        allowed = await tool.execute(
            code=(
                "from pathlib import Path\n"
                "source = Path('../input.txt').read_text(encoding='utf-8')\n"
                "target = Path('research/result.txt')\n"
                "target.parent.mkdir(parents=True, exist_ok=True)\n"
                "target.write_text(source, encoding='utf-8')\n"
                "print(target)"
            ),
            session_id=session_id,
        )

        assert allowed.success is True
        assert (session_root / "output" / "research" / "result.txt").read_text(
            encoding="utf-8"
        ) == "source"
    finally:
        await JupyterSandboxTool.shutdown_all()
