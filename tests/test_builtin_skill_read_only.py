"""Task tools cannot rewrite the bundled code they use to validate output."""
import os
from pathlib import Path

import pytest

from box_agent.tools import safety
from box_agent.tools.file_tools import WriteTool, AppendTool, EditTool
from box_agent.tools.staged_file_write_tool import StagedFileWriteTool
from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine


@pytest.fixture
def bundled(tmp_path, monkeypatch):
    root = tmp_path / "bundled"
    root.mkdir()
    monkeypatch.setattr(safety, "BUILTIN_SKILLS_ROOT", root)
    target = root / "validator.js"
    target.write_text("original")
    return root, target


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["write", "append", "edit", "staged"])
async def test_file_tools_reject_bundled_writes_even_with_full_access(tmp_path, bundled, kind):
    root, target = bundled
    link = tmp_path / "linked.js"
    link.symlink_to(target)
    classes = {"write": WriteTool, "append": AppendTool, "edit": EditTool, "staged": StagedFileWriteTool}
    tool = classes[kind](workspace_dir=str(tmp_path), allow_full_access=True)
    args = {"path": str(link)}
    if kind == "edit":
        args.update(old_str="original", new_str="bypass")
    elif kind == "staged":
        args.update(action="begin", expected_chunks=1)
    else:
        args.update(content="bypass")
    result = await tool.execute(**args)
    assert not result.success
    assert "BUILTIN_SKILL_READ_ONLY" in result.error
    assert target.read_text() == "original"
    assert not result.permission_request
    if kind != "staged":
        args["path"] = str(tmp_path / "output.txt")
        if kind == "edit":
            Path(args["path"]).write_text("original")
        assert (await tool.execute(**args)).success


@pytest.mark.parametrize("scope", ["session_workspace", "user_home", "full_access"])
def test_permission_scope_does_not_make_bundled_code_writable(tmp_path, bundled, scope):
    root, target = bundled
    engine = PermissionEngine(CapabilityPolicy(filesystem_scope=scope, allowed_directories=(str(root),)), tmp_path)
    engine._builtin_skills_dir = root
    denied = engine.check("filesystem.write", {"path": str(target)})
    assert not denied.allowed
    assert "BUILTIN_SKILL_READ_ONLY" in denied.reason
    assert denied.permission_request is None
    assert engine.check("filesystem.read", {"path": str(target)}, tool_name="bash").allowed


@pytest.mark.parametrize("command", [
    "sed -i '' s/original/bypass/ '{target}'",
    "cp patch.js '{target}'",
    "printf bypass > '{target}'",
    "cd '{root}' && node -e \"require('fs').writeFileSync('validator.js','bypass')\"",
    "python -c \"from pathlib import Path; Path('{target}').write_text('bypass')\"",
])
def test_shell_guard_rejects_direct_bundled_code_mutations(tmp_path, bundled, command):
    root, target = bundled
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(command.format(root=root, target=target), tmp_path)
    assert target.read_text() == "original"


@pytest.mark.parametrize("command", [
    "node '{target}' --out index.html",
    "cat '{target}'",
    "node -e \"console.log(require('fs').readFileSync('{target}','utf8'))\"",
    "cp '{target}' './copy.js'",
])
def test_shell_guard_keeps_bundled_read_and_execute_available(tmp_path, bundled, command):
    root, target = bundled
    assert safety.builtin_skill_command_write_error(command.format(target=target), tmp_path) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell destination expansion")
