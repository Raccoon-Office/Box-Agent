"""Small, side-effect-free shell structure inspection helpers.

The inspector deliberately extracts only the facts needed by policy code:
which command words are invoked, whether shell control syntax is present, and
which regions could not be parsed.  Heredoc bodies and quoted ``-c`` payloads
belong to another language and are never scanned as top-level shell commands.
"""

from __future__ import annotations

import glob
import platform
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field, replace


_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PARAMETER_EXPANSION_RE = re.compile(
    r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}\n]+\}|[@*#?$!0-9_-])"
)
_ARITHMETIC_MUTATION_RE = re.compile(r"\+\+|--|<<=|>>=|(?<![!<>=])=(?!=)")
_HEREDOC_RE = re.compile(
    r"(?<!<)<<(?P<strip_tabs>-)?(?!<)\s*"
    r"(?P<word>(?:\\.|'[^']*'|\"[^\"]*\"|[^\s;&|<>])+)"
)
_DIRECT_WRAPPERS = frozenset(
    {
        "busybox",
        "chroot",
        "command",
        "env",
        "exec",
        "ionice",
        "nice",
        "nohup",
        "setsid",
        "stdbuf",
        "sudo",
        "time",
        "timeout",
        "toybox",
        "xargs",
    }
)
_CONTROL_PREFIXES = frozenset(
    {"!", "{", "}", "do", "elif", "else", "if", "then", "until", "while"}
)
_SHELL_EXECUTABLES = frozenset({"ash", "bash", "dash", "ksh", "sh", "zsh"})
_MAX_NESTED_SHELL_DEPTH = 2
_SUBSTITUTION_PLACEHOLDER = "__box_agent_substitution__"
_SUBSTITUTION_MARKER_BASE = 0xE000
_WRAPPER_OPTIONS_WITH_VALUES = {
    "command": frozenset(),
    "busybox": frozenset(),
    "chroot": frozenset({"--groups", "--userspec"}),
    "env": frozenset({"-C", "--argv0", "--chdir", "--split-string", "--unset", "-S", "-u"}),
    "exec": frozenset({"-a"}),
    "ionice": frozenset(
        {
            "--class",
            "--classdata",
            "--pgid",
            "--pid",
            "--uid",
            "-P",
            "-c",
            "-n",
            "-p",
            "-u",
        }
    ),
    "nice": frozenset({"--adjustment", "-n"}),
    "nohup": frozenset(),
    "setsid": frozenset(),
    "stdbuf": frozenset({"--error", "--input", "--output", "-e", "-i", "-o"}),
    "sudo": frozenset(
        {
            "--chdir",
            "--chroot",
            "--close-from",
            "--command-timeout",
            "--group",
            "--host",
            "--prompt",
            "--role",
            "--type",
            "--user",
            "-C",
            "-D",
            "-g",
            "-h",
            "-p",
            "-r",
            "-R",
            "-t",
            "-T",
            "-u",
        }
    ),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "timeout": frozenset({"--kill-after", "--signal", "-k", "-s"}),
    "toybox": frozenset(),
    "xargs": frozenset(
        {
            "--arg-file",
            "--delimiter",
            "--eof",
            "--max-args",
            "--max-chars",
            "--max-lines",
            "--max-procs",
            "--process-slot-var",
            "--replace",
            "-E",
            "-I",
            "-L",
            "-P",
            "-a",
            "-d",
            "-n",
            "-s",
        }
    ),
}
_WRAPPER_POSITIONAL_VALUES = {
    "chroot": 1,
    "timeout": 1,
}
_SHELL_OPTIONS_WITH_VALUES = frozenset(
    {"+O", "+o", "--init-file", "--rcfile", "-O", "-o"}
)
_FIND_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


