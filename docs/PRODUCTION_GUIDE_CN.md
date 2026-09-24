# Agent 生产环境指南

> 从 Demo 到生产环境的实践指南

## 目录

- [1. 运行时能力概述](#1-运行时能力概述)
- [2. 可升级方向](#2-可升级方向)
- [3. 生产部署](#3-生产部署)
  - [3.1 独立 Runtime（Electron / 桌面应用）](#31-独立-runtimeelectron--桌面应用)
  - [3.2 容器化部署建议](#32-容器化部署建议)
  - [3.3 资源限制](#33-资源限制)
  - [3.4 Linux 账户权限限制](#34-linux-账户权限限制)

---

## 1. 运行时能力概述

Box-Agent 现在同时提供 Python 包和面向桌面宿主的独立 ACP runtime。本文聚焦部署约束和生产运行时的注意事项。

### 当前实现的能力

| 功能           | 当前实现                                                                                                    |
| -------------- | ----------------------------------------------------------------------------------------------------------- |
| **上下文管理** | ✅ 通过 `MemoryManager` 实现跨会话持久化记忆；分阶段压缩参数/工具结果，并在 token 阈值触发摘要。 |
| **工具调用**   | ✅ 提供了基础的 Read/Write/Edit/Bash 工具。                                                                  |
| **错误处理**   | ✅ 支持 provider 错误人性化提示、重试机制和 ACP 错误传播。                                                   |
| **日志**       | ✅ ACP 诊断输出走 stderr，并支持可选日志文件。                                                               |


## 2. 升级与拓展方向

### 2.1 高级上下文管理

- **引入分布式文件系统**：对上下文进行统一的持久化管理和备份。
- **优化 Token 计算**：使用更精确的方式计算 Token 数量。
- **丰富消息压缩策略**：引入更丰富的消息压缩策略，例如保留最近 N 条消息、保留核心元信息、优化摘要 Prompt，或集成召回系统等。

### 2.2 模型回退机制

当前默认使用单一主模型配置，调用失败时会按 provider 错误分类返回。

- **建立模型池**：配置多个模型账号，建立模型池以提高服务可用性。
- **引入高可用策略**：为模型池引入自动健康检测、故障节点切换、熔断等高可用策略。

### 2.3 模型幻觉的检测与修正

模型输出仍需要结合工具结果、权限策略和宿主侧校验共同约束。

- **输入参数安全检查**：对部分工具的调用参数进行安全性检查，防止执行高危操作。
- **输出结果合理性检查**：对部分工具的调用结果进行反思（Self-reflection），检查其合理性。

## 3. 生产环境部署

### 3.1 独立 Runtime（Electron / 桌面应用）

Electron 或其它桌面宿主应优先使用独立 runtime。它打包 Python 与依赖，通过 stdio 暴露 ACP JSON-RPC。

#### 下载

```bash
# 下载最新已发布 runtime（当前为 v0.8.71）
gh release download --repo Raccoon-Office/Box-Agent \
  --pattern "box-agent-runtime-*.tar.gz"
```

源码树中的 package version 可能高于最新已发布 runtime。嵌入产物前先查看
[发布状态](RELEASE_STATE.md)；除非对应 tag 和 asset 已真实存在，不要用开发版本号
拼接下载地址。

runtime 的 `manifest.json` 同时声明默认 ACP 入口和内置 stdio MCP。Box-Agent
会以 runtime 根目录解析相对 `entry`，并在 CLI 与 ACP 开始 MCP 发现前，将配置
同步到用户目录 `~/.box-agent/config/mcp.json`。宿主仍可直接消费同一声明，但
OfficeV3 不再需要单独实现注册逻辑：

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

启动同步还会注册托管的 `web_search` MCP。首次迁移会启用旧模板中默认禁用的
条目；后续启动会保留用户主动设置的 `disabled` 状态。已存在的 hosted-search URL
继续由宿主或用户拥有，避免 test、pre、production 环境互相覆盖；runtime 相对路径
仍按当前 manifest 刷新。源码安装环境在没有 frozen manifest 时使用
`box-agent-web-extract-mcp` console script。

`web_extract` 的 MCP 声明复用内置工具的显式参数 schema，避免可空联合类型在
部分 Gemini 网关中触发缺少 `type` 的错误。仅 `url` 必填；模型侧应省略不需要的
`model` / `max_output_tokens`，不要传 `null`。提供时分别为字符串和正整数，
不接受未声明的参数。MCP 服务的执行入口仍兼容旧客户端直接传入 `null`。
已打包用户需更新 runtime、重启 MCP/宿主并用原模型复测；源码测试不能替代此步骤。

Windows 专用构建器复用通用构建器的 PyInstaller hidden-import、collection 和
runtime manifest helper，确保 `bin/box-agent-acp.exe` 的 `web_extract`
dispatch 与 `mcp_servers` 声明一致；不要再维护 Windows 独立副本。

Windows 专用脚本 `scripts/build_win_runtime.py` 默认构建精简 ACP；
`--bundled-python-sandbox` 构建内含 Python/Node/PortableGit 的完整包。
`--exe-only` 只更新 `bin/`、manifest 和版本，不安装三件套，因此所选模式必须与
输出目录及 `--install-to` 目标的已有 manifest 一致。完整包增量重建仍需加
`--bundled-python-sandbox`。跨模式、清单缺失或模式无法确定时会在任何构建修改前
报错；需要切换模式时去掉 `--exe-only`，执行一次完整构建，建议使用独立输出目录。

#### 从源码构建

```bash
uv sync --group dev
uv run box-agent-build-runtime

# Apple Silicon 上构建 macOS Intel/x64 runtime：
# 已有 x86_64 .venv-x64（含项目依赖与 PyInstaller）时：
.venv-x64/bin/python -c 'import platform; print(platform.machine())'
uv pip check --python .venv-x64/bin/python
.venv-x64/bin/python -m box_agent.build_runtime_cli --target darwin-x64 --output dist/runtime-darwin-x64
```

构建 runtime 并立即安装到 officev3 `build-resources` 可以合并为一条命令：

```bash
uv run box-agent-build-runtime --version X.Y.Z --install-officev3
```

命令会自动查找常用的 `Dev/frontend/officev3` 目录。如果 officev3 位于其他
位置，传入 `--install-officev3 /path/to/officev3` 或设置
`BOX_AGENT_OFFICEV3_DIR`。

#### macOS 双架构一次构建

在 Box-Agent 项目目录执行（`0.9.13` 为示例 Agent 版本，不修改项目版本文件）：

```bash
# 检查两套 Python / PyInstaller 和输出冲突，不实际构建
uv run box-agent-build-runtime --mac-all --version 0.9.13 --dry-run

# 同一份源码快照，依次构建并校验 ARM + Intel
uv run box-agent-build-runtime --mac-all --version 0.9.13
```

前提是 macOS 上已有两套能运行的独立 Python 环境：默认 ARM 为 `.venv/bin/python`，
Intel 为 `.venv-x64/bin/python`；Apple Silicon 上运行 Intel Python 需要 Rosetta。
两套环境均需安装项目依赖和 PyInstaller，Python 主/次版本及 PyInstaller 版本需一致。
可用 `--arm-python /path/to/arm/python`、`--intel-python /path/to/intel/python` 指定其他环境。
命令不自动创建或修改这些环境；可以分别用 `uv pip check --python <python-path>` 检查依赖。

两边使用一次复制的当前源码（包含 Git 可见的未提交改动），不切换分支、不改开发 runtime。
快照不含本地 `config.yaml`、`mcp.json`、`.env*`、缓存及构建产物。源码必须是 Git checkout，
当前流程要求普通源文件；未展开的子模块或符号链接会明确报错，不能静默漏包。
ARM/Intel 的 PyInstaller 工作目录与缓存隔离。每个包校验 manifest、VERSION、全部 Mach-O
的目标架构及归档内原生文件，全部成功才汇总到本地输出目录；不上传 GitHub/TOS，不安装到宿主。

```text
dist/runtime/
  box-agent-runtime-v0.9.13-darwin-arm64.tar.gz
  box-agent-runtime-v0.9.13-darwin-arm64.tar.gz.sha256
  box-agent-runtime-v0.9.13-darwin-x64.tar.gz
  box-agent-runtime-v0.9.13-darwin-x64.tar.gz.sha256
  box-agent-runtime-v0.9.13-mac.json
```

支持 `--output DIR` / `BOX_AGENT_RUNTIME_OUTPUT`。版本必须显式传入 `--version` 或
`BOX_AGENT_RUNTIME_VERSION`，接受前缀 `v`；不自动沿用可能滞后的 Python 包版本。
不允许覆盖已有同版本产物；需要改内容时升新版本，或先用另一个空输出目录做验证。
任一架构失败都不会把半套产物放到输出目录顶层。临时工作区保留在输出目录内打印的
`.mac-all-v<version>-*` 路径，便于排查，确认后可自行清理。正常退出会释放同版本构建锁；
若被强制结束留下 `.mac-all-v<version>.lock`，先确认没有构建在运行，再处理残留锁。

随后在 OfficeV3 使用新加入的双架构发布入口，显式指定同一个 Agent 版本：

```bash
npm run release:mac -- --version 1.0.37 --agent-version 0.9.13
# 客户端验收后，再上传合并产物
npm run release:mac -- --publish-only ./publish-mac/1.0.37
```

OfficeV3 默认在相邻 `Box-Agent/dist/runtime*` 查找归档；自定义输出时传
`--runtime-dir /absolute/path/to/runtime`。Box-Agent 归档包含 ACP 及其内部依赖，
稳定 Python/Node 仍由宿主管理。这些构建检查不替代客户端打包、安装、权限和实际升级验证。

运行时约束：

| 通道 | 内容 | 规则 |
| ---- | ---- | ---- |
| stdout | ACP JSON-RPC | 只能输出协议数据，不能混入诊断日志 |
| stderr | 日志、工具加载状态、警告 | 可接入宿主日志系统 |
| stdin | ACP JSON-RPC 请求 | 宿主发送 initialize、newSession、prompt、cancel 等请求 |

### 3.2 容器化部署建议

我们推荐使用 Kubernetes 或 Docker 环境来部署 Agent。容器化部署具有以下优势：

- **资源隔离**：每个 Agent 实例运行在独立的容器中，互不干扰。
- **弹性扩展**：根据负载自动调整实例数量。
- **版本管理**：便于快速回滚和灰度发布。
- **环境一致性**：开发、测试、生产环境完全一致。

### 3.3 资源限制

#### 3.3.1 CPU 与内存限制

为防止 Agent 实例占用过多资源而影响宿主机，您必须为其设置 CPU 和内存的限制：

**Docker 配置示例**：
```yaml
# docker-compose.yml
services:
  agent:
    image: agent-demo:latest
    deploy:
      resources:
        limits:
          cpus: '2.0'      # 最多使用 2 个 CPU 核心
          memory: 2G       # 最多使用 2GB 内存
        reservations:
          cpus: '0.5'      # 保证至少 0.5 个核心
          memory: 512M     # 保证至少 512MB
```

#### 3.3.2 Agent 并发与超时限制

除了容器资源限制，还应在 `~/.box-agent/config/config.yaml` 设置运行时限制：

```yaml
max_steps: 300
max_parallel_tools: 8
parallel_tool_timeout_seconds: 900
provider_stale_seconds: 300
sub_agent_token_limit: 50000
sub_agent_batch_synthesis_timeout_seconds: 600
```

这些配置分别控制不同操作：`max_steps` 限制顶层模型迭代，`max_parallel_tools`
限制单步中 `parallel_safe` 调用并发量，`parallel_tool_timeout_seconds` 限制一个
并发批次，`provider_stale_seconds` 限制 Provider 流连续无新数据的等待时间，
`sub_agent_token_limit` 限制子 Agent 摘要前的独立上下文预算，最后一项只
限制传入 `files` 时推导出的无工具综合请求。将最后一项设为 `0` 只会关闭这层额外限制，
不会关闭 provider timeout。批处理策略还包含文件数量与内容硬限制，详见
[子 Agent 委派](SUB_AGENT_DELEGATION_CN.md)。
工具阈值默认值只保存在 `box_agent/config.py`，新生成的用户配置不会显式写入这些值，
因此 runtime 升级可以更新默认值。只有确实需要长期固定的覆盖项才应写入
`tool_limits:`；可通过 `box-agent config --json` 查看当前生效值。未知或非法字段会直接
拒绝加载，避免拼写错误后静默使用另一套默认值。

#### 3.3.3 磁盘限制

Agent 运行过程中可能会产生大量的临时文件和日志，因此需要限制其磁盘使用量：

**Docker Volume 配置**：
```yaml
# docker-compose.yml
services:
  agent:
    volumes:
      - type: tmpfs
        target: /tmp
        tmpfs:
          size: 1G         # 临时文件最多 1GB
      - type: volume
        source: agent-data
        target: /app/data
        volume:
          driver_opts:
            size: 5G       # 数据卷最多 5GB
```


### 3.4 Linux 账户权限限制

#### 3.4.1 最小权限原则

**请勿使用 root 用户运行 Agent**，这会带来严重的安全风险。

**Dockerfile 最佳实践**：
```dockerfile
FROM python:3.11-slim

# 安装必要的系统工具
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:$PATH"

# 创建非特权用户
RUN groupadd -r agent && useradd -r -g agent agent

# 设置工作目录
WORKDIR /app

# 方案1：从 Git 仓库克隆（适用于公开仓库）
RUN git clone https://github.com/Raccoon-Office/Box-Agent.git . && \
    chown -R agent:agent /app

# 方案2：从本地复制代码（适用于私有部署）
# COPY --chown=agent:agent . /app

# 切换到非特权用户后安装依赖
USER agent

# 使用 uv 同步依赖
RUN uv sync

# 启动应用
CMD ["uv", "run", "box-agent"]
```

#### 3.4.2 文件系统权限

您应限制 Agent 只能访问必要的目录：

```bash
# 创建受限的工作目录
mkdir -p /app/workspace
chown agent:agent /app/workspace
chmod 750 /app/workspace  # 所有者读写执行，组只读执行

# 限制敏感目录的访问
chmod 700 /etc/agent      # 配置目录只有所有者能访问
chmod 600 /etc/agent/*.yaml  # 配置文件只有所有者能读写
```