@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["file", "directory", "target-option", "variable", "touch"])
async def test_bash_destination_globs_keep_bundled_links_read_only(tmp_path, bundled, monkeypatch, form):
    from box_agent.tools.bash_tool import BashTool

    root, target = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    product = tmp_path / "product"
    product.mkdir()
    (workspace / "payload").write_text("changed")
    (workspace / target.name).write_text("changed")
    (product / "entry.js").symlink_to(target)
    (product / "dir-link").symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(product))
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    engine._builtin_skills_dir = root.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    commands = {
        "file": f'cp payload "{product}/"*.js',
        "directory": f'cp validator.js "{product}/"dir-*',
        "target-option": f'cp -t "{product}/"dir-* validator.js',
        "variable": f'DEST="{product}"; cp payload "$DEST/"*.js',
        "touch": f'touch "{product}/"*.js',
    }
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(commands[form], workspace)
    denied = await tool.execute(command=commands[form])
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    assert target.read_text() == "original"

    # Ordinary product globs and accepted links to unrelated external output work.
    external = tmp_path / "external.txt"
    external.write_text("old")
    (product / "ordinary.out").symlink_to(external)
    allowed = await tool.execute(command=f'cp payload "{product}/"*.out')
    assert allowed.success, allowed.error
    assert external.read_text() == "changed"


@pytest.mark.asyncio
async def test_bash_blocks_bundled_mutation_but_writes_workspace_output(tmp_path, bundled):
    from box_agent.tools.bash_tool import BashTool
    _, target = bundled
    tool = BashTool(workspace_dir=str(tmp_path), allow_full_access=True)
    denied = await tool.execute(command=f"node -e \"require('fs').writeFileSync('{target}','bypass')\"")
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    assert target.read_text() == "original"
    command = "Set-Content -LiteralPath result.txt -Value finished -NoNewline" if os.name == "nt" else "printf 'finished' > result.txt"
    allowed = await tool.execute(command=command)
    assert allowed.success
    assert (tmp_path / 'result.txt').read_text() == 'finished'


@pytest.mark.parametrize("arguments", [
    "-R source/. '{target}'",
    "-R -t '{target}' source/.",
    "-R -t'{target}' source/.",
    "-Rt'{target}' source/.",
    "-R --target-directory '{target}' source/.",
    "-R --target-directory='{target}' source/.",
    "--rec source/. '{target}'",
    "--parents source/skills/validator.js '{target}'",
    "-R --target-dir='{target}' source/.",
    "source/. '{target}' -R",
])
@pytest.mark.parametrize("executable", ["cp", "cp.exe"])
def test_shell_copy_target_options_cannot_overwrite_bundled_ancestors(tmp_path, bundled, arguments, executable):
    root, _ = bundled
    command = f"{executable} " + arguments.format(target=root.parent)
    error = safety.builtin_skill_command_write_error(command, tmp_path)
    assert error is not None
    assert "BUILTIN_SKILL_READ_ONLY" in error


@pytest.mark.parametrize("arguments", [
    "-- -t copied.txt",
    "-S -t source copied.txt",
    "-St source copied.txt",
    "-- -R .",
    "-S -R source .",
])
def test_shell_copy_t_in_file_names_and_suffix_values_is_not_a_target_option(tmp_path, bundled, arguments):
    assert safety.builtin_skill_command_write_error(f"cp {arguments}", tmp_path) is None


@pytest.mark.parametrize("target_option", [
    "-t '{target}'", "-t'{target}'", "--target-directory '{target}'", "--target-directory='{target}'",
])
def test_install_target_options_reject_bundled_destinations(tmp_path, bundled, target_option):
    root, _ = bundled
    command = "install " + target_option.format(target=root) + " validator.js"
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(command, tmp_path)


def test_install_mode_after_target_does_not_replace_the_write_destination(tmp_path, bundled):
    root, _ = bundled
    command = f"install validator.js '{root}' --mode 'u=rw,go=r'"
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(command, tmp_path)


@pytest.mark.parametrize("arguments", [
    "-a source/ '{target}/'",
    "source/ '{target}/' -a",
    "-a source/ '{target}/' --exclude '*.tmp'",
    "-a source/ '{target}/' -e 'ssh -p 22'",
    "-a -- source/ '{target}/'",
])
def test_rsync_write_target_options_protect_bundled_ancestors(tmp_path, bundled, arguments):
    root, _ = bundled
    command = "rsync " + arguments.format(target=root.parent)
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(command, tmp_path)


