"""Configuration management module

Provides unified configuration loading and management functionality
"""

import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, PrivateAttr


class RetryConfig(BaseModel):
    """Retry configuration"""

    enabled: bool = True
    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0


class LLMConfig(BaseModel):
    """LLM configuration"""

    api_key: str
    api_base: str = "https://api.anthropic.com"
    model: str = "claude-sonnet-4-20250514"
    provider: str = "anthropic"  # "anthropic" or "openai"
    retry: RetryConfig = Field(default_factory=RetryConfig)


class AgentConfig(BaseModel):
    """Agent configuration"""

    max_steps: int = 50
    workspace_dir: str = "./workspace"
    system_prompt_path: str = "system_prompt.md"
    analysis_prompt_path: str = "analysis_prompt.md"
    ppt_plan_chat_prompt_path: str = "ppt_plan_chat_prompt.md"
    ppt_outline_prompt_path: str = "ppt_outline_prompt.md"
    ppt_editor_prompt_path: str = "ppt_editor_standard_html_prompt.md"
    # Memory
    enable_memory: bool = True
    memory_dir: str = "~/.box-agent/memory"
    # Memory auto-extraction
    enable_memory_extraction: bool = True
    memory_extraction_cooldown: int = 300  # seconds between extractions
    memory_extraction_step_interval: int = 10  # extract every N agent steps


class MCPConfig(BaseModel):
    """MCP (Model Context Protocol) timeout configuration"""

    connect_timeout: float = 10.0  # Connection timeout (seconds)
    execute_timeout: float = 60.0  # Tool execution timeout (seconds)
    sse_read_timeout: float = 120.0  # SSE read timeout (seconds)


class ToolsConfig(BaseModel):
    """Tools configuration"""

    # Basic tools (file operations, bash)
    enable_file_tools: bool = True
    enable_bash: bool = True
    enable_todo: bool = True  # Task tracking for multi-step workflows
    enable_sub_agent: bool = True  # Sub-agent for isolated context execution
    enable_web_search: bool = True  # Fallback web search when no MCP search service

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

    ``allowed_directories`` extends ``session_workspace`` and ``custom`` scopes
    with a whitelist of additional directories (paths may contain ``~`` or
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

        # Parse LLM configuration
        if "api_key" not in data:
            raise ValueError("Configuration file missing required field: api_key")

        if not data["api_key"] or data["api_key"] == "YOUR_API_KEY_HERE":
            raise ValueError("Please configure a valid API Key")

        # Parse retry configuration
        retry_data = data.get("retry", {})
        retry_config = RetryConfig(
            enabled=retry_data.get("enabled", True),
            max_retries=retry_data.get("max_retries", 3),
            initial_delay=retry_data.get("initial_delay", 1.0),
            max_delay=retry_data.get("max_delay", 60.0),
            exponential_base=retry_data.get("exponential_base", 2.0),
        )

        llm_config = LLMConfig(
            api_key=data["api_key"],
            api_base=data.get("api_base", "https://api.anthropic.com"),
            model=data.get("model", "claude-sonnet-4-20250514"),
            provider=data.get("provider", "anthropic"),
            retry=retry_config,
        )

        # Parse Agent configuration
        agent_config = AgentConfig(
            max_steps=data.get("max_steps", 50),
            workspace_dir=data.get("workspace_dir", "./workspace"),
            system_prompt_path=data.get("system_prompt_path", "system_prompt.md"),
            enable_memory=data.get("enable_memory", True),
            memory_dir=data.get("memory_dir", "~/.box-agent/memory"),
            enable_memory_extraction=data.get("enable_memory_extraction", True),
            memory_extraction_cooldown=data.get("memory_extraction_cooldown", 300),
            memory_extraction_step_interval=data.get("memory_extraction_step_interval", 10),
        )

        # Parse tools configuration
        tools_data = data.get("tools", {})

        # Parse MCP configuration
        mcp_data = tools_data.get("mcp", {})
        mcp_config = MCPConfig(
            connect_timeout=mcp_data.get("connect_timeout", 10.0),
            execute_timeout=mcp_data.get("execute_timeout", 60.0),
            sse_read_timeout=mcp_data.get("sse_read_timeout", 120.0),
        )

        tools_config = ToolsConfig(
            enable_file_tools=tools_data.get("enable_file_tools", True),
            enable_bash=tools_data.get("enable_bash", True),
            enable_todo=tools_data.get("enable_todo", True),
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
