# 贡献指南

感谢你对 Box Agent 项目的兴趣！我们欢迎各种形式的贡献。

## 如何贡献

### 报告 Bug

如果你发现了 bug，请创建一个 Issue 并包含以下信息：

- **问题描述**：清晰描述问题
- **复现步骤**：详细的复现步骤
- **预期行为**：你期望发生什么
- **实际行为**：实际发生了什么
- **环境信息**：
  - Python 版本
  - 操作系统
  - 相关依赖版本

### 提出新功能

如果你有新功能的想法，请先创建一个 Issue 讨论：

- 描述功能的用途和价值
- 说明预期的使用场景
- 如果可能，提供设计思路

### 提交代码

#### 准备工作

1. Fork 本仓库
2. 克隆你的 fork：
   ```bash
   git clone https://github.com/Raccoon-Office/Box-Agent box-agent
   cd box-agent
   ```

3. 创建新分支：
   ```bash
   git checkout -b feature/your-feature-name
   # 或
   git checkout -b fix/your-bug-fix
   ```

4. 安装开发依赖：
   ```bash
   uv sync
   ```

5. 运行一次快速本地检查：
   ```bash
   uv run pytest tests/ -q
   ```

6. 需要手动冒烟测试时，启动开发版 CLI：
   ```bash
   uv run python -m box_agent.cli
   ```

#### 团队协作基线

- 优先提交小 PR，一次只修改一个行为或一个子系统。
- 遵循[分层架构与所有权规则](docs/ARCHITECTURE_CN.md)。共享行为不自动属于 `core.py`：优先放在公共契约后的能力/策略模块中，CLI/ACP 保持为适配层。
- 修改代码路径前，如果 `.understand-anything/` 可用，应先用它做代码导航，再用源码阅读、`rg`、测试、日志或运行探针验证。范围和刷新步骤见[代码图谱指南](docs/UNDERSTAND_ANYTHING_CN.md)。
- 将 Understand Anything 的共享刷新基线与配置纳入 Git（`.understand-anything/` 下的 `knowledge-graph.json`、`meta.json`、`fingerprints.json`、`.understandignore` 和 `config.json`）。架构边界或阅读路线变化时，应一起重新生成并审查图谱、元数据和 fingerprint。不要提交 `last-run-summary.json`、intermediate、trash、dashboard token 或 cache 文件。
- 不要提交本地凭据或用户配置。`config.yaml`、`mcp.json`、日志和 `workspace/` 都属于本地运行文件。
- 如果改动会影响 officev3 或任何 packaged runtime，需要说明本次只验证了源码行为，还是也完成了 runtime rebuild/install/probe。

#### 维护设计与变更记录

[设计索引](docs/design/README.md)和
[变更历史索引](docs/changes/README.md)是供贡献者、维护者和 Reviewer 共同使用的项目记录。
它们应帮助读者理解当前设计及其形成过程中的重要决定，但不需要记录每个实现细节或每次提交。

当改动新增、调整、重命名或废弃重要子系统、归属边界、公开或宿主协议、兼容性规则，
或者作为事实来源的设计文档时，应更新 `docs/design/README.md`。在对应路由行中补充或调整
容易识别的领域或路径、应优先阅读的文档、需要保持的当前边界，以及主要验证依据。
同时存在中英文文档时，应保持两者一致。若内部重构或 Bug 修复没有改变已记录的设计，
通常不需要更新该索引。

当改动对公开或宿主协议、稳定内核或工具契约、安全边界、兼容性默认值、迁移、
发布/runtime 预期、回滚方式或跨仓库依赖具有长期影响时，应更新
`docs/changes/README.md`。详细条目应记录当前可用的变更或 PR 引用、持久影响、
兼容性或迁移影响、验证依据、残余缺口及回滚方向。改动仍在 Review 阶段时，不要虚构未来的
merge SHA；先记录 PR 和已有实现提交，合并后再核对补充。

新增或修改详细历史条目时，如果当前有效决定或它与早期条目的关系发生变化，也应同步更新
快速路由表。使用稳定且便于检索的模块、路径、协议和配置名称，同时保持摘要便于阅读。
应明确说明新条目是替代、补强、回滚早期决定，还是需要与早期决定联读，不要静默覆盖历史。

