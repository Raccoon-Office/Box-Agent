"""Agent run logger"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .llm.debug_logging import (
    full_payload_logging_enabled,
    summarize_request_payload_for_logging,
)
from .schema import Message, ToolCall

from box_agent.user_paths import state_path


class AgentLogger:
    """Agent run logger

    Responsible for recording the complete interaction process of each agent run, including:
    - LLM requests and responses
    - Tool calls and results
    """

    def __init__(self):
        """Initialize logger

        Logs are stored in ~/.box-agent/log/ directory
        """
        # Use ~/.box-agent/log/ directory for logs
        self.log_dir = state_path('log')
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = None
        self.log_index = 0

    def start_new_run(self):
        """Start new run, create new log file"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        suffix = 0
        while True:
            discriminator = f"_{suffix}" if suffix else ""
            candidate = self.log_dir / f"agent_run_{timestamp}{discriminator}.log"
            try:
                handle = candidate.open("x", encoding="utf-8")
                break
            except FileExistsError:
                suffix += 1
        self.log_file = candidate
        self.log_index = 0

        # Write log header
        with handle as f:
            f.write("=" * 80 + "\n")
            f.write(f"Agent Run Log - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")

    def log_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        cache_fingerprint: Mapping[str, Any] | None = None,
    ):
        """Log LLM request

        Args:
            messages: Message list
            tools: Tool list (optional)
            cache_fingerprint: Cache-sensitive request fingerprint (optional)
        """
        self.log_index += 1

        # Build complete request data structure
        request_data = {
            "messages": [],
            "tools": [],
        }

        # Convert messages to JSON serializable format
        for msg in messages:
            msg_dict = {
                "role": msg.role,
                "content": msg.content,
            }
            if msg.thinking:
                msg_dict["thinking"] = msg.thinking
            if msg.tool_calls:
                msg_dict["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
            if msg.tool_call_id:
                msg_dict["tool_call_id"] = msg.tool_call_id
            if msg.name:
                msg_dict["name"] = msg.name

            request_data["messages"].append(msg_dict)

        # Only record tool names
        if tools:
            request_data["tools"] = [tool.name for tool in tools]

        if cache_fingerprint is not None:
            request_data["cache_fingerprint"] = dict(cache_fingerprint)

        if not full_payload_logging_enabled():
            request_data = summarize_request_payload_for_logging(request_data)

        # Format as JSON
        content = "LLM Request:\n\n"
        content += json.dumps(request_data, indent=2, ensure_ascii=False)

        self._write_log("REQUEST", content)

    def log_response(
        self,
        content: str,
        thinking: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        finish_reason: str | None = None,
        usage: Any | None = None,
        provider_request_id: str | None = None,
    ):
        """Log LLM response

        Args:
            content: Response content
            thinking: Thinking content (optional)
            tool_calls: Tool call list (optional)
            finish_reason: Finish reason (optional)
            usage: TokenUsage from the API response (optional). Recorded so
                truncation/cost diagnostics survive even when LLM debug logging
                is off.
            provider_request_id: Vendor request id (optional).
        """
        self.log_index += 1

        # Build complete response data structure
        response_data = {
            "content": content,
        }

        if thinking:
            response_data["thinking"] = thinking

        if tool_calls:
            response_data["tool_calls"] = [tc.model_dump() for tc in tool_calls]

        if finish_reason:
            response_data["finish_reason"] = finish_reason

        if usage is not None:
            response_data["usage"] = usage.model_dump() if hasattr(usage, "model_dump") else usage

        if provider_request_id:
            response_data["provider_request_id"] = provider_request_id

        if not full_payload_logging_enabled():
            response_data = summarize_request_payload_for_logging(response_data)

        # Format as JSON
        log_content = "LLM Response:\n\n"
        log_content += json.dumps(response_data, indent=2, ensure_ascii=False)

        self._write_log("RESPONSE", log_content)

    def log_llm_debug_record(self, record: dict[str, Any]):
        """Log provider-level LLM request/response metadata.

        These records are produced by the LLM client layer and contain the
        exact SDK request payload sent to the provider (with credentials
        redacted) plus response metadata such as the provider request id.
        """
        self.log_index += 1

        event = record.get("event", "llm/debug")
        request_id = record.get("request_id") or ""
        summary = f"LLM Debug Record: {event}"
        if request_id:
            summary += f" request_id={request_id}"

        content = summary + "\n\n"
        content += json.dumps(record, indent=2, ensure_ascii=False, default=str)

        self._write_log("LLM_DEBUG", content)

    def log_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result_success: bool,
        result_content: str | None = None,
        result_error: str | None = None,
        raw_output: dict[str, Any] | None = None,
        tool_id: str | None = None,
        server_name: str | None = None,
    ):
        """Log tool execution result

        Args:
            tool_name: Tool name
            arguments: Tool arguments
            result_success: Whether successful
            result_content: Result content (on success)
            result_error: Error message (on failure)
            raw_output: Optional structured result payload for host/CLI UIs
            tool_id: Stable MCP catalog target identifier, when applicable
            server_name: MCP server owning the target, when applicable
        """
        self.log_index += 1

        # Build complete tool execution result data structure
        tool_result_data = {
            "tool_name": tool_name,
            "arguments": arguments,
            "success": result_success,
        }
        if tool_id is not None:
            tool_result_data["tool_id"] = tool_id
        if server_name is not None:
            tool_result_data["server_name"] = server_name

        if result_success:
            tool_result_data["result"] = result_content
        else:
            tool_result_data["error"] = result_error
        if raw_output is not None:
            tool_result_data["raw_output"] = raw_output

        if not full_payload_logging_enabled():
            tool_result_data = summarize_request_payload_for_logging(tool_result_data)

        # Format as JSON
        content = "Tool Execution:\n\n"
        content += json.dumps(tool_result_data, indent=2, ensure_ascii=False)

        self._write_log("TOOL_RESULT", content)

    def _write_log(self, log_type: str, content: str):
        """Write log entry

        Args:
            log_type: Log type (REQUEST, RESPONSE, TOOL_RESULT)
            content: Log content
        """
        if self.log_file is None:
            return

        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write("\n" + "-" * 80 + "\n")
            f.write(f"[{self.log_index}] {log_type}\n")
            f.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}\n")
            f.write("-" * 80 + "\n")
            f.write(content + "\n")

    def get_log_file_path(self) -> Path:
        """Get current log file path"""
        return self.log_file
