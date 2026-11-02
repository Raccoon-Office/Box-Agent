from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from box_agent.tool_result_storage import ToolResultStorage
from box_agent.tools.engine.results import ToolResultPipelineInput, process_tool_result
from box_agent.tools.file_result_adapter import _tool_message_content_for_model
from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine
from box_agent.tools.ripgrep_tool import (
    GlobTool,
    GrepTool,
    MAX_STDERR_BYTES,
    _RipgrepTool,
    resolve_ripgrep_executable,
)


RG = shutil.which("rg")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_class", [GrepTool, GlobTool])
async def test_whole_home_search_stays_blocked_with_specific_location_guidance(tmp_path, monkeypatch, tool_class):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    tool = tool_class(workspace_dir=str(tmp_path), executable="rg")
    result = await tool.execute(pattern="report", path=str(fake_home))
    assert not result.success
    assert result.error.startswith("BROAD_HOME_SEARCH_BLOCKED:")
    assert "~/Desktop" in result.error
    assert "Do not retry the same scope with another tool" in result.error


@pytest.mark.asyncio
async def test_legacy_search_keeps_same_home_protection_and_recovery_guidance(tmp_path, monkeypatch):
    from box_agent.tools.file_tools import SearchFilesTool
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    result = await SearchFilesTool(workspace_dir=str(tmp_path)).execute(pattern="report", path=str(fake_home))
    assert not result.success
    assert result.error.startswith("BROAD_HOME_SEARCH_BLOCKED:")
    assert "~/Desktop" in result.error
    assert "Do not retry the same scope with another tool" in result.error


def require_rg() -> str:
    if RG is None:
        if os.environ.get("BOX_AGENT_TEST_REQUIRE_RG"):
            pytest.fail("ripgrep is required by BOX_AGENT_TEST_REQUIRE_RG")
        pytest.skip("ripgrep is not installed")
    return RG


def fake_rg_tool(tool_cls, tmp_path: Path, script: str, **kwargs):
    """Run a Python stand-in for ripgrep; portable to Windows (no shebang)."""

    script_path = tmp_path / "fake_rg.py"
    script_path.write_text(script, encoding="utf-8")

    class FakeRipgrepTool(tool_cls):
        spawned: list[asyncio.subprocess.Process] = []

        async def _spawn(self, *, cwd, args):
            process, error = await super()._spawn(
                cwd=cwd, args=[str(script_path), *args]
            )
            if process is not None:
                self.spawned.append(process)
            return process, error

    kwargs.setdefault("workspace_dir", str(tmp_path))
    return FakeRipgrepTool(executable=sys.executable, **kwargs)


def test_resolve_ripgrep_prefers_injected_executable(tmp_path: Path) -> None:
    executable = tmp_path / "rg"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)

    assert resolve_ripgrep_executable({"BOX_AGENT_RG": str(executable)}) == str(
        executable.resolve()
    )


def test_glob_description_distinguishes_search_from_directory_listing(
    tmp_path: Path,
) -> None:
    description = GlobTool(
        workspace_dir=str(tmp_path), executable="rg"
    ).description.casefold()

    assert "basename-only patterns match files at any depth" in description
    assert "does not return directories" in description
    assert "do not use glob('*') to inspect a directory" in description


@pytest.mark.asyncio
async def test_stderr_reader_drains_after_retained_output_is_truncated() -> None:
    class ChunkedReader:
        def __init__(self) -> None:
            self.chunks = [b"x" * MAX_STDERR_BYTES, b"overflow", b""]
            self.read_calls = 0

        async def read(self, _size: int) -> bytes:
            self.read_calls += 1
            return self.chunks.pop(0)

    reader = ChunkedReader()

    output = await _RipgrepTool._read_stderr(reader)  # type: ignore[arg-type]

    assert reader.read_calls == 3
    assert output.endswith("...[stderr truncated]")


@pytest.mark.asyncio
async def test_search_tools_do_not_request_full_result_sorting(tmp_path: Path) -> None:
    script = (
        "import json, sys\n"
        "if any(arg.startswith('--sort') for arg in sys.argv[1:]):\n"
        "    print('sorting disables bounded streaming', file=sys.stderr)\n"
        "    raise SystemExit(9)\n"
        "if '--files' in sys.argv:\n"
        "    print('src/app.py', end='\\0')\n"
        "else:\n"
        "    print(json.dumps({'type': 'match', 'data': {"
        "'path': {'text': 'src/app.py'}, 'line_number': 1, "
        "'lines': {'text': 'needle\\n'}, 'submatches': [{'start': 0}]}}))\n"
    )

    glob_result = await fake_rg_tool(GlobTool, tmp_path, script).execute(
        pattern="*.py"
    )
    grep_result = await fake_rg_tool(GrepTool, tmp_path, script).execute(
        pattern="needle"
    )

    assert glob_result.success is True
    assert glob_result.raw_output["files"] == ["src/app.py"]
    assert grep_result.success is True
    assert grep_result.raw_output["matches"] == [
        {"path": "src/app.py", "line": 1, "column": 1, "text": "needle"}
    ]


@pytest.mark.asyncio
async def test_cancelling_search_terminates_the_ripgrep_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "fake_rg.pid"
    tool = fake_rg_tool(
        GlobTool,
        tmp_path,
        "import os, pathlib, time\n"
        "pathlib.Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\n"
        "while True:\n"
        "    time.sleep(1)\n",
    )
    task = asyncio.create_task(tool.execute(pattern="*.py"))
    for _ in range(500):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(tool.spawned) == 1
    assert tool.spawned[0].returncode is not None


