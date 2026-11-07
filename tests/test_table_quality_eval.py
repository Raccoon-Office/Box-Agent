from test_workspace.table_quality import audit_numeric_claims, audit_validation_order
from test_workspace.table_quality import audit_json_artifacts

EXPECTED = {"chargeback_cny": "-1591.21", "refund_cny": "-261.90",
            "negative_contribution_cny": "-1853.11", "delta_cny": "-775.99",
            "delta_percent": "-18.6%"}


def report(values=EXPECTED):
    return "\n".join(f"{k}: {v}" for k, v in values.items())


def test_every_requested_derived_metric_is_checked():
    result = audit_numeric_claims(report(), EXPECTED)
    assert result["named_metrics_passed"]
    assert not result["unmapped_numeric_lines"]
    assert result["requires_semantic_review"]
    for name in EXPECTED:
        bad = dict(EXPECTED, **{name: "0.00"})
        assert not audit_numeric_claims(report(bad), EXPECTED)["named_metrics_passed"]


def test_prior_wrong_narrative_is_flagged_even_with_correct_json_and_named_totals():
    text = report() + "\nq2 出现 3 笔 chargeback 与 2 笔纯退款（合计负贡献约 −1785.11 CNY）。"
    result = audit_numeric_claims(text, EXPECTED)
    assert result["named_metrics_passed"]
    assert result["unmapped_numeric_lines"][0]["literal"] == "−1785.11"


def test_duplicate_missing_sign_or_percentage_unit_cannot_pass():
    assert not audit_numeric_claims(report() + "\nrefund_cny: -261.90", EXPECTED)["named_metrics_passed"]
    assert not audit_numeric_claims(report().replace("-1853.11", "1853.11"), EXPECTED)["named_metrics_passed"]
    assert not audit_numeric_claims(report().replace("-18.6%", "-18.6"), EXPECTED)["named_metrics_passed"]
    assert not audit_numeric_claims(report().replace("delta_cny: -775.99", ""), EXPECTED)["named_metrics_passed"]
    assert audit_numeric_claims(report().replace("-1853.11", "−1,853.11"), EXPECTED)["named_metrics_passed"]


def event(kind, data, call="x"):
    return {"event": kind, "data": data, "turn_id": "turn-1", "tool_call_id": call}


def test_late_validation_cannot_repair_execution_order_and_denied_calls_do_not_count():
    events = [event("table.inputs", {"sources": {"/data.csv": "hash"}}),
              event("tool.request", {"tool_name": "execute_code"}),
              event("tool.response", {"tool_name": "execute_code", "success": True}),
              event("tool.request", {"tool_name": "validate_table", "arguments": {"path": "/data.csv"}}, "v"),
              event("tool.response", {"tool_name": "validate_table", "success": True,
                    "raw_output": {"path": "/data.csv", "validation_complete": True, "data_valid": True}}, "v"),
              event("tool.request", {"tool_name": "execute_code"}, "next"),
              event("tool.response", {"tool_name": "execute_code", "success": True}, "next")]
    assert [r["passed"] for r in audit_validation_order(events)] == [False, True]
    events[2]["data"]["success"] = False
    assert [r["passed"] for r in audit_validation_order(events)] == [True]
    events[4]["data"]["raw_output"]["data_valid"] = False
    assert [r["passed"] for r in audit_validation_order(events)] == [False]


def test_correct_report_numbers_do_not_replace_required_original_receipt(tmp_path):
    import json
    receipt = {"period": "q2", "rates": {"USD": "7.25"}, "receipt": "test-id", "source": "local-mcp"}
    (tmp_path / "analysis-q2.json").write_text(json.dumps({"net_sales_cny": "3387.02", **receipt}))
    expected = {"rates-q2.json": receipt}
    assert not audit_json_artifacts(tmp_path, expected)[0]["passed"]
    (tmp_path / "rates-q2.json").write_text(json.dumps(receipt))
    assert audit_json_artifacts(tmp_path, expected)[0]["passed"]
    (tmp_path / "rates-q2.json").write_text(json.dumps({"rates": receipt["rates"], "receipt": "test-id"}))
    assert not audit_json_artifacts(tmp_path, expected)[0]["passed"]


def test_malformed_or_external_receipt_cannot_count_as_delivery(tmp_path):
    (tmp_path / "rates.json").write_text("not json")
    assert not audit_json_artifacts(tmp_path, {"rates.json": {}})[0]["passed"]
    assert not audit_json_artifacts(tmp_path, {"../outside.json": {}})[0]["passed"]
