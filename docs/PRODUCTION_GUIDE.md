# Agent Production Guide

> A Complete Guide from Demo to Production

## Table of Contents

- [1. Demo Features](#1-demo-features)
- [2. Upgrade Directions](#2-upgrade-directions)
- [3. Production Deployment](#3-production-deployment)

---

## 1. Demo Features

This project is a **teaching-level demo** that demonstrates the core concepts and execution flow of an Agent. To reach production level, many complex issues still need to be addressed.

### What We've Implemented (Demo Level)

| Feature                | Demo Implementation                                                                                                   |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **Context Management** | ✅ Simple persistence via SessionNoteTool with file storage; basic summarization when approaching context window limit |
| **Tool Calling**       | ✅ Basic Read/Write/Edit/Bash                                                                                          |
| **Error Handling**     | ✅ Basic exception catching                                                                                            |
| **Logging**            | ✅ Simple print output                                                                                                 |


## 2. Upgrade Directions

### 2.1 Advanced Context Management

- Introduce distributed file systems for unified context persistence management and backup
- Use more precise methods for token counting
- Introduce more strategies for message compression, including keeping the most recent N messages, preserving fixed metadata, prompt optimization for summarization, introducing recall systems, etc.

### 2.2 Model Fallback Mechanism

Currently using a single fixed model (MiniMax-M2.5), which will directly report errors on failure.

- Introduce a model pool by configuring multiple model accounts to improve availability
- Introduce automatic health checks, failure removal, circuit breaker strategies for the model pool

### 2.3 Model Hallucination Detection and Correction

Currently directly trusts model output without validation mechanism

- Perform security checks on input parameters for certain tool calls to prevent high-risk actions
- Perform reflection on results from certain tool calls to check if they are reasonable

## 3. Production Deployment

### 3.1 Container Deployment Recommendations

We recommend using K8s/Docker environments for Agent deployment. Containerized deployment has the following advantages:

- **Resource Isolation**: Each Agent instance runs in an independent container without interference
- **Elastic Scaling**: Automatically adjust instance count based on load
- **Version Management**: Easy rollback and canary releases
- **Environment Consistency**: Development, testing, and production environments are completely consistent

### 3.2 Resource Limit Configuration

#### 3.2.1 CPU and Memory Limits

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

#### 3.2.2 Disk Limits

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


### 3.3 Linux Account Permission Restrictions

#### 3.3.1 Principle of Least Privilege

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
RUN git clone https://github.com/MiniMax-AI/agent-demo.git . && \
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

#### 3.3.2 File System Permissions

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

