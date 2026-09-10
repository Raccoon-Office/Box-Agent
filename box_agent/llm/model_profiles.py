"""Session-scoped LLM profile registry.

OfficeV3 stores immutable profile revisions in a local, owner-readable registry.
ACP bindings carry only the non-secret profile identity; provider credentials are
resolved here and never copied into session metadata or traces.
Missing revisions may resolve within the same profile when the available route
is unambiguous; existing revisions remain pinned.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from box_agent.schema import LLMProvider

from .llm_wrapper import LLMClient

from box_agent.user_paths import state_path


REGISTRY_VERSION = 1
_log = logging.getLogger(__name__)


class ModelProfileUnavailable(ValueError):
    """Raised when a requested immutable profile revision cannot be resolved."""


def default_model_profile_registry_path() -> Path:
    configured = os.environ.get("BOX_AGENT_MODEL_PROFILES_FILE", "").strip()
    if configured:
        return state_path("config/model-profiles.json", configured)
    return state_path('config/model-profiles.json')


def _positive_int(value: Any, *, field: str, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelProfileUnavailable(f"model profile {field} is invalid")
    return value


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelProfileUnavailable(f"model profile {field} is invalid")
    return value.strip()


def load_model_profile_revision(
    profile_revision: str,
    *,
    registry_path: Path | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Load a revision, optionally recovering a missing revision within its profile."""

    revision = _required_text(profile_revision, field="revision")
    path = registry_path or default_model_profile_registry_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelProfileUnavailable(
            f"model profile registry is unavailable: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, Mapping) or payload.get("version") != REGISTRY_VERSION:
        raise ModelProfileUnavailable("model profile registry version is unsupported")
    profiles = payload.get("profiles")
    if not isinstance(profiles, Mapping):
        raise ModelProfileUnavailable("model profile registry is invalid")
    if revision not in profiles and profile_id is not None:
        # Compatibility: old conversations may outlive their local revision.
        # Only use valid records for the same profile, with one unambiguous route.
        expected_id = _required_text(profile_id, field="profileId")
        candidates: list[tuple[str, int, dict[str, Any]]] = []
        for order, (key, candidate) in enumerate(profiles.items()):
            if not isinstance(candidate, Mapping) or candidate.get("profileId") != expected_id:
                continue
            try:
                validated = _validate_model_profile(key, candidate)
            except (TypeError, ValueError):
                continue
            created_at = candidate.get("createdAt")
            # OfficeV3 writes createdAt in UTC ISO format; registry order breaks ties.
            candidates.append((created_at if isinstance(created_at, str) else "", order, validated))
        routes = {(p["provider"], p["apiBase"]) for _, _, p in candidates}
        if len(routes) > 1:
            raise ModelProfileUnavailable(
                f"model profile revision recovery is ambiguous for profile id: {expected_id}"
            )
        if candidates:
            profile = max(candidates, key=lambda item: item[:2])[2]
            _log.info(
                "model_profile/revision_recovered profile_id=%s requested_revision=%s resolved_revision=%s",
                expected_id, revision, profile["profileRevision"],
            )
            return profile
    profile = profiles.get(revision)
    if not isinstance(profile, Mapping):
        raise ModelProfileUnavailable(f"model profile revision is unavailable: {revision}")
    return _validate_model_profile(revision, profile)


def _validate_model_profile(revision: str, profile: Mapping[str, Any]) -> dict[str, Any]:
    provider = _required_text(profile.get("provider"), field="provider").lower()
    if provider not in {LLMProvider.OPENAI.value, LLMProvider.ANTHROPIC.value}:
        raise ModelProfileUnavailable(f"model profile provider is unsupported: {provider}")
    api_base = _required_text(profile.get("apiBase"), field="apiBase").rstrip("/")
    default_model = _required_text(profile.get("defaultModel"), field="defaultModel")
    profile_id = _required_text(profile.get("profileId"), field="profileId")
    if _required_text(profile.get("profileRevision"), field="profileRevision") != revision:
        raise ModelProfileUnavailable("model profile revision identity is inconsistent")

    return {
        "profileId": profile_id,
        "profileRevision": revision,
        "provider": provider,
        "apiBase": api_base,
        "apiKey": str(profile.get("apiKey") or ""),
        "authFile": str(profile.get("authFile") or ""),
        "defaultModel": default_model,
        "contextWindow": _positive_int(
            profile.get("contextWindow"), field="contextWindow", default=180_000
        ),
        "maxTokens": _positive_int(
            profile.get("maxTokens"), field="maxTokens", default=63_999
        ),
        "timeout": float(profile.get("timeout") or 1200.0),
    }


def client_for_model_profile(
    binding: Mapping[str, Any],
    *,
    fallback_client: LLMClient,
) -> LLMClient:
    """Construct an isolated client for a v2 Session profile binding."""

    expected_profile_id = _required_text(binding.get("profileId"), field="profileId")
    profile = load_model_profile_revision(
        str(binding.get("profileRevision") or ""), profile_id=expected_profile_id
    )
    if profile["profileId"] != expected_profile_id:
        raise ModelProfileUnavailable("model profile id does not match its revision")

    model = _required_text(binding.get("model"), field="model")
    max_tokens = binding.get("maxTokens", profile["maxTokens"])
    max_output_tokens = _positive_int(max_tokens, field="maxTokens")
    provider = LLMProvider(profile["provider"])
    return LLMClient(
        api_key=profile["apiKey"],
        provider=provider,
        api_base=profile["apiBase"],
        model=model,
        retry_config=getattr(fallback_client, "retry_config", None),
        max_output_tokens=max_output_tokens,
        auth_file=profile["authFile"],
        timeout=profile["timeout"],
    )


__all__ = [
    "ModelProfileUnavailable",
    "client_for_model_profile",
    "default_model_profile_registry_path",
    "load_model_profile_revision",
]
