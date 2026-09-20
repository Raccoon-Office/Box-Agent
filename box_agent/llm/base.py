"""Base class for LLM clients."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..auth import (
    HostedAuthRequiredError,
    ensure_hosted_auth_ready,
    read_auth_org_code,
    read_auth_token_file,
    _xiaohuanxiong_refresh_url,
    refresh_hosted_auth_token_if_needed,
    request_auth_headers,
)
from ..client_info import current_client_headers, should_attach_client_headers
from ..retry import RetryConfig
from ..schema import LLMResponse, Message, StreamEvent

HOSTED_AUTH_API_KEY_PLACEHOLDERS = {
    "",
    "box-agent-auth-json",
    "box-agent-no-auth",
    "YOUR_API_KEY_HERE",
}


class LLMClientBase(ABC):
    """Abstract base class for LLM clients.

    This class defines the interface that all LLM clients must implement,
    regardless of the underlying API protocol (Anthropic, OpenAI, etc.).
    """

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        retry_config: RetryConfig | None = None,
        auth_token: str = "",
        auth_file: str = "",
        timeout: float = 600.0,
    ):
        """Initialize the LLM client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API
            model: Model name to use
            retry_config: Optional retry configuration
            auth_token: Optional in-memory product login token.
            auth_file: Optional auth.json path read before every request.
            timeout: Wall-clock cap (seconds) handed to the provider SDK.
        """
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.retry_config = retry_config or RetryConfig()
        self.auth_token = auth_token
        self.auth_file = auth_file
        self.timeout = timeout

        # Callback for tracking retry count
        self.retry_callback = None

    async def _auth_headers(
        self,
        existing: dict[str, str | bytes] | None = None,
    ) -> dict[str, str | bytes]:
        """Refresh hosted login auth when needed and return request headers."""
        headers = dict(existing or {})
        if self.api_key.strip() not in HOSTED_AUTH_API_KEY_PLACEHOLDERS:
            return headers

        await ensure_hosted_auth_ready(
            self.api_base,
            self.auth_file,
            explicit_token=self.auth_token,
        )

        if should_attach_client_headers(self.api_base):
            org_code = read_auth_org_code(self.auth_file)
            if org_code:
                headers["X-Org-Code"] = org_code

        return request_auth_headers(
            auth_file=self.auth_file,
            explicit_token=self.auth_token,
            existing=headers,
            url=self.api_base,
        )

    def _uses_hosted_auth_json(self) -> bool:
        """Only recover credentials actually supplied by a hosted auth file."""
        return (
            bool(self.auth_file)
            and bool(read_auth_token_file(self.auth_file))
            and not self.auth_token.strip()
            and bool(_xiaohuanxiong_refresh_url(self.api_base))
            and self.api_key.strip() in HOSTED_AUTH_API_KEY_PLACEHOLDERS
        )

    async def _call_with_hosted_auth_retry(self, operation):
        """Recover a rejected file token once, before any stream is consumed."""
        from .error_messages import is_hosted_provider_auth_rejection

        await self._auth_headers()
        uses_file = self._uses_hosted_auth_json()
        rejected_token = read_auth_token_file(self.auth_file) if uses_file else None

        async def call_once():
            response = await operation()
            code = response.get("code") if isinstance(response, dict) else getattr(response, "code", None)
            # Streaming SDKs expose the HTTP response, not a parsed JSON envelope.
            # A gateway may reject authentication with JSON instead of SSE even
            # when stream=True. Inspect only JSON, before consuming any events.
            http_response = getattr(response, "response", None)
            if uses_file and isinstance(http_response, httpx.Response):
                content_type = http_response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type == "application/json" or content_type.endswith("+json"):
                    try:
                        await http_response.aread()
                        payload = http_response.json()
                    finally:
                        await http_response.aclose()
                    if isinstance(payload, dict) and payload.get("code") in (200003, "200003"):
                        raise httpx.HTTPStatusError(
                            "provider authorization_verify_error (200003)",
                            request=http_response.request, response=http_response,
                        )
            if uses_file and code in (200003, "200003"):
                raise ValueError("provider authorization_verify_error (200003)")
            return response

        try:
            return await call_once()
        except Exception as first:
            if not uses_file or not is_hosted_provider_auth_rejection(first):
                raise
            # Prefer the credential actually sent, including pre-request refresh.
            request = getattr(first, "request", None)
            headers = getattr(request, "headers", {})
            authorization = headers.get("Authorization", "")
            if authorization.startswith("Bearer "):
                rejected_token = authorization[7:]
            await refresh_hosted_auth_token_if_needed(
                self.api_base, self.auth_file, force=True,
                rejected_token=rejected_token,
            )
            try:
                return await call_once()
            except Exception as second:
                if is_hosted_provider_auth_rejection(second):
                    raise HostedAuthRequiredError("登录态已过期，请重新登录") from second
                raise

    @staticmethod
    def _agent_headers(
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> dict[str, str | bytes]:
        """Return non-empty agent correlation headers for one LLM request."""
        values = (
            ("X-RACCOON-Session-ID", session_id),
            ("X-RACCOON-Turn-ID", turn_id),
            ("X-RACCOON-Title", title),
            ("X-RACCOON-Call-Kind", call_kind),
        )
        return {
            header: cleaned if cleaned.isascii() else cleaned.encode("utf-8")
            for header, value in values
            if (cleaned := (value or "").strip())
        }

    @staticmethod
    def _session_header(session_id: str = "") -> dict[str, str | bytes]:
        """Backward-compatible helper for callers that only have a session id."""
        return LLMClientBase._agent_headers(session_id=session_id)

    def _request_headers(
        self,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> dict[str, str | bytes]:
        """Return correlation plus host client headers for one request."""
        if not should_attach_client_headers(self.api_base):
            return {}
        headers = self._agent_headers(session_id, turn_id, title, call_kind)
        headers.update(current_client_headers(self.api_base))
        return headers

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> LLMResponse:
        """Generate response from LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts
            thinking_enabled: When True, request extended thinking from the
                provider (Anthropic native, or Qwen-style ``enable_thinking``
                for OpenAI-compatible endpoints). Silent no-op for providers
                that don't support it.
            session_id: Optional caller-owned session id.
            turn_id: Optional caller-owned turn id.
            title: Optional trace title. Non-ASCII values are emitted as UTF-8
                header bytes so localized titles remain valid.

        Returns:
            LLMResponse containing the generated content, thinking, and tool calls
        """
        pass

    @abstractmethod
    async def generate_stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """Generate streaming response from LLM.

        Yields StreamEvent chunks for thinking/text deltas as they arrive.
        The final event has type="finish" and carries tool_calls + usage.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts
            thinking_enabled: See ``generate()``.
            session_id: See ``generate()``.
            turn_id: See ``generate()``.
            title: See ``generate()``.

        Yields:
            StreamEvent chunks
        """
        pass
        # Make it a valid async generator
        if False:  # pragma: no cover
            yield  # type: ignore[misc]

    @abstractmethod
    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request payload for the API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing the request payload
        """
        pass

    @abstractmethod
    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal message format to API-specific format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
        """
        pass
