"""Configuration management module

Provides unified configuration loading and management functionality
"""

import shutil
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from .auth import should_attach_auth_header

DEFAULT_API_KEY_PLACEHOLDER = "YOUR_API_KEY_HERE"
DEFAULT_MODEL = "claude-sonnet-4-20250514"
HOSTED_GATEWAY_API_KEY_PLACEHOLDER = "box-agent-auth-json"
XIAOHUANXIONG_MAX_OUTPUT_TOKENS = 80000
USER_CONFIGURED_MAX_OUTPUT_TOKENS = 63999
CONTEXT_INPUT_SAFETY_RATIO = 0.8


def derive_context_token_limit(context_window: int, max_output_tokens: int) -> int:
    """Return the safe input budget for one model capability declaration."""

    if context_window <= 0:
        raise ValueError("context_window must be positive")
    if max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be positive")
    if max_output_tokens >= context_window:
        raise ValueError("max_output_tokens must be smaller than context_window")
    token_limit = int(
        (context_window - max_output_tokens) * CONTEXT_INPUT_SAFETY_RATIO
    )
    if token_limit <= 0:
        raise ValueError("model capabilities leave no safe input token budget")
    return token_limit


def _is_xiaohuanxiong_api_base(api_base: str) -> bool:
    try:
        hostname = urlparse(api_base).hostname or ""
    except ValueError:
        return False
    hostname = hostname.lower().rstrip(".")
    return hostname == "xiaohuanxiong.com" or hostname.endswith(".xiaohuanxiong.com")


def _default_max_output_tokens_for_api_base(api_base: str) -> int:
    if _is_xiaohuanxiong_api_base(api_base):
        return XIAOHUANXIONG_MAX_OUTPUT_TOKENS
    return USER_CONFIGURED_MAX_OUTPUT_TOKENS


class RetryConfig(BaseModel):
    """Retry configuration"""

    enabled: bool = True
    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0


class LLMConfig(BaseModel):
    """LLM configuration"""

    api_key: str = ""
    api_base: str = "https://api.anthropic.com"
    model: str = DEFAULT_MODEL
    provider: str = "anthropic"  # "anthropic" or "openai"
    auth_file: str = ""
    context_window: int = 180000
    max_output_tokens: int = USER_CONFIGURED_MAX_OUTPUT_TOKENS
    # Wall-clock cap (seconds) handed to the underlying provider SDK. For
    # streaming calls this bounds the gap between bytes (connect + per-read),
    # not the total generation, so a long answer is fine as long as tokens keep
    # flowing. Defaults to the OpenAI/Anthropic SDK default of 600s; lower it to
    # fail fast and surface a clean error instead of hanging when a gateway
    # stalls before the first token.
    timeout: float = 600.0
    retry: RetryConfig = Field(default_factory=RetryConfig)

    @property
    def context_token_limit(self) -> int:
        """Token threshold that triggers context summarization.

        Derived as 80% of the input budget — i.e. 80% of
        ``context_window - max_output_tokens``. The 20% headroom absorbs
        token-estimate drift and the summarization request itself.
        """
        return derive_context_token_limit(self.context_window, self.max_output_tokens)


class LiteLLMConfig(BaseModel):
    """Lightweight LLM for small tool-free tasks (titles, summaries, rewrites).

    When the ``lite_llm:`` block is absent from ``config.yaml``, ``_present``
    stays ``False`` and the ACP layer aliases the lite client to the main
    LLM. Auth follows the same hosted-gateway rules as the main block:
    empty ``api_key`` against a hosted ``api_base`` falls back to ``auth.json``.

    ``max_output_tokens`` is deliberately distinct from the main model: many
    lightweight endpoints reject values above ~65k. The default of ``63999``
    sits just under the common 65536 ceiling.
    """

    _present: bool = PrivateAttr(default=False)
    api_key: str = ""
    api_base: str = ""
    model: str = ""
    provider: str = "openai"
    auth_file: str = ""
    max_output_tokens: int = 63999
    # See ``LLMConfig.timeout``. Mirrors the main model's wall-clock cap.
    timeout: float = 600.0
    retry: RetryConfig = Field(default_factory=RetryConfig)


