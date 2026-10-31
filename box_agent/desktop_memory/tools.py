"""Named desktop memory tools; identity and write qualification stay private."""

from __future__ import annotations

from typing import Any

from ..memory import MemoryEditError
from ..tools.base import Tool, ToolResult
from .backend import model_json
from .transport import MemoryProxyError


class DesktopMemoryTool(Tool):
    def __init__(self, backend, operation: str) -> None:
        self._backend, self.operation = backend, operation

    @property
    def name(self) -> str:
        return "memory_" + self.operation

    @property
    def max_result_size_chars(self) -> float | None:
        # Full core reads have already passed final-input capacity admission.
        return float("inf") if self.operation == "read" else None

    @property
    def description(self) -> str:
        return {
            "read": "完整读取 user.md 或 memory.md，保留保护属性及修改规则。只有全文真正进入模型请求后才可修改；预载不代替读取。",
            "write": "完整写入 user.md 或 memory.md；须先完整 memory_read 并取得资格，保护条目不得重写。每次写后必须重新读取。",
            "edit": "修改 user.md 或 memory.md 的完整条目；须先完整读取。old_text 唯一匹配连续条目，空值追加；new_text 空值删除。保护项不得改删。",
            "search": "搜索当前轮次来源中的历史记忆，返回有界片段及引用；实时状态仍须核实。",
            "managed_read": "按 entry_ref 和同一文件 version 分段读取用户维护条目，版本改变须重新搜索，不解锁自动核心修改。",
            "experience_search": "检索并校验可复用经验，返回适用的 Observation；独立于普通历史搜索，仅在线云端可用。",
        }[self.operation]

    @property
    def parameters(self) -> dict[str, Any]:
        path = {"type": "string", "enum": ["user.md", "memory.md"]}
        if self.operation == "read":
            properties, required = {"path": path}, ["path"]
        elif self.operation == "write":
            properties = {"path": path, "content": {"type": "string", "description": "全部 <mem>...</mem> 条目，不带属性"}}
            required = list(properties)
        elif self.operation == "edit":
            properties = {"path": path, "old_text": {"type": "string"}, "new_text": {"type": "string"}}
            required = list(properties)
        elif self.operation == "managed_read":
            properties = {"entry_ref": {"type": "string"}, "version": {"type": "string"},
                          "offset": {"type": "integer", "minimum": 0, "default": 0},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 4000, "default": 2000}}
            required = ["entry_ref", "version"]
        else:
            properties = {"query": {"type": "string"},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6}}
            required = ["query"]
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    async def execute(self, **arguments: Any) -> ToolResult:
        try:
            method = {"read": self._backend.read_core_memory, "write": self._backend.write_core_memory,
                      "edit": self._backend.edit_core_memory, "search": self._backend.search_memory,
                      "managed_read": self._backend.managed_read, "experience_search": self._backend.experience_search}[self.operation]
            data = await method(**arguments)
            content = model_json(data)
            return ToolResult(success=True, content=content, model_context=content,
                              raw_output=data if isinstance(data, dict) else {"results": data})
        except MemoryProxyError as exc:
            return ToolResult(success=False, error=exc.code, raw_output={"code": exc.code, "outcome": exc.outcome})
        except MemoryEditError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception:
            return ToolResult(success=False, error="MEMORY_OPERATION_FAILED")


def create_desktop_memory_tools(backend) -> list[Tool]:
    return [DesktopMemoryTool(backend, operation) for operation in ("read", "write", "edit", "search", "managed_read", "experience_search")]
