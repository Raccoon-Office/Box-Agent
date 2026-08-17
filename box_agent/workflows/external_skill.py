"""Generic lifecycle policy for explicitly invoked third-party Skills.

The Skill remains an opaque instruction package.  Box-Agent only owns the
host-side lifecycle: explicit selection, bounded execution, artifact handoff,
and durable pause/resume metadata.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import Any, ClassVar

from ..artifacts import artifact_scan_root
from ..config import ToolLimitsConfig
from ..loop_guards import CompletionGate, artifact_signatures_for_globs
from ..tools.base import ToolResult
from ..tools.permissions import extract_absolute_paths
from ..tools.skill_loader import Skill, SkillLoader
from ..workflow_checkpoint_store import (
    WorkflowPauseCheckpoint,
    checkpoint_resume_instruction,
)
from ..workflow_policy import WorkflowCheckpointUpdate


EXTERNAL_SKILL_WORKFLOW_KIND = "external_skill"
EXTERNAL_SKILL_CHECKPOINT_MARKER = "[BOX_AGENT_EXTERNAL_SKILL_CHECKPOINT]"
SKILL_NAME_OPTION = "skill_name"
SKILL_SOURCE_OPTION = "skill_source"
SKILL_ROOT_OPTION = "skill_root"
TASK_TEXT_OPTION = "task_text"
ARTIFACT_GLOBS_OPTION = "artifact_globs"
OBSERVED_PATHS_OPTION = "observed_paths"
LAST_FAILURES_OPTION = "last_failures"

_MAX_TASK_TEXT_CHARS = 4_000
_MAX_OBSERVED_PATHS = 12
_MAX_FAILURES = 3
_MAX_FAILURE_CHARS = 800
_DEFAULT_EXTERNAL_SKILL_LIMITS = ToolLimitsConfig().external_skill
_DEFAULT_MAX_TOOL_CALLS = _DEFAULT_EXTERNAL_SKILL_LIMITS.max_tool_calls
_COMPLETION_RESERVE_TOOL_CALLS = (
    _DEFAULT_EXTERNAL_SKILL_LIMITS.completion_reserve_calls
)
_EXPLICIT_SKILL_RE = re.compile(
    r"(?<![\w./:-])/(?P<name>[a-z0-9][a-z0-9._-]{0,127})"
    r"(?=$|[\s,，.。!！?？;；:：)）])",
    re.IGNORECASE,
)
_AUTHORING_RE = re.compile(
    r"(?:create|generate|build|author|export|render|produce|convert|"
    r"创建|制作|生成|导出|渲染|转换)",
    re.IGNORECASE,
)
_DELIVERY_FORMATS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![a-z0-9])pptx(?![a-z0-9])", re.IGNORECASE), "pptx"),
    (re.compile(r"(?<![a-z0-9])docx(?![a-z0-9])", re.IGNORECASE), "docx"),
    (re.compile(r"(?<![a-z0-9])xlsx(?![a-z0-9])", re.IGNORECASE), "xlsx"),
    (re.compile(r"(?<![a-z0-9])pdf(?![a-z0-9])", re.IGNORECASE), "pdf"),
    (re.compile(r"(?<![a-z0-9])csv(?![a-z0-9])", re.IGNORECASE), "csv"),
    (re.compile(r"(?<![a-z0-9])html?(?![a-z0-9])", re.IGNORECASE), "html"),
    (re.compile(r"(?<![a-z0-9])mp4(?![a-z0-9])", re.IGNORECASE), "mp4"),
    (re.compile(r"(?<![a-z0-9])zip(?![a-z0-9])", re.IGNORECASE), "zip"),
)


def explicit_skill_invocation_name(user_text: str) -> str | None:
    """Return the first standalone slash Skill token, if present."""
    match = _EXPLICIT_SKILL_RE.search(user_text)
    return match.group("name") if match is not None else None


def resolve_explicit_skill_invocation(
    skill_loader: SkillLoader | None,
    user_text: str,
) -> Skill | None:
    """Resolve only an installed, enabled Skill named by a standalone slash token."""
    if skill_loader is None:
        return None
    requested = explicit_skill_invocation_name(user_text)
    if requested is None:
        return None
    canonical = next(
        (name for name in skill_loader.list_skills() if name.casefold() == requested.casefold()),
        None,
    )
    if canonical is None:
        return None
    skill = skill_loader.get_skill(canonical)
    if skill is None or skill.broken:
        return None
    return skill


def infer_skill_delivery_globs(skill: Skill) -> tuple[str, ...]:
    """Infer a conservative file contract from static Skill metadata only."""
    routing_text = " ".join(
        (
            skill.name,
            " ".join(skill.keywords or ()),
            skill.description,
        )
    )
    if _AUTHORING_RE.search(routing_text) is None:
        return ()
    formats: list[str] = []
    for pattern, extension in _DELIVERY_FORMATS:
        if pattern.search(routing_text) is not None and extension not in formats:
            formats.append(extension)
    if (
        not formats
        and "presentation.authoring" in (skill.capabilities or ())
    ):
        formats.append("pptx")
    return tuple(f"output/**/*.{extension}" for extension in formats)


def external_skill_workflow_options(
    *,
    skill_name: str,
    skill_source: str,
    skill_root: str | None,
    task_text: str,
    artifact_globs: tuple[str, ...],
    observed_paths: tuple[str, ...] = (),
    last_failures: tuple[str, ...] = (),
) -> dict[str, str]:
    """Return the bounded data-only contract used for durable recovery."""
    return {
        SKILL_NAME_OPTION: skill_name.strip(),
        SKILL_SOURCE_OPTION: skill_source.strip(),
        SKILL_ROOT_OPTION: (skill_root or "").strip(),
        TASK_TEXT_OPTION: task_text.strip()[:_MAX_TASK_TEXT_CHARS],
        ARTIFACT_GLOBS_OPTION: json.dumps(artifact_globs, ensure_ascii=False),
        OBSERVED_PATHS_OPTION: json.dumps(observed_paths, ensure_ascii=False),
        LAST_FAILURES_OPTION: json.dumps(last_failures, ensure_ascii=False),
    }


def build_external_skill_completion_gate(
    *,
    user_text: str,
    workspace_dir: str | Path,
    skill: Skill,
    tool_limits: ToolLimitsConfig | None = None,
) -> CompletionGate:
    """Build a generic lifecycle gate for one explicit Skill invocation."""
    artifact_globs = infer_skill_delivery_globs(skill)
    skill_root = str(skill.skill_path.parent) if skill.skill_path is not None else None
    limits = (tool_limits or ToolLimitsConfig()).external_skill
    return CompletionGate(
        required_changed_artifact_globs=artifact_globs,
        baseline_artifact_signatures=artifact_signatures_for_globs(
            artifact_globs,
            str(workspace_dir),
        ),
        max_continuations=3,
        deadline_seconds=900.0,
        max_tool_calls=limits.max_tool_calls,
        completion_reserve_tool_calls=(
            limits.completion_reserve_calls if artifact_globs else 0
        ),
        pause_tools=frozenset({"request_user_input", "request_user_decision"}),
        workflow_checkpoint_kind=EXTERNAL_SKILL_WORKFLOW_KIND,
        workflow_options=external_skill_workflow_options(
            skill_name=skill.name,
            skill_source=skill.source,
            skill_root=skill_root,
            task_text=user_text,
            artifact_globs=artifact_globs,
        ),
    )


def build_external_skill_completion_gate_from_options(
    *,
    workspace_dir: str | Path,
    workflow_options: Mapping[str, Any],
    tool_limits: ToolLimitsConfig | None = None,
) -> CompletionGate:
    """Rebuild a generic gate from validated, data-only checkpoint options."""
    artifact_globs = _decode_string_tuple(
        _option_text(workflow_options, ARTIFACT_GLOBS_OPTION),
        limit=8,
    )
    options = external_skill_workflow_options(
        skill_name=_option_text(workflow_options, SKILL_NAME_OPTION) or "unknown",
        skill_source=_option_text(workflow_options, SKILL_SOURCE_OPTION) or "unknown",
        skill_root=_option_text(workflow_options, SKILL_ROOT_OPTION),
        task_text=_option_text(workflow_options, TASK_TEXT_OPTION) or "",
        artifact_globs=artifact_globs,
        observed_paths=_decode_string_tuple(
            _option_text(workflow_options, OBSERVED_PATHS_OPTION),
            limit=_MAX_OBSERVED_PATHS,
        ),
        last_failures=_decode_string_tuple(
            _option_text(workflow_options, LAST_FAILURES_OPTION),
            limit=_MAX_FAILURES,
        ),
    )
    limits = (tool_limits or ToolLimitsConfig()).external_skill
    return CompletionGate(
        required_changed_artifact_globs=artifact_globs,
        baseline_artifact_signatures=artifact_signatures_for_globs(
            artifact_globs,
            str(workspace_dir),
        ),
        max_continuations=3,
        deadline_seconds=900.0,
        max_tool_calls=limits.max_tool_calls,
        completion_reserve_tool_calls=(
            limits.completion_reserve_calls if artifact_globs else 0
        ),
        pause_tools=frozenset({"request_user_input", "request_user_decision"}),
        workflow_checkpoint_kind=EXTERNAL_SKILL_WORKFLOW_KIND,
        workflow_options=options,
    )


def external_skill_policy_from_options(
    *,
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
    workflow_options: Mapping[str, Any] | None,
) -> ExternalSkillRunPolicy:
    """Create the built-in host policy without executing third-party code."""
    options = workflow_options or {}
    return ExternalSkillRunPolicy(
        workspace_dir=workspace_dir,
        artifact_root_dir=artifact_root_dir,
        skill_name=_option_text(options, SKILL_NAME_OPTION),
        skill_source=_option_text(options, SKILL_SOURCE_OPTION),
        skill_root=_option_text(options, SKILL_ROOT_OPTION),
        task_text=_option_text(options, TASK_TEXT_OPTION),
        artifact_globs=_decode_string_tuple(
            _option_text(options, ARTIFACT_GLOBS_OPTION),
            limit=8,
        ),
        observed_paths=list(
            _decode_string_tuple(
                _option_text(options, OBSERVED_PATHS_OPTION),
                limit=_MAX_OBSERVED_PATHS,
            )
        ),
        last_failures=list(
            _decode_string_tuple(
                _option_text(options, LAST_FAILURES_OPTION),
                limit=_MAX_FAILURES,
            )
        ),
    )


def _option_text(options: Mapping[str, Any], key: str) -> str | None:
    value = options.get(key)
    return value if isinstance(value, str) and value else None


def _decode_string_tuple(raw: str | None, *, limit: int) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, str))[:limit]


def _is_relative_to(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(slots=True)
class ExternalSkillRunPolicy:
    """Opaque third-party Skill execution wrapped in a trusted host lifecycle."""

    workspace_dir: str | None
    artifact_root_dir: str | Path | None
    skill_name: str | None = None
    skill_source: str | None = None
    skill_root: str | None = None
    task_text: str | None = None
    artifact_globs: tuple[str, ...] = ()
    observed_paths: list[str] = field(default_factory=list)
    last_failures: list[str] = field(default_factory=list)
    stage: str | None = None
    _last_checkpoint_text: str | None = None
    _resume_checkpoint: WorkflowPauseCheckpoint | None = None

    kind: ClassVar[str] = EXTERNAL_SKILL_WORKFLOW_KIND
    checkpoint_injection_id: ClassVar[str] = EXTERNAL_SKILL_CHECKPOINT_MARKER
    evidence_read_batch_size: ClassVar[int] = 1

    def attach_resume_checkpoint(self, checkpoint: WorkflowPauseCheckpoint) -> None:
        self._resume_checkpoint = checkpoint
        options = checkpoint.workflow_options
        self.skill_name = self.skill_name or options.get(SKILL_NAME_OPTION)
        self.skill_source = self.skill_source or options.get(SKILL_SOURCE_OPTION)
        self.skill_root = self.skill_root or options.get(SKILL_ROOT_OPTION)
        self.task_text = self.task_text or options.get(TASK_TEXT_OPTION)
        if not self.artifact_globs:
            self.artifact_globs = _decode_string_tuple(
                options.get(ARTIFACT_GLOBS_OPTION),
                limit=8,
            )
        if not self.observed_paths:
            self.observed_paths.extend(
                _decode_string_tuple(
                    options.get(OBSERVED_PATHS_OPTION),
                    limit=_MAX_OBSERVED_PATHS,
                )
            )
        if not self.last_failures:
            self.last_failures.extend(
                _decode_string_tuple(
                    options.get(LAST_FAILURES_OPTION),
                    limit=_MAX_FAILURES,
                )
            )

    def checkpoint_options(self) -> dict[str, str]:
        return external_skill_workflow_options(
            skill_name=self.skill_name or "unknown",
            skill_source=self.skill_source or "unknown",
            skill_root=self.skill_root,
            task_text=self.task_text or "",
            artifact_globs=self.artifact_globs,
            observed_paths=tuple(self.observed_paths),
            last_failures=tuple(self.last_failures),
        )

    def build_checkpoint(self) -> str:
        artifact_root = artifact_scan_root(self.workspace_dir, self.artifact_root_dir)
        delivered = bool(
            artifact_root is not None
            and artifact_root.is_dir()
            and any(path.is_file() for path in artifact_root.rglob("*"))
        )
        self.stage = "artifacts_published" if delivered else "skill_active"
        paths = ", ".join(self.observed_paths) if self.observed_paths else "none"
        failures = " | ".join(self.last_failures) if self.last_failures else "none"
        globs = ", ".join(self.artifact_globs) if self.artifact_globs else "none"
        checkpoint_text = (
            f"{EXTERNAL_SKILL_CHECKPOINT_MARKER}\n"
            f"skill_name={self.skill_name or 'unknown'}\n"
            f"skill_source={self.skill_source or 'unknown'}\n"
            f"stage={self.stage}\n"
            f"task={(self.task_text or '').strip() or 'Resume the explicit Skill task.'}\n"
            f"artifact_root={artifact_root or 'none'}\n"
            f"required_artifacts={globs}\n"
            f"observed_working_paths={paths}\n"
            f"recent_failures={failures}\n"
            "This is a Box-Agent host lifecycle contract, not executable Skill code. "
            "Continue following the named Skill without modifying its installed files. "
            "The Skill may keep intermediate work in its own approved directories, but "
            "before declaring completion publish final user-facing files to artifact_root. "
            "For missing facts, call request_user_input. For a finite choice that materially "
            "changes the user-visible result, call request_user_decision instead of only asking "
            "in plain text. Internal implementation choices are yours to make. Preserve "
            "completed work and continue from the next unfinished action."
        )
        if self._resume_checkpoint is not None:
            checkpoint_text = (
                f"{checkpoint_resume_instruction(self._resume_checkpoint)}\n\n"
                f"{checkpoint_text}"
            )
            self._resume_checkpoint = None
        return checkpoint_text

    def update_checkpoint(self, checkpoint_text: str) -> WorkflowCheckpointUpdate:
        changed = checkpoint_text != self._last_checkpoint_text
        self._last_checkpoint_text = checkpoint_text
        return WorkflowCheckpointUpdate(text=checkpoint_text, changed=changed)

    def _record_candidate_path(self, raw_path: str) -> None:
        if not raw_path or len(raw_path) > 2_048:
            return
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            return
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        if not resolved.exists():
            return
        workspace = (
            Path(self.workspace_dir).expanduser().resolve()
            if self.workspace_dir
            else None
        )
        skill_root = (
            Path(self.skill_root).expanduser().resolve()
            if self.skill_root
            else None
        )
        if not (
            _is_relative_to(resolved, workspace)
            or _is_relative_to(resolved, skill_root)
        ):
            return
        rendered = str(resolved)
        if rendered not in self.observed_paths:
            self.observed_paths.append(rendered)
            del self.observed_paths[_MAX_OBSERVED_PATHS:]

    def record_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        *,
        executed: bool = True,
    ) -> None:
        for key, value in arguments.items():
            if not isinstance(value, str):
                continue
            if key in {"command", "cmd"}:
                for path in extract_absolute_paths(value):
                    self._record_candidate_path(path)
            elif key in {
                "path",
                "file_path",
                "directory",
                "cwd",
                "workspace",
                "workspace_dir",
                "output_dir",
                "project_dir",
            }:
                self._record_candidate_path(value)
        content = getattr(result, "content", None)
        if isinstance(content, str):
            for path in extract_absolute_paths(content):
                self._record_candidate_path(path)
        if not bool(getattr(result, "success", False)):
            error = str(getattr(result, "error", "") or "Tool execution failed")
            compact = " ".join(error.split())[:_MAX_FAILURE_CHARS]
            if compact:
                self.last_failures.append(f"{tool_name}: {compact}")
                self.last_failures[:] = self.last_failures[-_MAX_FAILURES:]

    def plan_scope_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        return None

    def tool_call_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        verified_evidence_urls: set[str],
        parallel: bool = False,
    ) -> str | None:
        return None

    def exempts_tool_budget(self, tool_name: str) -> bool:
        return False

    def uses_evidence_read_budget(self, tool_name: str) -> bool:
        return False

    def is_direct_evidence_read_tool(self, tool_name: str) -> bool:
        return False

    def allows_completion_continuation(self) -> bool:
        return True

    def suppresses_generic_final_summary(self) -> bool:
        return True
