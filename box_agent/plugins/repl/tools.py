"""Two independently discoverable tool groups backed by one REPL implementation."""
from __future__ import annotations

from box_agent.tools.base import Tool, ToolInvocationContext, ToolResult


class ReplTool(Tool):
    name = ""
    description = ""
    parameters = {}
    uses_invocation_context = True
    parallel_safe = False

    def __init__(self, runtime, capability: str, *, reset: bool = False):
        self.runtime, self.capability, self.reset = runtime, capability, reset
        self.name = f"{capability}_repl_{'reset' if reset else 'js'}"
        route = ("With a tool_ref, use call_tool and serialize target fields inside arguments_json. "
                 "If the reference is missing or expired, discover this tool first. ")
        if capability == "browser":
            init = "var browser = await cua.getBrowser('managed'); var tab;"
            self.call_example_arguments = {"mode": "managed"}
            if reset:
                action = "Reset only the selected mode's JS bindings and refs. Existing tabs stay open."
            else:
                self.call_example_arguments["code"] = init
                action = (f"Run browser JS as directed by browser-use. In a fresh managed session: {init} "
                          "For user mode use 'user' in both the code and the mode field. Reuse browser and tab. "
                          "For user current-page body text use tab.readText() on its current tab. "
                          "When asked to open a user-browser URL use browser.openAndRead(url). For managed navigation use tab.goto(url); refresh element refs with tab.getState() after evaluate.")
        elif reset:
            self.call_example_arguments = {}
            action = "Reset native App JS bindings and observations. Supply {}. Open apps and edits survive."
        else:
            self.call_example_arguments = {"code": "await cua.listApps()", "title": "List apps"}
            action = ("Run persistent native App JS as directed by computer-use. Bind the requested app with "
                      "cua.getApp(name), or first list candidates with await cua.listApps(). Await every action. "
                      "For call_tool, put optional title alongside code inside arguments_json.")
        self.description = route + action
        properties = {} if reset else {
            "code": {"type": "string", "minLength": 1, "maxLength": 100000},
            "title": {"type": "string"},
            "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 120000, "default": 60000},
        }
        required = [] if reset else ["code"]
        if capability == "browser":
            properties["mode"] = {"type": "string", "enum": ["managed", "user"]}
            required.append("mode")
        self.parameters = {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}

    @property
    def transient_followup_allowed(self) -> bool:
        if self.capability != "cua" or self.reset or self.runtime.cua_service is None:
            return False
        from box_agent.tools.mcp_result_hooks import allowed_mcp_result_adapters
        return bool(allowed_mcp_result_adapters(
            server_name=self.runtime.cua_service.name, remote_name=self.name,
        ))

    async def execute(self, **kwargs):
        return await self.invoke(kwargs)

    async def _invoke_validated(self, arguments: dict, *, context: ToolInvocationContext | None):
        try:
            if self.reset:
                await self.runtime.reset(arguments.get("mode") or self.capability)
                return ToolResult(success=True, content="JS bindings cleared; apps and user tabs left open.")
            return await self.runtime.execute(
                self.capability, arguments.get("mode"), arguments["code"],
                arguments.get("title"), arguments.get("timeout_ms", 60000), context,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            return ToolResult(success=False, error=str(exc))


def create_tools(runtime, *, capabilities=("cua", "browser")):
    return [ReplTool(runtime, capability, reset=reset)
            for capability in capabilities for reset in (False, True)]