def test_rsync_filter_value_does_not_become_the_write_target(tmp_path, bundled):
    root, _ = bundled
    command = f"rsync -a source/ copied/ --exclude '{root.parent}'"
    assert safety.builtin_skill_command_write_error(command, tmp_path) is None


@pytest.mark.parametrize("arguments", [
    "cp '{source}' '{product}'",
    "cp '{source}' other.js '{product}'",
    "cp -t '{product}' '{source}'",
    "cp --target-directory='{product}' '{source}'",
    "install '{source}' '{product}' --mode 644",
    "install -t'{product}' '{source}'",
])
def test_copy_into_directory_checks_each_existing_destination_link(tmp_path, bundled, arguments):
    _, target = bundled
    product = tmp_path / "product"
    product.mkdir()
    (product / target.name).symlink_to(target)
    source = tmp_path / target.name
    source.write_text("changed")
    command = arguments.format(source=source, product=product)
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(command, tmp_path)
    assert safety.builtin_skill_command_write_error(f"cp -T '{source}' '{product}'", tmp_path) is None
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary")
    assert safety.builtin_skill_command_write_error(f"cp '{ordinary}' '{product}'", tmp_path) is None
    assert target.read_text() == "original"


@pytest.mark.skipif(os.name == "nt", reason="POSIX cp and symlink syntax")
@pytest.mark.asyncio
@pytest.mark.parametrize("source_form", ["literal", "glob", "relative-glob", "inline-variable-glob", "environment-variable-glob", "runtime-variable-glob"])
async def test_bash_copy_to_directory_cannot_follow_link_into_bundled_code(tmp_path, bundled, monkeypatch, source_form):
    from box_agent.tools.bash_tool import BashTool

    root, target = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    product = tmp_path / "product"
    product.mkdir()
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(product))
    if source_form == "environment-variable-glob":
        monkeypatch.setenv("BOX_AGENT_COPY_SOURCE", str(workspace))
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    engine._builtin_skills_dir = root.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    source = workspace / target.name
    source.write_text("changed")
    linked = await tool.execute(command=f'ln -s "{target}" "{product / target.name}"')
    assert linked.success, linked.error
    command = f'cp "{source}" "{product}/"'
    if source_form == "glob":
        command = f'cp "{workspace}"/*.js "{product}/"'
    elif source_form == "relative-glob":
        command = f'cp ./*.js "{product}/"'
    elif source_form == "inline-variable-glob":
        command = f'SOURCE="{workspace}"; cp "$SOURCE"/*.js "{product}/"'
    elif source_form == "environment-variable-glob":
        command = f'cp "$BOX_AGENT_COPY_SOURCE"/*.js "{product}/"'
    elif source_form == "runtime-variable-glob":
        tool.update_runtime_env({"COPY_SOURCE": str(workspace)})
        command = f'cp "$COPY_SOURCE"/*.js "{product}/"'
    denied = await tool.execute(command=command)
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    assert denied.permission_request is None
    assert target.read_text() == "original"
    ordinary = workspace / "ordinary.txt"
    ordinary.write_text("ordinary")
    allowed = await tool.execute(command=f'cp "{workspace}"/*.txt "{product}/"')
    assert allowed.success, allowed.error
    assert (product / ordinary.name).read_text() == "ordinary"


