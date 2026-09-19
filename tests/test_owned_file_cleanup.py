import pytest

from box_agent.core import run_agent_loop
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.events import ToolCallResult
from box_agent.tools.bash_tool import BashTool
from box_agent.tools.file_tools import WriteTool


@pytest.mark.asyncio
async def test_declared_new_screenshots_can_be_cleaned_without_approval(tmp_path):
    task = tmp_path / "report"
    task.mkdir()
    tool = BashTool(workspace_dir=str(tmp_path))
    result = await tool.execute(
        command="printf image > report/top.png && printf image > report/mid.png",
        temporary_files=["report/top.png", "report/mid.png"],
    )
    assert result.success, result.error
    result = await tool.execute(command='cd ./report && rm -f top.png mid.png && ls -la')
    assert result.success, result.error
    assert result.permission_request is None
    assert list(task.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["foreign-session", "modified", "replaced", "published", "symlink", "hardlink"])
async def test_cleanup_never_adopts_foreign_changed_or_published_files(tmp_path, change):
    tool = BashTool(workspace_dir=str(tmp_path))
    path = tmp_path / "qa.png"
    result = await tool.execute(command="printf image > qa.png", temporary_files=["qa.png"])
    assert result.success
    if change == "foreign-session":
        tool = BashTool(workspace_dir=str(tmp_path))
    elif change == "modified":
        path.write_text("another task's revision")
    elif change == "replaced":
        replacement = tmp_path / "replacement"
        replacement.write_text("image")
        replacement.replace(path)
    elif change == "published":
        (tmp_path / ".qa.png.artifact.json").write_text('{"type":"artifact"}')
    elif change == "symlink":
        path.unlink()
        other = tmp_path / "user.png"
        other.write_text("user data")
        path.symlink_to(other)
    elif change == "hardlink":
        (tmp_path / "alias.png").hardlink_to(path)
    result = await tool.execute(command="rm -f qa.png")
    assert not result.success
    assert result.permission_request is not None
    assert path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "rm -rf .", "rm *.png", "rm qa.png user.txt", "rm qa.png && rm user.txt",
    "cd missing || rm qa.png", "rm qa.png; rm user.txt", "rm qa.png | sh",
    "sudo rm qa.png", "sh -c 'rm qa.png'", "rm qa.png > user.txt",
    "X=qa.png && rm $X", "rm qa.png && kill 12345",
    "cd nested/.. && rm qa.png", "rm nested/../qa.png", "cd nested && rm qa.png",
])
async def test_cleanup_exception_does_not_weaken_other_dangerous_commands(tmp_path, command):
    tool = BashTool(workspace_dir=str(tmp_path))
    (tmp_path / "user.txt").write_text("user data")
    assert (await tool.execute(command="printf image > qa.png", temporary_files=["qa.png"])).success
    result = await tool.execute(command=command)
    assert not result.success
    assert result.permission_request is not None
    assert (tmp_path / "qa.png").exists()
    assert (tmp_path / "user.txt").read_text() == "user data"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["existing.png", "../outside.png", "alias/new.png"])
