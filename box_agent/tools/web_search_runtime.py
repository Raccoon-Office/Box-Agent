"""Search normalization, ranking, deduplication, and model-result metadata."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Final
from urllib.parse import urlsplit

from ..evidence import normalize_search_url as _normalize_search_url

_log = logging.getLogger("box_agent.core")


_WEB_SEARCH_IMAGE_URL_KEYS: Final[tuple[str, ...]] = (
    "image_url",
    "imageUrl",
    "ImageUrl",
    "thumbnail",
    "thumbnail_url",
    "thumbnailUrl",
)

_WEB_SEARCH_IMAGE_LIST_KEYS: Final[tuple[str, ...]] = (
    "images",
    "Images",
    "image_urls",
    "imageUrls",
)


def _web_search_http_url(value: Any) -> str:
    """Return an HTTP(S) URL without rewriting signed image query strings."""
    url = str(value or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return ""
    return url


def _web_search_image_detail(
    value: Any,
    *,
    allow_plain_url: bool = False,
) -> dict[str, Any] | None:
    if isinstance(value, str):
        url = _web_search_http_url(value)
        return {"url": url} if url else None
    if not isinstance(value, dict):
        return None

    nested = _first_present(value, ("image", "Image"))
    candidate = nested if isinstance(nested, dict) else value
    url_keys = (
        (*_WEB_SEARCH_IMAGE_URL_KEYS, "url", "Url")
        if isinstance(nested, dict) or allow_plain_url
        else _WEB_SEARCH_IMAGE_URL_KEYS
    )
    url = _web_search_http_url(_first_present(candidate, url_keys))
    if not url:
        return None

    detail: dict[str, Any] = {"url": url}
    for output_key, source_keys in (
        ("width", ("width", "Width")),
        ("height", ("height", "Height")),
    ):
        raw_value = _first_present(candidate, source_keys)
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            detail[output_key] = raw_value
    alt = _first_present(candidate, ("alt", "Alt", "alt_text", "altText"))
    if alt not in (None, ""):
        detail["alt"] = str(alt).strip()
    for output_key, source_keys in (
        ("shape", ("shape", "Shape")),
        ("clarity", ("blur_des", "blurDes", "BlurDes")),
        ("category", ("category", "Category")),
        ("watermark", ("watermark", "Watermark")),
    ):
        raw_value = _first_present(candidate, source_keys)
        if raw_value not in (None, ""):
            detail[output_key] = str(raw_value).strip()
    features = _first_present(candidate, ("features", "Features"))
    if isinstance(features, dict):
        for output_key, source_keys in (
            ("description", ("description", "Description")),
            ("style_type", ("style_type", "styleType", "StyleType")),
        ):
            raw_value = _first_present(features, source_keys)
            if raw_value not in (None, ""):
                detail[output_key] = str(raw_value).strip()
    return detail


def _search_item_image_details(item: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[tuple[Any, bool]] = [(item, False)]
    image_values = _first_present(item, _WEB_SEARCH_IMAGE_LIST_KEYS)
    if isinstance(image_values, list):
        candidates.extend((value, True) for value in image_values)

    snippets = _first_present(item, ("snippet", "Snippet"))
    if isinstance(snippets, list):
        candidates.extend((value, True) for value in snippets)
    elif isinstance(snippets, dict):
        candidates.append((snippets, True))

    details: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for candidate, allow_plain_url in candidates:
        detail = _web_search_image_detail(
            candidate,
            allow_plain_url=allow_plain_url,
        )
        if detail is None or detail["url"] in seen_urls:
            continue
        seen_urls.add(detail["url"])
        details.append(detail)
    return details


def _search_item_reference_tag(item: dict[str, Any], index: int) -> str:
    explicit = _first_present(item, ("reference_tag", "referenceTag"))
    if explicit not in (None, ""):
        value = str(explicit).strip()
        if value.casefold().startswith("ref_"):
            return value.casefold()
        if value.isdigit():
            return f"ref_{value}"

    sort_id = _first_present(item, ("sort_id", "sortId", "SortId"))
    if isinstance(sort_id, (int, float)) and not isinstance(sort_id, bool):
        return f"ref_{max(1, round(sort_id))}"

    rank = _first_present(item, ("rank", "Rank"))
    if isinstance(rank, (int, float)) and not isinstance(rank, bool):
        return f"ref_{max(1, round(rank) + 1)}"
    return f"ref_{index + 1}"


def _search_item_metadata(item: dict[str, Any], key: str) -> Any:
    for container_key in ("DocumentInfo", "documentInfo", "HostInfo", "hostInfo"):
        container = item.get(container_key)
        if isinstance(container, dict):
            value = _first_present(container, (key,))
            if value not in (None, ""):
                return value
    return None


def _normalize_web_search_refs(payload: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for index, item in enumerate(_candidate_search_items(payload)):
        image_details = _search_item_image_details(item)
        source_url = _web_search_http_url(_search_item_url(item))
        if not source_url and image_details:
            source_url = image_details[0]["url"]
        if not source_url:
            continue

        title = _search_item_title(item)
        if not title and image_details:
            title = str(image_details[0].get("alt") or "").strip()
        title = title or source_url

        domain = str(
            _first_present(
                item,
                (
                    "display_url",
                    "displayUrl",
                    "DisplayUrl",
                    "domain",
                    "Domain",
                    "site_name",
                    "siteName",
                    "SiteName",
                ),
            )
            or _search_item_metadata(item, "Hostname")
            or (urlsplit(source_url).hostname or "")
        ).strip()
        publish_time = _first_present(
            item,
            ("date", "Date", "published_at", "publishedAt", "publishTime", "PublishTime"),
        )
        if publish_time in (None, ""):
            publish_time = _search_item_metadata(item, "PublishTime")
        score = _first_present(item, ("score", "Score", "rankScore", "RankScore"))
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            score = 0

        ref: dict[str, Any] = {
            "date": str(publish_time or "").strip(),
            "images": [detail["url"] for detail in image_details],
            "score": score,
            "title": title,
            "url": source_url,
            "domain": domain,
            "passage": _search_item_snippet(item),
            "type": "web",
            "reference_tag": _search_item_reference_tag(item, index),
        }
        # Models may cite the upstream result Id instead of its display SortId.
        source_id = _first_present(item, ("Id", "id", "result_id", "resultId"))
        if isinstance(source_id, str):
            source_id = source_id.strip()
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", source_id):
                alias = source_id if source_id.startswith("ref_") else f"ref_{source_id}"
                ref["reference_aliases"] = [alias]
        if image_details:
            ref["image_details"] = image_details
        refs.append(ref)
    return refs

_WEB_SEARCH_COMPACT_MAX_ITEMS = 8


def _extract_web_search_payload(tool_name: str, content: str) -> dict[str, Any] | None:
    """Return frontend refs from supported structured web_search results."""
    if tool_name != "web_search" or not content:
        return None

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    if isinstance(payload.get("refs"), list):
        return payload

    refs = _normalize_web_search_refs(payload)
    if not refs:
        return None
    return {"type": "web_search", "refs": refs}


def _short_tool_text(value: Any, limit: int = 180) -> str:
    """Return a one-line text fragment suitable for compacted history."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    lower_mapping = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        value = lower_mapping.get(key.lower())
        if value not in (None, ""):
            return value
    return None