@pytest.mark.skipif(os.name == "nt", reason="POSIX recursive cp syntax")
@pytest.mark.asyncio
@pytest.mark.parametrize("executable", ["cp", "rsync"])
@pytest.mark.parametrize("target_form", ["root", "alias", "relative", "inline-variable", "environment-variable", "runtime-variable"])
async def test_bash_rejects_recursive_overwrite_of_bundled_descendants(
    tmp_path, monkeypatch, target_form, executable,
):
    import shutil

    from box_agent.tools.bash_tool import BashTool

    if shutil.which(executable) is None:
        pytest.skip(f"{executable} is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    product = tmp_path / "product"
    builtin = product / "skills"
    builtin.mkdir(parents=True)
    validator = builtin / "validator.js"
    validator.write_text("original")
    source = workspace / "input"
    (source / "skills").mkdir(parents=True)
    (source / "skills" / "validator.js").write_text("changed")
    entry = tmp_path / "product-entry"
    entry.symlink_to(product, target_is_directory=True)
    monkeypatch.setattr(safety, "BUILTIN_SKILLS_ROOT", builtin)
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(product))
    engine = PermissionEngine(CapabilityPolicy(session_workspace_root=str(workspace)), workspace)
    engine._builtin_skills_dir = builtin.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    target = entry if target_form == "alias" else product
    copy_prefix = f'cp -R "{source}/."' if executable == "cp" else f'rsync -a "{source}/"'
    command = f'{copy_prefix} "{target}/"'
    if target_form == "relative":
        command = f'cd "{entry}" && {copy_prefix} .'
    elif target_form == "inline-variable":
        command = f'DEST="{product}"; {copy_prefix} "$DEST"'
    elif target_form == "environment-variable":
        command = f'{copy_prefix} "$BOX_AGENT_CLIENT_ROOT"'
    elif target_form == "runtime-variable":
        tool.update_runtime_env({"COPY_TARGET": str(product)})
        command = f'{copy_prefix} "$COPY_TARGET"'
    result = await tool.execute(command=command)
    assert not result.success
    assert "BUILTIN_SKILL_READ_ONLY" in result.error
    assert result.permission_request is None
    assert validator.read_text() == "original"

    exported = workspace / "exported"
    export_command = (
        f'cp -R "{product}" "{exported}"' if executable == "cp"
        else f'rsync -a "{product}/" "{exported}/"'
    )
    result = await tool.execute(command=export_command)
    assert result.success, result.error
    assert (exported / "skills" / "validator.js").read_text() == "original"
    ordinary = workspace / "ordinary.txt"
    ordinary.write_text("ordinary product data")
    if executable == "cp":
        result = await tool.execute(command=f'cp "{ordinary}" "{product}/"')
        assert result.success, result.error
    result = await tool.execute(command=f'{executable} "{ordinary}" "{product / "ordinary.txt"}"')
    assert result.success, result.error
    tool.update_runtime_env({"COPY_TARGET": str(product / "ordinary.txt")})
    result = await tool.execute(command=f'{executable} "{ordinary}" "$COPY_TARGET"')
    assert result.success, result.error
    result = await tool.execute(command=f'printf updated > "{product / "ordinary.txt"}"')
    assert result.success, result.error
    assert (product / "ordinary.txt").read_text() == "updated"


