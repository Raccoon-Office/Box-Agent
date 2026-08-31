"""ACP host-backed SkillHub search for confirmed hard capability gaps."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .base import Tool, ToolResult

SKILLHUB_RECOMMENDATIONS_TYPE = "skillhub_recommendations"
SKILLHUB_SEARCH_METHOD = "session/skillhub_search"
SKILLHUB_SEARCH_CAPABILITY_VERSION = 1

SEARCH_REQUEST_KINDS = (
    "capability_gap",
    "explicit_install_request",
)

GAP_TYPES = (
    "missing_connector",
    "missing_tool_or_runtime",
    "missing_artifact_format",
    "missing_specialized_workflow",
    "missing_domain_knowledge",
)

_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)(?!\d)")
_ABSOLUTE_PATH_RE = re.compile(r"(?:^|\s)(?:/[^\s]+|[A-Za-z]:[\\/][^\s]+|~[/\\][^\s]+)")
_URL_RE = re.compile(r"https?://|file://", re.IGNORECASE)

CapabilitySnapshotProvider = Callable[[], Mapping[str, Any]]
SkillHubSearcher = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


HARD_CAPABILITY_GAP_PROMPT = """## SkillHub capability-gap fallback
When the user requests an executable outcome that the currently installed Skills and
available tools cannot faithfully deliver, first use `tool_search` if deferred MCP
tools are available. If a hard capability gap still remains, call `search_skillhub`
with `request_kind=capability_gap` once before concluding the turn. Missing input,
ambiguity, authentication, permission, unsupported platform, and transient failures
are not hard capability gaps.

When the user explicitly asks to find or install a Skill from the product's SkillHub,
call `search_skillhub` with `request_kind=explicit_install_request`; this direct market
request does not require `tool_search` first. Never use `get_skill` to install anything:
it only loads Skills that are already installed.

