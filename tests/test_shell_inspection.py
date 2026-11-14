"""Observable behavior tests for shell command inspection."""

import pytest

from box_agent.tools.shell_inspection import inspect_shell_command


def _executables(command: str) -> list[str]:
    return [
        invocation.executable.replace("\\", "/").rsplit("/", 1)[-1]
        for invocation in inspect_shell_command(command, posix=True).invocations
    ]


def test_command_parameter_bindings_follow_assignment_order():
    inspection = inspect_shell_command(
        'DEST=/first; cp input "$DEST"; DEST=/second; cp input "${DEST}/file"',
        posix=True,
    )
    first, second = inspection.invocations
    assert first.expand_known_parameters(first.arguments[-1]) == "/first"
    assert second.expand_known_parameters(second.arguments[-1]) == "/second/file"


@pytest.mark.parametrize("nested", [False, True])
def test_environment_parameter_bindings_are_not_in_inspection_repr(nested):
    command = 'cp input "$DEST"'
    if nested:
        command = "bash -c '" + command + "'"
    inspection = inspect_shell_command(
        command, posix=True, environment={"DEST": "/private-environment-marker"},
    )
    invocation = inspection.invocations[-1]
    assert invocation.expand_known_parameters(invocation.arguments[-1]) == "/private-environment-marker"
    assert "/private-environment-marker" not in repr(inspection)


def test_heredoc_body_is_not_parsed_as_shell_commands():
    command = """python3 << 'PY'\n# Let's discuss Q1'24 values.\ndd = 12\nformat = 'wide'\nPY"""

    inspection = inspect_shell_command(command, posix=True)

    assert _executables(command) == ["python3"]
    assert inspection.ambiguous_regions == ()


def test_non_identifier_heredoc_delimiters_mask_only_their_bodies():
    command = "python3 <<EOF-1\nrm body.txt\nEOF-1\nrm live.txt"

    assert _executables(command) == ["python3", "rm"]


def test_numeric_heredoc_delimiter_is_supported():
    command = "python3 <<123\nrm body.txt\n123"

    assert _executables(command) == ["python3"]


def test_quoted_and_commented_heredoc_markers_do_not_mask_later_commands():
    quoted = "python3 -c \"print('<<EOF')\"\nrm quoted.tmp"
    commented = "echo ok # <<EOF\nrm commented.tmp"

    assert _executables(quoted) == ["python3", "rm"]
    assert _executables(commented) == ["echo", "rm"]


def test_multiline_quoted_shift_operator_does_not_mask_later_command():
    command = 'python3 -c "x=1\nprint(x << 2)\n"\nrm live.tmp'

    assert _executables(command) == ["python3", "rm"]


def test_extracts_chained_wrapped_and_nested_shell_invocations():
    command = (
        "echo ready && env MODE=test dd if=in of=out; "
        "bash -c 'rm cache.tmp'"
    )

    inspection = inspect_shell_command(command, posix=True)

    assert _executables(command) == ["echo", "dd", "bash", "rm"]
    assert [invocation.indirect for invocation in inspection.invocations] == [
        False,
        False,
        False,
        True,
    ]


def test_extracts_commands_from_subshell_and_brace_groups():
    command = "(rm first.tmp); { rm second.tmp; }"

    assert _executables(command) == ["rm", "rm"]


def test_depth_limited_nested_shell_payload_is_reported_as_ambiguous():
    command = "bash -c \"bash -c \\\"bash -c 'rm cache.tmp'\\\"\""

    inspection = inspect_shell_command(command, posix=True)

    assert "rm cache.tmp" in inspection.ambiguous_regions


def test_shell_arithmetic_is_data_but_nested_command_substitutions_are_inspected():
    arithmetic_only = "echo $((dd << 1)); (( dws = 1 ))\nrm live.tmp"
    with_substitution = "echo $(( $(rm cache.tmp) + 1 ))"

    assert _executables(arithmetic_only) == ["echo", "rm"]
    assert _executables(with_substitution) == ["echo", "rm"]


