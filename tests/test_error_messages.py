"""Tests for box_agent.llm.error_messages.humanize_llm_error."""

from types import SimpleNamespace

from box_agent.llm.error_messages import (
    classify_llm_error,
    extract_llm_error_code,
    humanize_llm_error,
    structured_llm_error,
)
from box_agent.retry import RetryExhaustedError, StreamInterrupted


class _FakeAPIError(Exception):
    """Mimic an openai/anthropic style exception with structured attrs."""

    def __init__(self, message, *, code=None, status_code=None, body=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.body = body


def test_content_filter_from_raw_string():
    exc = Exception(
        "Error code: 400 - {'error': {'code': 'content_filter', "
        "'message': 'Inappropriate input/output rejected for security reasons', "
        "'param': None, 'type': 'invalid_request_error'}}"
    )
    fe = classify_llm_error(exc)
    assert fe.category == "content_filter"
    assert "换个问题" in fe.message


def test_content_filter_is_soft():
    exc = Exception("content_filter triggered")
    assert classify_llm_error(exc).is_soft is True


def test_non_soft_errors_are_hard():
    for raw, _cat in (
        ("Rate limit reached", "rate_limit"),
        ("Incorrect API key", "auth"),
        ("Internal server error", "server_error"),
        ("totally novel boom", "unknown"),
    ):
        assert classify_llm_error(Exception(raw)).is_soft is False


def test_content_filter_from_attrs():
    exc = _FakeAPIError("bad request", code="content_filter", status_code=400)
    assert classify_llm_error(exc).category == "content_filter"


def test_auth_error():
    exc = _FakeAPIError("Incorrect API key provided", status_code=401)
    assert classify_llm_error(exc).category == "auth"


def test_rate_limit_error():
    exc = _FakeAPIError("Rate limit reached for requests", status_code=429)
    assert classify_llm_error(exc).category == "rate_limit"


def test_quota_error():
    exc = Exception("You exceeded your current quota, please check your billing")
    assert classify_llm_error(exc).category == "quota"


def test_points_insufficient_error_preserves_structured_business_code():
    exc = _FakeAPIError(
        "Bad request",
        status_code=400,
        body={"error": {"code": "1000007", "message": "insufficient_points"}},
    )

    assert classify_llm_error(exc).category == "quota"
    assert extract_llm_error_code(exc) == 1000007


def test_error_code_extraction_does_not_parse_unstructured_message_text():
    assert extract_llm_error_code(Exception("request failed with code 1000007")) is None


def test_context_length_error():
    exc = Exception("This model's maximum context length is 128000 tokens")
    assert classify_llm_error(exc).category == "context_length"


def test_sensenova_prompt_token_limit_error_is_concise_and_actionable():
    exc = _FakeAPIError(
        (
            "Error code: 400 - {'error': {'code': '400', 'message': \""
            "litellm.BadRequestError: Custom_raccoonException - the input prompt "
            "token len 319439 + max_new_tokens 63999 > 262144No fallback model "
            "group found for original model_group=sn-sensenova-6-8-flash-lite.\"}}"
        ),
        status_code=400,
        body={
            "error": {
                "code": "400",
                "message": (
                    "litellm.BadRequestError: Custom_raccoonException - the input "
                    "prompt token len 319439 + max_new_tokens 63999 > 262144No "
                    "fallback model group found for original "
                    "model_group=sn-sensenova-6-8-flash-lite."
                ),
            }
        },
    )

    details = structured_llm_error(
        exc,
        provider="openai",
        model="sn-sensenova-6-8-flash-lite",
    )

    assert details["category"] == "context_length"
    assert details["reason"] == "context_length"
    assert details["code"] == 400
    assert details["httpStatus"] == 400
    assert details["retryable"] is False
    assert details["message"] == (
        "当前对话内容过长，超出所选模型可处理的上限。"
        "请新建会话，或精简历史消息、附件和本次输入后重试。"
    )
    assert "Error code" not in str(details["message"])
    assert "fallback model group" not in str(details["message"])


def test_server_error():
    exc = _FakeAPIError("Internal server error", status_code=500)
    assert classify_llm_error(exc).category == "server_error"


def test_model_not_found_beats_endpoint_404():
    exc = _FakeAPIError(
        "Error code: 404 - {'error': {'code': 'model_not_found'}}",
        code="model_not_found",
        status_code=404,
    )
    fe = classify_llm_error(exc)
    assert fe.category == "model_not_found"
    assert "model" in fe.message


def test_unsupported_model_returns_structured_configuration_error():
    exc = _FakeAPIError(
        "Error code: 400 - {'error': {'code': 'invalid_parameter_error', "
        "'message': 'model `qwen3.7-plus1` is not supported.', "
        "'param': None, 'type': 'invalid_request_error'}, "
        "'request_id': '959710cc-09b9-995b-adee-dddb692b43cc'}",
        status_code=400,
        body={
            "error": {
                "code": "invalid_parameter_error",
                "message": "model `qwen3.7-plus1` is not supported.",
                "param": None,
                "type": "invalid_request_error",
            },
            "request_id": "959710cc-09b9-995b-adee-dddb692b43cc",
        },
    )

    details = structured_llm_error(
        exc,
        provider="anthropic",
        model="qwen3.7-plus1",
    )

    assert details == {
        "source": "llm_provider",
        "category": "model_configuration",
        "reason": "model_not_supported",
        "code": "invalid_parameter_error",
        "type": "invalid_request_error",
        "httpStatus": 400,
        "provider": "anthropic",
        "model": "qwen3.7-plus1",
        "message": (
            "当前配置的模型 `qwen3.7-plus1` 不受支持。"
            "请检查 model 名称及其与当前 `anthropic` provider 的兼容性。"
        ),
        "retryable": False,
        "requestId": "959710cc-09b9-995b-adee-dddb692b43cc",
    }
    assert "Error code" not in str(details["message"])


def test_structured_error_extracts_request_id_from_response_headers():
    exc = _FakeAPIError("Bad request", status_code=400)
    exc.response = SimpleNamespace(headers={"x-request-id": "provider-request-1"})

    details = structured_llm_error(exc)

    assert details["requestId"] == "provider-request-1"


def test_supported_model_names_mismatch_is_model_configuration_error():
    exc = _FakeAPIError(
        "Error code: 400",
        status_code=400,
        body={
            "error": {
                "message": (
                    "The supported API model names are deepseek-v4-pro, "
                    "deepseek-v4-flash, and deepseek-v4-flash-vision-exp, "
                    "but you passed deepseek-v4-pro1."
                ),
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_request_error",
            }
        },
    )

    details = structured_llm_error(
        exc,
        provider="openai",
        model="deepseek-v4-pro1",
    )

    assert details["category"] == "model_configuration"
    assert details["reason"] == "model_not_supported"
    assert details["code"] == "invalid_request_error"
    assert details["type"] == "invalid_request_error"
    assert details["retryable"] is False
    assert details["message"] == (
        "当前配置的模型 `deepseek-v4-pro1` 不受支持。"
        "请检查 model 名称及其与当前 `openai` provider 的兼容性。"
    )


def test_endpoint_404_suggests_provider_protocol_mismatch():
    exc = _FakeAPIError("404 page not found", status_code=404)
    fe = classify_llm_error(exc)
    assert fe.category == "endpoint_not_found"
    assert "api_base" in fe.message
    assert "provider" in fe.message
    assert "xiaohuanxiong.com/api/web/llm/v2" in fe.message
    assert "provider: openai" in fe.message


def test_unwraps_retry_exhausted():
    inner = _FakeAPIError("content_filter triggered", code="content_filter")
    wrapped = RetryExhaustedError(inner, attempts=3)
    assert classify_llm_error(wrapped).category == "content_filter"


def test_unwraps_stream_interrupted():
    inner = Exception("rate limit exceeded (429)")
    wrapped = StreamInterrupted(inner, partial_text="hi")
    assert classify_llm_error(wrapped).category == "rate_limit"


def test_unknown_falls_back_to_trimmed_raw():
    exc = Exception("some totally novel boom")
    fe = classify_llm_error(exc)
    assert fe.category == "unknown"
    assert "some totally novel boom" in fe.message


def test_unknown_long_message_is_truncated():
    exc = Exception("x" * 5000)
    msg = humanize_llm_error(exc)
    assert msg.endswith("…")
    assert len(msg) < 400


def test_no_raw_json_blob_in_content_filter_message():
    exc = Exception(
        "Error code: 400 - {'error': {'code': 'content_filter', 'message': 'x'}}"
    )
    msg = humanize_llm_error(exc)
    assert "{" not in msg and "error code" not in msg.lower()


# ── Bulletproofing: error-handling code must never raise ──────────────────


class _ExplodingStr(Exception):
    """An exception whose __str__ itself raises — must not crash classification."""

    def __str__(self):
        raise RuntimeError("boom in __str__")


class _ExplodingAttr(Exception):
    """An exception whose attributes raise on access."""

    @property
    def code(self):
        raise RuntimeError("boom in code")

    @property
    def status_code(self):
        raise RuntimeError("boom in status_code")

    @property
    def body(self):
        raise RuntimeError("boom in body")


class _ExplodingBodyRepr:
    """An object that raises on str()/repr() — used as a .body value."""

    def __str__(self):
        raise RuntimeError("boom")

    __repr__ = __str__


class _BodyAttrError(Exception):
    def __init__(self):
        super().__init__("wrapper")
        self.body = _ExplodingBodyRepr()


def test_exploding_str_does_not_raise():
    fe = classify_llm_error(_ExplodingStr())
    assert isinstance(fe.message, str) and fe.message
    assert fe.category == "unknown"


def test_exploding_attrs_do_not_raise():
    fe = classify_llm_error(_ExplodingAttr("rate limit reached"))
    # str() still works here, so the rate_limit token is found.
    assert fe.category == "rate_limit"


def test_exploding_body_value_does_not_raise():
    fe = classify_llm_error(_BodyAttrError())
    assert isinstance(fe.message, str) and fe.message


def test_humanize_never_raises_on_weird_input():
    for weird in (_ExplodingStr(), _ExplodingAttr("x"), _BodyAttrError(),
                  Exception(""), RuntimeError()):
        msg = humanize_llm_error(weird)
        assert isinstance(msg, str) and msg  # always non-empty string


def test_structured_error_never_raises_on_weird_input():
    details = structured_llm_error(_ExplodingAttr("x"))
    assert details["source"] == "llm_provider"
    assert isinstance(details["message"], str) and details["message"]


def test_self_referential_last_exception_does_not_loop():
    exc = Exception("rate limit")
    exc.last_exception = exc  # pathological self-reference
    fe = classify_llm_error(exc)
    assert fe.category == "rate_limit"


def test_empty_exception_falls_back_to_generic():
    msg = humanize_llm_error(Exception(""))
    assert "模型调用失败" in msg or "请稍后重试" in msg