_WEB_SEARCH_RESULT_KEYS: Final[tuple[str, ...]] = (
    "refs",
    "results",
    "Results",
    "webResults",
    "WebResults",
    "web_results",
    "imageResults",
    "ImageResults",
    "image_results",
    "items",
    "value",
    "organic_results",
    "data",
)

_SITE_QUERY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)site:([a-z0-9.-]+)",
    re.IGNORECASE,
)

_SITE_QUERY_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)site:[^\s]+",
    re.IGNORECASE,
)

_SEARCH_QUERY_TERM_RE: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9]+|[\u3400-\u9fff]+",
    re.IGNORECASE,
)

_SEARCH_QUERY_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "all",
        "and",
        "for",
        "in",
        "of",
        "official",
        "on",
        "search",
        "source",
        "sources",
        "the",
        "to",
        "verify",
        "查找",
        "搜索",
        "来源",
        "核实",
        "检索",
        "官方",
        "权威",
        "查证",
        "验证",
        "调研",
    }
)


def _normalize_web_search_query(arguments: dict[str, Any]) -> str:
    query = _first_present(
        arguments,
        (
            "query",
            "Query",
            "q",
            "search_query",
            "searchQuery",
            "search_terms",
            "keywords",
        ),
    )
    if query is None:
        return ""
    return " ".join(str(query).casefold().split())


