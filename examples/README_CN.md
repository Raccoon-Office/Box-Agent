# Box Agent Examples

这个目录包含了一系列渐进式的示例，帮助你理解如何使用 Box Agent 框架。

## 📚 示例列表

### 01_basic_tools.py - 基础工具使用

**难度**: ⭐ 入门

**内容**:
- 如何直接使用 ReadTool、WriteTool、EditTool、BashTool
- 不涉及 Agent 或 LLM，纯粹的工具调用演示
- 适合理解每个工具的基本功能

**运行**:
```bash
python examples/01_basic_tools.py
```

**学习要点**:
- 工具的输入参数格式
- ToolResult 的返回结构
- 错误处理方式

---

### 02_simple_agent.py - 简单 Agent 使用

**难度**: ⭐⭐ 初级

**内容**:
- 创建最简单的 Agent
- 让 Agent 执行文件创建任务
- 让 Agent 执行 bash 命令任务
- 理解 Agent 的执行流程

**运行**:
```bash
# 需要先配置 API key
python examples/02_simple_agent.py
```

**学习要点**:
- Agent 的初始化流程
- 如何给 Agent 下达任务
- Agent 如何自主选择工具
- 任务完成的判断标准

**前置要求**:
- 已配置 `box_agent/config/config.yaml` 中的 API key

---

### 03_session_notes.py - Session Note 工具

**难度**: ⭐⭐⭐ 中级

**内容**:
- 直接使用 Session Note 工具（record_note, recall_notes）
- Agent 使用 Session Note 保持跨会话记忆
- 演示两个 Agent 实例如何共享记忆

**运行**:
```bash
python examples/03_session_notes.py
```

**学习要点**:
- Session Note 的工作原理
- 笔记的分类管理（category）
- 如何在 system prompt 中引导 Agent 使用笔记
- 跨会话记忆的实现方式

**亮点**:
这是本项目的核心特性之一！展示了一种轻量但有效的会话记忆管理方案。

---

### 04_full_agent.py - 完整功能 Agent

**难度**: ⭐⭐⭐⭐ 高级

**内容**:
- 包含所有功能的完整 Agent 设置
- 集成基础工具 + Session Notes + MCP 工具
- 复杂任务的完整执行流程
- 多轮对话示例

**运行**:
```bash
python examples/04_full_agent.py
```

**学习要点**:
- 如何组合多种工具
- MCP 工具的加载和使用
- 复杂任务的分解和执行
- 生产环境的 Agent 配置

**前置要求**:
- 已配置 API key
- （可选）配置了 MCP 工具

---

## 🚀 快速开始

### 1. 配置 API Key

```bash
# 复制配置模板
cp box_agent/config/config-example.yaml box_agent/config/config.yaml

# 编辑配置文件，填入你的 MiniMax API Key
vim box_agent/config/config.yaml
```

### 2. 运行第一个示例

```bash
# 不需要 API key 的示例
python examples/01_basic_tools.py

# 需要 API key 的示例
python examples/02_simple_agent.py
```

### 3. 逐步学习

建议按照编号顺序学习：
1. **01_basic_tools.py** - 理解工具
2. **02_simple_agent.py** - 理解 Agent
3. **03_session_notes.py** - 理解记忆管理
4. **04_full_agent.py** - 理解完整系统

---

## 📖 与测试用例的对应关系

这些示例都是基于 `tests/` 目录中的测试用例提炼而来：

| Example             | Based on Test                                        | Description           |
| ------------------- | ---------------------------------------------------- | --------------------- |
| 01_basic_tools.py   | tests/test_tools.py                                  | 基础工具单元测试      |
| 02_simple_agent.py  | tests/test_agent.py                                  | Agent 基本功能测试    |
| 03_session_notes.py | tests/test_note_tool.py<br>tests/test_integration.py | Session Note 工具测试 |
| 04_full_agent.py    | tests/test_integration.py                            | 完整集成测试          |

---

## 💡 学习路径建议

### 路径 1: 快速上手
1. 运行 `01_basic_tools.py` - 了解工具
2. 运行 `02_simple_agent.py` - 运行第一个 Agent
3. 直接使用 `box-agent` 进入交互模式

### 路径 2: 深入理解
1. 阅读并运行所有示例 (01 → 04)
2. 阅读对应的测试用例 (`tests/`)
3. 阅读核心实现代码 (`box_agent/`)
4. 尝试修改示例，实现自己的功能

### 路径 3: 生产应用
1. 理解所有示例
2. 阅读 [生产环境部署指南](../docs/PRODUCTION_GUIDE.md)
3. 配置 MCP 工具和 Skills
4. 根据需求扩展工具集

---

## 🔧 故障排除

### API Key 错误
```
❌ API key not configured in config.yaml
```
**解决**: 确保在 `box_agent/config/config.yaml` 中配置了有效的 MiniMax API Key

### 找不到 config.yaml
```
❌ config.yaml not found
```
**解决**:
```bash
cp box_agent/config/config-example.yaml box_agent/config/config.yaml
```

### MCP 工具加载失败
```
⚠️ MCP tools not loaded: [error message]
```
**解决**: MCP 工具是可选的，不影响基本功能。如需使用，请参考主 README 中的 MCP 配置章节。

---

## 📚 更多资源

- [项目主 README](../README.md) - 完整项目文档
- [测试用例](../tests/) - 更多使用示例
- [核心实现](../box_agent/) - 源代码
- [生产环境指南](../docs/PRODUCTION_GUIDE.md) - 部署指南

---

## 🤝 贡献示例

如果你有好的使用示例，欢迎提交 PR！

建议的新示例方向：
- Web 搜索集成示例（使用 MiniMax Search MCP）
- Skills 使用示例（文档处理、设计等）
- 自定义工具开发示例
- 错误处理和重试机制示例

---

**⭐ 如果这些示例对你有帮助，欢迎给项目一个 Star！**
