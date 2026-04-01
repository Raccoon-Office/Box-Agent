# Agent 生产环境指南

> 从 Demo 到生产环境的实践指南

## 目录

- [1. Demo功能实现](#1-demo功能)
- [2. 可升级方向](#2-可升级方向)
- [3. 生产部署](#3-生产部署)

---

## 1. Demo 功能概述

本项目是一个**教学级 Demo**，旨在演示 Agent 的核心概念与基本执行流程。要在生产环境中使用，还需要解决一系列复杂问题。

### Demo 实现的功能

| 功能           | Demo 实现                                                                                                   |
| -------------- | ----------------------------------------------------------------------------------------------------------- |
| **上下文管理** | ✅ 通过 `MemoryManager` 实现跨会话持久化记忆，支持自动会话摘要；两层上下文压缩机制。 |
| **工具调用**   | ✅ 提供了基础的 Read/Write/Edit/Bash 工具。                                                                  |
| **错误处理**   | ✅ 实现了基础的异常捕获机制。                                                                                |
| **日志**       | ✅ 使用简单的 `print` 函数输出日志。                                                                         |


## 2. 升级与拓展方向

### 2.1 高级上下文管理

- **引入分布式文件系统**：对上下文进行统一的持久化管理和备份。
- **优化 Token 计算**：使用更精确的方式计算 Token 数量。
- **丰富消息压缩策略**：引入更丰富的消息压缩策略，例如保留最近 N 条消息、保留核心元信息、优化摘要 Prompt，或集成召回系统等。

### 2.2 模型回退机制

当前 Demo 固定使用单一模型，调用失败时会直接报错。

- **建立模型池**：配置多个模型账号，建立模型池以提高服务可用性。
- **引入高可用策略**：为模型池引入自动健康检测、故障节点切换、熔断等高可用策略。

### 2.3 模型幻觉的检测与修正

当前 Demo 完全信任模型输出，缺乏验证机制。

- **输入参数安全检查**：对部分工具的调用参数进行安全性检查，防止执行高危操作。
- **输出结果合理性检查**：对部分工具的调用结果进行反思（Self-reflection），检查其合理性。

## 3. 生产环境部署

### 3.1 容器化部署建议

我们推荐使用 Kubernetes 或 Docker 环境来部署 Agent。容器化部署具有以下优势：

- **资源隔离**：每个 Agent 实例运行在独立的容器中，互不干扰。
- **弹性扩展**：根据负载自动调整实例数量。
- **版本管理**：便于快速回滚和灰度发布。
- **环境一致性**：开发、测试、生产环境完全一致。

### 3.2 资源限制

#### 3.2.1 CPU 与内存限制

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

#### 3.2.2 磁盘限制

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


### 3.3 Linux 账户权限限制

#### 3.3.1 最小权限原则

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

#### 3.3.2 文件系统权限

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