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
   git clone https://github.com/MiniMax-AI/Box-Agent box-agent
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

#### 开发流程

1. **编写代码**
   - 遵循项目的代码风格（参考 [开发指南](docs/DEVELOPMENT.md#代码规范)）
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

5. **推送到你的 fork**
   ```bash
   git push origin feature/your-feature-name
   ```

6. **创建 Pull Request**
   - 在 GitHub 上创建 Pull Request
   - 清楚描述你的更改
   - 引用相关的 Issue（如果有）

#### Pull Request 检查清单

在提交 PR 之前，请确保：

- [ ] 代码遵循项目规范
- [ ] 所有测试通过
- [ ] 添加了必要的测试
- [ ] 更新了相关文档
- [ ] 提交消息清晰明确
- [ ] 没有不相关的更改

### 代码审查

所有 Pull Request 需要经过代码审查：

- 我们会尽快审查你的代码
- 可能会要求一些修改
- 请保持耐心并及时响应反馈
- 审查通过后会被合并到主分支

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

