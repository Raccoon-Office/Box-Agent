#!/usr/bin/env python3
"""Validate the small, source-first research handoff contract.

The validator deliberately checks only what downstream consumers need:

* there is at least one narrative research artifact;
* the evidence ledger is readable; and
* every row handed off as verified points to a concrete HTTP(S) source page.

Formatting, dimension counts, footnote style, and prose/evidence similarity are
quality hints, not delivery gates.  This keeps a usable partial or framework
handoff from turning into a repair loop.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit


FOOTNOTE_MARKER_RE = re.compile(r"\[\^([A-Za-z0-9_.:-]+)\]")
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([A-Za-z0-9_.:-]+)\]:", re.MULTILINE)
SEARCH_RESULT_HOSTS = frozenset(
    {
        "bing.com",
        "www.bing.com",
        "google.com",
        "www.google.com",
        "search.yahoo.com",
    }
)
EVIDENCE_STATUSES = frozenset({"verified", "conflicting", "unverified"})
NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?%?")
LATIN_WORD_RE = re.compile(r"[a-z][a-z-]*")
DIRECTION_TERMS = {
    "increase": (
        "increase",
        "increased",
        "increasing",
        "grew",
        "growth",
        "rise",
        "risen",
        "rose",
        "上涨",
        "上升",
        "增加",
        "增长",
        "提升",
    ),
    "decrease": (
        "decrease",
        "decreased",
        "decreasing",
        "decline",
        "declined",
        "drop",
        "dropped",
        "fall",
        "fell",
        "fallen",
        "下跌",
        "下降",
        "减少",
        "降低",
        "衰退",
    ),
    "above": (
        "above",
        "exceed",
        "exceeded",
        "greater",
        "higher",
        "超过",
        "高于",
    ),
    "below": (
        "below",
        "fewer",
        "less",
        "lower",
        "under",
        "低于",
        "少于",
        "不足",
    ),
}
OPPOSITE_DIRECTIONS = {
    "increase": "decrease",
    "decrease": "increase",
    "above": "below",
    "below": "above",
}
NEGATABLE_CJK_TERMS = (
    "发布",
    "推出",
    "上线",
    "批准",
    "确认",
    "增长",
    "增加",
    "上升",
    "下降",
    "减少",
    "超过",
    "达到",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate source URLs in a research handoff."
    )
    parser.add_argument("--research-dir", required=True, type=Path)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--route", required=True, choices=("A", "B", "C", "D"))
    parser.add_argument(
        "--min-dimensions",
        type=int,
        default=None,
        help=(
            "Deprecated compatibility option. Dimension counts are no longer "
            "validated or used as a delivery threshold."
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional JSON report path for downstream workflow checkpoints.",
    )
    parser.add_argument(
        "--allow-missing-footnotes",
        action="store_true",
        help="Deprecated compatibility option; missing definitions are always warnings.",
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


def normalized_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold().strip(".")
    except ValueError:
        return ""


def is_concrete_source_page(
    url: str,
    *,
    source_type: str = "secondary",
) -> tuple[bool, str | None]:
    """Return whether *url* identifies a concrete page rather than discovery UI."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False, "source_url is not a valid URL"
    if source_type == "user_input" and parsed.scheme in {"file", "user-input"}:
        return True, None
    host = (parsed.hostname or "").casefold().strip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        return False, "source_url must be an absolute http(s) URL"
    if host in SEARCH_RESULT_HOSTS:
        return False, "source_url points to a search-results page"
    if not parsed.path.strip("/") and not parsed.query:
        return False, "source_url must identify a concrete page, not an origin homepage"
    return True, None


