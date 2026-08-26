"""Tool initialization helpers shared by CLI and ACP entry-points.

Extracted from ``cli.py`` so that ``box_agent.acp`` can assemble the
tool belt without pulling in ``prompt_toolkit`` and the rest of the
interactive-CLI surface.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Mapping, Optional

from box_agent.config import Config, ToolLimitsConfig
from box_agent.llm.capabilities import image_input_support, model_candidate_has_tag
from box_agent.llm.model_routing import resolve_model_client
from box_agent.tools.base import Tool
from box_agent.tools.argument_limits import RECOMMENDED_GENERATED_BODY_CHARS
from box_agent.tools.bash_tool import BashKillTool, BashOutputTool, BashTool
from box_agent.tools.execution_result_tool import ReportExecutionResultTool
from box_agent.tools.file import JsonlQueryTool, ReadTool
from box_agent.tools.file_tools import (
    AppendTool,
    EditTool,
    SearchFilesTool,
    WriteTool,
)
from box_agent.tools.image_generation_tool import (
    GenerateImageTool,
    resolve_image_generation_endpoint,
)
from box_agent.tools.jupyter_tool import (
    JupyterSandboxTool,
    MAX_EXECUTE_CODE_CHARS,
    SandboxEnvironment,
    SandboxStatusTool,
)
from box_agent.tools.mcp_loader import load_mcp_tools_async, set_mcp_timeout_config
from box_agent.tools.mcp_bootstrap import bootstrap_managed_mcp_config
from box_agent.tools.mcp_tool_catalog import get_mcp_tool_catalog
from box_agent.tools.memory_tool import MemoryReadTool, MemorySearchTool, MemoryWriteTool
from box_agent.tools.obsidian_tool import create_obsidian_tools
from box_agent.tools.plan_tool import PlanReadTool, PlanStore, PlanWriteTool
from box_agent.tools.request_user_decision_tool import RequestUserDecisionTool
from box_agent.tools.request_user_input_tool import RequestUserInputTool
from box_agent.tools.runtime import SkillRuntimeContext, build_skill_runtime_context
from box_agent.tools.skill_execution_env import build_skill_execution_env
from box_agent.tools.mcp_config_tool import McpConfigTool
from box_agent.tools.schedule_tool import CreateScheduledTaskTool
from box_agent.tools.skill_tool import create_skill_tools
from box_agent.tools.sub_agent_tool import SubAgentTool
from box_agent.tools.todo_tool import TodoReadTool, TodoStore, TodoWriteTool
from box_agent.tools.image_inspection_tool import ImageInspectionTool

if TYPE_CHECKING:
    from box_agent.tools.permissions import PermissionEngine


def _image_capable_llm(llm: Any | None) -> Any | None:
    """Return an image-capable client, or None for known text-only bindings."""
    if llm is None:
        return None

    current_support = image_input_support(llm)
    if current_support is True:
        return llm

    model = str(getattr(llm, "model", "") or "").strip()
    candidates = tuple(getattr(llm, "auto_model_candidates", ()) or ())
    if candidates:
        vision_candidates = tuple(
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and model_candidate_has_tag(candidate, "vision")
            and not (
                current_support is False
                and candidate.get("model") == model
            )
        )
        if not vision_candidates:
            return None
        resolved, diagnostic = resolve_model_client(
            llm,
            task="分析图片内容并执行视觉质量审查",
            strategy="utility",
            auto_model_candidates=vision_candidates,
            task_tags=("vision", "analysis"),
            required_ability_level=2,
        )
        return resolved if diagnostic.get("mode") == "auto" else None

    return None if current_support is False else llm


def build_sandbox_info_prompt(use_output_dir: bool = True) -> str:
    """Build the sandbox prompt block for output or project workspace mode."""
    if use_output_dir:
        location_line = (
            "沙箱有独立 `sys.executable`，cwd 已是 `{workspace}/output/`"
            "（或 host 指定的当前会话 output 根），"
            "存盘用相对路径（如 `plt.savefig(\"chart.png\")`）；禁写 `/mnt/data/`、"
            "`sandbox:` 前缀；读用户上传文件用 `../<name>` 回 workspace 根。"
        )
    else:
        location_line = (
            "沙箱有独立 `sys.executable`，cwd 已是当前工作区/代码项目根目录，"
            "存盘用项目内相对路径；不要默认创建或使用 `output/` 目录；"
            "禁写 `/mnt/data/`、`sandbox:` 前缀。"
        )

    return f"""
## Python Sandbox (execute_code)