class ImageGenerationConfig(BaseModel):
    """Image generation service configuration."""

    endpoint: str = ""
    api_key: str = ""
    model: str = "gpt-image-1"
    timeout: float = 120.0
    auth_file: str = ""


class ToolLimitsModel(BaseModel):
    """Strict base so misspelled limit keys fail instead of silently defaulting."""

    model_config = ConfigDict(extra="forbid")


class GeneralToolLimitsConfig(ToolLimitsModel):
    """Limits shared by ordinary top-level agent turns."""

    final_summary_after_calls: int = Field(default=150, ge=1, le=512)
    wrapup_remaining_steps: int = Field(default=10, ge=0, le=50)


class WebSearchLimitsConfig(ToolLimitsModel):
    """Per-turn web-search batching and total-call budgets."""

    batch_size: int = Field(default=6, ge=1, le=32)
    concurrency: int = Field(default=2, ge=1, le=32)
    total_calls: int = Field(default=50, ge=0, le=512)
    deep_research_total_calls: int = Field(default=100, ge=0, le=512)

    @model_validator(mode="after")
    def validate_concurrency(self) -> "WebSearchLimitsConfig":
        if self.concurrency > self.batch_size:
            raise ValueError("concurrency cannot exceed batch_size")
        return self


class SearchFilesLimitsConfig(ToolLimitsModel):
    """Circuit-breaker limits for repeated local file searches."""

    consecutive_empty_limit: int = Field(default=6, ge=1, le=50)


class WorkflowToolLimitsConfig(ToolLimitsModel):
    """Total-call and completion-reserve limits for artifact workflows."""

    max_tool_calls: int = Field(default=128, ge=1, le=512)
    completion_reserve_calls: int = Field(default=10, ge=0, le=128)

    @model_validator(mode="after")
    def validate_completion_reserve(self) -> "WorkflowToolLimitsConfig":
        if self.completion_reserve_calls > self.max_tool_calls:
            raise ValueError("completion_reserve_calls cannot exceed max_tool_calls")
        return self


class PresentationToolLimitsConfig(WorkflowToolLimitsConfig):
    """Controlled-presentation budgets, including deep research."""

    deep_research_max_tool_calls: int = Field(default=200, ge=1, le=512)
    research_rounds: int = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def validate_deep_research_reserve(self) -> "PresentationToolLimitsConfig":
        if self.completion_reserve_calls > self.deep_research_max_tool_calls:
            raise ValueError(
                "completion_reserve_calls cannot exceed deep_research_max_tool_calls"
            )
        return self


class SubAgentToolLimitsConfig(ToolLimitsModel):
    """Sub-agent loop and stall-detection limits."""

    general_max_steps: int = Field(default=60, ge=1, le=256)
    general_max_tool_calls: int = Field(default=100, ge=1, le=256)
    no_progress_steps: int = Field(default=6, ge=1, le=50)


class ToolLimitsConfig(ToolLimitsModel):
    """Single typed source for user-tunable tool execution limits."""

    general: GeneralToolLimitsConfig = Field(default_factory=GeneralToolLimitsConfig)
    web_search: WebSearchLimitsConfig = Field(default_factory=WebSearchLimitsConfig)
    search_files: SearchFilesLimitsConfig = Field(default_factory=SearchFilesLimitsConfig)
    external_skill: WorkflowToolLimitsConfig = Field(default_factory=WorkflowToolLimitsConfig)
    presentation: PresentationToolLimitsConfig = Field(
        default_factory=PresentationToolLimitsConfig
    )
    sub_agent: SubAgentToolLimitsConfig = Field(default_factory=SubAgentToolLimitsConfig)


