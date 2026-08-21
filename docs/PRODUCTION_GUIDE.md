# Agent Production Guide

> A Complete Guide from Demo to Production

## Table of Contents

- [1. Runtime Capabilities](#1-runtime-capabilities)
- [2. Upgrade Directions](#2-upgrade-directions)
- [3. Production Deployment](#3-production-deployment)
  - [3.1 Standalone Runtime (Electron / Desktop Apps)](#31-standalone-runtime-electron--desktop-apps)
  - [3.2 (Reserved)](#32-reserved)
  - [3.3 Container Deployment](#33-container-deployment-recommendations)
  - [3.4 Resource Limits](#34-resource-limit-configuration)
  - [3.5 Linux Permissions](#35-linux-account-permission-restrictions)

---

## 1. Runtime Capabilities

Box-Agent now ships as both a Python package and a standalone ACP runtime for desktop hosts. This guide focuses on deployment constraints and operational hardening.

### Implemented Capabilities

| Feature                | Current Implementation                                                                                                |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **Context Management** | ✅ Cross-session memory via MemoryManager; staged argument/tool-result compaction plus token-triggered summaries |
| **Tool Calling**       | ✅ Basic Read/Write/Edit/Bash                                                                                          |
| **Error Handling**     | ✅ Humanized provider errors, retry support, and ACP error propagation                                                 |
| **Logging**            | ✅ Structured ACP diagnostics on stderr and optional log files                                                         |


## 2. Upgrade Directions

### 2.1 Advanced Context Management

- Introduce distributed file systems for unified context persistence management and backup
- Use more precise methods for token counting
- Introduce more strategies for message compression, including keeping the most recent N messages, preserving fixed metadata, prompt optimization for summarization, introducing recall systems, etc.

### 2.2 Model Fallback Mechanism

Currently using a single fixed model, which will directly report errors on failure.

- Introduce a model pool by configuring multiple model accounts to improve availability
- Introduce automatic health checks, failure removal, circuit breaker strategies for the model pool

### 2.3 Model Hallucination Detection and Correction

Currently directly trusts model output without validation mechanism

- Perform security checks on input parameters for certain tool calls to prevent high-risk actions
- Perform reflection on results from certain tool calls to check if they are reasonable

## 3. Production Deployment

### 3.1 Standalone Runtime (Electron / Desktop Apps)

For embedding Box Agent in Electron or other desktop applications, use the standalone runtime binary. It bundles Python and all dependencies — no external Python installation required.

#### Downloading

```bash
# Download the latest published runtime (currently v0.8.71)
gh release download --repo Raccoon-Office/Box-Agent \
  --pattern "box-agent-runtime-*.tar.gz"
```

The package version in the source tree may be newer than the latest published
runtime. Check [Release State](RELEASE_STATE.md) before embedding an artifact;
never construct a download URL from the development version unless that tag and
asset actually exist.

#### Directory Structure

After extraction:
```
box-agent-runtime/
├── manifest.json     # Machine-readable metadata
├── VERSION           # Plain text version string
├── bin/
│   ├── box-agent-acp # Main executable
│   └── _internal/    # Bundled Python runtime + packages
└── runtimes/
    └── node/         # Bundled macOS Node.js runtime for skill scripts
```

`manifest.json` declares both the default ACP entry and bundled stdio MCP
servers. Box-Agent resolves each relative `entry` from the runtime directory
and reconciles it into the user-owned `~/.box-agent/config/mcp.json` before
both CLI and ACP MCP discovery. Hosts may also consume the same declaration
directly, but no OfficeV3-specific registration step is required:

```json
{
  "entry": "bin/box-agent-acp",
  "mcp_servers": {
    "box-agent-web-extract": {
      "entry": "bin/box-agent-acp",
      "args": ["--web-extract-mcp"],
      "transport": "stdio"
    }
  }
}
```

#### Spawning from Host Process

```typescript
// Example: Node.js / Electron
import { spawn } from 'child_process';

const proc = spawn('build-resources/box-agent-runtime/bin/box-agent-acp', [], {
  stdio: ['pipe', 'pipe', 'pipe'],
  env: {
    ...process.env,
    BOX_AGENT_LOG_LEVEL: 'INFO',
    BOX_AGENT_LOG_FILE: '/tmp/box-agent.log',
  },
});

// stdin/stdout: ACP JSON-RPC protocol (pure, no stray bytes)
// stderr: diagnostic logs (safe to pipe to host logger)
```

The bootstrap also registers the hosted `web_search` MCP. On first migration it
enables the previously disabled template entries; later starts preserve an
explicit user `disabled` choice while refreshing managed endpoints and runtime
paths. Source installs use the `box-agent-web-extract-mcp` console script when
no frozen manifest is available.

#### Key Constraints

| Channel | Content | Rule |
|---------|---------|------|
| **stdout** | ACP JSON-RPC only | Zero diagnostic output. Any stray byte breaks the protocol. |
| **stderr** | Logs, tool loading status, warnings | Safe to capture for debugging. |
| **stdin** | ACP JSON-RPC requests | Host sends `initialize`, `newSession`, `prompt`, `cancel`. |

#### Debug Logging

Control via environment variables:

| Variable | Values | Default |
|----------|--------|---------|
| `BOX_AGENT_LOG_LEVEL` | `DEBUG`, `INFO`, `WARN`, `ERROR` | `INFO` |
| `BOX_AGENT_LOG_FILE` | File path | *(stderr only)* |
| `BOX_AGENT_LOG_FORMAT` | `text`, `json` | `text` |

#### Building from Source

```bash
uv sync --group dev
uv run box-agent-build-runtime --version X.Y.Z
```

Produces `dist/runtime/box-agent-runtime-v{version}-{platform}-{arch}.tar.gz`.

To build the archive and install it into officev3 `build-resources` in one
command:

```bash
uv run box-agent-build-runtime --version X.Y.Z --install-officev3
```

The command auto-detects the usual `Dev/frontend/officev3` checkout. Use
`--install-officev3 /path/to/officev3` or set `BOX_AGENT_OFFICEV3_DIR` for a
different layout.

Supported platforms: `darwin-arm64`, `darwin-x64`, `linux-x64`, `linux-arm64`, `win32-x64`.

Build the current machine architecture:

```bash
uv run box-agent-build-runtime
```

Build macOS Intel/x64 from an Apple Silicon Mac:

```bash
UV_PROJECT_ENVIRONMENT=.venv-x64 arch -x86_64 ~/.local/bin-x64/uv run box-agent-build-runtime --arch x64
```

The long form is also supported:

```bash
UV_PROJECT_ENVIRONMENT=.venv-x64 arch -x86_64 ~/.local/bin-x64/uv run box-agent-build-runtime --target darwin-x64
```

Optional environment defaults:

```bash
BOX_AGENT_RUNTIME_VERSION=X.Y.Z uv run box-agent-build-runtime
BOX_AGENT_RUNTIME_OUTPUT=dist/runtime uv run box-agent-build-runtime
BOX_AGENT_RUNTIME_TARGET=darwin-x64 arch -x86_64 uv run box-agent-build-runtime
```

The older direct script entry remains available for compatibility:

```bash
uv run python scripts/build_runtime.py --target darwin-arm64
```

macOS runtime artifacts additionally bundle a pinned Node.js runtime under
`box-agent-runtime/runtimes/node/`. The Node manifest uses relative paths so the
runtime directory remains relocatable after extraction. Runtime npm state
(`npm_config_cache`, `npm_config_prefix`, and `NODE_PATH`) is still kept under
the user's `~/.box-agent/runtimes/node/sandbox/` directory.

### 3.3 Container Deployment Recommendations

We recommend using K8s/Docker environments for Agent deployment. Containerized deployment has the following advantages:

- **Resource Isolation**: Each Agent instance runs in an independent container without interference
- **Elastic Scaling**: Automatically adjust instance count based on load
- **Version Management**: Easy rollback and canary releases
- **Environment Consistency**: Development, testing, and production environments are completely consistent

### 3.4 Resource Limit Configuration

#### 3.4.1 CPU and Memory Limits

To prevent the Agent from consuming excessive CPU/Memory resources and affecting the host, CPU and memory limits must be set:

**Docker Configuration Example**:
```yaml
# docker-compose.yml
services:
  agent:
    image: agent-demo:latest
    deploy:
      resources:
        limits:
          cpus: '2.0'      # Maximum 2 CPU cores
          memory: 2G       # Maximum 2GB memory
        reservations:
          cpus: '0.5'      # Guarantee at least 0.5 cores
          memory: 512M     # Guarantee at least 512MB
```

#### 3.4.2 Agent concurrency and timeout limits

Set runtime-level limits in `~/.box-agent/config/config.yaml` as well as
container limits:

```yaml
max_steps: 300
max_parallel_tools: 8
parallel_tool_timeout_seconds: 900
sub_agent_token_limit: 50000
sub_agent_batch_synthesis_timeout_seconds: 600
```

These limits control different operations: `max_steps` bounds top-level model
iterations, `max_parallel_tools` caps `parallel_safe` calls in one step,
`parallel_tool_timeout_seconds` caps one such parallel batch,
`sub_agent_token_limit` bounds each child context before summarization, and the
batch synthesis setting caps only the tool-free request inferred from `files`.
Setting the last value to `0` disables that extra cap, not the provider timeout.
The inferred batch path also enforces file/count/content limits; see
[Sub-agent Delegation](SUB_AGENT_DELEGATION.md).
Tool-limit defaults live only in `box_agent/config.py` and are intentionally
absent from newly generated user configs, so runtime upgrades can update them.
Add only deliberate overrides under `tool_limits:`; inspect effective values
with `box-agent config --json`. Unknown or invalid keys fail configuration
loading instead of silently using a different default.

#### 3.4.3 Disk Limits

Agents may generate large amounts of temporary files and log files, so disk usage needs to be limited:

**Docker Volume Configuration**:
```yaml
# docker-compose.yml
services:
  agent:
    volumes:
      - type: tmpfs
        target: /tmp
        tmpfs:
          size: 1G         # Maximum 1GB for temporary files
      - type: volume
        source: agent-data
        target: /app/data
        volume:
          driver_opts:
            size: 5G       # Maximum 5GB for data volume
```


### 3.5 Linux Account Permission Restrictions

#### 3.5.1 Principle of Least Privilege

**Never run the Agent as root user**, as this poses serious security risks.

**Dockerfile Best Practices**:
```dockerfile
FROM python:3.11-slim

# Install necessary system tools
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:$PATH"

# Create non-privileged user
RUN groupadd -r agent && useradd -r -g agent agent

# Set working directory
WORKDIR /app

# Option 1: Clone from Git repository (for public repos)
RUN git clone https://github.com/Raccoon-Office/Box-Agent.git . && \
    chown -R agent:agent /app

# Option 2: Copy code from local (for private deployments)
# COPY --chown=agent:agent . /app

# Switch to non-privileged user before installing dependencies
USER agent

# Sync dependencies using uv
RUN uv sync

# Start the application
CMD ["uv", "run", "box-agent"]
```

#### 3.5.2 File System Permissions

Restrict the Agent to only access necessary directories:

```bash
# Create restricted workspace directory
mkdir -p /app/workspace
chown agent:agent /app/workspace
chmod 750 /app/workspace  # Owner: read/write/execute, Group: read/execute

# Restrict access to sensitive directories
chmod 700 /etc/agent      # Config directory only accessible by owner
chmod 600 /etc/agent/*.yaml  # Config files only readable/writable by owner
```
