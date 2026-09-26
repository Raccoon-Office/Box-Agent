from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine
from box_agent.tools.file_result_adapter import _tool_message_content_for_model
from box_agent.tools.ripgrep_tool import (
    GlobTool,
    GrepTool,
    MAX_STDERR_BYTES,
    _RipgrepTool,
    resolve_ripgrep_executable,
)


RG = shutil.which("rg")


def require_rg() -> str:
    if RG is None:
        pytest.skip("ripgrep is not installed")
    return RG


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
    executable = tmp_path / "fake-rg"
    executable.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        "if any(arg.startswith('--sort') for arg in sys.argv[1:]):\n"
        "    print('sorting disables bounded streaming', file=sys.stderr)\n"
        "    raise SystemExit(9)\n"
        "if '--files' in sys.argv:\n"
        "    print('src/app.py')\n"
        "else:\n"
        "    print(json.dumps({'type': 'match', 'data': {"
        "'path': {'text': 'src/app.py'}, 'line_number': 1, "
        "'lines': {'text': 'needle\\n'}, 'submatches': [{'start': 0}]}}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    glob_result = await GlobTool(
        workspace_dir=str(tmp_path), executable=str(executable)
    ).execute(pattern="*.py")
    grep_result = await GrepTool(
        workspace_dir=str(tmp_path), executable=str(executable)
    ).execute(pattern="needle")

    assert glob_result.success is True
    assert glob_result.raw_output["files"] == ["src/app.py"]
    assert grep_result.success is True
    assert grep_result.raw_output["matches"] == [
        {"path": "src/app.py", "line": 1, "column": 1, "text": "needle"}
    ]


@pytest.mark.asyncio
async def test_cancelling_search_terminates_the_ripgrep_process(tmp_path: Path) -> None:
    executable = tmp_path / "slow-rg"
    pid_file = tmp_path / "slow-rg.pid"
    executable.write_text(
        "#!" + sys.executable + "\n"
        "import os, pathlib, time\n"
        "pathlib.Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    task = asyncio.create_task(
        GlobTool(workspace_dir=str(tmp_path), executable=str(executable)).execute(
            pattern="*.py"
        )
    )
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


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
async def test_grep_exact_file_respects_nonmatching_include(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("needle\n", encoding="utf-8")

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", path="app.py", include="*.ts")

    assert result.success is True
    assert result.raw_output["matches"] == []


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
    assert model_content == result.model_context
    assert model_content.startswith("[Incomplete grep results:")
    assert "pattern=needle" in model_content
    assert "returned=1" in model_content
    assert "Narrow path or pattern" in model_content


@pytest.mark.asyncio
async def test_grep_truncation_keeps_returned_matches_for_host_but_limits_model_sample(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text(
        "".join(f"needle {index:02}\n" for index in range(12)), encoding="utf-8"
    )

    result = await GrepTool(
        workspace_dir=str(tmp_path), executable=require_rg()
    ).execute(pattern="needle", limit=11)

    assert result.raw_output["returned_matches"] == 11
    assert all(match["text"] in result.content for match in result.raw_output["matches"])
    assert result.model_context is not None
    assert len(result.model_context.splitlines()) < len(result.content.splitlines())


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
