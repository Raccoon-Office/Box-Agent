"""Deterministic merge, filter, vote tally and verdict for the G2 vote review.

This module is pure standard-library Python. It never calls an LLM, so the
decision of which findings reach a human is reproducible from the recorded
hunter findings and verifier votes alone.

Rules implemented here (see ``general_review/vote/README.md``):

* Style-only categories are dropped before voting.
* Findings in the same file and category within ``LINE_WINDOW`` lines merge.
* A finding is posted when CONFIRMED votes reach ``ceil(2K/3)`` of the K
  requested verifiers. Missing, malformed or evidence-free votes are UNSURE.
* A P0/P1 finding with at least one confirmation but below the threshold is
  kept as "disputed" for human review instead of being dropped.
* Final severity is the median of the confirming verifiers' severities; ties
  resolve toward the more severe level.
* A majority of confirmers calling the trigger "contrived" caps a non-P0
  finding at P3: the defect may be real, but it does not block.
* A finding outside the changed lines of the merge-base diff is Pre-existing
  and only blocks when it is P0. A target-interaction finding is exempt: it is
  caused by this change meeting the target branch, wherever its line falls.
"""

from __future__ import annotations

import fnmatch
import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

SEVERITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")
BLOCKING_SEVERITIES = frozenset({"P0", "P1", "P2"})
DROPPED_CATEGORIES = frozenset({"style", "naming", "formatting", "cleanup"})
VERDICTS = frozenset({"CONFIRMED", "REJECTED", "UNSURE"})
REACHABILITY = frozenset({"normal", "edge", "contrived"})
TARGET_INTERACTION = "target-interaction"
LINE_WINDOW = 3

POSTED = "posted"
DISPUTED = "disputed"
DROPPED = "dropped"


def _severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def _normalize_severity(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if text in SEVERITIES else None


def _normalize_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass
class Finding:
    id: str
    file: str
    line: int
    category: str
    severity: str
    title: str
    trigger: str
    impact: str
    evidence: str
    suggestion: str
    lenses: list[str] = field(default_factory=list)
    instances: list[dict[str, Any]] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Best-effort identity across review rounds (line numbers drift)."""

        title = re.sub(r"\s+", " ", self.title.strip().lower())
        raw = f"{self.file}|{self.category}|{title}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file": self.file,
            "line": self.line,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "trigger": self.trigger,
            "impact": self.impact,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
            "lenses": list(self.lenses),
            "instances": list(self.instances),
            "fingerprint": self.fingerprint(),
        }


@dataclass(frozen=True)
class Vote:
    finding_id: str
    voter: str
    verdict: str
    severity: str | None
    evidence: tuple[dict[str, Any], ...]
    reason: str
    reachability: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "voter": self.voter,
            "verdict": self.verdict,
            "severity": self.severity,
            "evidence": [dict(item) for item in self.evidence],
            "reason": self.reason,
            "reachability": self.reachability,
        }


@dataclass
class Outcome:
    finding: Finding
    status: str
    confirmations: int
    required: int
    voters: int
    severity: str
    pre_existing: bool
    votes: list[Vote]
    reachability_capped: bool = False

    @property
    def label(self) -> str:
        if self.pre_existing:
            return "Pre-existing"
        return "Important" if self.severity in BLOCKING_SEVERITIES else "Nit"

    @property
    def blocking(self) -> bool:
        if self.status != POSTED:
            return False
        if self.pre_existing:
            return self.severity == "P0"
        return self.severity in BLOCKING_SEVERITIES

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding": self.finding.to_dict(),
            "status": self.status,
            "confirmations": self.confirmations,
            "required": self.required,
            "voters": self.voters,
            "severity": self.severity,
            "label": self.label,
            "pre_existing": self.pre_existing,
            "blocking": self.blocking,
            "reachability_capped": self.reachability_capped,
            "votes": [vote.to_dict() for vote in self.votes],
        }


def parse_findings(raw: Any, *, lens: str, id_prefix: str) -> tuple[list[Finding], list[str]]:
    """Validate one hunter's JSON payload. Malformed items are reported, not guessed."""

    errors: list[str] = []
    items = raw.get("findings") if isinstance(raw, Mapping) else None
    if not isinstance(items, list):
        return [], [f"{lens}: payload has no 'findings' list"]
    findings: list[Finding] = []
    for index, item in enumerate(items):
        where = f"{lens}[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{where}: finding is not an object")
            continue
        path = _normalize_path(item.get("file"))
        line = _positive_int(item.get("line"))
        severity = _normalize_severity(item.get("severity"))
        title = str(item.get("title") or "").strip()
        if not path or line is None or severity is None or not title:
            errors.append(f"{where}: requires file, positive line, severity P0-P3 and title")
            continue
        instances = []
        for instance in item.get("instances") or []:
            if isinstance(instance, Mapping):
                instance_path = _normalize_path(instance.get("file"))
                instance_line = _positive_int(instance.get("line"))
                if instance_path and instance_line is not None:
                    instances.append({"file": instance_path, "line": instance_line})
        findings.append(Finding(
            id=f"{id_prefix}{index + 1}",
            file=path,
            line=line,
            category=str(item.get("category") or "correctness").strip().lower(),
            severity=severity,
            title=title,
            trigger=str(item.get("trigger") or "").strip(),
            impact=str(item.get("impact") or "").strip(),
            evidence=str(item.get("evidence") or "").strip(),
            suggestion=str(item.get("suggestion") or "").strip(),
            lenses=[lens],
            instances=instances,
        ))
    return findings, errors