Python 代码通过 `execute_code` 在**隔离 Jupyter kernel** 中运行，和 host Python 独立：

- **运行位置**：{location_line}
- **状态持久**：同会话内变量、import、已加载数据保留到下一次调用；保持分步执行避免错误。
- **预装包**：pandas、numpy、matplotlib、seaborn、scikit-learn、openpyxl、xlrd、python-docx、pypdf、pdfplumber、reportlab、python-pptx、beautifulsoup4、lxml、pillow、requests、pyyaml、python-dateutil、chardet + 标准库——**不要重装**，会拖慢首次执行。
- **装新包**：仅在确认缺失时，在 `execute_code` 内用 `%pip install <pkg>` / `!pip install <pkg>`（走当前 kernel 的 pip，落沙箱 venv）。**绝对禁止** `bash` 跑 `pip install`——会装到 host，沙箱仍 `ModuleNotFoundError`。
- **用 execute_code**：数据分析、可视化、CSV/Excel/JSON/图片读写、Word/PDF/PPT 处理、多步计算、需保留状态的脚本。
- **单次代码大小**：每次 `execute_code(code=...)` 控制在 {MAX_EXECUTE_CODE_CHARS} 字符以内；大脚本/模板/数据要预先分段执行，最后再读取校验；不要等到 `EXECUTE_CODE_TOO_LARGE` 后才拆，也不要把大段内容塞进一个工具参数。生成静态内容（共享样式/HTML/CSS/JS/JSON manifest/base64/生成文件正文）时，除非必须用 Python 处理，否则不要把正文塞进 `execute_code`。
- **大文件落盘**：小文件用 `write_file(path, content)` 一次写完；若预计单次模型输出容纳不下，就对同一路径按顺序调用 `write_file`，首块用 `chunk_index=0, final=false`，后续逐次递增索引，最后一块用 `final=true`。每个生成块建议不超过 {RECOMMENDED_GENERATED_BODY_CHARS:,} 字符。禁止把文件正文、heredoc 或 base64 载荷塞进 `bash`；最后用 `read_file` 或渲染检查校验。
- **必须执行**：用户要求“用/使用/运行 Python”得到一个具体结果（如生成随机数、计算数值、处理数据/文件、运行脚本）时，必须调用 `execute_code` 返回真实执行结果；不要只给代码示例。只有用户明确问“怎么写/示例代码/解释代码”时才只返回代码。
- **用 bash**：仓库代码编辑、测试/构建、系统命令、git——与沙箱无关。

### 文档处理优先级

Excel/Word/PDF/PowerPoint 优先在沙箱内用 Python 包，避免外部 CLI：

- **Excel**：`pandas`+`openpyxl` 读写，`xlrd` 读 `.xls`；仅公式重算才考虑 LibreOffice。
- **Word**：`python-docx` 读写；跨格式转换才用 `pandoc`。
- **PDF**：`pypdf`（合并/拆分）、`pdfplumber`（文本/表格抽取）、`reportlab`（生成）。
- **PowerPoint**：`python-pptx` 用于读取/抽取/检查/窄范围编辑；新建 PPT/PPTX 必须走 skill，不要直接用 `execute_code`+`python-pptx` 创建交付 PPT。

**Skill vs Sandbox**：数据抽取/格式转换/表格处理 → 沙箱；复杂版式/OOXML 精操作/模板化生成/公式重算 → 先加载 skill。
"""


def build_file_delivery_prompt(use_output_dir: bool = True) -> str:
    """Build file-delivery guidance for output or project workspace mode."""
    preview_directory = '"$BOX_AGENT_OUTPUT_DIR"' if use_output_dir else '"$PWD"'
    preview_guidance = (
        "\n- **本地 HTML 预览**：Playwright MCP 不要打开 `file://`。用 bash 后台启动仅监听 "
        "loopback 的动态端口预览：`${BOX_AGENT_PYTHON:-python3} -u -m http.server 0 "
        f"--bind 127.0.0.1 --directory {preview_directory}`，从 `bash_output` 读取实际端口后访问 "
        "`http://127.0.0.1:<port>/...`；验证后立即 `bash_kill`。任务结束时 runtime 会兜底回收仍在运行的后台进程。"
    )
    if use_output_dir:
        return (
            "- **目录**：交付物落当前会话的 output 根目录；以沙箱 cwd 和 host 提供的工作区信息为准，"
            "不要写到 `~/.box-agent/` 等内部目录。\n"
            "- **相对路径**：bash、文件工具、`generate_image` 和视觉检查的相对路径都已从当前 output 根开始；"
            "使用 `assets/generated/a.png`，不要再添加 `output/` 前缀。读取会话根的上传文件时使用 `../<name>`。\n"
            "- **桌面交付**：完成后说明文件名即可。宿主会从结构化 ArtifactEvent 渲染可打开的文件卡。"
            + preview_guidance
        )
    return (
        "- **目录**：这是现有项目工作区。交付物可以在项目树中合适的位置；不要默认创建或使用 `output/`。\n"
        "- **桌面交付**：完成后说明文件名和项目内相对位置即可。宿主会根据文件变更渲染可验证的文件入口。"
        + preview_guidance
    )


def image_generation_service_configured(
    config: Config,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return the standard Box-Agent image service state for CLI and ACP."""
    image_config = getattr(config, "image_generation", None)
    configured_endpoint = getattr(image_config, "endpoint", "") or None
    return bool(resolve_image_generation_endpoint(configured_endpoint, env))