class AgentConfig(BaseModel):
    """Agent configuration"""

    max_steps: int = 300
    workspace_dir: str = "./workspace"
    # Hard ceiling on how many parallel_safe tool calls (sub_agent,
    # generate_image) run concurrently within a single step, regardless of how
    # many the model emits. Excess calls queue and run as slots free up. Guards
    # against a prompt like "spawn as many sub-agents as possible" exhausting
    # LLM rate limits / processes / memory.
    max_parallel_tools: int = 8
    # Wall-clock cap for one batch of parallel_safe tool calls. Completed
    # sibling results are kept; unfinished siblings get synthetic timeout
    # failures so the parent can continue instead of waiting forever.
    parallel_tool_timeout_seconds: float = 900.0
    # Continue an active durable goal after a natural end_turn, bounded so a
    # third-party outage or bad plan cannot loop forever.
    goal_autopilot_enabled: bool = True
    goal_autopilot_max_turns: int = 3
    goal_autopilot_max_seconds: float = 14400.0
    goal_autopilot_no_progress_turns: int = 2
    # Suspected-truncation continuation: some upstream models / relay gateways
    # stop a streamed text turn mid-sentence yet report a normal finish_reason
    # ("stop"/"end_turn") or omit it. When enabled, the loop detects this
    # conservatively (loop_guards.looks_like_truncated_output) and injects a
    # one-shot continuation so the reply finishes within the same message.
    retry_on_suspected_truncation: bool = True
    max_truncation_continuations: int = 3
    # Truncated tool-call retry: a broken tool_call JSON stream can be
    # recovered by retrying the same turn (with the SAME messages) and
    # boosting the per-request max_tokens on genuine output-cap truncations.
    # ``max_truncated_tool_call_retries`` caps the attempts; the boosted
    # per-request cap never exceeds ``truncated_tool_call_boost_cap``.
    max_truncated_tool_call_retries: int = 3
    truncated_tool_call_boost_cap: int = 32768
    # Keep repeated read_file output compact only while an exact source remains
    # in the live model context. Disable for immediate rollback.
    context_resource_dedup_enabled: bool = True
    system_prompt_path: str = "system_prompt.md"
    analysis_prompt_path: str = "analysis_prompt.md"
    code_prompt_path: str = "code_prompt.md"
    # Memory
    enable_memory: bool = True
    memory_dir: str = "~/.box-agent/memory"
    # Memory auto-extraction
    enable_memory_extraction: bool = True
    memory_extraction_cooldown: int = 300  # seconds between extractions
    memory_extraction_step_interval: int = 10  # extract every N agent steps
    # Memory maintenance (decay + dedup)
    memory_maintainer_enabled: bool = True
    memory_maintainer_interval_hours: int = 24  # min hours between maintenance runs
    memory_decay_days: int = 30  # active → archive after this many days without hits
    memory_archive_days: int = 90  # archive → trash after this many more days
    memory_dedup_jaccard: float = 0.85  # token-overlap threshold for entry merging
    memory_compaction_enabled: bool = True  # LLM topic-cluster compaction in maintainer
    memory_context_max_entries: int = 50  # compaction triggers above this
    memory_context_max_tokens: int = 8000  # compaction triggers above this (estimated)
    memory_conflict_resolution_enabled: bool = True  # LLM-arbitrated semantic conflict pass
    memory_conflict_cluster_threshold: float = 0.3  # Jaccard for clustering conflict candidates
    memory_conflict_max_clusters_per_run: int = 5  # cap LLM calls per maintainer run
    memory_promotion_proposal_enabled: bool = True  # suggest v2 experience → core
    memory_promotion_hit_threshold: int = 5  # min explicit-search hits before suggesting promotion
    memory_promotion_cooldown_days: int = 14  # skip re-proposing for this long


class MCPConfig(BaseModel):
    """MCP (Model Context Protocol) timeout configuration"""

    deferred_loading_enabled: bool = True
    connect_timeout: float = 60.0  # Connection timeout (seconds)
    execute_timeout: float = 60.0  # Tool execution timeout (seconds)
    sse_read_timeout: float = 120.0  # SSE read timeout (seconds)