def test_extracts_command_substitution_without_treating_quoted_source_as_shell():
    command = "echo $(rm cache.tmp) && python3 -c \"print('format report')\""

    inspection = inspect_shell_command(command, posix=True)

    assert _executables(command) == ["echo", "python3", "rm"]
    assert inspection.substitutions == ("rm cache.tmp",)
    assert inspection.invocations[-1].indirect


def test_marks_only_substitutions_used_as_the_executable_as_dynamic():
    inspection = inspect_shell_command(
        'value="$(echo dws)"; $(echo dws) auth login',
        posix=True,
    )

    assert inspection.invocations[0].dynamic_executable_sources == ("echo dws",)


def test_wrapper_options_do_not_hide_the_real_executable():
    command = "env -u HOME dws auth status; sudo -u root rm cache.tmp"

    assert _executables(command) == ["dws", "rm"]


def test_redirections_are_structured_and_do_not_hide_the_executable():
    inspection = inspect_shell_command(
        ">audit.log 2>>errors.log rm cache.tmp",
        posix=True,
    )

    assert _executables(">audit.log 2>>errors.log rm cache.tmp") == ["rm"]
    assert [
        (redirection.operator, redirection.target)
        for redirection in inspection.redirections
    ] == [(">", "audit.log"), ("2>>", "errors.log")]


def test_quoted_redirection_text_is_data_not_shell_syntax():
    inspection = inspect_shell_command(
        "python3 -c \"print('> /etc/example')\"",
        posix=True,
    )

    assert inspection.redirections == ()


def test_reports_unparseable_top_level_region():
    inspection = inspect_shell_command("echo 'unterminated", posix=True)

    assert inspection.invocations == ()
    assert inspection.ambiguous_regions == ("echo 'unterminated",)


@pytest.mark.parametrize(
    "command",
    [
        'cmd=rm; "$cmd" cache.tmp',
        "cmd=rm; ${cmd} cache.tmp",
        "ref=cmd; cmd=rm; ${!ref} cache.tmp",
    ],
)
def test_parameter_expansion_in_executable_positions_is_dynamic(command: str):
    inspection = inspect_shell_command(command, posix=True)

    assert any(
        invocation.dynamic_executable_sources
        for invocation in inspection.invocations
    )


def test_known_parameterized_shell_payload_is_inspected_after_expansion():
    inspection = inspect_shell_command(
        "payload='rm cache.tmp'; bash -c \"$payload\"",
        posix=True,
    )

    assert [invocation.executable for invocation in inspection.invocations] == [
        "bash",
        "rm",
    ]
    assert inspection.invocations[-1].indirect


def test_tracks_standalone_assignments_in_nested_and_top_level_shell_commands():
    inspection = inspect_shell_command(
        "FIRST=one SECOND=two; bash -c 'INNER=three; echo ok'", posix=True
    )

    assert inspection.assigned_variables == frozenset({"FIRST", "SECOND", "INNER"})


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('cmd=rm; "$cmd" cache.tmp', "rm"),
        ("ref=cmd; cmd=dws; ${!ref} auth login", "dws"),
        ("D=d\"w\"s; $D auth login", "dws"),
        ("$(printf d)ws auth login", "printf dws"),
        ("$(printf git) dws.txt", "printf git"),
    ],
)
def test_dynamic_executable_evidence_excludes_unrelated_arguments(
    command: str,
    expected: str,
):
    inspection = inspect_shell_command(command, posix=True)
    dynamic = next(
        invocation
        for invocation in inspection.invocations
        if invocation.dynamic_executable_sources
    )

    assert dynamic.dynamic_executable_evidence == expected


