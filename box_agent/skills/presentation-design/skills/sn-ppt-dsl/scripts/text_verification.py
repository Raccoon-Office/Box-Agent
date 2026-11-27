"""逐页核对实际渲染文字，保留数字、单位和拼音的验收范围。"""

from collections import Counter
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile


NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def normalized(value: str) -> str:
    """换行和等价 Unicode 编码不构成内容缺失。"""
    value = unicodedata.normalize("NFKC", value).replace("\u2212", "-")
    return "".join(char for char in value if not char.isspace() and unicodedata.category(char) != "Cf")


def numeric_tokens(value: str) -> Counter:
    value = unicodedata.normalize("NFKC", value).replace("\u2212", "-")
    return Counter(normalized(item) for item in re.findall(r"(?<![0-9.])[-+]?\d+(?:[.,]\d+)*(?:\s*[%‰])?", value))


def pptx_text_pages(path) -> list[list[str]]:
    """以文本框或表格单元格为单元，避免把不同对象的数字拼接。"""
    with zipfile.ZipFile(path) as archive:
        names = sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                       key=lambda name: int(re.search(r"slide(\d+)", name).group(1)))
        pages = []
        for name in names:
            root = ET.fromstring(archive.read(name))
            blocks = []
            for body in root.iter():
                if body.tag.rsplit("}", 1)[-1] != "txBody":
                    continue
                # 段落属性没有可见文本；只取 a:t，保留富文本连续性。
                paragraphs = ["".join(node.text or "" for node in p.findall(".//a:t", NS)) for p in body.findall("a:p", NS)]
                if any(paragraphs):
                    blocks.append("\n".join(paragraphs))
            pages.append(blocks)
        return pages


def compare_rendered_pages(expected: list[list[str]], actual: list[str]) -> dict:
    """字符缺失和数值变化为错误，抽取顺序差异交给真实页面复查。"""
    if len(expected) != len(actual):
        raise ValueError(f"渲染文字页数不符：实际 {len(actual)}，预期 {len(expected)}")
    pages = []
    for page, (blocks, rendered) in enumerate(zip(expected, actual), 1):
        source = "\n".join(blocks)
        missing = Counter(normalized(source)) - Counter(normalized(rendered))
        missing_numbers = numeric_tokens(source) - numeric_tokens(rendered)
        fragments = [block[:100] for block in blocks if normalized(block) and normalized(block) not in normalized(rendered)]
        pages.append({
            "page": page, "ok": not missing and not missing_numbers,
            "missing_characters": dict(missing), "missing_numeric_tokens": dict(missing_numbers),
            "risk_fragments": fragments,
        })
    return {"ok": all(page["ok"] for page in pages), "pages": pages}


def extract_pdf_pages(executable: str | None, path) -> list[str]:
    if not executable:
        # Some desktop Poppler bundles provide the image converter alone.
        # Read the same rendered PDF and apply the same per-page comparison.
        from pypdf import PdfReader
        return [page.extract_text() or "" for page in PdfReader(path).pages]
    import subprocess
    # raw 保留真实文本顺序，避免 layout 为分栏补空格而制造假缺字。
    text = subprocess.run([executable, "-raw", str(path), "-"], check=True, capture_output=True, text=True).stdout
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages
