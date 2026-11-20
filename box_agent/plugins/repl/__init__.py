"""Optional session-owned REPL with a lightweight run context binding."""
from __future__ import annotations

import sys

from box_agent.plugins.builtins import AgentRunBindingPort
from box_agent.plugins.descriptors import PluginDescriptor, PluginScope
from .config import ReplConfig


def prepare_repl(resources, *, allow_full_access: bool, cua_service=None):
    config = ReplConfig.model_validate(resources.context.config.plugins.get("repl", {}))
    if not allow_full_access or sys.platform != "darwin" or not config.trusted_local_code:
        return
    capabilities = ["browser"]
    if cua_service is not None and cua_service.config.enabled:
        capabilities.append("cua")
    from .runtime import ReplRuntime, resolve_node
    from .tools import create_tools

    runtime = ReplRuntime(node=resolve_node(config.node_path), workspace=resources.context.workspace,
                          cua_service=cua_service)
    resources.tools.extend(create_tools(runtime, capabilities=capabilities))
    resources.cleanup.push_async_callback(runtime.close)


class RunBinding:
    def __init__(self, run):
        self.run = run
        self.runtimes = []

    def install_agent(self, agent):
        from .tools import ReplTool
        from .policy import model_tool_filter
        for tool in agent.tools.values():
            if isinstance(tool, ReplTool) and tool.runtime not in self.runtimes:
                runtime = tool.runtime
                runtime.agent, runtime.turn_text = agent, self.run.options.current_turn_text
                self.runtimes.append(runtime)
        # Install this policy even when the session has no JS worker. Previously
        # prepared calls and restored refs must follow the current turn's policy.
        plugins = self.run.session.config.plugins
        server = (plugins.get("cua") or {}).get("server_name", "computer-use")
        allowed = model_tool_filter(server)
        exposure = getattr(agent, "mcp_tool_exposure", None)
        if exposure is not None:
            exposure.model_tool_filter = allowed
        search = agent.tools.get("tool_search")
        if search is not None:
            search.model_tool_filter = allowed

    def close(self):
        for runtime in self.runtimes:
            runtime.agent = None
            runtime.turn_text = None
        self.runtimes.clear()


def plugin_descriptors():
    return (PluginDescriptor(
        "run.repl", "1.0.0", (AgentRunBindingPort,), scope=PluginScope.RUN,
        dependencies=("run.services",), context_factory=lambda ctx: RunBinding(ctx.context),
        disposer=lambda binding: binding.close(),
    ),)
