"""核对 XML 图表数据、导出缓存与内嵌工作簿的一致性。"""

import csv
from io import BytesIO
import math
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile


NS = {
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


def _child(node, tag):
    return next((item for item in node.get("children", []) if item["tag"] == tag), {})


def _plain(node):
    return node.get("text", "") + "".join(_plain(item) + item.get("tail", "") for item in node.get("children", []))


def source_charts(payload: dict) -> list[list[list[dict]]]:
    pages = []
    for page in (node for node in payload.get("children", []) if node["tag"] == "slide"):
        charts = []
        for chart in (node for node in _child(page, "data").get("children", []) if node["tag"] == "chart"):
            data = _child(chart, "chartData")
            category = _child(_child(data, "dim1"), "chartField")
            labels = [item.strip() for item in next(csv.reader([_plain(category)]))]
            charts.append([{
                "name": field.get("attrs", {}).get("name", ""), "labels": labels,
                "values": [float(item.strip()) for item in next(csv.reader([_plain(field)]))],
            } for field in _child(data, "dim2").get("children", []) if field["tag"] == "chartField"])
        pages.append(charts)
    return pages


def _relationships(archive, part):
    name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if name not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(name))
    return {node.get("Id"): posixpath.normpath(posixpath.join(posixpath.dirname(part), node.get("Target", ""))).lstrip("/")
            for node in root if node.get("TargetMode") != "External"}


def _cached_values(node):
    points = sorted(node.findall(".//c:pt", NS), key=lambda item: int(item.get("idx")))
    return [point.findtext("c:v", default="", namespaces=NS) for point in points]


def _column_number(value):
    result = 0
    for char in value:
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def _workbook_cells(content):
    with zipfile.ZipFile(BytesIO(content)) as workbook:
        strings = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            strings = ["".join(item.itertext()) for item in ET.fromstring(workbook.read("xl/sharedStrings.xml"))]
        book = ET.fromstring(workbook.read("xl/workbook.xml"))
        relations = _relationships(workbook, "xl/workbook.xml")
        sheets = {}
        for sheet in book.findall("x:sheets/x:sheet", NS):
            target = relations[sheet.get(f"{{{NS['r']}}}id")]
            cells = {}
            for cell in ET.fromstring(workbook.read(target)).findall(".//x:c", NS):
                value = cell.findtext("x:v", default="", namespaces=NS)
                if cell.get("t") == "s":
                    value = strings[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall(".//x:t", NS))
                cells[cell.get("r")] = value
            sheets[sheet.get("name")] = cells
        return sheets


def _formula_values(sheets, formula):
    match = re.fullmatch(r"(?:'((?:[^']|'')+)'|([^!]+))!\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?", formula)
    if not match:
        raise ValueError(f"图表工作簿范围不能核验：{formula}")
    quoted, plain, col1, row1, col2, row2 = match.groups()
    sheet = sheets[(quoted or plain).replace("''", "'")]
    first_col, last_col = _column_number(col1), _column_number(col2 or col1)
    first_row, last_row = int(row1), int(row2 or row1)
    values = []
    for row in range(first_row, last_row + 1):
        for col in range(first_col, last_col + 1):
            label, remainder = "", col
            while remainder:
                remainder, digit = divmod(remainder - 1, 26)
                label = chr(65 + digit) + label
            values.append(sheet.get(f"{label}{row}", ""))
    return values


def _equal_values(expected, actual, numeric=False):
    if len(expected) != len(actual):
        return False
    if not numeric:
        return list(map(str, expected)) == list(map(str, actual))
    try:
        return all(math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12) for a, b in zip(expected, actual))
    except (ValueError, TypeError):
        return False


def verify_charts(archive, slide_names, expected_pages=None):
    checked = []
    for page, slide_name in enumerate(slide_names, 1):
        root = ET.fromstring(archive.read(slide_name))
        references = root.findall(".//c:chart", NS)
        expected = expected_pages[page - 1] if expected_pages is not None else None
        if expected is not None and len(expected) != len(references):
            raise ValueError(f"第 {page} 页图表数量与源 XML 不符")
        relations = _relationships(archive, slide_name)
        for number, reference in enumerate(references):
            part = relations[reference.get(f"{{{NS['r']}}}id")]
            chart = ET.fromstring(archive.read(part))
            series = sorted(chart.findall(".//c:ser", NS), key=lambda item: int(item.find("c:order", NS).get("val")))
            wanted = expected[number] if expected is not None else None
            if wanted is not None and len(series) != len(wanted):
                raise ValueError(f"第 {page} 页第 {number + 1} 张图表系列数量不符")
            external = chart.find("c:externalData", NS)
            if external is None:
                raise ValueError(f"第 {page} 页图表缺少可编辑数据工作簿")
            workbook = _relationships(archive, part)[external.get(f"{{{NS['r']}}}id")]
            sheets = _workbook_cells(archive.read(workbook))
            for index, item in enumerate(series):
                for tag, key, numeric in (("tx", "name", False), ("cat", "labels", False), ("val", "values", True)):
                    node = item.find(f"c:{tag}", NS)
                    if node is None:
                        raise ValueError(f"第 {page} 页图表缺少 {key}")
                    cached = _cached_values(node)
                    if tag == "tx" and not cached:
                        cached = [node.findtext("c:v", default="", namespaces=NS)]
                    formula = node.findtext(".//c:f", namespaces=NS)
                    if not formula:
                        raise ValueError(f"第 {page} 页图表缺少 {key} 的工作簿引用")
                    cells = _formula_values(sheets, formula)
                    baseline = ([wanted[index][key]] if key == "name" else wanted[index][key]) if wanted is not None else cached
                    if not _equal_values(baseline, cached, numeric) or not _equal_values(baseline, cells, numeric):
                        raise ValueError(f"第 {page} 页第 {number + 1} 张图表系列 {index + 1} 的 {key} 与源数据或工作簿不符")
            checked.append({"page": page, "chart": number + 1, "series": len(series), "workbook": workbook,
                            "source_checked": wanted is not None})
    return checked