def _web_search_query_terms(query: str) -> set[str]:
    """Return conservative intent terms for near-duplicate search detection."""
    site_match = _SITE_QUERY_RE.search(query)
    site_term = f"site-{site_match.group(1).strip('.').casefold()}" if site_match else ""
    without_site_path = _SITE_QUERY_TOKEN_RE.sub(" ", query)
    terms = {
        term.casefold()
        for term in _SEARCH_QUERY_TERM_RE.findall(without_site_path)
        if term.casefold() not in _SEARCH_QUERY_STOPWORDS
    }
    if site_term:
        terms.add(site_term)
    return terms


def _web_search_queries_are_near_duplicates(first: str, second: str) -> bool:
    """Detect only high-overlap rewrites while preserving distinct research gaps."""
    if not first or not second:
        return False
    if first == second:
        return True
    first_site = _SITE_QUERY_RE.search(first)
    second_site = _SITE_QUERY_RE.search(second)
    first_domain = first_site.group(1).strip(".").casefold() if first_site else ""
    second_domain = second_site.group(1).strip(".").casefold() if second_site else ""
    if first_domain != second_domain:
        return False
    first_terms = _web_search_query_terms(first)
    second_terms = _web_search_query_terms(second)
    if min(len(first_terms), len(second_terms)) < 3:
        return False
    overlap = len(first_terms & second_terms)
    containment = overlap / min(len(first_terms), len(second_terms))
    coverage = overlap / max(len(first_terms), len(second_terms))
    return containment >= 0.9 and coverage >= 0.65


def _requested_site_domain(arguments: dict[str, Any]) -> str:
    query = _normalize_web_search_query(arguments)
    match = _SITE_QUERY_RE.search(query)
    if match is None:
        return ""
    return match.group(1).strip(".").casefold()