@dataclass(frozen=True)
class ShellInvocation:
    """One executable command extracted from a shell command string."""

    words: tuple[str, ...] = field(repr=False)
    executable_index: int
    raw_segment: str = field(repr=False)
    indirect: bool = False
    dynamic_executable_sources: tuple[str, ...] = field(default=(), repr=False)
    dynamic_executable_evidence: str = field(default="", repr=False)
    known_variables: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    shell_depth: int = field(default=0, compare=False, repr=False)
    sequence_index: int = field(default=0, compare=False, repr=False)
    directory_changes: tuple[str, ...] = field(default=(), compare=False, repr=False)
    raw_words: tuple[str | None, ...] = field(default=(), compare=False, repr=False)

    @property
    def executable(self) -> str:
        return self.words[self.executable_index]

    @property
    def arguments(self) -> tuple[str, ...]:
        return self.words[self.executable_index + 1 :]

    @property
    def prefix(self) -> tuple[str, ...]:
        return self.words[: self.executable_index]

    def expand_known_parameters(self, value: str) -> str:
        """Expand literal bindings available when this command is invoked."""
        return _expand_known_parameters(value, dict(self.known_variables))

    def expand_argument(self, index: int, value: str, *, glob_pattern: bool = False) -> str:
        """Expand one operand, optionally escaping literal wildcard characters."""
        return self.expand_word(self.executable_index + 1 + index, value, glob_pattern=glob_pattern)

    def expand_word(self, index: int, value: str, *, glob_pattern: bool = False) -> str:
        """Expand an argument or wrapper-prefix word from its original quoting."""
        raw = self.raw_words[index] if 0 <= index < len(self.raw_words) else None
        if raw is None:
            return self.expand_known_parameters(value)
        pattern = _expand_script_word(raw, dict(self.known_variables), glob_pattern=glob_pattern)
        word = self.words[index]
        # Attached option values, e.g. -tDEST and --target-directory=DEST.
        prefix = word[:len(word) - len(value)] if word.endswith(value) else ""
        return pattern[len(prefix):] if pattern.startswith(prefix) else pattern


@dataclass(frozen=True)
class ShellRedirection:
    """One shell redirection extracted outside quoted or embedded code."""

    operator: str
    target: str = field(repr=False)
    indirect: bool = False
    invocation_index: int = field(default=0, compare=False, repr=False)
    shell_depth: int = field(default=0, compare=False, repr=False)
    sequence_index: int = field(default=0, compare=False, repr=False)
    directory_changes: tuple[str, ...] = field(default=(), compare=False, repr=False)
    known_variables: tuple[tuple[str, str], ...] = field(default=(), compare=False, repr=False)
    literal_target: bool = field(default=False, compare=False, repr=False)
    glob_target: bool = field(default=False, compare=False, repr=False)
    raw_target: str | None = field(default=None, compare=False, repr=False)

    def expand_target(self) -> str:
        """Expand bindings from when this redirection is performed."""
        if self.raw_target is not None:
            return _expand_script_word(self.raw_target, dict(self.known_variables))
        if self.literal_target:
            return self.target
        return _expand_known_parameters(self.target, dict(self.known_variables))

    def target_glob_pattern(self) -> str:
        if self.raw_target is None:
            return self.expand_target()
        return _expand_script_word(self.raw_target, dict(self.known_variables), glob_pattern=True)


@dataclass(frozen=True)
class ShellInspection:
    """Policy-relevant facts extracted from a shell command."""

    invocations: tuple[ShellInvocation, ...]
    has_control_operators: bool
    substitutions: tuple[str, ...]
    redirections: tuple[ShellRedirection, ...]
    ambiguous_regions: tuple[str, ...] = field(repr=False)
    shell_text: str
    assigned_variables: frozenset[str] = field(default=frozenset(), repr=False)
    mutating_arithmetic: bool = False


@dataclass
class _ShellScope:
    variables: dict[str, str] = field(repr=False)
    known_variables: dict[str, str] = field(repr=False)
    directory_changes: tuple[str, ...] = field(repr=False)

    def child(self) -> _ShellScope:
        return _ShellScope(dict(self.variables), dict(self.known_variables), self.directory_changes)


def _newline_only(value: str) -> str:
    if value.endswith("\r\n"):
        return " " * (len(value) - 2) + "\r\n"
    if value.endswith("\n") or value.endswith("\r"):
        return " " * (len(value) - 1) + value[-1]
    return " " * len(value)


def _heredoc_declarations(
    line: str,
    *,
    initial_quote: str | None,
) -> tuple[tuple[tuple[str, bool], ...], str | None]:
    """Return real heredoc declarations outside shell quotes and comments."""
    declarations: list[tuple[str, bool]] = []
    quote = initial_quote
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            index += 1
            continue
        if char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if quote is None and char == "#" and (
            index == 0 or line[index - 1].isspace() or line[index - 1] in ";&|(){}"
        ):
            break
        if quote != "'" and line.startswith("$((", index):
            end = _matching_parenthesis(line, index + 2)
            if end is None:
                break
            index = end + 1
            continue
        if quote is None and line.startswith("((", index):
            end = _matching_parenthesis(line, index + 1)
            if end is None:
                break
            index = end + 1
            continue
        if quote is None and (match := _HEREDOC_RE.match(line, index)):
            delimiter = _dequote_shell_word(match.group("word"), posix=True)
            if delimiter:
                declarations.append(
                    (delimiter, bool(match.group("strip_tabs")))
                )
            index = match.end()
            continue
        index += 1
    return tuple(declarations), quote


