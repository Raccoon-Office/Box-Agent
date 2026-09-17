"""Expose Box-Agent's direct web extractor as a stdio MCP server."""

from __future__ import annotations

from functools import lru_cache

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool as MCPFunctionTool

from box_agent.config import Config
from box_agent.llm import LLMClient
from box_agent.mcp_servers.web_extract import WebExtractTool
from box_agent.retry import RetryConfig
from box_agent.schema import LLMProvider


def _create_configured_llm() -> LLMClient:
    config = Config.load()
    retry = config.llm.retry
    provider = (
        LLMProvider.ANTHROPIC
        if config.llm.provider.lower() == "anthropic"
        else LLMProvider.OPENAI
    )
    return LLMClient(
        api_key=config.llm.api_key,
        provider=provider,
        api_base=config.llm.api_base,
        model=config.llm.model,
        retry_config=RetryConfig(
            enabled=retry.enabled,
            max_retries=retry.max_retries,
            initial_delay=retry.initial_delay,
            max_delay=retry.max_delay,
            exponential_base=retry.exponential_base,
        ),
        max_output_tokens=config.llm.max_output_tokens,
        max_request_body_bytes=config.llm.max_request_body_bytes,
        auth_file=config.llm.auth_file,
        timeout=config.llm.timeout,
        reasoning_effort_when_disabled=config.llm.reasoning_effort_when_disabled,
    )


@lru_cache(maxsize=1)
def _extractor() -> WebExtractTool:
    return WebExtractTool(llm=_create_configured_llm())


async def web_extract(
    url: str,
    model: str | None = None,
    max_output_tokens: int | None = None,
) -> str:
    """Fetch one URL and return extracted text or an LLM summary."""
    result = await _extractor().execute(
        url=url,
        model=model,
        max_output_tokens=max_output_tokens,
    )
    if not result.success:
        raise ValueError(result.error or "Web extraction failed")
    return result.content or ""


_mcp_tool = MCPFunctionTool.from_function(
    web_extract,
    name="web_extract",
    description=(
        "Fetch and extract text from one public web page. Pages over 5,000 "
        "characters are summarized with the configured Box-Agent model, or with "
        "the optional model supplied by the caller. JavaScript is not executed."
    ),
)
# Reuse the tool's explicit schema: FastMCP's inferred nullable unions can be
# rejected by model APIs. Keep the function metadata accepting legacy nulls.
_mcp_tool.parameters = WebExtractTool(llm=None).parameters
mcp = FastMCP(
    "box-agent-web-extract",
    instructions=(
        "Fetch public HTTP(S) pages without executing JavaScript. Long pages are "
        "summarized with the configured Box-Agent model or the model requested by "
        "the caller."
    ),
    tools=[_mcp_tool],
)


def main() -> None:
    """Run the bundled MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
