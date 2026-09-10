from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine
from box_agent.tools.rg_tool import RgTool


RG = shutil.which("rg")
pytestmark = pytest.mark.skipif(RG is None, reason="ripgrep is not installed")


@pytest.mark.asyncio
async def test_rg_searches_content_with_structured_bounded_results(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "alpha\nneedle here\nneedle again\n",
        encoding="utf-8",
    )

    result = await RgTool(workspace_dir=str(tmp_path), executable=RG).execute(
        pattern="needle",
        mode="content",
        path=".",
        globs=["*.py"],
        limit=1,
    )

    assert result.success is True
    assert result.content.startswith("src/app.py:2:1:needle here")
    assert "needle again" not in result.content
    assert "more matches available" in result.content
    assert result.raw_output == {
        "mode": "content",
        "path": str(tmp_path),
        "returned_matches": 1,
        "truncated": True,
        "matches": [
            {
                "path": "src/app.py",
                "line": 2,
                "column": 1,
                "text": "needle here",
            }
        ],
    }


@pytest.mark.asyncio
async def test_rg_finds_files_while_respecting_ignore_and_hidden_defaults(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("included\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("ignored\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("hidden\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()

    result = await RgTool(workspace_dir=str(tmp_path), executable=RG).execute(
        pattern="*.py",
        mode="files",
        path=".",
        limit=10,
    )

    assert result.success is True
    assert result.content == "src/app.py"
    assert result.raw_output["files"] == ["src/app.py"]
    assert result.raw_output["truncated"] is False


@pytest.mark.asyncio
async def test_rg_content_globs_explicitly_include_gitignored_files(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("needle kept\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("needle ignored\n", encoding="utf-8")

    result = await RgTool(workspace_dir=str(tmp_path), executable=RG).execute(
        pattern="needle",
        mode="content",
        globs=["*.py"],
    )

    assert result.success is True
    assert "kept.py" in result.content
    assert "ignored.py" in result.content


@pytest.mark.asyncio
async def test_rg_content_globs_exclude_oversized_unrelated_matches(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("needle kept\n", encoding="utf-8")
    (tmp_path / "minified.js").write_text(
        f"needle{'x' * 1_100_000}\n",
        encoding="utf-8",
    )

    result = await RgTool(workspace_dir=str(tmp_path), executable=RG).execute(
        pattern="needle",
        mode="content",
        globs=["docs/**"],
    )

    assert result.success is True
    assert "docs/guide.md:1:1:needle kept" in result.content
    assert "minified.js" not in result.content
    assert "oversized matching lines were skipped" not in result.content


@pytest.mark.asyncio
async def test_rg_content_mode_returns_requested_context_lines(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "before\nneedle\nafter\n",
        encoding="utf-8",
    )

    result = await RgTool(workspace_dir=str(tmp_path), executable=RG).execute(
        pattern="needle",
        context=1,
    )

    assert result.success is True
    assert result.content.splitlines() == [
        "app.py:1:-before",
        "app.py:2:1:needle",
        "app.py:3:-after",
    ]
    assert result.raw_output["returned_matches"] == 1


@pytest.mark.asyncio
async def test_rg_bounds_oversized_matching_lines(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text(
        f"needle{'x' * 100_000}\n",
        encoding="utf-8",
    )

    result = await RgTool(workspace_dir=str(tmp_path), executable=RG).execute(
        pattern="needle",
    )

    assert result.success is True
    assert len(result.content) < 2_100
    assert result.raw_output["matches"][0]["text"].endswith("... [line truncated]")


@pytest.mark.asyncio
async def test_rg_skips_oversized_records_and_keeps_other_matches(tmp_path: Path) -> None:
    (tmp_path / "minified.js").write_text(
        f"needle{'x' * 1_100_000}\nneedle kept\n",
        encoding="utf-8",
    )

    result = await RgTool(workspace_dir=str(tmp_path), executable=RG).execute(
        pattern="needle",
    )

    assert result.success is True
    assert "minified.js:2:1:needle kept" in result.content
    assert "one or more oversized matching lines were skipped" in result.content


@pytest.mark.asyncio
async def test_rg_returns_permission_request_before_searching_external_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = Path.home() / "box-agent-rg-permission-probe"
    engine = PermissionEngine(
        CapabilityPolicy(
            filesystem_scope="session_workspace",
            session_workspace_root=str(workspace),
        ),
        workspace,
    )

    result = await RgTool(
        workspace_dir=str(workspace),
        executable=RG,
        allow_full_access=False,
        permission_engine=engine,
    ).execute(pattern="needle", path=str(outside))

    assert result.success is False
    assert result.permission_request is not None
    assert result.permission_request["path"] == str(outside)


@pytest.mark.asyncio
async def test_rg_blocks_recursive_search_from_user_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    result = await RgTool(workspace_dir=str(workspace), executable=RG).execute(
        pattern="*.py",
        mode="files",
        path="~",
    )

    assert result.success is False
    assert result.error.startswith("BROAD_HOME_SEARCH_BLOCKED:")
    assert result.permission_request is None