def _mask_heredoc_bodies(command: str) -> str:
    """Replace heredoc payloads with whitespace while preserving line layout."""
    lines = command.splitlines(keepends=True)
    masked: list[str] = []
    pending: list[tuple[str, bool]] = []
    quote: str | None = None

    for line in lines:
        if pending:
            delimiter, strip_tabs = pending[0]
            candidate = line.rstrip("\r\n")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            masked.append(_newline_only(line))
            if candidate == delimiter:
                pending.pop(0)
            continue

        masked.append(line)
        declarations, quote = _heredoc_declarations(
            line,
            initial_quote=quote,
        )
        pending.extend(declarations)

    return "".join(masked)


def _mask_shell_comments(value: str) -> str:
    """Mask shell comments while preserving quoted hash characters and lines."""
    masked: list[str] = []
    quote: str | None = None
    for line in value.splitlines(keepends=True):
        chars = list(line)
        escaped = False
        index = 0
        while index < len(line):
            char = line[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if char == "\\" and quote != "'":
                escaped = True
                index += 1
                continue
            if char == "'" and quote != '"':
                quote = None if quote == "'" else "'"
                index += 1
                continue
            if char == '"' and quote != "'":
                quote = None if quote == '"' else '"'
                index += 1
                continue
            if quote is None and char == "#" and (
                index == 0
                or line[index - 1].isspace()
                or line[index - 1] in ";&|(){}"
            ):
                chars[index:] = _newline_only(line[index:])
                break
            index += 1
        masked.append("".join(chars))
    return "".join(masked)


def _matching_parenthesis(value: str, start: int) -> int | None:
    depth = 1
    quote: str | None = None
    escaped = False
    index = start
    while index < len(value):
        char = value[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            index += 1
            continue
        if char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if quote is None:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
        index += 1
    return None


def _substitution_marker(index: int) -> str:
    """Return a length-preserving private-use marker for one substitution."""
    return chr(_SUBSTITUTION_MARKER_BASE + index)


def _extract_and_mask_substitutions(value: str) -> tuple[str, tuple[str, ...], bool, tuple[int, ...]]:
    """Return top-level shell text plus nested command/process substitutions."""
    chars = list(value)
    substitutions: list[str] = []
    substitution_positions: list[int] = []
    mutating_arithmetic = False
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            index += 1
            continue
        if char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if quote != "'" and value.startswith("$((", index):
            end = _matching_parenthesis(value, index + 2)
            if end is None:
                chars[index:] = " " * (len(value) - index)
                break
            arithmetic = value[index + 3 : end - 1]
            _, nested_substitutions, nested_mutation, positions = _extract_and_mask_substitutions(arithmetic)
            substitutions.extend(nested_substitutions)
            substitution_positions.extend(index + 3 + position for position in positions)
            mutating_arithmetic |= bool(_ARITHMETIC_MUTATION_RE.search(arithmetic)) or nested_mutation
            span = end + 1 - index
            chars[index : end + 1] = list(
                _SUBSTITUTION_PLACEHOLDER[:span].ljust(span)
            )
            index = end + 1
            continue
        if quote is None and value.startswith("((", index):
            end = _matching_parenthesis(value, index + 1)
            if end is None:
                chars[index:] = " " * (len(value) - index)
                break
            arithmetic = value[index + 2 : end - 1]
            _, nested_substitutions, nested_mutation, positions = _extract_and_mask_substitutions(arithmetic)
            substitutions.extend(nested_substitutions)
            substitution_positions.extend(index + 2 + position for position in positions)
            mutating_arithmetic |= bool(_ARITHMETIC_MUTATION_RE.search(arithmetic)) or nested_mutation
            chars[index : end + 1] = " " * (end + 1 - index)
            index = end + 1
            continue
        if quote != "'" and (
            value.startswith("$(", index)
            or value.startswith("<(", index)
            or value.startswith(">(", index)
        ):
            end = _matching_parenthesis(value, index + 2)
            if end is None:
                substitutions.append(value[index + 2 :])
                substitution_positions.append(index)
                chars[index:] = " " * (len(value) - index)
                break
            marker = _substitution_marker(len(substitutions))
            substitutions.append(value[index + 2 : end])
            substitution_positions.append(index)
            span = end + 1 - index
            chars[index : end + 1] = marker * span
            index = end + 1
            continue
        if quote != "'" and char == "`":
            end = index + 1
            while end < len(value):
                if value[end] == "`" and value[end - 1] != "\\":
                    break
                end += 1
            if end >= len(value):
                substitutions.append(value[index + 1 :])
                substitution_positions.append(index)
                chars[index:] = " " * (len(value) - index)
                break
            marker = _substitution_marker(len(substitutions))
            substitutions.append(value[index + 1 : end])
            substitution_positions.append(index)
            span = end + 1 - index
            chars[index : end + 1] = marker * span
            index = end + 1
            continue
        index += 1
    return "".join(chars), tuple(substitutions), mutating_arithmetic, tuple(substitution_positions)


def _dequote_shell_word(value: str, *, posix: bool) -> str:
    try:
        words = shlex.split(value, posix=posix)
    except ValueError:
        return value
    if not posix:
        words = _strip_matching_quotes(words)
    return words[0] if len(words) == 1 else value


def _extract_and_mask_redirections(
    value: str,
    *,
    posix: bool,
) -> tuple[str, tuple[ShellRedirection, ...], tuple[str, ...]]:
    """Mask shell redirections and return their targets.

    This scanner runs after command substitutions have been masked, so ``>(…)``
    is never confused with an output redirection. Quoted ``>`` characters are
    data and remain untouched.
    """
    chars = list(value)
    redirections: list[ShellRedirection] = []
    ambiguous: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0

    while index < len(value):
        char = value[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            index += 1
            continue
        if char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            index += 1
            continue
        combined_output = char == "&" and value[index + 1:index + 2] == ">"
        if quote is not None or (char not in "<>" and not combined_output):
            index += 1
            continue

        operator_start = index
        if not combined_output:
            descriptor_start = index
            while descriptor_start > 0 and value[descriptor_start - 1].isdigit():
                descriptor_start -= 1
            if descriptor_start == 0 or not (
                value[descriptor_start - 1].isalnum()
                or value[descriptor_start - 1] == "_"
            ):
                operator_start = descriptor_start

        operator_end = index + 1
        while operator_end < len(value) and value[operator_end] in "<>&|":
            operator_end += 1
        target_start = operator_end
        while target_start < len(value) and value[target_start] in " \t":
            target_start += 1

        target_end = target_start
        target_quote: str | None = None
        target_escaped = False
        target_glob = False
        while target_end < len(value):
            target_char = value[target_end]
            if target_escaped:
                target_escaped = False
                target_end += 1
                continue
            if target_char == "\\" and target_quote != "'":
                target_escaped = True
                target_end += 1
                continue
            if target_char == "'" and target_quote != '"':
                target_quote = None if target_quote == "'" else "'"
                target_end += 1
                continue
            if target_char == '"' and target_quote != "'":
                target_quote = None if target_quote == '"' else '"'
                target_end += 1
                continue
            if target_quote is None and (target_char.isspace() or target_char in ";&|<>"):
                break
            if target_quote is None and target_char in "*?[$":
                target_glob = True
            target_end += 1

        raw_operator = value[operator_start:operator_end]
        raw_target = value[target_start:target_end]
        if raw_target:
            redirections.append(
                ShellRedirection(
                    operator=raw_operator,
                    target=_dequote_shell_word(raw_target, posix=posix),
                    literal_target=bool(re.fullmatch(r"'[^']*'", raw_target)),
                    glob_target=target_glob,
                    raw_target=raw_target,
                )
            )
        else:
            ambiguous.append(value[operator_start:operator_end])
        chars[operator_start:target_end] = " " * (target_end - operator_start)
        index = max(target_end, operator_end)

    return "".join(chars), tuple(redirections), tuple(ambiguous)


def _split_shell_segments(value: str) -> tuple[list[tuple[str, int, tuple[int, ...]]], bool]:
    segments: list[tuple[str, int, tuple[int, ...]]] = []
    start = 0
    quote: str | None = None
    escaped = False
    has_control = False
    groups: list[int] = []
    and_list_starts = [0]
    pipeline = False
    index = 0
    while index < len(value):
        char = value[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            index += 1
            continue
        if char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if quote is None and char in ";&|\n()":
            paired = index + 1 < len(value) and value[index + 1] == char and char in "&|"
            isolated = pipeline or (char == "|" and not paired)
            scope = (*groups, -(start + 1)) if isolated else tuple(groups)
            segments.append((value[start:index], start, scope))
            has_control = True
            if char == "&" and not paired:
                # An asynchronous AND/OR-list is one child shell, including
                # commands before the final ampersand, with pipelines inside.
                first = and_list_starts[-1]
                parent = tuple(groups)
                background = -(segments[first][1] + 1)
                for segment_index in range(first, len(segments)):
                    text, position, child_scope = segments[segment_index]
                    segments[segment_index] = (
                        text, position, (*parent, background, *child_scope[len(parent):]),
                    )
            if char == "(":
                groups.append(index + 1)
                and_list_starts.append(len(segments))
            elif char == ")" and groups:
                groups.pop()
                and_list_starts.pop()
            elif char in ";\n" or (char == "&" and not paired):
                and_list_starts[-1] = len(segments)
            pipeline = char == "|" and not paired
            if paired:
                index += 1
            start = index + 1
        index += 1
    scope = (*groups, -(start + 1)) if pipeline else tuple(groups)
    segments.append((value[start:], start, scope))
    return segments, has_control


def _strip_matching_quotes(words: list[str]) -> list[str]:
    return [
        word[1:-1]
        if len(word) >= 2 and word[0] == word[-1] and word[0] in {'"', "'"}
        else word
        for word in words
    ]


def _command_name(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if name.endswith((".cmd", ".com", ".exe")):
        name = name.rsplit(".", 1)[0]
    return name


def _leading_assignments(words: list[str]) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for word in words:
        if not _ASSIGNMENT_RE.match(word):
            break
        name, value = word.split("=", 1)
        assignments[name] = value
    return assignments


def _expand_known_parameters(value: str, variables: dict[str, str]) -> str:
    def replace_parameter(match: re.Match[str]) -> str:
        expression = match.group(0)
        if expression.startswith("${"):
            name = expression[2:-1]
            if name.startswith("!"):
                referenced_name = variables.get(name[1:])
                return (
                    variables.get(referenced_name, expression)
                    if referenced_name
                    else expression
                )
            name = name.split("[", 1)[0]
        elif expression.startswith("$") and len(expression) > 1:
            name = expression[1:]
        else:
            return expression
        return variables.get(name, expression)

    return _PARAMETER_EXPANSION_RE.sub(replace_parameter, value)


def _dynamic_executable_evidence(
    executable: str,
    substitutions: tuple[str, ...],
    variables: dict[str, str],
) -> str:
    evidence: list[str] = []
    index = 0
    while index < len(executable):
        source_index = ord(executable[index]) - _SUBSTITUTION_MARKER_BASE
        if 0 <= source_index < len(substitutions):
            marker = executable[index]
            while index < len(executable) and executable[index] == marker:
                index += 1
            evidence.append(substitutions[source_index])
            continue
        evidence.append(executable[index])
        index += 1
    return _expand_known_parameters("".join(evidence), variables)


def _expand_env_split_strings(
    words: list[str],
    *,
    posix: bool,
    origins: list[str | None],
) -> tuple[list[str], bool]:
    """Expand env -S/--split-string values before wrapper resolution."""
    expanded = list(words)
    index = 0
    while index < len(expanded):
        if _command_name(expanded[index]) != "env":
            index += 1
            continue
        option_index = index + 1
        while option_index < len(expanded):
            option = expanded[option_index]
            split_value: str | None = None
            consumed = 1
            if option in {"-S", "--split-string"}:
                if option_index + 1 >= len(expanded):
                    return expanded, True
                split_value = expanded[option_index + 1]
                consumed = 2
            elif option.startswith("--split-string="):
                split_value = option.split("=", 1)[1]
            if split_value is None:
                option_index += 1
                continue
            try:
                replacement = shlex.split(split_value, posix=posix)
            except ValueError:
                return expanded, True
            if not posix:
                replacement = _strip_matching_quotes(replacement)
            if not replacement:
                return expanded, True
            expanded[option_index : option_index + consumed] = replacement
            origins[option_index : option_index + consumed] = [None] * len(replacement)
            break
        index = option_index + 1
    return expanded, False


def _executable_index(words: list[str]) -> int | None:
    index = 0
    while index < len(words) and _ASSIGNMENT_RE.match(words[index]):
        index += 1
    while index < len(words) and words[index].casefold() in _CONTROL_PREFIXES:
        index += 1
    if index >= len(words):
        return None

    while index < len(words) and _command_name(words[index]) in _DIRECT_WRAPPERS:
        wrapper_index = index
        wrapper = _command_name(words[index])
        if wrapper == "command" and (
            index + 1 >= len(words) or words[index + 1] in {"-v", "-V"}
        ):
            return index
        index += 1
        options_with_values = _WRAPPER_OPTIONS_WITH_VALUES[wrapper]
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            index += 1
            option_name = option.split("=", 1)[0]
            if (
                option_name in options_with_values
                and "=" not in option
                and index < len(words)
            ):
                index += 1
        if wrapper in {"env", "sudo"}:
            while index < len(words) and _ASSIGNMENT_RE.match(words[index]):
                index += 1
        positional_values = _WRAPPER_POSITIONAL_VALUES.get(wrapper, 0)
        index += min(positional_values, len(words) - index)
        if index >= len(words) and wrapper == "sudo":
            return wrapper_index
    return index if index < len(words) else None


def _shell_script_indices(invocation: ShellInvocation) -> tuple[int, ...]:
    executable = _command_name(invocation.executable)
    if executable == "eval":
        return tuple(range(invocation.executable_index + 1, len(invocation.words)))
    if executable not in _SHELL_EXECUTABLES:
        return ()
    arguments = invocation.arguments
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return ()
        if argument == "-c" and index + 1 < len(arguments):
            return (invocation.executable_index + index + 2,)
        if (
            argument.startswith("-")
            and not argument.startswith("--")
            and "c" in argument[1:]
            and index + 1 < len(arguments)
        ):
            return (invocation.executable_index + index + 2,)
        option_name = argument.split("=", 1)[0]
        if option_name in _SHELL_OPTIONS_WITH_VALUES and "=" not in argument:
            index += 2
            continue
        if argument.startswith(("-", "+")):
            index += 1
            continue
        return ()
    return ()


def _raw_shell_words(segment: str, words: list[str], *, posix: bool) -> list[str | None]:
    """Retain outer quoting alongside shlex's dequoted argument list."""
    raw = re.findall(r'''(?:[^\s'"\\]|\\[\s\S]|'(?:[^']*)'|"(?:[^"\\]|\\[\s\S])*")+''', segment)
    if posix and len(raw) == len(words) and all(
        _dequote_shell_word(source, posix=True) == word
        for source, word in zip(raw, words)
    ):
        return raw
    return [None] * len(words)


def _expand_script_word(raw: str, variables: dict[str, str], *, glob_pattern: bool = False) -> str:
    """Perform the outer shell's quote removal and known parameter expansion."""
    result: list[str] = []
    quote = ""
    index = 0
    while index < len(raw):
        char = raw[index]
        if char in {"'", '"'} and (not quote or quote == char):
            quote = "" if quote else char
            index += 1
            continue
        if char == "\\" and quote != "'" and index + 1 < len(raw):
            following = raw[index + 1]
            if not quote or following in {'$', '`', '"', "\\", "\n"}:
                if following != "\n":
                    result.append(glob.escape(following) if glob_pattern else following)
                index += 2
                continue
        if quote != "'" and (match := _PARAMETER_EXPANSION_RE.match(raw, index)):
            value = _expand_known_parameters(match.group(0), variables)
            result.append(glob.escape(value) if glob_pattern and quote else value)
            index = match.end()
            continue
        result.append(glob.escape(char) if glob_pattern and quote else char)
        index += 1
    return "".join(result)


def _find_embedded_commands(invocation: ShellInvocation) -> tuple[str, ...]:
    """Return command payloads executed by find's -exec/-ok family."""
    executable = _command_name(invocation.executable)
    if executable != "find":
        return ()

    commands: list[str] = []
    arguments = list(invocation.arguments)
    index = 0
    while index < len(arguments):
        if arguments[index] not in _FIND_EXEC_ACTIONS:
            index += 1
            continue
        index += 1
        start = index
        while index < len(arguments) and arguments[index] not in {";", "+"}:
            index += 1
        if index > start:
            commands.append(shlex.join(arguments[start:index]))
        if index < len(arguments):
            index += 1
    return tuple(commands)


def _environment_context(
    invocation: ShellInvocation,
    scope: _ShellScope,
    assignments: dict[str, str],
    known_assignments: dict[str, str],
) -> _ShellScope:
    context = _ShellScope(
        {**scope.variables, **assignments},
        {**scope.known_variables, **known_assignments},
        scope.directory_changes,
    )
    prefix = invocation.prefix
    index = 0
    while index < len(prefix):
        if _command_name(prefix[index]) != "env":
            index += 1
            continue
        cursor = index + 1
        while cursor < len(prefix) and prefix[cursor].startswith("-"):
            option = prefix[cursor]
            value_index = cursor
            cursor += 1
            if option == "--":
                break
            option_name, equals, value = option.partition("=")
            if option in {"-i", "--ignore-environment"}:
                context.variables.clear()
                context.known_variables.clear()
                continue
            if option.startswith("-C") and option != "-C":
                option_name, equals, value = "-C", "=", option[2:]
            if option_name in _WRAPPER_OPTIONS_WITH_VALUES["env"] and not equals and cursor < len(prefix):
                value_index = cursor
                value = prefix[cursor]
                cursor += 1
            if option_name in {"-C", "--chdir"}:
                context.directory_changes += (invocation.expand_word(value_index, value),)
            elif option_name in {"-u", "--unset"}:
                unset = invocation.expand_word(value_index, value)
                context.variables.pop(unset, None)
                context.known_variables.pop(unset, None)
        while cursor < len(prefix) and _ASSIGNMENT_RE.match(prefix[cursor]):
            name, value = prefix[cursor].split("=", 1)
            context.variables[name] = value
            context.known_variables[name] = invocation.expand_word(cursor, prefix[cursor]).split("=", 1)[1]
            cursor += 1
        index = cursor
    return context


def inspect_shell_command(
    command: str,
    *,
    posix: bool | None = None,
    environment: Mapping[str, str] | None = None,
    _depth: int = 0,
    _variables: dict[str, str] | None = None,
    _known_variables: dict[str, str] | None = None,
    _directory_changes: tuple[str, ...] = (),
    _sequence: list[int] | None = None,
) -> ShellInspection:
    """Inspect shell command structure without executing it.

    Parsing is intentionally bounded. Unparseable regions are returned to the
    policy caller, which can fail closed only when its own capability appears
    in that region.
    """
    if posix is None:
        posix = platform.system() != "Windows"
    sequence = [0] if _sequence is None else _sequence

    def next_sequence() -> int:
        index = sequence[0]
        sequence[0] += 1
        return index

    shell_text = _mask_shell_comments(_mask_heredoc_bodies(command))
    top_level_text, substitutions, mutating_arithmetic, substitution_positions = _extract_and_mask_substitutions(shell_text)
    command_text, _, redirect_ambiguous = _extract_and_mask_redirections(top_level_text, posix=posix)
    segments, has_control = _split_shell_segments(command_text)
    invocations: list[ShellInvocation] = []
    assigned_variables: set[str] = set()
    all_redirections: list[ShellRedirection] = []
    ambiguous = list(redirect_ambiguous)
    deferred_substitutions: list[ShellInspection] = []
    scopes: dict[tuple[int, ...], _ShellScope] = {(): _ShellScope(
        dict(_variables or {}),
        dict(environment or {}) if _known_variables is None else dict(_known_variables),
        _directory_changes,
    )}

    def get_scope(key: tuple[int, ...]) -> _ShellScope:
        if key not in scopes:
            scopes[key] = get_scope(key[:-1]).child()
        return scopes[key]

    def merge_nested(nested: ShellInspection) -> None:
        nonlocal mutating_arithmetic
        offset = len(invocations)
        invocations.extend(replace(invocation, indirect=True) for invocation in nested.invocations)
        all_redirections.extend(
            replace(redirect, indirect=True, invocation_index=offset + redirect.invocation_index)
            for redirect in nested.redirections
        )
        assigned_variables.update(nested.assigned_variables)
        mutating_arithmetic |= nested.mutating_arithmetic
        ambiguous.extend(nested.ambiguous_regions)

    def include_nested(script: str, context: _ShellScope, *, defer: bool = False) -> None:
        if _depth >= _MAX_NESTED_SHELL_DEPTH:
            ambiguous.append(script)
            return
        nested = inspect_shell_command(
            script,
            posix=posix,
            environment=environment,
            _depth=_depth + 1,
            _variables=context.variables,
            _known_variables=context.known_variables,
            _directory_changes=context.directory_changes,
            _sequence=sequence,
        )
        if defer:
            deferred_substitutions.append(nested)
        else:
            merge_nested(nested)

    for raw_segment, segment_start, scope_key in segments:
        scope = get_scope(scope_key)
        original_segment = top_level_text[segment_start : segment_start + len(raw_segment)]
        _, segment_redirections, _ = _extract_and_mask_redirections(original_segment, posix=posix)
        segment_substitutions = tuple(
            source for source, position in zip(substitutions, substitution_positions)
            if segment_start <= position < segment_start + len(raw_segment)
        )
        segment = raw_segment.strip()
        if not segment and not segment_redirections and not segment_substitutions:
            continue
        try:
            words = shlex.split(segment, posix=posix)
        except ValueError:
            ambiguous.append(segment)
            continue
        if not posix:
            words = _strip_matching_quotes(words)
        origins = _raw_shell_words(segment, words, posix=posix)
        words, split_string_ambiguous = _expand_env_split_strings(words, posix=posix, origins=origins)
        if split_string_ambiguous:
            ambiguous.append(segment)
            continue
        # Command/process substitutions execute in the owning command's scope,
        # before its redirections and before later commands can change state.
        for substitution in segment_substitutions:
            include_nested(substitution, scope, defer=True)
        segment_assignments = _leading_assignments(words)
        known_assignments: dict[str, str] = {}
        for index, word in enumerate(words):
            if not _ASSIGNMENT_RE.match(word):
                break
            name, value = word.split("=", 1)
            variables = {**scope.known_variables, **known_assignments}
            known_assignments[name] = (
                _expand_script_word(origins[index], variables).split("=", 1)[1]
                if origins[index] is not None else _expand_known_parameters(value, variables)
            )
        executable_index = _executable_index(words)
        redirect_variables = dict(scope.known_variables)
        if executable_index is None:
            redirect_variables.update(known_assignments)
        all_redirections.extend(
            replace(
                redirection,
                invocation_index=len(invocations),
                shell_depth=_depth + len(scope_key),
                sequence_index=next_sequence(),
                directory_changes=scope.directory_changes,
                known_variables=tuple(redirect_variables.items()),
            )
            for redirection in segment_redirections
        )
        if executable_index is None:
            assigned_variables.update(segment_assignments)
            scope.variables.update(segment_assignments)
            scope.known_variables.update(known_assignments)
            continue
        substitution_sources = tuple(
            source for source_index, source in enumerate(substitutions)
            if _substitution_marker(source_index) in words[executable_index]
        )
        parameter_sources = tuple(match.group(0) for match in _PARAMETER_EXPANSION_RE.finditer(words[executable_index]))
        dynamic_executable_sources = tuple(dict.fromkeys((*substitution_sources, *parameter_sources)))
        invocation = ShellInvocation(
            tuple(words), executable_index, segment,
            known_variables=tuple(scope.known_variables.items()),
            shell_depth=_depth + len(scope_key),
            sequence_index=next_sequence(),
            directory_changes=scope.directory_changes,
            raw_words=tuple(origins),
            dynamic_executable_sources=dynamic_executable_sources,
            dynamic_executable_evidence=(
                _dynamic_executable_evidence(words[executable_index], substitutions, scope.variables)
                if dynamic_executable_sources else ""
            ),
        )
        context = _environment_context(invocation, scope, segment_assignments, known_assignments)
        invocation = replace(invocation, directory_changes=context.directory_changes)
        invocations.append(invocation)
        name = _command_name(invocation.executable)
        if name in {"declare", "export", "readonly", "typeset"}:
            for index, argument in enumerate(invocation.arguments):
                if not _ASSIGNMENT_RE.match(argument):
                    continue
                variable, raw_value = argument.split("=", 1)
                scope.variables[variable] = raw_value
                variable, value = invocation.expand_argument(index, argument).split("=", 1)
                scope.known_variables[variable] = value
        if name in {"cd", "set-location"} and invocation.arguments:
            scope.directory_changes += (invocation.expand_argument(len(invocation.arguments) - 1, invocation.arguments[-1]),)
        if script_indices := _shell_script_indices(invocation):
            def script_with(variables: dict[str, str]) -> str:
                parts = []
                for index in script_indices:
                    raw = origins[index]
                    value = invocation.words[index]
                    parts.append(
                        _expand_script_word(raw, variables) if raw is not None
                        else _expand_known_parameters(value, variables)
                    )
                return " ".join(parts)

            # Argument expansion precedes command-prefix/env assignments. Single
            # quotes and escaped dollars defer expansion to the child shell.
            script = script_with(scope.known_variables)
            include_nested(script, context)
            source_script = script_with(scope.variables)
            if script != source_script and _depth < _MAX_NESTED_SHELL_DEPTH:
                # Materializing host values must not turn an unresolved dynamic
                # executable into a trusted literal. Retain only those approval
                # facts, not speculative writes/cd effects from unexpanded code.
                source = inspect_shell_command(
                    source_script, posix=posix, _depth=_depth + 1,
                    _variables=context.variables,
                    _known_variables=context.variables,
                    _directory_changes=context.directory_changes,
                )
                invocations.extend(
                    replace(item, indirect=True, sequence_index=next_sequence())
                    for item in source.invocations if item.dynamic_executable_sources
                )
        for embedded_command in _find_embedded_commands(invocation):
            include_nested(embedded_command, context)

    for nested in deferred_substitutions:
        merge_nested(nested)

    return ShellInspection(
        invocations=tuple(invocations), has_control_operators=has_control,
        substitutions=substitutions, redirections=tuple(all_redirections),
        ambiguous_regions=tuple(ambiguous), shell_text=shell_text,
        assigned_variables=frozenset(assigned_variables), mutating_arithmetic=mutating_arithmetic,
    )
