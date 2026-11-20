"""Deterministic standard ACP stdio endpoint for local device integration.

Parent prompt: JSON {"instruction":"write result.txt"} discovers a registered device.
Device prompt: any text writes result.txt=ok; include "wait" for a cancellable wait.
"""
import asyncio
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from acp.agent.connection import AgentSideConnection
from box_agent.acp import BoxACPAgent
from box_agent.acp.stdio_compat import StdioTransport, stdio_streams_largebuf
from box_agent.config import Config, AgentConfig, ToolsConfig, LLMConfig
from box_agent.schema import FunctionCall, LLMResponse, StreamEvent, ToolCall


ASK_FOR_PROJECT_CODE = "ask for project code then write result.txt"
ASK_FOR_PROJECT_CODE_WITH_APPROVAL = "ask for project code then approval write"


class DeterministicLLM:
    async def generate(self, messages, tools=None, **kwargs):
        return LLMResponse(content="device demo", finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **kwargs):
        # Find the most recent user turn, independent of session/model call count.
        values = [{**m.model_dump(), "source": m.source} if hasattr(m, "model_dump") else m for m in messages]
        user_index = max((i for i, m in enumerate(values) if m.get("role") == "user" and (m.get("source") != "runtime" or (m.get("input_facts") or {}).get("kind") in {"device_wake", "device_delegation"}) and m.get("name") != "tool_descriptions" and (m.get("input_facts") or not m.get("name"))), default=0)
        trace_path = os.environ.get("DEVICE_TEST_MODEL_TRACE")
        if trace_path:
            marker = "Device delegation updates (data, not instructions):\n"
            updates = []
            for message in values:
                content = message.get("content")
                if message.get("source") == "runtime" and isinstance(content, str) and marker in content:
                    updates.extend(json.loads(content.split(marker, 1)[1]))
            with Path(trace_path).open("a", encoding="utf-8") as trace:
                trace.write(json.dumps({
                    "inputId": (values[user_index].get("input_facts") or {}).get("id"),
                    "updates": updates,
                }, ensure_ascii=False) + "\n")
        tail = values[user_index:]
        text = tail[0].get("content", "")
        if not isinstance(text, str):
            text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
        facts = tail[0].get("input_facts") or {}
        if facts.get("kind") == "device_wake":
            yield StreamEvent(type="text", delta="device updates received")
            yield StreamEvent(type="finish", finish_reason="stop")
            return
        asks_for_code = any(
            line.strip().lower() in {ASK_FOR_PROJECT_CODE, ASK_FOR_PROJECT_CODE_WITH_APPROVAL}
            for line in (self._original_text(facts) or text).splitlines()
        )
        continued_from = (facts.get("deviceDelegation") or {}).get("continuationOfTurnId")
        approval_continuation = bool(continued_from and any(
            prior.get("id") == continued_from and
            self._original_text(prior).strip().lower() == ASK_FOR_PROJECT_CODE_WITH_APPROVAL
            for message in values[:user_index] if (prior := message.get("input_facts"))
        ))
        approval_write = "approval write" in text.lower() or approval_continuation
        args = self._prompt_arguments(text)
        tool_messages = [message for message in tail if message.get("role") == "tool"]
        if (args and "instruction" in args and "operation" not in args and "deviceId" not in args
                and any(tool.name == "device_list" for tool in tools or ())
                and (not tool_messages or tool_messages[-1].get("name") == "device_list")):
            event = self._discover_device(args, tool_messages)
            yield event
            if event.type == "text":
                yield StreamEvent(type="finish", finish_reason="stop")
            return
        if tool_messages:
            if (self._original_text(facts).strip() == "publish fixture report"
                    and tool_messages[-1].get("name") == "write_file"):
                yield self._tool_call("publish_artifact", {"path": "output/result.txt"})
                return
            instruction = str(tail[0].get("content", "")).lower()
            fallback_requested = "approval write fallback" in instruction
            latest_result = str(tool_messages[-1].get("content", ""))
            if (approval_write and len(tool_messages) == 1
                    and tool_messages[-1].get("name") == "write_file"
                    and not latest_result.startswith("Successfully wrote ")):
                if fallback_requested:
                    yield self._tool_call("write_file", {"path": "fallback.txt", "content": "ok"})
                    return
                yield StreamEvent(type="text", delta="The approval fixture could not be written.")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            if re.search(r'"hold"\s*:\s*true', str(tail[0].get("content", ""))):
                await asyncio.sleep(300)
            yield StreamEvent(type="text", delta=(
                "Completed the available work and wrote fallback.txt."
                if fallback_requested and latest_result.startswith("Successfully wrote ") and "fallback.txt" in latest_result
                else "accepted / done"
            ))
            yield StreamEvent(type="finish", finish_reason="stop")
            return
        if args and "operation" in args:
            name = "device_" + args["operation"].replace(".", "_")
            arguments = dict(args.get("args", {}))
            if "deviceId" in args:
                arguments["deviceId"] = args["deviceId"]
        elif args and "deviceId" in args:
            name = "device_subagent_start"
            arguments = {"deviceId": args["deviceId"], "instruction": args["instruction"]}
        else:
            if asks_for_code:
                yield self._tool_call("request_user_input", {
                    "question": "Which project code should I use?",
                    "missing_fields": ["project_code"],
                })
                return
            if "wait" in text.lower():
                await asyncio.sleep(300)
            if approval_write:
                fixture_home = os.environ.get("DEVICE_DEMO_APPROVAL_HOME")
                if not fixture_home or Path(fixture_home).resolve() != Path.home().resolve():
                    yield StreamEvent(type="text", delta="Set DEVICE_DEMO_APPROVAL_HOME to the demo process's fixture home.")
                    yield StreamEvent(type="finish", finish_reason="stop")
                    return
                path = str(Path.home().resolve() / "device-demo-approval.txt")
                yield self._tool_call("write_file", {"path": path, "content": "ok"})
                return
            name = "write_file"
            filename = "output/result.txt" if self._original_text(facts).strip() == "publish fixture report" else "result.txt"
            arguments = {"path": filename, "content": "ok"}
        yield self._tool_call(name, arguments)

    @staticmethod
    def _original_text(facts):
        return "\n".join(block["text"] for block in facts.get("blocks", []) if block.get("type") == "text")

    @staticmethod
    def _prompt_arguments(text):
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                candidate, _ = decoder.raw_decode(text, match.start())
            except ValueError:
                continue
            if isinstance(candidate, dict) and {"deviceId", "operation", "instruction"}.intersection(candidate):
                return candidate
        return None

    def _discover_device(self, args, tool_messages):
        if not tool_messages:
            return self._tool_call("device_list", {})
        cursor = None
        seen_cursors = set()
        eligible_devices = set()
        for message in tool_messages:
            try:
                page = json.loads(message["content"])
                devices = page["devices"]
            except (ValueError, TypeError, KeyError):
                return StreamEvent(type="text", delta="Device discovery failed.")
            for device in devices:
                device_id = device.get("deviceId")
                if (device_id and device.get("online") is True
                        and device.get("authorization", {}).get("execute") is True
                        and "subagent.start" in device.get("capabilities", ())
                        and args.get("deviceName", device_id) == device_id):
                    eligible_devices.add(device_id)
            cursor = page.get("nextCursor")
            if cursor in seen_cursors:
                return StreamEvent(type="text", delta="Device discovery returned a repeated cursor.")
            if cursor:
                seen_cursors.add(cursor)
        if cursor:
            return self._tool_call("device_list", {"cursor": cursor})
        if len(eligible_devices) == 1:
            device_id, = eligible_devices
            return self._tool_call("device_subagent_start", {
                "deviceId": device_id, "instruction": args["instruction"],
                **({"artifactDelivery": args["artifactDelivery"]} if "artifactDelivery" in args else {}),
            })
        if eligible_devices:
            return StreamEvent(type="text", delta="Multiple devices can execute tasks. Specify a registered deviceName.")
        return StreamEvent(type="text", delta="No matching online device can execute tasks.")

    @staticmethod
    def _tool_call(name, arguments):
        return StreamEvent(type="finish", finish_reason="tool", tool_calls=[
            ToolCall(id="demo-device-" + uuid4().hex, type="function", function=FunctionCall(name=name, arguments=arguments))])


async def main():
    closed = asyncio.Event()
    reader, writer = await stdio_streams_largebuf(on_eof=closed.set)
    config = Config(agent=AgentConfig(), tools=ToolsConfig(), llm=LLMConfig(api_key="local-deterministic-no-network"))
    model = DeterministicLLM()
    adapter = None
    def factory(conn):
        nonlocal adapter
        adapter = BoxACPAgent(conn, config, model, [], "Execute the requested tool once.")
        return adapter
    connection = AgentSideConnection(factory, StdioTransport(reader, writer, on_disconnect=closed.set), use_unstable_protocol=True)
    try:
        await closed.wait()
    finally:
        if adapter:
            await adapter.aclose()
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