def build_image_generation_prompt(
    config: Config,
    env: Mapping[str, str] | None = None,
) -> str:
    """Build the image-generation policy shared by CLI and ACP sessions."""
    configured = image_generation_service_configured(config, env)
    status = (
        "已配置，可以调用 `generate_image`。"
        if configured
        else "未配置；调用失败时必须如实报告阻塞，不得假装已生成图片。"
    )
    return (
        "## Native Image Generation\n\n"
        "- `generate_image` 是 Box-Agent 的标准工具，CLI 与 ACP 共用；"
        "是否可用只由 Box-Agent 自身的 `image_generation.endpoint` 或对应环境变量决定，"
        "不由宿主 `env_context` 控制。\n"
        f"- 当前生图服务：{status}\n"
        "- 用户明确要求生图、生成新图片、插画、海报或位图信息图，且没有要求可编辑 HTML 时，"
        "优先调用 `generate_image`。\n"
        "- 用户明确禁止 HTML/CSS/SVG、PIL 或截图回退时，`generate_image` 失败后必须如实报告阻塞，"
        "不得擅自改用这些路径。"
    )


# Single source of truth for the default sandbox / Python-execution block
# injected into the system prompt. ACP may build a per-session variant when a
# host marks the session as an existing project workspace.
SANDBOX_INFO_PROMPT = build_sandbox_info_prompt(use_output_dir=True)


# Minimal color constants used in status messages.
# The full ``Colors`` class lives in ``cli.py``; we only need a small subset.
class Colors:
    """Terminal color subset for tool-setup status messages."""

    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BRIGHT_CYAN = "\033[96m"
    DIM = "\033[2m"
    RESET = "\033[0m"


