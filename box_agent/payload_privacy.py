"""Credential filtering for diagnostic copies of agent payloads."""

import ast
import base64
import copy
import json
import logging
import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

_REDACTED = "<redacted>"
CREDENTIAL_FIELD = re.compile(
    r"(?i)(?!(?:[\w.-]+[_.-])?(?:total|input|output|prompt|completion)[_.-]token$)"
    r"(?:[\w.-]+[_.-])?(?:authorization|(?:set[_.-]?)?cookie|password\d*|pgpassword|passwd|pwd|pass|passphrase|credentials|"
    r"(?:api|access|private|secret|db|encryption|signing|ssh)[_.-]?key(?:[_.-]?id)?|"
    r"(?:(?:access|refresh|session|auth|bearer|api)[_.-]?)?token|"
    r"(?:client[_.-]?)?secret|secret[_.-]?access[_.-]?key|密码|密钥|令牌)"
)
KNOWN_TOKEN_PATTERN = (
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"glpat-[A-Za-z0-9_-]+|xox[baprs]-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]{12,}|"
    r"sk_live_[A-Za-z0-9_]+|hf_[A-Za-z0-9_]+|npm_[A-Za-z0-9_]+|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
# Providers use case-sensitive prefixes. Evidence capture uses the same
# vocabulary with its own conservative, case-insensitive policy.
_KNOWN_TOKEN = re.compile(KNOWN_TOKEN_PATTERN)
_ASSIGNMENT = re.compile(
    r'''(?<![\w.-])(?P<key>"(?:\\u[0-9a-f]{4}|[\w.-])+"|(?:api|access|private|secret)[ \t]+key|[\w.-]+)'''
    r"""(?P<separator>\\?["']?[ \t]*[:=：][ \t]*)""", re.I,
)
_VALUE = re.compile(
    # A partial provider response can end in a quoted credential.
    r"""\\\"(?:[^"\\]|\\(?!"))*(?:\\\"|\Z)|"""
    r""""(?:\\.|[^"\\])*(?:"|\\?\Z)|'(?:\\.|[^'\\])*(?:'|\\?\Z)|"""
    r"""(?:Bearer|Basic)[ \t]+[^\s,;&"'\]}]+|[^\s,;&"'\]}]+""", re.I,
)
_SECRET_FLAG = r"--(?:password|passwd|passphrase|token|api-key|credentials)"
_AUTH_COMMANDS = {"curl", "mysql", "mariadb"}
_SHELL_COMMANDS = {"sh", "bash", "dash", "zsh", "ksh"}
# Keep shell word spans so masking can preserve the caller's exact spacing.
# Quoted fragments and escaped characters belong to the same argument.
_SHELL_TOKEN = re.compile(
    r"""(?P<boundary>[\n;&|])|(?:\\[\s\S]|"(?:\\[\s\S]|[^"\\])*(?:"|\\?\Z)|"""
    r"""'[^']*(?:'|\Z)|[^\s;&|"'\\])+"""
)
# A diagnostic prefix may contain an ordinary contraction. Once a command or
# credential option is active, apostrophes retain their shell quoting meaning.
_DIAGNOSTIC_TOKEN = re.compile(r"\b[A-Za-z]+(?:'[A-Za-z]+)+(?=$|[\s.,:!?])|" + _SHELL_TOKEN.pattern)
_ARGV_ARRAY = re.compile(r'\[\s*(?:"(?:\\.|[^"\\])*"\s*,\s*)*"(?:\\.|[^"\\])*"\s*\]')
# An HTTP Cookie header can contain multiple assignments separated by ';'.
_COOKIE_LINE = re.compile(r"^([ \t]*(?:set-cookie|cookie)[ \t]*:[ \t]*)[^\r\n]*", re.I | re.M)
_COOKIE_HEADER = re.compile(
    r"""\b((?:set-cookie|cookie)[ \t]*:[ \t]*)(?:(?<==)"[^"\r\n]*"|(?<==)'[^'\r\n]*'|[^\r\n"'])+""", re.I,
)
_AUTH_TOKEN = re.compile(
    r"\b(Bearer|Basic)([ \t]+)([a-z0-9._~+/=-]+)", re.IGNORECASE | re.ASCII,
)
_URL_USERINFO = re.compile(r"""(://)[^\s/?#@"']+@""")
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*(?:"|\Z)')
_JSON_STRUCTURE = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]]')
_OPEN_CONTAINER = re.compile(r"[\[{]")
_CONTAINER_TOKEN = re.compile(
    r'''\\[\s\S]|"(?:\\[\s\S]|[^"\\])*(?:"|\\?\Z)|'(?:\\[\s\S]|[^'\\])*(?:'|\\?\Z)|[{}\[\]]'''
)
_JSON_COLON = re.compile(r"\s*:\s*")
_PEM_MARKER = re.compile(r"-----(BEGIN|END) ((?:[A-Z0-9]+ )*KEY)-----")
_SCHEMA_LISTS = frozenset(("anyOf", "oneOf", "allOf", "prefixItems", "items"))