如果一个 PR 既改变设计边界，又形成了长期有效的兼容性、迁移、安全、发布或回滚决定，
应同时更新两个索引。普通实现说明、不产生长期契约影响的提交，以及已经被索引准确覆盖的改动，
不需要单独增加条目。

#### 归属边界

- `box_agent/core.py` 是可修改但低频变化、由核心团队维护的内核。产品层与能力层必须使用 `Agent.run_events()` 或明确的共享 API，不能直接导入 Core 实现。
- Agent 循环不变量、事件语义、调度、取消、工具调用闭合和安全执行点属于稳定内核/契约；Core 改动需要核心维护者评审。
- 可复用的工具、Skills、Provider 和存储默认属于能力层，除非确实需要新增与宿主无关的内核契约。有状态的领域工作流应放在自包含的 Skill 或插件中；不得在 Session Log 之外新增第二个持久状态源。
- CLI 代码负责终端交互、渲染、slash commands 和本地提示，不应复制 ACP 也需要的核心行为。
- ACP 代码负责把共享事件翻译成 ACP protocol updates 和 host extension methods。stdout 必须保持协议纯净；诊断信息应走 stderr 或结构化日志。
- Provider 特定的 wire 行为属于 `box_agent/llm/`，不要把 provider 假设散落到 tools、skills、CLI 或 ACP。
- Tool 行为属于 `box_agent/tools/`，应返回结构化 `ToolResult`。新增工具语义需要直接回归测试。
- 内置 skill 加载由 `box_agent/skill_loader.py`、`box_agent/skills/` 和 `box_agent/skills/_manifest.json` 控制。内置 skills 变化时，review 前必须重新生成 manifest。
- PPT/文档能力默认由 Skill 驱动，除非有明确且与宿主无关的核心 contract 变化。意图路由、进度推导、校验和恢复说明属于对应 Skill 或插件；不要向核心循环、适配器或通用工具加入隐藏的格式专用生命周期。跨能力共用的 fail-closed Tool 安全约束仍属于共享 Tool contract。
- Packaged runtime 行为不能只靠源码改动证明。如果 officev3 或 standalone runtime 依赖本次改动，需要说明 runtime rebuild/install/probe 状态。

#### TPR Pull Request 标准

每个非平凡 PR 都要能通过 TPR 被审查：

- **Task**：改了什么行为、为什么改、影响哪些入口、哪些内容明确不在本次范围内。
- **Proof**：具体命令、测试、探针、截图、日志、重新生成的 manifest 或 runtime 验证。
- **Risk**：兼容性、打包/runtime 影响、迁移、配置/密钥、回滚方案和跨仓库后续事项。

PR 还必须说明受影响的架构层，并记录是否检查了 merge base 之后目标分支的相关变化。
完整门禁与严重级别定义见 [PR 审查规范](docs/PR_REVIEW_STANDARD_CN.md)。

#### 开发流程

1. **编写代码**
   - 遵循项目的代码风格（参考 [开发指南](docs/DEVELOPMENT_GUIDE_CN.md)）
   - 添加必要的注释和文档字符串
   - 保持代码简洁清晰

2. **添加测试**
   - 为新功能添加测试用例
   - 确保所有测试通过：
     ```bash
     pytest tests/ -v
     ```

3. **更新文档**
   - 如果添加了新功能，更新 README 或相关文档
   - 保持文档与代码同步

4. **提交更改**
   - 使用清晰的提交消息：
     ```bash
     git commit -m "feat(tools): 添加新的文件搜索工具"
     # 或
     git commit -m "fix(agent): 修复工具调用错误处理"
     ```
   
   - 提交消息格式：
     - `feat`: 新功能
     - `fix`: Bug 修复
     - `docs`: 文档更新
     - `style`: 代码格式调整
     - `refactor`: 代码重构
     - `test`: 测试相关
     - `chore`: 构建或辅助工具

5. **Rebase 到最新 `main`**
   - 创建或更新 Pull Request 前，必须将当前分支 rebase 到基础仓库最新的
     `main`；不要把 `main` merge 进功能分支。
   - 通过 Fork 贡献时，先将基础仓库添加为 `upstream`（只需执行一次），再进行
     rebase：
     ```bash
     git remote add upstream https://github.com/Raccoon-Office/Box-Agent.git  # 仅首次需要
     git fetch upstream main
     git rebase upstream/main
     ```
   - 直接协作者可以改用 `origin/main`。如果分支已经推送，改写共享分支前需要先
     协调，并使用 `git push --force-with-lease` 更新；禁止使用 `--force`。

