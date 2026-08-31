# Development Guide

## Table of Contents

- [Development Guide](#development-guide)
  - [Table of Contents](#table-of-contents)
  - [1. Project Architecture](#1-project-architecture)
  - [2. Basic Usage](#2-basic-usage)
    - [2.1 Interactive Commands](#21-interactive-commands)
    - [2.2 Integrated MCP Tools](#22-integrated-mcp-tools)
      - [Tavily - Web Search and Extraction](#tavily---web-search-and-extraction)
      - [Memory - MCP Knowledge Graph Server](#memory---mcp-knowledge-graph-server)
      - [Playwright - Browser Automation](#playwright---browser-automation)
  - [3. Extended Abilities](#3-extended-abilities)
    - [3.1 Adding Custom Tools](#31-adding-custom-tools)
      - [Steps](#steps)
      - [Example](#example)
    - [3.2 Adding MCP Tools](#32-adding-mcp-tools)
    - [3.3 Built-in Skills](#33-built-in-skills)
      - [Recommended Skills for officev3](#recommended-skills-for-officev3)
    - [3.4 Adding a New Skill](#34-adding-a-new-skill)
    - [3.5 Customizing System Prompt](#35-customizing-system-prompt)
      - [What You Can Customize](#what-you-can-customize)
  - [4. Troubleshooting](#4-troubleshooting)
    - [4.1 Common Issues](#41-common-issues)
      - [API Key Configuration Error](#api-key-configuration-error)
      - [Dependency Installation Failure](#dependency-installation-failure)
      - [MCP Tool Loading Failure](#mcp-tool-loading-failure)
    - [4.2 Debugging Tips](#42-debugging-tips)
      - [Enable Verbose Logging](#enable-verbose-logging)
      - [Using the Python Debugger](#using-the-python-debugger)
      - [Inspecting Tool Calls](#inspecting-tool-calls)

---

## 1. Project Architecture

The ownership rules, dependency direction, and stable integration API are
defined in the [Layered Architecture](ARCHITECTURE.md). Read it before adding
shared runtime behavior.

```
box-agent/
├── box_agent/              # Core source code
│   ├── core.py              # Execution core — run_agent_loop() (the agent loop)
│   ├── agent.py             # Public API wrapper (Agent class)
│   ├── runtime.py           # Composition root and stable Core bridge
│   ├── completion.py        # Generic deliverable-router composition
│   ├── delivery.py          # Generic deliverable-intent classification
│   ├── workflow_policy.py   # Stable workflow contract consumed by Core
│   ├── workflows/           # Workflow routing, checkpoints, and policies
│   ├── artifacts.py         # Shared artifact contract helpers
│   ├── turn_policy.py       # Shared turn classification policies
│   ├── llm/                 # Provider clients and LLM wrapper
│   ├── acp/                 # ACP server and host integration
│   ├── cli.py               # Command-line interface
│   ├── config.py            # Configuration loading
│   ├── tools/               # Tool implementations (file, bash, MCP, skills, etc.)
│   └── skills/              # Built-in Skills and manifest
├── tests/                   # Test code
├── docs/                    # Documentation
├── workspace/               # Working directory
└── pyproject.toml           # Project configuration
```

## 2. Basic Usage

### 2.1 Interactive Commands

When running the agent in interactive mode (`box-agent`), the following commands are available:

| Command                | Description                                                 |
| ---------------------- | ----------------------------------------------------------- |
| `/exit`, `/quit`, `/q` | Exit the agent and display session statistics               |
| `/help`                | Display help information and available commands             |
| `/clear`               | Clear message history and start a new session               |
| `/clear_all`           | Clear message history and shut down the sandbox kernel      |
| `/history`             | Show the current session message count                      |
| `/stats`               | Display session statistics (steps, tool calls, tokens used) |
| `/sandbox_status`      | Show sandbox session status                                 |
| `/log`                 | Show log directory or read a specific log file              |
| `/goal`                | Show or manage the durable session goal                     |
| `/memory review`       | Review memory promotion candidates                          |

CLI management commands are also scriptable:

```bash
box-agent config --get model
box-agent config --set max_steps 300
box-agent config --json
box-agent doctor --json
box-agent --task "summarize README.md" --json
box-agent --task "create a PPT" --force-plan-start
box-agent --task "create a PPT" --no-completion-gate
box-agent --goal "Finish release checklist" --task "run verification"
box-agent --goal "Finish release checklist" --task "run verification" --no-goal-autopilot
box-agent goal status --json
box-agent goal progress "updated ACP docs"
box-agent goal complete --evidence "uv run pytest tests/ -q passed"
box-agent --deep-think --task "review this repository"
```

### 2.2 Integrated MCP Tools

This project ships a disabled-by-default MCP example configuration at `box_agent/config/mcp-example.json`.
Run `box-agent install-browser` to install Chromium and enable the Playwright entry in the user config.
Other MCP servers must be enabled explicitly in `~/.box-agent/config/mcp.json`.

#### Tavily - Web Search and Extraction

**Function**: Web search and content extraction via Tavily MCP.

**Status**: Disabled by default; requires a Tavily API key in the MCP URL.

#### Memory - MCP Knowledge Graph Server

**Function**: Optional Model Context Protocol memory server.

**Status**: Disabled by default. Box-Agent's built-in memory tools are separate from this MCP server and are controlled by `enable_memory`.

#### Playwright - Browser Automation

**Function**: Browser automation through `@playwright/mcp`.

**Status**: Disabled by default. Run `box-agent install-browser` to install Chromium and flip `mcpServers.playwright.disabled` to `false` in the user MCP config.

**Configuration Example**

```json
{
  "mcpServers": {
    "tavily": {
      "description": "Tavily - Web search and content extraction",
      "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=YOUR_API_KEY",
      "type": "streamable_http",
      "disabled": false
    },
    "playwright": {
      "description": "Playwright - Browser automation (Chromium)",
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"],
      "disabled": false
    }
  }
}
```

## 3. Extended Abilities

### 3.1 Adding Custom Tools

#### Steps

1.  Create a new tool file under `box_agent/tools/`.
2.  Inherit from the `Tool` base class.
3.  Implement the required properties and methods.
4.  Register the tool during Agent initialization.

The runtime dispatches tool calls through `Tool.invoke(arguments)`. This
validates each call's arguments against `parameters` before delegating to the
tool's `execute()` implementation. Tool authors implement `execute()`; runtime
callers should use `invoke()` so they do not bypass argument validation.
Malformed parameter schemas fail closed with `INVALID_TOOL_SCHEMA`; schema and
argument values are omitted from that diagnostic.

Adapters that must invoke a deterministic Tool outside the agent loop, such as
ACP processing a structured attachment before the next model turn, must use
`box_agent.runtime.invoke_tool_with_permissions()`. It preserves the same
schema validation, host permission negotiation, bounded retry, and repeated-
request protection as model-selected tool calls. Calling `Tool.invoke()`
directly is appropriate only when no runtime permission request can occur.

#### Tool Names and Aliases

`Tool.name` is the canonical name serialized in the provider-facing tool
schema. A tool may additionally declare execution-only compatibility names:

```python
class MyTool(Tool):
    aliases = ("legacy_my_tool",)
```

For the canonical name and every declared alias, Box-Agent accepts the exact
name and a generated variant with every underscore replaced by a hyphen. For
example, the declaration above accepts `my_tool`, `my-tool`,
`legacy_my_tool`, and `legacy-my-tool`. This conversion is one-way: a declared
hyphenated name does not generate an underscore variant.

Aliases are resolved only against the tools offered in the current model step
and are converted back to the canonical name before permission checks, loop
guards, deduplication, and execution. Aliases are not added to the provider
schema. Empty, repeated, or conflicting canonical/alias/generated names fail
closed. Deferred MCP tools whose canonical names conflict with this complete
call-name namespace are rejected before activation; other conflicts raise
`ValueError` when the offered tool index is built.

Built-in tools accept these compatibility names from equivalent OpenClaw and
Hermes capabilities:

| Canonical Box-Agent name | Compatibility names |
| --- | --- |
| `read_file` | `read` (OpenClaw) |
| `write_file` | `write` (OpenClaw) |
| `edit_file` | `edit` (OpenClaw) |
| `bash` | `exec` (OpenClaw), `terminal` (Hermes) |
| `generate_image` | `image_generate` (OpenClaw and Hermes) |
| `sub_agent` | `sessions_spawn` (OpenClaw), `delegate_task` (Hermes) |
| `request_user_input` | `clarify` (Hermes) |
| `get_skill` | `skill_view` (Hermes) |

These are name-only compatibility mappings. Calls still use the canonical
Box-Agent parameter schema advertised to the model; aliases do not translate
another agent's argument format. Equivalent tools already named `read_file`,
`write_file`, `search_files`, `execute_code`, or `memory_search` need no
additional alias.

#### Example

```python
# box_agent/tools/my_tool.py
from box_agent.tools.base import Tool, ToolResult
from typing import Dict, Any

class MyTool(Tool):
    @property
    def name(self) -> str:
        """A unique name for the tool."""
        return "my_tool"

    @property
    def description(self) -> str:
        """A description for the LLM to understand the tool's purpose."""
        return "My custom tool for doing something useful"

    @property
    def parameters(self) -> Dict[str, Any]:
        """Parameter schema in JSON Schema format."""
        return {
            "type": "object",
            "properties": {
                "param1": {
                    "type": "string",
                    "description": "First parameter"
                },
                "param2": {
                    "type": "integer",
                    "description": "Second parameter",
                    "default": 10
                }
            },
            "required": ["param1"]
        }

    async def execute(self, param1: str, param2: int = 10) -> ToolResult:
        """
        The main logic of the tool.

        Args:
            param1: The first parameter.
            param2: The second parameter, with a default value.

        Returns:
            A ToolResult object.
        """
        try:
            # Implement your logic here
            result = f"Processed {param1} with param2={param2}"

            return ToolResult(
                success=True,
                content=result
            )
        except Exception as e:
            return ToolResult(
                success=False,
                content=f"Error: {str(e)}"
            )

# In cli.py or agent initialization code
from box_agent.tools.my_tool import MyTool

# Add the new tool when creating the Agent
tools = [
    ReadTool(workspace_dir),
    WriteTool(workspace_dir),
    MyTool(),  # Add your custom tool
]

agent = Agent(
    llm=llm,
    tools=tools,
    max_steps=100
)
```

Durable goals use bounded autopilot in CLI `--task` mode and ACP sessions. If a turn ends while the goal is still `active`, Box-Agent injects an internal continuation until the model calls `goal_write complete`, calls `goal_write block`, the user cancels, the `goal_autopilot_max_turns` / `goal_autopilot_max_seconds` config budget is reached, or `goal_autopilot_no_progress_turns` consecutive automatic continuations make no recorded goal progress.

### 3.2 Adding MCP Tools

Edit `mcp.json` to add a new MCP Server:

```json
{
  "mcpServers": {
    "my_custom_mcp": {
      "description": "My custom MCP server",
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@my-org/my-mcp-server"],
      "env": {
        "API_KEY": "your-api-key"
      },
      "disabled": false,
      "notes": {
        "description": "This is a custom MCP server.",
        "api_key_url": "https://example.com/api-keys"
      }
    }
  }
}
```

### 3.3 Built-in Skills

Built-in skills are committed under `box_agent/skills/` and loaded through `box_agent/skills/_manifest.json`.
No git submodule setup is required for normal development.

The current manifest lists only 12 core built-in skills:

- **System foundations**: `memory-guide`, `browser-use`, `mcp-config`, `scheduled-task`
- **Office core**: `docx`, `pdf`, `xlsx`, `pptx`
- **Core artifacts**: `data-dashboard`
- **Workflow contracts**: `roadmap`, `research-synthesis`
- **Internal dependency**: `html-templates`

`BUILTIN_SKILL_NAMES` in `scripts/generate_skills_manifest.py` is the explicit
allowlist. Other repository skills may remain physically present in the
wheel/runtime during the marketplace migration, but they are omitted from
`_manifest.json` and are not visible to ordinary sessions as built-ins.

Before release, regenerate and commit the manifest if built-in skills change:

```bash
uv run python scripts/generate_skills_manifest.py
```

#### Marketplace Skills

New professional, third-party, and community skills are marketplace skills by
default and must not be added to the built-in allowlist:

1. Put the skill directory under `box_agent/skills/<skill-slug>/`. Keep
   `SKILL.md` frontmatter complete, including `name`, `description`, and
   `author` when the card should show attribution.
2. Do not add the skill name to `BUILTIN_SKILL_NAMES` in
   `scripts/generate_skills_manifest.py`. Only direct host runtime contracts
   and core Office workflows belong in that allowlist.
3. Regenerate the manifest:

   ```bash
   uv run python scripts/generate_skills_manifest.py
   ```

   Verify the script logs `info: excluding '<skill-slug>/SKILL.md'` and that
   `box_agent/skills/_manifest.json` does not list the skill.
4. Publish and install the package through the Skill marketplace. Installed
   skills live under `~/.box-agent/skills/` and are discovered as user skills.

During migration, directories required by existing recommended/expert install
flows remain physically bundled in the runtime. Manifest exclusion controls
built-in discovery only; it does not mean the marketplace package has already
been removed from the ACP artifact.

#### Conversational Skill marketplace installation

An ACP host may enable read-only recommendation and confirmed conversational
installation independently:

```json
{
  "host_capabilities": {
    "skillhub_search": 1,
    "skillhub_install": 1
  }
}
```

`search_skillhub` retains only candidates returned by the host for that session.
The `skillhub_*` names are compatibility identifiers for the product's Skill
marketplace protocol, not the user-facing product name. Direct-source and broad
discovery routing is defined by the shared system prompt; an empty marketplace
result is scoped to that source and cannot terminate other requested discovery.
`install_skillhub_skill` accepts one exact retained `skill_id`, emits a one-shot
ACP permission request, and calls `session/skillhub_install` only after approval.
The host owns authenticated download, integrity checks, conflict handling, and
installation into `~/.box-agent/skills/`. The reverse request contains
`sessionId`, `skillId`, `slug`, `displayName`, `publisherDisplayName`, and the
recommended `version`. It returns
`{"status":"installed","skill":{"name":"<skill-slug>"}}` or
`{"status":"already_installed",...}`. Failures return `status: "failed"` with
an optional bounded `error`; an unavailable host returns `status: "unavailable"`.
After success Box-Agent refreshes the live `SkillLoader`, loads the installed
Skill through `get_skill`, and continues the original task. A model-provided
name, slug, URL, or prose choice is never sufficient authority to select an
installation target.

**More information:**

- [Claude Skills Official Documentation](https://docs.claude.com/zh-CN/docs/agents-and-tools/agent-skills)
- [Anthropic Blog: Equipping agents for the real world](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)

### 3.4 Adding a New Skill

Create a custom Skill:

```bash
# Create a user skill directory
mkdir -p ~/.box-agent/skills/my-custom-skill
cd ~/.box-agent/skills/my-custom-skill

# Create the SKILL.md file
cat > SKILL.md << 'EOF'
---
name: my-custom-skill
description: My custom skill for handling specific tasks.
allowed-tools:
  - read_file
---

# Overview

This skill provides the following capabilities:
- Capability 1
- Capability 2

# Usage

1. Step one...
2. Step two...

# Best Practices

- Practice 1
- Practice 2

# FAQ

Q: Question 1
A: Answer 1
```

The new Skill will be automatically loaded and recognized by the Agent.

`allowed-tools` (or `allowed_tools`) is normalized, deduplicated, and sorted at
load time. It remains routing metadata in the Skill catalog; selecting a Skill
for a sub-agent does not add tools or widen the derived child policy. Callers
must name needed tools explicitly. Use the smallest list the Skill actually
needs. Skill dependencies belong in `required_skills`; `related_skills` are
suggestions and are not loaded automatically. See
[Sub-agent Delegation](SUB_AGENT_DELEGATION.md).

### 3.5 Customizing System Prompt

The system prompt (`system_prompt.md`) defines the Agent's behavior, capabilities, and working guidelines. You can customize it to tailor the Agent for specific use cases.

#### What You Can Customize

1. **Core Capabilities**: Add or modify tool descriptions
2. **Working Guidelines**: Define custom workflows and best practices
3. **Domain-Specific Knowledge**: Add expertise in specific areas
4. **Communication Style**: Adjust how the Agent interacts with users
5. **Task Priorities**: Set preferences for how tasks should be approached

After modifying `system_prompt.md`, be sure to restart the Agent to apply changes

## 4. Troubleshooting

### 4.1 Common Issues

#### API Key Configuration Error

```bash
# Error message
Error: Invalid API key

# Solution
1. Check that the API key in `config.yaml` is correct.
2. Ensure there are no extra spaces or quotes.
3. Verify that the API key has not expired.
```

#### Dependency Installation Failure

```bash
# Error message
uv sync failed

# Solution
1. Update uv to the latest version: `uv self update`
2. Clear the cache: `uv cache clean`
3. Try syncing again: `uv sync`
```

#### MCP Tool Loading Failure

```bash
# Error message
Failed to load MCP server

# Solution
1. Check the configuration in `mcp.json` is correct.
2. Ensure Node.js is installed (required for most MCP tools).
3. Verify that any required API keys are configured.
4. View detailed logs: `pytest tests/test_mcp.py -v -s`
```

### 4.2 Debugging Tips

#### Enable Verbose Logging

```python
# At the beginning of cli.py or a test file
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

#### Using the Python Debugger

```python
# Set a breakpoint in your code
import pdb; pdb.set_trace()

# Or use ipdb for a better experience
import ipdb; ipdb.set_trace()
```

#### Inspecting Tool Calls

```python
# Add logging in the Agent to see tool interactions
logger.debug(f"Tool call: {tool_call.name}")
logger.debug(f"Tool arguments: {tool_call.arguments}")
logger.debug(f"Tool result: {result.content[:200]}")
```
