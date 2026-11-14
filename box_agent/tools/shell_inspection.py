"""Small, side-effect-free shell structure inspection helpers.

The inspector deliberately extracts only the facts needed by policy code:
which command words are invoked, whether shell control syntax is present, and
which regions could not be parsed.  Heredoc bodies and quoted ``-c`` payloads
belong to another language and are never scanned as top-level shell commands.
"""

from __future__ import annotations

import platform
import re
import shlex
from dataclasses import dataclass, replace


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

    words: tuple[str, ...]
    executable_index: int
    raw_segment: str
    indirect: bool = False
    dynamic_executable_sources: tuple[str, ...] = ()
    dynamic_executable_evidence: str = ""

    @property
    def executable(self) -> str:
        return self.words[self.executable_index]

    @property
    def arguments(self) -> tuple[str, ...]:
        return self.words[self.executable_index + 1 :]

    @property
    def prefix(self) -> tuple[str, ...]:
        return self.words[: self.executable_index]


@dataclass(frozen=True)
class ShellRedirection:
    """One shell redirection extracted outside quoted or embedded code."""

    operator: str
    target: str
    indirect: bool = False


@dataclass(frozen=True)
class ShellInspection:
    """Policy-relevant facts extracted from a shell command."""

    invocations: tuple[ShellInvocation, ...]
    has_control_operators: bool
    substitutions: tuple[str, ...]
    redirections: tuple[ShellRedirection, ...]
    ambiguous_regions: tuple[str, ...]
    shell_text: str
    assigned_variables: frozenset[str] = frozenset()
    mutating_arithmetic: bool = False


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


def _extract_and_mask_substitutions(value: str) -> tuple[str, tuple[str, ...], bool]:
    """Return top-level shell text plus nested command/process substitutions."""
    chars = list(value)
    substitutions: list[str] = []
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
            _, nested_substitutions, nested_mutation = _extract_and_mask_substitutions(arithmetic)
            substitutions.extend(nested_substitutions)
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
            _, nested_substitutions, nested_mutation = _extract_and_mask_substitutions(arithmetic)
            substitutions.extend(nested_substitutions)
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
                chars[index:] = " " * (len(value) - index)
                break
            marker = _substitution_marker(len(substitutions))
            substitutions.append(value[index + 2 : end])
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
                chars[index:] = " " * (len(value) - index)
                break
            marker = _substitution_marker(len(substitutions))
            substitutions.append(value[index + 1 : end])
            span = end + 1 - index
            chars[index : end + 1] = marker * span
            index = end + 1
            continue
        index += 1
    return "".join(chars), tuple(substitutions), mutating_arithmetic


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
        if quote is not None or char not in "<>":
            index += 1
            continue

        operator_start = index
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
            target_end += 1

        raw_operator = value[operator_start:operator_end]
        raw_target = value[target_start:target_end]
        if raw_target:
            redirections.append(
                ShellRedirection(
                    operator=raw_operator,
                    target=_dequote_shell_word(raw_target, posix=posix),
                )
            )
        else:
            ambiguous.append(value[operator_start:operator_end])
        chars[operator_start:target_end] = " " * (target_end - operator_start)
        index = max(target_end, operator_end)

    return "".join(chars), tuple(redirections), tuple(ambiguous)


def _split_shell_segments(value: str) -> tuple[list[str], bool]:
    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    has_control = False
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
            segments.append(value[start:index])
            has_control = True
            if index + 1 < len(value) and value[index + 1] == char and char in "&|":
                index += 1
            start = index + 1
        index += 1
    segments.append(value[start:])
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


def _shell_script_argument(invocation: ShellInvocation) -> str | None:
    executable = _command_name(invocation.executable)
    if executable == "eval":
        return " ".join(invocation.arguments) or None
    if executable not in _SHELL_EXECUTABLES:
        return None
    arguments = invocation.arguments
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return None
        if argument == "-c" and index + 1 < len(arguments):
            return arguments[index + 1]
        if (
            argument.startswith("-")
            and not argument.startswith("--")
            and "c" in argument[1:]
            and index + 1 < len(arguments)
        ):
            return arguments[index + 1]
        option_name = argument.split("=", 1)[0]
        if option_name in _SHELL_OPTIONS_WITH_VALUES and "=" not in argument:
            index += 2
            continue
        if argument.startswith(("-", "+")):
            index += 1
            continue
        return None
    return None


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