async def test_temporary_declaration_cannot_claim_existing_or_escaped_paths(tmp_path, target):
    (tmp_path / "existing.png").write_text("original")
    (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
    tool = BashTool(workspace_dir=str(tmp_path))
    result = await tool.execute(command="touch executed", temporary_files=[target])
    assert not result.success
    assert not (tmp_path / "executed").exists()
    assert (tmp_path / "existing.png").read_text() == "original"


@pytest.mark.asyncio
async def test_failed_temporary_declaration_leaves_no_reserved_placeholders(tmp_path):
    (tmp_path / "existing.png").write_text("original")
    tool = BashTool(workspace_dir=str(tmp_path))
    result = await tool.execute(command="true", temporary_files=["new.png", "existing.png"])
    assert not result.success
    assert not (tmp_path / "new.png").exists()


@pytest.mark.asyncio
async def test_temporary_output_reservation_honors_filesystem_write_permission(tmp_path):
    from types import SimpleNamespace

    class ReadOnly:
        def check(self, *, capability, **kwargs):
            return SimpleNamespace(allowed=capability != "filesystem.write",
                                   reason="read-only workspace", permission_request=None)

    tool = BashTool(workspace_dir=str(tmp_path), permission_engine=ReadOnly())
    result = await tool.execute(command="true", temporary_files=["reserved.png"])
    assert not result.success
    assert "read-only workspace" in result.error
    assert not (tmp_path / "reserved.png").exists()


@pytest.mark.asyncio
async def test_cleanup_proof_does_not_bypass_revoked_write_permission(tmp_path):
    from types import SimpleNamespace
    tool = BashTool(workspace_dir=str(tmp_path))
    assert (await tool.execute(command="printf image > qa.png", temporary_files=["qa.png"])).success

    class ReadOnly:
        def check(self, **kwargs):
            return SimpleNamespace(allowed=False, reason="read-only workspace", permission_request=None)

    tool._perm = ReadOnly()
    result = await tool.execute(command="rm qa.png")
    assert not result.success
    assert "read-only workspace" in result.error
    assert (tmp_path / "qa.png").exists()


@pytest.mark.asyncio
async def test_task_cannot_reserve_a_temp_file_another_task_has_reserved(tmp_path):
    first = BashTool(workspace_dir=str(tmp_path))
    second = BashTool(workspace_dir=str(tmp_path))
    reservations = first.owned_file_cleanup.reserve(["same.png"])
    try:
        result = await second.execute(command="printf overwrite > same.png", temporary_files=["same.png"])
        assert not result.success
        assert (tmp_path / "same.png").read_bytes() == b""
        assert not second.owned_file_cleanup.files
    finally:
        first.owned_file_cleanup.discard_empty(reservations)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("temporary", [False, True])
async def test_only_explicit_temporary_creation_allows_cleanup(tmp_path, existing, temporary):
    if existing:
        (tmp_path / "part.py").write_text("original")
    bash = BashTool(workspace_dir=str(tmp_path))
    writer = WriteTool(workspace_dir=str(tmp_path))

    class LLM:
        step = 0

        async def generate_stream(self, **kwargs):
            self.step += 1
            if self.step <= 2:
                name, arguments = (("write_file", {"path": "part.py", "content": "pass", "temporary": temporary})
                                   if self.step == 1 else ("bash", {"command": "rm part.py"}))
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    ToolCall(id=str(self.step), type="function", function=FunctionCall(
                        name=name, arguments=arguments,
                    )),
                ])
            else:
                yield StreamEvent(type="finish", finish_reason="stop")

    events = [event async for event in run_agent_loop(
        llm=LLM(), tools={"write_file": writer, "bash": bash},
        messages=[Message(role="user", content="Create and clean a temporary script")],
        workspace_dir=str(tmp_path), max_steps=3,
    )]
    results = [e for e in events if isinstance(e, ToolCallResult) and e.tool_name == "bash"]
    assert results
    allowed = temporary and not existing
    assert results[0].success is allowed
    assert (tmp_path / "part.py").exists() is (not allowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("variable", ["HYPERFRAMES_BROWSER_PATH", "PRODUCER_HEADLESS_SHELL_PATH"])
async def test_verified_host_browser_executes_without_dangerous_command_prompt(tmp_path, variable):
    browser = tmp_path / "browser"
    browser.write_text("#!/bin/sh\nprintf screenshot-ok\n")
    browser.chmod(0o755)
    tool = BashTool(workspace_dir=str(tmp_path), runtime_env={variable: str(browser)})
    result = await tool.execute(command=f'"${variable}" --headless')
    assert result.success, result.error
    assert result.permission_request is None
    assert result.stdout == "screenshot-ok"
    result = await tool.execute(command=f'{variable}=rm; "${variable}" --headless')
    assert not result.success
    assert result.permission_request is not None


@pytest.mark.asyncio
async def test_unverified_host_browser_still_requires_approval(tmp_path):
    tool = BashTool(workspace_dir=str(tmp_path), runtime_env={"HYPERFRAMES_BROWSER_PATH": str(tmp_path / "missing")})
    result = await tool.execute(command='"$HYPERFRAMES_BROWSER_PATH" --headless')
    assert not result.success
    assert result.permission_request is not None
