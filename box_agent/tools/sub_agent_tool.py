"""Sub-agent tool for isolated context execution.

Spawns a child agent loop with its own message history so that
intermediate tool output (file reads, exploratory analysis, etc.)
stays out of the parent context.  Only the final summary is returned.

Multiple sub-agent calls are ``parallel_safe`` and will be executed
concurrently via ``asyncio.gather`` in the core loop.
"""

from __future__ import annotations

from typing import Any

from ..events import DoneEvent
from ..schema import Message
from .base import Tool, ToolResult

_SUB_AGENT_SYSTEM_PROMPT = """\
You are a focused sub-agent executing a specific task delegated by the main agent.

Rules:
1. Complete the assigned task thoroughly using the available tools.
2. If a Jupyter kernel session already exists, variables from previous executions \
are still in scope — reuse them directly.
3. When you are done, output a concise but complete summary of your findings or \
results.  Include key numbers, conclusions, and any file paths produced.
4. Do NOT ask follow-up questions — complete the task with what you have.
"""


class SubAgentTool(Tool):
    """Run a task in an isolated agent context.

    The child agent shares the same LLM client and tool instances (so
    Jupyter kernel sessions, sandbox state, etc. are preserved), but has
    its own message history.  Only the final textual summary is returned
    to the parent agent, keeping the parent context clean.
    """

    parallel_safe = True

    def __init__(
        self,
        *,
        llm,
        parent_tools: dict[str, Tool],
        workspace_dir: str | None = None,
        max_steps: int = 20,
        token_limit: int = 40_000,
    ):
        self._llm = llm
        # Exclude ourselves to prevent recursive sub-agent spawning.
        self._child_tools = {n: t for n, t in parent_tools.items() if n != self.name}
        self._workspace_dir = workspace_dir
        self._max_steps = max_steps
        self._token_limit = token_limit

    @property
    def name(self) -> str:
        return "sub_agent"

    @property
    def description(self) -> str:
        return (
            "Delegate a self-contained task to a sub-agent that runs in an isolated "
            "context. The sub-agent has access to the same tools (file, bash, sandbox, "
            "etc.) but maintains its own conversation history. Only its final summary "
            "is returned, keeping the main context clean.\n\n"
            "Use this when a task will produce a lot of intermediate output (e.g. "
            "reading multiple files, exploratory data analysis) and you only need the "
            "conclusion. You can call sub_agent multiple times in parallel for "
            "independent tasks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "A clear, self-contained description of the task for the "
                        "sub-agent to execute. Include all necessary context — the "
                        "sub-agent cannot see prior conversation history."
                    ),
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str) -> ToolResult:  # type: ignore[override]
        # Import here to avoid circular dependency (core → tools → core).
        from ..core import run_agent_loop

        messages: list[Message] = [
            Message(role="system", content=_SUB_AGENT_SYSTEM_PROMPT),
            Message(role="user", content=task),
        ]

        final_content = ""
        try:
            async for event in run_agent_loop(
                llm=self._llm,
                messages=messages,
                tools=self._child_tools,
                max_steps=self._max_steps,
                token_limit=self._token_limit,
                workspace_dir=self._workspace_dir,
            ):
                if isinstance(event, DoneEvent):
                    final_content = event.final_content
        except Exception as exc:
            return ToolResult(
                success=False,
                content="",
                error=f"Sub-agent execution failed: {type(exc).__name__}: {exc}",
            )

        if not final_content:
            return ToolResult(
                success=False,
                content="",
                error="Sub-agent finished without producing output.",
            )

        return ToolResult(success=True, content=final_content)
