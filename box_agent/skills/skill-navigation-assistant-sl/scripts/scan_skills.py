#!/usr/bin/env python3
"""Scan local Office Raccoon skill directories and output normalized metadata.

v1.1.0 improvements:
- evidence-based Chinese name resolution with confidence
- graceful handling for broken SKILL.md / YAML
- user-friendly health hints
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

DEFAULT_SKILL_DIRS = list(dict.fromkeys(path.resolve() for path in [
    Path.home() / ".box-agent" / "skills",
    Path(__file__).resolve().parents[2],
    Path("/Applications/office-raccoon.app/Contents/Resources/box-agent-runtime/skills"),
]))

CATEGORY_RULES = [
    ("技能导航", ["skill-map", "skill-navigation", "navigation", "技能地图", "技能导航", "技能库导航"]),
    ("技能创建", ["skill-creator", "skillhub 标准", "skillhub 技能", "skill maker", "创建技能", "技能包", "打包发布", "yaml 修复", "上架资产"]),
    ("地图出行", ["map-assistant", "tencentmap", "地图", "路线", "天气", "poi", "腾讯位置"]),
    ("飞书协作", ["lark", "飞书"]),
    ("企微协作", ["wecom", "企微", "企业微信"]),
    ("记忆管理", ["memory", "记忆"]),
    ("知识管理", ["knowledge", "ima", "知识库", "笔记"]),
    ("写作内容", ["writer", "writing", "写作", "文章", "公众号", "作者", "仿写"]),
    ("数据分析", ["data", "analysis", "excel", "数据", "分析"]),
    ("专家顾问", ["expert", "team", "专家", "顾问", "思维", "服务设计", "咨询"]),
    ("通用办公", ["office", "文档", "报告", "ppt", "word"]),
]

WORD_TRANSLATION = {
    "skill": "技能",
    "map": "地图",
    "navigation": "导航",
    "assistant": "助手",
    "creator": "创建器",
    "optimized": "优化版",
    "architect": "架构师",
    "memory": "记忆",
    "lark": "飞书",
    "map-assistant": "地图助手",
}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def parse_front_matter(text: str) -> Tuple[Dict[str, Any], Optional[str]]:
    if not text.startswith("---"):
        return {}, "missing_front_matter"
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not match:
        return {}, "invalid_front_matter_block"
    if yaml is None:
        return {}, "pyyaml_not_available"
    try:
        data = yaml.safe_load(match.group(1)) or {}
        if not isinstance(data, dict):
            return {}, "front_matter_not_mapping"
        return data, None
    except Exception as exc:
        return {}, f"yaml_parse_error: {exc}"


def first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def zh_fragments(text: str) -> List[str]:
    fragments = []
    for raw in re.findall(r"[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9·：:（）()《》/\- ]{1,32}", text or ""):
        item = raw.strip(" ：:/-")
        if 2 <= len(item) <= 28 and item not in fragments:
            fragments.append(item)
    return fragments


def resolve_chinese_name(name: str, fm: Dict[str, Any], heading: str, description: str) -> Tuple[str, str, str]:
    for key in ["zh_name", "display_name", "title"]:
        value = str(fm.get(key) or "").strip()
        zhs = zh_fragments(value)
        if zhs:
            return zhs[0], "高", f"YAML {key}"
    zhs = zh_fragments(heading)
    if zhs:
        return zhs[0], "高", "SKILL.md 一级标题"
    for key in ["description", "triggers"]:
        value = fm.get(key) if key == "triggers" else description
        if isinstance(value, list):
            value = " ".join(str(v) for v in value)
        zhs = zh_fragments(str(value or ""))
        if zhs:
            return zhs[0], "中", f"{key} 中的中文短语"
    parts = [p for p in re.split(r"[-_]+", name) if p]
    translated = "".join(WORD_TRANSLATION.get(p.lower(), "") for p in parts)
    if translated:
        return translated, "低", "根据英文 slug 词根推断"
    return "中文名待确认", "低", "未找到稳定中文名称证据"


def keyword_hit(haystack: str, keyword: str) -> bool:
    kw = keyword.lower()
    if re.search(r"[\u4e00-\u9fff]", kw):
        return kw in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", haystack) is not None


def categorize(name: str, description: str, triggers: Any = None) -> str:
    if isinstance(triggers, list):
        trigger_text = " ".join(str(x) for x in triggers)
    else:
        trigger_text = str(triggers or "")
    haystack = f"{name} {description} {trigger_text}".lower()
    for category, keywords in CATEGORY_RULES:
        if any(keyword_hit(haystack, k) for k in keywords):
            return category
    return "未分类"


def health_from(skill_md: Path, err: Optional[str], name: str, description: str, confidence: str) -> Tuple[str, List[str]]:
    hints: List[str] = []
    if not skill_md.exists():
        return "warning", ["缺少 SKILL.md，只能识别目录名。"]
    if err and err != "missing_front_matter":
        hints.append("注册信息格式异常，建议修复 YAML front matter。")
    if not description:
        hints.append("缺少 description，推荐准确性会下降。")
    if confidence == "低":
        hints.append("中文名称可信度低，建议补充 zh_name 或中文标题。")
    if not name:
        hints.append("缺少 name 字段。")
    if err and "yaml_parse" in err:
        return "error", hints
    return ("normal" if not hints else "warning"), hints or ["结构和说明基本可用。"]


def scan_skill_dir(root: Path) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if not root.exists():
        return results
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        skill_md = child / "SKILL.md"
        text = read_text(skill_md) if skill_md.exists() else ""
        fm, err = parse_front_matter(text) if text else ({}, "missing_skill_md")
        name = str(fm.get("name") or child.name).strip()
        heading = first_heading(text)
        description = str(fm.get("description") or "").strip()
        if not description:
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("---") and not l.startswith("#")]
            description = lines[0][:180] if lines else ""
        zh_name, confidence, evidence = resolve_chinese_name(name, fm, heading, description)
        health, hints = health_from(skill_md, err, name, description, confidence)
        results.append({
            "english_name": name,
            "slug": child.name,
            "chinese_name": zh_name,
            "name_confidence": confidence,
            "name_evidence": evidence,
            "description_zh": description or "该技能缺少清晰说明，需要补充 description。",
            "version": str(fm.get("version") or "unknown"),
            "category": categorize(name, description, fm.get("triggers")),
            "invoke": f"/{name}",
            "path": str(child),
            "source": str(root),
            "has_skill_md": skill_md.exists(),
            "front_matter_status": "ok" if not err else err,
            "health": health,
            "health_hints": hints,
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan local skills")
    parser.add_argument("--dirs", nargs="*", help="Skill roots to scan")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()
    roots = [Path(p).expanduser() for p in args.dirs] if args.dirs else DEFAULT_SKILL_DIRS
    skills: List[Dict[str, Any]] = []
    for root in roots:
        skills.extend(scan_skill_dir(root))
    if args.json:
        print(json.dumps(skills, ensure_ascii=False, indent=2))
        return
    print(f"扫描到 {len(skills)} 个技能")
    for item in skills:
        print(f"- {item['english_name']} / {item['chinese_name']}（{item['name_confidence']}） | {item['category']} | {item['invoke']} | {item['health']}")
        if item.get("name_confidence") == "低":
            print(f"  提醒：中文名来自{item['name_evidence']}，建议人工确认。")


if __name__ == "__main__":
    main()