def _normalize_search_title(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _web_search_result_key(item: dict[str, Any]) -> str:
    url = _first_present(item, ("url", "Url", "href", "link", "Link"))
    normalized_url = _normalize_search_url(url)
    if normalized_url:
        return f"url:{normalized_url}"

    title = _normalize_search_title(_first_present(item, ("title", "Title", "name", "Name")))
    if not title:
        return ""
    domain = str(_first_present(item, ("domain", "Domain", "source", "Source", "site", "Site")) or "").casefold()
    return f"title:{domain}:{title}"


def _search_item_url(item: dict[str, Any]) -> str:
    return str(_first_present(item, ("url", "Url", "href", "link", "Link")) or "").strip()


def _url_matches_domain(value: Any, domain: str) -> bool:
    if not domain:
        return True
    try:
        hostname = (urlsplit(str(value or "")).hostname or "").casefold().strip(".")
    except ValueError:
        return False
    return hostname == domain or hostname.endswith(f".{domain}")


def _with_filtered_search_items(payload: Any, filtered_items: list[dict[str, Any]]) -> Any:
    if isinstance(payload, list):
        return filtered_items
    if not isinstance(payload, dict):
        return payload

    for key in _WEB_SEARCH_RESULT_KEYS:
        value = payload.get(key)
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            updated = dict(payload)
            updated[key] = filtered_items
            return updated

    for key, value in payload.items():
        if isinstance(value, dict) and _candidate_search_items(value):
            updated = dict(payload)
            updated[key] = _with_filtered_search_items(value, filtered_items)
            return updated

    return payload


def _candidate_search_items(payload: Any) -> list[dict[str, Any]]:
    """Extract likely search-result rows from common web_search payload shapes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    for key in _WEB_SEARCH_RESULT_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            items = [item for item in value if isinstance(item, dict)]
            if items:
                return items

    for value in payload.values():
        if isinstance(value, dict):
            nested = _candidate_search_items(value)
            if nested:
                return nested
        elif isinstance(value, list):
            items = [item for item in value if isinstance(item, dict)]
            if any(_first_present(item, ("title", "Title", "url", "Url", "href", "link")) for item in items):
                return items

    return []


def _search_result_list_found(payload: Any) -> bool:
    """Return whether a structured result-list field exists, even when empty."""
    if isinstance(payload, list):
        return True
    if not isinstance(payload, dict):
        return False
    for key in _WEB_SEARCH_RESULT_KEYS:
        if isinstance(payload.get(key), list):
            return True
    return any(
        _search_result_list_found(value)
        for value in payload.values()
        if isinstance(value, dict)
    )


def _search_item_title(item: dict[str, Any]) -> str:
    return str(_first_present(item, ("title", "Title", "name", "Name")) or "").strip()


def _search_item_snippet(item: dict[str, Any]) -> str:
    value = _first_present(
        item,
        (
            "snippet",
            "Snippet",
            "summary",
            "Summary",
            "description",
            "Description",
            "content",
            "Content",
        ),
    )
    if isinstance(value, list):
        text_parts: list[str] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            text = _first_present(entry, ("text", "Text"))
            if text not in (None, ""):
                text_parts.append(str(text).strip())
        return "\n".join(part for part in text_parts if part)
    if isinstance(value, dict):
        text = _first_present(value, ("text", "Text"))
        return str(text or "").strip()
    return str(value or "").strip()


def _web_search_match_terms(query: str) -> tuple[str, ...]:
    """Extract stable entity/topic terms for result relevance scoring."""
    without_site = _SITE_QUERY_TOKEN_RE.sub(" ", query)
    terms: list[str] = []
    for raw in _SEARCH_QUERY_TERM_RE.findall(without_site):
        term = raw.casefold()
        if term in _SEARCH_QUERY_STOPWORDS:
            continue
        candidates = [term]
        if re.fullmatch(r"[\u3400-\u9fff]+", term) and len(term) > 2:
            candidates.extend(term[index : index + 2] for index in range(len(term) - 1))
        for candidate in candidates:
            if candidate and candidate not in terms:
                terms.append(candidate)
    return tuple(terms[:24])


def _web_search_item_rank(
    item: dict[str, Any],
    *,
    query_terms: tuple[str, ...],
    requested_site: str,
) -> tuple[int, int, int, int]:
    """Return relevance, domain, first-party, and coverage scores."""
    title = _normalize_search_title(_search_item_title(item))
    snippet = _normalize_search_title(_search_item_snippet(item))
    url = _search_item_url(item)
    host = ""
    try:
        host = (urlsplit(url).hostname or "").casefold().strip(".")
    except ValueError:
        pass
    entity_score = 0
    matched_terms = 0
    for term in query_terms:
        matched = False
        if term in title:
            entity_score += 6
            matched = True
        if term in snippet:
            entity_score += 2
            matched = True
        if term in host or term in url.casefold():
            entity_score += 3
            matched = True
        if matched:
            matched_terms += 1
    coverage_score = (
        round((matched_terms / len(query_terms)) * 20) if query_terms else 0
    )
    domain_score = 50 if requested_site and _url_matches_domain(url, requested_site) else 0
    explicit_source_type = str(
        _first_present(
            item,
            ("source_type", "SourceType", "sourceType", "authority", "Authority"),
        )
        or ""
    ).casefold()
    first_party_score = 0
    if domain_score:
        first_party_score = 3
    elif explicit_source_type in {"first_party", "official", "primary"}:
        first_party_score = 2
    elif "official" in title or "官网" in title or "官方" in title:
        first_party_score = 1
    return entity_score, domain_score, first_party_score, coverage_score


def _rank_web_search_items(
    items: list[dict[str, Any]],
    arguments: dict[str, Any],
) -> list[dict[str, Any]]:
    query = _normalize_web_search_query(arguments)
    query_terms = _web_search_match_terms(query)
    requested_site = _requested_site_domain(arguments)
    ranked = [
        (
            item,
            _web_search_item_rank(
                item,
                query_terms=query_terms,
                requested_site=requested_site,
            ),
            index,
        )
        for index, item in enumerate(items)
    ]
    ranked.sort(
        key=lambda entry: (
            -entry[1][1],
            -entry[1][0],
            -entry[1][2],
            -entry[1][3],
            entry[2],
        )
    )
    return [item for item, _, _ in ranked]


def _web_search_result_metadata(
    items: list[dict[str, Any]],
    arguments: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    query = _normalize_web_search_query(arguments)
    query_terms = _web_search_match_terms(query)
    requested_site = _requested_site_domain(arguments)
    ranking = []
    direct_read_candidates = []
    for item in items[:_WEB_SEARCH_COMPACT_MAX_ITEMS]:
        url = _search_item_url(item)
        entity_score, domain_score, first_party_score, coverage_score = (
            _web_search_item_rank(
                item,
                query_terms=query_terms,
                requested_site=requested_site,
            )
        )
        ranking.append(
            {
                "title": _short_tool_text(_search_item_title(item), 120),
                "url": _short_tool_text(url, 180),
                "entity_match_score": entity_score,
                "domain_match_score": domain_score,
                "first_party_level": first_party_score,
                "query_coverage_score": coverage_score,
            }
        )
        if url and (domain_score > 0 or first_party_score >= 2):
            direct_read_candidates.append(url)
    return {
        "SearchStatus": status,
        "NormalizedResultCount": len(items),
        "SearchResultRanking": ranking,
        "DirectReadCandidates": list(dict.fromkeys(direct_read_candidates))[:5],
        "DirectReadNotice": (
            "When an exact first-party URL is known, read that page with an available "
            "direct browser/page tool before using it as evidence."
        ),
    }


def _with_web_search_metadata(
    payload: Any,
    metadata: dict[str, Any],
) -> Any:
    if not isinstance(payload, dict):
        return payload
    return {**payload, **metadata}


def _log_web_search_model_results(
    arguments: dict[str, Any],
    visible_content: str,
    model_content: str,
) -> None:
    """Log the ranked rows that were actually eligible for model context."""
    try:
        payload = json.loads(visible_content)
    except json.JSONDecodeError:
        _log.info(
            "web_search/model_results query=%r structured=false model_chars=%d",
            _normalize_web_search_query(arguments),
            len(model_content),
        )
        return
    items = _candidate_search_items(payload)
    status = payload.get("SearchStatus") if isinstance(payload, dict) else None
    top = [
        {
            "title": _short_tool_text(_search_item_title(item), 120),
            "url": _short_tool_text(_search_item_url(item), 180),
        }
        for item in items[:5]
    ]
    _log.info(
        "web_search/model_results query=%r status=%s model_chars=%d top=%s",
        _normalize_web_search_query(arguments),
        status or "unknown",
        len(model_content),
        json.dumps(top, ensure_ascii=False, separators=(",", ":")),
    )


def _dedupe_web_search_content(
    content: str,
    seen_result_keys: set[str],
    arguments: dict[str, Any] | None = None,
) -> tuple[str, int, int, list[str], bool]:
    """Filter duplicate web_search rows for this turn.

    Returns ``(content, new_count, duplicate_count, new_labels, inspected)``.
    ``inspected`` is true only when structured search rows were found; plain
    text results should not count as "no new evidence" just because they
    cannot be deduped structurally.
    """
    if not content:
        return content, 0, 0, [], False

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content, 0, 0, [], False

    items = _candidate_search_items(payload)
    structured_result_list = _search_result_list_found(payload)
    if not items:
        if not structured_result_list:
            return content, 0, 0, [], False
        requested_site = _requested_site_domain(arguments or {})
        status = "site_no_results" if requested_site else "no_results"
        updated_payload = _with_web_search_metadata(
            payload,
            _web_search_result_metadata([], arguments or {}, status=status),
        )
        if isinstance(updated_payload, dict) and requested_site:
            updated_payload = {
                **updated_payload,
                "RequestedSiteDomain": requested_site,
                "SiteFilterDroppedCount": 0,
                "SiteFilterMatchedCount": 0,
                "SiteFilterNotice": (
                    f"No results were returned for site:{requested_site}. "
                    "Do not treat this as proof that no official page exists, do not "
                    "invent a URL, and use a known exact URL with a direct page-read "
                    "tool when available."
                ),
            }
        return json.dumps(updated_payload, ensure_ascii=False), 0, 0, [], True

    requested_site = _requested_site_domain(arguments or {})
    site_filtered_count = 0
    site_matched_count = len(items) if requested_site else 0
    if requested_site:
        matched_items = [
            item for item in items if _url_matches_domain(_search_item_url(item), requested_site)
        ]
        site_matched_count = len(matched_items)
        site_filtered_count = len(items) - len(matched_items)
        if site_filtered_count:
            payload = _with_filtered_search_items(payload, matched_items)
            if isinstance(payload, dict):
                payload = {
                    **payload,
                    "RequestedSiteDomain": requested_site,
                    "SiteFilterDroppedCount": site_filtered_count,
                    "SiteFilterMatchedCount": len(matched_items),
                    "SiteFilterNotice": (
                        f"Only URLs hosted on {requested_site} are valid for this site: query. "
                        "Do not cite or relabel dropped results, and do not invent a replacement URL."
                    ),
                }
            items = matched_items
            if not items:
                payload = _with_web_search_metadata(
                    payload,
                    _web_search_result_metadata(
                        [],
                        arguments or {},
                        status="site_no_results",
                    ),
                )
                if isinstance(payload, dict):
                    payload["SearchEmptyReason"] = "all_provider_results_were_off_domain"
                return json.dumps(payload, ensure_ascii=False), 0, 0, [], True

    items = _rank_web_search_items(items, arguments or {})
    payload = _with_filtered_search_items(payload, items)

    filtered_items: list[dict[str, Any]] = []
    new_labels: list[str] = []
    duplicate_count = 0
    for item in items:
        key = _web_search_result_key(item)
        if key and key in seen_result_keys:
            duplicate_count += 1
            continue
        if key:
            seen_result_keys.add(key)
        filtered_items.append(item)
        label = _first_present(item, ("title", "Title", "name", "Name")) or _first_present(
            item, ("url", "Url", "href", "link", "Link")
        )
        if label:
            new_labels.append(_short_tool_text(label, 100))

    updated_payload = _with_filtered_search_items(payload, filtered_items)
    if isinstance(updated_payload, dict):
        updated_payload = {
            **updated_payload,
            "DedupedDuplicateCount": duplicate_count,
            "DedupedNewCount": len(filtered_items),
            **_web_search_result_metadata(
                filtered_items,
                arguments or {},
                status="ok" if filtered_items else "no_new_results",
            ),
        }
        if requested_site:
            updated_payload = {
                **updated_payload,
                "RequestedSiteDomain": requested_site,
                "SiteFilterDroppedCount": site_filtered_count,
                "SiteFilterMatchedCount": site_matched_count,
            }
    return json.dumps(updated_payload, ensure_ascii=False), len(filtered_items), duplicate_count, new_labels, True