async def initialize_base_tools(
    config: Config,
    output=None,
    memory_manager=None,
    llm=None,
    defer_skills: bool = False,
    mcp_start_gate: asyncio.Event | None = None,
):
    """Initialize base tools (independent of workspace)

    These tools are loaded from package configuration and don't depend on workspace.
    Note: File tools are now workspace-dependent and initialized in add_workspace_tools()

    Args:
        config: Configuration object
        output: Callable for status messages (default: print). Pass a stderr
                writer when stdout must stay clean (e.g. ACP mode).
        memory_manager: Optional MemoryManager instance for memory tools.
        llm: Optional LLM client used to model-merge context memory writes.
        defer_skills: If True (ACP path), skill discovery is moved off the
            critical path and returned as an asyncio.Task in ``skill_task``.
            The returned ``skill_loader`` is still valid immediately — its
            ``loaded_skills`` dict just stays empty until the task completes.
            Callers must ``await skill_task`` (or its completion) before
            they need the skill catalog. CLI keeps the default (False) so
            users still see the "Loading Claude Skills..." status inline.
        mcp_start_gate: Optional gate used by ACP to keep MCP subprocess and
            network startup behind protocol readiness. CLI leaves this unset,
            so its existing eager background loading behavior is unchanged.

    Returns:
        Tuple of (tools, skill_loader, mcp_task, skill_task). The MCP task
        loads in the background — call ``await_mcp_tools(mcp_task)`` before
        running an agent turn to ensure MCP tools are available. ``mcp_task``
        is ``None`` when MCP is disabled. ``skill_task`` is a discovery task
        when ``defer_skills=True``, otherwise ``None`` (discovery already ran
        inline). See :func:`await_skill_discovery`.
    """
    _out = output or print

    tools = []
    skill_loader = None
    skill_task: Optional[asyncio.Task] = None

    # 0. Memory tools (cross-session, workspace-independent)
    if memory_manager is not None:
        tools.append(MemoryReadTool(memory_manager))
        tools.append(MemoryWriteTool(memory_manager, llm=llm))
        tools.append(MemorySearchTool(memory_manager))
        _out(f"{Colors.GREEN}✅ Loaded memory tools (memory_read, memory_write, memory_search){Colors.RESET}")

    # 1. Bash auxiliary tools (output monitoring and kill)
    # Note: BashTool itself is created in add_workspace_tools() with workspace_dir as cwd
    if config.tools.enable_bash:
        bash_output_tool = BashOutputTool()
        tools.append(bash_output_tool)
        _out(f"{Colors.GREEN}✅ Loaded Bash Output tool{Colors.RESET}")

        bash_kill_tool = BashKillTool()
        tools.append(bash_kill_tool)
        _out(f"{Colors.GREEN}✅ Loaded Bash Kill tool{Colors.RESET}")

    # 1b. Scheduled-task draft tool (workspace-independent).
    # Pops a pre-filled "create scheduled task" window on the desktop host via
    # ToolResult.raw_output → tool_call_update.rawOutput. Does not persist anything
    # itself; the renderer owns the actual save.
    tools.append(CreateScheduledTaskTool())
    _out(f"{Colors.GREEN}✅ Loaded Scheduled Task tool (create_scheduled_task){Colors.RESET}")

    tools.append(McpConfigTool())
    _out(f"{Colors.GREEN}✅ Loaded MCP Config tool (mcp_config){Colors.RESET}")

    # 2. Claude Skills (loaded from package directory)
    if config.tools.enable_skills:
        _out(f"{Colors.BRIGHT_CYAN}Loading Claude Skills...{Colors.RESET}")
        try:
            # Resolve builtin skills directory with priority search
            skills_path = Path(config.tools.skills_dir).expanduser()
            if skills_path.is_absolute():
                builtin_dir = skills_path
            else:
                # Search in priority order:
                # 1. Current directory (dev mode: ./skills or ./box_agent/skills)
                # 2. Package directory (installed: site-packages/box_agent/skills)
                search_paths = [
                    skills_path,  # ./skills for backward compatibility
                    Path("box_agent") / skills_path,  # ./box_agent/skills
                    Config.get_package_dir() / skills_path,  # site-packages/box_agent/skills
                ]

                builtin_dir = skills_path  # default
                for path in search_paths:
                    if path.exists():
                        builtin_dir = path.resolve()
                        break

            # User skills directory: ~/.box-agent/skills/
            # Auto-created so officev3 can drop new skills in and we pick them up on mtime change.
            user_skills_dir = Path.home() / ".box-agent" / "skills"
            user_skills_dir.mkdir(parents=True, exist_ok=True)

            # User skills take priority over builtin on name conflict
            sources = [
                (user_skills_dir, "user"),
                (builtin_dir, "managed" if config.tools.skills_manifest_required else "builtin"),
            ]
            if config.tools.bootstrap_skills_dir:
                bootstrap_dir = Path(config.tools.bootstrap_skills_dir).expanduser()
                if bootstrap_dir.exists() and bootstrap_dir.resolve() != builtin_dir.resolve():
                    sources.append((bootstrap_dir.resolve(), "builtin"))

            # ACP path: defer discovery to a background task so a directory
            # full of malformed SKILL.md files can't block stdio setup and
            # trip the host's `initialize` timeout. The loader object is
            # returned right away; its ``loaded_skills`` dict fills in when
            # the task completes. `SkillSelector` runs on the first user
            # turn — well after the discovery task has finished on any real
            # skill catalog.
            if defer_skills:
                skill_tools, skill_loader = create_skill_tools(
                    sources=sources, defer_discovery=True
                )
                if skill_tools:
                    tools.extend(skill_tools)

                async def _discover() -> int:
                    # Offload the sync rglob + YAML parse to a thread so a
                    # slow disk (or a directory with many broken skills)
                    # never blocks the event loop.
                    try:
                        skills = await asyncio.to_thread(skill_loader.discover_skills)
                    except Exception as exc:  # pragma: no cover — defensive
                        _out(
                            f"{Colors.YELLOW}⚠️  Failed to discover Skills: {exc}{Colors.RESET}"
                        )
                        return 0
                    _out(
                        f"{Colors.GREEN}✅ Loaded Skill tool (get_skill) — "
                        f"user: {user_skills_dir}, builtin: {builtin_dir} "
                        f"({len(skills)} skills){Colors.RESET}"
                    )
                    return len(skills)

                skill_task = asyncio.create_task(_discover(), name="skills-background-load")
            else:
                skill_tools, skill_loader = create_skill_tools(sources=sources)
                if skill_tools:
                    tools.extend(skill_tools)
                    _out(
                        f"{Colors.GREEN}✅ Loaded Skill tool (get_skill) — "
                        f"user: {user_skills_dir}, builtin: {builtin_dir}{Colors.RESET}"
                    )
                else:
                    _out(f"{Colors.YELLOW}⚠️  No available Skills found{Colors.RESET}")
        except Exception as e:
            _out(f"{Colors.YELLOW}⚠️  Failed to load Skills: {e}{Colors.RESET}")

    # 3. MCP tools (loaded with priority search, in background to avoid blocking startup)
    mcp_task: Optional[asyncio.Task] = None
    if config.tools.enable_mcp:
        mcp_config = config.tools.mcp
        set_mcp_timeout_config(
            connect_timeout=mcp_config.connect_timeout,
            execute_timeout=mcp_config.execute_timeout,
            sse_read_timeout=mcp_config.sse_read_timeout,
        )
        # Keep CLI and ACP on the same user-owned configuration. Reconcile the
        # hosted search endpoint and any MCP servers advertised by the frozen
        # runtime before background discovery starts.
        configured_mcp = Path(config.tools.mcp_config_path).expanduser()
        bootstrap_target = (
            configured_mcp
            if configured_mcp.is_absolute()
            else Path.home() / ".box-agent" / "config" / "mcp.json"
        )
        bootstrap = bootstrap_managed_mcp_config(bootstrap_target)
        if bootstrap.warning:
            _out(f"{Colors.YELLOW}⚠️  {bootstrap.warning}{Colors.RESET}")
        mcp_config_path = (
            bootstrap.path
            if bootstrap.path.exists()
            else Config.find_config_file(config.tools.mcp_config_path)
        )
        if mcp_config_path:
            get_mcp_tool_catalog().mark_loading()
            _out(f"{Colors.BRIGHT_CYAN}Loading MCP tools in background (from: {mcp_config_path})...{Colors.RESET}")
            _out(
                f"{Colors.DIM}  MCP timeouts: connect={mcp_config.connect_timeout}s, "
                f"execute={mcp_config.execute_timeout}s, sse_read={mcp_config.sse_read_timeout}s{Colors.RESET}"
            )

            async def _load() -> List[Tool]:
                try:
                    if mcp_start_gate is not None:
                        await mcp_start_gate.wait()
                    loaded = await load_mcp_tools_async(
                        str(mcp_config_path),
                        auth_file=config.llm.auth_file,
                    )
                    if loaded:
                        _out(f"{Colors.GREEN}✅ Loaded {len(loaded)} MCP tools (from: {mcp_config_path}){Colors.RESET}")
                    else:
                        _out(f"{Colors.YELLOW}⚠️  No available MCP tools found{Colors.RESET}")
                    return loaded
                except Exception as e:
                    _out(f"{Colors.YELLOW}⚠️  Failed to load MCP tools: {e}{Colors.RESET}")
                    return []

            mcp_task = asyncio.create_task(_load(), name="mcp-background-load")
        else:
            _out(f"{Colors.YELLOW}⚠️  MCP config file not found: {config.tools.mcp_config_path}{Colors.RESET}")

    _out("")  # Empty line separator
    return tools, skill_loader, mcp_task, skill_task


