"""Obsidian CLI tools for writing notes through the official desktop CLI."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult

from box_agent.user_paths import state_path

OBSIDIAN_PERMISSION_SCOPE = "external_app"
OBSIDIAN_LAUNCH_SCOPE = "obsidian_launch"
OBSIDIAN_CONFIG_ENV = "BOX_AGENT_OBSIDIAN_CONFIG"
OBSIDIAN_CLI_ENV = "BOX_AGENT_OBSIDIAN_CLI"
OBSIDIAN_APP_ENV = "BOX_AGENT_OBSIDIAN_APP"

_DEFAULT_CONFIG_PATH = state_path('config/obsidian.json')
_DEFAULT_CLI_NAME = "obsidian"
_NOTE_TITLE_RE = re.compile(r"[^A-Za-z0-9\u4e00-\u9fff._ -]+")


def obsidian_config_path() -> Path:
    return state_path("config/obsidian.json", os.environ.get(OBSIDIAN_CONFIG_ENV, str(_DEFAULT_CONFIG_PATH)))


def _launch_permission_request(reason: str) -> dict[str, Any]:
    return {
        "type": "permission_request",
        "scope": OBSIDIAN_PERMISSION_SCOPE,
        "requested_scope": OBSIDIAN_LAUNCH_SCOPE,
        "path": "",
        "reason": reason,
        "temporary_supported": True,
        "persistent_supported": False,
        "persistent_label": "",
    }


def _clean_optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _config_from_env_context(env_context: Any) -> dict[str, Any]:
    if env_context is None:
        return {}
    obsidian = getattr(env_context, "obsidian", None)
    if obsidian is None and isinstance(env_context, dict):
        obsidian = env_context.get("obsidian")
    if obsidian is None:
        return {}
    if isinstance(obsidian, dict):
        return dict(obsidian)
    return {
        "enabled": getattr(obsidian, "enabled", None),
        "vault_path": getattr(obsidian, "vault_path", None),
        "vault_name": getattr(obsidian, "vault_name", None),
        "cli_path": getattr(obsidian, "cli_path", None),
        "app_path": getattr(obsidian, "app_path", None),
        "cli_available": getattr(obsidian, "cli_available", None),
        "app_running": getattr(obsidian, "app_running", None),
    }


def load_obsidian_config(env_context: Any = None) -> dict[str, Any]:
    file_config = _read_json(obsidian_config_path())
    host_config = _config_from_env_context(env_context)
    merged = {**file_config, **{k: v for k, v in host_config.items() if v is not None}}

    cli_override = _clean_optional_str(os.environ.get(OBSIDIAN_CLI_ENV))
    app_override = _clean_optional_str(os.environ.get(OBSIDIAN_APP_ENV))
    if cli_override:
        merged["cli_path"] = cli_override
    if app_override:
        merged["app_path"] = app_override
    return merged


def _validate_config(config: dict[str, Any]) -> tuple[bool, str]:
    if config.get("enabled") is False:
        return False, "Obsidian 尚未启用。请先在 officev3 的 设置-连接数据源 中绑定 Vault。"

    vault_path = _clean_optional_str(config.get("vault_path"))
    if not vault_path:
        return False, "未找到 Obsidian Vault 配置。请先在 officev3 的 设置-连接数据源 中绑定 Vault。"
    vault = Path(vault_path).expanduser()
    if not vault.exists() or not vault.is_dir():
        return False, f"Obsidian Vault 不存在或不是目录：{vault}"
    if not (vault / ".obsidian").is_dir():
        return False, f"所配置目录不是有效 Obsidian Vault（缺少 .obsidian）：{vault}"

    cli_path = _clean_optional_str(config.get("cli_path")) or _DEFAULT_CLI_NAME
    if os.sep in cli_path or (os.altsep and os.altsep in cli_path):
        cli = Path(cli_path).expanduser()
        if not cli.exists():
            return False, f"Obsidian CLI 不存在：{cli}"
    elif config.get("cli_available") is False:
        return False, "Obsidian CLI 不可用。请在 officev3 的 设置-连接数据源 中检测或安装 CLI。"
    return True, ""


def _safe_note_path(title: str, folder: str | None = None, path: str | None = None) -> tuple[bool, str]:
    raw = (path or "").strip()
    if raw:
        candidate = raw
    else:
        cleaned = _NOTE_TITLE_RE.sub("", (title or "").strip()).strip(" .")
        if not cleaned:
            return False, "title 不能为空，且必须能生成有效 Markdown 文件名。"
        candidate = cleaned if cleaned.endswith(".md") else f"{cleaned}.md"
        if folder:
            candidate = f"{folder.strip().strip('/')}/{candidate}"

    candidate = candidate.replace("\\", "/")
    pure = Path(candidate)
    if pure.is_absolute():
        return False, "Obsidian 写入路径必须是 Vault 相对路径，不能是绝对路径。"
    candidate = candidate.strip("/")
    pure = Path(candidate)
    if any(part in ("", ".", "..") for part in pure.parts):
        return False, "Obsidian 写入路径不能包含空路径段、'.' 或 '..'。"
    if pure.suffix.lower() != ".md":
        return False, "Obsidian 写入目标必须是 .md Markdown 笔记。"
    return True, pure.as_posix()


def _encode_cli_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


def _cli_executable(config: dict[str, Any]) -> str:
    return _clean_optional_str(config.get("cli_path")) or _DEFAULT_CLI_NAME


async def _run_process(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "Obsidian CLI 命令执行超时。"
    return (
        int(proc.returncode or 0),
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def _launch_obsidian(config: dict[str, Any]) -> tuple[bool, str]:
    app_path = _clean_optional_str(config.get("app_path"))
    system = platform.system()
    if system == "Darwin":
        if app_path:
            args = ["open", app_path]
        else:
            args = ["open", "-a", "Obsidian"]
    elif app_path:
        args = [app_path]
    else:
        args = ["obsidian"]

    try:
        if system == "Darwin":
            code, _out, err = await _run_process(args, timeout=20)
            return code == 0, err
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await asyncio.sleep(0.1)
        return proc.returncode in (None, 0), ""
    except Exception as exc:
        return False, str(exc)


async def _wait_cli_ready(config: dict[str, Any], timeout: float = 30.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            code, _out, _err = await _run_process([_cli_executable(config), "version"], timeout=5)
            if code == 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


class ObsidianCliClient:
    def __init__(self, env_context: Any = None):
        self.env_context = env_context
        self._launch_approved_once = False
        self._launch_performed_once = False

    def approve_permission_request(self, permission_request: dict[str, Any]) -> None:
        if permission_request.get("scope") == OBSIDIAN_PERMISSION_SCOPE and permission_request.get("requested_scope") == OBSIDIAN_LAUNCH_SCOPE:
            self._launch_approved_once = True

    async def run(self, args: list[str]) -> ToolResult:
        config = load_obsidian_config(self.env_context)
        ok, error = _validate_config(config)
        if not ok:
            return ToolResult(success=False, error=error)

        if config.get("app_running") is False and not self._launch_performed_once:
            if not self._launch_approved_once:
                return ToolResult(
                    success=False,
                    error="需要启动 Obsidian 才能继续执行该操作。",
                    permission_request=_launch_permission_request("需要启动 Obsidian 以写入或打开笔记。"),
                )
            launched, launch_error = await _launch_obsidian(config)
            if not launched:
                return ToolResult(success=False, error=f"无法启动 Obsidian：{launch_error or 'unknown error'}")
            self._launch_performed_once = True
            await _wait_cli_ready(config)

        try:
            code, stdout, stderr = await _run_process([_cli_executable(config), *args])
        except FileNotFoundError:
            return ToolResult(
                success=False,
                error="Obsidian CLI 未找到。请在 officev3 的 设置-连接数据源 中检测或安装 CLI。",
            )
        except PermissionError:
            return ToolResult(
                success=False,
                error=f"Obsidian CLI 无法执行，请检查权限：{_cli_executable(config)}",
            )
        except OSError as exc:
            return ToolResult(success=False, error=f"Obsidian CLI 执行失败：{exc}")
        if code != 0:
            return ToolResult(success=False, error=stderr or stdout or f"Obsidian CLI 退出码 {code}")
        return ToolResult(success=True, content=stdout or "Obsidian 操作已完成。", raw_output={"argv": args})


class _ObsidianToolBase(Tool):
    parallel_safe = False

    def __init__(self, env_context: Any = None, client: ObsidianCliClient | None = None):
        self.client = client or ObsidianCliClient(env_context)

    def approve_permission_request(self, permission_request: dict[str, Any]) -> None:
        self.client.approve_permission_request(permission_request)


class ObsidianCreateNoteTool(_ObsidianToolBase):
    @property
    def name(self) -> str:
        return "obsidian_create_note"

    @property
    def description(self) -> str:
        return "在已绑定的 Obsidian Vault 中新建 Markdown 笔记。写入 Obsidian 时必须优先使用本工具，不要用 bash 直接拼 obsidian create。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "笔记标题；未提供 path 时用于生成文件名。"},
                "content": {"type": "string", "description": "要写入笔记的 Markdown 内容。"},
                "folder": {"type": "string", "description": "可选 Vault 相对文件夹。"},
                "path": {"type": "string", "description": "可选 Vault 相对 .md 路径；提供后优先于 title/folder。"},
                "overwrite": {"type": "boolean", "description": "是否覆盖同名笔记。", "default": False},
                "open_after": {"type": "boolean", "description": "写入后是否打开笔记。", "default": True},
            },
            "required": ["title", "content"],
        }

    async def execute(self, title: str, content: str, folder: str | None = None, path: str | None = None, overwrite: bool = False, open_after: bool = True) -> ToolResult:
        ok, note_path = _safe_note_path(title=title, folder=folder, path=path)
        if not ok:
            return ToolResult(success=False, error=note_path)
        if not isinstance(content, str) or not content.strip():
            return ToolResult(success=False, error="content 不能为空。")
        args = ["create", f"path={note_path}", f"content={_encode_cli_value(content)}"]
        if overwrite:
            args.append("overwrite")
        if open_after:
            args.append("open")
        result = await self.client.run(args)
        if result.success:
            result.content = f"已创建 Obsidian 笔记：{note_path}"
            result.raw_output = {"path": note_path, "operation": "create"}
        return result


class ObsidianUpdateNoteTool(_ObsidianToolBase):
    @property
    def name(self) -> str:
        return "obsidian_update_note"

    @property
    def description(self) -> str:
        return "追加、前置或覆盖更新已绑定 Obsidian Vault 中的 Markdown 笔记。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault 相对 .md 路径。"},
                "content": {"type": "string", "description": "要写入的 Markdown 内容。"},
                "mode": {"type": "string", "enum": ["append", "prepend", "overwrite"], "default": "append"},
                "inline": {"type": "boolean", "description": "append/prepend 时是否以内联方式写入。", "default": False},
                "open_after": {"type": "boolean", "description": "写入后是否打开笔记。", "default": True},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, mode: str = "append", inline: bool = False, open_after: bool = True) -> ToolResult:
        ok, note_path = _safe_note_path(title="", path=path)
        if not ok:
            return ToolResult(success=False, error=note_path)
        if not isinstance(content, str) or not content.strip():
            return ToolResult(success=False, error="content 不能为空。")
        mode = (mode or "append").strip()
        if mode not in {"append", "prepend", "overwrite"}:
            return ToolResult(success=False, error="mode 必须是 append、prepend 或 overwrite。")
        if mode == "overwrite":
            args = ["create", f"path={note_path}", f"content={_encode_cli_value(content)}", "overwrite"]
            if open_after:
                args.append("open")
            result = await self.client.run(args)
        else:
            args = [mode, f"path={note_path}", f"content={_encode_cli_value(content)}"]
            if inline:
                args.append("inline")
            result = await self.client.run(args)
            if result.success and open_after:
                result = await self.client.run(["open", f"path={note_path}"])
        if result.success:
            result.content = f"已更新 Obsidian 笔记：{note_path}"
            result.raw_output = {"path": note_path, "operation": mode}
        return result


class ObsidianDailyNoteTool(_ObsidianToolBase):
    @property
    def name(self) -> str:
        return "obsidian_daily_note"

    @property
    def description(self) -> str:
        return "打开、读取、追加或前置写入 Obsidian Daily Note。适合'生成今天的 todo 到 Obsidian'这类请求。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["open", "read", "append", "prepend"], "default": "open"},
                "content": {"type": "string", "description": "append/prepend 时要写入的 Markdown 内容。"},
                "inline": {"type": "boolean", "default": False},
                "open_after": {"type": "boolean", "default": True},
            },
            "required": [],
        }

    async def execute(self, action: str = "open", content: str | None = None, inline: bool = False, open_after: bool = True) -> ToolResult:
        action = (action or "open").strip()
        if action not in {"open", "read", "append", "prepend"}:
            return ToolResult(success=False, error="action 必须是 open、read、append 或 prepend。")
        if action in {"append", "prepend"} and (not isinstance(content, str) or not content.strip()):
            return ToolResult(success=False, error=f"action={action} 时 content 不能为空。")

        if action == "open":
            args = ["daily"]
            result = await self.client.run(args)
        elif action == "read":
            args = ["daily:read"]
            result = await self.client.run(args)
        else:
            args = [f"daily:{action}", f"content={_encode_cli_value(content or '')}"]
            if inline:
                args.append("inline")
            result = await self.client.run(args)
            if result.success and open_after:
                result = await self.client.run(["daily"])
        if result.success:
            result.raw_output = {"operation": f"daily:{action}"}
        return result


def obsidian_tool_registrations(env_context: Any = None) -> Iterator[tuple[str, Callable[[], Tool]]]:
    """Declare the same tools for resource-free preflight and construction."""
    yield "obsidian_create_note", lambda: ObsidianCreateNoteTool(env_context=env_context)
    yield "obsidian_update_note", lambda: ObsidianUpdateNoteTool(env_context=env_context)
    yield "obsidian_daily_note", lambda: ObsidianDailyNoteTool(env_context=env_context)


def create_obsidian_tools(env_context: Any = None) -> list[Tool]:
    return [create() for _, create in obsidian_tool_registrations(env_context)]
