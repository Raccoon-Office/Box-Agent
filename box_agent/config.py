"""Configuration management module

Provides unified configuration loading and management functionality
"""

import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


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
    # Memory
    enable_memory: bool = True
    memory_dir: str = "~/.box-agent/memory"
    memory_recall_days: int = 3


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
    enable_note: bool = True
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


class Config(BaseModel):
    """Main configuration class"""

    llm: LLMConfig
    agent: AgentConfig
    tools: ToolsConfig

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
            memory_recall_days=data.get("memory_recall_days", 3),
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
            enable_note=tools_data.get("enable_note", True),
            allow_full_access=tools_data.get("allow_full_access", False),
            enable_skills=tools_data.get("enable_skills", True),
            skills_dir=tools_data.get("skills_dir", "./skills"),
            enable_mcp=tools_data.get("enable_mcp", True),
            mcp_config_path=tools_data.get("mcp_config_path", "mcp.json"),
            mcp=mcp_config,
        )

        return cls(
            llm=llm_config,
            agent=agent_config,
            tools=tools_config,
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
