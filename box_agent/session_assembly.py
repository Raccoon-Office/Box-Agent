"""Shared session capability preparation, invoked by configured plugins.

Factories create lightweight objects. These asynchronous preparation functions
run outside the Host activation lock, within the runtime initialization lease.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config
from .composition import (
    _attach_cleanup_error as attach_cleanup_error,
    _combined_cleanup_error as combine_cleanup_errors,
)
from .env_context import EnvContext, build_env_context_prompt
from .experts import ExpertSessionContext
from .tools.permissions import CapabilityPolicy
from .tools.runtime import (
    SkillRuntimeContext, build_skill_runtime_context, build_skill_runtime_prompt,
)
from .tools.setup import build_sandbox_info_prompt, build_file_delivery_prompt
from .project_context import (
    PROJECT_WORKSPACE_MODE_PROMPT, append_prompt_segment, compose_prompt_segments,
    build_project_startup_context_prompt,
)
from .session_prompts import (
    GENERAL_DIRECTORY_ORGANIZATION_PROMPT,
    build_action_hints_prompt, build_follow_up_suggestions_prompt,
    is_memory_scarce, is_playwright_unavailable,
    is_playwright_unavailable_from_env_context,
)
from .user_paths import state_path

if TYPE_CHECKING:
    from .plugins.builtins import SessionResources


def _prepared(resources: SessionResources) -> bool:
    host = resources.context.host
    return host.tools is not None and host.system_prompt is not None


async def _stop_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def prepare_model(resources: SessionResources) -> None:
    context = resources.context
    resources.llm_client = context.host.llm_client
    if resources.llm_client is None:
        from .agent_runtime import build_llm_client
        from .retry import RetryConfig
        from .schema import LLMProvider

        llm = context.config.llm
        retry = llm.retry
        resources.llm_client = build_llm_client(
            api_key=llm.api_key,
            provider=(LLMProvider.ANTHROPIC if llm.provider.lower() == "anthropic"
                      else LLMProvider.OPENAI),
            api_base=llm.api_base, model=llm.model,
            retry_config=RetryConfig(
                enabled=retry.enabled, max_retries=retry.max_retries,
                initial_delay=retry.initial_delay, max_delay=retry.max_delay,
                exponential_base=retry.exponential_base,
                retryable_exceptions=(Exception,),
            ) if retry.enabled else None,
            max_output_tokens=llm.max_output_tokens, auth_file=llm.auth_file,
            timeout=llm.timeout,
            reasoning_effort_when_disabled=llm.reasoning_effort_when_disabled,
        )
        resources.cleanup.push_async_callback(resources.llm_client.aclose)
    if context.host.summary_llm is not None:
        resources.state.setdefault("summary_llm", context.host.summary_llm)


async def prepare_memory(resources: SessionResources) -> None:
    context = resources.context
    config = context.config
    resources.memory_manager = context.host.memory_manager
    if _prepared(resources) or context.options.utility:
        return
    if (resources.memory_manager is None and config.agent.enable_memory
            and context.options.profile != "acp"):
        from .agent_runtime import build_memory_manager

        resources.memory_manager = build_memory_manager(
            memory_dir=config.agent.memory_dir,
            dedup_jaccard_threshold=config.agent.memory_dedup_jaccard,
        )
    if resources.memory_manager is not None and context.options.profile == "cli":
        try:
            await resources.memory_manager.import_openclaw(resources.llm_client)
        except Exception:
            pass
        if config.agent.memory_maintainer_enabled:
            from .memory_maintainer import MemoryMaintainer
            async def maintain():
                try:
                    await MemoryMaintainer(resources.memory_manager, config.agent, llm=resources.llm_client).run_if_due()
                except Exception:
                    pass
            task = asyncio.create_task(maintain(), name="memory-maintainer")
            resources.cleanup.push_async_callback(_stop_task, task)
    if resources.memory_manager is not None and config.agent.enable_memory_extraction:
        from .agent_runtime import build_memory_extractor

        resources.state.setdefault("memory_extractor", build_memory_extractor(
            llm=resources.llm_client, memory_manager=resources.memory_manager,
            cooldown=config.agent.memory_extraction_cooldown,
            step_interval=config.agent.memory_extraction_step_interval,
            session_id=resources.state.get("upstream_session_id"),
        ))


def _bind_skill_runtime(resources: SessionResources) -> None:
    from .agent_service import AgentService
    from .skill_runtime import SkillRuntime

    options = resources.context.options
    loader = None if options.utility else resources.skill_loader
    if loader is None and not options.utility:
        loader = AgentService.resolve_skill_loader(resources.tools)
    if loader is not None or options.profile == "acp" or options.resume_session_log:
        runtime = resources.state.setdefault("skill_runtime", SkillRuntime(
            loader, session_log=resources.context.host.session_log,
            allow_partial_restore=options.profile == "acp",
        ))
        if options.resume_session_log:
            session_log = resources.context.host.session_log
            if session_log is None:
                raise ValueError("resume_session_log requires a borrowed SessionLog")
            runtime.restore_records(session_log.replay().skills)
            session_log.prepare_resume()


async def prepare_tools(resources: SessionResources) -> None:
    context = resources.context
    host, options, config = context.host, context.options, context.config
    resources.skill_loader = host.skill_loader or resources.state.get("skill_loader")
    resources.mcp_task, resources.skill_task = host.mcp_task, host.skill_task
    if host.tools is not None:
        resources.tools = host.tools
        _bind_skill_runtime(resources)
        return
    expert = resources.state.get("expert_context")
    if expert is not None and resources.skill_loader is not None:
        resources.skill_loader = resources.skill_loader.with_expert_skill_sources(expert.skill_names())
    runtime_context = resources.state.get("skill_runtime_context") or build_skill_runtime_context(
        sandbox_mode=options.sandbox_mode,
        env_context=resources.state.get("env_context"),
        shell_python_path=options.shell_python_path,
    )
    resources.state.update(
        skill_runtime_context=runtime_context, skill_loader=resources.skill_loader,
    )
    if options.utility:
        resources.tools = []
        _bind_skill_runtime(resources)
        return
    from .tools.setup import add_workspace_tools, initialize_base_tools
    from .tools.permissions import CapabilityPolicy, GrantStore
    from .agent_runtime import build_permission_engine

    if host.base_tools is None:
        (resources.tools, resources.skill_loader, resources.mcp_task,
         resources.skill_task) = await (host.base_tools_factory or initialize_base_tools)(
            config, output=host.output,
            memory_manager=resources.memory_manager, llm=resources.llm_client,
            defer_skills=options.profile == "acp", mcp_start_gate=host.mcp_start_gate,
        )
        for task in (resources.mcp_task, resources.skill_task):
            if task is not None:
                resources.cleanup.push_async_callback(_stop_task, task)
    else:
        resources.tools = list(host.base_tools)

    grant_store = resources.state.get("grant_store")
    permission_engine = resources.state.get("permission_engine")
    allow_full_access = (
        config.tools.allow_full_access
        if options.allow_full_access is None else options.allow_full_access
    )
    if not options.non_interactive and grant_store is None:
        grant_store = GrantStore()
    if not allow_full_access and permission_engine is None and options.profile != "acp":
        grant_store = grant_store if grant_store is not None else GrantStore()
        policy = (CapabilityPolicy.from_config(config)
                  if getattr(config.officev3, "_present", False)
                  else CapabilityPolicy(session_workspace_root=str(context.workspace)))
        if not policy.session_workspace_root:
            policy = policy.model_copy(update={"session_workspace_root": str(context.workspace)})
        permission_engine = build_permission_engine(
            policy, context.workspace, grant_store=grant_store,
        )
    if resources.skill_loader is not None:
        from .tools.skill_catalog_tool import ListSkillsTool
        from .tools.skill_tool import GetSkillTool
        from .execution_profile import FAST_OPTIONAL_SKILLS

        resources.tools = [
            (ListSkillsTool if isinstance(tool, ListSkillsTool) else GetSkillTool)(
                resources.skill_loader,
                include_disabled=False,
                allowed_skill_names=getattr(tool, "allowed_skill_names", None),
                skill_access_filter=host.skill_access_filter,
                **({"skill_filter": host.skill_catalog_filter}
                   if isinstance(tool, ListSkillsTool) else {}),
                blocked_skill_names=(
                    frozenset(getattr(tool, "blocked_skill_names", ()))
                    | (FAST_OPTIONAL_SKILLS if resources.state.get("execution_profile") == "fast" else frozenset())
                ),
                explicitly_allowed_skill_names=resources.state.setdefault("explicitly_allowed_skill_names", set()),
            )
            if isinstance(tool, (GetSkillTool, ListSkillsTool)) else tool
            for tool in resources.tools
        ]
    scratch = (host.workspace_tools_factory or add_workspace_tools)(
        resources.tools, config, context.workspace,
        sandbox_mode=options.sandbox_mode,
        allow_full_access=allow_full_access,
        non_interactive=options.non_interactive, output=host.output,
        llm=resources.llm_client, permission_engine=permission_engine,
        skill_runtime_context=runtime_context, skill_loader=resources.skill_loader,
        skill_access_filter=host.skill_access_filter,
        env_context=resources.state.get("env_context"),
        session_mode=options.session_mode,
        capability_state_provider=host.capability_state_provider or (lambda: (
            "loading" if resources.mcp_task is not None and not resources.mcp_task.done()
            else "ready"
        )),
        **(dict(
            process_owner_id=options.process_owner_id,
            bypass_dangerous_command_approval=options.permission_mode == "full_access",
        ) if options.profile == "acp" else {}),
    )
    resources.tools.extend(host.extra_tools)
    resources.state.update(
        permission_engine=permission_engine, grant_store=grant_store,
        skill_runtime_context=runtime_context, skill_loader=resources.skill_loader,
        skill_scratch_dir=scratch,
    )
    _bind_skill_runtime(resources)
    if scratch is not None:
        from .tools.skill_scratch import cleanup_skill_scratch_dir

        resources.cleanup.callback(cleanup_skill_scratch_dir, scratch)


async def prepare_prompt(resources: SessionResources) -> None:
    context = resources.context
    host, options, config = context.host, context.options, context.config
    if _prepared(resources):
        resources.system_prompt = host.system_prompt
        return
    from .config import Config
    from .project_context import append_prompt_segment, compose_prompt_segments
    from .tools.setup import (build_file_delivery_prompt, build_image_generation_prompt,
                              build_sandbox_info_prompt, render_system_prompt_template)
    from .tools.runtime import build_skill_runtime_prompt

    if options.profile == "acp":
        prompt = build_acp_session_prompt(
            config, host.system_prompt or "You are a helpful AI assistant.",
            resources.memory_manager, options.session_mode,
            workspace=context.workspace, policy=options.effective_policy,
            env_context=resources.state.get("env_context"),
            skill_runtime_context=resources.state.get("skill_runtime_context"),
            expert_context=resources.state.get("expert_context"),
            workspace_layout=options.workspace_layout,
            enable_general_directory_policy=(
                not options.utility and options.session_mode in {None, "general"}
            ),
            follow_up_suggestions_enabled=options.follow_up_suggestions_enabled,
        )
        if resources.memory_manager is not None and not options.utility:
            memory = await asyncio.to_thread(resources.memory_manager.recall)
            resources.state["memory_block"] = memory or None
            if memory:
                prompt = append_prompt_segment(prompt, memory)
        if not options.utility:
            prompt = f"{prompt.rstrip()}\n\n{build_image_generation_prompt(config)}"
        if host.prompt_suffix:
            prompt = f"{prompt.rstrip()}\n\n{host.prompt_suffix.strip()}"
        resources.system_prompt = prompt
        return

    def status(message: str, *, success: bool = True) -> None:
        if options.profile == "cli" and host.output is not None:
            from .tools.setup import Colors
            color = Colors.GREEN if success else Colors.YELLOW
            host.output(f"{color}{message}{Colors.RESET}")

    prompt = host.system_prompt
    if prompt is None:
        path = Config.find_config_file(config.agent.system_prompt_path)
        prompt = (render_system_prompt_template(path.read_text(encoding="utf-8"))
                  if path and path.exists() else
                  "You are Box-Agent, an intelligent assistant that can help users complete various tasks.")
        if path and path.exists():
            status(f"✅ Loaded system prompt (from: {path})")
        else:
            status("⚠️  System prompt not found, using default", success=False)
    if resources.skill_loader is not None:
        from .tools.skill_loader import SKILL_SLOT_SENTINEL

        prompt = prompt.replace("{SKILLS_METADATA}", SKILL_SLOT_SENTINEL)
    else:
        prompt = prompt.replace("{SKILLS_METADATA}", "")
    prompt = compose_prompt_segments(prompt, replacements={
        "{SANDBOX_INFO}": build_sandbox_info_prompt() if options.sandbox_mode else "",
        "{FILE_DELIVERY_INFO}": build_file_delivery_prompt(),
    })
    if options.profile == "cli" and options.session_mode == "code_agent":
        prompt = compose_prompt_segments(prompt, segments=(
            PROJECT_WORKSPACE_MODE_PROMPT,
            build_project_startup_context_prompt(context.workspace),
        ))
        code_path = Config.find_config_file(config.agent.code_prompt_path)
        if code_path and code_path.exists():
            code_prompt = code_path.read_text(encoding="utf-8").strip()
            prompt = append_prompt_segment(prompt, code_prompt)
            if code_prompt:
                status(f"✅ Loaded code workspace prompt (from: {code_path})")
        else:
            status("⚠️  Code workspace prompt not found", success=False)
    if not options.utility:
        prompt = append_prompt_segment(prompt, build_image_generation_prompt(config))
    runtime_context = resources.state.get("skill_runtime_context")
    if runtime_context is not None:
        prompt = append_prompt_segment(prompt, build_skill_runtime_prompt(runtime_context))
    if options.profile == "cli":
        prompt = append_prompt_segment(prompt, build_env_context_prompt(resources.state.get("env_context")))
    if resources.memory_manager is not None and not options.utility:
        memory = await asyncio.to_thread(resources.memory_manager.recall)
        resources.state["memory_block"] = memory
        if memory:
            prompt = append_prompt_segment(prompt, memory)
    resources.system_prompt = prompt


async def prepare_hooks(resources: SessionResources) -> None:
    host = resources.context.host
    resources.hooks = host.hooks
    if host.hooks is None and not _prepared(resources) and resources.context.options.profile != "acp":
        from .hooks import load_hooks

        configured = resources.context.config.hooks.hooks
        resources.hooks = load_hooks(configured) if configured else None


async def finish_session(session: Any, resources: SessionResources) -> None:
    """Restore and bind shared Skills only after the Agent owns its prompt."""
    if _prepared(resources):
        return
    agent = session.agent
    session_skill_loader = resources.skill_loader
    expert_context = resources.state.get("expert_context")
    grants = resources.state.get("connector_skill_grants")
    if grants is not None and session_skill_loader is not None:
        # Agent has already validated and restored the session's read records.
        # Derive the compatibility grant set without restoring a second time.
        for name in agent.skill_runtime.active_names:
            skill = session_skill_loader.get_skill(name)
            if skill is not None and getattr(skill, "source", None) == "connector":
                grants.add(skill.name)
    if session_skill_loader:
        from box_agent.tools.skill_loader import SkillSelector, move_skill_slot_to_end

        relocated_prompt = move_skill_slot_to_end(agent.messages[0].content)
        if relocated_prompt != agent.messages[0].content:
            agent.set_system_prompt(relocated_prompt)
        selector = SkillSelector(
            session_skill_loader,
            include_disabled=False,
            skill_filter=resources.context.host.skill_catalog_filter,
        )
        selector.bind(agent.messages[0].content)
        if expert_context:
            expert_skill_prompt = selector.update(expert_context.skill_query())
            if expert_skill_prompt is not None:
                agent.set_system_prompt(expert_skill_prompt)
        session.skill_selector = selector


def _workspace_layout_path(
    layout: Any,
    workspace: Path,
    *keys: str,
) -> Path | None:
    if not isinstance(layout, dict):
        return None
    raw = None
    for key in keys:
        value = layout.get(key)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            break
    if raw is None:
        return None
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace / path
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _workspace_layout_prompt(
    *,
    workspace: Path,
    layout: Any,
) -> str:
    """Describe the selected root without reviving deprecated output roots."""
    selected_root = _workspace_layout_path(
        layout,
        workspace,
        "selected_root_dir",
        "selectedRootDir",
    ) or workspace
    lines = [
        "## Workspace Layout",
        f"- 工作区（selected workspace root）：`{selected_root}`",
        (
            f"- 当前会话工作目录（cwd）：`{workspace}`。工具相对路径和 artifact 扫描都从"
            "该目录开始；会话生命周期内不得改变它。"
        ),
        (
            "- 模型为整理产物而创建的子目录只是普通文件组织，不成为新的 workspace，"
            "也不改变 cwd。"
        ),
        (
            "- 判空规则：必须先使用目标目录的绝对路径实际查询其内容，"
            "只有查询成功且确认无内容时，才可判断该目标目录为空。"
            "查询失败、权限不足或结果被过滤、截断时，不得据此判空。"
        ),
    ]
    return "\n".join(lines)


def _filesystem_access_prompt(workspace: Path, policy: CapabilityPolicy | None) -> str:
    """Build per-session filesystem guidance for the model.

    Tools still enforce permissions. This prompt only prevents the model
    from assuming workspace-only access when officev3 has granted extra
    roots such as ~/Documents.
    """
    if policy is None:
        return (
            "## File Access Context\n"
            f"- Current workspace: `{workspace}`\n"
            "- File tools and bash may access paths allowed by the active runtime policy.\n"
            "- If a file is outside the allowed scope, the tool will return a permission error; "
            "try the tool instead of assuming denial."
        )

    allowed_roots = [workspace]
    if policy.session_workspace_root:
        allowed_roots.append(Path(policy.session_workspace_root).expanduser())
    for directory in policy.allowed_directories:
        allowed_roots.append(Path(directory).expanduser())

    seen: set[str] = set()
    root_lines: list[str] = []
    for root in allowed_roots:
        root_s = str(root)
        if root_s not in seen:
            seen.add(root_s)
            root_lines.append(f"- `{root_s}`")

    if policy.filesystem_scope == "user_home":
        scope_line = "- Active filesystem scope: `user_home`; paths under the user home directory are allowed."
    elif policy.filesystem_scope in ("session_workspace", "custom"):
        scope_line = (
            f"- Active filesystem scope: `{policy.filesystem_scope}`; the workspace, "
            "session workspace root, and configured allowed directories are allowed."
        )
    else:
        scope_line = f"- Active filesystem scope: `{policy.filesystem_scope}`; unknown scopes fail closed in tools."

    return (
        "## File Access Context\n"
        f"{scope_line}\n"
        "- Allowed filesystem roots for this session include:\n"
        + "\n".join(root_lines)
        + "\n- These are currently pre-authorized roots, not the complete set of paths that may be requested."
        + "\n- When the task requires it, you may try a specific, narrow path outside these roots; "
        "the runtime will request permission when appropriate."
        + "\n- A permission denial applies only to the requested path. "
        "Do not generalize it to other specific candidate paths."
        + "\n- Prefer absolute paths when the user names a location such as ~/Documents."
        + "\n- Do not claim you can only access the workspace based only on the listed "
        "roots or a denial for another path."
    )


def _build_action_hints_prompt(config, memory, env_context: EnvContext | None = None) -> str:
    """Detect onboarding / browser-tools scenarios and build the hint contract."""
    memory_scarce = is_memory_scarce(memory.read_core() if memory else None)

    try:
        _host_mcp = os.environ.get("BOX_AGENT_MCP_CONFIG_PATH", "").strip()
        _user_mcp = (
            state_path("config/mcp.json", Path(_host_mcp).expanduser())
            if _host_mcp
            else state_path("config/mcp.json")
        )
        mcp_path = _user_mcp if _user_mcp.exists() else Config.find_config_file(config.tools.mcp_config_path)
    except Exception:
        mcp_path = None
    playwright_unavailable = is_playwright_unavailable(
        mcp_path,
        mcp_globally_enabled=config.tools.enable_mcp,
    ) or is_playwright_unavailable_from_env_context(env_context)

    return build_action_hints_prompt(
        memory_scarce=memory_scarce,
        playwright_unavailable=playwright_unavailable,
    )


def build_acp_session_prompt(
    config, system_prompt, memory,
    session_mode: str | None,
    workspace: Path | None = None,
    policy: CapabilityPolicy | None = None,
    env_context: EnvContext | None = None,
    skill_runtime_context: SkillRuntimeContext | None = None,
    expert_context: ExpertSessionContext | None = None,
    workspace_layout: Any = None,
    enable_general_directory_policy: bool = False,
    follow_up_suggestions_enabled: bool = False,
) -> str:
    """Build system prompt with conditional mode-specific injection."""
    _MODE_PROMPT_MAP = {
        "data_analysis": "analysis_prompt_path",
        "code_agent": "code_prompt_path",
    }

    base_prompt = compose_prompt_segments(
        system_prompt,
        replacements={
            "{SANDBOX_INFO}": build_sandbox_info_prompt(),
            "{FILE_DELIVERY_INFO}": build_file_delivery_prompt(),
        },
        segments=(
            PROJECT_WORKSPACE_MODE_PROMPT
            if session_mode == "code_agent"
            else None,
        ),
    )
    if workspace is not None:
        base_prompt = append_prompt_segment(
            base_prompt,
            _filesystem_access_prompt(workspace, policy),
        )
        layout_prompt = _workspace_layout_prompt(
            workspace=workspace,
            layout=workspace_layout,
        )
        base_prompt = append_prompt_segment(base_prompt, layout_prompt)

    if enable_general_directory_policy:
        base_prompt = append_prompt_segment(
            base_prompt, GENERAL_DIRECTORY_ORGANIZATION_PROMPT,
        )

    if session_mode == "code_agent" and workspace is not None:
        base_prompt = append_prompt_segment(
            base_prompt,
            build_project_startup_context_prompt(workspace),
        )

    env_prompt = build_env_context_prompt(env_context)
    if env_prompt:
        base_prompt = append_prompt_segment(base_prompt, env_prompt)

    runtime_context = skill_runtime_context or build_skill_runtime_context(
        sandbox_mode=True,
        env_context=env_context,
    )
    base_prompt = append_prompt_segment(
        base_prompt,
        build_skill_runtime_prompt(runtime_context),
    )

    hints_prompt = _build_action_hints_prompt(config, memory, env_context)
    if hints_prompt:
        base_prompt = append_prompt_segment(base_prompt, hints_prompt)

    if follow_up_suggestions_enabled:
        base_prompt = append_prompt_segment(
            base_prompt,
            build_follow_up_suggestions_prompt(),
        )

    attr = _MODE_PROMPT_MAP.get(session_mode or "")
    if attr:
        prompt_filename = getattr(config.agent, attr, None)
        if prompt_filename:
            mode_path = Config.find_config_file(prompt_filename)
            if mode_path and mode_path.exists():
                mode_prompt = mode_path.read_text(encoding="utf-8").strip()
                base_prompt = append_prompt_segment(
                    base_prompt,
                    mode_prompt,
                    skip_empty=False,
                )
            else:
                pass  # Missing optional mode prompt.

    if expert_context:
        expert_prompt = expert_context.render_prompt()
        if expert_prompt:
            base_prompt = append_prompt_segment(base_prompt, expert_prompt)
    return base_prompt


def create_application_runtime():
    """Create the application scope without exposing plugin internals to adapters."""
    from .plugins.runtime import PluginRuntime
    return PluginRuntime()


def build_session_permission_policy(config, workspace, permission_mode, filesystem_policy):
    """Compose normalized host filesystem context with configured capabilities."""
    policy = CapabilityPolicy.from_config(config) if getattr(config.officev3, "_present", False) else CapabilityPolicy()
    if permission_mode == "default":
        policy = policy.with_filesystem_overrides(
            session_workspace_root=str(workspace), allowed_directories=[],
            filesystem_scope="session_workspace", replace_allowed_directories=True,
        )
    if filesystem_policy is not None:
        policy = policy.with_filesystem_overrides(
            **filesystem_policy, replace_allowed_directories=permission_mode == "default",
        )
    return policy


async def close_owned_clients(clients) -> None:
    """Release each process-owned model transport once, even if another fails."""
    import sys

    primary_error = sys.exc_info()[1]
    errors = []
    unique = {id(client): client for client in clients if client is not None}
    for client in reversed(list(unique.values())):
        close = getattr(client, "aclose", None)
        if close is not None:
            try:
                await close()
            except BaseException as error:
                errors.append(error)
    if errors:
        cleanup_error = combine_cleanup_errors(errors)
        if primary_error is not None:
            attach_cleanup_error(primary_error, cleanup_error)
        else:
            raise cleanup_error
