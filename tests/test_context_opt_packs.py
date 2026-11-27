"""Offline consistency of the context-optimization special-case packs.

These checks never call a model. They make sure every case parses with the
real evaluation driver, uses only oracle kinds the evaluator implements, and
that expected values are derived from the fixture data itself.
"""

import csv
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from box_agent.tools.file.table_validation import scan_table
from test_workspace import quality_baseline as qb

ROOT = Path(__file__).resolve().parents[1]
PACKS = [ROOT / "test_workspace/inputs/context_opt", ROOT / "test_workspace/inputs/context_opt_routing"]
EVALUATOR_KINDS = set(re.findall(r'kind == "([a-z_]+)"', (ROOT / "test_workspace/quality_baseline.py").read_text()))


def pack_cases(pack):
    specs = {spec["id"]: spec for spec in json.loads((pack / "oracles.json").read_text(encoding="utf-8"))["cases"]}
    records = [json.loads(line) for line in (pack / "dataset.jsonl").read_text(encoding="utf-8").splitlines()]
    return records, specs


@pytest.mark.parametrize("pack", PACKS, ids=lambda pack: pack.name)
def test_pack_cases_parse_with_the_driver_and_use_known_oracles(pack, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "test_workspace/acp_eval/src"))
    from acp_eval.models import CaseMetadata, WorkspaceSetup

    records, specs = pack_cases(pack)
    assert [record["id"] for record in records] == list(specs)
    for record in records:
        CaseMetadata.from_record(record)
        WorkspaceSetup.from_record(record)
        for name in [*record["input_files"], *record.get("workspace_setup", {}).get("files", [])]:
            assert (pack / name).is_file(), name
        spec = specs[record["id"]]
        assert spec["split"] in {"dev", "holdout"}
        assert spec.get("expected_state", "completed") in {"completed", "waiting_for_user", "completed_or_waiting"}
        assert {check["kind"] for check in spec["checks"]} <= EVALUATOR_KINDS
        assert {"kind": "no_unknown_tool"} in spec["checks"]
        for check in spec["checks"]:
            if check["kind"] in {"reply_matches", "tool_args_not_matching"}:
                re.compile(check["pattern"])


@pytest.mark.parametrize("pack", PACKS, ids=lambda pack: pack.name)
def test_each_pack_has_trigger_similar_and_second_turn_cases(pack):
    records, specs = pack_cases(pack)
    groups = defaultdict(set)
    for record in records:
        family = record["id"].split("-", 1)[0]
        checks = specs[record["id"]]["checks"]
        if record.get("follow_up_turns"):
            groups[family].add("second_turn")
        elif any(check["kind"] == "no_tool" or check["kind"] == "tool_args_not_matching" for check in checks):
            groups[family].add("similar")
        else:
            groups[family].add("trigger")
    for family, kinds in groups.items():
        assert {"trigger", "similar", "second_turn"} <= kinds, family
    holdout = [spec for spec in specs.values() if spec["split"] == "holdout"]
    assert holdout, "acceptance needs held-out cases"


TABLE_PACK = PACKS[0]
ORDER_FIELDS = {"id": {"type": "integer"}, "region": {"type": "text"},
                "amount": {"type": "decimal", "minimum": "0", "scale": 2}, "date": {"type": "date"}}


def test_tail_error_fixture_is_detected_only_on_its_last_record():
    scan = scan_table(TABLE_PACK / "orders.csv", ORDER_FIELDS)
    assert scan.complete and not scan.valid and scan.invalid_records == 1
    assert scan.records == 20 and "12.3.4" in json.dumps(scan.errors, ensure_ascii=False)


def test_second_turn_expected_totals_are_derived_from_the_fixture():
    totals = defaultdict(Decimal)
    with (TABLE_PACK / "sales_region.csv").open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            totals[row["region"]] += Decimal(row["amount"])
    _, specs = pack_cases(TABLE_PACK)
    expected = next(check["value"] for check in specs["t-csv-second-turn"]["checks"] if check["kind"] == "json")
    assert {region: float(total) for region, total in totals.items()} == expected
    assert scan_table(TABLE_PACK / "sales_region.csv", {
        "id": {"type": "integer"}, "region": {"type": "text"},
        "amount": {"type": "decimal", "minimum": "0", "scale": 2}}).valid


def test_append_oracle_matches_the_fixture_tail_and_reply_patterns_match_intended_text():
    _, specs = pack_cases(TABLE_PACK)
    tail = (TABLE_PACK / "orders_small.csv").read_text(encoding="utf-8").rstrip("\n").rsplit("\n", 1)[-1]
    contains = next(c["value"] for c in specs["t-inplace-append"]["checks"] if c["kind"] == "contains")
    assert contains.startswith(tail + "\n")
    tail_error = next(c["pattern"] for c in specs["t-tail-error-compute"]["checks"] if c["kind"] == "reply_matches")
    assert re.search(tail_error, "最后一行 amount 为 12.3.4，无法计算", re.I)
    assert not re.search(tail_error, "总和为 4321.00", re.I)
    serve = next(c["pattern"] for c in specs["h-serve-for-user"]["checks"] if c["kind"] == "reply_matches")
    assert re.search(serve, "打开 http://127.0.0.1:53817/hello.html 查看")
    report = (TABLE_PACK / "report.html").read_text(encoding="utf-8")
    keep = [c["value"] for c in specs["h-edit-no-preview"]["checks"] if c["kind"] == "contains"]
    assert keep[1] in report and keep[0] not in report


def test_web_follow_up_asks_about_text_web_extract_actually_returns():
    """web_extract returns page text without link targets, so the follow-up must be answerable from text."""
    _, specs = pack_cases(PACKS[1])
    pattern = next(c["pattern"] for c in specs["w-follow-up-same-page"]["checks"] if c["kind"] == "reply_matches")
    assert re.search(pattern, "不可以，页面明确说不要依赖它进行测试或监控。", re.I)
    assert re.search(pattern, "它写明应避免把这个域名用于测试和监控。", re.I)
    assert not re.search(pattern, "这个页面说明 example.com 不是真实服务。", re.I)
    assert not re.search(pattern, "可以，随便用来测试。", re.I)


def test_oracles_pass_on_ideal_evidence_and_fail_on_a_violation(tmp_path):
    """Run the evaluator's own check_rule on hand-built evidence."""
    _, specs = pack_cases(TABLE_PACK)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.html").write_text("<h1>你好，预览</h1>", encoding="utf-8")
    (tmp_path / "assistant.txt").write_text("已启动：http://127.0.0.1:40123/hello.html", encoding="utf-8")
    good = [{"tool_name": "bash", "success": True,
             "arguments": {"command": "python3 -m http.server 0 --bind 127.0.0.1", "lifetime": "runtime"}}]
    checks = specs["h-serve-for-user"]["checks"]
    assert all(qb.check_rule(rule, tmp_path, good) for rule in checks)
    turn_only = [{**good[0], "arguments": {**good[0]["arguments"], "lifetime": "turn"}}]
    assert not all(qb.check_rule(rule, tmp_path, turn_only) for rule in checks)
    self_check = specs["h-self-check-only"]["checks"]
    rule = next(r for r in self_check if r["kind"] == "tool_args_not_matching")
    assert qb.check_rule(rule, tmp_path, turn_only) and not qb.check_rule(rule, tmp_path, good)