async def await_skill_discovery(skill_task: Optional[asyncio.Task]) -> None:
    """Await the background skill-discovery task, if any.

    ACP callers invoke this before they need a populated skill catalog
    (e.g. before the first turn's SkillSelector filter). Safe to call
    multiple times — asyncio.Task caches its result — and safe to call
    with ``None`` when discovery ran inline.
    """
    if skill_task is None:
        return
    try:
        await skill_task
    except Exception:
        # Discovery already logs its own failure; swallow so the caller
        # can proceed with whatever partial catalog the loader has.
        return


async def await_mcp_tools(mcp_task: Optional[asyncio.Task]) -> List[Tool]:
    """Await the background MCP loading task (no-op if already awaited or absent).

    Safe to call multiple times — asyncio.Task results are cached.
    Returns the list of loaded MCP tools, or [] if none/failed.
    """
    if mcp_task is None:
        return []
    try:
        return await mcp_task
    except Exception:
        return []


def register_mcp_tools(tool_map: dict[str, Tool], mcp_tools: list[Tool]) -> None:
    """Register MCP tools, allowing them to override same-named fallback tools."""
    for tool in mcp_tools:
        existing = tool_map.get(tool.name)
        if getattr(existing, "reserved_deferred_mcp_search", False):
            # A remote MCP tool cannot replace the session-bound discovery
            # control entry after background loading completes.
            continue
        tool_map[tool.name] = tool


