#!/usr/bin/env python3
"""Validate deep research artifacts and Markdown footnote integrity."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit


FOOTNOTE_MARKER_RE = re.compile(r"\[\^([A-Za-z0-9_.:-]+)\]")
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([A-Za-z0-9_.:-]+)\]:", re.MULTILINE)
NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?%?")
LATIN_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,}")
CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
SOURCE_TYPES = frozenset(
    {
        "first_party",
        "government",
        "regulator",
        "filing",
        "standards_body",
        "academic",
        "reputable_media",
        "secondary",
        "user_input",
    }
)
CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})
EVIDENCE_STATUSES = frozenset({"verified", "conflicting", "unverified"})
SEARCH_RESULT_HOSTS = frozenset(
    {
        "bing.com",
        "www.bing.com",
        "google.com",
        "www.google.com",
        "search.yahoo.com",
    }
)
SEMANTIC_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "also",
        "and",
        "are",
        "been",
        "for",
        "from",
        "has",
        "have",
        "into",
        "its",
        "that",
        "the",
        "their",
        "this",
        "was",
        "were",
        "with",
    }
)


ROUTE_REQUIRED = {
    "A": ["{topic}_cross_verification.md", "{topic}_insight.md"],
    "B": ["{topic}_cross_verification.md", "{topic}_insight.md"],
    "C": [
        "{topic}_file_analysis.md",
        "{topic}_cross_verification.md",
        "{topic}_insight.md",
    ],
    "D": [
        "{topic}_file_analysis.md",
        "{topic}_cross_verification.md",
        "{topic}_insight.md",
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate deep research output files for a topic."
    )
    parser.add_argument("--research-dir", required=True, type=Path)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--route", required=True, choices=sorted(ROUTE_REQUIRED))
    parser.add_argument("--min-dimensions", type=int, default=10)
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional JSON report path for downstream workflow checkpoints.",
    )
    parser.add_argument(
        "--allow-missing-footnotes",
        action="store_true",
        help="Only warn when footnote markers lack definitions.",
    )
    return parser.parse_args()


def write_report(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def collect_footnotes(path: Path) -> tuple[set[str], set[str]]:
    text = path.read_text(encoding="utf-8")
    definitions = set(FOOTNOTE_DEF_RE.findall(text))
    markers = set(FOOTNOTE_MARKER_RE.findall(text))
    return markers - definitions, definitions


def normalized_text(value: object) -> str:
    return re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
    )


def semantic_tokens(value: object) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    tokens = {
        token
        for token in LATIN_TOKEN_RE.findall(text)
        if token not in SEMANTIC_STOPWORDS
    }
    for run in CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def normalized_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold().strip(".")
    except ValueError:
        return ""


def host_matches_domain(host: str, domain: str) -> bool:
    normalized_domain = domain.casefold().strip(".")
    return bool(
        host
        and normalized_domain
        and (host == normalized_domain or host.endswith(f".{normalized_domain}"))
    )


def canonical_evidence(record: dict[str, object]) -> str:
    return " | ".join(
        str(record.get(field) or "").strip()
        for field in ("entity", "claim", "source_type", "source_url")
    )


def validate_evidence_ledger(
    path: Path,
    topic: str,
) -> tuple[list[str], list[str], dict[str, object]]:
    errors: list[str] = []
    warnings: list[str] = []
    verified_evidence: list[dict[str, object]] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"could not read evidence ledger {path.name}: {exc}"], [], {}
    except json.JSONDecodeError as exc:
        return [f"{path.name}: invalid JSON: {exc}"], [], {}
    if not isinstance(payload, dict):
        return [f"{path.name}: root must be a JSON object"], [], {}
    if payload.get("schema_version") != 1:
        errors.append(f"{path.name}: schema_version must be 1")
    if payload.get("topic") != topic:
        errors.append(f"{path.name}: topic must equal {topic!r}")

    raw_entities = payload.get("target_entities")
    if not isinstance(raw_entities, list) or not raw_entities:
        errors.append(f"{path.name}: target_entities must be a non-empty array")
        raw_entities = []
    entities: dict[str, dict[str, object]] = {}
    for index, raw_entity in enumerate(raw_entities):
        label = f"{path.name}: target_entities.{index}"
        if not isinstance(raw_entity, dict):
            errors.append(f"{label} must be an object")
            continue
        entity = str(raw_entity.get("entity") or "").strip()
        if not entity:
            errors.append(f"{label}.entity must be non-empty")
            continue
        key = normalized_text(entity)
        if key in entities:
            errors.append(f"{label}.entity duplicates {entity!r}")
            continue
        aliases = raw_entity.get("aliases")
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            errors.append(f"{label}.aliases must be a non-empty string array")
            aliases = []
        official_domains = raw_entity.get("official_domains", [])
        if not isinstance(official_domains, list) or not all(
            isinstance(domain, str) and domain.strip() for domain in official_domains
        ):
            errors.append(f"{label}.official_domains must be a string array")
            official_domains = []
        entities[key] = {
            "entity": entity,
            "aliases": [entity, *aliases],
            "official_domains": [
                str(domain).casefold().strip(".") for domain in official_domains
            ],
        }

    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        errors.append(f"{path.name}: evidence must be a non-empty array")
        raw_evidence = []
    verified_by_entity: dict[str, int] = {key: 0 for key in entities}
    first_party_by_entity: dict[str, int] = {key: 0 for key in entities}
    status_counts = {status: 0 for status in EVIDENCE_STATUSES}
    seen_canonical: set[str] = set()
    for index, raw_record in enumerate(raw_evidence):
        label = f"{path.name}: evidence.{index}"
        record_error_start = len(errors)
        if not isinstance(raw_record, dict):
            errors.append(f"{label} must be an object")
            continue
        missing = [
            field
            for field in (
                "entity",
                "claim",
                "source_url",
                "source_type",
                "evidence_excerpt",
                "confidence",
                "status",
            )
            if not isinstance(raw_record.get(field), str)
            or not str(raw_record.get(field)).strip()
        ]
        if missing:
            errors.append(f"{label} missing non-empty fields: {', '.join(missing)}")
            continue
        record = {
            key: str(value).strip() if isinstance(value, str) else value
            for key, value in raw_record.items()
        }
        entity_key = normalized_text(record["entity"])
        entity_spec = entities.get(entity_key)
        if entity_spec is None:
            errors.append(
                f"{label}.entity {record['entity']!r} is not declared in target_entities"
            )
            continue
        source_type = str(record["source_type"]).casefold()
        confidence = str(record["confidence"]).casefold()
        status = str(record["status"]).casefold()
        if source_type not in SOURCE_TYPES:
            errors.append(
                f"{label}.source_type must be one of {', '.join(sorted(SOURCE_TYPES))}"
            )
        if confidence not in CONFIDENCE_LEVELS:
            errors.append(
                f"{label}.confidence must be one of {', '.join(sorted(CONFIDENCE_LEVELS))}"
            )
        if status not in EVIDENCE_STATUSES:
            errors.append(
                f"{label}.status must be one of {', '.join(sorted(EVIDENCE_STATUSES))}"
            )
            continue
        status_counts[status] += 1

        source_url = str(record["source_url"])
        try:
            parsed_url = urlsplit(source_url)
        except ValueError:
            parsed_url = urlsplit("")
        host = normalized_host(source_url)
        is_user_file_reference = (
            source_type == "user_input"
            and parsed_url.scheme in {"file", "user-input"}
        )
        if (
            not is_user_file_reference
            and (parsed_url.scheme not in {"http", "https"} or not host)
        ):
            errors.append(
                f"{label}.source_url must be an absolute http(s) URL, or a "
                "file:/user-input: reference for source_type=user_input"
            )
        if not is_user_file_reference and host in SEARCH_RESULT_HOSTS:
            errors.append(
                f"{label}.source_url points to a search-results page, not evidence"
            )
        aliases = [
            normalized_text(alias)
            for alias in entity_spec["aliases"]
            if normalized_text(alias)
        ]
        excerpt_normalized = normalized_text(record["evidence_excerpt"])
        if not any(alias in excerpt_normalized for alias in aliases):
            errors.append(
                f"{label}.evidence_excerpt does not name entity "
                f"{entity_spec['entity']!r} or one of its aliases"
            )

        claim = str(record["claim"])
        excerpt = str(record["evidence_excerpt"])
        claim_numbers = set(NUMBER_RE.findall(claim))
        excerpt_numbers = set(NUMBER_RE.findall(excerpt))
        missing_numbers = sorted(claim_numbers - excerpt_numbers)
        if missing_numbers:
            errors.append(
                f"{label}.evidence_excerpt is missing claim number(s): "
                + ", ".join(missing_numbers)
            )
        claim_tokens = semantic_tokens(claim)
        excerpt_tokens = semantic_tokens(excerpt)
        overlap = claim_tokens & excerpt_tokens
        required_overlap = max(1, math.ceil(len(claim_tokens) * 0.25))
        if claim_tokens and len(overlap) < required_overlap:
            errors.append(
                f"{label}.evidence_excerpt does not support the claim closely enough "
                f"(token overlap {len(overlap)}/{len(claim_tokens)}, "
                f"requires {required_overlap})"
            )

        valid_first_party = False
        if source_type == "first_party":
            official_domains = entity_spec["official_domains"]
            if not official_domains:
                errors.append(
                    f"{label}: first_party evidence requires official_domains for "
                    f"{entity_spec['entity']!r}"
                )
            elif not any(
                host_matches_domain(host, domain) for domain in official_domains
            ):
                errors.append(
                    f"{label}.source_url host {host!r} does not match an official "
                    f"domain for {entity_spec['entity']!r}"
                )
            else:
                valid_first_party = True

        user_input_claim = str(record.get("user_input_claim") or "").strip()
        user_input_alignment = str(
            record.get("user_input_alignment") or ""
        ).casefold().strip()
        if user_input_claim:
            if user_input_alignment not in {
                "supported",
                "conflicting",
                "unverified",
            }:
                errors.append(
                    f"{label}.user_input_alignment must be supported, conflicting, "
                    "or unverified when user_input_claim is present"
                )
            if user_input_alignment == "conflicting" and status != "conflicting":
                errors.append(
                    f"{label}: a conflicting user input claim must use status=conflicting"
                )
        if status == "conflicting" and not str(
            record.get("conflict_note") or ""
        ).strip():
            errors.append(f"{label}.conflict_note is required for status=conflicting")
        if status == "unverified" and not str(
            record.get("unverified_reason") or ""
        ).strip():
            errors.append(
                f"{label}.unverified_reason is required for status=unverified"
            )
        if status != "verified":
            # Discovery rows are retained for limitations and future recovery, but
            # they are not part of the factual handoff.  Claim/excerpt quality
            # findings on those rows must not fail the whole research package.
            record_errors = errors[record_error_start:]
            del errors[record_error_start:]
            warnings.extend(record_errors)
            warnings.append(
                f"{label}: status={status}; excluded from downstream verified evidence"
            )
            continue
        if confidence == "low":
            errors.append(f"{label}: verified evidence cannot use confidence=low")
            continue
        if len(errors) > record_error_start:
            # A row labelled verified is usable only when every entity, URL,
            # excerpt, number, and source-ownership check above passed.
            continue

        canonical = canonical_evidence(record)
        if len(canonical) > 280:
            errors.append(
                f"{label}: canonical evidence exceeds 280 characters; shorten the "
                "claim without removing the entity, source type, or URL"
            )
            continue
        canonical_key = normalized_text(canonical)
        if canonical_key in seen_canonical:
            errors.append(f"{label}: duplicate verified evidence")
            continue
        seen_canonical.add(canonical_key)
        verified_by_entity[entity_key] += 1
        if valid_first_party:
            first_party_by_entity[entity_key] += 1
        verified_evidence.append(
            {
                "entity": record["entity"],
                "claim": record["claim"],
                "source_url": record["source_url"],
                "source_type": source_type,
                "evidence_excerpt": record["evidence_excerpt"],
                "confidence": confidence,
                "status": status,
                "canonical": canonical,
            }
        )

    for entity_key, entity_spec in entities.items():
        if verified_by_entity[entity_key] == 0:
            warnings.append(
                f"{path.name}: no verified evidence for target entity "
                f"{entity_spec['entity']!r}"
            )
        if (
            entity_spec["official_domains"]
            and first_party_by_entity[entity_key] == 0
        ):
            warnings.append(
                f"{path.name}: no verified first_party evidence from an official "
                f"domain for target entity {entity_spec['entity']!r}"
            )

    summary: dict[str, object] = {
        "evidence_schema_version": 1,
        "evidence_file": str(path.resolve()),
        "target_entity_count": len(entities),
        "evidence_count": len(raw_evidence),
        "verified_evidence_count": len(verified_evidence),
        "conflicting_evidence_count": status_counts["conflicting"],
        "unverified_evidence_count": status_counts["unverified"],
        "first_party_entity_count": sum(
            1 for count in first_party_by_entity.values() if count > 0
        ),
        "verified_evidence": verified_evidence,
    }
    return errors, warnings, summary


def main() -> int:
    args = parse_args()
    research_dir = args.research_dir
    errors: list[str] = []
    warnings: list[str] = []
    dim_files: list[Path] = []
    wide_files: list[Path] = []
    files_to_check: list[Path] = []
    evidence_summary: dict[str, object] = {}

    if not research_dir.exists():
        errors.append(f"research dir does not exist: {research_dir}")
    elif not research_dir.is_dir():
        errors.append(f"research path is not a directory: {research_dir}")

    if not errors:
        required = [name.format(topic=args.topic) for name in ROUTE_REQUIRED[args.route]]
        dim_files = sorted(research_dir.glob(f"{args.topic}_dim*.md"))
        wide_files = sorted(research_dir.glob(f"{args.topic}_wide*.md"))
        evidence_file = research_dir / f"{args.topic}_evidence.json"

        for file_name in required:
            if not (research_dir / file_name).exists():
                errors.append(f"missing required file: {file_name}")

        if len(dim_files) < args.min_dimensions:
            errors.append(
                f"expected at least {args.min_dimensions} dimension files, found {len(dim_files)}"
            )

        if args.route == "A" and not wide_files:
            errors.append("route A requires at least one wide exploration file")
        if not evidence_file.is_file():
            errors.append(f"missing required file: {evidence_file.name}")
        else:
            evidence_errors, evidence_warnings, evidence_summary = (
                validate_evidence_ledger(evidence_file, args.topic)
            )
            errors.extend(evidence_errors)
            warnings.extend(evidence_warnings)

        files_to_check = sorted(
            {
                *dim_files,
                *wide_files,
                *(research_dir / file_name for file_name in required),
                *(research_dir.glob(f"{args.topic}_final.md")),
                *([evidence_file] if evidence_file.is_file() else []),
            }
        )

        for path in files_to_check:
            if not path.exists() or path.suffix.casefold() != ".md":
                continue
            missing_defs, definitions = collect_footnotes(path)
            if missing_defs:
                message = (
                    f"{path.name}: missing footnote definitions for "
                    + ", ".join(sorted(missing_defs))
                )
                if args.allow_missing_footnotes:
                    warnings.append(message)
                else:
                    errors.append(message)
            if "[^" in path.read_text(encoding="utf-8") and not definitions:
                errors.append(f"{path.name}: contains footnote markers but no definitions")

    verified_evidence_count = evidence_summary.get("verified_evidence_count", 0)
    if not errors and verified_evidence_count == 0:
        errors.append("no verified evidence available for downstream factual handoff")

    quality_ok = not errors
    required_outputs_present = bool(
        research_dir.is_dir()
        and dim_files
        and all(
            (research_dir / name.format(topic=args.topic)).is_file()
            for name in ROUTE_REQUIRED[args.route]
        )
    )
    delivery_allowed = bool(
        required_outputs_present
        and evidence_summary.get("evidence_schema_version") == 1
    )
    dimension_coverage_ratio = min(
        1.0,
        len(dim_files) / max(1, args.min_dimensions),
    )
    handoff_status = (
        "invalid"
        if not delivery_allowed
        else (
            "full"
            if quality_ok and verified_evidence_count > 0
            else "partial" if verified_evidence_count > 0 else "framework"
        )
    )
    presentation_handoff = {
        "schema_version": 1,
        "delivery_mode": handoff_status,
        "verified_facts": [
            {
                field: record[field]
                for field in (
                    "entity",
                    "claim",
                    "source_type",
                    "source_url",
                    "canonical",
                )
            }
            for record in evidence_summary.get("verified_evidence", [])
        ],
        "gaps": [*errors, *warnings],
        "quality_summary": {
            "quality_ok": quality_ok,
            "issue_count": len(errors),
            "warning_count": len(warnings),
            "actual_dimensions": len(dim_files),
            "recommended_dimensions": args.min_dimensions,
        },
        "context_files": [
            str(path.relative_to(research_dir))
            for path in files_to_check
            if path.exists()
        ],
    }
    report_payload: dict[str, object] = {
        # Keep the top-level fields for research QA diagnostics and older
        # runtimes. New presentation consumers use ``presentation_handoff``.
        "ok": quality_ok,
        "quality_ok": quality_ok,
        "delivery_allowed": delivery_allowed,
        "handoff_status": handoff_status,
        "validator": "research-synthesis",
        "route": args.route,
        "topic": args.topic,
        "research_dir": str(research_dir.resolve()),
        "min_dimensions": args.min_dimensions,
        "dimension_count": len(dim_files),
        "wide_count": len(wide_files),
        "coverage": {
            "actual_dimensions": len(dim_files),
            "recommended_dimensions": args.min_dimensions,
            "dimension_ratio": round(dimension_coverage_ratio, 4),
            "wide_required": args.route == "A",
            "wide_present": bool(wide_files),
        },
        "files_checked": [str(path.resolve()) for path in files_to_check if path.exists()],
        "issues": errors,
        "warnings": warnings,
        # Stable downstream contract. Presentation workflows consume this
        # object instead of interpreting research-synthesis's internal QA state.
        "presentation_handoff": presentation_handoff,
        **evidence_summary,
    }
    write_report(args.report, report_payload)

    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        f"OK: {args.route} artifacts for topic '{args.topic}' validated in {research_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
