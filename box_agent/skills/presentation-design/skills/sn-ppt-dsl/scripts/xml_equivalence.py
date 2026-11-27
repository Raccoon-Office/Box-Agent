"""Strict, local XML equivalences; unsupported inputs remain for normal validation.

This does not relax the chart schema. A chart still has one dim2 container with
one chartField per series. Only redundant, otherwise unambiguous wrappers are
removed, preserving every field object, value, and series position.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import xml.etree.ElementTree as ET


NAMESPACE = "https://www.larkoffice.com/sml/2.0"


def _name(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""


def _blank(text: str | None) -> bool:
    return not (text or "").strip()


def _paths(root: ET.Element):
    def visit(node, path):
        yield node, path
        counts = Counter()
        for child in node:
            name = _name(child)
            counts[name] += 1
            yield from visit(child, f"{path}/{name}[{counts[name]}]")

    return visit(root, f"/{_name(root)}[1]")


def normalize_chart_containers(root: ET.Element) -> list[dict]:
    """Merge only consecutive, plain dim2 wrappers under one chartData.

    A non-equivalent candidate is untouched, so the existing schema validation
    rejects it normally. A returned entry always describes an applied change.
    Ordinary valid XML is not mutated. This helper never drops a series or
    adjusts data, axes, chart type, colors, dimensions, or series indices.
    """
    fixes = []
    parents = {child: parent for parent in root.iter() for child in parent}
    for data, path in list(_paths(root)):
        if data.tag not in {"chartData", f"{{{NAMESPACE}}}chartData"}:
            continue
        prefix = data.tag[:-len("chartData")]
        children = list(data)
        dims = [node for node in children if node.tag == prefix + "dim2"]
        if len(dims) < 2:
            continue
        # A shared category dimension followed by adjacent value wrappers is
        # the only accepted shape. Unknown or interleaved nodes are not guessed.
        if ([node.tag for node in children] !=
                [prefix + "dim1", *([prefix + "dim2"] * len(dims))]):
            continue
        if not _blank(data.text) or not _blank(children[0].tail):
            continue
        if any(node.attrib or not _blank(node.text) or not _blank(node.tail)
               or not len(node) for node in dims):
            continue
        fields = [field for node in dims for field in node]
        if any(field.tag != prefix + "chartField" or not _blank(field.tail)
               for field in fields):
            continue
        # Duplicate or unnamed series need an author decision rather than a
        # guess about whether two wrappers represent the same logical series.
        names = [field.get("name") for field in fields]
        if any(not name for name in names) or len(set(names)) != len(names):
            continue
        field_bytes = [ET.tostring(field, encoding="utf-8") for field in fields]
        first = dims[0]
        for redundant in dims[1:]:
            for field in list(redundant):
                redundant.remove(field)
                first.append(field)
            data.remove(redundant)
        assert field_bytes == [ET.tostring(field, encoding="utf-8") for field in first]
        chart = parents.get(data)
        fixes.append({
            "code": "merge_chart_dim2_containers",
            "path": path,
            "chart_id": chart.get("id") if chart is not None and _name(chart) == "chart" else None,
            "containers_before": len(dims),
            "containers_after": 1,
            "removed_containers": len(dims) - 1,
            "series_count": len(fields),
            "series_names": names,
            "series_sha256": hashlib.sha256(b"\0".join(field_bytes)).hexdigest(),
        })
    return fixes


def normalize_shape_fill(root: ET.Element) -> list[dict]:
    changes = []
    for shape, path in list(_paths(root)):
        if shape.tag not in {'shape', f'{{{NAMESPACE}}}shape'}:
            continue
        ns = shape.tag[:-len('shape')]
        styles = shape.findall(ns + 'style')
        if len(styles) != 1 or shape.find(ns + 'fill') is not None:
            continue
        style = styles[0]
        if style.attrib or not _blank(style.text) or len(style) != 1:
            continue
        fill = style[0]
        if fill.tag != ns + 'fill' or not _blank(fill.tail):
            continue
        index = list(shape).index(style)
        fill.tail = style.tail
        shape.remove(style)
        shape.insert(index, fill)
        changes.append({'code': 'unwrap_shape_fill', 'path': path, 'shape_id': shape.get('id')})
    return changes