@pytest.mark.asyncio
async def test_glob_supports_braces_without_returning_ignored_files(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "dist").mkdir()
    (tmp_path / ".gitignore").write_text("dist/\nignored.ts\n", encoding="utf-8")
    (tmp_path / "src" / "app.ts").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "src" / "view.tsx").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "src" / "note.js").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "dist" / "built.tsx").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "ignored.ts").write_text("export {}\n", encoding="utf-8")

    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="*.{ts,tsx}")

    assert result.success is True
    assert set(result.content.splitlines()) == {"src/app.ts", "src/view.tsx"}
    assert result.raw_output["path"] == str(tmp_path)
    assert result.raw_output["returned_files"] == 2
    assert result.raw_output["truncated"] is False
    assert set(result.raw_output["files"]) == {"src/app.ts", "src/view.tsx"}


@pytest.mark.asyncio
async def test_glob_truncation_requires_narrowing_before_drawing_conclusions(
    tmp_path: Path,
) -> None:
    (tmp_path / "first.py").write_text("", encoding="utf-8")
    (tmp_path / "second.py").write_text("", encoding="utf-8")

    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="*.py", limit=1)

    assert result.success is True
    assert result.raw_output["truncated"] is True
    assert "Results are incomplete" in result.content
    assert "Narrow path or pattern before drawing conclusions" in result.content
    assert "Do not retry with '*' to inspect directory structure" in result.content
    model_content = _tool_message_content_for_model(
        tool_name="glob",
        arguments={"pattern": "*.py", "limit": 1},
        result=result,
        visible_content=result.content,
        visible_error=None,
    )
    assert model_content == result.model_context
    assert model_content.startswith("[Incomplete glob results:")
    assert "pattern=*.py" in model_content
    assert "returned=1" in model_content
    assert "Narrow path or pattern" in model_content


@pytest.mark.asyncio
async def test_glob_truncation_keeps_returned_files_for_host_but_limits_model_sample(
    tmp_path: Path,
) -> None:
    for index in range(12):
        (tmp_path / f"file_{index:02}.py").write_text("", encoding="utf-8")

    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="*.py", limit=11)

    assert result.raw_output["returned_files"] == 11
    assert all(path in result.content for path in result.raw_output["files"])
    assert result.model_context is not None
    assert len(result.model_context.splitlines()) < len(result.content.splitlines())


@pytest.mark.asyncio
async def test_glob_matches_project_relative_path_patterns(tmp_path: Path) -> None:
    (tmp_path / "src" / "app" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "app" / "page.tsx").write_text(
        "export default function Page() {}\n", encoding="utf-8"
    )
    (tmp_path / "src" / "app" / "nested" / "route.ts").write_text(
        "export function GET() {}\n", encoding="utf-8"
    )

    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="src/app/*")

    assert result.success is True
    assert result.raw_output["files"] == ["src/app/page.tsx"]


@pytest.mark.asyncio
async def test_glob_accepts_dot_relative_name_patterns(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('app')\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("export {}\n", encoding="utf-8")

    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="./*.py")

    assert result.success is True
    assert result.raw_output["files"] == ["app.py"]


@pytest.mark.asyncio
async def test_glob_rejects_excessive_brace_expansion(tmp_path: Path) -> None:
    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="{a,b}" * 1_100)

    assert result.success is False
    assert "brace alternatives" in (result.error or "")


@pytest.mark.asyncio
async def test_glob_recursive_name_pattern_keeps_ignored_files_out(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "vendor" / "pkg").mkdir(parents=True)
    (tmp_path / ".gitignore").write_text("vendor/\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    (tmp_path / "vendor" / "pkg" / "hidden.py").write_text(
        "print('hidden')\n", encoding="utf-8"
    )

    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="**/*.py")

    assert result.success is True
    assert result.raw_output["files"] == ["src/app.py"]


@pytest.mark.asyncio
async def test_glob_project_relative_pattern_keeps_ignored_files_out(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "generated").mkdir(parents=True)
    (tmp_path / ".gitignore").write_text("src/generated/\n", encoding="utf-8")
    (tmp_path / "src" / "app" / "page.ts").write_text(
        "export const page = true\n", encoding="utf-8"
    )
    (tmp_path / "src" / "generated" / "hidden.ts").write_text(
        "export const hidden = true\n", encoding="utf-8"
    )

    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="src/**/*.ts")

    assert result.success is True
    assert result.raw_output["files"] == ["src/app/page.ts"]


@pytest.mark.asyncio
async def test_glob_path_pattern_keeps_individually_ignored_files_out(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / ".gitignore").write_text(
        "src/app/hidden.ts\n", encoding="utf-8"
    )
    (tmp_path / "src" / "app" / "visible.ts").write_text(
        "export const visible = true\n", encoding="utf-8"
    )
    (tmp_path / "src" / "app" / "hidden.ts").write_text(
        "export const hidden = true\n", encoding="utf-8"
    )

    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="src/app/**/*.ts")

    assert result.success is True
    assert result.raw_output["files"] == ["src/app/visible.ts"]


@pytest.mark.asyncio
async def test_glob_path_pattern_supports_brace_alternatives(tmp_path: Path) -> None:
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "server").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "app" / "page.ts").write_text(
        "export const page = true\n", encoding="utf-8"
    )
    (tmp_path / "server" / "main.py").write_text(
        "print('server')\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "readme.md").write_text("docs\n", encoding="utf-8")

    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="{src/**/*.ts,server/**/*.py}")

    assert result.success is True
    assert set(result.raw_output["files"]) == {
        "src/app/page.ts",
        "server/main.py",
    }


