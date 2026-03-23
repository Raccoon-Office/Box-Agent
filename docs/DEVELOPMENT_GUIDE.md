# Development Guide

## Table of Contents

- [Development Guide](#development-guide)
  - [Table of Contents](#table-of-contents)
  - [1. Project Architecture](#1-project-architecture)
  - [2. Basic Usage](#2-basic-usage)
    - [2.1 Interactive Commands](#21-interactive-commands)
    - [2.2 Integrated MCP Tools](#22-integrated-mcp-tools)
      - [Memory - Knowledge Graph Memory System](#memory---knowledge-graph-memory-system)
      - [Web Search - Web Search and Browse](#web-search---web-search-and-browse)
  - [3. Extended Abilities](#3-extended-abilities)
    - [3.1 Adding Custom Tools](#31-adding-custom-tools)
      - [Steps](#steps)
      - [Example](#example)
    - [3.2 Adding MCP Tools](#32-adding-mcp-tools)
    - [3.3 Customizing Note Storage](#33-customizing-note-storage)
    - [3.4 Initialize Claude Skills (Recommended)](#34-initialize-claude-skills-recommended)
    - [3.5 Adding a New Skill](#35-adding-a-new-skill)
    - [3.6 Customizing System Prompt](#36-customizing-system-prompt)
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

```
box-agent/
├── box_agent/              # Core source code
│   ├── agent.py             # Main agent loop
│   ├── llm.py               # LLM client
│   ├── cli.py               # Command-line interface
│   ├── config.py            # Configuration loading
│   ├── tools/               # Tool implementations (file, bash, MCP, skills, etc.)
│   └── skills/              # Claude Skills (submodule)
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
| `/history`             | Show the current session message count                      |
| `/stats`               | Display session statistics (steps, tool calls, tokens used) |

### 2.2 Integrated MCP Tools

This project comes with pre-configured MCP (Model Context Protocol) tools that extend the agent's capabilities:

#### Memory - Knowledge Graph Memory System

**Function**: Provides long-term memory storage and retrieval based on graph database

**Status**: Enabled by default (`disabled: false`)

**Configuration**: No API Key required, works out of the box

**Capabilities**:
- Store and retrieve information across sessions
- Build knowledge graphs from conversations
- Semantic search through stored memories

---

#### Web Search - Web Search and Browse

**Function**: Provides three powerful tools:
- `search` - Web search capability
- `parallel_search` - Execute multiple searches simultaneously
- `browse` - Intelligent web browsing and content extraction

**Status**: Disabled by default, needs configuration to enable

**Configuration Example**

```json
{
  "mcpServers": {
    "web_search": {
      "disabled": false,
      "env": {
        "JINA_API_KEY": "your-jina-api-key",
        "SERPER_API_KEY": "your-serper-api-key"
      }
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
    max_steps=50
)
```

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

### 3.3 Customizing Note Storage

To replace the storage backend for the `SessionNoteTool`:

```python
# Current implementation: JSON file
class SessionNoteTool:
    def __init__(self, memory_file: str = "./workspace/.agent_memory.json"):
        self.memory_file = Path(memory_file)
    
    async def _save_notes(self, notes: List[Dict]):
        with open(self.memory_file, 'w') as f:
            json.dump(notes, f, indent=2, ensure_ascii=False)

# Example extension: PostgreSQL
class PostgresNoteTool(Tool):
    def __init__(self, db_url: str):
        self.db = PostgresDB(db_url)
    
    async def _save_notes(self, notes: List[Dict]):
        await self.db.execute(
            "INSERT INTO notes (content, category, timestamp) VALUES ($1, $2, $3)",
            notes
        )

# Example extension: Vector Database
class MilvusNoteTool(Tool):
    def __init__(self, milvus_host: str):
        self.vector_db = MilvusClient(host=milvus_host)
    
    async def _save_notes(self, notes: List[Dict]):
        # Generate embeddings
        embeddings = await self.get_embeddings([n["content"] for n in notes])
        
        # Store in the vector database
        await self.vector_db.insert(
            collection="agent_notes",
            data=notes,
            embeddings=embeddings
        )
```

### 3.4 Initialize Claude Skills (Recommended) 

This project integrates Claude's official skills repository via git submodule. Initialize it after first clone:

```bash
# Initialize submodule
git submodule update --init --recursive
```

Skills provide 20+ professional capabilities, making the Agent work like a professional:

- 📄 **Document Processing**: Create and edit PDF, DOCX, XLSX, PPTX
- 🎨 **Design Creation**: Generate artwork, posters, GIF animations
- 🧪 **Development & Testing**: Web automation testing (Playwright), MCP server development
- 🏢 **Enterprise Applications**: Internal communication, brand guidelines, theme customization

✨ **This is one of the core highlights of this project.** For details, see the "Configure Skills" section below.

**More information:**

- [Claude Skills Official Documentation](https://docs.claude.com/zh-CN/docs/agents-and-tools/agent-skills)
- [Anthropic Blog: Equipping agents for the real world](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)

### 3.5 Adding a New Skill

Create a custom Skill:

```bash
# Create a new skill directory under skills/
mkdir skills/my-custom-skill
cd skills/my-custom-skill

# Create the SKILL.md file
cat > SKILL.md << 'EOF'
---
name: my-custom-skill
description: My custom skill for handling specific tasks.
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

### 3.6 Customizing System Prompt

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
