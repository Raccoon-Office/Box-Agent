"""Preserve files exposed to chat, including their local dependency closure.

Session Log owns reference/release facts. Artifact sidecars are only a derived
cross-tool cache; they never establish that a file is disposable.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlsplit
import zipfile
from xml.etree import ElementTree

from ..artifact_metadata import read_metadata as read_file_metadata, legacy_metadata_path, remove_metadata
from ..artifact_publication import (
    retain_chat_file, retained_chat_file_in,
    retained_chat_files_in, ChatReferenceScanError, is_publication_metadata,
)
from .base import ToolResult
from .shell_inspection import inspect_shell_command


_CURRENT: ContextVar[tuple["ChatReferences", frozenset[Path]] | None] = ContextVar(
    "chat_references", default=None,
)
_CSS_URL = re.compile(r'''(?:url\(\s*|@import\s+)(?:"([^"]+)"|'([^']+)'|([^\s)"';]+))''', re.I)
_JS_IMPORT = re.compile(r'''(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*|\bfetch\s*\(\s*|new\s+(?:URL|Worker)\s*\(\s*)["']([^"']+)["']''')
_MARKDOWN_LINK = re.compile(r'''!?\[[^\]\n]*\]\(\s*<?([^>\n]+?)>?(?:\s+["'][^\n]*["'])?\s*\)''')
_DELETE = re.compile(r"(?:删除|删掉|移除|清空|清理|\bdelete\b|\bremove\b|\berase\b)", re.I)
_NEGATED_DELETE = re.compile(r"(?:不要|不能|不得|禁止|别|保留|避免|不再|勿|防止|\bnot\b|\bnever\b|\bkeep\b|\bprevent\b)", re.I)
_CONDITIONAL = re.compile(r"(?:如果|假如|若|例如|举例|假设|是否|能否|为什么|如何|怎么|告诉|说明|解释|分析|\bif\b|\bexample\b|\bwhether\b|\bhow\b|\bexplain\b|\bdescribe\b)", re.I)


def _css_links(text: str) -> list[str]:
    return [next(value for value in match if value) for match in _CSS_URL.findall(text)]


class _HTMLLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.base: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "base" and attrs.get("href"):
            self.base = attrs["href"]
        for key in ("src", "href", "poster", "data", "xlink:href"):
            if attrs.get(key):
                self.links.append(attrs[key])
        if attrs.get("srcset"):
            self.links.extend(part.strip().split()[0] for part in attrs["srcset"].split(",") if part.strip())
        self.links.extend(_css_links(attrs.get("style", "")))


def _local_path(value: str, base: Path, root: Path) -> Path | None:
    value = value.strip().strip("<>")
    if value.startswith("local-file://"):
        value = value[len("local-file://"):]
    if os.name == "nt" and re.match(r"^[A-Za-z]:[/\\]", value):
        path = Path(os.path.abspath(value))
        return path if path.name else None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme and parsed.scheme.lower() != "file":
        return None
    if parsed.netloc not in ("", "localhost") or not parsed.path:
        return None
    decoded = unquote(parsed.path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", decoded):
        decoded = decoded[1:]
    path = Path(decoded)
    if "\x00" in decoded or any(len(os.fsencode(part)) > 255 for part in path.parts):
        return None
    path = Path(os.path.abspath(path if path.is_absolute() else base / path))
    return path if path.name and path != root else None


def local_dependencies(path: Path, root: Path) -> set[Path]:
    """Read local links only; never execute delivered HTML, JS or documents."""
    links: list[str] = []
    base = path.parent
    if path.suffix.lower() in {".html", ".htm", ".svg", ".css", ".js", ".mjs", ".md"}:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError(f"CHAT_REFERENCE_DEPENDENCIES_TOO_LARGE: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() in {".html", ".htm", ".svg"}:
            parser = _HTMLLinks()
            parser.feed(text)
            links.extend(parser.links)
            if parser.base:
                resolved = _local_path(parser.base, base, root)
                if resolved is not None:
                    base = resolved if parser.base.endswith("/") else resolved.parent
        links.extend(_css_links(text))
        links.extend(_JS_IMPORT.findall(text))
        if path.suffix.lower() == ".md":
            links.extend(_MARKDOWN_LINK.findall(text))
            links.extend(re.findall(r"^\s*\[[^\]]+\]:\s*<?([^\n>]+)>?\s*$", text, re.M))
    elif path.suffix.lower() in {".docx", ".xlsx", ".pptx"} and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.filename.endswith(".rels"):
                    if info.file_size > 1024 * 1024:
                        raise ValueError("CHAT_REFERENCE_RELATIONSHIPS_TOO_LARGE")
                    for node in ElementTree.fromstring(archive.read(info)).iter():
                        if node.attrib.get("TargetMode") == "External" and node.attrib.get("Target"):
                            links.append(node.attrib["Target"])
    explicit = read_file_metadata(path, root).get("dependencies", [])
    if isinstance(explicit, list):
        links.extend(link for link in explicit if isinstance(link, str))
    return {candidate for link in links if (candidate := _local_path(link, base, root)) is not None}


@dataclass(frozen=True)
class Mutation:
    path: Path
    operation: str


def _python_mutations(code: str, root: Path) -> list[Mutation]:
    """Resolve literal/assigned paths; the kernel audit hook checks dynamic I/O."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    values: dict[str, str] = {}
    path_objects: set[str] = set()
    filesystem_modules = {"os", "shutil"}
    imported_calls: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            filesystem_modules.update(alias.asname or alias.name for alias in node.names if alias.name in {"os", "shutil"})
        elif isinstance(node, ast.ImportFrom) and node.module in {"os", "shutil"}:
            imported_calls.update((alias.asname or alias.name, alias.name) for alias in node.names)

    def is_path(node):
        return (
            isinstance(node, ast.Name) and node.id in path_objects
            or isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == "Path"
                or isinstance(node.func, ast.Attribute) and node.func.attr == "Path"
            )
            or isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and is_path(node.left)
        )

    def value(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            left, right = value(node.left), value(node.right)
            if left is not None and right is not None:
                return left + right if isinstance(node.op, ast.Add) else str(Path(left) / right)
        if isinstance(node, ast.Call) and node.args:
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name in {"Path", "str", "join"}:
                parts = [value(arg) for arg in node.args]
                if all(part is not None for part in parts):
                    return str(Path(*parts))
        return None

    result = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    if (resolved := value(node.value)) is not None:
                        values[target.id] = resolved
                    if is_path(node.value):
                        path_objects.add(target.id)
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if isinstance(node.func, ast.Name):
            name = imported_calls.get(name, name)
        receiver = value(node.func.value) if isinstance(node.func, ast.Attribute) and is_path(node.func.value) else None
        if name in {"remove", "rename", "replace", "move"} and isinstance(node.func, ast.Attribute):
            # Lists, strings and DataFrames have remove/replace/rename methods.
            # They are not filesystem mutations merely because names coincide.
            owner = node.func.value
            if not is_path(owner) and not (isinstance(owner, ast.Name) and owner.id in filesystem_modules):
                continue
        args = [value(arg) for arg in node.args]
        targets: list[tuple[str | None, str]] = []
        if name in {"unlink", "rmdir", "remove", "rmtree"}:
            targets = [(receiver or (args[0] if args else None), "delete")]
        elif name in {"rename", "replace", "move"}:
            targets = [(receiver or (args[0] if args else None), "move")]
        for target, operation in targets:
            if target:
                result.append(Mutation((root / target).absolute(), operation))
    return result


def _javascript_mutations(code: str, root: Path) -> list[Mutation]:
    result = []
    methods = r"unlink|rm|rmdir|rename"
    calls = list(re.finditer(r"\b(" + methods + r")(?:Sync)?\s*\(", code))
    literals = list(re.finditer(r"\b(" + methods + r")(?:Sync)?\s*\(\s*(['\"])(.*?)\2(?:\s*,\s*(['\"])(.*?)\4)?", code))
    if len(calls) != len(literals):
        result.append(Mutation(root, "unresolved"))
    for match in literals:
        method, _, source, _, _ = match.groups()
        result.append(Mutation((root / source).absolute(), "move" if method == "rename" else "delete"))
    return result


def mutations(tool_name: str, arguments: dict, root: Path, *, python_runtime=False) -> list[Mutation]:
    if tool_name == "execute_code":
        return _python_mutations(arguments.get("code", ""), root)
    if tool_name != "bash":
        return []
    from .permissions import expand_inline_shell_path_variables
    import glob

    command = expand_inline_shell_path_variables(arguments.get("command", ""))
    inspection = inspect_shell_command(command)
    cwd = root
    directories = {root}
    result = []
    for invocation in inspection.invocations:
        name = Path(invocation.executable.replace("\\", "/")).name.lower()
        args = list(invocation.arguments)
        if name in {"cd", "set-location"} and args:
            cwd = (cwd / args[-1]).resolve()
            directories.add(cwd)
            continue
        targets: list[tuple[str, str]] = []
        separator = args.index("--") if "--" in args else len(args)
        positional = [arg for i, arg in enumerate(args) if i > separator or not arg.startswith("-")]
        if name in {"rm", "rmdir", "remove-item", "del", "erase", "unlink"}:
            targets = [(arg, "delete") for arg in positional]
        elif name in {"mv", "move", "move-item", "rename", "rename-item"} and len(positional) >= 2:
            targets = [(arg, "move") for arg in positional[:-1]]
        elif name == "find" and "-delete" in args:
            targets = [(arg, "delete") for arg in args[:next((i for i, arg in enumerate(args) if arg.startswith("-")), len(args))]] or [(".", "delete")]
        elif name in {"node", "nodejs"}:
            if any(option in args for option in ("-e", "--eval")):
                index = next(args.index(option) for option in ("-e", "--eval") if option in args)
                if index + 1 < len(args):
                    for directory in directories:
                        result.extend(_javascript_mutations(args[index + 1], directory))
            elif positional:
                for directory in directories:
                    script = (directory / positional[0]).resolve()
                    if script.is_file() and script.stat().st_size <= 1024 * 1024:
                        for execution_dir in directories:
                            result.extend(_javascript_mutations(script.read_text(errors="replace"), execution_dir))
        elif name.startswith(("python", "pypy")) and "-c" in args and not python_runtime:
            index = args.index("-c")
            if index + 1 < len(args):
                for directory in directories:
                    result.extend(_python_mutations(args[index + 1], directory))
        # External Python files are not executed in their entirety. The child
        # audit hook checks actual I/O, including imports and dynamic paths.
        for target, operation in targets:
            if any(char in target for char in "$`{}"):
                result.append(Mutation(root, "unresolved"))
                continue
            for directory in directories:
                path = directory / target
                candidates = [path, *glob.glob(str(path))] if glob.has_magic(str(path)) else [path]
                result.extend(Mutation(Path(candidate).absolute(), operation) for candidate in candidates)
    for match in re.finditer(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\1(?:\n|$)", command if not python_runtime else "", re.S):
        for directory in directories:
            result.extend(_python_mutations(match[2], directory))
    return result


class ChatReferences:
    def __init__(self, workspace: str | Path, messages=(), session_log=None) -> None:
        self.root = Path(workspace).resolve()
        self.messages = messages
        self.log = session_log
        parent = _CURRENT.get()
        self._authority = parent[0] if parent else None
        self.paths: set[Path] = self._authority.paths.copy() if self._authority else set()
        self.denials: set[tuple[str, str]] = self._authority.denials if self._authority else set()
        self._restore()
        self._seen_assistant_messages = {id(message) for message in self.messages if getattr(message, "role", "") == "assistant"}
        self._check_lock = asyncio.Lock()

    def _append(self, kind: str, data: dict) -> None:
        if self.log is not None and callable(getattr(self.log, "append", None)):
            self.log.append(kind, data, ignorable=True)
            self.log.flush()

    def _references_in_text(self, content) -> set[Path]:
        if isinstance(content, list):
            paths: set[Path] = set()
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        paths.update(self._references_in_text(block.get("text", "")))
                    for key in ("image_url", "file_url", "resource", "source"):
                        value = block.get(key)
                        if isinstance(value, dict):
                            value = value.get("url") or value.get("uri") or value.get("path")
                        if isinstance(value, str) and (path := _local_path(value, self.root, self.root)) and path.is_file():
                            paths.add(path)
            return paths
        if not isinstance(content, str):
            return set()
        links = _MARKDOWN_LINK.findall(content)
        links += re.findall(r'''(?:local-file|file)://[^\s<>"'`\)\]]+''', content)
        links += re.findall(re.escape(str(self.root)) + r'''/[^\n<>"'`]{1,512}?\.[a-zA-Z0-9]{1,10}(?=[\s\]\)},;]|$)''', content)
        return {path for link in links if (path := _local_path(link, self.root, self.root)) is not None and path.is_file()}

    def _restore(self) -> None:
        events = getattr(self.log, "events", ()) if self.log is not None else ()
        legacy: set[Path] = set()
        deleted: set[Path] = set()
        temporary_calls: set[str] = set()
        for event in events if isinstance(events, (list, tuple)) else ():
            data = event.get("data", {})
            if event.get("type") == "chat/reference":
                for value in data.get("paths", []):
                    if isinstance(value, str):
                        path = Path(os.path.abspath(value))
                        if path.name and (not path.exists() or path.resolve().name):
                            self.paths.add(path)
            elif event.get("type") == "chat/reference_deleted":
                released = {Path(path).resolve() for path in data.get("paths", [])}
                self.paths.difference_update(released)
                deleted.update(released)
            elif event.get("type") in {"tool/result", "assistant/message", "user/message"}:
                message = data if event.get("type") == "user/message" else data.get("message", {})
                result = data.get("result", {})
                raw = result.get("raw_output") or {}
                if raw.get("temporary") is True:
                    temporary_calls.add(data.get("tool_call_id", message.get("tool_call_id", "")))
                    continue
                if result.get("user_visible", True):
                    legacy.update(self._references_in_text(message.get("content")))
                    if raw.get("type") == "artifact":
                        for key in ("abs_path", "absolute_path", "path", "uri"):
                            if isinstance(raw.get(key), str):
                                path = _local_path(raw[key], self.root, self.root)
                                if path and path.is_file():
                                    legacy.add(path)
        for message in self.messages:
            if getattr(message, "role", "") in {"assistant", "tool", "user"} and getattr(message, "tool_call_id", None) not in temporary_calls:
                legacy.update(self._references_in_text(message.content))
        for path in legacy - self.paths - deleted:
            self.retain(path)
        # Rebuild missing caches from durable facts, including after compaction.
        for path in self.paths:
            if path.exists():
                retain_chat_file(path, self.root)

    def retain_text(self, content) -> None:
        for path in self._references_in_text(content):
            self.retain(path)

    def retain_output(self, raw) -> None:
        if isinstance(raw, dict) and raw.get("type") == "artifact":
            for key in ("abs_path", "absolute_path", "path", "uri"):
                if isinstance(raw.get(key), str):
                    path = _local_path(raw[key], self.root, self.root)
                    if path is not None:
                        self.retain(path)

    def retain(self, path: Path) -> None:
        path = Path(os.path.abspath(path))
        if not path.is_file():
            return
        pending, closure = [path], set()
        while pending:
            candidate = pending.pop()
            if (candidate in closure or is_publication_metadata(candidate, self.root)
                    or is_publication_metadata(candidate.resolve(), self.root)):
                continue
            closure.add(candidate)
            closure.add(candidate.resolve())
            if len(closure) > 2048:
                closure.update(item.parent for item in pending)
                closure.add(candidate.parent)
                break
            if candidate.is_file():
                try:
                    pending.extend(local_dependencies(candidate, self.root) - closure)
                except (OSError, ValueError, ElementTree.ParseError, zipfile.BadZipFile):
                    # An unreadable/oversized dependency manifest must fail
                    # closed. Retain its bundle directory rather than guessing
                    # that the unresolved neighboring files are disposable.
                    closure.add(candidate.parent)
        # Root links (including symlinks and fallback parents) must not protect
        # the entire filesystem or reach the per-file metadata writer.
        closure = {candidate for candidate in closure if candidate.resolve().name}
        new = closure - self.paths
        if new:
            self._append("chat/reference", {"root": str(path), "paths": sorted(map(str, closure))})
            self.paths.update(closure)
        for candidate in closure:
            if candidate.exists():
                retain_chat_file(candidate, self.root)

    def affected(self, path: Path) -> set[Path]:
        lexical = Path(os.path.abspath(path))
        path = path.resolve()
        if path.name.startswith(".") and path.name.endswith(".artifact.json"):
            path = path.with_name(path.name[1:-len(".artifact.json")])
        result = {ref for ref in self.paths if any(ref == target or ref.is_relative_to(target) or ref.is_dir() and target.is_relative_to(ref) for target in (path, lexical))}
        if path.is_file():
            identity = path.stat()
            if identity.st_nlink > 1:
                for ref in self.paths:
                    if ref.is_file() and ref.samefile(path):
                        result.add(ref)
        if not path.name:
            return result
        result.update(retained_chat_files_in(path, self.root))
        return result

    def _user_request(self) -> str:
        if self._authority is not None:
            return self._authority._user_request()
        for message in reversed(self.messages):
            if getattr(message, "role", "") == "user" and getattr(message, "source", "user") == "user":
                content = message.content
                if isinstance(content, str):
                    return content
                return "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
        return ""

    def explicit_delete(self, path: Path) -> bool:
        # Only the current human request is authority. Tool output, assistant
        # rationale, runtime injections, stale requests and an approval flag are not.
        request = re.sub(r"```[\s\S]*?```", "", self._user_request())
        request = re.sub(r"^\s*>.*$", "", request, flags=re.M)
        for clause in re.split(r"[。！？!?;；，,\n]", request):
            clause = clause.strip()
            action = _DELETE.search(clause)
            if action and not _NEGATED_DELETE.search(clause) and not _CONDITIONAL.search(clause):
                if not (action.start() == 0 or re.match(r"^(?:请|帮我|麻烦|现在|直接|把|将|我要求|我要|please\b|can you\b|could you\b|i want\b)", clause, re.I)):
                    continue
                relative = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)
                candidates = [str(path), relative]
                if len({ref for ref in self.paths if ref.name == path.name}) <= 1:
                    candidates.append(path.name)
                for candidate in candidates:
                    if len(candidate) > 2 and re.search(r"(?<![A-Za-z0-9_./\\-])" + re.escape(candidate) + r"(?![A-Za-z0-9_./\\-])", clause):
                        return True
        return False

    def _prepare_check(self) -> None:
        if self._authority is not None:
            self.paths.update(self._authority.paths)
        # Assistant links can be emitted between tools in the same run, without
        # going through artifact discovery. Capture them before any mutation.
        for message in self.messages:
            if getattr(message, "role", "") == "assistant" and id(message) not in self._seen_assistant_messages:
                self.retain_text(message.content)
                self._seen_assistant_messages.add(id(message))

    def check(self, name: str, arguments: dict, approval: dict | None = None) -> tuple[ToolResult | None, frozenset[Path]]:
        self._prepare_check()
        return self._check_mutations(name, arguments, approval)

    def _check_mutations(self, name: str, arguments: dict, approval: dict | None) -> tuple[ToolResult | None, frozenset[Path]]:
        impacted = []
        try:
            changes = mutations(name, arguments, self.root, python_runtime=name == "bash")
            if name == "bash":
                # Literal inline deletions can still use the existing explicit
                # user confirmation. Ordinary Python branches are checked only
                # when they execute, so function definitions cannot block help.
                changes.extend(change for change in mutations(name, arguments, self.root)
                               if self.explicit_delete(change.path))
            changes = dict.fromkeys(changes)
            for change in changes:
                refs = self.paths.copy() if change.operation == "unresolved" else self.affected(change.path)
                if not refs and (retained := retained_chat_file_in(change.path, self.root)):
                    refs.add(retained)
                if refs:
                    impacted.append((change, refs))
        except ChatReferenceScanError as exc:
            return ToolResult(success=False, error=str(exc)), frozenset()
        if not impacted:
            return None, frozenset()
        request_hash = hashlib.sha256(self._user_request().encode()).hexdigest()
        if any((request_hash, str(ref)) in self.denials for _, refs in impacted for ref in refs):
            return ToolResult(success=False, error="CHAT_REFERENCE_DELETE_DENIED: the user declined; stop deletion attempts."), frozenset()
        if any(change.operation != "delete" or not self.explicit_delete(change.path) for change, _ in impacted):
            paths = sorted({str(ref) for _, refs in impacted for ref in refs})
            return ToolResult(success=False, error=(
                "CHAT_REFERENCE_PROTECTED: Cleanup skipped; files used by chat were preserved: "
                + ", ".join(paths[:5])
                + (" (and more)" if len(paths) > 5 else "")
                + ". Stop cleanup retries, including alternate tools or parent-directory deletion, "
                "and continue the task. Normal edits and appends are allowed. "
                "Put future disposable scripts in $BOX_AGENT_SCRATCH_DIR and let the session "
                "clean them. Automatic cleanup must not request deletion approval."
            )), frozenset()
        paths = {ref for _, refs in impacted for ref in refs}
        paths.update(change.path for change, _ in impacted)
        command = arguments.get("command", f"{name} " + json.dumps(arguments, sort_keys=True, ensure_ascii=False))
        fingerprints = {}
        for path in paths:
            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                fingerprints[str(path)] = digest.hexdigest()
        risk = "rm: removes files/directories" if name == "bash" else "deletes chat-referenced files"
        request = {
            "type": "permission_request", "scope": "safety", "requested_scope": "dangerous_command",
            "reason": "用户明确要求删除聊天引用的文件。确认后相关历史文件或页面依赖可能不可用。",
            "risk": risk, "command": command, "path": "; ".join(sorted(map(str, paths))),
            "temporary_supported": True, "persistent_supported": False,
            "chat_reference_delete": {
                "paths": sorted(map(str, paths)), "fingerprints": fingerprints,
                "invocation": hashlib.sha256(json.dumps([name, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "request": hashlib.sha256(self._user_request().encode()).hexdigest(),
            },
        }
        if isinstance(approval, dict) and approval.get("chat_reference_delete") == request["chat_reference_delete"]:
            return None, frozenset(paths)
        return ToolResult(success=False, error="CHAT_REFERENCE_DELETE_CONFIRMATION_REQUIRED", permission_request=request), frozenset()

    async def acheck(self, name: str, arguments: dict, approval: dict | None = None) -> tuple[ToolResult | None, frozenset[Path]]:
        """Keep cleanup scans off the event loop and serialize guard state."""
        async with self._check_lock:
            # Reference/Session Log commits stay on the run's event loop;
            # only read-only filesystem inspection runs on a worker.
            self._prepare_check()
            task = asyncio.create_task(asyncio.to_thread(self._check_mutations, name, arguments, approval))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A cancelled scan must settle before another check mutates the
                # same reference cache; it never authorizes tool execution.
                await asyncio.shield(task)
                raise

    def deleted(self, paths: frozenset[Path]) -> None:
        gone = {path for path in paths if not path.exists()}
        if gone:
            self._append("chat/reference_deleted", {"paths": sorted(map(str, gone))})
            self.paths.difference_update(gone)
            # The release is durable before removing its derived cache. Do not
            # leave an orphan marker that would protect a future unrelated file.
            for path in gone:
                remove_metadata(path, self.root)
                # Release existing legacy caches only after durable deletion.
                legacy_metadata_path(path).unlink(missing_ok=True)

    def denied(self, request: dict) -> None:
        data = request.get("chat_reference_delete", {})
        self.denials.update((data.get("request", ""), path) for path in data.get("paths", []))


@contextmanager
def bind_chat_references(guard: ChatReferences | None, allowed: frozenset[Path] = frozenset()) -> Any:
    parent = _CURRENT.get()
    if parent is not None and guard is not None:
        guard.paths.update(parent[0].paths)
    token = _CURRENT.set((guard, allowed) if guard is not None else _CURRENT.get())
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_chat_policy() -> tuple[list[str], list[str]]:
    current = _CURRENT.get()
    return (sorted(map(str, current[0].paths)), sorted(map(str, current[1]))) if current else ([], [])


def chat_mutation_error(path: Path, operation="write", workspace: str | Path | None = None) -> ToolResult | None:
    if operation == "write":
        return None
    path = path.resolve()
    current = _CURRENT.get()
    try:
        protected = current[0].affected(path) if current else set()
        retained = None if protected else retained_chat_file_in(path, current[0].root if current else workspace)
    except ChatReferenceScanError as exc:
        return ToolResult(success=False, error=str(exc))
    if not protected and retained is None:
        return None
    if operation == "delete" and current and path in current[1]:
        return None
    return ToolResult(success=False, error=f"CHAT_REFERENCE_PROTECTED: Cleanup skipped; keep {path} available to chat; do not {operation} it as cleanup. Stop cleanup retries and continue the task. Normal edits and appends are allowed. Put future disposable scripts in $BOX_AGENT_SCRATCH_DIR and let the session clean them.")


def bash_chat_mutation_error(command: str, workspace: Path, *, python_runtime=True, retention_workspace: str | Path | None = None) -> ToolResult | None:
    from .python_mutation_guard import python_guard_error

    if error := python_guard_error(command):
        return ToolResult(success=False, error=error)
    for mutation in dict.fromkeys(mutations("bash", {"command": command}, workspace, python_runtime=python_runtime)):
        if denied := chat_mutation_error(mutation.path, mutation.operation, retention_workspace or workspace):
            return denied
    return None