SkillHub search is read-only and never installs a Skill. Search with 2-5 short,
independent, non-sensitive capability keywords: include the user's language plus an
English synonym or known tool/Skill alias when possible. Prefer terms such as `TTS`,
`text-to-speech`, and `语音合成` over one long task description. Never send conversation
text, file contents, paths, credentials, contact details, or personal data. If candidates
are found and `install_skillhub_skill` is available, call it with the chosen candidate's
exact `skill_id`; that tool owns the mandatory user confirmation. Do not offer prose-only
lettered installation choices. If several candidates are genuinely plausible, use
`request_user_decision` to select one, end that turn, then install the selected ID after
the host resumes the task. If installation is unavailable, direct the user to the host's
SkillHub card. If installation is denied or fails, respect that result and do not bypass
it with package managers, shell commands, browser automation, or an unrelated installer.
If every search succeeds with no matches, explicitly say no matching Skill was found,
then provide the safest useful best-effort answer. If search is unavailable, say it could
not be performed, then provide the same bounded fallback. In legal, medical, financial,
compliance, engineering-signoff, or other high-risk domains, best effort is informational
only and must not fabricate a professional verdict, approval, guarantee, or validated
deliverable.
"""


def _bounded_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _query_is_safe(query: str) -> bool:
    return not (
        "\n" in query
        or "\r" in query
        or _EMAIL_RE.search(query)
        or _PHONE_RE.search(query)
        or _ABSOLUTE_PATH_RE.search(query)
        or _URL_RE.search(query)
    )


def _normalize_candidate(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    skill_id = _bounded_text(value.get("id"), limit=80)
    slug = _bounded_text(value.get("slug"), limit=80)
    name = _bounded_text(value.get("name"), limit=160)
    if not skill_id or not slug or not name:
        return None
    platforms = value.get("platforms")
    risk_labels = value.get("riskLabels")
    download_count = value.get("downloadCount")
    return {
        "id": skill_id,
        "slug": slug,
        "name": name,
        "description": _bounded_text(value.get("description"), limit=500),
        "publisherDisplayName": _bounded_text(
            value.get("publisherDisplayName"), limit=160
        ),
        "currentVersion": _bounded_text(value.get("currentVersion"), limit=40),
        "platforms": [
            item[:40]
            for item in platforms[:8]
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(platforms, list)
        else [],
        "riskLabels": [
            item[:80]
            for item in risk_labels[:8]
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(risk_labels, list)
        else [],
        "downloadCount": (
            download_count
            if isinstance(download_count, int) and not isinstance(download_count, bool)
            and download_count >= 0
            else 0
        ),
    }


class SkillHubSearchTool(Tool):
    """Search SkillHub for a hard gap or an explicit market-install request."""

    parallel_safe = False
    max_result_size_chars = 8_000

    def __init__(
        self,
        searcher: SkillHubSearcher,
        *,
        snapshot_provider: CapabilitySnapshotProvider | None = None,
        timeout_seconds: float = 8.0,
        installation_available: bool = False,
    ) -> None:
        self._searcher = searcher
        self._snapshot_provider = snapshot_provider or (lambda: {})
        self._timeout_seconds = timeout_seconds
        self._installation_available = installation_available
        self._searched_this_turn = False
        self._candidates_by_id: dict[str, dict[str, Any]] = {}

    def set_snapshot_provider(self, provider: CapabilitySnapshotProvider) -> None:
        self._snapshot_provider = provider

    def reset_turn(self) -> None:
        self._searched_this_turn = False

    def candidate(self, skill_id: str) -> dict[str, Any] | None:
        candidate = self._candidates_by_id.get(skill_id.strip())
        return dict(candidate) if candidate is not None else None

    def candidates(self) -> list[dict[str, Any]]:
        return [dict(candidate) for candidate in self._candidates_by_id.values()]

    @property
    def name(self) -> str:
        return "search_skillhub"

    @property
    def description(self) -> str:
        return (
            "Search SkillHub once either after a hard capability gap has been confirmed "
            "or because the user explicitly asked to find or install a market Skill. "
            "Capability-gap searches require relevant deferred MCP discovery first; direct "
            "market-install requests do not. Do not use for missing input, ambiguity, auth, "
            "permission, platform, transient failures, or optional quality upgrades. Search "
            "is read-only; never claim a candidate is installed and never install without "
            "user confirmation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "requested_outcome": {
                    "type": "string",
                    "description": "Short description of the executable outcome requested.",
                    "minLength": 2,
                    "maxLength": 300,
                },
                "request_kind": {
                    "type": "string",
                    "enum": list(SEARCH_REQUEST_KINDS),
                    "description": (
                        "Use capability_gap after local discovery proves an executable "
                        "capability is missing. Use explicit_install_request when the user "
                        "directly asks to find or install a Skill from SkillHub."
                    ),
                },
                "missing_capability": {
                    "type": "string",
                    "description": "The indispensable capability that is not available.",
                    "minLength": 2,
                    "maxLength": 300,
                },
                "gap_type": {
                    "type": "string",
                    "enum": list(GAP_TYPES),
                },
                "fallback_assessment": {
                    "type": "string",
                    "description": (
                        "Why current capabilities cannot faithfully meet the hard constraints."
                    ),
                    "minLength": 2,
                    "maxLength": 500,
                },
                "queries": {
                    "type": "array",
                    "description": (
                        "2-5 independent sanitized capability keywords. Include the user's "
                        "language and an English synonym or known alias. Use short terms, not "
                        "one detailed task phrase; no user text, paths, files, URLs, credentials, "
                        "contact details, or personal data."
                    ),
                    "minItems": 2,
                    "maxItems": 5,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "minLength": 2,
                        "maxLength": 60,
                    },
                },
            },
            "required": [
                "requested_outcome",
                "request_kind",
                "missing_capability",
                "gap_type",
                "fallback_assessment",
                "queries",
            ],
            "additionalProperties": False,
        }

    async def execute(
        self,
        requested_outcome: str,
        request_kind: str,
        missing_capability: str,
        gap_type: str,
        fallback_assessment: str,
        queries: list[str],
    ) -> ToolResult:
        requested_outcome = requested_outcome.strip()
        missing_capability = missing_capability.strip()
        fallback_assessment = fallback_assessment.strip()
        normalized_queries = []
        for query in queries if isinstance(queries, list) else []:
            if not isinstance(query, str):
                continue
            normalized = query.strip()
            if normalized and normalized not in normalized_queries:
                normalized_queries.append(normalized)

        if request_kind not in SEARCH_REQUEST_KINDS:
            return self._guard_error(
                "INVALID_SEARCH_REQUEST",
                "Unsupported SkillHub search request kind.",
            )
        if gap_type not in GAP_TYPES:
            return self._guard_error("NOT_CAPABILITY_GAP", "Unsupported capability-gap type.")
        if not all((requested_outcome, missing_capability, fallback_assessment)):
            return self._guard_error(
                "NOT_CAPABILITY_GAP",
                "The requested outcome, missing capability, and fallback assessment are required.",
            )
        if (
            not 2 <= len(normalized_queries) <= 5
            or any(
                not 2 <= len(query) <= 60 or not _query_is_safe(query)
                for query in normalized_queries
            )
        ):
            return self._guard_error(
                "UNSAFE_MARKET_QUERY",
                "Provide 2-5 short, independent, non-sensitive capability keywords and try once.",
            )
        if self._searched_this_turn:
            return self._guard_error(
                "SEARCH_ALREADY_PERFORMED",
                "Do not search SkillHub again this turn. Continue with the existing result or best effort.",
            )

        snapshot = dict(self._snapshot_provider() or {})
        if (
            request_kind == "capability_gap"
            and snapshot.get("tool_search_available")
            and not snapshot.get("tool_search_used")
        ):
            return self._guard_error(
                "LOCAL_DISCOVERY_REQUIRED",
                "Use tool_search once to check deferred MCP capabilities before SkillHub.",
            )

        self._searched_this_turn = True

        async def search_queries() -> tuple[list[dict[str, Any]], list[str], bool]:
            candidates: list[dict[str, Any]] = []
            candidate_ids: set[str] = set()
            searched_queries: list[str] = []
            unavailable = False
            for query in normalized_queries:
                searched_queries.append(query)
                payload = {
                    "query": query,
                    "gapType": gap_type,
                    "limit": 3,
                }
                try:
                    response = await self._searcher(payload)
                except Exception:
                    unavailable = True
                    continue
                if not isinstance(response, dict):
                    unavailable = True
                    continue
                status = response.get("status")
                if status == "unavailable":
                    unavailable = True
                    continue
                raw_items = response.get("items")
                if status != "found" or not isinstance(raw_items, list):
                    continue
                for item in raw_items:
                    candidate = _normalize_candidate(item)
                    if candidate is None or candidate["id"] in candidate_ids:
                        continue
                    candidate_ids.add(candidate["id"])
                    candidates.append(candidate)
                    if len(candidates) == 3:
                        return candidates, searched_queries, unavailable
            return candidates, searched_queries, unavailable

        try:
            candidates, searched_queries, unavailable = await asyncio.wait_for(
                search_queries(), timeout=self._timeout_seconds
            )
        except Exception:
            candidates = []
            searched_queries = []
            unavailable = True

        if candidates:
            self._candidates_by_id = {
                candidate["id"]: dict(candidate) for candidate in candidates
            }
            names = ", ".join(candidate["name"] for candidate in candidates)
            installation_guidance = (
                "Immediately call install_skillhub_skill with the selected candidate's exact "
                "skill_id. Do not ask for confirmation in prose and do not end the turn first: "
                "that tool owns the single mandatory host confirmation. Do not call get_skill "
                "before installation succeeds and do not present prose-only letter choices."
                if self._installation_available
                else (
                    "Installation is not available in this host. Direct the user to the "
                    "SkillHub recommendation card to review and confirm installation."
                )
            )
            content = (
                f"SkillHub found {len(candidates)} candidate Skill(s): {names}. "
                f"They are not installed. {installation_guidance} You may also provide "
                "bounded interim help without claiming the missing capability is available."
            )
            candidate_context = "\n".join(
                f"- skill_id={candidate['id']!r}; slug={candidate['slug']!r}; "
                f"name={candidate['name']!r}"
                for candidate in candidates
            )
            model_context = (
                f"{content}\n\nExact SkillHub candidates returned for this session:\n"
                f"{candidate_context}\nUse the exact skill_id value above; never substitute "
                "the name, slug, translated text, or a guessed identifier."
            )
            normalized_status = "found"
        elif not unavailable:
            self._candidates_by_id = {}
            query_summary = ", ".join(repr(query) for query in searched_queries)
            content = (
                f"SkillHub was searched for {query_summary} and returned no matching Skill. "
                f"Say this explicitly, then use existing capabilities to help as far as safely "
                f"possible. State that the missing capability is: {missing_capability}. "
                "For high-risk work, provide informational guidance only and no professional verdict."
            )
            normalized_status = "empty"
            candidates = []
            model_context = content
        else:
            self._candidates_by_id = {}
            content = (
                "SkillHub could not be searched right now. Do not say that no matching Skill "
                f"exists. Continue with the safest best-effort response and state that the "
                f"missing capability is: {missing_capability}. For high-risk work, provide "
                "informational guidance only and no professional verdict."
            )
            normalized_status = "unavailable"
            candidates = []
            model_context = content

        return ToolResult(
            success=True,
            content=content,
            model_context=model_context,
            raw_output={
                "type": SKILLHUB_RECOMMENDATIONS_TYPE,
                "status": normalized_status,
                "requestKind": request_kind,
                "query": searched_queries[0] if searched_queries else normalized_queries[0],
                "queries": normalized_queries,
                "searchedQueries": searched_queries,
                "gapType": gap_type,
                "missingCapability": missing_capability[:300],
                "items": candidates,
            },
        )

    @staticmethod
    def _guard_error(code: str, message: str) -> ToolResult:
        return ToolResult(
            success=False,
            error=f"{code}: {message}",
            raw_output={
                "type": SKILLHUB_RECOMMENDATIONS_TYPE,
                "status": "rejected",
                "code": code,
            },
        )


def capability_snapshot(agent: Any, skill_loader: Any | None) -> dict[str, Any]:
    """Return objective session evidence used by the pre-search guard."""

    tool_names = sorted(str(name) for name in getattr(agent, "tools", {}))
    tool_search_used = False
    for message in reversed(getattr(agent, "messages", [])):
        if getattr(message, "role", "") == "user":
            break
        for tool_call in getattr(message, "tool_calls", None) or ():
            function = getattr(tool_call, "function", None)
            if getattr(function, "name", "") == "tool_search":
                tool_search_used = True
                break
        if tool_search_used:
            break

    skill_names: list[str] = []
    if skill_loader is not None:
        try:
            skill_names = sorted(skill_loader.list_skills())
        except Exception:
            skill_names = []

    return {
        "tool_names": tool_names,
        "skill_names": skill_names,
        "tool_search_available": "tool_search" in tool_names,
        "tool_search_used": tool_search_used,
    }