@pytest.mark.parametrize(
    "command",
    [
        "bash --norc -c 'rm cache.tmp'",
        "bash --rcfile /dev/null -c 'rm cache.tmp'",
        "bash -O extglob -c 'rm cache.tmp'",
    ],
)
def test_shell_options_before_c_do_not_hide_nested_script(command: str):
    inspection = inspect_shell_command(command, posix=True)

    assert [invocation.executable for invocation in inspection.invocations] == [
        "bash",
        "rm",
    ]
    assert inspection.invocations[-1].indirect


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("eval 'rm cache.tmp'", ["eval", "rm"]),
        ("printf 'cache.tmp\\n' | xargs rm", ["printf", "rm"]),
        ("timeout 5 rm cache.tmp", ["rm"]),
        ("nice -n 5 rm cache.tmp", ["rm"]),
        ("chroot /tmp rm cache.tmp", ["rm"]),
        ("busybox rm cache.tmp", ["rm"]),
        ("/usr/bin/env MODE=test /bin/rm cache.tmp", ["/bin/rm"]),
        ("env -S 'rm cache.tmp'", ["rm"]),
        ("env --split-string='rm cache.tmp'", ["rm"]),
        ("/usr/bin/timeout 5 /bin/rm cache.tmp", ["/bin/rm"]),
        ("find . -name '*.tmp' -exec rm {} +", ["find", "rm"]),
    ],
)
def test_execution_wrappers_expose_dispatched_commands(command: str, expected: list[str]):
    inspection = inspect_shell_command(command, posix=True)

    assert [invocation.executable for invocation in inspection.invocations] == expected


def test_redirections_keep_command_variable_snapshots_and_literal_quotes():
    inspection = inspect_shell_command(
        "TARGET=first; printf x > \"$TARGET\"; TARGET=second; printf y > \"$TARGET\"; printf z > '$TARGET'",
        posix=True,
        environment={"PRIVATE_VALUE": "host-private-value"},
    )
    assert [redirect.expand_target() for redirect in inspection.redirections] == ["first", "second", "$TARGET"]
    assert [redirect.invocation_index for redirect in inspection.redirections] == [0, 1, 2]
    assert "host-private-value" not in repr(inspection)


@pytest.mark.parametrize("command, expected", [
    ('TARGET=before; TARGET=after > "$TARGET"', "after"),
    ('TARGET=before; TARGET=after printf x > "$TARGET"', "before"),
])
def test_redirection_expansion_respects_assignment_only_and_command_prefixes(command, expected):
    inspection = inspect_shell_command(command, posix=True)
    assert inspection.redirections[0].expand_target() == expected


def test_nested_redirections_retain_parent_invocation_offsets_and_shell_depth():
    inspection = inspect_shell_command(
        "printf top; bash -c 'TARGET=child; printf x > \"$TARGET\"'; printf after > top.txt",
        posix=True,
    )
    assert [(redirect.invocation_index, redirect.shell_depth, redirect.expand_target())
            for redirect in inspection.redirections] == [(2, 1, "child"), (3, 0, "top.txt")]


def test_binding_snapshots_resolve_chained_assignments_before_later_reassignment():
    inspection = inspect_shell_command(
        'ROOT=skills; TARGET="$ROOT/validator.js"; ROOT=ordinary; printf x > "$TARGET"; cp patch.js "$TARGET"',
        posix=True,
    )
    assert inspection.redirections[0].expand_target() == "skills/validator.js"
    assert inspection.invocations[-1].expand_known_parameters("$TARGET") == "skills/validator.js"


def test_binding_snapshots_resolve_host_and_declared_variables_without_exposing_environment():
    inspection = inspect_shell_command(
        'export ROOT="$HOST_PATH"; TARGET="$ROOT/validator.js"; printf x > "$TARGET"',
        posix=True,
        environment={"HOST_PATH": "/host-private-path"},
    )
    assert inspection.redirections[0].expand_target() == "/host-private-path/validator.js"
    assert "/host-private-path" not in repr(inspection)