def test_bundled_copy_checks_variable_values_in_command_order(tmp_path, bundled):
    root, _ = bundled
    command = f'DEST="{root.parent}"; cp -R input/. "$DEST"; DEST="{tmp_path / "safe"}"'
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(command, tmp_path)
    command = f'DEST="{root.parent}"; DEST="{tmp_path / "safe"}" cp -R input/. "$DEST"'
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(command, tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX redirection and shell syntax")
@pytest.mark.asyncio
@pytest.mark.parametrize("target_form", ["inline", "environment", "runtime", "assignment-only", "append", "reassigned", "nested", "nested-double", "environment-nested-double", "prefix", "cd-redirect", "chained-relative", "chained-reassigned"])
async def test_bash_variable_redirections_cannot_modify_bundled_code(tmp_path, bundled, monkeypatch, target_form):
    from box_agent.tools.bash_tool import BashTool

    root, target = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(tmp_path))
    if target_form in {"environment", "prefix", "environment-nested-double"}:
        monkeypatch.setenv("REDIRECT_TARGET", str(target))
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    engine._builtin_skills_dir = root.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    command = f'cd "{tmp_path}" && TARGET=bundled/validator.js && printf changed > "$TARGET"'
    if target_form == "environment":
        command = 'printf changed > "$REDIRECT_TARGET"'
    elif target_form == "runtime":
        tool.update_runtime_env({"REDIRECT_TARGET": str(target)})
        command = 'printf changed > "$REDIRECT_TARGET"'
    elif target_form == "assignment-only":
        command = f'cd "{tmp_path}"; TARGET=bundled/validator.js > "$TARGET"'
    elif target_form == "append":
        command = f'cd "{tmp_path}"; TARGET=bundled/validator.js; printf changed >> "$TARGET"'
    elif target_form == "reassigned":
        command = f'TARGET=ordinary.txt; TARGET="{target}"; printf changed > "$TARGET"'
    elif target_form == "nested":
        command = f"bash -c 'cd \"{tmp_path}\" && TARGET=bundled/validator.js && printf changed > \"$TARGET\"'"
    elif target_form == "nested-double":
        command = f'TARGET="{target}"; bash -c "TARGET=ordinary.txt; printf changed > $TARGET"'
    elif target_form == "environment-nested-double":
        command = 'bash -c "REDIRECT_TARGET=ordinary.txt; printf changed > $REDIRECT_TARGET"'
    elif target_form == "prefix":
        command = 'REDIRECT_TARGET=ordinary.txt printf changed > "$REDIRECT_TARGET"'
    elif target_form == "cd-redirect":
        command = f'cd "{root}"; cd "{workspace}" > validator.js'
    elif target_form == "chained-relative":
        command = f'cd "{tmp_path}"; ROOT=bundled; TARGET="$ROOT/validator.js"; printf changed > "$TARGET"'
    elif target_form == "chained-reassigned":
        command = f'cd "{tmp_path}"; ROOT=bundled; TARGET="$ROOT/validator.js"; ROOT=ordinary; printf changed > "$TARGET"'
    denied = await tool.execute(command=command)
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    assert denied.permission_request is None
    assert target.read_text() == "original"

    ordinary = tmp_path / "ordinary.txt"
    allowed = await tool.execute(command=f'TARGET="{ordinary}"; printf ordinary > "$TARGET"')
    assert allowed.success, allowed.error
    assert ordinary.read_text() == "ordinary"


@pytest.mark.skipif(os.name == "nt", reason="POSIX nested shell syntax")
@pytest.mark.asyncio
async def test_child_shell_directory_does_not_change_parent_redirection_target(tmp_path, bundled, monkeypatch):
    from box_agent.tools.bash_tool import BashTool

    root, target = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(tmp_path))
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    engine._builtin_skills_dir = root.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    command = f"bash -c 'cd \"{root}\"'; printf ordinary > validator.js"
    allowed = await tool.execute(command=command)
    assert allowed.success, allowed.error
    assert (workspace / "validator.js").read_text() == "ordinary"
    assert target.read_text() == "original"


def test_numeric_file_redirection_is_protected_but_descriptor_duplication_is_allowed(tmp_path, bundled):
    root, _ = bundled
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(f'cd "{root}"; printf changed > 2', tmp_path)
    assert safety.builtin_skill_command_write_error(f'cd "{root}"; FD=2; printf output 1>&"$FD"', tmp_path) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell execution contexts")
