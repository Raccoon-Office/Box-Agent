from __future__ import annotations

from types import SimpleNamespace

import pytest

from box_agent.schema import FunctionCall, Message, ToolCall
from box_agent.tools.skillhub_search_tool import (
    HARD_CAPABILITY_GAP_PROMPT,
    SKILL_SOURCE_DISCOVERY_PROMPT,
    SkillHubSearchTool,
    capability_snapshot,
)


def _arguments(**overrides):
    values = {
        "requested_outcome": "Review a landscape construction drawing",
        "request_kind": "capability_gap",
        "missing_capability": "Professional landscape drawing review workflow",
        "gap_type": "missing_specialized_workflow",
        "fallback_assessment": (
            "Generic search cannot provide a professional sign-off contract"
        ),
        "queries": ["landscape review", "景观图纸审查"],
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_skillhub_search_requires_deferred_local_discovery_first() -> None:
    calls = []

    async def searcher(payload):
        calls.append(payload)
        return {"status": "empty", "items": []}

    tool = SkillHubSearchTool(
        searcher,
        snapshot_provider=lambda: {
            "tool_search_available": True,
            "tool_search_used": False,
        },
    )

    result = await tool.execute(**_arguments())

    assert not result.success
    assert result.error.startswith("LOCAL_DISCOVERY_REQUIRED:")
    assert calls == []


@pytest.mark.asyncio
async def test_skillhub_search_normalizes_found_candidates_and_never_installs() -> None:
    async def searcher(payload):
        if payload["query"] == "landscape review":
            return {"status": "empty", "items": []}
        assert payload == {
            "query": "景观图纸审查",
            "gapType": "missing_specialized_workflow",
            "limit": 3,
        }
        return {
            "status": "found",
            "items": [
                {
                    "id": "skill-1",
                    "slug": "landscape-review",
                    "name": "Landscape Review",
                    "description": "Review drawings",
                    "publisherDisplayName": "Publisher",
                    "currentVersion": "1.0.0",
                    "platforms": ["darwin", "win32"],
                    "riskLabels": ["professional"],
                    "downloadCount": 12,
                }
            ],
        }

    tool = SkillHubSearchTool(
        searcher,
        snapshot_provider=lambda: {
            "tool_search_available": True,
            "tool_search_used": True,
        },
    )

    result = await tool.execute(**_arguments())

    assert result.success
    assert "not installed" in result.content
    assert "skill_id='skill-1'" in result.model_context
    assert "slug='landscape-review'" in result.model_context
    assert result.raw_output == {
        "type": "skillhub_recommendations",
        "status": "found",
        "requestKind": "capability_gap",
        "query": "landscape review",
        "queries": ["landscape review", "景观图纸审查"],
        "searchedQueries": ["landscape review", "景观图纸审查"],
        "gapType": "missing_specialized_workflow",
        "missingCapability": "Professional landscape drawing review workflow",
        "items": [
            {
                "id": "skill-1",
                "slug": "landscape-review",
                "name": "Landscape Review",
                "description": "Review drawings",
                "publisherDisplayName": "Publisher",
                "currentVersion": "1.0.0",
                "platforms": ["darwin", "win32"],
                "riskLabels": ["professional"],
                "downloadCount": 12,
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_status", "expected_text"),
    [
        (
            {"status": "empty", "items": []},
            "empty",
            "returned no matching Skill",
        ),
        (
            {"status": "unavailable"},
            "unavailable",
            "could not be searched",
        ),
    ],
)
async def test_skillhub_search_preserves_empty_and_unavailable_states(
    response, expected_status, expected_text
) -> None:
    async def searcher(_payload):
        return response

    tool = SkillHubSearchTool(searcher)
    result = await tool.execute(**_arguments())

    assert result.success
    assert result.raw_output["status"] == expected_status
    assert expected_text in result.content
    assert result.raw_output["items"] == []
    assert SKILL_SOURCE_DISCOVERY_PROMPT in result.model_context

    if expected_status == "empty":
        assert "scoped only to the Skill marketplace" in result.model_context
        assert "Do not end the turn" in result.model_context
        assert "broader discovery" in result.model_context


@pytest.mark.asyncio
async def test_explicit_marketplace_request_skips_deferred_tool_discovery() -> None:
    calls = []

    async def searcher(payload):
        calls.append(payload)
        return {"status": "empty", "items": []}

    tool = SkillHubSearchTool(
        searcher,
        snapshot_provider=lambda: {
            "tool_search_available": True,
            "tool_search_used": False,
        },
    )

    result = await tool.execute(
        **_arguments(request_kind="explicit_marketplace_request")
    )

    assert result.success
    assert len(calls) == 2
    assert result.raw_output["requestKind"] == "explicit_marketplace_request"


def test_marketplace_prompt_uses_product_name_and_scopes_direct_sources() -> None:
    normalized_prompt = " ".join(HARD_CAPABILITY_GAP_PROMPT.split())

    assert "Skill marketplace capability-gap fallback" in normalized_prompt
    assert "repository URL" in normalized_prompt
    assert "not an explicit marketplace request" in normalized_prompt
    assert "SkillHub" not in normalized_prompt


@pytest.mark.parametrize(
    "guidance",
    [
        "do not assume marketplace-only intent",
        "verify the repository and SKILL.md",
        "No separate request for broad discovery is needed",
        "only when the user explicitly limits the source to it",
        "Inspect a user-supplied source first",
        "must not bypass a denied installation authorization",
        "Never invent a marketplace skill_id for an external source",
    ],
)
def test_marketplace_prompt_preserves_source_and_authorization_boundaries(guidance) -> None:
    assert guidance in " ".join(HARD_CAPABILITY_GAP_PROMPT.split())


def test_marketplace_search_schema_does_not_imply_marketplace_only_intent() -> None:
    async def searcher(_payload):
        return {"status": "empty", "items": []}

    tool = SkillHubSearchTool(searcher)
    assert "unspecified-source" in tool.description
    assert "does not imply marketplace-only intent" in (
        tool.parameters["properties"]["request_kind"]["description"]
    )


@pytest.mark.asyncio
async def test_skillhub_search_uses_short_aliases_and_deduplicates_results() -> None:
    calls = []

    async def searcher(payload):
        calls.append(payload["query"])
        if payload["query"] == "TTS":
            return {
                "status": "found",
                "items": [
                    {
                        "id": "skill-tts",
                        "slug": "edge-tts",
                        "name": "Edge TTS",
                    }
                ],
            }
        return {
            "status": "found",
            "items": [
                {
                    "id": "skill-tts",
                    "slug": "edge-tts",
                    "name": "Edge TTS",
                }
            ],
        }

    tool = SkillHubSearchTool(searcher)
    result = await tool.execute(
        **_arguments(queries=["TTS", "语音合成", "text-to-speech"])
    )

    assert result.success
    assert calls == ["TTS", "语音合成", "text-to-speech"]
    assert result.raw_output["queries"] == ["TTS", "语音合成", "text-to-speech"]
    assert [item["slug"] for item in result.raw_output["items"]] == ["edge-tts"]


@pytest.mark.asyncio
async def test_install_capable_search_retains_exact_candidate_for_follow_up() -> None:
    async def searcher(_payload):
        return {
            "status": "found",
            "items": [
                {
                    "id": "skill-tts",
                    "slug": "edge-tts",
                    "name": "Edge TTS",
                }
            ],
        }

    tool = SkillHubSearchTool(searcher, installation_available=True)

    result = await tool.execute(
        **_arguments(request_kind="explicit_marketplace_request")
    )

    assert result.success
    assert "Immediately call install_skillhub_skill" in result.content
    assert "Do not ask for confirmation in prose" in result.content
    assert "do not present prose-only letter choices" in result.content
    assert tool.candidate("skill-tts")["slug"] == "edge-tts"
    assert tool.candidates() == [result.raw_output["items"][0]]


@pytest.mark.asyncio
async def test_skillhub_search_rejects_sensitive_query_and_second_search() -> None:
    calls = []

    async def searcher(payload):
        calls.append(payload)
        return {"status": "empty", "items": []}

    tool = SkillHubSearchTool(searcher)
    unsafe = await tool.execute(
        **_arguments(queries=["landscape review", "alice@example.com"])
    )
    first = await tool.execute(**_arguments())
    second = await tool.execute(
        **_arguments(queries=["another review", "另一种审查"])
    )

    assert unsafe.error.startswith("UNSAFE_MARKET_QUERY:")
    assert first.success
    assert second.error.startswith("SEARCH_ALREADY_PERFORMED:")
    assert [call["query"] for call in calls] == [
        "landscape review",
        "景观图纸审查",
    ]


def test_capability_snapshot_detects_tool_search_in_current_turn() -> None:
    user = Message(role="user", content="do this")
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[
            ToolCall(
                id="search-1",
                type="function",
                function=FunctionCall(name="tool_search", arguments={"query": "email"}),
            )
        ],
    )
    tool_result = Message(role="tool", content="none", tool_call_id="search-1")
    agent = SimpleNamespace(
        tools={"tool_search": object(), "search_skillhub": object()},
        messages=[Message(role="system", content="system"), user, assistant, tool_result],
    )
    loader = SimpleNamespace(list_skills=lambda: ["pptx", "data-dashboard"])

    snapshot = capability_snapshot(agent, loader)

    assert snapshot["tool_search_available"] is True
    assert snapshot["tool_search_used"] is True
    assert snapshot["skill_names"] == ["data-dashboard", "pptx"]
