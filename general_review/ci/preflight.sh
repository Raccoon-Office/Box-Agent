#!/usr/bin/env bash

set -euo pipefail

echo "[G1.1] Install locked dependencies"
uv sync --frozen --all-extras

echo "[G1.2] Compile Python sources"
uv run python -m compileall -q box_agent

echo "[G1.3] Run the Box-Agent test suite"
uv run pytest tests/ -q --tb=short \
  --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server

echo "[G1.4] Build source and wheel distributions"
uv build
