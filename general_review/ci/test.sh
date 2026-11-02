#!/usr/bin/env bash

set -euo pipefail

uv run pytest tests/ -q --tb=short \
  -n auto --maxprocesses=4 --dist=loadfile --durations=20 \
  --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server