def sync_mcp_tools(
    tool_map: dict[str, Tool],
    mcp_tools: list[Tool],
    fallback_tools: dict[str, Tool],
) -> None:
    """Rebuild MCP registrations while preserving overwritten stable tools."""
    for name, tool in list(tool_map.items()):
        if getattr(tool, "mcp_tool_id", None) is None:
            continue
        fallback = fallback_tools.get(name)
        if fallback is None:
            tool_map.pop(name, None)
        else:
            tool_map[name] = fallback

    fallback_tools.clear()
    for tool in mcp_tools:
        existing = tool_map.get(tool.name)
        if getattr(existing, "reserved_deferred_mcp_search", False):
            continue
        if existing is not None and getattr(existing, "mcp_tool_id", None) is None:
            fallback_tools.setdefault(tool.name, existing)
        tool_map[tool.name] = tool


def merge_mcp_tools(base_tools: list[Tool], mcp_tools: list[Tool]) -> None:
    """Merge MCP tools into a tool list, replacing same-named fallback tools."""
    mcp_by_name = {tool.name: tool for tool in mcp_tools}
    if not mcp_by_name:
        return

    replaced_names: set[str] = set()
    for index, tool in enumerate(base_tools):
        replacement = mcp_by_name.get(tool.name)
        if replacement is not None:
            base_tools[index] = replacement
            replaced_names.add(tool.name)

    for tool in mcp_tools:
        if tool.name not in replaced_names:
            base_tools.append(tool)


def sync_mcp_tool_list(
    base_tools: list[Tool],
    mcp_tools: list[Tool],
    fallback_tools: dict[str, Tool],
) -> None:
    """List-form adapter for provenance-aware MCP registration rebuilds."""
    tool_map = {tool.name: tool for tool in base_tools}
    sync_mcp_tools(tool_map, mcp_tools, fallback_tools)
    base_tools[:] = list(tool_map.values())


