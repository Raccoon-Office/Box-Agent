"""Fixed provider-facing entry point for discovered tools.

The engine resolves this entry before invocation; this object never forwards calls.
"""
from .base import Tool, ToolResult


class CallTool(Tool):
    @property
    def name(self) -> str:
        return "call_tool"

    @property
    def description(self) -> str:
        return "Call a discovered tool using its tool_ref and JSON-encoded arguments."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "tool_ref": {"type": "string", "description": "Copy the tool_ref returned by discovery."},
                "arguments_json": {"type": "string", "description": "A JSON object matching the tool's input_schema."},
            },
            "required": ["tool_ref", "arguments_json"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=False, error="call_tool must be resolved by the tool engine.")
