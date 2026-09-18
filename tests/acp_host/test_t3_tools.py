"""T3 — host-visible catalog / tool-call surface via real ACP subprocess."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import skip_if_llm_env_failure
from .issue_draft import to_issue_draft
from .probe import (
    AcpHostProbe,
    CaseResult,
    collect_session_updates,
    default_acp_command,
    tool_events_from_updates,
)


def _skill_has_schema_fields(skill: dict) -> bool:
    """Skills metadata acts as the host-visible catalog (name + structured fields)."""
    if not isinstance(skill.get("name"), str) or not skill["name"].strip():
        return False
    interesting = (
        "description",
        "source",
        "path",
        "allowed_tools",
        "required_skills",
        "capabilities",
    )
    return any(k in skill for k in interesting)


def _fail_case(case: CaseResult) -> None:
    draft = to_issue_draft(case)
    pytest.fail(f"{case.case_id} failed; IssueDraft:\n{draft['title']}\n{draft['body']}")


@pytest.mark.asyncio
async def test_t3_01_tool_catalog_non_empty_with_name_schema(tmp_path: Path) -> None:
    """T3-01: host-visible catalog non-empty with name/schema-like fields.

    ACP does not expose a dedicated tools/list RPC for agent tools. Hosts
    discover the capability catalog via ``session/new`` ``_meta.skills`` and
    ``_list_skills``. Each entry must have ``name`` plus structured fields.
    """
    probe = AcpHostProbe(command=default_acp_command(), cwd=tmp_path, timeout_s=60.0)
    logs: list[str] = []
    try:
        await probe.start()
        init = await probe.initialize()
        logs.append(f"initialize keys={list(init)!r}" if isinstance(init, dict) else repr(init))

        session_result = await probe.request(
            "session/new",
            {"cwd": str(tmp_path), "mcpServers": []},
        )
        assert isinstance(session_result, dict)
        session_id = session_result.get("sessionId")
        meta = session_result.get("_meta") or {}
        skills_from_session = meta.get("skills") if isinstance(meta, dict) else None
        logs.append(f"sessionId={session_id}")

        skills_from_ext = await probe.ext_request("list_skills", {})
        skills_ext_list: list = []
        if isinstance(skills_from_ext, dict):
            raw = skills_from_ext.get("skills")
            if isinstance(raw, list):
                skills_ext_list = raw

        catalog: list[dict] = []
        if isinstance(skills_from_session, list):
            catalog.extend(s for s in skills_from_session if isinstance(s, dict))
        if not catalog:
            catalog.extend(s for s in skills_ext_list if isinstance(s, dict))

        logs.append(f"catalog_count={len(catalog)} ext_count={len(skills_ext_list)}")
        if catalog:
            logs.append(f"sample={json.dumps(catalog[0], ensure_ascii=False)[:400]}")

        named = [s for s in catalog if _skill_has_schema_fields(s)]
        case = CaseResult(
            case_id="T3-01",
            ok=len(named) > 0,
            expected="Non-empty host-visible catalog entries with name + structured schema fields",
            actual=f"catalog={len(catalog)} with_schema_fields={len(named)}",
            logs="\n".join(logs) + "\n" + probe.stderr_text[-3000:],
            repro_steps=[
                "initialize + session/new",
                "inspect result._meta.skills and/or _list_skills",
                "expect non-empty list of {name, description, ...}",
            ],
        )
        if not case.ok:
            _fail_case(case)
        assert session_id
    except Exception as exc:
        if hasattr(exc, "tablename") or type(exc).__name__ in {"Failed", "XFailed"}:
            raise
        # pytest.fail raises _pytest.outcomes.Failed
        import _pytest.outcomes

        if isinstance(exc, _pytest.outcomes.Failed):
            raise
        _fail_case(
            CaseResult(
                case_id="T3-01",
                ok=False,
                expected="Non-empty host-visible catalog with name/schema",
                actual=f"{type(exc).__name__}: {exc}",
                logs="\n".join(logs) + "\n" + probe.stderr_text[-4000:],
                repro_steps=[
                    "uv run pytest tests/acp_host/test_t3_tools.py::test_t3_01_tool_catalog_non_empty_with_name_schema -q",
                ],
            )
        )
    finally:
        await probe.stop()


@pytest.mark.asyncio
async def test_t3_02_readonly_tool_succeeds_via_acp(tmp_path: Path, require_llm: str) -> None:
    """T3-02: read-only tool succeeds; result returns via ACP session/update."""
    target = tmp_path / "probe_readonly.txt"
    target.write_text("ACP_HOST_PROBE_READONLY_OK\n", encoding="utf-8")

    probe = AcpHostProbe(command=default_acp_command(), cwd=tmp_path, timeout_s=180.0)
    logs: list[str] = [f"llm={require_llm}", f"target={target}"]
    try:
        await probe.start()
        await probe.initialize()
        session_id = await probe.session_new(
            cwd=str(tmp_path),
            meta={"permission_mode": "full_access"},
        )
        probe.drain_notifications()

        prompt = (
            "Use the read_file tool to read the file probe_readonly.txt in the "
            "current workspace. After the tool returns, reply with exactly the "
            "file contents and no other commentary."
        )
        result = await probe.session_prompt(session_id, prompt)
        skip_if_llm_env_failure(probe.stderr_text, case_id="T3-02")
        notes = probe.drain_notifications()
        updates = collect_session_updates(notes)
        tool_events = tool_events_from_updates(updates)
        blob = json.dumps({"result": result, "updates": updates}, ensure_ascii=False)
        logs.append(f"stop={result.get('stopReason') if isinstance(result, dict) else None}")
        logs.append(f"tool_events={len(tool_events)}")
        logs.append(f"blob_has_marker={'ACP_HOST_PROBE_READONLY_OK' in blob}")

        case = CaseResult(
            case_id="T3-02",
            ok="ACP_HOST_PROBE_READONLY_OK" in blob,
            expected="read_file (read-only) succeeds and result is visible via ACP updates/response",
            actual=(
                f"tool_events={len(tool_events)} "
                f"marker_in_acp={('ACP_HOST_PROBE_READONLY_OK' in blob)} "
                f"stop={(result.get('stopReason') if isinstance(result, dict) else None)!r}"
            ),
            logs="\n".join(logs) + "\n" + probe.stderr_text[-4000:],
            repro_steps=[
                f"Write {target.name} in session cwd",
                "session/prompt asking the agent to read_file it",
                "Expect tool_call updates and/or message containing file contents",
            ],
        )
        if not case.ok:
            _fail_case(case)
    except Exception as exc:
        import _pytest.outcomes

        if isinstance(exc, _pytest.outcomes.Failed):
            raise
        _fail_case(
            CaseResult(
                case_id="T3-02",
                ok=False,
                expected="read-only tool succeeds; result returns via ACP",
                actual=f"{type(exc).__name__}: {exc}",
                logs="\n".join(logs) + "\n" + probe.stderr_text[-4000:],
                repro_steps=[
                    "uv run pytest tests/acp_host/test_t3_tools.py::test_t3_02_readonly_tool_succeeds_via_acp -q",
                ],
            )
        )
    finally:
        await probe.stop()


@pytest.mark.asyncio
async def test_t3_03_intentional_failing_call_error_visible(tmp_path: Path, require_llm: str) -> None:
    """T3-03: intentional failing tool call — error visible via ACP."""
    missing = tmp_path / "definitely_missing_acp_probe_file_xyz.txt"
    if missing.exists():
        missing.unlink()

    probe = AcpHostProbe(command=default_acp_command(), cwd=tmp_path, timeout_s=180.0)
    logs: list[str] = [f"llm={require_llm}", f"missing={missing}"]
    try:
        await probe.start()
        await probe.initialize()
        session_id = await probe.session_new(
            cwd=str(tmp_path),
            meta={"permission_mode": "full_access"},
        )
        probe.drain_notifications()

        prompt = (
            "Use the read_file tool exactly once to read "
            "'definitely_missing_acp_probe_file_xyz.txt'. "
            "Do not create the file. After the tool fails, briefly report the error."
        )
        result = await probe.session_prompt(session_id, prompt)
        skip_if_llm_env_failure(probe.stderr_text, case_id="T3-03")
        notes = probe.drain_notifications()
        updates = collect_session_updates(notes)
        tool_events = tool_events_from_updates(updates)
        blob = json.dumps(
            {"result": result, "updates": updates, "tools": tool_events},
            ensure_ascii=False,
        ).lower()
        logs.append(f"tool_events={len(tool_events)}")
        logs.append(f"stop={result.get('stopReason') if isinstance(result, dict) else None}")

        error_markers = (
            "error",
            "fail",
            "not found",
            "no such file",
            "does not exist",
            "errno",
            "missing",
            "unable",
            "cannot",
            "can't",
        )
        visible = any(m in blob for m in error_markers)
        for ev in tool_events:
            status = str(ev.get("status") or "").lower()
            if status in {"failed", "error"}:
                visible = True

        case = CaseResult(
            case_id="T3-03",
            ok=visible,
            expected="Failing read_file error is visible via ACP tool updates or agent message",
            actual=f"visible={visible} tool_events={len(tool_events)} blob_tail={blob[-400:]!r}",
            logs="\n".join(logs) + "\n" + probe.stderr_text[-4000:],
            repro_steps=[
                "session/prompt asking read_file on a nonexistent path",
                "Expect failed tool_call status or error text in session/update",
            ],
        )
        if not case.ok:
            _fail_case(case)
    except Exception as exc:
        import _pytest.outcomes

        if isinstance(exc, _pytest.outcomes.Failed):
            raise
        _fail_case(
            CaseResult(
                case_id="T3-03",
                ok=False,
                expected="intentional failing call; error visible via ACP",
                actual=f"{type(exc).__name__}: {exc}",
                logs="\n".join(logs) + "\n" + probe.stderr_text[-4000:],
                repro_steps=[
                    "uv run pytest tests/acp_host/test_t3_tools.py::test_t3_03_intentional_failing_call_error_visible -q",
                ],
            )
        )
    finally:
        await probe.stop()
