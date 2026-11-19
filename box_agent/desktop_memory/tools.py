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
            "edit": ("追加、替换或删除 user.md 或 memory.md 的完整条目。修改前须先完整 memory_read 同一路径，"
                     "每次修改成功后须重新读取。保护项不得改删。"),
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
            properties = {
                "path": path,
                "old_text": {
                    "type": "string",
                    "description": "空字符串表示追加。替换或删除时用完整的 <mem>...</mem> 包裹原正文，唯一匹配连续条目。",
                },
                "new_text": {
                    "type": "string",
                    "description": "新增或替换的内容使用完整的 <mem>...</mem> 条目，标签不带属性，正文非空。空字符串表示删除。",
                },
            }
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
            return ToolResult(
                success=True,
                content=content,
                model_context=content,
                raw_output=data if isinstance(data, dict) else {"results": data},
                raw_output_notice_in_model=self.operation != "read",
            )
        except MemoryProxyError as exc:
            error = exc.code
            if exc.code == "CORE_CONTEXT_TOO_LARGE":
                error = (
                    "CORE_CONTEXT_TOO_LARGE: 当前会话历史、工具定义与本次记忆全文合计超过安全输入预算。"
                    "这不等于记忆文件本身过大，也不代表云端读取失败；请先压缩会话或新建短会话，"
                    "再重新 memory_read。不要据此要求用户删除记忆，也不要在上下文未改变时反复重试。"
                )
            elif exc.code == "CORE_READ_REQUIRED" and self.operation in {"write", "edit"}:
                error = ("CORE_READ_REQUIRED: 请先对同一 path 调用 memory_read，"
                         "完整读取并核对当前条目后再修改。每次写入成功后都需要重新读取。")
            return ToolResult(success=False, error=error, raw_output={"code": exc.code, "outcome": exc.outcome})
        except MemoryEditError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception:
            return ToolResult(success=False, error="MEMORY_OPERATION_FAILED")


def create_desktop_memory_tools(backend) -> list[Tool]:
    return [DesktopMemoryTool(backend, operation) for operation in ("read", "write", "edit", "search", "managed_read", "experience_search")]