def filter_findings(
    findings: Iterable[Finding], *, previous_fingerprints: Iterable[str] = (),
) -> tuple[list[Finding], list[dict[str, str]]]:
    """Drop style-only categories and findings already posted in earlier rounds."""

    known = set(previous_fingerprints)
    kept: list[Finding] = []
    dropped: list[dict[str, str]] = []
    for finding in findings:
        if finding.category in DROPPED_CATEGORIES:
            dropped.append({"id": finding.id, "reason": f"category '{finding.category}' is not reviewed"})
        elif finding.fingerprint() in known:
            dropped.append({"id": finding.id, "reason": "already posted in an earlier round"})
        else:
            kept.append(finding)
    return kept, dropped


def merge_findings(findings: Sequence[Finding], *, line_window: int = LINE_WINDOW) -> list[Finding]:
    """Merge near-duplicate findings from different lenses.

    The most severe report (then the earliest) stays primary; lenses and
    instances from merged duplicates are kept on it.
    """

    ordered = sorted(
        findings, key=lambda item: (item.file, item.category, item.line, _severity_rank(item.severity)),
    )
    merged: list[Finding] = []
    for finding in ordered:
        target = next(
            (
                existing for existing in merged
                if existing.file == finding.file
                and existing.category == finding.category
                and abs(existing.line - finding.line) <= line_window
            ),
            None,
        )
        if target is None:
            merged.append(Finding(**{**finding.__dict__, "lenses": list(finding.lenses),
                                     "instances": list(finding.instances)}))
            continue
        if _severity_rank(finding.severity) < _severity_rank(target.severity):
            for name in ("id", "line", "severity", "title", "trigger", "impact", "evidence", "suggestion"):
                setattr(target, name, getattr(finding, name))
        for lens in finding.lenses:
            if lens not in target.lenses:
                target.lenses.append(lens)
        for instance in finding.instances:
            if instance not in target.instances:
                target.instances.append(instance)
    return merged


def parse_votes(raw: Any, *, voter: str, finding_ids: Iterable[str]) -> tuple[list[Vote], list[str]]:
    """Validate one verifier's payload. Votes without evidence count as UNSURE."""

    expected = set(finding_ids)
    errors: list[str] = []
    items = raw.get("votes") if isinstance(raw, Mapping) else None
    if not isinstance(items, list):
        return [], [f"{voter}: payload has no 'votes' list"]
    votes: dict[str, Vote] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            errors.append(f"{voter}[{index}]: vote is not an object")
            continue
        finding_id = str(item.get("id") or "").strip()
        if finding_id not in expected:
            errors.append(f"{voter}[{index}]: unknown finding id '{finding_id}'")
            continue
        if finding_id in votes:
            errors.append(f"{voter}[{index}]: duplicate vote for '{finding_id}' ignored")
            continue
        verdict = str(item.get("verdict") or "").strip().upper()
        if verdict not in VERDICTS:
            verdict = "UNSURE"
        evidence = tuple(
            {"file": _normalize_path(entry.get("file")), "line": _positive_int(entry.get("line")),
             "note": str(entry.get("note") or "").strip()}
            for entry in (item.get("evidence") or [])
            if isinstance(entry, Mapping) and _normalize_path(entry.get("file"))
            and _positive_int(entry.get("line")) is not None
        )
        if verdict != "UNSURE" and not evidence:
            verdict = "UNSURE"
        reachability = str(item.get("reachability") or "").strip().lower()
        votes[finding_id] = Vote(
            finding_id=finding_id,
            voter=voter,
            verdict=verdict,
            severity=_normalize_severity(item.get("severity")),
            evidence=evidence,
            reason=str(item.get("reason") or "").strip(),
            reachability=reachability if reachability in REACHABILITY else None,
        )
    return list(votes.values()), errors


def required_confirmations(voters: int) -> int:
    if voters < 1:
        raise ValueError("at least one verifier is required")
    return math.ceil(2 * voters / 3)


