"""Validate persisted Skill facts before resolving or replacing session state."""

from hashlib import sha256
from typing import Any

from .skill_dependencies import SkillDependencyError, resolve_required_skills


def invalid_restore(field: str) -> SkillDependencyError:
    return SkillDependencyError("SKILL_RESTORE_INVALID", f"Invalid Skill restore metadata: {field}.")


def validate_restore_records(records: list[dict[str, Any]]) -> None:
    if not isinstance(records, list):
        raise invalid_restore("records must be a list")
    names: set[str] = set()
    orders: set[int] = set()
    for record in records:
        if not isinstance(record, dict):
            raise invalid_restore("each record must be an object")
        name = record.get("name")
        if not isinstance(name, str) or not name.strip() or name != name.strip() or name in names:
            raise invalid_restore("name must be a unique nonempty name")
        names.add(name)
        revision = record.get("sha256")
        # Legacy hosts supplied opaque hash labels; preserve that compatibility.
        if not isinstance(revision, str) or not revision.strip():
            raise invalid_restore("sha256 must be a nonempty string")
        order = record.get("loadOrder")
        if type(order) is not int or order < 1 or order in orders:
            raise invalid_restore("loadOrder must be a unique positive integer")
        orders.add(order)
        for field in ("source", "path", "reason", "prompt"):
            if field in record and not isinstance(record[field], str):
                raise invalid_restore(f"{field} must be a string")
        if "deliveredComplete" in record and type(record["deliveredComplete"]) is not bool:
            raise invalid_restore("deliveredComplete must be boolean")
        ranges = record.get("deliveredRanges", ())
        if not isinstance(ranges, (list, tuple)):
            raise invalid_restore("deliveredRanges must be a sequence")
        for pair in ranges:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(type(value) is not int for value in pair)
                    or not 0 <= pair[0] < pair[1]):
                raise invalid_restore("deliveredRanges must contain valid integer [start, end] pairs")


def recover_available_records(records: Any, loader: Any) -> list[dict[str, Any]]:
    """Project legacy optional state onto current sources without writing facts.

    Missing legacy hashes remain explicitly unverified. Invalid modern coverage
    metadata is skipped, so recovery never grants visibility from damaged data.
    """
    if not isinstance(records, list) or loader is None:
        return []
    loader.maybe_reload()
    recovered: list[dict[str, Any]] = []
    names: set[str] = set()
    orders: set[int] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            continue
        try:
            skill = resolve_required_skills(loader, [name])[-1]
            candidate = dict(record)
            candidate.setdefault("sha256", "unverified-legacy-reference")
            candidate.setdefault("loadOrder", max(orders, default=0) + 1)
            validate_restore_records([candidate])
            if candidate["loadOrder"] in orders:
                continue
            prompt = skill.to_prompt()
            if candidate["sha256"] == sha256(prompt.encode()).hexdigest() and any(
                end > len(prompt.splitlines()) for _, end in candidate.get("deliveredRanges", ())
            ):
                continue
        except SkillDependencyError:
            continue
        recovered.append(candidate)
        names.add(name)
        orders.add(candidate["loadOrder"])
    return recovered
