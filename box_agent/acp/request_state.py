"""Release received frames on the pinned ACP 0.12.1 connection.

The SDK now owns only live outgoing futures, so the 0.6 state store and sender
are no longer needed. Its receive loop still holds the last message while
waiting for the next one. Give dispatch a shallow envelope copy, then clear
the receive-loop envelope so an idle cloud agent cannot retain a finished
prompt or host response. The handler task / reply future owns the payload.
"""

from acp.connection import Connection


def release_received_frames(connection: Connection) -> None:
    """Install one instance-local hook before the connection starts listening.

    This private SDK hook is intentionally covered by real-Connection lifetime
    tests and tied to the exact dependency pin; routing stays inside the SDK.
    """
    dispatch = connection._process_message

    def dispatch_and_release(message: dict) -> None:
        try:
            dispatch(message.copy())
        finally:
            message.clear()

    connection._process_message = dispatch_and_release
