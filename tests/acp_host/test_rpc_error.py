"""Transport errors preserve diagnostic fields and normal exception semantics."""

from __future__ import annotations

import pytest

from .probe import RpcError


def test_rpc_error_raise_and_catch() -> None:
    with pytest.raises(RpcError) as ei:
        raise RpcError(code="eof", message="ACP subprocess closed stdout (EOF)", data={"n": 1})
    err = ei.value
    assert isinstance(err, Exception)
    assert isinstance(err, RpcError)
    assert err.code == "eof"
    assert "EOF" in err.message
    assert err.data == {"n": 1}
    assert err.args  # Exception.args populated via __post_init__
    assert "eof" in str(err)


def test_rpc_error_catch_as_exception() -> None:
    caught: Exception | None = None
    try:
        raise RpcError(code= -32000, message="server error")
    except Exception as exc:
        caught = exc
    assert isinstance(caught, RpcError)
    assert caught.code == -32000