@pytest.mark.asyncio
async def test_grep_include_supports_braces_and_keeps_ignore_semantics(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.ts\n", encoding="utf-8")
    (tmp_path / "src" / "app.tsx").write_text(
        "before\nneedle here\nafter\n", encoding="utf-8"
    )
    (tmp_path / "src" / "note.js").write_text("needle js\n", encoding="utf-8")
    (tmp_path / "ignored.ts").write_text("needle ignored\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", include="*.{ts,tsx}")

    assert result.success is True
    assert result.content == "src/app.tsx:2:1:needle here"
    assert result.raw_output == {
        "path": str(tmp_path),
        "returned_matches": 1,
        "truncated": False,
        "matches": [
            {
                "path": "src/app.tsx",
                "line": 2,
                "column": 1,
                "text": "needle here",
            }
        ],
    }


@pytest.mark.asyncio
async def test_grep_include_matches_project_relative_path_patterns(
    tmp_path: Path,
) -> None:
    (tmp_path / "src" / "app" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "src" / "app" / "nested" / "route.ts").write_text(
        "const needle = true\n", encoding="utf-8"
    )
    (tmp_path / "src" / "lib" / "helper.ts").write_text(
        "const needle = false\n", encoding="utf-8"
    )

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", include="src/app/**/*.ts")

    assert result.success is True
    assert result.raw_output["matches"] == [
        {
            "path": "src/app/nested/route.ts",
            "line": 1,
            "column": 7,
            "text": "const needle = true",
        }
    ]


@pytest.mark.asyncio
async def test_grep_accepts_dot_relative_name_include(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("needle\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", include="./*.py")

    assert result.success is True
    assert [match["path"] for match in result.raw_output["matches"]] == ["app.py"]


@pytest.mark.asyncio
async def test_grep_recursive_name_include_keeps_ignored_files_out(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "vendor" / "pkg").mkdir(parents=True)
    (tmp_path / ".gitignore").write_text("vendor/\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "vendor" / "pkg" / "hidden.py").write_text(
        "needle\n", encoding="utf-8"
    )

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", include="**/*.py")

    assert result.success is True
    assert [match["path"] for match in result.raw_output["matches"]] == [
        "src/app.py"
    ]


@pytest.mark.asyncio
async def test_grep_path_include_keeps_individually_ignored_files_out(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / ".gitignore").write_text(
        "src/app/hidden.ts\n", encoding="utf-8"
    )
    (tmp_path / "src" / "app" / "visible.ts").write_text(
        "needle visible\n", encoding="utf-8"
    )
    (tmp_path / "src" / "app" / "hidden.ts").write_text(
        "needle hidden\n", encoding="utf-8"
    )

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", include="src/app/**/*.ts")

    assert result.success is True
    assert [match["path"] for match in result.raw_output["matches"]] == [
        "src/app/visible.ts"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content", ["needle\n", "needle " + "x" * (2 * 1024 * 1024) + "\n"],
    ids=["short", "oversized"],
)
async def test_grep_exact_file_respects_nonmatching_include(tmp_path: Path, content: str) -> None:
    target = tmp_path / "app.py"
    target.write_text(content, encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", path="app.py", include="*.ts")

    assert result.success is True
    assert result.raw_output["matches"] == []
    assert result.raw_output["truncated"] is False


@pytest.mark.asyncio
async def test_grep_reports_truncation_after_bounded_matches(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "needle first\nneedle second\n", encoding="utf-8"
    )

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", limit=1)

    assert result.success is True
    assert result.raw_output["returned_matches"] == 1
    assert result.raw_output["truncated"] is True
    assert "needle first" in result.content
    assert "needle second" not in result.content
    assert "more matches available" in result.content
    model_content = _tool_message_content_for_model(
        tool_name="grep",
        arguments={"pattern": "needle", "limit": 1},
        result=result,
        visible_content=result.content,
        visible_error=None,
    )
    assert model_content == result.content
    assert model_content.startswith("[Incomplete grep results:")
    assert "pattern=needle" in model_content
    assert "returned=1" in model_content
    assert "Narrow path or pattern" in model_content


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [11, 100])
async def test_grep_truncation_keeps_all_returned_matches_in_model_history(
    tmp_path: Path, limit: int
) -> None:
    (tmp_path / "app.py").write_text(
        "".join(f"needle {index:03}\n" for index in range(limit + 1)), encoding="utf-8"
    )

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", limit=limit)

    assert result.raw_output["returned_matches"] == limit
    assert all(match["text"] in result.content for match in result.raw_output["matches"])
    model_content = _tool_message_content_for_model(
        tool_name="grep",
        arguments={"pattern": "needle", "limit": limit},
        result=result,
        visible_content=result.content,
        visible_error=None,
    )
    assert all(match["text"] in model_content for match in result.raw_output["matches"])
    assert f"needle {limit:03}" not in model_content
    assert model_content.startswith("[Incomplete grep results:")


@pytest.mark.asyncio
@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.parametrize("aggregate", [False, True])
async def test_large_grep_output_is_recoverable_from_model_history(
    tmp_path: Path, truncated: bool, aggregate: bool
) -> None:
    (tmp_path / "app.py").write_text(
        "".join(f"needle {index:03} {'x' * 500}\n" for index in range(20 + truncated)),
        encoding="utf-8",
    )
    tool = GrepTool(workspace_dir=str(tmp_path), executable=require_rg())
    arguments = {"pattern": "needle", "limit": 20}
    result = await tool.execute(**arguments)
    assert result.raw_output["truncated"] is truncated

    storage = ToolResultStorage(
        tmp_path / "stored",
        default_result_limit=100_000 if aggregate else 1_000,
        aggregate_budget=1_000,
    )
    messages = []
    outcome = process_tool_result(
        ToolResultPipelineInput(
            messages=messages,
            tool_call_id="grep-1",
            tool_name="grep",
            arguments=arguments,
            result=result,
            visible_content=result.content,
            visible_error=None,
            result_storage=storage,
            tool=tool,
            session_id="session-a",
        )
    )
    if aggregate:
        assert messages[-1].content == result.content
        budget = storage.enforce_fresh_budget(
            messages, tools={"grep": tool}, session_id="session-a"
        )
        assert budget.persisted_count == 1
        assert len(messages[-1].content) <= storage.aggregate_budget

    path = tmp_path / "stored" / "session-a" / "tool-results" / "grep-1.txt"
    model_content = messages[-1].content
    assert model_content.startswith("<persisted-output>")
    assert str(path) in model_content
    assert outcome.visible_content == result.content
    recovered = path.read_text(encoding="utf-8")
    assert recovered == result.content
    assert "needle 019" in recovered
    if truncated:
        assert "Incomplete grep results" in model_content
        assert "needle 020" not in recovered


@pytest.mark.asyncio
async def test_grep_returns_invalid_regex_as_an_error(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("content\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="[")

    assert result.success is False
    assert "regex parse error" in (result.error or "")


@pytest.mark.asyncio
async def test_glob_returns_permission_request_for_external_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = Path.home() / "box-agent-glob-permission-probe"
    engine = PermissionEngine(CapabilityPolicy(), workspace)

    result = await GlobTool(
        workspace_dir=str(workspace),
        executable=require_rg(),
        allow_full_access=False,
        permission_engine=engine,
    ).execute(pattern="*.py", path=str(external))

    assert result.success is False
    assert result.permission_request is not None
    assert result.permission_request["path"] == str(external)



@pytest.mark.asyncio
async def test_search_tools_include_hidden_files_but_not_git_metadata(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.yml").write_text("needle: git\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "needle: ci\n", encoding="utf-8"
    )
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "stale.yml").write_text("needle: cache\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".cache/\n", encoding="utf-8")
    rg = require_rg()

    by_path = await GlobTool(workspace_dir=str(tmp_path), executable=rg).execute(
        pattern=".github/**/*.yml"
    )
    by_name = await GlobTool(workspace_dir=str(tmp_path), executable=rg).execute(
        pattern="*.yml"
    )
    grep = await GrepTool(workspace_dir=str(tmp_path), executable=rg).execute(
        pattern="needle"
    )

    assert by_path.raw_output["files"] == [".github/workflows/ci.yml"]
    assert by_name.raw_output["files"] == [".github/workflows/ci.yml"]
    assert [match["path"] for match in grep.raw_output["matches"]] == [
        ".github/workflows/ci.yml"
    ]


@pytest.mark.asyncio
async def test_grep_reports_character_columns_for_non_ascii_lines(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("# 中文注释 needle\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle")

    assert result.success is True
    assert result.raw_output["matches"][0]["column"] == 8
    assert result.content.startswith("app.py:1:8:")


_PARTIAL_EXIT_SCRIPT = (
    "import json, sys\n"
    "if '--files' in sys.argv:\n"
    "    print('src/app.py', end='\\0')\n"
    "else:\n"
    "    print(json.dumps({'type': 'match', 'data': {"
    "'path': {'text': 'src/app.py'}, 'line_number': 1, "
    "'lines': {'text': 'needle\\n'}, 'submatches': [{'start': 0}]}}))\n"
    "sys.exit(2)\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_cls", "pattern", "result_key"),
    [(GlobTool, "*.py", "files"), (GrepTool, "needle", "matches")],
)
async def test_search_keeps_results_when_some_paths_are_unreadable(
    tmp_path: Path, tool_cls, pattern: str, result_key: str
) -> None:
    # ripgrep exits 2 when a directory or file cannot be read, even though
    # --no-messages hides the per-path error and other matches were found.
    result = await fake_rg_tool(tool_cls, tmp_path, _PARTIAL_EXIT_SCRIPT).execute(
        pattern=pattern
    )

    assert result.success is True
    assert len(result.raw_output[result_key]) == 1
    assert result.raw_output["truncated"] is True
    assert "could not be read" in result.content
    model_content = _tool_message_content_for_model(
        tool_name="grep" if tool_cls is GrepTool else "glob",
        arguments={"pattern": pattern},
        result=result,
        visible_content=result.content,
        visible_error=None,
    )
    assert "some paths could not be read" in model_content


@pytest.mark.asyncio
async def test_search_reports_other_ripgrep_failures_as_errors(tmp_path: Path) -> None:
    result = await fake_rg_tool(
        GrepTool,
        tmp_path,
        "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n",
    ).execute(pattern="needle")

    assert result.success is False
    assert result.error == "boom"


@pytest.mark.asyncio
async def test_grep_decodes_non_utf8_lines_instead_of_dropping_matches(
    tmp_path: Path,
) -> None:
    (tmp_path / "blob.txt").write_bytes(b"\xffneedle\n")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle")

    assert result.success is True
    assert result.raw_output["matches"] == [
        {"path": "blob.txt", "line": 1, "column": 2, "text": "�needle"}
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="only Linux filesystems accept non-UTF-8 file names",
)
async def test_grep_marks_non_utf8_paths_incomplete_without_fabricating_names(
    tmp_path: Path,
) -> None:
    with open(os.path.join(os.fsencode(tmp_path), b"bad\xff.py"), "wb") as handle:
        handle.write(b"needle\n")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle")

    assert result.raw_output["matches"] == []
    assert result.raw_output["truncated"] is True
    assert "non-UTF-8 paths" in result.content
    assert "Incomplete grep results" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_cls", [GlobTool, GrepTool])
async def test_search_marks_undecodable_paths_incomplete(
    tmp_path: Path, tool_cls
) -> None:
    script = (
        "import base64, json, sys\n"
        "if '--files' in sys.argv:\n"
        "    sys.stdout.buffer.write(b'bad\\xff.py\\0')\n"
        "else:\n"
        "    print(json.dumps({'type':'match','data':{'path':{'bytes':base64.b64encode(b'bad\\xff.py').decode()},'lines':{'text':'needle\\n'},'line_number':1,'submatches':[{'start':0}]}}))\n"
    )
    result = await fake_rg_tool(tool_cls, tmp_path, script).execute(
        pattern="needle" if tool_cls is GrepTool else "*.py"
    )
    assert result.success is True
    assert result.raw_output["truncated"] is True
    assert result.raw_output.get("matches", result.raw_output.get("files")) == []
    assert "non-UTF-8 paths" in result.content
    assert "Incomplete" in (result.model_context or result.content)


@pytest.mark.asyncio
async def test_grep_marks_skipped_oversized_matching_lines_incomplete(tmp_path: Path) -> None:
    (tmp_path / "large.log").write_text(
        "needle " + "x" * 1_100_000 + "\n", encoding="utf-8"
    )
    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle")
    assert result.success is True
    assert result.raw_output["truncated"] is True
    assert "oversized" in result.content
    assert "Incomplete grep results" in result.content


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Windows forbids newlines in names")
async def test_glob_keeps_newline_bearing_file_names_intact(tmp_path: Path) -> None:
    (tmp_path / "prefix\nreported.py").write_text("", encoding="utf-8")

    result = await GlobTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="*.py")

    assert result.raw_output["files"] == ["prefix\nreported.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_cls", "kwargs"),
    [
        (GlobTool, {"pattern": "*", "path": ".git"}),
        (GrepTool, {"pattern": "needle", "path": ".git"}),
        (GrepTool, {"pattern": "needle", "path": ".git/config"}),
    ],
)
async def test_search_rejects_git_metadata_as_search_root(
    tmp_path: Path, tool_cls, kwargs
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("needle\n", encoding="utf-8")

    result = await tool_cls(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(**kwargs)

    assert result.success is False
    assert ".git" in (result.error or "")


@pytest.mark.asyncio
async def test_glob_and_grep_honor_escaped_braces(tmp_path: Path) -> None:
    (tmp_path / "foo{a,b}.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "fooa.py").write_text("needle\n", encoding="utf-8")
    rg = require_rg()

    literal = await GlobTool(workspace_dir=str(tmp_path), executable=rg).execute(
        pattern=r"foo\{a,b\}.py"
    )
    alternatives = await GlobTool(
        workspace_dir=str(tmp_path), executable=rg
    ).execute(pattern="foo{a,b}.py")
    grep = await GrepTool(workspace_dir=str(tmp_path), executable=rg).execute(
        pattern="needle", include=r"foo\{a,b\}.py"
    )

    assert literal.raw_output["files"] == ["foo{a,b}.py"]
    assert alternatives.raw_output["files"] == ["fooa.py"]
    assert [match["path"] for match in grep.raw_output["matches"]] == ["foo{a,b}.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_class", [GlobTool, GrepTool])
@pytest.mark.parametrize("prefix", ["", "src/"])
@pytest.mark.parametrize(
    ("file_pattern", "expected"),
    [
        ("[^a].py", {"src/b.py", "src/!.py"}),
        (r"[\!a].py", {"src/!.py", "src/a.py"}),
        (r"[\!].py", {"src/!.py"}),
    ],
)
async def test_search_character_classes_keep_ripgrep_semantics(
    tmp_path: Path, tool_class, prefix: str, file_pattern: str, expected: set[str]
) -> None:
    (tmp_path / "src").mkdir()
    for name in ("a.py", "b.py", "!.py"):
        (tmp_path / "src" / name).write_text("needle\n", encoding="utf-8")
    tool = tool_class(workspace_dir=str(tmp_path), executable=require_rg())

    if tool_class is GlobTool:
        result = await tool.execute(pattern=prefix + file_pattern)
        paths = result.raw_output["files"]
    else:
        result = await tool.execute(pattern="needle", include=prefix + file_pattern)
        paths = [match["path"] for match in result.raw_output["matches"]]

    assert result.success is True
    assert result.raw_output["truncated"] is False
    assert set(paths) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("include", ["src/**/*.py", "{src/**/*.py,other/**/*.py}"])
async def test_grep_path_include_does_not_search_excluded_or_ignored_contents(
    tmp_path: Path, include: str
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / ".gitignore").write_text("src/ignored.py\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("needle\n", encoding="utf-8")
    oversized = "needle " + "x" * (2 * 1024 * 1024) + "\n"
    (tmp_path / "unrelated.py").write_text(oversized, encoding="utf-8")
    (tmp_path / "src" / "ignored.py").write_text(oversized, encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", include=include)

    assert result.success is True
    assert [match["path"] for match in result.raw_output["matches"]] == ["src/app.py"]
    assert result.raw_output["truncated"] is False
    assert "oversized" not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include", "expected"),
    [
        ("**/*.py", {"src/main.py", "src/app/main.py", "src/apple/outside.py", "other/outside.py"}),
        ("src/**/*.py", {"src/main.py", "src/app/main.py", "src/apple/outside.py"}),
        ("src/app/**/*.{py,ts}", {"src/app/main.py", "src/app/nested/module.ts"}),
        (".github/**/*.yml", {".github/workflows/ci.yml"}),
    ],
)
async def test_grep_common_recursive_includes_need_only_one_native_search(
    tmp_path: Path, include: str, expected: set[str]
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text(
        "src/app/ignored.py\nsrc/ignored-dir/\n!other/outside.py\n", encoding="utf-8",
    )
    files = [
        "src/main.py", "src/app/main.py", "src/app/nested/module.ts",
        "src/apple/outside.py", "other/outside.py", ".github/workflows/ci.yml",
        ".git/config.py", "src/app/ignored.py", "src/ignored-dir/ignored.py",
    ]
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("needle\n", encoding="utf-8")
    commands: list[list[str]] = []

    class RecordingGrep(GrepTool):
        async def _spawn(self, *, cwd, args):
            commands.append(args)
            return await super()._spawn(cwd=cwd, args=args)

    result = await RecordingGrep(
        workspace_dir=str(tmp_path), executable=require_rg(),
    ).execute(pattern="needle", include=include)

    assert result.success is True
    assert result.raw_output["truncated"] is False
    assert {match["path"] for match in result.raw_output["matches"]} == expected
    assert len(commands) == 1
    assert "--json" in commands[0]


@pytest.mark.asyncio
async def test_grep_recursive_include_keeps_prefix_siblings_out(tmp_path: Path) -> None:
    for directory in ("s", "sr", "src", "srcx", "Src", "中文"):
        path = tmp_path / directory
        path.mkdir(exist_ok=True)
        # Avoid case-only directory aliases on case-insensitive filesystems.
        name = "upper.py" if directory == "Src" else "main.py"
        (path / name).write_text("needle\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg(),
    ).execute(pattern="needle", include="src/**/*.py")

    assert result.success is True
    actual = {match["path"] for match in result.raw_output["matches"]}
    expected = {"src/main.py"}
    if (tmp_path / "src" / "upper.py").exists():
        expected.add("src/upper.py")
    assert actual == expected


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Backslash is a path separator on Windows")
async def test_grep_native_prefix_uses_actual_posix_directory_boundaries(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("needle\n", encoding="utf-8")
    oversized = "needle " + "x" * (2 * 1024 * 1024) + "\n"
    (tmp_path / "src\\fake.py").write_text(oversized, encoding="utf-8")
    (tmp_path / "src\\nested").mkdir()
    (tmp_path / "src\\nested" / "outside.py").write_text(oversized, encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg(),
    ).execute(pattern="needle", include="src/**/*.py")

    assert result.success is True
    assert result.raw_output["truncated"] is False
    assert [match["path"] for match in result.raw_output["matches"]] == ["src/real.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("filename", "include"), [("src", "src/**/*"), ("src.py", "src.py/**/*.py")])
async def test_grep_recursive_include_does_not_read_a_file_at_the_directory_prefix(
    tmp_path: Path, filename: str, include: str
) -> None:
    (tmp_path / filename).write_text("needle " + "x" * (2 * 1024 * 1024) + "\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg(),
    ).execute(pattern="needle", include=include)

    assert result.success is True
    assert result.raw_output["matches"] == []
    assert result.raw_output["truncated"] is False


@pytest.mark.asyncio
async def test_grep_recursive_include_respects_an_ignored_directory_prefix(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / ".gitignore").write_text("src/\n", encoding="utf-8")
    (tmp_path / "src" / "ignored.py").write_text("needle\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg(),
    ).execute(pattern="needle", include="src/**/*.py")

    assert result.success is True
    assert result.raw_output["matches"] == []
    assert result.raw_output["truncated"] is False


@pytest.mark.asyncio
async def test_grep_recursive_filters_fall_back_when_windows_command_budget_is_tight(
    tmp_path: Path, monkeypatch
) -> None:
    from subprocess import list2cmdline
    import box_agent.tools.ripgrep_tool as ripgrep_tool

    monkeypatch.setattr(ripgrep_tool, "_MAX_SEARCH_COMMAND_CHARS", 400)
    folder = tmp_path / "source" / "app" / "deep"
    folder.mkdir(parents=True)
    (folder / "main.py").write_text("needle\n", encoding="utf-8")
    commands: list[list[str]] = []

    class RecordingGrep(GrepTool):
        async def _spawn(self, *, cwd, args):
            commands.append([self.executable, *args])
            return await super()._spawn(cwd=cwd, args=args)

    result = await RecordingGrep(
        workspace_dir=str(tmp_path), executable=require_rg(),
    ).execute(pattern="needle", include="source/app/deep/**/*.py")

    assert result.success is True
    assert result.raw_output["truncated"] is False
    assert [match["path"] for match in result.raw_output["matches"]] == ["source/app/deep/main.py"]
    assert any("--files" in args for args in commands)
    assert all(len(list2cmdline(args).encode("utf-16-le")) // 2 + 1 <= 400 for args in commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["x" * 3000, "中" * 3000], ids=["ascii", "unicode"])
async def test_grep_long_line_preview_contains_the_match(tmp_path: Path, prefix: str) -> None:
    (tmp_path / "app.js").write_text(
        prefix + "UNIQUE_NEEDLE" + "y" * 3000 + "\n", encoding="utf-8"
    )

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="UNIQUE_NEEDLE", path="app.js")

    assert result.success is True
    match = result.raw_output["matches"][0]
    assert match["column"] == 3001
    assert "UNIQUE_NEEDLE" in match["text"]
    assert "line truncated" in match["text"]
    assert len(match["text"]) < 2100
    assert "UNIQUE_NEEDLE" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 3, 10])
async def test_grep_path_include_keeps_one_result_limit_across_batches(
    tmp_path: Path, monkeypatch, limit: int
) -> None:
    import box_agent.tools.ripgrep_tool as ripgrep_tool

    monkeypatch.setattr(ripgrep_tool, "_MAX_SEARCH_TARGETS", 2)
    (tmp_path / "src").mkdir()
    for index in range(5):
        (tmp_path / "src" / f"文件 {index}.py").write_text("needle\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", include="src*/**/*.py", limit=limit)

    assert result.success is True
    paths = [match["path"] for match in result.raw_output["matches"]]
    assert len(paths) == min(limit, 5)
    assert len(set(paths)) == len(paths)
    assert result.raw_output["truncated"] is (limit < 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["needle", "["])
async def test_grep_empty_path_include_never_falls_back_to_the_workspace(
    tmp_path: Path, pattern: str
) -> None:
    (tmp_path / "outside.py").write_text("needle\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern=pattern, include="missing/**/*.py")

    if pattern == "[":
        assert result.success is False
        assert "regex" in result.error.casefold()
    else:
        assert result.success is True
        assert result.raw_output["matches"] == []
        assert result.raw_output["truncated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("include", "process_count"), [("src/**/*.py", 1), ("src*/**/*.py", 2)])
async def test_cancelling_grep_path_include_terminates_discovery_and_search(
    tmp_path: Path, include: str, process_count: int
) -> None:
    tool = fake_rg_tool(
        GrepTool, tmp_path,
        "import sys, time\n"
        "if '--files' in sys.argv:\n"
        "    for i in range(129):\n"
        "        sys.stdout.write(f'src/file{i}.py\\0')\n"
        "    sys.stdout.flush()\n"
        "while True:\n"
        "    time.sleep(1)\n",
    )
    task = asyncio.create_task(tool.execute(pattern="needle", include=include))
    try:
        for _ in range(500):
            if len(tool.spawned) == process_count:
                break
            await asyncio.sleep(0.01)
        assert len(tool.spawned) == process_count
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert all(process.returncode is not None for process in tool.spawned)


@pytest.mark.asyncio
async def test_grep_native_include_timeout_keeps_partial_results(tmp_path: Path) -> None:
    script = (
        "import json, time\n"
        "print(json.dumps({'type':'match', 'data': {'path': {'text':'src/app.py'}, "
        "'line_number':1, 'lines': {'text':'needle\\n'}, 'submatches':[{'start':0}]}}), flush=True)\n"
        "time.sleep(30)\n"
    )
    tool = fake_rg_tool(GrepTool, tmp_path, script, timeout_seconds=1.0)

    result = await tool.execute(pattern="needle", include="src/**/*.py")

    assert result.success is True
    assert result.raw_output["returned_matches"] == 1
    assert result.raw_output["truncated"] is True
    assert "timed out" in result.content
    assert len(tool.spawned) == 1
    assert tool.spawned[0].returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["file", "directory", "git_metadata"])
@pytest.mark.parametrize("include", ["src/**/*.py", "src*/**/*.py"])
async def test_grep_discovered_paths_do_not_follow_replaced_symlinks(
    tmp_path: Path, replacement: str, include: str
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    source.mkdir(parents=True)
    (source / "module.py").write_text("ordinary source\n", encoding="utf-8")
    private = tmp_path / "private"
    private.mkdir()
    (private / "module.py").write_text("PRIVATE_NEEDLE\n", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("PRIVATE_NEEDLE\n", encoding="utf-8")

    class SwappingGrep(GrepTool):
        async def _spawn(self, *, cwd, args):
            if "--json" in args:
                if replacement == "directory":
                    source.rename(workspace / "old-src")
                    _symlink_or_skip(source, str(private), directory=True)
                else:
                    target = (private / "module.py" if replacement == "file"
                              else workspace / ".git" / "config")
                    (source / "module.py").unlink()
                    _symlink_or_skip(source / "module.py", str(target), directory=False)
            return await super()._spawn(cwd=cwd, args=args)

    result = await SwappingGrep(
        workspace_dir=str(workspace), executable=require_rg(), allow_full_access=False,
    ).execute(pattern="PRIVATE_NEEDLE", include=include)

    assert result.success is True
    assert result.raw_output["matches"] == []
    assert "PRIVATE_NEEDLE" not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("discovery", ["idle", "busy"])
async def test_grep_searches_pending_candidates_before_discovery_times_out(
    tmp_path: Path, discovery: str
) -> None:
    after_candidate = (
        "    while True:\n"
        "        sys.stdout.buffer.write(b'unrelated/file.txt\\0' * 1000); sys.stdout.flush()\n"
        if discovery == "busy" else "    time.sleep(30)\n"
    )
    script = (
        "import sys, time, json\n"
        "if '--files' in sys.argv:\n"
        "    sys.stdout.write('src/app.py\\0'); sys.stdout.flush()\n"
        + after_candidate +
        "else:\n"
        "    print(json.dumps({'type':'match', 'data': {'path': {'text':'src/app.py'}, "
        "'line_number':1, 'lines': {'text':'needle\\n'}, 'submatches':[{'start':0}]}}))\n"
    )
    tool = fake_rg_tool(GrepTool, tmp_path, script, timeout_seconds=1.0)

    result = await tool.execute(pattern="needle", include="src*/**/*.py")

    assert result.success is True
    assert result.raw_output["returned_matches"] == 1
    assert result.raw_output["truncated"] is True
    assert "timed out" in result.content
    assert len(tool.spawned) == 2
    assert all(process.returncode is not None for process in tool.spawned)


@pytest.mark.asyncio
async def test_grep_batches_fit_windows_command_limit_after_literal_escaping(tmp_path: Path, monkeypatch) -> None:
    from subprocess import list2cmdline
    import box_agent.tools.ripgrep_tool as ripgrep_tool

    monkeypatch.setattr(ripgrep_tool, "_MAX_SEARCH_COMMAND_CHARS", 32767)
    (tmp_path / "src").mkdir()
    for index in range(100):
        name = "[" * 100 + str(index) + ".py"
        (tmp_path / "src" / name).write_text("needle\n", encoding="utf-8")
    commands: list[list[str]] = []

    class RecordingGrep(GrepTool):
        async def _spawn(self, *, cwd, args):
            if "--json" in args:
                commands.append([self.executable, *args])
            return await super()._spawn(cwd=cwd, args=args)

    result = await RecordingGrep(
        workspace_dir=str(tmp_path), executable=require_rg(),
    ).execute(pattern="x" * 12000 + "|needle", include="src*/**/*.py")

    assert result.success is True
    assert result.raw_output["returned_matches"] == 100
    assert result.raw_output["truncated"] is False
    assert all(len(list2cmdline(args).encode("utf-16-le")) // 2 + 1 <= 32767 for args in commands)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "decoy"),
    [
        ("[ab].py", "a.py"), ("file{a,b}.py", "filea.py"),
        ("file*.py", "file-extra.py"), ("file?.py", "file1.py"),
        ("!file.py", "file.py"), ("中 文.py", "other.py"),
        ("back\\slash.py", "slash.py"), ("line\nbreak.py", "break.py"),
    ],
)
async def test_grep_batches_keep_literal_filenames_without_duplicate_matches(
    tmp_path: Path, monkeypatch, filename: str, decoy: str
) -> None:
    import box_agent.tools.ripgrep_tool as ripgrep_tool

    if sys.platform == "win32" and any(char in filename for char in "*?\\\n"):
        pytest.skip("Windows forbids these literal filename characters")
    monkeypatch.setattr(ripgrep_tool, "_MAX_SEARCH_TARGETS", 1)
    (tmp_path / "src").mkdir()
    for name in (filename, decoy):
        (tmp_path / "src" / name).write_text("needle\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", include="src*/**/*.py")

    assert result.success is True
    assert result.raw_output["truncated"] is False
    paths = [match["path"] for match in result.raw_output["matches"]]
    assert len(paths) == 2
    assert set(paths) == {f"src/{filename}", f"src/{decoy}"}


@pytest.mark.asyncio
async def test_grep_result_limit_drains_blocked_discovery_output(tmp_path: Path) -> None:
    script = (
        "import sys, time, json\n"
        "if '--files' in sys.argv:\n"
        "    sys.stdout.buffer.write(b'src/app.py\\0' * 500000); sys.stdout.flush()\n"
        "else:\n"
        "    for i in range(2):\n"
        "        print(json.dumps({'type':'match', 'data': {'path': {'text':'src/app.py'}, "
        "'line_number':i+1, 'lines': {'text':'needle\\n'}, 'submatches':[{'start':0}]}}), flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    tool = fake_rg_tool(GrepTool, tmp_path, script)

    result = await asyncio.wait_for(
        tool.execute(pattern="needle", include="src*/**/*.py", limit=1), timeout=5.0,
    )

    assert result.raw_output["returned_matches"] == 1
    assert all(process.returncode is not None for process in tool.spawned)
    assert all(process.stdout.at_eof() for process in tool.spawned)


def _symlink_or_skip(link: Path, target: str, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # Windows without symlink rights
        pytest.skip(f"symlinks unavailable: {exc}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_cls", "path", "link_target", "directory"),
    [
        (GlobTool, "alias", ".git", True),
        (GrepTool, "alias", ".git", True),
        (GrepTool, "alias/config", ".git", True),
        (GrepTool, "cfg", ".git/config", False),
    ],
)
async def test_search_rejects_symlinks_into_git_metadata(
    tmp_path: Path, tool_cls, path: str, link_target: str, directory: bool
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("needle\n", encoding="utf-8")
    _symlink_or_skip(
        tmp_path / path.split("/")[0], link_target, directory=directory
    )

    result = await tool_cls(
        workspace_dir=str(tmp_path),
        executable=require_rg(),
        allow_full_access=False,
    ).execute(pattern="*" if tool_cls is GlobTool else "needle", path=path)

    assert result.success is False
    assert ".git" in (result.error or "")


@pytest.mark.asyncio
async def test_grep_marks_results_incomplete_when_oversized_lines_are_skipped(
    tmp_path: Path,
) -> None:
    (tmp_path / "huge.txt").write_text(
        "needle " + "x" * (2 * 1024 * 1024) + "\n", encoding="utf-8"
    )
    (tmp_path / "small.txt").write_text("needle\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle")

    assert result.success is True
    assert [match["path"] for match in result.raw_output["matches"]] == ["small.txt"]
    assert result.raw_output["truncated"] is True
    assert "oversized matching lines were skipped" in result.content