def test_env_wrapper_binds_child_environment_and_directory_without_changing_outer_redirection():
    inspection = inspect_shell_command(
        "env -C product DEST=product bash -c 'cp -R input/. \"$DEST\"' > parent.log",
        posix=True,
    )
    child = next(invocation for invocation in inspection.invocations if invocation.executable == "cp")
    assert child.expand_known_parameters("$DEST") == "product"
    assert child.directory_changes == ("product",)
    assert inspection.redirections[0].directory_changes == ()


def test_env_clear_and_unset_are_retained_in_nested_snapshots():
    for prefix in ('env -i', 'env -u HOST_PATH'):
        inspection = inspect_shell_command(
            prefix + " bash -c 'printf x > \"$HOST_PATH\"'", posix=True,
            environment={"HOST_PATH": "/host-private-path"},
        )
        assert inspection.redirections[0].expand_target() == "$HOST_PATH"
        assert "/host-private-path" not in repr(inspection)


def test_substitution_retains_owning_command_directory_and_bindings():
    inspection = inspect_shell_command(
        'cd product; TARGET=protected; printf "%s" "$(printf changed > "$TARGET")"; cd workspace; TARGET=ordinary',
        posix=True,
    )
    redirect = inspection.redirections[0]
    assert redirect.directory_changes == ("product",)
    assert redirect.expand_target() == "protected"


def test_env_option_values_are_not_reinterpreted_as_nested_wrappers():
    inspection = inspect_shell_command("env --argv0 env -C product bash -c 'printf x > output'", posix=True)
    assert inspection.redirections[0].directory_changes == ("product",)


@pytest.mark.parametrize("form", ["subshell", "pipeline", "background"])
def test_child_shell_bindings_and_directories_do_not_change_parent_facts(form):
    operators = {"subshell": '(cd child; TARGET=ordinary)', "pipeline": 'cd child | cat', "background": 'cd child & wait'}
    inspection = inspect_shell_command(
        f'cd product; TARGET=protected; {operators[form]}; printf changed > "$TARGET"', posix=True,
    )
    redirect = inspection.redirections[-1]
    assert redirect.directory_changes == ("product",)
    assert redirect.expand_target() == "protected"


@pytest.mark.parametrize("body", ["cd child && true", "cd child || false", "(cd child) && true", "cd child | cat && true"])
def test_async_and_or_lists_do_not_change_parent_directory_or_bindings(body):
    inspection = inspect_shell_command(
        f'cd product; TARGET=protected; {body} & wait; printf x > "$TARGET"', posix=True,
    )
    assert inspection.redirections[-1].directory_changes == ("product",)
    assert inspection.redirections[-1].expand_target() == "protected"


def test_commands_in_one_async_list_share_a_child_scope():
    inspection = inspect_shell_command(
        'cd product; cd child && printf x > output & wait; printf x > parent', posix=True,
    )
    assert [redirect.directory_changes for redirect in inspection.redirections] == [
        ("product", "child"), ("product",),
    ]


@pytest.mark.parametrize("operator", ["&>", "&>>"])
def test_combined_output_redirection_does_not_background_the_command(operator):
    inspection = inspect_shell_command(f'cd product {operator} output; printf x > target', posix=True)
    assert [redirect.operator for redirect in inspection.redirections] == [operator, ">"]
    assert inspection.redirections[-1].directory_changes == ("product",)
    numeric_argument = inspect_shell_command(f'printf 2{operator} output', posix=True)
    assert numeric_argument.invocations[0].arguments == ("2",)
    assert numeric_argument.redirections[0].operator == operator


def test_execution_sequence_keeps_substitution_effects_before_following_commands():
    inspection = inspect_shell_command('printf "%s" "$(mv before after)"; cd before; printf x > output', posix=True)
    # Public enumeration remains top-level-first, but execution facts retain
    # when substitution effects actually occur.
    assert [invocation.executable for invocation in inspection.invocations] == ["printf", "cd", "printf", "mv"]
    ordered = sorted(inspection.invocations, key=lambda invocation: invocation.sequence_index)
    assert [invocation.executable for invocation in ordered] == ["mv", "printf", "cd", "printf"]
