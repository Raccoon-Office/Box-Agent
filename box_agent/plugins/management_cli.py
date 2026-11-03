"""Compatibility module for the shared host management entry point."""

from ..plugin_management import MAX_REQUEST_BYTES, PUBLIC_METHODS, handle_request, main

__all__ = ["handle_request", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