def _credential_key(key: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", key).replace(" ", "-")
    return bool(CREDENTIAL_FIELD.fullmatch(normalized))


def _object_schema(value: Any) -> bool:
    return (isinstance(value, Mapping) and isinstance(value.get("properties"), Mapping)
            and (value.get("type") == "object" or "$schema" in value))


def _field_context(parent: str, key: str) -> str:
    """Distinguish tool schema declarations from the credentials they describe."""
    if parent == "properties":
        return "credential_schema" if _credential_key(key) else "schema"
    if parent in ("schema", "credential_schema"):
        if key in ("properties", "patternProperties", "$defs", "definitions"):
            return "properties"
        if parent == "credential_schema":
            if key in ("default", "example", "const"):
                return "credential"
            if key in ("enum", "examples"):
                return "credential_list"
        if key in _SCHEMA_LISTS or key in ("additionalProperties", "not", "if", "then", "else"):
            return parent
    if key in ("tools", "parameters", "input_schema", "inputSchema"):
        return "possible_schema"
    return "credential" if _credential_key(key) else ""


def _mask_field(context: str, key: str, value: Any) -> Any:
    if context == "credential_list" and isinstance(value, list):
        return [_REDACTED for _ in value]
    if context == "credential_schema":
        if isinstance(value, bool) or (key in _SCHEMA_LISTS and isinstance(value, list)):
            return value
        if isinstance(value, Mapping) and "value" not in value:
            return value
    if context == "credential" and key.lower() == "pass" and isinstance(value, bool):
        return value  # Boolean QA verdict.
    return _REDACTED


def _container_end(text: str, start: int) -> int:
    """Find the end of a composite value, including an unfinished value at EOF."""
    closing = ["}" if text[start] == "{" else "]"]
    for token in _CONTAINER_TOKEN.finditer(text, start + 1):
        if token[0] in ("{", "["):
            closing.append("}" if token[0] == "{" else "]")
        elif token[0] in ("}", "]"):
            if token[0] != closing.pop():
                return len(text)
            if not closing:
                return token.end()
    return len(text)


def _redact_assignments(text: str) -> str:
    pieces = []
    end = 0
    for match in _ASSIGNMENT.finditer(text):
        if match.start() < end:
            continue
        key = match["key"]
        if key.startswith('"'):
            try:
                key = json.loads(key)
            except ValueError:
                continue  # A quoted label in ordinary text need not be valid JSON.
        if not _credential_key(key.removeprefix("--")):
            continue
        start = match.end()
        if text[start:start + 1] in ("{", "["):
            pieces.extend((text[end:start], '"<redacted>"'))
            end = _container_end(text, start)
            continue
        value = _VALUE.match(text, start)
        if not value:
            continue
        if key.lower() == "pass" and value[0].lower() in ("true", "false"):
            continue
        quote = '\\"' if value[0].startswith('\\"') else value[0][:1]
        if quote not in ('\\"', '"', "'"):
            quote = ""
        pieces.extend((text[end:start], quote + _REDACTED + quote))
        end = value.end()
    if not pieces:
        return text
    pieces.append(text[end:])
    return "".join(pieces)


def _redact_auth_token(match: re.Match) -> str:
    scheme, space, token = match.groups()
    if scheme.lower() == "bearer":
        if token.rstrip(".").lower() == "token":  # Common in prose, e.g. "set a Bearer token".
            return match[0]
    else:
        # Basic credentials encode user:password. Preserve ordinary phrases like
        # "basic ideas", but honor apparent credentials even outside a header.
        try:
            if b":" not in base64.b64decode(token.rstrip("."), validate=True):
                return match[0]
        except ValueError:
            return match[0]
    return scheme + space + _REDACTED + token[len(token.rstrip(".")):]


def _redact_provider_token(match: re.Match) -> str:
    token = match[0]
    if token.startswith(("npm_", "hf_")):
        # Both prefixes also identify ordinary environment variables and API
        # names. Keep those readable, including npm_config_* and hf_hub_*.
        suffix = token.partition("_")[2]
        if len(suffix) < 20 or ("_" in suffix and (suffix.islower() or not any(c.isdigit() for c in suffix))):
            return token
    return _REDACTED


def _redact_pem(text: str) -> str:
    if "-----BEGIN " not in text:
        return text
    parts = []
    label = None
    end = 0
    for marker in _PEM_MARKER.finditer(text):
        if label is None and marker[1] == "BEGIN":
            parts.extend((text[end:marker.start()], _REDACTED))
            label = marker[2]
        elif label == marker[2] and marker[1] == "END":
            end = marker.end()
            label = None
    if label is None:
        parts.append(text[end:])
    return "".join(parts)


def _redact_command_options(text: str) -> str:
    pieces = []
    end = 0
    command = None
    mask_next = False
    shell_script = False
    position = 0
    while position < len(text):
        lexer = _SHELL_TOKEN if mask_next or command in _AUTH_COMMANDS else _DIAGNOSTIC_TOKEN
        token = lexer.search(text, position)
        if token is None:
            break
        position = token.end()
        word = token[0]
        if token["boundary"]:
            command = None
            mask_next = False
            shell_script = False
            continue
        if mask_next:
            replacement = _REDACTED
            mask_next = False
        elif shell_script:
            shell_script = False
            quote = word[0] if word[0] in ("'", '"') and word.endswith(word[0]) else ""
            script = word[1:-1] if quote else word
            replacement = quote + _redact_command_options(script) + quote
            if replacement == word:
                continue
        else:
            name, equals, value = word.partition("=")
            if equals and _credential_key(name) and any(c in value for c in ("\\", "'", '"')):
                # A shell assignment can concatenate quoted / unquoted parts
                # or escape whitespace. Non-shell assignments use _VALUE below.
                quote = value[0] if value[0] in ("'", '"') else ""
                replacement = name + "=" + quote + _REDACTED + quote
            else:
                executable = word.strip("'\"").lower().removesuffix(".exe")
                if command not in _AUTH_COMMANDS and executable in _AUTH_COMMANDS | _SHELL_COMMANDS:
                    # Diagnostic sentences and wrappers can prefix a command.
                    # An argument URL/path containing its name cannot select it.
                    command = executable
                elif command is None and not (equals and name.isidentifier()):
                    command = _command_name(word.strip("'\""))
                if command in _SHELL_COMMANDS and re.fullmatch("-[A-Za-z]*c[A-Za-z]*", word):
                    shell_script = True
                    continue
                prefix = _credential_option(command, word)
                if prefix is None:
                    continue
                if prefix == word and not prefix.endswith("="):
                    mask_next = True
                    continue
                replacement = prefix + _REDACTED
        pieces.extend((text[end:token.start()], replacement))
        end = token.end()
    pieces.append(text[end:])
    return "".join(pieces)


def _command_name(first: Any) -> str:
    return first.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe") if isinstance(first, str) else ""


def _credential_option(command: str | None, arg: str) -> str | None:
    flag, equals, _ = arg.partition("=")
    if re.fullmatch(_SECRET_FLAG, flag) or (command == "curl" and flag in ("-u", "--user")):
        return flag + equals
    if command == "curl" and arg.startswith("-u"):
        return "-u"
    if command in ("mysql", "mariadb") and arg.startswith("-p") and len(arg) > 2:
        return "-p"
    return None


def _redact_argv(values: list | tuple) -> list:
    result = list(values)
    first = values[0] if values else None
    command = _command_name(first)
    for i, arg in enumerate(result):
        if not isinstance(arg, str):
            continue
        prefix = _credential_option(command, arg)
        if prefix is None:
            continue
        if prefix != arg or prefix.endswith("="):
            result[i] = prefix + _REDACTED
        elif i + 1 < len(result):
            result[i + 1] = _REDACTED
    return result


def _redact_serialized_argv(match: re.Match) -> str:
    array = match[0]
    try:
        args = json.loads(array)
    except ValueError:
        return array
    clean = _redact_argv(args)
    if clean == args:
        return array
    # Keep whitespace and original escape spelling for unaffected options.
    replacements = iter(zip(args, clean))

    def replace_arg(token: re.Match) -> str:
        original, filtered = next(replacements)
        return token[0] if original == filtered else json.dumps(filtered)

    return _JSON_STRING.sub(replace_arg, array)


def _redact_json(text: str, parsed: dict | list) -> str:
    """Rewrite only credential values, preserving JSON layout and nested argument strings."""
    parts = []
    end = 0
    decoder = json.JSONDecoder()
    containers = []
    field_context = "schema" if _object_schema(parsed) else ""
    for token in _JSON_STRUCTURE.finditer(text):
        if token.start() < end:
            continue
        if token[0] in ("[", "{"):
            # Array members share the schema context of the array (e.g. anyOf).
            context = containers[-1][1] if containers and containers[-1][0] == "[" else field_context
            if context == "possible_schema" and token[0] == "{":
                candidate, _ = decoder.raw_decode(text, token.start())
                context = "schema" if _object_schema(candidate) else ""
            containers.append((token[0], context))
            continue
        if token[0] in ("]", "}"):
            containers.pop()
            continue
        value = json.loads(token[0])
        colon = _JSON_COLON.match(text, token.end())
        if colon:
            field_context = _field_context(containers[-1][1], value)
            if field_context not in ("credential", "credential_list", "credential_schema"):
                continue
            start = colon.end()
            item, stop = decoder.raw_decode(text, start)
            clean = _mask_field(field_context, value, item)
            if clean == item:
                continue
            replacement = json.dumps(clean, ensure_ascii=False)
        else:
            clean = redact_diagnostic_text(value)
            if clean == value:
                continue
            start, stop = token.span()
            # An escaped filesystem surrogate in the original JSON must remain
            # representable when the diagnostic copy is written to UTF-8 logs.
            replacement = json.dumps(clean, ensure_ascii=bool(re.search(r"[\ud800-\udfff]", clean)))
        parts.extend((text[end:start], replacement))
        end = stop
    parts.append(text[end:])
    return "".join(parts)


def _json_literal(value: Any) -> bool:
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_literal(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_json_literal(item) for item in value)
    return value is None or isinstance(value, (str, int, float, bool))


def _filter_embedded_containers(text: str) -> str:
    """Filter structured fragments and retain enclosing shell/header/field context."""
    pieces: list[str] = []
    fragments: list[str] = []
    # Opaque words let the outer filters consume a whole argument, e.g.
    # credentials={...}, while preserving the formatting of unrelated fragments.
    prefix = "BoxTraceFragment"
    if prefix in text:
        prefix += uuid4().hex
    end = scan = 0
    while match := _OPEN_CONTAINER.search(text, scan):
        start = match.start()
        scan = _container_end(text, start)
        fragment = text[start:scan]
        if '"' not in fragment and "'" not in fragment:
            continue
        try:
            parsed = json.loads(fragment)
        except (ValueError, RecursionError):
            if "'" not in fragment or fragment[-1] not in "}]":
                continue
            # Python SDKs also format request dictionaries with repr().
            try:
                parsed = ast.literal_eval(fragment)
            except (ValueError, TypeError, SyntaxError, RecursionError):
                continue
            # The structured filter handles JSON values. Preserve other Python
            # representations such as tuples and sets in ordinary error text.
            if not isinstance(parsed, (dict, list)) or not _json_literal(parsed):
                continue
            cleaned = redact_diagnostic_value(parsed)
            filtered = fragment if cleaned == parsed else repr(cleaned)
        else:
            filtered = _redact_json(fragment, parsed)
        pieces.extend((text[end:start], f"{prefix}{len(fragments)}X"))
        fragments.append(filtered)
        end = scan
    if not fragments:
        return _redact_plain_text(text)
    pieces.append(text[end:])
    masked = _redact_plain_text("".join(pieces))
    return re.sub(prefix + r"(\d+)X", lambda m: fragments[int(m[1])], masked)


def redact_diagnostic_text(text: str) -> str:
    """Mask recognized credential values while preserving surrounding diagnostics."""
    # Inspect serialized arguments one string at a time. Running header regexes
    # on the encoded object could swallow a JSON-escaped newline or delimiter.
    if "[" in text and "-" in text:
        text = _ARGV_ARRAY.sub(_redact_serialized_argv, text)
    if (text.lstrip().startswith(("{", "[")) and text.rstrip().endswith(("}", "]"))
            and '"' in text):
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            pass  # Also handle escaped shell fragments and Python reprs.
        else:
            return _redact_json(text, parsed)
    if ('"' in text or "'" in text) and ("{" in text or "[" in text):
        return _filter_embedded_containers(text)
    return _redact_plain_text(text)


def _redact_plain_text(text: str) -> str:
    text = _redact_pem(text)
    if "-" in text or "=" in text:
        text = _redact_command_options(text)
    if ":" in text or "=" in text or "：" in text:
        text = _URL_USERINFO.sub(r"\1<redacted>@", text)
        text = _COOKIE_LINE.sub(r"\1<redacted>", text)
        text = _COOKIE_HEADER.sub(r"\1<redacted>", text)
        text = _redact_assignments(text)
    return _KNOWN_TOKEN.sub(_redact_provider_token, _AUTH_TOKEN.sub(_redact_auth_token, text))


class DiagnosticFormatter(logging.Formatter):
    """Apply the same text filter after formatting log arguments and tracebacks."""

    def __init__(self, *args, delegate: logging.Formatter | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._delegate = delegate

    def format(self, record: logging.LogRecord) -> str:
        # SDKs pass request options as a mapping to %s. Filter that copy before
        # repr() adds escapes. Preserve the type of other formatting arguments
        # (notably integer %d) and other handlers' original record.
        record = copy.copy(record)
        if isinstance(record.args, Mapping):
            record.args = redact_diagnostic_value(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(
                redact_diagnostic_value(arg) if isinstance(arg, (str, Mapping, list)) else arg
                for arg in record.args
            )
        formatted = self._delegate.format(record) if self._delegate is not None else super().format(record)
        return redact_diagnostic_text(formatted)


def _message_text_fields(message):
    content = message.get("content")
    if isinstance(content, str):
        yield message, "content"
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str):
                yield block, "text"


def diagnostic_continuation_quote(prompt: str, reply: str) -> str | None:
    """Filter a runtime quote when its 40-character cut removed a credential label."""
    from .loop_guards import truncation_continuation_text

    tail = reply.rstrip()[-40:]
    if not tail or prompt != truncation_continuation_text(tail):
        return None
    safe = redact_diagnostic_text(reply).rstrip()
    if safe.endswith(tail):
        return None
    quoted = redact_diagnostic_text(tail)
    if not safe.endswith(quoted):
        # The quote begins inside a credential. Keep the suffix after its
        # marker without introducing text from before the original cut.
        for start in range(len(quoted) + 1):
            candidate = _REDACTED + quoted[start:]
            if safe.endswith(candidate):
                quoted = candidate
                break
        else:
            quoted = _REDACTED
    return truncation_continuation_text(quoted)


def _redact_continuation_quotes(messages: list, cleaned: list) -> None:
    for index in range(1, len(messages)):
        reply, prompt = messages[index - 1], messages[index]
        if not (isinstance(reply, Mapping) and reply.get("role") == "assistant"
                and isinstance(prompt, Mapping) and prompt.get("role") == "user"):
            continue
        # Provider wire messages omit source and may wrap text in blocks. An
        # exact match against the preceding reply identifies the runtime quote.
        original = "".join(block[key] for block, key in _message_text_fields(reply))
        for (block, key), (target, _) in zip(_message_text_fields(prompt), _message_text_fields(cleaned[index])):
            quote = diagnostic_continuation_quote(block[key], original)
            if quote is not None:
                target[key] = quote


def redact_diagnostic_value(value: Any, *, _context: str = "possible_schema") -> Any:
    """Build a JSON-safe diagnostic copy without changing model or tool data."""
    if isinstance(value, Mapping):
        if _context == "possible_schema":
            _context = "schema" if _object_schema(value) else ""
        # Reuse the existing provider image handling before traversing large
        # inline images. Leave memory evidence's separate sanitizer unchanged.
        if value.get("type") in ("image", "image_url"):
            from .llm.debug_logging import sanitize_for_logging

            value = sanitize_for_logging(value)
        result = {}
        for key, item in value.items():
            key = str(key)
            context = _field_context(_context, key)
            if context in ("credential", "credential_list", "credential_schema"):
                masked = _mask_field(context, key, item)
                if masked is not item:
                    result[key] = masked
                    continue
            result[key] = redact_diagnostic_value(item, _context=context)
        if isinstance(value.get("messages"), list):
            _redact_continuation_quotes(value["messages"], result["messages"])
        return result
    if isinstance(value, (list, tuple)):
        return [redact_diagnostic_value(item, _context=_context) for item in _redact_argv(value)]
    if hasattr(value, "model_dump"):
        try:
            return redact_diagnostic_value(value.model_dump(exclude_none=True))
        except Exception:
            pass
    if hasattr(value, "to_schema"):
        try:
            return redact_diagnostic_value(value.to_schema())
        except Exception:
            pass
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_diagnostic_text(str(value))


def diagnostic_event_fields(event: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the payload and recover missing exit codes from legacy shell errors."""
    result = redact_diagnostic_value(fields)
    if (event in {"tool.response", "tool/fail"} and fields.get("tool_name") in ("bash", "bash_output")
            and (event == "tool/fail" or fields.get("success") is False)
            and type(fields.get("exit_code")) is not int and isinstance(fields.get("error"), str)):
        match = re.match(r"Command failed with exit code (-?[0-9]{1,10})\b", fields["error"])
        if match:
            result["exit_code"] = int(match[1])
    return result