@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["env-binding", "env-cwd-copy", "env-cwd-redirect", "subshell", "pipeline", "background", "substitution", "future-glob", "future-parent-glob", "async-and", "async-or", "combined-redirect", "child-variable"])
async def test_bash_execution_contexts_preserve_bundled_write_protection(tmp_path, bundled, monkeypatch, form):
    from box_agent.tools.bash_tool import BashTool

    root, target = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "input" / root.name
    source.mkdir(parents=True)
    (source / target.name).write_text("changed")
    (tmp_path / target.name).symlink_to(target)
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(tmp_path))
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    engine._builtin_skills_dir = root.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    commands = {
        "env-binding": f"env DEST=\"{tmp_path}\" bash -c 'cp -R input/. \"$DEST\"'",
        "env-cwd-copy": f'env -C "{tmp_path}" cp -R "{workspace}/input/." .',
        "env-cwd-redirect": f"env -C \"{root}\" bash -c 'printf changed > validator.js'",
        "subshell": f'cd "{tmp_path}"; (cd "{workspace}"); printf changed > bundled/validator.js',
        "pipeline": f'cd "{tmp_path}"; cd "{workspace}" | cat; printf changed > bundled/validator.js',
        "background": f'cd "{tmp_path}"; cd "{workspace}" & wait; printf changed > bundled/validator.js',
        "substitution": f'cd "{tmp_path}"; printf "%s" "$(printf changed > bundled/validator.js)"; cd "{workspace}"',
        "future-glob": f'printf changed > validator.js; cp ./*.js "{tmp_path}"',
        "future-parent-glob": f'mkdir -p generated/run; printf changed > generated/run/validator.js; cp generated/*/validator.js "{tmp_path}"',
        "async-and": f'cd "{tmp_path}"; cd "{workspace}" && true & wait; printf changed > bundled/validator.js',
        "async-or": f'cd "{tmp_path}"; cd "{workspace}" || false & wait; printf changed > bundled/validator.js',
        "combined-redirect": f'cd "{tmp_path}" &> /dev/null; printf changed > bundled/validator.js',
        "child-variable": f'cd "{tmp_path}"; TARGET=bundled/validator.js; (TARGET=ordinary.txt); printf changed > "$TARGET"',
    }
    denied = await tool.execute(command=commands[form])
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    assert denied.permission_request is None
    assert target.read_text() == "original"
    if form == "future-glob":
        assert not (workspace / target.name).exists()
    elif form == "future-parent-glob":
        assert not (workspace / "generated").exists()

    # Child directory changes must also leave an ordinary parent write usable.
    allowed = await tool.execute(command=f'(cd "{root}"); printf ordinary > validator.js')
    assert allowed.success, allowed.error
    assert (workspace / target.name).read_text() == "ordinary"
    assert target.read_text() == "original"


@pytest.mark.skipif(os.name == "nt", reason="POSIX cd and redirection syntax")
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "file", "broken-link"])
async def test_failed_cd_does_not_hide_a_write_in_the_current_bundled_directory(tmp_path, bundled, monkeypatch, failure):
    from box_agent.tools.bash_tool import BashTool

    root, target = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    destination = workspace / "unavailable"
    if failure == "file":
        destination.write_text("not a directory")
    elif failure == "broken-link":
        destination.symlink_to(workspace / "missing-directory")
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(tmp_path))
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    engine._builtin_skills_dir = root.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    command = f'cd "{root}"; cd "{destination}"; printf changed > validator.js; cd "{root}"'
    denied = await tool.execute(command=command)
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    assert denied.permission_request is None
    assert target.read_text() == "original"

    allowed = await tool.execute(command=f'cd "{root}"; cd "{workspace}"; printf ordinary > validator.js')
    assert allowed.success, allowed.error
    assert (workspace / "validator.js").read_text() == "ordinary"
    assert target.read_text() == "original"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mkdir, cd and redirection")
@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["redirect", "copy"])
async def test_directory_created_before_cd_cannot_hide_bundled_write(tmp_path, bundled, monkeypatch, writer):
    from box_agent.tools.bash_tool import BashTool

    root, target = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "patch.js"
    source.write_text("changed")
    created = tmp_path / "created"
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(tmp_path))
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    engine._builtin_skills_dir = root.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    write = 'printf changed > ../bundled/validator.js' if writer == "redirect" else f'cp "{source}" ../bundled/validator.js'
    denied = await tool.execute(command=f'mkdir "{created}"; cd "{created}"; {write}')
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    assert target.read_text() == "original"
    assert not created.exists()

    ordinary = workspace / "ordinary.txt"
    allowed = await tool.execute(command=f'mkdir "{created}"; cd "{created}"; printf ordinary > ordinary.txt; printf ordinary > "{ordinary}"')
    assert allowed.success, allowed.error
    assert (created / "ordinary.txt").read_text() == "ordinary"
    assert ordinary.read_text() == "ordinary"
    assert target.read_text() == "original"