class ToolsConfig(BaseModel):
    """Tools configuration"""

    # Basic tools (file operations, bash)
    enable_file_tools: bool = True
    enable_bash: bool = True
    enable_todo: bool = True  # Task tracking for multi-step workflows
    enable_plan: bool = True  # Structured user-visible planning snapshots
    enable_sub_agent: bool = True  # Sub-agent for isolated context execution

    # Safety
    allow_full_access: bool = False  # When False, tools are restricted to workspace

    # Skills
    enable_skills: bool = True
    skills_dir: str = "./skills"

    # MCP tools
    enable_mcp: bool = True
    mcp_config_path: str = "mcp.json"
    mcp: MCPConfig = Field(default_factory=MCPConfig)


class FilesystemPermissions(BaseModel):
    """Filesystem capability permissions.

    Canonical field is ``scope`` (maps to officev3 ``fileAccessScope``).
    Read and write share the same scope — no protocol-level read/write split.

    ``allowed_directories`` extends the selected base scope with a whitelist
    of additional directories (paths may contain ``~`` or
    ``$HOME`` — expansion happens at engine construction time).
    """

    scope: str = "session_workspace"
    allowed_directories: list[str] = Field(default_factory=list)


class MemoryPermissions(BaseModel):
    """Memory capability permissions."""

    openclaw_import: bool = True


class Officev3Permissions(BaseModel):
    """Officev3 permission settings."""

    filesystem: FilesystemPermissions = Field(default_factory=FilesystemPermissions)
    memory: MemoryPermissions = Field(default_factory=MemoryPermissions)


class Officev3Paths(BaseModel):
    """Officev3 path settings."""

    session_workspace_root: str = ""


class Officev3Config(BaseModel):
    """Officev3 configuration block."""

    _present: bool = PrivateAttr(default=False)  # True if officev3 block exists in config.yaml
    permissions: Officev3Permissions = Field(default_factory=Officev3Permissions)
    paths: Officev3Paths = Field(default_factory=Officev3Paths)


class HooksConfig(BaseModel):
    """Lifecycle hooks configuration.

    Each entry is a fully-qualified Python class path that will be
    imported and instantiated at startup.  The same list is loaded
    by both CLI and ACP for consistent behaviour.
    """

    hooks: list[str] = Field(default_factory=list)


