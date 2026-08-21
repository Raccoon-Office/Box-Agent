"""Clean entry point for the standalone ACP runtime binary.

PyInstaller freezes this module as the main script.  It must NOT import
anything at module level that prints to stdout — the ACP protocol owns
stdout exclusively.
"""

import os
import sys


WEB_EXTRACT_MCP_ARG = "--web-extract-mcp"


class _McpStdoutProxy:
    """Give MCP an owned stdout buffer without closing the launcher stream."""

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self.buffer = os.fdopen(os.dup(stream.fileno()), "wb")

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def main() -> None:
    if sys.argv[1:] == [WEB_EXTRACT_MCP_ARG]:
        # Reuse the frozen runtime binary for the bundled stdio MCP server.
        # This keeps desktop packaging to one PyInstaller payload while still
        # giving hosts a stable command they can advertise in mcp.json.
        from box_agent.mcp_servers.web_extract_server import main as run_web_extract_mcp

        protocol_stdout = sys.stdout
        sys.stdout = _McpStdoutProxy(protocol_stdout)
        try:
            run_web_extract_mcp()
        finally:
            # MCP owns and may close the duplicate buffer. Restore the original
            # stream before PyInstaller performs its final stdout flush.
            sys.stdout = protocol_stdout
        return

    # Ensure stdout is only used for ACP protocol — redirect any stray
    # stdlib print() calls to stderr before importing anything else.
    # Use sys.stderr directly (NOT TextIOWrapper(sys.stderr.buffer))
    # because TextIOWrapper.__del__ closes the underlying buffer,
    # which would destroy sys.stderr when the wrapper is GC'd.
    sys.stdout = sys.stderr

    # Now safe to import and run
    import asyncio
    from box_agent.acp import run_acp_server
    asyncio.run(run_acp_server())


if __name__ == "__main__":
    main()