6. **推送到你的 fork**
   ```bash
   git push origin feature/your-feature-name
   ```

7. **创建 Pull Request**
   - 在 GitHub 上创建 Pull Request
   - 清楚描述你的更改
   - 引用相关的 Issue（如果有）

#### Pull Request 检查清单

在提交 PR 之前，请确保：

- [ ] PR 描述包含 Task、Proof、Risk。
- [ ] 代码遵循项目规范和现有架构。
- [ ] 针对本次行为改动运行了聚焦测试。
- [ ] 改动共享核心、工具、MCP、memory、CLI、ACP、skills 或打包行为时，运行了更广的验证。
- [ ] 添加了必要的回归测试，或明确说明未添加的原因。
- [ ] 更新了相关文档。
- [ ] 已评估设计与变更历史索引，并在设计边界或长期有效决定受到影响时完成更新。
- [ ] 内置 skills 发生变化时，重新生成了 manifest。
- [ ] 影响 packaged runtime 行为时，说明了 runtime rebuild/install/probe 状态。
- [ ] 没有包含不相关改动、本地配置、日志、workspace 文件或 Understand Anything 生成图谱/cache。
- [ ] 提交消息清晰，并遵循本仓库现有 conventional 风格。
- [ ] 创建或更新 PR 前，当前分支已 rebase 到基础仓库最新的 `main`，且没有把 `main` merge 进功能分支。

### 代码审查

所有 Pull Request 需要经过代码审查。维护者使用详细的
[维护者 Review 指南](docs/REVIEW_GUIDE_CN.md) 来确定 review 顺序、阻塞项和
proof 要求：

- 完整 PR 门禁、严重级别和 verdict 契约见
  [PR 审查规范](docs/PR_REVIEW_STANDARD_CN.md)。

- 审查从 TPR 证据开始。缺少 proof 视为工作未完成，而不是让 reviewer 代为确认。
- Reviewer 应先检查行为、归属边界、测试、打包/runtime 影响和文档，再看代码风格细节。
- 对共享行为，Reviewer 需要确认改动没有在 CLI 和 ACP 中各自实现一份，除非有明确的入口特异性原因。
- 对 skill 改动，Reviewer 需要检查 `box_agent/skills/_manifest.json` 以及是否影响 officev3 推荐卡片。
- 对 packaged runtime 改动，Reviewer 需要判断源码测试是否足够，还是必须 rebuild/install/probe runtime。
- 审查通过后会合并到主分支。

## 代码规范

### Python 代码风格

遵循 PEP 8 和 Google Python Style Guide：

```python
# 好的示例 ✅
class MyClass:
    """类的简短描述。
    
    详细描述...
    """
    
    def my_method(self, param1: str, param2: int = 10) -> str:
        """方法的简短描述。
        
        Args:
            param1: 参数1的描述
            param2: 参数2的描述
        
        Returns:
            返回值的描述
        """
        pass

# 不好的示例 ❌
class myclass:  # 类名应该用 PascalCase
    def MyMethod(self,param1,param2=10):  # 方法名应该用 snake_case
        pass  # 缺少 docstring
```

### 类型注解

使用 Python 类型注解：

```python
from typing import List, Dict, Optional

async def process_messages(
    messages: List[Dict[str, Any]],
    max_tokens: Optional[int] = None
) -> str:
    """处理消息列表"""
    pass
```

### 测试

- 为新功能编写测试
- 保持测试简单清晰
- 测试覆盖关键路径

```python
import pytest
from box_agent.tools.my_tool import MyTool

@pytest.mark.asyncio
async def test_my_tool():
    """测试自定义工具"""
    tool = MyTool()
    result = await tool.execute(param="test")
    assert result.success
    assert "expected" in result.content
```

## 社区准则

请遵守我们的[行为准则](CODE_OF_CONDUCT.md)，保持友好和尊重。

## 问题和帮助

如果有任何问题：

- 查看 [README](README.md) 和 [文档](docs/)
- 搜索现有的 Issues
- 创建新的 Issue 提问

## 许可证

提交代码即表示你同意将代码以 [MIT License](LICENSE) 发布。

---

再次感谢你的贡献！ 🎉
