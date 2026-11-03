#!/usr/bin/env python3
"""Prepare bounded, verbatim source views for frozen Production groups.

Only _trace/group-input/<group>/part-NN.md is written. The returned part list
is authoritative for this invocation; older parts are deliberately not deleted.
Reference routes use ``- filename.md: Exact heading > Exact child heading``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys


MAX_LINES = 450
MAX_CHARS = 40_000
SKILL_ROOT = Path(__file__).resolve().parents[1]
GROUP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


@dataclass
class Source:
    path: Path
    lines: list[str]
    start: int
    end: int

    def location(self, start: int | None = None, end: int | None = None) -> dict:
        return {"path": str(self.path), "start_line": start or self.start,
                "end_line": end or self.end}


def fail(path: Path, line: int, message: str) -> None:
    raise ValueError(f"{path}:{line}: {message}")


def safe_path(root: Path, value: str | Path) -> Path:
    """Check lexical containment and every existing component before opening it."""
    path = Path(value)
    if ".." in path.parts:
        raise ValueError(f"unsafe path traversal: {path}")
    path = path if path.is_absolute() else root / path
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes allowed root {root}: {path}") from None
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise ValueError(f"symlink is not allowed: {current}")
    return path


def read_source(path: Path) -> Source:
    with path.open(encoding="utf-8", newline="") as stream:
        lines = stream.read().splitlines(keepends=True)
    if not lines:
        fail(path, 1, "required source is empty")
    return Source(path, lines, 1, len(lines))


def markdown_lines(source: Source):
    """Ignore fenced examples while parsing the narrow production contract."""
    fence = None
    for number, line in enumerate(source.lines, 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        yield number, line.rstrip("\r\n")


def headings(source: Source) -> list[tuple[int, int, str, tuple[str, ...]]]:
    found = []
    parents = []
    for number, line in markdown_lines(source):
        match = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            continue
        level, title = len(match[1]), re.sub(r"\s+#+$", "", match[2])
        while parents and parents[-1][0] >= level:
            parents.pop()
        parents.append((level, title))
        found.append((number, level, title, tuple(item[1] for item in parents)))
    return found


def section_end(source: Source, heading: tuple, outline: list) -> int:
    return next((n - 1 for n, level, _, _ in outline
                 if n > heading[0] and level <= heading[1]), len(source.lines))


def field(source: Source, name: str, start: int = 1, end: int | None = None) -> tuple[int, str]:
    matches = []
    for number, line in markdown_lines(source):
        if start <= number <= (end or source.end):
            match = re.fullmatch(r"\s*-\s+" + re.escape(name) + r"\s*[:：]\s*(.*?)\s*", line)
            if match:
                matches.append((number, match[1]))
    if len(matches) != 1 or not matches[0][1]:
        fail(source.path, start, f"requires exactly one nonempty {name} field")
    return matches[0]


def production_groups(source: Source, expected: int) -> list[dict]:
    outline = headings(source)
    sections = [h for h in outline if h[1:3] == (2, "Production groups")]
    if len(sections) != 1:
        fail(source.path, 1, "requires exactly one ## Production groups section")
    section = sections[0]
    end = section_end(source, section, outline)
    group_heads = [h for h in outline if section[0] < h[0] <= end and h[1] == 3]
    groups, owners = [], {}
    names = set()
    for index, heading in enumerate(group_heads):
        start, _, name, _ = heading
        if not GROUP_ID.fullmatch(name):
            fail(source.path, start, f"unsafe group id: {name}")
        if name in names:
            fail(source.path, start, f"duplicate group id: {name}")
        names.add(name)
        stop = group_heads[index + 1][0] - 1 if index + 1 < len(group_heads) else end
        line, raw_pages = field(source, "pages", start, stop)
        if not re.fullmatch(r"[0-9]+(?:\s*,\s*[0-9]+)*", raw_pages):
            fail(source.path, line, "pages must be an explicit comma-separated list of page numbers")
        pages = [int(page.strip()) for page in raw_pages.split(",")]
        for page in pages:
            if not 1 <= page <= expected:
                fail(source.path, line, f"page {page} is outside expected range 1..{expected}")
            if page in owners:
                fail(source.path, line, f"page {page} belongs to more than one group/list entry ({owners[page]}, {name})")
            owners[page] = name
        field(source, "boundary_handoff", start, stop)
        groups.append({"group": name, "pages": pages,
                       "contract": Source(source.path, source.lines, start, stop)})
    missing = sorted(set(range(1, expected + 1)) - owners.keys())
    if missing:
        fail(source.path, section[0], f"missing pages from Production groups: {missing}")
    return groups


def references(plan: Source, cache: dict[Path, Source]) -> list[Source]:
    outline = headings(plan)
    sections = [h for h in outline if h[1:3] == (2, "Reference route")]
    if len(sections) != 1:
        fail(plan.path, 1, "requires exactly one ## Reference route section")
    start, end = sections[0][0], section_end(plan, sections[0], outline)
    selected = []
    for number, line in markdown_lines(plan):
        if not start < number <= end or not line.strip():
            continue
        match = re.fullmatch(r"\s*-\s+([^:：]+)[:：]\s*(.+?)\s*", line)
        if not match:
            fail(plan.path, number, "Reference route requires filename.md: Exact heading > Child heading")
        filename = match[1].strip().strip("`")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.md", filename):
            fail(plan.path, number, f"reference filename must be a plain .md filename: {filename}")
        path = safe_path(SKILL_ROOT, Path("references") / filename)
        if path not in cache:
            try:
                cache[path] = read_source(path)
            except OSError as error:
                fail(plan.path, number, f"reference {path} cannot be read: {error}")
        source = cache[path]
        route = tuple(item.strip() for item in match[2].split(" > "))
        reference_headings = headings(source)
        hits = [h for h in reference_headings if h[3][-len(route):] == route]
        if len(hits) != 1:
            reason = "not found" if not hits else f"ambiguous at lines {[h[0] for h in hits]}"
            fail(plan.path, number, f"reference {path}: heading {match[2]!r} {reason}; use a unique exact heading path")
        heading = hits[0]
        selected.append(Source(path, source.lines, heading[0], section_end(source, heading, reference_headings)))
    if not selected:
        fail(plan.path, start, "Reference route must name at least one reference section")
    return selected


def merge_sources(sources: list[Source]) -> list[Source]:
    """Deduplicate shared and overlapping sections without rewriting their text."""
    by_path = {}
    for source in sources:
        by_path.setdefault(source.path, []).append(source)
    merged = []
    for candidates in by_path.values():
        for source in sorted(candidates, key=lambda item: (item.start, item.end)):
            if merged and merged[-1].path == source.path and source.start <= merged[-1].end + 1:
                merged[-1].end = max(merged[-1].end, source.end)
            else:
                merged.append(Source(source.path, source.lines, source.start, source.end))
    return merged


def asset_ids(plan: Source) -> list[str]:
    ids = []
    for number, line in markdown_lines(plan):
        match = re.match(r"\s*-\s+asset_id\s*[:：]\s*(.*)", line)
        if match:
            value = re.match(r"`?([A-Za-z0-9_]+)(?:`|\s|$)", match[1])
            if not value:
                fail(plan.path, number, "asset_id must begin with a stable alphanumeric/underscore id")
            ids.append(value[1])
    return ids


def catalog_source(root: Path, ids: list[str]) -> Source | None:
    if not ids:
        return None
    source = read_source(safe_path(root, "assets/catalog.json"))
    payload = json.loads("".join(source.lines))
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list):
        fail(source.path, 1, "asset catalog requires an assets array")
    for asset_id in ids:
        matches = [item for item in assets if isinstance(item, dict) and item.get("asset_id") == asset_id]
        if len(matches) != 1:
            fail(source.path, 1, f"asset_id {asset_id!r} must have exactly one catalog entry")
        raw_path = matches[0].get("path")
        if not isinstance(raw_path, str) or not raw_path.startswith("assets/"):
            fail(source.path, 1, f"asset {asset_id!r} path must be relative under assets/")
        path = safe_path(root, raw_path)
        if not path.is_file():
            fail(source.path, 1, f"asset {asset_id!r} path does not exist: {path}")
    # Keep the original JSON rather than dropping provenance or reserializing entries.
    return source


def annotation(source: Source, start: int, end: int, raw: str) -> str:
    marker = json.dumps(source.location(start, end), ensure_ascii=False)
    separator = "" if raw.endswith(("\n", "\r")) else "\n"
    return f"<!-- source: {marker} -->\n{raw}{separator}<!-- end-source -->\n"


def fits(text: str) -> bool:
    return len(text) <= MAX_CHARS and len(text.splitlines()) <= MAX_LINES


def split_parts(group: dict, sources: list[Source], root: Path) -> list[str]:
    prefix = (f"# Production group {group['group']}\n"
              f"deck_dir: {root}\n"
              f"pages: {','.join(str(page) for page in group['pages'])}\n"
              "Read every part in the returned inputs list. Source text below is verbatim.\n"
              "Write only these HTML files:\n" +
              "".join(f"- {path}\n" for path in group["outputs"]) + "\n")
    if not fits(prefix):
        raise ValueError(f"group {group['group']}: output path header exceeds part limits")
    parts, current = [], prefix
    for source in sources:
        start = source.start
        while start <= source.end:
            raw, end = "", start - 1
            while end < source.end:
                candidate = raw + source.lines[end]
                block = annotation(source, start, end + 1, candidate)
                if not fits(current + block):
                    break
                raw, end = candidate, end + 1
            if end < start:
                if current != prefix:
                    parts.append(current)
                    current = prefix
                    continue
                fail(source.path, start, "single line plus source/header annotations exceeds 450 lines or 40000 characters")
            current += annotation(source, start, end, raw)
            start = end + 1
            if start <= source.end:
                parts.append(current)
                current = prefix
    if current != prefix:
        parts.append(current)
    return parts


def prepare(root_value: str, expected: int, group_id: str | None, tool_contract: str | None) -> dict:
    root = Path(root_value)
    if not root.is_absolute():
        raise ValueError("deck_dir must be an already verified absolute path")
    if ".." in root.parts or root.resolve(strict=True) != root:
        raise ValueError(f"deck_dir must be canonical, without traversal or symlink: {root}")
    if not root.is_dir():
        raise ValueError(f"deck_dir is not a directory: {root}")
    if expected <= 0:
        raise ValueError("expected must be a positive page count")
    task = read_source(safe_path(root, "task_pack.json"))
    pack = json.loads("".join(task.lines))
    if not isinstance(pack, dict) or pack.get("deck_dir") != str(root):
        fail(task.path, 1, "task_pack.deck_dir must match the actual absolute task_pack parent directory")
    deck = read_source(safe_path(root, "plan/deck.md"))
    brief = read_source(safe_path(root, "plan/design-brief.md"))
    css = read_source(safe_path(root, "base.css"))
    instructions = read_source(safe_path(SKILL_ROOT, "subagents/slide.md"))
    shared = [task, brief, css, instructions]
    if tool_contract:
        contract = Path(tool_contract)
        if not contract.is_absolute():
            raise ValueError("tool-contract must be an explicit absolute path")
        shared.append(read_source(safe_path(Path(contract.anchor), contract)))
    groups = production_groups(deck, expected)
    references_cache = {}
    prepared = []
    # Validate every frozen group, including references/assets, before --group selection.
    for group in groups:
        sources = [group.pop("contract"), *shared]
        group["outputs"] = [str(safe_path(root, f"slides/slide_{page:02d}.html")) for page in group["pages"]]
        selected_refs, ids = [], []
        for page in group["pages"]:
            plan = read_source(safe_path(root, f"plan/slide_{page:02d}.md"))
            number, actual_group = field(plan, "production_group")
            if actual_group != group["group"]:
                fail(plan.path, number, f"production_group {actual_group!r} does not match {group['group']!r}")
            sources.append(plan)
            selected_refs.extend(references(plan, references_cache))
            ids.extend(asset_ids(plan))
        sources.extend(selected_refs)
        catalog = catalog_source(root, ids)
        if catalog:
            sources.append(catalog)
        sources = merge_sources(sources)
        parts = split_parts(group, sources, root)
        group["sources"] = [source.location() for source in sources]
        prepared.append((group, parts))
    if group_id is not None:
        prepared = [(group, parts) for group, parts in prepared if group["group"] == group_id]
        if not prepared:
            raise ValueError(f"unknown group: {group_id}")
    # Preflight all destinations before the first write; never follow output symlinks.
    for group, parts in prepared:
        group["inputs"] = [str(safe_path(root, f"_trace/group-input/{group['group']}/part-{index:02d}.md"))
                           for index in range(1, len(parts) + 1)]
        for value in group["inputs"]:
            path = Path(value)
            if path.exists() and not path.is_file():
                raise ValueError(f"part destination is not a regular file: {path}")
    for group, parts in prepared:
        group["parts"] = []
        for value, text in zip(group["inputs"], parts):
            path = safe_path(root, value)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            group["parts"].append({"path": value, "lines": len(text.splitlines()), "chars": len(text)})
        group["part_count"] = len(parts)
        group["total_chars"] = sum(part["chars"] for part in group["parts"])
    return {"deck_dir": str(root), "groups": [group for group, _ in prepared]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deck_dir")
    parser.add_argument("--expected", type=int, required=True)
    parser.add_argument("--group")
    parser.add_argument("--tool-contract")
    args = parser.parse_args()
    try:
        result = prepare(args.deck_dir, args.expected, args.group, args.tool_contract)
    except (ValueError, OSError) as error:
        print(f"group-input: not applicable: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
