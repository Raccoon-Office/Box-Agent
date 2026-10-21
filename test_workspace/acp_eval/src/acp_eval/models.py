from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Mapping

from acp_eval import SCHEMA_VERSION


SESSION_MODES = frozenset({"general", "code_agent", "data_analysis"})


@dataclass(frozen=True)
class WorkspaceSetup:
    """Project files placed in the workspace before the turn, never attached.

    ``files`` are copied (by basename) and, with ``git_commit``, committed as
    the repository baseline; ``dirty_files`` are copied afterwards so the tree
    starts with uncommitted user work. With ``root`` (a directory in the case
    pack), paths are relative to it and keep their subdirectories, so nested
    project layouts such as ``pkg/AGENTS.md`` can be staged.
    """

    files: tuple[str, ...] = ()
    git_commit: bool = False
    dirty_files: tuple[str, ...] = ()
    root: str | None = None

    def destination(self, path: str) -> str:
        """Workspace-relative destination of a staged path."""
        destination = path if self.root is not None else Path(path).name
        parts = PureWindowsPath(destination).parts
        if any(part.lower().rstrip(' .') == '.git' or part.lower().startswith('git~') for part in parts):
            raise ValueError("workspace_setup: Git control paths are forbidden")
        return destination

    def source(self, path: str) -> str:
        """Pack-relative source of a staged path."""
        return f"{self.root}/{path}" if self.root is not None else path

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "WorkspaceSetup":
        value = record.get("workspace_setup", {})
        if not isinstance(value, dict) or set(value) - {"files", "git_commit", "dirty_files", "root"}:
            raise ValueError("workspace_setup: unsupported fields or type")

        def paths(key: str) -> tuple[str, ...]:
            items = value.get(key, [])
            if not isinstance(items, list) or not all(
                isinstance(item, str) and item and "\x00" not in item for item in items
            ):
                raise ValueError(f"workspace_setup: {key} must be a list of paths")
            return tuple(items)

        git_commit = value.get("git_commit", False)
        if not isinstance(git_commit, bool):
            raise ValueError("workspace_setup: git_commit must be a boolean")
        root = value.get("root")
        if root is not None and (not isinstance(root, str) or not root or "\x00" in root
                                 or PureWindowsPath(root).is_absolute() or Path(root).is_absolute()
                                 or "\\" in root or ".." in Path(root).parts):
            raise ValueError("workspace_setup: root must be a relative directory in the pack")
        files, dirty = paths("files"), paths("dirty_files")
        if root is not None and any(Path(item).is_absolute() or PureWindowsPath(item).drive
                                    or "\\" in item or ".." in Path(item).parts
                                    for item in (*files, *dirty)):
            raise ValueError("workspace_setup: rooted paths must stay inside root")
        setup = cls(files=files, git_commit=git_commit, dirty_files=dirty, root=root)
        for item in (*files, *dirty):
            setup.destination(item)
        if setup.git_commit and not setup.files:
            raise ValueError("workspace_setup: git_commit needs files to commit")
        return setup


@dataclass(frozen=True)
class CaseMetadata:
    """Explicit ACP controls supported by offline cases; identity stays runner-owned."""

    session: Mapping[str, Any] = field(default_factory=dict)
    prompt: Mapping[str, Any] = field(default_factory=dict)
    allowed_directories: tuple[str, ...] = ()

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "CaseMetadata":
        def metadata(name: str, allowed: set[str]) -> dict[str, Any]:
            value = record.get(name, {})
            if not isinstance(value, dict) or set(value) - allowed:
                raise ValueError(f"case metadata: unsupported {name} fields or type")
            return dict(value)

        def boolean(values: Mapping[str, Any], key: str) -> None:
            if key in values and not isinstance(values[key], bool):
                raise ValueError(f"case metadata: {key} must be a boolean")

        session = metadata("session_meta", {"deep_think", "session_mode"})
        prompt = metadata("prompt_meta", {"auto_approve_plan", "selected_skill_names"})
        boolean(session, "deep_think")
        if "session_mode" in session and session["session_mode"] not in SESSION_MODES:
            raise ValueError(f"case metadata: session_mode must be one of {sorted(SESSION_MODES)}")
        boolean(prompt, "auto_approve_plan")
        if "selected_skill_names" in prompt:
            names = prompt["selected_skill_names"]
            if not isinstance(names, list) or not all(
                isinstance(name, str) and name.strip() and "\x00" not in name
                for name in names
            ):
                raise ValueError("case metadata: selected_skill_names must be a string list")
            prompt["selected_skill_names"] = list(names)
        directories = record.get("session_allowed_directories", [])
        if not isinstance(directories, list) or not all(
            isinstance(path, str) and "\x00" not in path and Path(path).is_absolute()
            for path in directories
        ):
            raise ValueError("case metadata: session_allowed_directories must be absolute paths")
        return cls(session=session, prompt=prompt, allowed_directories=tuple(directories))


@dataclass(frozen=True)
class StderrFinding:
    category: Literal["error", "timeout", "warning"]
    line_number: int
    timestamp: str | None
    text: str


@dataclass
class AttemptManifest:
    """The identity and lifecycle metadata for one captured attempt."""

    run_id: str
    case_id: str
    attempt_id: str
    started_at: str | None = None
    finished_at: str | None = None
    status: str = "starting"
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "attempt_id": self.attempt_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
        }


@dataclass
class RunResult:
    """The observed ACP/process outcome and collection status for an attempt."""

    run_id: str
    case_id: str
    attempt_id: str
    started_at: str | None = None
    finished_at: str | None = None
    acp_status: str | None = None
    process_exit_code: int | None = None
    stderr_counts: Mapping[str, int] = field(
        default_factory=lambda: {"error": 0, "timeout": 0, "warning": 0}
    )
    completeness_status: str = "incomplete"
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "attempt_id": self.attempt_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "acp_status": self.acp_status,
            "process_exit_code": self.process_exit_code,
            "stderr_counts": dict(self.stderr_counts),
            "completeness_status": self.completeness_status,
        }
