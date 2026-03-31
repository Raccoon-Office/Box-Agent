"""Clean entry point for the standalone ACP runtime binary.

PyInstaller freezes this module as the main script.  It must NOT import
anything at module level that prints to stdout — the ACP protocol owns
stdout exclusively.
"""

import sys


def main() -> None:
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
