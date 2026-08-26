"""Resolve an enabled Skill provider for presentation authoring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from box_agent.delivery import has_deliverable_intent
from box_agent.tools.skill_loader import Skill, SkillLoader

from .presentation_contract import WORKFLOW_KIND


PRESENTATION_AUTHORING_CAPABILITY = "presentation.authoring"

_PRESENTATION_SIGNALS = (
    "ppt",
    "pptx",
    "powerpoint",
    "slide",
    "slides",
    "presentation",
    "幻灯片",
    "演示文稿",
)
_AUTHORING_SIGNALS = (
    "create",
    "generate",
    "make",
    "build",
    "author",
    "edit",
    "modify",
    "创建",
    "制作",
    "生成",
    "编辑",
    "修改",
)
_NON_AUTHORING_NAME_SIGNALS = ("outline", "大纲")
_NON_AUTHORING_DESCRIPTION_SIGNALS = (
    "do not use for creating",
    "not for creating",
    "不要用于创建",
    "不用于创建",
)
_NON_AUTHORING_PRESENTATION_RE = re.compile(
    r"(?:不负责|不支持).{0,80}?(?:原生)?(?:pptx?|powerpoint|幻灯片|演示文稿)"
    r"(?:创建|制作|生成|authoring|creation)",
    re.IGNORECASE,
)
_ROUTING_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
_GENERIC_PROVIDER_TOKENS = frozenset(
    {
        "ppt",
        "pptx",
        "powerpoint",
        "slide",
        "slides",
        "deck",
        "presentation",
        "create",
        "generate",
        "make",
        "build",
        "author",
        "edit",
        "modify",
        "to",
        "for",
        "and",
        "with",
        "use",
        "using",
        "the",
        "幻灯",
        "灯片",
        "演示",
        "示文",
        "文稿",
        "创建",
        "制作",
        "生成",
        "编辑",
        "修改",
        "新建",
        "帮我",
        "一个",
        "一份",
        "使用",
        "需要",
        "用于",
        "可以",
        "用户",
    }
)
_LARK_PROVIDER_RE = re.compile(r"(?:飞书|feishu|\blark\b)", re.IGNORECASE)


@dataclass(frozen=True)
class HostPresentationConfig:
    intent: str
    confirmed_by: str
    preferred_skill: str | None = None


@dataclass(frozen=True)
class PresentationSkillProvider:
    skill_name: str
    workflow: str | None
    declared_capability: bool
    source: str

    @property
    def uses_controlled_workflow(self) -> bool:
        return self.workflow == WORKFLOW_KIND


def parse_host_presentation_config(
    prompt_meta: Mapping[str, Any] | None,
) -> HostPresentationConfig | None:
    """Parse the trusted ACP metadata emitted by an office presentation preflight."""
    if not isinstance(prompt_meta, Mapping):
        return None
    raw = prompt_meta.get("presentation_config")
    if not isinstance(raw, Mapping):
        return None

    intent = raw.get("intent")
    confirmed_by = raw.get("confirmed_by", raw.get("confirmedBy"))
    schema_version = raw.get("schema_version", raw.get("schemaVersion"))
    if intent != "create" or confirmed_by not in {"user", "timeout", "implicit"}:
        return None
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        return None

    preferred = raw.get("preferred_skill", raw.get("preferredSkill"))
    preferred_skill = preferred.strip() if isinstance(preferred, str) else None
    return HostPresentationConfig(
        intent=intent,
        confirmed_by=confirmed_by,
        preferred_skill=preferred_skill or None,
    )


def _legacy_presentation_authoring_skill(skill: Skill) -> bool:
    """Recognize old presentation Skills without requiring a manifest migration."""
    name = skill.name.casefold()
    description = skill.description.casefold()
    keywords = " ".join(skill.keywords or []).casefold()
    routing_text = f"{name} {keywords} {description}"
    if any(signal in name for signal in _NON_AUTHORING_NAME_SIGNALS):
        return False
    if any(signal in description for signal in _NON_AUTHORING_DESCRIPTION_SIGNALS):
        return False
    if _NON_AUTHORING_PRESENTATION_RE.search(description):
        return False
    if name == "pptx":
        return True
    return any(signal in routing_text for signal in _PRESENTATION_SIGNALS) and any(
        signal in description for signal in _AUTHORING_SIGNALS
    )


def _routing_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for chunk in _ROUTING_TOKEN_RE.findall(text.casefold()):
        if "\u4e00" <= chunk[0] <= "\u9fff":
            if len(chunk) >= 2:
                tokens.add(chunk)
            tokens.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
        elif len(chunk) >= 2:
            tokens.add(chunk)
    return tokens


def _has_provider_specific_match(skill: Skill, query: str | None) -> bool:
    if not query or not query.strip():
        return False
    query_tokens = _routing_tokens(query) - _GENERIC_PROVIDER_TOKENS
    if not query_tokens:
        return False
    provider_tokens = _routing_tokens(
        f"{skill.name} {' '.join(skill.keywords or [])} {skill.description}"
    ) - _GENERIC_PROVIDER_TOKENS
    return bool(query_tokens & provider_tokens)


def _query_explicitly_names_provider(skill: Skill, query: str | None) -> bool:
    """Return whether the query names this provider beyond generic PPT terms."""
    if not query or not query.strip():
        return False
    query_tokens = _routing_tokens(query) - _GENERIC_PROVIDER_TOKENS
    name_tokens = _routing_tokens(skill.name) - _GENERIC_PROVIDER_TOKENS
    return bool(name_tokens) and name_tokens <= query_tokens


def _is_lark_presentation_provider(skill: Skill) -> bool:
    routing_text = f"{skill.name} {' '.join(skill.keywords or [])} {skill.description}"
    return _LARK_PROVIDER_RE.search(routing_text) is not None


def _query_requests_lark(query: str | None) -> bool:
    return bool(query and _LARK_PROVIDER_RE.search(query))


def presentation_provider_for_skill(skill: Skill | None) -> PresentationSkillProvider | None:
    if skill is None or skill.broken:
        return None
    declared = PRESENTATION_AUTHORING_CAPABILITY in (skill.capabilities or [])
    if not declared and not _legacy_presentation_authoring_skill(skill):
        return None
    workflow = skill.workflow
    if workflow is None and skill.name == "pptx" and skill.source == "builtin":
        workflow = WORKFLOW_KIND
    return PresentationSkillProvider(
        skill_name=skill.name,
        workflow=workflow,
        declared_capability=declared,
        source=skill.source,
    )


def resolve_presentation_skill_provider(
    skill_loader: SkillLoader,
    matched_skill_names: tuple[str, ...] = (),
    *,
    preferred_skill: str | None = None,
    query: str | None = None,
) -> PresentationSkillProvider | None:
    """Resolve a provider without reviving disabled Skills.

    Explicit host preference wins. Otherwise normal routing order selects the
    first eligible matched presentation provider, so a more specific installed
    Skill can replace the generic builtin without every presentation integration
    becoming the global default. Lark/Feishu presentation providers are scoped:
    they are eligible only when the query explicitly names Lark or Feishu. The
    builtin ``pptx`` remains the compatibility default. Legacy Skills without
    capability fields are eligible only when normal routing has already matched
    them (or the host explicitly names them).
    """

    def provider(name: str | None) -> PresentationSkillProvider | None:
        if not name:
            return None
        return presentation_provider_for_skill(skill_loader.get_skill(name))

    preferred_provider = provider(preferred_skill)
    if preferred_provider is not None:
        return preferred_provider

    matched_providers = [
        (skill, candidate)
        for name in matched_skill_names
        if (skill := skill_loader.get_skill(name)) is not None
        if (candidate := presentation_provider_for_skill(skill)) is not None
    ]
    requests_lark = _query_requests_lark(query)
    if requests_lark:
        for skill, candidate in matched_providers:
            if _is_lark_presentation_provider(skill):
                return candidate
        for name in skill_loader.list_skills():
            skill = skill_loader.get_skill(name)
            candidate = presentation_provider_for_skill(skill)
            if (
                skill is not None
                and candidate is not None
                and _is_lark_presentation_provider(skill)
            ):
                return candidate

    eligible_matched_providers = [
        (skill, candidate)
        for skill, candidate in matched_providers
        if not _is_lark_presentation_provider(skill)
    ]
    for skill, candidate in eligible_matched_providers:
        if _query_explicitly_names_provider(skill, query):
            return candidate
    for skill, candidate in eligible_matched_providers:
        if _has_provider_specific_match(skill, query):
            return candidate
    if eligible_matched_providers:
        return eligible_matched_providers[0][1]

    default_provider = provider("pptx")
    if default_provider is not None:
        return default_provider

    declared_providers: list[PresentationSkillProvider] = []
    for name in skill_loader.list_skills():
        skill = skill_loader.get_skill(name)
        candidate = presentation_provider_for_skill(skill)
        if (
            skill is not None
            and candidate is not None
            and candidate.declared_capability
            and not _is_lark_presentation_provider(skill)
        ):
            declared_providers.append(candidate)
    return declared_providers[0] if declared_providers else None


def resolve_query_matched_presentation_skill_provider(
    skill_loader: SkillLoader,
    query: str | None,
) -> PresentationSkillProvider | None:
    """Resolve a presentation provider selected by normal Skill matching.

    The regular catalog filter intentionally caps the metadata shown to the
    model. Provider routing must inspect every enabled Skill so a uniquely
    relevant presentation provider is not dropped behind several broad
    matches. Unlike :func:`resolve_presentation_skill_provider`, this helper
    does not fall back to the builtin provider when the query matched no
    presentation Skill.
    """
    if not query or not query.strip() or not has_deliverable_intent(query):
        return None
    max_skills = max(16, len(skill_loader.list_skills()))
    matched_skill_names = tuple(
        skill.name
        for skill in skill_loader.filter_by_query(query, max_skills=max_skills)
    )
    provider = resolve_presentation_skill_provider(
        skill_loader,
        matched_skill_names,
        query=query,
    )
    if provider is None or provider.skill_name not in matched_skill_names:
        return None
    return provider
