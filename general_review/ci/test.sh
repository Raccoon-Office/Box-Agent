#!/usr/bin/env bash

set -euo pipefail

# The default CI suite includes these offline contracts. This switch is the
# focused local/CI gate; it never invokes the real-model ACP benchmark.
if [[ "${1:-}" == "--context-cache-only" ]]; then
  uv run pytest -q --tb=short \
    tests/test_context_cache_eval.py \
    tests/test_table_quality_eval.py tests/test_table_execution_gate.py tests/test_skill_recovery_gate.py \
    tests/test_compaction_budget_convergence.py \
    tests/test_stable_request_prefix.py tests/test_stable_tool_exposure.py \
    tests/test_skill_append_only_context.py tests/test_compaction_resource_retention.py tests/test_openai_cache_usage.py \
    tests/test_stable_acp_protocol.py tests/test_tool_call_routing.py \
    tests/test_tool_interface_sessions.py tests/test_stable_session_adapters.py
  PYTHONPATH="test_workspace/acp_eval/src${PYTHONPATH:+:$PYTHONPATH}" \
    uv run pytest test_workspace/acp_eval/tests -q --tb=short
  exit 0
fi

uv run pytest tests/ -q --tb=short \
  -n auto --maxprocesses=4 --dist=loadfile --durations=20 \
  --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server

# Exercise multi-turn capture with the local fake ACP server, without model calls.
PYTHONPATH="test_workspace/acp_eval/src${PYTHONPATH:+:$PYTHONPATH}" \
  uv run pytest test_workspace/acp_eval/tests -q --tb=short