def inspect_shell_command(
    command: str,
    *,
    posix: bool | None = None,
    _depth: int = 0,
    _variables: dict[str, str] | None = None,
) -> ShellInspection:
    """Inspect shell command structure without executing it.

    Parsing is intentionally bounded.  Unparseable regions are returned to the
    policy caller, which can fail closed only when its own capability appears
    in that region.
    """
    if posix is None:
        posix = platform.system() != "Windows"
    shell_text = _mask_shell_comments(_mask_heredoc_bodies(command))
    top_level_text, substitutions, mutating_arithmetic = _extract_and_mask_substitutions(shell_text)
    command_text, redirections, redirect_ambiguous = _extract_and_mask_redirections(
        top_level_text,
        posix=posix,
    )
    segments, has_control = _split_shell_segments(command_text)
    invocations: list[ShellInvocation] = []
    assigned_variables: set[str] = set()
    all_redirections = list(redirections)
    ambiguous = list(redirect_ambiguous)
    variables = dict(_variables or {})

    for raw_segment in segments:
        segment = raw_segment.strip()
        if not segment:
            continue
        try:
            words = shlex.split(segment, posix=posix)
        except ValueError:
            ambiguous.append(segment)
            continue
        if not posix:
            words = _strip_matching_quotes(words)
        words, split_string_ambiguous = _expand_env_split_strings(
            words,
            posix=posix,
        )
        if split_string_ambiguous:
            ambiguous.append(segment)
            continue
        segment_assignments = _leading_assignments(words)
        executable_index = _executable_index(words)
        if executable_index is None:
            assigned_variables.update(segment_assignments)
            variables.update(segment_assignments)
            continue
        substitution_sources = tuple(
            source
            for source_index, source in enumerate(substitutions)
            if _substitution_marker(source_index) in words[executable_index]
        )
        parameter_sources = tuple(
            match.group(0)
            for match in _PARAMETER_EXPANSION_RE.finditer(words[executable_index])
        )
        dynamic_executable_sources = tuple(
            dict.fromkeys((*substitution_sources, *parameter_sources))
        )
        invocation = ShellInvocation(
            tuple(words),
            executable_index,
            segment,
            dynamic_executable_sources=dynamic_executable_sources,
            dynamic_executable_evidence=(
                _dynamic_executable_evidence(
                    words[executable_index],
                    substitutions,
                    variables,
                )
                if dynamic_executable_sources
                else ""
            ),
        )
        invocations.append(invocation)

        invocation_variables = {**variables, **segment_assignments}
        if _command_name(invocation.executable) in {
            "declare",
            "export",
            "readonly",
            "typeset",
        }:
            variables.update(_leading_assignments(list(invocation.arguments)))

        if script := _shell_script_argument(invocation):
            script = _expand_known_parameters(script, invocation_variables)
            if _depth < _MAX_NESTED_SHELL_DEPTH:
                nested = inspect_shell_command(
                    script,
                    posix=posix,
                    _depth=_depth + 1,
                    _variables=invocation_variables,
                )
                invocations.extend(
                    replace(nested_invocation, indirect=True)
                    for nested_invocation in nested.invocations
                )
                all_redirections.extend(
                    replace(redirection, indirect=True)
                    for redirection in nested.redirections
                )
                assigned_variables.update(nested.assigned_variables)
                mutating_arithmetic |= nested.mutating_arithmetic
                ambiguous.extend(nested.ambiguous_regions)
            else:
                ambiguous.append(script)

        for embedded_command in _find_embedded_commands(invocation):
            if _depth < _MAX_NESTED_SHELL_DEPTH:
                nested = inspect_shell_command(
                    embedded_command,
                    posix=posix,
                    _depth=_depth + 1,
                    _variables=invocation_variables,
                )
                invocations.extend(
                    replace(nested_invocation, indirect=True)
                    for nested_invocation in nested.invocations
                )
                all_redirections.extend(
                    replace(redirection, indirect=True)
                    for redirection in nested.redirections
                )
                assigned_variables.update(nested.assigned_variables)
                mutating_arithmetic |= nested.mutating_arithmetic
                ambiguous.extend(nested.ambiguous_regions)
            else:
                ambiguous.append(embedded_command)

    if _depth < _MAX_NESTED_SHELL_DEPTH:
        for substitution in substitutions:
            nested = inspect_shell_command(
                substitution,
                posix=posix,
                _depth=_depth + 1,
                _variables=variables,
            )
            invocations.extend(
                replace(nested_invocation, indirect=True)
                for nested_invocation in nested.invocations
            )
            all_redirections.extend(
                replace(redirection, indirect=True)
                for redirection in nested.redirections
            )
            assigned_variables.update(nested.assigned_variables)
            mutating_arithmetic |= nested.mutating_arithmetic
            ambiguous.extend(nested.ambiguous_regions)
    else:
        ambiguous.extend(substitutions)

    return ShellInspection(
        invocations=tuple(invocations),
        has_control_operators=has_control,
        substitutions=substitutions,
        redirections=tuple(all_redirections),
        ambiguous_regions=tuple(ambiguous),
        shell_text=shell_text,
        assigned_variables=frozenset(assigned_variables),
        mutating_arithmetic=mutating_arithmetic,
    )