def add_workspace_tools(tools: List[Tool], config: Config, workspace_dir: Path, sandbox_mode: bool = False,
                        allow_full_access: bool = True, non_interactive: bool = False, output=None,
                        llm=None, permission_engine: PermissionEngine | None = None,
                        skill_runtime_context: SkillRuntimeContext | None = None,
                        skill_loader=None, capability_state_provider=None,
                        use_output_dir: bool = True,
                        artifact_root_dir: str | Path | None = None,
                        env_context=None,
                        process_owner_id: str | None = None,
                        bypass_dangerous_command_approval: bool = False):
    """Add workspace-dependent tools

    These tools need to know the workspace directory.

    Args:
        tools: Existing tools list to add to
        config: Configuration object
        workspace_dir: Workspace directory path
        sandbox_mode: If True, enable Jupyter sandbox mode
        allow_full_access: If True, tools can access full system; if False, restricted to workspace
        non_interactive: If True, bash never prompts on stdin; dangerous
            commands return approval requests for the core/host.
        output: Callable for status messages (default: print)
        llm: LLM client instance (needed for sub_agent tool)
        permission_engine: If provided, tools use capability-based permission checks
        skill_runtime_context: Runtime env to expose to subprocess-backed tools
        skill_loader: Current live SkillLoader for explicit child Skill selection
        capability_state_provider: Read-only callable returning MCP loading/ready state
        use_output_dir: If True, execute_code chdirs into {workspace}/output.
        artifact_root_dir: Optional host-supplied output root for this session.
        process_owner_id: Optional ACP session identifier used to scope and
            reclaim background shell processes.
        bypass_dangerous_command_approval: Skip dangerous-command approval for
            an explicitly trusted full-access session.
    """
    _out = output or print
    # Ensure workspace directory exists
    workspace_dir.mkdir(parents=True, exist_ok=True)
    artifact_root = None
    if use_output_dir:
        artifact_root = (
            Path(artifact_root_dir).expanduser().resolve()
            if artifact_root_dir
            else (workspace_dir / "output").resolve()
        )
        artifact_root.mkdir(parents=True, exist_ok=True)
    relative_root = artifact_root or workspace_dir

    # Relative tool paths use the project root or the active artifact root.
    runtime_context = skill_runtime_context or build_skill_runtime_context(sandbox_mode=sandbox_mode)
    runtime_env = build_skill_execution_env(runtime_context)
    if artifact_root is not None:
        # Make the canonical delivery root available to subprocess-backed
        # skills even when a generated command unnecessarily changes cwd.
        # File tools and generate_image already resolve relative paths from
        # this directory; exposing the same root keeps shell authoring on the
        # identical boundary.
        runtime_env["BOX_AGENT_OUTPUT_DIR"] = str(artifact_root)
    if config.tools.enable_bash:
        sandbox_venv_path = None
        if sandbox_mode and not getattr(sys, "frozen", False):
            sandbox_venv_path = str(SandboxEnvironment().venv_dir)
        bash_tool = BashTool(
            workspace_dir=str(relative_root),
            scope_root_dir=str(workspace_dir),
            allow_full_access=allow_full_access,
            non_interactive=non_interactive,
            sandbox_venv_path=sandbox_venv_path,
            permission_engine=permission_engine,
            runtime_env=runtime_env,
            process_owner_id=process_owner_id,
            bypass_dangerous_command_approval=bypass_dangerous_command_approval,
        )
        tools.append(bash_tool)
        if process_owner_id is not None:
            # These session-scoped instances intentionally override the
            # process-global base tools when Agent builds its name map.
            tools.append(BashOutputTool(process_owner_id=process_owner_id))
            tools.append(BashKillTool(process_owner_id=process_owner_id))
        _out(f"{Colors.GREEN}✅ Loaded Bash tool (cwd: {relative_root}){Colors.RESET}")

    # File tools resolve relative paths from relative_root and retain workspace scope.
    if config.tools.enable_file_tools:
        tools.extend(
            [
                ReadTool(
                    workspace_dir=str(workspace_dir),
                    allow_full_access=allow_full_access,
                    permission_engine=permission_engine,
                    relative_root_dir=str(relative_root),
                ),
                JsonlQueryTool(
                    workspace_dir=str(workspace_dir),
                    allow_full_access=allow_full_access,
                    permission_engine=permission_engine,
                    relative_root_dir=str(relative_root),
                ),
                SearchFilesTool(
                    workspace_dir=str(workspace_dir),
                    allow_full_access=allow_full_access,
                    permission_engine=permission_engine,
                    relative_root_dir=str(relative_root),
                ),
                WriteTool(
                    workspace_dir=str(workspace_dir),
                    allow_full_access=allow_full_access,
                    permission_engine=permission_engine,
                    relative_root_dir=str(relative_root),
                ),
                AppendTool(
                    workspace_dir=str(workspace_dir),
                    allow_full_access=allow_full_access,
                    permission_engine=permission_engine,
                    relative_root_dir=str(relative_root),
                ),
                EditTool(
                    workspace_dir=str(workspace_dir),
                    allow_full_access=allow_full_access,
                    permission_engine=permission_engine,
                    relative_root_dir=str(relative_root),
                ),
            ]
        )
        _out(
            f"{Colors.GREEN}✅ Loaded file operation tools "
            f"(relative root: {relative_root}, scope: {workspace_dir}){Colors.RESET}"
        )

    # Todo tool - task tracking for multi-step workflows
    if config.tools.enable_todo:
        store = TodoStore()
        tools.append(TodoWriteTool(store))
        tools.append(TodoReadTool(store))
        _out(f"{Colors.GREEN}✅ Loaded todo tools (todo_write, todo_read){Colors.RESET}")

    # Plan tool - user-visible approach/scope/verification snapshots
    if getattr(config.tools, "enable_plan", True):
        store = PlanStore()
        tools.append(PlanWriteTool(store))
        tools.append(PlanReadTool(store))
        _out(f"{Colors.GREEN}✅ Loaded plan tools (plan_write, plan_read){Colors.RESET}")

    # Structured clarification pause. The core completion gate recognizes this
    # tool as a resumable wait state instead of forcing the model to fabricate
    # missing facts or abandon an in-progress artifact workflow.
    tools.append(RequestUserInputTool())
    _out(f"{Colors.GREEN}✅ Loaded user-input request tool{Colors.RESET}")

    # Public declarative decision surface for built-in and user-provided Skills.
    # The host owns rendering; this trusted tool validates any requested timeout.
    tools.append(RequestUserDecisionTool())
    _out(f"{Colors.GREEN}✅ Loaded user-decision request tool{Colors.RESET}")

    # Host-neutral execution receipt. External workflow identity, task context,
    # versions, and submission remain the host's responsibility.
    tools.append(ReportExecutionResultTool())
    _out(f"{Colors.GREEN}✅ Loaded execution result reporting tool{Colors.RESET}")

    # Jupyter sandbox tool - Python code execution environment
    if sandbox_mode:
        sandbox_tool = JupyterSandboxTool(
            workspace_dir=str(workspace_dir),
            runtime_env=runtime_context.env(),
            use_output_dir=use_output_dir,
            output_dir=str(artifact_root) if artifact_root else None,
            process_owner_id=process_owner_id,
        )
        tools.append(sandbox_tool)
        # Also add sandbox status tool
        status_tool = SandboxStatusTool(sandbox_tool)
        tools.append(status_tool)
        _out(f"{Colors.GREEN}✅ Loaded Jupyter sandbox tool (execute_code){Colors.RESET}")
        _out(f"{Colors.GREEN}✅ Loaded sandbox status tool{Colors.RESET}")

    # Image inspection tool — only expose it when the bound model can actually
    # accept image blocks. Known text-only endpoints otherwise invite costly,
    # futile resize/retry loops during presentation QA.
    image_llm = _image_capable_llm(llm)
    if image_llm is not None:
        tools.append(
            ImageInspectionTool(
                llm=image_llm,
                workspace_dir=str(workspace_dir),
                allow_full_access=allow_full_access,
                permission_engine=permission_engine,
                relative_root_dir=str(relative_root),
                native_supported=(
                    image_llm is llm and image_input_support(llm) is not False
                ),
                native_capability_llm=llm,
            )
        )
        _out(f"{Colors.GREEN}✅ Loaded image inspection tool (inspect_images){Colors.RESET}")

    # Image generation tool — standard Box-Agent capability shared by CLI and ACP.
    # Only register when an endpoint is configured (via config or env): an
    # unconfigured generate_image can only fail on every call and wastes steps.
    image_generation_config = getattr(config, "image_generation", None)
    if image_generation_service_configured(config):
        tools.append(
            GenerateImageTool(
                workspace_dir=str(workspace_dir),
                output_dir=str(artifact_root) if artifact_root else None,
                allow_full_access=allow_full_access,
                permission_engine=permission_engine,
                endpoint=getattr(image_generation_config, "endpoint", "") or None,
                api_key=getattr(image_generation_config, "api_key", "") or None,
                model=getattr(image_generation_config, "model", "") or None,
                auth_file=(
                    getattr(image_generation_config, "auth_file", "")
                    or getattr(getattr(config, "llm", None), "auth_file", "")
                ),
                timeout=getattr(image_generation_config, "timeout", None),
                max_dimension=getattr(image_generation_config, "max_dimension", None),
            )
        )
        _out(f"{Colors.GREEN}✅ Loaded image generation tool (generate_image){Colors.RESET}")
    else:
        _out(
            f"{Colors.DIM}⏭️  Skipped image generation tool (no image_generation.endpoint configured){Colors.RESET}"
        )

    # Obsidian tools — host/Vault dependent, so register with workspace tools
    # before sub-agent so child agents inherit the native Obsidian capability.
    obsidian_tools = create_obsidian_tools(env_context=env_context)
    tools.extend(obsidian_tools)
    _out(f"{Colors.GREEN}✅ Loaded Obsidian tools (obsidian_create_note, obsidian_update_note, obsidian_daily_note){Colors.RESET}")

    # Sub-agent tool — must be registered last so it can reference all other tools
    if config.tools.enable_sub_agent and llm is not None:
        parent_tools = {t.name: t for t in tools}
        tool_limits = getattr(config, "tool_limits", ToolLimitsConfig())
        sub_agent_tool = SubAgentTool(
            llm=llm,
            parent_tools=parent_tools,
            workspace_dir=str(workspace_dir),
            tool_limits=tool_limits,
            token_limit=config.agent.sub_agent_token_limit,
            batch_synthesis_timeout_seconds=(
                config.agent.sub_agent_batch_synthesis_timeout_seconds
            ),
            artifact_detection_enabled=use_output_dir,
            artifact_root_dir=str(artifact_root) if artifact_root else None,
            provider_stale_seconds=config.agent.provider_stale_seconds,
        )
        if skill_loader is not None:
            sub_agent_tool.set_skill_provider(lambda: skill_loader)
        if capability_state_provider is not None:
            sub_agent_tool.set_capability_state_provider(capability_state_provider)
        tools.append(sub_agent_tool)
        _out(f"{Colors.GREEN}✅ Loaded sub-agent tool (sub_agent){Colors.RESET}")
