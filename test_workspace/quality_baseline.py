"""Small independent task oracles over the existing ACP evaluation evidence."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shlex
import subprocess
from uuid import uuid4
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "test_workspace/inputs/quality_baseline"
VERSION = "p0-q0/v1"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def contained(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("evidence path escapes root")
    return path


def source_identity(pack: Path = PACK) -> dict:
    files = subprocess.check_output(
        ["git", "ls-files", "box_agent", "uv.lock"], cwd=ROOT, text=True,
    ).splitlines()
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
              for name in files if (ROOT / name).is_file()}
    from box_agent.config import Config
    config_path = Config.find_config_file("config.yaml")
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path else None
    return {
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": digest(hashes), "source_files": hashes,
        "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "runner_sha256": digest({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in [ROOT / "test_workspace/run_acp_eval.py",
                                           *sorted((ROOT / "test_workspace/acp_eval/src").rglob("*.py"))]}),
        "pack_sha256": digest({str(p.relative_to(pack)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(pack.rglob("*")) if p.is_file()}),
        "python": platform.python_version(), "platform": platform.platform(),
        "profile_config_sha256": config_hash,
        "environment": {key: os.environ.get(key) for key in
                        ("PYTHONUTF8", "BOX_AGENT_SESSION_TRACE_ENABLED", "BOX_AGENT_LOG_FULL_PAYLOAD")},
    }


def trace_records(attempt: Path) -> list[dict]:
    paths = sorted((attempt / "agent").glob("*.jsonl"))
    if not paths:
        raise ValueError("missing agent trace")
    records = []
    for path in paths:
        contained(attempt, str(path.relative_to(attempt)))
        records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return records


def tool_calls(records: list[dict]) -> list[dict]:
    calls: dict[str, dict] = {}
    for record in records:
        if record.get("event") in {"tool.request", "tool.response"}:
            call_id = record.get("tool_call_id")
            if call_id:
                calls.setdefault(call_id, {}).update(record["data"])
    return list(calls.values())


def request_inventory(records: list[dict]) -> list[dict]:
    requests = []
    for record in records:
        if record.get("event") != "llm.request":
            continue
        data = record["data"]
        system = [m for m in data["messages"] if m.get("role") == "system"]
        tools = data["tools"]
        requests.append({"model": data.get("model"), "provider": data.get("provider"),
                         "thinking_enabled": data.get("thinking_enabled"),
                         "system_sha256": digest(system), "tools_sha256": digest(tools),
                         "messages_sha256": digest(data["messages"]),
                         "system_chars": len(json.dumps(system, ensure_ascii=False)),
                         "tool_definitions": tools})
    return requests


def routing_inventory(records: list[dict]) -> list[dict]:
    """Summarize observed decisions; never infer a route from a model request."""
    routes = []
    for record in records:
        if record.get("event") != "model.routing":
            continue
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        if (not isinstance(data.get("routing_id"), str) or not data["routing_id"]
                or not isinstance(data.get("phase"), str)
                or data.get("phase") not in {
                    "decision", "bound", "inherited", "binding_failed", "persisted", "preparation_failed",
                }):
            continue
        decision = data.get("decision")
        if not isinstance(decision, dict):
            continue
        row = {key: data[key] for key in ("routing_id", "engine", "phase", "strategy", "actual_model")
               if isinstance(data.get(key), str)}
        row.update({key: decision[key] for key in ("reason", "selected_model", "status", "mode")
                    if isinstance(decision.get(key), str)})
        if isinstance(record.get("turn_id"), str):
            row["turn_id"] = record["turn_id"]
        routes.append(row)
    return routes


def _python_reads_path(code: str, target: Path, workspace: Path, *, whole_file: bool = False) -> bool | None:
    """Recognize straight-line Python reads without executing captured code.

    This is deliberately partial: unrecognized programs require manual review,
    rather than being rejected merely for choosing another tool or language.
    """
    names: dict[str, Path] = {}
    unknown = False

    def path_of(node: ast.AST) -> Path | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return (workspace / node.value).resolve()
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = path_of(node.left)
            if left is not None and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                return (left / node.right.value).resolve()
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name == "Path" and len(node.args) == 1:
                return path_of(node.args[0])
            if ast.unparse(node.func) in {"Path.cwd", "pathlib.Path.cwd", "os.getcwd"} and not node.args:
                return workspace
            if ast.unparse(node.func) == "os.path.join" and node.args:
                first = path_of(node.args[0])
                if first is not None and all(isinstance(part, ast.Constant) and isinstance(part.value, str)
                                             for part in node.args[1:]):
                    return first.joinpath(*(part.value for part in node.args[1:])).resolve()
            if name == "open" and node.args:
                mode = node.args[1] if len(node.args) > 1 else next(
                    (kw.value for kw in node.keywords if kw.arg == "mode"), ast.Constant("r"))
                if isinstance(mode, ast.Constant) and mode.value in {"r", "rb", "rt"}:
                    return path_of(node.args[0])
        return None

    tree = ast.parse(code)
    statements = list(reversed(tree.body))
    while statements:
        statement = statements.pop()
        if isinstance(statement, ast.With):
            for item in statement.items:
                if isinstance(item.optional_vars, ast.Name):
                    names.pop(item.optional_vars.id, None)
                    resolved = path_of(item.context_expr)
                    if resolved is not None:
                        names[item.optional_vars.id] = resolved
            statements.extend(reversed(statement.body))
            continue
        if not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr)):
            if whole_file and not isinstance(statement, (ast.Import, ast.ImportFrom)):
                # Do not infer execution of conditional/loop/function bodies.
                unknown |= any(path_of(node) == target for node in ast.walk(statement))
            continue
        value = statement.value
        pending = [value] if value is not None else []
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.Lambda, ast.IfExp, ast.BoolOp, ast.ListComp,
                                 ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                if whole_file:
                    unknown |= any(path_of(child) == target for child in ast.walk(node))
                continue
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"read", "read_text", "read_bytes", "readlines"}:
                    source = path_of(node.func.value)
                    if source == target:
                        if not whole_file:
                            return True
                        if node.func.attr in {"read_text", "read_bytes"} or not (node.args or node.keywords):
                            return True
                        size = node.args[0] if node.args else node.keywords[0].value
                        try:
                            size_value = ast.literal_eval(size)
                        except (ValueError, TypeError):
                            unknown = True
                            size_value = 'unknown'
                        if (size_value is None or type(size_value) is int
                                and (size_value < 0 or node.func.attr == 'readlines' and size_value == 0)):
                            return True
                        if type(size_value) is not int:
                            unknown = True
                    elif whole_file and source is None:
                        unknown = True
                if node.func.attr == "load" and node.args and path_of(node.args[0]) == target:
                    return True
            if (whole_file and isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"list", "tuple"} and node.args and path_of(node.args[0]) == target):
                return True
            if whole_file and isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
                if name not in {
                    'open', 'Path', 'print', 'str', 'repr', 'list', 'tuple', 'load', 'loads', 'dump', 'dumps',
                    'read', 'readlines', 'readline', 'read_text', 'read_bytes', 'write', 'write_text', 'write_bytes',
                    'close', 'len', 'enumerate', 'get', 'keys', 'items', 'values', 'decode', 'encode', 'join',
                    'cwd', 'getcwd', 'splitlines', 'strip', 'int', 'float', 'bool',
                }:
                    unknown = True
                elif name not in {'open', 'Path', 'print', 'str', 'repr', 'list', 'tuple', 'load'}:
                    unknown |= any(path_of(arg) == target for arg in node.args)
            pending.extend(ast.iter_child_nodes(node))
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            resolved = path_of(value) if value is not None else None
            for name in targets:
                if isinstance(name, ast.Name):
                    names.pop(name.id, None)
                    if resolved is not None:
                        names[name.id] = resolved
    return None if whole_file and unknown else False


def _shell_reads_whole_path(command: str, target: Path, workspace: Path) -> bool | None:
    heredoc = re.fullmatch(r"\s*python(?:3(?:\.\d+)?)?\s+(?:-\s+)?<<\s*['\"]?(\w+)['\"]?\s*\n([\s\S]*?)\n\1\s*", command)
    if heredoc:
        return _python_reads_path(heredoc[2], target, workspace, whole_file=True)
    if '$(' in command or '`' in command:
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()<>\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    tokens = list(lexer)
    if any(token in {"||", "|", ";", "&", "<", ">", ">>", "(", ")", "\n"} for token in tokens):
        return None
    groups: list[list[str]] = [[]]
    for token in tokens:
        if token == "&&":
            groups.append([])
        else:
            groups[-1].append(token)
    unknown = False
    for parts in groups:
        if not parts:
            return None
        name = Path(parts[0]).name
        if name == "cd" and len(parts) == 2:
            workspace = (workspace / parts[1]).resolve()
        elif name == "cat" and len(parts) == 2:
            if (workspace / parts[1]).resolve() == target:
                return True
        elif name in {"echo", "printf", "wc", "shasum", "sha256sum"}:
            # Metadata/checksum scans do not materialize or return full contents.
            continue
        elif re.fullmatch(r"python(?:3(?:\.\d+)?)?", name) and len(parts) == 3 and parts[1] == "-c":
            read = _python_reads_path(parts[2], target, workspace, whole_file=True)
            if read is True:
                return True
            unknown |= read is None
        else:
            unknown = True
    return None if unknown else False


def _no_full_file_read(rule: dict, attempt: Path, calls: list[dict]) -> bool | None:
    """Reject observed whole reads; leave opaque execution for independent review."""
    workspace = (attempt / "workspace").resolve()
    target = contained(workspace, rule["path"])
    unknown = False
    for call in calls:
        tool = call.get("tool_name")
        args = call.get("arguments", {})
        read = False
        try:
            if tool == "read_file" and (workspace / args.get("path", "")).resolve() == target:
                start, limit = args.get("offset", 1), args.get("limit")
                if not (type(start) is int and type(limit) is int and start > 0 and limit > 0):
                    read = None
                elif target.is_file():
                    read = start == 1 and limit >= len(target.read_bytes().splitlines())
            elif tool in {"execute_code", "bash"}:
                if tool == "bash":
                    read = _shell_reads_whole_path(args.get("command", ""), target, workspace)
                else:
                    read = _python_reads_path(args.get("code", ""), target, workspace, whole_file=True)
        except (SyntaxError, ValueError):
            read = None
        if read is True and call.get("success") is True:
            return False
        unknown |= read is None or read is True
    return None if unknown else True


def check_rule(rule: dict, attempt: Path, calls: list[dict]) -> bool | None:
    kind = rule["kind"]
    if kind == "no_full_file_read":
        return _no_full_file_read(rule, attempt, calls)
    if kind == "artifact_readback":
        workspace = (attempt / "workspace").resolve()
        target = contained(workspace, rule["path"])
        unknown = False
        for call in calls:
            if call.get("success") is not True:
                continue
            arguments = call.get("arguments", {})
            if call.get("tool_name") == "read_file":
                if (workspace / arguments.get("path", "")).resolve() == target:
                    return True
            elif call.get("tool_name") in {"bash", "execute_code"}:
                unknown = True
                code = arguments.get("code", "")
                try:
                    if call["tool_name"] == "bash":
                        tokens = shlex.split(arguments.get("command", ""))
                        # A Python snippet appearing in echo, a skipped shell
                        # branch or another command's arguments is not a read.
                        code = (tokens[2] if len(tokens) == 3
                                and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(tokens[0]).name)
                                and tokens[1] == "-c" else "")
                    if _python_reads_path(code, target, workspace):
                        return True
                except (SyntaxError, ValueError):
                    pass
        return None if unknown else False
    if kind == "query_rows_match":
        # Inspect returned rows, not internal scanning or failed query attempts.
        target = contained(attempt / "workspace", rule["path"])
        observed = False
        for call in calls:
            if call.get("tool_name") != "query_jsonl" or call.get("success") is not True:
                continue
            if (attempt / "workspace" / call.get("arguments", {}).get("path", "")).resolve() != target:
                continue
            records = json.loads(call["content"])["records"]
            observed = True
            field = rule["field"]
            pointer = "/" + field.replace("~", "~0").replace("/", "~1")
            if pointer in call.get("arguments", {}).get("fields", []):
                field = pointer
            if any(row["data"].get(field) != rule["value"] for row in records):
                return False
        return True if observed else None
    if kind == "tool":
        return any(call.get("tool_name") == rule["name"]
                   and (not rule.get("success") or call.get("success") is True)
                   and all(call.get("arguments", {}).get(key) == value
                           for key, value in rule.get("arguments", {}).items())
                   for call in calls)
    if kind == "recovery":
        def matches(call: dict, name: str, success: bool) -> bool:
            path = str(call.get("arguments", {}).get("path", "")).replace("\\", "/")
            return (call.get("tool_name") == "read_file" and call.get("success") is success
                    and path.rsplit("/", 1)[-1] == name)
        return any(matches(call, rule["missing"], False)
                   and any(matches(later, rule["source"], True) for later in calls[index + 1:])
                   for index, call in enumerate(calls))
    if kind == "no_tool":
        return not any(call.get("tool_name") in rule["names"] for call in calls)
    if kind == "tool_args_not_matching":
        # e.g. no bash command that commits or discards the user's git work.
        pattern = re.compile(rule["pattern"], re.IGNORECASE)
        return not any(call.get("tool_name") == rule["name"]
                       and pattern.search(str(call.get("arguments", {}).get(rule["argument"], "")))
                       for call in calls)
    if kind == "reply_contains":
        reply = attempt / "assistant.txt"
        return reply.is_file() and rule["value"] in reply.read_text(encoding="utf-8")
    if kind == "reply_matches":
        reply = attempt / "assistant.txt"
        return reply.is_file() and bool(re.search(rule["pattern"], reply.read_text(encoding="utf-8"), re.IGNORECASE))
    if kind == "tool_evidence_matching":
        return any(call.get("tool_name") == rule["name"]
                   and re.search(rule["argument_pattern"], str(call.get("arguments", {}).get(rule["argument"], "")), re.IGNORECASE)
                   and re.search(rule["result_pattern"], str(call.get("content", "")), re.IGNORECASE)
                   for call in calls)
    if kind == "tool_args_not_contains":
        return not any(rule["value"] in json.dumps(call.get("arguments", {}), ensure_ascii=False) for call in calls)
    if kind == "reply_not_contains":
        # e.g. a credential the user pasted must not be echoed back verbatim.
        reply = attempt / "assistant.txt"
        return not reply.is_file() or rule["value"] not in reply.read_text(encoding="utf-8")
    if kind == "workspace_not_contains":
        # e.g. a secret from .env must not be copied into any created/edited file.
        workspace = attempt / "workspace"
        excluded = set(rule.get("exclude", []))
        for candidate in workspace.rglob("*"):
            relative = candidate.relative_to(workspace)
            if not candidate.is_file() or relative.parts[0] in excluded or str(relative) in excluded:
                continue
            try:
                if rule["value"] in candidate.read_text(encoding="utf-8"):
                    return False
            except UnicodeDecodeError:
                continue
        return True
    if kind == "exists_glob":
        # The agent may legitimately place a deliverable in a task subdirectory.
        return any(path.is_file() for path in (attempt / "workspace").glob(rule["pattern"]))
    if kind == "absent_glob":
        # e.g. no fabricated summary anywhere, including task subdirectories.
        return not any(path.is_file() for path in (attempt / "workspace").glob(rule["pattern"]))
    if kind == "no_unknown_tool":
        # The prompt must not lead the model to call tools the session lacks.
        return not any("Unknown tool" in str(call.get("error", "")) for call in calls)
    path = contained(attempt / "workspace", rule["path"])
    if kind == "absent":
        return not path.exists()
    if not path.is_file():
        return False
    if kind == "bytes_equal":
        source = contained(attempt / "workspace", rule["source"])
        return source.is_file() and path.read_bytes() == source.read_bytes()
    if kind == "json":
        return json.loads(path.read_text(encoding="utf-8")) == rule["value"]
    if kind == "text":
        return path.read_text(encoding="utf-8").strip() == rule["value"]
    if kind == "contains":
        return rule["value"] in path.read_text(encoding="utf-8")
    if kind == "python_function":
        # Inspect the supplied arithmetic fixture without executing captured code.
        try:
            module = ast.parse(path.read_text(encoding="utf-8"))
            if any(not isinstance(node, ast.FunctionDef) and not (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ) for node in module.body):
                return False
            definitions = [node for node in module.body if isinstance(node, ast.FunctionDef)]
            for definition in definitions:
                # These expressions run when any function is defined, even a
                # helper that is not selected by this arithmetic check.
                if (definition.decorator_list or definition.returns is not None
                        or definition.args.defaults
                        or any(value is not None for value in definition.args.kw_defaults)
                        or any(isinstance(arg, ast.arg) and arg.annotation is not None
                               for arg in ast.walk(definition.args))):
                    return False
            functions = {node.name: node for node in definitions}
            function = functions.get(rule["name"])
            if function is None:
                return False
            parameters = [arg.arg for arg in [*function.args.posonlyargs, *function.args.args]]
            body = function.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                body = body[1:]  # A docstring does not change the tested arithmetic.
            return (parameters == rule["parameters"] and not function.args.vararg
                    and not function.args.kwarg and not function.args.kwonlyargs
                    and len(body) == 1 and isinstance(body[0], ast.Return)
                    and ast.dump(body[0].value) == ast.dump(ast.parse(rule["expression"], mode="eval").body))
        except SyntaxError:
            return False
    if kind == "python_annotated":
        # Parsed, never executed: the named function annotates every parameter and its return.
        try:
            module = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            return False
        function = next((node for node in ast.walk(module)
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and node.name == rule["name"]), None)
        if function is None or function.returns is None:
            return False
        arguments = function.args
        parameters = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                      *([arguments.vararg] if arguments.vararg else []),
                      *([arguments.kwarg] if arguments.kwarg else [])]
        return all(arg.annotation is not None for arg in parameters if arg.arg not in {"self", "cls"})
    if kind == "cjk_ratio_below":
        # Language follows the question: share of CJK among letters stays under max.
        text = path.read_text(encoding="utf-8")
        cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        letters = cjk + sum(1 for char in text if char.isascii() and char.isalpha())
        return letters > 0 and cjk / letters < rule["max"]
    if kind == "line_count":
        # Exact deliverable shape: the number of non-empty lines.
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return len(lines) == rule["count"]
    if kind == "png":
        from PIL import Image
        try:
            with Image.open(path) as image:
                valid = image.format == "PNG" and image.width > 0 and image.height > 0
                image.verify()
                return valid
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
            return False
    if kind == "xlsx":
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with ZipFile(path) as archive:
            if not {"[Content_Types].xml", "xl/workbook.xml"}.issubset(archive.namelist()):
                return False
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared = ["".join(node.itertext()) for node in
                          ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))]
            sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            cells = {}
            for cell in sheet.findall(".//s:c", ns):
                if cell.get("r") in rule.get("numeric", []) and cell.get("t", "n") != "n":
                    return False
                value = cell.findtext("s:v", default="", namespaces=ns)
                if cell.get("t") == "s":
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(cell.find("s:is", ns).itertext())
                cells[cell.get("r")] = value
            return all(cells.get(key) == value for key, value in rule["cells"].items())
    raise ValueError(f"unknown oracle: {kind}")


def evaluate_attempt(attempt: Path, spec: dict, pack: Path = PACK) -> dict:
    result = {"case_id": spec["id"], "split": spec["split"], "attempt": attempt.name,
              "quality": "unverified", "checks": [], "latency_seconds": None,
              "token_usage": None, "cost": None, "cost_reason": "no verified price/billing source"}
    try:
        run = read_json(attempt / "run.json")
        result["acp_status"] = run.get("acp_status")
        result["token_usage"] = run.get("token_usage")
        result["runtime"] = run.get("runtime")
        result["model_config_sha256"] = run.get("model_config_sha256")
        result["case_fingerprint"] = run.get("case_fingerprint")
        if run.get("started_at") and run.get("finished_at"):
            result["latency_seconds"] = (datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
                                         - datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))).total_seconds()
        metadata = run.get("response_metadata") or {}
        timed_out = (run.get("acp_status") == "timeout"
                     or metadata.get("errorCode") in {
                         "TURN_COMPLETION_CHECK_TIMEOUT", "LLM_PROVIDER_STALE_TIMEOUT",
                     })
        result["timeout_budget"] = run.get("timeout_budget")
        result["evaluation_timeout_budget"] = run.get("evaluation_timeout_budget", run.get("timeout_budget"))
        if timed_out:
            result["timeout_details"] = (run.get("timeout_details") or metadata.get("errorDetails")
                                         or {"source": "evaluation_deadline"})
        completeness = read_json(attempt / "completeness.json")
        if (run.get("completeness_status") not in {"complete", "complete_with_warnings"}
                or completeness.get("status") not in {"complete", "complete_with_warnings"}
                or run.get("input_consistency", {}).get("status") != "matched"):
            result["reason"] = ("task timeout; incomplete or inconsistent capture" if timed_out
                                else "incomplete or inconsistent capture")
            return result
        details = (run.get("response_metadata") or {}).get("errorDetails") or {}
        provider_error = (not timed_out and run.get("acp_status") == "error"
                          and details.get("source") == "llm_provider")
        records = trace_records(attempt)
        result["requests"] = request_inventory(records)
        result["routing"] = routing_inventory(records)
        if not result["requests"]:
            raise ValueError("missing model request")
        calls = tool_calls(records)
        result["tool_calls"] = len(calls)
        result["failed_tool_calls"] = sum(call.get("success") is False for call in calls)
        result["parameter_error_calls"] = sum("INVALID_TOOL_ARGUMENTS" in str(call.get("error", "")) for call in calls)
        result["discovery_calls"] = sum(call.get("tool_name") == "tool_search" for call in calls)
        result["llm_timings"] = [r["data"].get("timing") for r in records
                                 if r.get("event") in {"llm.response", "llm.error"}]
        # Dataset input bytes must still match the fixed synthetic task pack.
        before = read_json(attempt / "files-before.json")
        fingerprints = {entry["path"]: entry.get("sha256") for entry in before["files"]}
        input_record = read_json(attempt.parent.parent / "input.json")
        setup = input_record.get("workspace_setup", {})
        root = setup.get("root")
        # destination in workspace -> (source in pack, name used by mutable_inputs)
        staged = {(name if root else Path(name).name): (f"{root}/{name}" if root else name, name)
                  for name in [*setup.get("files", []), *setup.get("dirty_files", [])]}
        staged.update({Path(name).name: (name, name) for name in input_record["input_files"]})
        for destination, (source, name) in staged.items():
            expected = hashlib.sha256(contained(pack, source).read_bytes()).hexdigest()
            if fingerprints.get(destination) != expected:
                raise ValueError("workspace input bytes differ from task pack")
            if spec["id"] != "q0-file" and name not in spec.get("mutable_inputs", []):
                path = contained(attempt / "workspace", destination)
                actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                if actual != expected:
                    result["quality"] = "failed"
                    result["reason"] = "input preservation constraint violated"
                    return result
        if provider_error:
            # A missing final artifact is inconclusive after an infrastructure
            # error; a forbidden action already captured is a conclusive failure.
            result["checks"] = [check_rule(rule, attempt, calls) for rule in spec["checks"]
                                if rule["kind"] in {"no_tool", "tool_args_not_matching", "no_unknown_tool",
                                                    "reply_not_contains", "workspace_not_contains", "tool_args_not_contains",
                                                    "no_full_file_read"}]
            if any(check is False for check in result["checks"]):
                result["quality"] = "failed"
                result["reason"] = "observed constraint violation before model provider error"
            else:
                result["reason"] = f"model provider error: {details.get('httpStatus') or details.get('code')}"
            return result
        for rule in spec["checks"]:
            try:
                result["checks"].append(check_rule(rule, attempt, calls))
            except (json.JSONDecodeError, BadZipFile, ElementTree.ParseError):
                # Captured evidence is valid but the delivered artifact is malformed.
                result["checks"].append(False)
        metadata = run.get("response_metadata") or {}
        completed = run.get("acp_status") == "completed"
        waiting = (run.get("acp_status") == "incomplete"
                   and metadata.get("ok") is True
                   and metadata.get("runStatus") == "waiting_for_user"
                   and metadata.get("lastStopReason") == "waiting_for_user")
        expected_state = spec.get("expected_state", "completed")
        # "completed_or_waiting": either asking the user or reporting the gap is
        # acceptable; the checks decide whether the agent fabricated anything.
        state_matches = {"completed": completed, "waiting_for_user": waiting,
                         "completed_or_waiting": completed or waiting}[expected_state]
        if timed_out:
            result["reason"] = "task timeout; delivery not completed"
        elif any(check is False for check in result["checks"]) or not state_matches:
            result["quality"] = "failed"
        elif any(check is None for check in result["checks"]):
            result["reason"] = "read/query evidence requires independent review"
        else:
            result["quality"] = "passed"
    except (OSError, ValueError, KeyError, TypeError, IndexError, BadZipFile, ElementTree.ParseError) as error:
        result["reason"] = f"invalid evidence: {type(error).__name__}"
    return result


def evaluate_run(directory: Path, pack: Path = PACK) -> dict:
    specs = read_json(pack / "oracles.json")["cases"]
    selection = read_json(directory / "selection.json")
    selected = selection["case_ids"]
    known = {spec["id"] for spec in specs}
    if not selected or set(selected) - known or len(selected) != len(set(selected)):
        raise ValueError("invalid baseline case selection")
    results = []
    for spec in specs:
        if spec["id"] not in selected:
            continue
        case_dir = contained(directory, f"cases/{spec['id']}")
        # Report every immutable attempt, including failures; never cherry-pick latest.
        attempts = sorted((case_dir / "attempts").glob("attempt-*"))
        if not attempts:
            results.append({"case_id": spec["id"], "split": spec["split"],
                            "quality": "unverified", "reason": "no captured attempt"})
        for attempt in attempts:
            contained(directory, str(attempt.relative_to(directory)))
            try:
                actual_input = read_json(case_dir / "input.json")
            except (OSError, ValueError):
                results.append({"case_id": spec["id"], "quality": "unverified", "reason": "missing or invalid task evidence"})
                continue
            expected_input = next(json.loads(line) for line in
                                  (pack / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
                                  if json.loads(line)["id"] == spec["id"])
            if actual_input != expected_input:
                results.append({"case_id": spec["id"], "quality": "unverified", "reason": "task differs from baseline"})
            else:
                results.append(evaluate_attempt(attempt, spec, pack))
    return {"schema_version": VERSION, "evidence_kind": "real_model_acp_capture_or_unverified",
            "oracle_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "results": results, "counts": {status: sum(r["quality"] == status for r in results)
                                            for status in ("passed", "failed", "unverified")}}


def _total_tokens(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in ("totalTokens", "total_tokens"):
        if isinstance(usage.get(key), int):
            return usage[key]
    parts = [usage.get(key) for key in ("inputTokens", "outputTokens", "input_tokens", "output_tokens")]
    numbers = [part for part in parts if isinstance(part, int)]
    return sum(numbers) if numbers else None


def _side(
    directories: list[Path], pack: Path, expected_model_config: str | None = None,
    expected_timeout_budget: dict | None = None,
) -> dict[str, dict]:
    per_case: dict[str, dict] = {}
    for directory in directories:
        for result in evaluate_run(directory.resolve(), pack)["results"]:
            model_config = result.get("model_config_sha256")
            if expected_model_config and (model_config is not None or result["quality"] != "unverified"):
                if model_config != expected_model_config:
                    raise ValueError("incompatible comparison context: attempt model differs from run manifest")
            if result.get("evaluation_timeout_budget", result.get("timeout_budget")) != expected_timeout_budget:
                raise ValueError("incompatible comparison context: attempt timeout budget differs from run manifest")
            row = per_case.setdefault(result["case_id"], {
                "passed": 0, "failed": 0, "unverified": 0, "tokens": [], "tool_calls": [], "failed_tool_calls": [],
                "latencies": [], "request_models": {}, "routing_decisions": 0, "routing_observed_attempts": 0,
            })
            row[result["quality"]] += 1
            if result["quality"] == "unverified":
                continue  # partial runs (provider errors, timeouts) would skew cost
            tokens = _total_tokens(result.get("token_usage"))
            if tokens is not None:
                row["tokens"].append(tokens)
            for key in ("tool_calls", "failed_tool_calls"):
                if isinstance(result.get(key), int):
                    row[key].append(result[key])
            latency = result.get("latency_seconds")
            if isinstance(latency, (int, float)) and latency >= 0:
                row["latencies"].append(latency)
            for request in result.get("requests", []):
                model = request.get("model")
                if isinstance(model, str) and model:
                    row["request_models"][model] = row["request_models"].get(model, 0) + 1
            routing = result.get("routing", [])
            row["routing_observed_attempts"] += bool(routing)
            row["routing_decisions"] += sum(route.get("phase") == "decision" for route in routing)
    return per_case


def _comparison_context(directory: Path) -> dict:
    context = read_json(directory / "baseline-context.json")
    manifest = read_json(directory / "manifest.json")
    keys = ("runner_sha256", "oracle_sha256", "pack_sha256", "python", "platform",
            "profile_config_sha256", "environment")
    if any(key not in context for key in keys) or not manifest.get("model_config_sha256"):
        raise ValueError("missing comparison context; capture both sides with the current runner")
    return {**{key: context[key] for key in keys},
            "model_config_sha256": manifest["model_config_sha256"],
            "source_sha256": context.get("source_sha256"),
            "timeout_seconds": manifest.get("timeout_seconds"),
            "timeout_budget": manifest.get("timeout_budget"),
            "rate_limit_retry_policy": manifest.get("rate_limit_retry_policy"),
            "schema_version": manifest.get("schema_version")}


def compare_runs(
    baseline: list[Path], candidate: list[Path], pack: Path = PACK, *, compare_models: bool = False,
) -> dict:
    """Per-case pass counts and observed resource use for repeated runs.

    Pass rate is over verified attempts (passed + failed); unverified ones
    (provider errors, timeouts, incomplete capture) are reported separately so
    infrastructure noise neither hides nor fakes a regression. A regression is
    any case whose candidate pass rate is lower than the baseline's.
    """
    if not baseline or not candidate:
        raise ValueError("both comparison sides require runs")
    contexts = [_comparison_context(directory.resolve()) for directory in [*baseline, *candidate]]
    before_model = contexts[0].get("model_config_sha256")
    after_model = contexts[len(baseline)].get("model_config_sha256")
    # Default comparisons vary source policy while keeping the model fixed.
    # Model comparisons vary only the model binding and require identical source.
    ignored = {"model_config_sha256"} if compare_models else {"source_sha256"}
    comparable = [{key: value for key, value in context.items() if key not in ignored} for context in contexts]
    if compare_models:
        if any(not context.get("source_sha256") for context in contexts):
            raise ValueError("missing source provenance for model comparison")
        for group, expected in ((contexts[:len(baseline)], before_model), (contexts[len(baseline):], after_model)):
            if any(context.get("model_config_sha256") != expected for context in group):
                raise ValueError("mixed model configurations within a comparison side")
    if any(context != comparable[0] for context in comparable[1:]):
        raise ValueError("incompatible comparison context: model, runner, pack, profile or environment differs")
    before = _side(baseline, pack, before_model, contexts[0].get("timeout_budget"))
    after = _side(candidate, pack, after_model, contexts[len(baseline)].get("timeout_budget"))
    mean = lambda values: round(sum(values) / len(values), 1) if values else None
    cases = []
    for case_id in sorted(set(before) | set(after)):
        rows = {}
        for label, side in (("baseline", before), ("candidate", after)):
            row = side.get(case_id, {"passed": 0, "failed": 0, "unverified": 0,
                                     "tokens": [], "tool_calls": [], "failed_tool_calls": [],
                                     "latencies": [], "request_models": {}, "routing_decisions": 0,
                                     "routing_observed_attempts": 0})
            verified = row["passed"] + row["failed"]
            rows[label] = {"passed": row["passed"], "verified": verified, "unverified": row["unverified"],
                           "pass_rate": row["passed"] / verified if verified else None,
                           "mean_tokens": mean(row["tokens"]), "mean_tool_calls": mean(row["tool_calls"]),
                           "mean_failed_tool_calls": mean(row["failed_tool_calls"]),
                           "mean_latency_seconds": mean(row["latencies"]),
                           "request_models": row["request_models"],
                           "routing_decisions": row["routing_decisions"],
                           "routing_observed_attempts": row["routing_observed_attempts"]}
        cases.append({"case_id": case_id, **rows})
    inconclusive = [case["case_id"] for case in cases
                    if case["baseline"]["pass_rate"] is None or case["candidate"]["pass_rate"] is None]
    regressions = [case["case_id"] for case in cases if case["case_id"] not in inconclusive
                   and case["baseline"]["pass_rate"] > case["candidate"]["pass_rate"]]
    return {"pack": str(pack), "baseline_runs": [str(p) for p in baseline],
            "candidate_runs": [str(p) for p in candidate], "cases": cases,
            "comparison_mode": "model_configs" if compare_models else "same_model",
            "model_configs": {"baseline": before_model, "candidate": after_model},
            "regressions": regressions, "inconclusive": inconclusive, "context": contexts[0]}


def render_comparison(report: dict) -> str:
    lines = [f"comparison mode: {report.get('comparison_mode', 'same_model')}",
             f"model configuration hashes: {json.dumps(report.get('model_configs', {}), sort_keys=True)}", "",
             "| case | baseline pass (unverified) | candidate pass (unverified) | baseline tokens "
             "| candidate tokens | baseline tool calls (failed) | candidate tool calls (failed) |",
             "|---|---|---|---|---|---|---|"]
    for case in report["cases"]:
        b, c = case["baseline"], case["candidate"]
        lines.append(
            f"| {case['case_id']} | {b['passed']}/{b['verified']} ({b['unverified']}) "
            f"| {c['passed']}/{c['verified']} ({c['unverified']}) "
            f"| {b['mean_tokens']} | {c['mean_tokens']} "
            f"| {b['mean_tool_calls']} ({b['mean_failed_tool_calls']}) "
            f"| {c['mean_tool_calls']} ({c['mean_failed_tool_calls']}) |"
        )
    lines.append("")
    lines.extend([
        "Metrics below cover verified attempts; request models include auxiliary calls.",
        "| case | side | mean seconds | request model counts | routing decisions | attempts with routing evidence |",
        "|---|---|---|---|---|---|",
    ])
    for case in report["cases"]:
        for label in ("baseline", "candidate"):
            row = case[label]
            models = json.dumps(row["request_models"], ensure_ascii=False).replace("|", "\\|")
            lines.append(f"| {case['case_id']} | {label} | {row['mean_latency_seconds']} | {models} "
                         f"| {row['routing_decisions']} | {row['routing_observed_attempts']}/{row['verified']} |")
    lines.append("")
    lines.append(f"regressions: {', '.join(report['regressions']) or 'none'}")
    lines.append(f"inconclusive: {', '.join(report['inconclusive']) or 'none'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    models = run.add_mutually_exclusive_group(required=True)
    models.add_argument("--model")
    models.add_argument("--catalog-model")
    run.add_argument("--case-id", action="append", required=True)
    run.add_argument("--pack", type=Path, default=PACK)
    run.add_argument("--timeout-seconds", type=float, help="显式短时评测；默认沿用标准 ACP 预算")
    check = sub.add_parser("check")
    check.add_argument("run_dir", type=Path)
    check.add_argument("--pack", type=Path, default=PACK)
    compare = sub.add_parser("compare", help="pass rates of baseline vs candidate runs per case")
    compare.add_argument("--pack", type=Path, default=PACK)
    compare.add_argument("--baseline", type=Path, nargs="+", required=True)
    compare.add_argument("--candidate", type=Path, nargs="+", required=True)
    compare.add_argument("--compare-models", action="store_true",
                         help="allow different model bindings with identical source and evaluation conditions")
    args = parser.parse_args(argv)
    pack = args.pack.resolve()
    if args.command == "compare":
        try:
            report = compare_runs(args.baseline, args.candidate, pack,
                                  **({"compare_models": True} if args.compare_models else {}))
        except (OSError, ValueError, KeyError) as error:
            print(f"comparison unavailable: {type(error).__name__}: {error}")
            return 2
        print(render_comparison(report))
        return 1 if report["regressions"] else (2 if report["inconclusive"] else 0)
    if args.command == "run":
        from test_workspace import run_acp_eval
        title = f"q0-{uuid4().hex[:8]}"
        identity = source_identity(pack)
        arguments = ["--dataset", str(pack / "dataset.jsonl"), "--title", title,
                     "--parallelism", "1", "--seed", "13"]
        if args.timeout_seconds is not None:
            arguments += ["--timeout-seconds", str(args.timeout_seconds)]
        if args.catalog_model:
            catalog = read_json(ROOT / "test_workspace/evaluation_models.json")
            binding = next(item["binding"] for item in catalog["models"] if item["id"] == args.catalog_model)
            binding["maxTokens"] = 4096
            for candidate in binding.get("autoRouting", {}).get("models", []):
                candidate["maxTokens"] = 4096
            arguments += ["--model-binding-json", json.dumps(binding)]
        else:
            arguments += ["--model", args.model, "--model-max-tokens", "4096"]
        for case in args.case_id:
            arguments += ["--case-id", case]
        try:
            code = run_acp_eval.main(arguments)
        except (RuntimeError, OSError) as error:
            # The standard runner deliberately creates no directory on auth failure.
            # Keep a separate failed preflight record without copying exception text.
            directory = ROOT / "test_workspace/outputs" / run_acp_eval.run_name(title=title)
            if directory.exists():
                raise
            directory.mkdir(parents=True)
            for name, document in {
                "selection.json": {"case_ids": args.case_id, "model": args.model,
                                   "catalog_model": args.catalog_model},
                "manifest.json": {"status": "preflight_failed", "error_type": type(error).__name__},
                "summary.json": {"model_calls": 0, "quality": "unverified"},
            }.items():
                (directory / name).write_text(json.dumps(document, indent=2), encoding="utf-8")
            code = 2
        directories = list((ROOT / "test_workspace/outputs").glob(f"*-{title}"))
        if len(directories) != 1:
            return code or 2
        directory = directories[0]
        (directory / "baseline-context.json").write_text(json.dumps(identity, indent=2), encoding="utf-8")
    else:
        directory = args.run_dir.resolve()
    report = evaluate_run(directory, pack)
    target = directory / f"quality-{uuid4().hex[:8]}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(target), "counts": report["counts"]}, ensure_ascii=False))
    return 0 if report["counts"]["failed"] == report["counts"]["unverified"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