def collect_footnote_warnings(path: Path) -> list[str]:
    """Return advisory Markdown findings without rejecting the handoff."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"could not read narrative file {path.name}: {exc}"]
    markers = set(FOOTNOTE_MARKER_RE.findall(text))
    definitions = set(FOOTNOTE_DEF_RE.findall(text))
    missing = sorted(markers - definitions)
    if not missing:
        return []
    return [
        f"{path.name}: missing footnote definitions for {', '.join(missing)}"
    ]


def normalized_text(value: object) -> str:
    return re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
    )


def _normalized_numbers(value: object) -> set[str]:
    return {match.replace(",", "") for match in NUMBER_RE.findall(str(value or ""))}


def _stem_latin_word(word: str) -> str:
    for suffix in ("ing", "ied", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            if suffix == "ied":
                return word[: -len(suffix)] + "y"
            return word[: -len(suffix)]
    return word


def _latin_words(value: object) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return {_stem_latin_word(word) for word in LATIN_WORD_RE.findall(text)}


def _negated_terms(value: object) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    terms = {
        _stem_latin_word(match.group(1))
        for match in re.finditer(
            r"\b(?:did\s+|does\s+|do\s+|is\s+|are\s+|was\s+|were\s+|"
            r"has\s+|have\s+)?(?:not|never)\s+([a-z][a-z-]*)",
            text,
        )
    }
    terms.update(
        term
        for term in NEGATABLE_CJK_TERMS
        if re.search(rf"(?:没有|并未|未曾|不曾|未|不)\s*{re.escape(term)}", text)
    )
    return terms


def _direction_signals(value: object) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    signals: set[str] = set()
    for direction, terms in DIRECTION_TERMS.items():
        for term in terms:
            pattern = rf"\b{re.escape(term)}\b" if term.isascii() else re.escape(term)
            for match in re.finditer(pattern, text):
                prefix = text[max(0, match.start() - 10) : match.start()]
                if re.search(r"(?:not|never)\s+$", prefix) or re.search(
                    r"(?:没有|并未|未曾|不曾|未|不)\s*$", prefix
                ):
                    continue
                signals.add(direction)
                break
    return signals


def claim_excerpt_conflicts(claim: str, excerpt: str) -> list[str]:
    """Return clear number, direction, or negation mismatches for one row."""
    findings: list[str] = []
    missing_numbers = sorted(_normalized_numbers(claim) - _normalized_numbers(excerpt))
    if missing_numbers:
        findings.append("excerpt is missing claim number(s): " + ", ".join(missing_numbers))

    claim_directions = _direction_signals(claim)
    excerpt_directions = _direction_signals(excerpt)
    for direction in sorted(claim_directions):
        opposite = OPPOSITE_DIRECTIONS[direction]
        if opposite in excerpt_directions and direction not in excerpt_directions:
            findings.append(
                f"claim direction '{direction}' conflicts with excerpt direction "
                f"'{opposite}'"
            )

    claim_negated = _negated_terms(claim)
    excerpt_negated = _negated_terms(excerpt)
    claim_words = _latin_words(claim) | {
        term for term in NEGATABLE_CJK_TERMS if term in claim
    }
    excerpt_words = _latin_words(excerpt) | {
        term for term in NEGATABLE_CJK_TERMS if term in excerpt
    }
    negated_only_in_claim = claim_negated & (excerpt_words - excerpt_negated)
    negated_only_in_excerpt = excerpt_negated & (claim_words - claim_negated)
    if negated_only_in_claim:
        findings.append(
            "claim negates term(s) affirmed by excerpt: "
            + ", ".join(sorted(negated_only_in_claim))
        )
    if negated_only_in_excerpt:
        findings.append(
            "excerpt negates term(s) affirmed by claim: "
            + ", ".join(sorted(negated_only_in_excerpt))
        )
    return findings


def _claim_signature(claim: str, entity: str) -> str:
    """Build a conservative predicate skeleton for rows lacking a fact key."""
    text = unicodedata.normalize("NFKC", claim).casefold()
    entity_text = unicodedata.normalize("NFKC", entity).casefold().strip()
    if entity_text:
        text = text.replace(entity_text, " ")
    for terms in DIRECTION_TERMS.values():
        for term in sorted(terms, key=len, reverse=True):
            pattern = rf"\b{re.escape(term)}\b" if term.isascii() else re.escape(term)
            text = re.sub(pattern, " <direction> ", text)
    text = re.sub(
        r"\b(?:did\s+|does\s+|do\s+|is\s+|are\s+|was\s+|were\s+|"
        r"has\s+|have\s+)?(?:not|never)\b",
        " ",
        text,
    )
    text = re.sub(r"(?:没有|并未|未曾|不曾|未|不)", "", text)
    text = NUMBER_RE.sub(" <number> ", text)
    return re.sub(r"[^a-z0-9\u3400-\u9fff<>]+", "", text)


def _claim_value_signature(claim: str) -> str:
    return "|".join(
        (
            "numbers=" + ",".join(sorted(_normalized_numbers(claim))),
            "directions=" + ",".join(sorted(_direction_signals(claim))),
            "negated=" + ",".join(sorted(_negated_terms(claim))),
        )
    )


def _fact_group_key(record: dict[str, object]) -> tuple[str, ...]:
    entity = str(record.get("entity") or "")
    fact_key = str(record.get("fact_key") or "").strip()
    if fact_key:
        return (
            "structured",
            normalized_text(entity),
            normalized_text(fact_key),
            normalized_text(record.get("time_basis") or "unspecified"),
            normalized_text(record.get("scope") or "unspecified"),
        )
    return (
        "heuristic",
        normalized_text(entity),
        _claim_signature(str(record.get("claim") or ""), entity),
        normalized_text(record.get("time_basis") or "unspecified"),
        normalized_text(record.get("scope") or "unspecified"),
    )


def canonical_evidence(record: dict[str, object], *, topic: str) -> str:
    entity = str(record.get("entity") or topic).strip()
    claim = str(record.get("claim") or "").strip()
    source_type = str(record.get("source_type") or "secondary").strip()
    source_url = str(record.get("source_url") or "").strip()
    return " | ".join((entity, claim, source_type, source_url))


def validate_evidence_ledger(
    path: Path,
    topic: str,
) -> tuple[list[str], list[str], dict[str, object]]:
    """Validate provenance and exclude contradictory verified candidates."""
    structural_errors: list[str] = []
    warnings: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"could not read evidence ledger {path.name}: {exc}"], [], {}
    except json.JSONDecodeError as exc:
        return [f"{path.name}: invalid JSON: {exc}"], [], {}
    if not isinstance(payload, dict):
        return [f"{path.name}: root must be a JSON object"], [], {}

    schema_version = payload.get("schema_version", 1)
    if not isinstance(schema_version, int) or schema_version < 1:
        structural_errors.append(f"{path.name}: schema_version must be a positive integer")
    ledger_topic = payload.get("topic")
    if ledger_topic not in (None, topic):
        structural_errors.append(f"{path.name}: topic must equal {topic!r}")

    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list):
        structural_errors.append(f"{path.name}: evidence must be an array")
        raw_evidence = []

    verified_evidence: list[dict[str, object]] = []
    verified_candidates: list[dict[str, object]] = []
    status_counts = {status: 0 for status in EVIDENCE_STATUSES}
    seen: set[str] = set()
    for index, raw_record in enumerate(raw_evidence):
        label = f"{path.name}: evidence.{index}"
        if not isinstance(raw_record, dict):
            warnings.append(f"{label} is not an object; excluded")
            continue

        status = str(raw_record.get("status") or "unverified").casefold().strip()
        if status not in EVIDENCE_STATUSES:
            warnings.append(f"{label}.status is unknown; treated as unverified")
            status = "unverified"
        status_counts[status] += 1
        if status != "verified":
            warnings.append(f"{label}: status={status}; excluded from verified facts")
            continue

        claim = str(raw_record.get("claim") or "").strip()
        source_url = str(raw_record.get("source_url") or "").strip()
        excerpt = str(raw_record.get("evidence_excerpt") or "").strip()
        row_warnings: list[str] = []
        if not claim:
            row_warnings.append("claim is empty")
        source_type = str(raw_record.get("source_type") or "secondary").strip()
        concrete, source_error = is_concrete_source_page(
            source_url,
            source_type=source_type,
        )
        if not concrete and source_error:
            row_warnings.append(source_error)
        if not excerpt:
            row_warnings.append("evidence_excerpt is empty")
        if claim and excerpt:
            row_warnings.extend(claim_excerpt_conflicts(claim, excerpt))
        if row_warnings:
            warnings.extend(f"{label}: {warning}; excluded" for warning in row_warnings)
            continue

        canonical = canonical_evidence(raw_record, topic=topic)
        if canonical in seen:
            warnings.append(f"{label}: duplicate verified fact; excluded")
            continue
        seen.add(canonical)
        candidate = {
            "entity": str(raw_record.get("entity") or topic).strip(),
            "claim": claim,
            "source_url": source_url,
            "source_type": source_type,
            "evidence_excerpt": excerpt,
            "confidence": str(raw_record.get("confidence") or "medium").strip(),
            "status": "verified",
            "canonical": canonical,
            **{
                field: raw_record[field]
                for field in (
                    "fact_key",
                    "fact_value",
                    "time_basis",
                    "scope",
                    "unit",
                    "published_at",
                    "retrieved_at",
                )
                if field in raw_record
            },
            "_ledger_index": index,
        }
        verified_candidates.append(candidate)

    candidates_by_fact: dict[
        tuple[str, ...], list[dict[str, object]]
    ] = defaultdict(list)
    for candidate in verified_candidates:
        candidates_by_fact[_fact_group_key(candidate)].append(candidate)

    auto_conflicting_indices: set[int] = set()
    detected_conflicts: list[dict[str, object]] = []
    for candidates in candidates_by_fact.values():
        if len(candidates) < 2:
            continue
        if all(str(item.get("fact_value") or "").strip() for item in candidates):
            values = {
                normalized_text(item.get("fact_value")) for item in candidates
            }
        else:
            values = {
                _claim_value_signature(str(item.get("claim") or ""))
                for item in candidates
            }
        units = {
            normalized_text(item.get("unit"))
            for item in candidates
            if str(item.get("unit") or "").strip()
        }
        reasons: list[str] = []
        if len(values) > 1:
            reasons.append("different values or directions")
        if len(units) > 1:
            reasons.append("different units")
        if not reasons:
            continue

        indices = sorted(int(item["_ledger_index"]) for item in candidates)
        auto_conflicting_indices.update(indices)
        conflict = {
            "entity": candidates[0]["entity"],
            "fact_key": candidates[0].get("fact_key"),
            "time_basis": candidates[0].get("time_basis"),
            "scope": candidates[0].get("scope"),
            "ledger_indices": indices,
            "reason": " and ".join(reasons),
        }
        detected_conflicts.append(conflict)
        warnings.append(
            f"{path.name}: evidence records {indices} conflict for the same fact "
            f"({conflict['reason']}); excluded from verified facts"
        )

    for candidate in verified_candidates:
        ledger_index = int(candidate.pop("_ledger_index"))
        if ledger_index not in auto_conflicting_indices:
            verified_evidence.append(candidate)

    summary: dict[str, object] = {
        "evidence_schema_version": schema_version,
        "evidence_file": str(path.resolve()),
        "evidence_count": len(raw_evidence),
        "verified_evidence_count": len(verified_evidence),
        "conflicting_evidence_count": (
            status_counts["conflicting"] + len(auto_conflicting_indices)
        ),
        "auto_conflicting_evidence_count": len(auto_conflicting_indices),
        "detected_conflict_count": len(detected_conflicts),
        "detected_conflicts": detected_conflicts,
        "unverified_evidence_count": status_counts["unverified"],
        "verified_evidence": verified_evidence,
    }
    return structural_errors, warnings, summary


def main() -> int:
    args = parse_args()
    research_dir = args.research_dir
    structural_errors: list[str] = []
    warnings: list[str] = []
    evidence_summary: dict[str, object] = {}
    narrative_files: list[Path] = []

    if not research_dir.exists():
        structural_errors.append(f"research dir does not exist: {research_dir}")
    elif not research_dir.is_dir():
        structural_errors.append(f"research path is not a directory: {research_dir}")
    else:
        narrative_files = sorted(
            path
            for path in research_dir.glob(f"{args.topic}_*.md")
            if path.is_file() and path.stat().st_size > 0
        )
        if not narrative_files:
            structural_errors.append(
                f"missing narrative research artifact for topic {args.topic!r}"
            )

        evidence_file = research_dir / f"{args.topic}_evidence.json"
        if not evidence_file.is_file():
            structural_errors.append(f"missing required file: {evidence_file.name}")
        else:
            ledger_errors, ledger_warnings, evidence_summary = (
                validate_evidence_ledger(evidence_file, args.topic)
            )
            structural_errors.extend(ledger_errors)
            warnings.extend(ledger_warnings)

        for path in narrative_files:
            warnings.extend(collect_footnote_warnings(path))

    verified_count = int(evidence_summary.get("verified_evidence_count", 0))
    delivery_allowed = not structural_errors
    quality_ok = bool(delivery_allowed and verified_count > 0 and not warnings)
    delivery_mode = (
        "invalid"
        if not delivery_allowed
        else "framework"
        if verified_count == 0
        else "partial"
        if warnings
        else "full"
    )
    context_files = [path.name for path in narrative_files]
    evidence_file_value = evidence_summary.get("evidence_file")
    if evidence_file_value:
        context_files.append(Path(str(evidence_file_value)).name)

    presentation_handoff = {
        "schema_version": 1,
        "delivery_mode": delivery_mode,
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
        "gaps": [*structural_errors, *warnings],
        "quality_summary": {
            "quality_ok": quality_ok,
            "issue_count": len(structural_errors),
            "warning_count": len(warnings),
            "actual_dimensions": len(
                [path for path in narrative_files if "_dim" in path.stem]
            ),
            "recommended_dimensions": None,
        },
        "context_files": context_files,
    }
    report_payload: dict[str, object] = {
        "ok": delivery_allowed,
        "quality_ok": quality_ok,
        "delivery_allowed": delivery_allowed,
        "handoff_status": delivery_mode,
        "validator": "research-synthesis",
        "route": args.route,
        "topic": args.topic,
        "research_dir": str(research_dir.resolve()),
        "min_dimensions": None,
        "dimension_count": len(
            [path for path in narrative_files if "_dim" in path.stem]
        ),
        "files_checked": [str(path.resolve()) for path in narrative_files],
        "issues": structural_errors,
        "warnings": warnings,
        "presentation_handoff": presentation_handoff,
        **evidence_summary,
    }
    write_report(args.report, report_payload)

    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)
    if structural_errors:
        for error in structural_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        f"OK: {args.route} research handoff for topic {args.topic!r} "
        f"validated with delivery_mode={delivery_mode}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