class Config(BaseModel):
    """Main configuration class"""

    llm: LLMConfig
    lite_llm: LiteLLMConfig = Field(default_factory=LiteLLMConfig)
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)
    tool_limits: ToolLimitsConfig = Field(default_factory=ToolLimitsConfig)
    agent: AgentConfig
    tools: ToolsConfig
    officev3: Officev3Config = Field(default_factory=Officev3Config)
    hooks: HooksConfig = Field(default_factory=HooksConfig)

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from the default search path."""
        config_path = cls.get_default_config_path()
        if not config_path.exists():
            raise FileNotFoundError("Configuration file not found. Run scripts/setup-config.sh or place config.yaml in box_agent/config/.")
        return cls.from_yaml(config_path)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Config":
        """Load configuration from YAML file

        Args:
            config_path: Configuration file path

        Returns:
            Config instance

        Raises:
            FileNotFoundError: Configuration file does not exist
            ValueError: Invalid configuration format or missing required fields
        """
        config_path = Path(config_path)

        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file does not exist: {config_path}")

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError("Configuration file is empty")

        api_base = data.get("api_base", "https://api.anthropic.com")
        uses_hosted_gateway = should_attach_auth_header(api_base)

        # Parse LLM configuration. Hosted officev3 gateways authenticate with
        # auth.json, so api_key/model may be intentionally omitted.
        raw_api_key = str(data.get("api_key", "")).strip()
        if not uses_hosted_gateway:
            if "api_key" not in data:
                raise ValueError("Configuration file missing required field: api_key")
            if not raw_api_key or raw_api_key == DEFAULT_API_KEY_PLACEHOLDER:
                raise ValueError("Please configure a valid API Key")
        if uses_hosted_gateway and raw_api_key == DEFAULT_API_KEY_PLACEHOLDER:
            api_key = HOSTED_GATEWAY_API_KEY_PLACEHOLDER
        else:
            api_key = raw_api_key or HOSTED_GATEWAY_API_KEY_PLACEHOLDER

        raw_model = data.get("model")
        if uses_hosted_gateway and (raw_model is None or str(raw_model).strip() == DEFAULT_MODEL):
            model = ""
        else:
            model = str(raw_model).strip() if raw_model is not None else DEFAULT_MODEL

        # Parse retry configuration
        retry_data = data.get("retry", {})
        retry_config = RetryConfig(
            enabled=retry_data.get("enabled", True),
            max_retries=retry_data.get("max_retries", 3),
            initial_delay=retry_data.get("initial_delay", 1.0),
            max_delay=retry_data.get("max_delay", 60.0),
            exponential_base=retry_data.get("exponential_base", 2.0),
        )

        default_max_output_tokens = _default_max_output_tokens_for_api_base(api_base)
        llm_config = LLMConfig(
            api_key=api_key,
            api_base=api_base,
            model=model,
            provider=data.get("provider", "anthropic"),
            auth_file=data.get("auth_file") or str(config_path.parent / "auth.json"),
            context_window=data.get("context_window", 180000),
            max_output_tokens=data.get("max_output_tokens", default_max_output_tokens),
            timeout=float(data.get("timeout", 600.0) or 600.0),
            retry=retry_config,
        )

        # Parse optional lite_llm block. Mirrors the main LLM auth rules:
        # hosted gateways (matched by api_base) accept an empty api_key and
        # fall through to auth.json. When the block is absent, _present stays
        # False and the ACP layer aliases the lite client to the main LLM.
        lite_llm_data = data.get("lite_llm")
        lite_llm_config = LiteLLMConfig()
        if isinstance(lite_llm_data, dict) and lite_llm_data:
            lite_api_base = str(lite_llm_data.get("api_base", "")).strip()
            if not lite_api_base:
                raise ValueError("lite_llm.api_base is required when the lite_llm block is present")
            lite_uses_hosted = should_attach_auth_header(lite_api_base)
            lite_raw_key = str(lite_llm_data.get("api_key", "")).strip()
            if not lite_uses_hosted and (not lite_raw_key or lite_raw_key == DEFAULT_API_KEY_PLACEHOLDER):
                raise ValueError("lite_llm.api_key is required for non-hosted endpoints")
            if lite_uses_hosted and (not lite_raw_key or lite_raw_key == DEFAULT_API_KEY_PLACEHOLDER):
                lite_api_key = HOSTED_GATEWAY_API_KEY_PLACEHOLDER
            else:
                lite_api_key = lite_raw_key
            lite_model_raw = lite_llm_data.get("model")
            lite_model = str(lite_model_raw).strip() if lite_model_raw is not None else ""
            if not lite_uses_hosted and not lite_model:
                raise ValueError("lite_llm.model is required for non-hosted endpoints")
            lite_retry_data = lite_llm_data.get("retry")
            if isinstance(lite_retry_data, dict):
                lite_retry = RetryConfig(
                    enabled=lite_retry_data.get("enabled", True),
                    max_retries=lite_retry_data.get("max_retries", 3),
                    initial_delay=lite_retry_data.get("initial_delay", 1.0),
                    max_delay=lite_retry_data.get("max_delay", 60.0),
                    exponential_base=lite_retry_data.get("exponential_base", 2.0),
                )
            else:
                lite_retry = retry_config
            lite_max_output_tokens = int(lite_llm_data.get("max_output_tokens", 63999) or 63999)
            if lite_max_output_tokens <= 0:
                raise ValueError("lite_llm.max_output_tokens must be positive")
            if lite_max_output_tokens > 65536:
                raise ValueError(
                    f"lite_llm.max_output_tokens={lite_max_output_tokens} exceeds the 65536 ceiling; common lite endpoints reject larger values"
                )
            lite_llm_config = LiteLLMConfig(
                api_key=lite_api_key,
                api_base=lite_api_base,
                model=lite_model,
                provider=str(lite_llm_data.get("provider", "openai")).strip() or "openai",
                auth_file=lite_llm_data.get("auth_file") or str(config_path.parent / "auth.json"),
                max_output_tokens=lite_max_output_tokens,
                timeout=float(lite_llm_data.get("timeout", 600.0) or 600.0),
                retry=lite_retry,
            )
            lite_llm_config._present = True

        # Parse image generation configuration. This mirrors LLM auth behavior:
        # by default it reads auth.json next to config.yaml before each hosted
        # request, while still allowing a dedicated service token when needed.
        image_generation_data = data.get("image_generation", {})
        if not isinstance(image_generation_data, dict):
            image_generation_data = {}
        image_generation_config = ImageGenerationConfig(
            endpoint=str(image_generation_data.get("endpoint", "") or "").strip(),
            api_key=str(image_generation_data.get("api_key", "") or "").strip(),
            model=str(image_generation_data.get("model", "gpt-image-1") or "").strip(),
            timeout=float(image_generation_data.get("timeout", 120.0) or 120.0),
            auth_file=image_generation_data.get("auth_file") or llm_config.auth_file,
        )

        tool_limits_data = data.get("tool_limits", {})
        if not isinstance(tool_limits_data, dict):
            raise ValueError("tool_limits must be a mapping")
        tool_limits_config = ToolLimitsConfig(**tool_limits_data)

        # Parse top-level AgentConfig keys without duplicating their defaults.
        # A runtime upgrade can therefore change one default in AgentConfig and
        # users who did not explicitly override that key inherit it immediately.
        agent_data = {
            field_name: data[field_name]
            for field_name in AgentConfig.model_fields
            if field_name in data
        }
        # Preserve the historical behavior where an explicit YAML null for the
        # parallel timeout means "use the runtime default".
        if agent_data.get("parallel_tool_timeout_seconds") is None:
            agent_data.pop("parallel_tool_timeout_seconds", None)
        agent_config = AgentConfig(**agent_data)

        # Parse tools configuration
        tools_data = data.get("tools", {})

        # Parse MCP configuration
        mcp_data = tools_data.get("mcp", {})
        mcp_config = MCPConfig(
            deferred_loading_enabled=mcp_data.get("deferred_loading_enabled", True),
            connect_timeout=mcp_data.get("connect_timeout", 60.0),
            execute_timeout=mcp_data.get("execute_timeout", 60.0),
            sse_read_timeout=mcp_data.get("sse_read_timeout", 120.0),
        )

        tools_config = ToolsConfig(
            enable_file_tools=tools_data.get("enable_file_tools", True),
            enable_bash=tools_data.get("enable_bash", True),
            enable_todo=tools_data.get("enable_todo", True),
            enable_plan=tools_data.get("enable_plan", True),
            enable_sub_agent=tools_data.get("enable_sub_agent", True),
            allow_full_access=tools_data.get("allow_full_access", False),
            enable_skills=tools_data.get("enable_skills", True),
            skills_dir=tools_data.get("skills_dir", "./skills"),
            enable_mcp=tools_data.get("enable_mcp", True),
            mcp_config_path=tools_data.get("mcp_config_path", "mcp.json"),
            mcp=mcp_config,
        )

        # Parse officev3 configuration
        officev3_data = data.get("officev3")
        officev3_config = Officev3Config()
        if officev3_data is not None and isinstance(officev3_data, dict):
            officev3_config._present = True
            perms_data = officev3_data.get("permissions", {})
            paths_data = officev3_data.get("paths", {})

            fs_data = perms_data.get("filesystem", {}) if isinstance(perms_data, dict) else {}
            mem_data = perms_data.get("memory", {}) if isinstance(perms_data, dict) else {}

            if isinstance(fs_data, dict):
                allowed_dirs_raw = fs_data.get("allowed_directories", [])
                allowed_dirs = (
                    [str(d) for d in allowed_dirs_raw if isinstance(d, str)]
                    if isinstance(allowed_dirs_raw, list)
                    else []
                )
                fs_perms = FilesystemPermissions(
                    scope=fs_data.get("scope", "session_workspace"),
                    allowed_directories=allowed_dirs,
                )
            else:
                fs_perms = FilesystemPermissions()

            mem_perms = MemoryPermissions(
                openclaw_import=mem_data.get("openclaw_import", True),
            ) if isinstance(mem_data, dict) else MemoryPermissions()

            officev3_config = Officev3Config(
                permissions=Officev3Permissions(filesystem=fs_perms, memory=mem_perms),
                paths=Officev3Paths(
                    session_workspace_root=paths_data.get("session_workspace_root", "") if isinstance(paths_data, dict) else "",
                ),
            )
            officev3_config._present = True

        # Parse hooks configuration
        hooks_data = data.get("hooks", [])
        if not isinstance(hooks_data, list):
            hooks_data = []
        hooks_config = HooksConfig(hooks=hooks_data)

        return cls(
            llm=llm_config,
            lite_llm=lite_llm_config,
            image_generation=image_generation_config,
            tool_limits=tool_limits_config,
            agent=agent_config,
            tools=tools_config,
            officev3=officev3_config,
            hooks=hooks_config,
        )

    @staticmethod
    def get_package_dir() -> Path:
        """Get the package installation directory

        Returns:
            Path to the box_agent package directory
        """
        # Get the directory where this config.py file is located
        return Path(__file__).parent

    @classmethod
    def find_config_file(cls, filename: str) -> Path | None:
        """Find configuration file with priority order

        Search for config file in the following order of priority:
        1) box_agent/config/{filename} in current directory (development mode)
        2) ~/.box-agent/config/{filename} in user home directory
        3) {package}/box_agent/config/{filename} in package installation directory

        Args:
            filename: Configuration file name (e.g., "config.yaml", "mcp.json", "system_prompt.md")

        Returns:
            Path to found config file, or None if not found
        """
        # Priority 1: Development mode - current directory's config/ subdirectory
        dev_config = Path.cwd() / "box_agent" / "config" / filename
        if dev_config.exists():
            return dev_config

        # Priority 2: User config directory
        user_config = Path.home() / ".box-agent" / "config" / filename
        if user_config.exists():
            return user_config

        # Priority 3: Package installation directory's config/ subdirectory
        package_config = cls.get_package_dir() / "config" / filename
        if package_config.exists():
            return package_config

        return None

    @classmethod
    def get_default_config_path(cls) -> Path:
        """Get the default config file path with priority search.

        If no config.yaml exists anywhere, auto-initializes one from the
        bundled example so that first-run users get a working file.

        Returns:
            Path to config.yaml (prioritizes: dev config/ > user config/ > package config/)
        """
        config_path = cls.find_config_file("config.yaml")
        if config_path:
            return config_path

        # No config.yaml found anywhere — bootstrap from example
        return cls._ensure_user_config()

    @classmethod
    def _ensure_user_config(cls) -> Path:
        """Copy config-example.yaml to ~/.box-agent/config/config.yaml.

        Returns:
            Path to the newly created config.yaml
        """
        user_config_dir = Path.home() / ".box-agent" / "config"
        user_config_dir.mkdir(parents=True, exist_ok=True)
        target = user_config_dir / "config.yaml"

        # Don't overwrite existing config
        if target.exists():
            return target

        example = cls.get_package_dir() / "config" / "config-example.yaml"
        if example.exists():
            shutil.copy2(example, target)
        else:
            # Fallback: write a minimal config
            target.write_text(
                '# Box Agent Configuration\n'
                '# Edit this file to add your API key and base URL\n'
                'api_key: "YOUR_API_KEY_HERE"\n'
                'api_base: "https://api.anthropic.com"\n'
                'model: "claude-sonnet-4-20250514"\n'
                'provider: "anthropic"\n',
                encoding="utf-8",
            )

        return target