@pytest.mark.skipif(os.name == "nt", reason="POSIX cd syntax")
@pytest.mark.asyncio
async def test_many_uncertain_directories_do_not_block_absolute_workspace_output(tmp_path, bundled, monkeypatch):
    from box_agent.tools.bash_tool import BashTool

    root, target = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(tmp_path))
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    engine._builtin_skills_dir = root.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    changes = "; ".join(f"cd missing{index}" for index in range(9))
    denied = await tool.execute(command=f'cd "{root}"; {changes}; printf changed > validator.js')
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    assert target.read_text() == "original"
    output = workspace / "ordinary.txt"
    allowed = await tool.execute(command=f'cd "{root}"; {changes}; printf ordinary > "{output}"')
    assert allowed.success, allowed.error
    assert output.read_text() == "ordinary"
    assert target.read_text() == "original"


@pytest.mark.skipif(os.name == "nt", reason="POSIX move, cd and redirection")
@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_directory_moved_before_cd_preserves_bundled_write_protection(tmp_path, bundled, monkeypatch, wrapped):
    from box_agent.tools.bash_tool import BashTool

    root, target = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    moved = tmp_path / "moved"
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(tmp_path))
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    engine._builtin_skills_dir = root.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    move = 'mv ../ordinary ../moved'
    if wrapped:
        move = 'printf "%s" "$(mv ../ordinary ../moved)"'
    # Keep the protected cd path relative so the legacy coarse mutator-path
    # classifier cannot mask this command-guard regression.
    command = f'cd "{tmp_path}"; cd bundled; {move}; cd ../ordinary; printf changed > validator.js; cd ../bundled'
    assert "BUILTIN_SKILL_READ_ONLY" in safety.builtin_skill_command_write_error(command, workspace, environment=tool._subprocess_env)
    denied = await tool.execute(command=command)
    assert not denied.success
    assert "BUILTIN_SKILL_READ_ONLY" in denied.error
    assert target.read_text() == "original"
    assert ordinary.is_dir()
    assert not moved.exists()

    # A move elsewhere does not make a valid workspace cd uncertain.
    allowed = await tool.execute(command=f'cd "{tmp_path}"; cd bundled; mv ../ordinary ../moved; cd ../workspace; printf ordinary > validator.js')
    assert allowed.success, allowed.error
    assert (workspace / "validator.js").read_text() == "ordinary"
    assert moved.is_dir()
    assert target.read_text() == "original"


@pytest.mark.skipif(os.name == "nt", reason="POSIX copy, move and cd")
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["copy-before", "move-after"])
async def test_unrelated_directory_operations_keep_normal_cd_writes_available(tmp_path, bundled, monkeypatch, operation):
    from box_agent.tools.bash_tool import BashTool

    root, target = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    source = workspace / "data.txt"
    source.write_text("data")
    monkeypatch.setenv("BOX_AGENT_CLIENT_ROOT", str(tmp_path))
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    engine._builtin_skills_dir = root.resolve()
    tool = BashTool(workspace_dir=str(workspace), permission_engine=engine)
    if operation == "copy-before":
        command = f'cd "{tmp_path}"; cd bundled; cp "{source}" "{ordinary}"; cd ../ordinary; printf ordinary > validator.js'
        output = ordinary / "validator.js"
    else:
        command = f'cd "{tmp_path}"; cd bundled; cd ../workspace; printf ordinary > validator.js; mv "{ordinary}" "{tmp_path / "moved"}"'
        output = workspace / "validator.js"
    result = await tool.execute(command=command)
    assert result.success, result.error
    assert output.read_text() == "ordinary"
    assert target.read_text() == "original"