def _median_severity(severities: Sequence[str]) -> str:
    ranks = sorted(_severity_rank(item) for item in severities)
    return SEVERITIES[ranks[(len(ranks) - 1) // 2]]


def tally_finding(
    finding: Finding, votes: Sequence[Vote], *, voters: int, pre_existing: bool,
) -> Outcome:
    """Decide one finding. ``voters`` is the number requested, not received."""

    required = required_confirmations(voters)
    confirmed = [vote for vote in votes if vote.verdict == "CONFIRMED"]
    confirm_severities = [vote.severity for vote in confirmed if vote.severity]
    severity = _median_severity(confirm_severities) if confirm_severities else finding.severity
    contrived = sum(1 for vote in confirmed if vote.reachability == "contrived")
    capped = severity in BLOCKING_SEVERITIES - {"P0"} and contrived * 2 > len(confirmed)
    if capped:
        severity = "P3"
    if len(confirmed) >= required:
        status = POSTED
    elif confirmed and severity in {"P0", "P1"}:
        status = DISPUTED
    else:
        status = DROPPED
    return Outcome(
        finding=finding,
        status=status,
        confirmations=len(confirmed),
        required=required,
        voters=voters,
        severity=severity,
        pre_existing=pre_existing,
        votes=list(votes),
        reachability_capped=capped,
    )


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(unified_diff_u0: str) -> dict[str, set[int]]:
    """New-side changed line numbers from ``git diff -U0`` output.

    A pure deletion records the new-side line at the deletion point so a
    finding anchored there is not mislabelled Pre-existing.
    """

    result: dict[str, set[int]] = {}
    current: str | None = None
    for line in unified_diff_u0.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = None if target == "/dev/null" else _normalize_path(target[2:] if target.startswith("b/") else target)
            if current is not None:
                result.setdefault(current, set())
            continue
        match = _HUNK_RE.match(line)
        if match and current is not None:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count == 0:
                result[current].add(max(start, 1))
            else:
                result[current].update(range(start, start + count))
    return result


def is_pre_existing(
    finding: Finding, changes: Mapping[str, set[int]], *, target_ahead: bool = False,
) -> bool:
    if target_ahead and finding.category == TARGET_INTERACTION:
        return False
    return finding.line not in changes.get(finding.file, set())


def classify_tier(paths: Iterable[str], rules: Mapping[str, Any]) -> str:
    """Highest tier across changed paths. Unmatched paths are ``default``."""

    order = ("low", "medium", "high")
    default = rules.get("default", "medium")
    best = "low"
    seen_any = False
    for raw_path in paths:
        path = _normalize_path(raw_path)
        if not path:
            continue
        seen_any = True
        tier = default
        for candidate in ("high", "medium", "low"):
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in rules.get(candidate, ())):
                tier = candidate
                break
        if order.index(tier) > order.index(best):
            best = tier
    return best if seen_any else default


_TEMPLATE_LABEL_ONLY = re.compile(r"^\s*-\s*[^:]+:\s*$")


def tpr_status(tpr_text: str | None) -> tuple[str, dict[str, str]]:
    """Check that Task/Proof/Risk sections exist and contain more than template labels.

    A filled Proof section is only "provided"; ``assess_proof`` decides whether
    it is sufficient, because a non-empty section says nothing about coverage.
    """

    if not tpr_text or not tpr_text.strip():
        return "not provided by runtime", {name: "unavailable" for name in ("Task", "Proof", "Risk")}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in tpr_text.splitlines():
        heading = re.match(r"^\s*#{2,4}\s*(Task|Proof|Risk)\b", line, re.I)
        if heading:
            current = heading.group(1).capitalize()
            sections.setdefault(current, [])
        elif re.match(r"^\s*#{1,4}\s", line):
            current = None
        elif current is not None:
            sections[current].append(line)
    details: dict[str, str] = {}
    for name, filled in (("Task", "complete"), ("Proof", "provided"), ("Risk", "complete")):
        body = [line for line in sections.get(name, []) if line.strip() and not _TEMPLATE_LABEL_ONLY.match(line)]
        details[name] = filled if body else ("insufficient" if name == "Proof" else "incomplete")
    complete = all(value in {"complete", "provided"} for value in details.values())
    return ("complete" if complete else "incomplete"), details


def assess_proof(proof: str, *, lens_completed: bool, blocked: bool) -> str:
    """Map a provided Proof section to the standard's values using the proof lens result."""

    if proof != "provided":
        return proof
    if not lens_completed:
        return "unavailable"
    return "insufficient" if blocked else "sufficient"


def decide_verdict(
    *, preflight: str, tpr: str, outcomes: Sequence[Outcome], agent_failures: int,
    merge_conflicts: int = 0,
) -> str:
    """PR_REVIEW_STANDARD section 9, plus: failed agents or merge conflicts can never yield APPROVE."""

    if preflight != "success":
        return "REQUEST_CHANGES"
    if tpr == "incomplete":
        return "REQUEST_CHANGES"
    if any(outcome.blocking for outcome in outcomes):
        return "REQUEST_CHANGES"
    if tpr != "complete" or agent_failures or merge_conflicts:
        return "COMMENT"
    if any(outcome.status == DISPUTED for outcome in outcomes):
        return "COMMENT"
    return "APPROVE"


__all__ = [
    "BLOCKING_SEVERITIES",
    "DISPUTED",
    "DROPPED",
    "DROPPED_CATEGORIES",
    "Finding",
    "Outcome",
    "POSTED",
    "REACHABILITY",
    "SEVERITIES",
    "TARGET_INTERACTION",
    "Vote",
    "assess_proof",
    "changed_lines",
    "classify_tier",
    "decide_verdict",
    "filter_findings",
    "is_pre_existing",
    "merge_findings",
    "parse_findings",
    "parse_votes",
    "required_confirmations",
    "tally_finding",
    "tpr_status",
]
