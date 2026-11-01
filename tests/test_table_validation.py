"""Whole-source validation, independent of the model's calculation/filtering."""

import csv

import pytest

from box_agent.tools.file.table_validation import scan_table


FIELDS = {
    "id": {"type": "text"},
    "revision": {"type": "integer", "minimum": "1"},
    "amount": {"type": "decimal", "minimum": "0", "scale": 2},
    "status": {"type": "enum", "values": ["paid", "cancelled"]},
    "date": {"type": "date"},
    "is_test": {"type": "boolean"},
    "note": {"type": "text", "nullable": True},
}


def write_table(path, rows, *, delimiter=","):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(FIELDS)
        writer.writerows(rows)


def row(index, **changes):
    values = dict(zip(FIELDS, [f"T{index}", "1", "1.25", "paid", "2026-10-31", "false", "note,\nline"]))
    return [changes.get(key, value) for key, value in values.items()]


def test_tail_old_and_cancelled_records_are_validated_before_filtering(tmp_path):
    path = tmp_path / "orders.csv"
    rows = [row(i) for i in range(2200)]
    rows[0] = row(0, amount="NaN")  # an old version later superseded
    rows[2010] = row(2010, amount="-1.00", status="cancelled")
    rows += [row(0, revision="2"), row(2201, id="", date="2026-02-30", is_test="maybe", status="bogus")]
    write_table(path, rows)
    result = scan_table(path, FIELDS)
    assert result.complete and not result.valid
    assert result.records == 2202
    assert result.invalid_records == 3
    assert {(e["record"], e["field"]) for e in result.errors} == {
        (1, "amount"), (2011, "amount"), (2202, "id"),
        (2202, "date"), (2202, "is_test"), (2202, "status"),
    }
    assert all(value["checked"] == 2202 for value in result.fields.values())
    assert result.fields["amount"]["invalid"] == 2


@pytest.mark.parametrize("extension,delimiter", [("csv", ","), ("tsv", "\t")])
def test_bom_and_embedded_delimiters_and_newlines_count_logical_records(tmp_path, extension, delimiter):
    path = tmp_path / f"input.{extension}"
    write_table(path, [row(1), row(2, note="quoted\tfield\nline"), row(3)], delimiter=delimiter)
    result = scan_table(path, FIELDS)
    assert result.complete and result.valid and result.records == 3
    assert len(result.source_sha256) == len(result.schema_sha256) == 64
    assert result.errors == []


@pytest.mark.parametrize("extension,delimiter", [("csv", ","), ("tsv", "\t")])
def test_large_text_fields_are_validated_through_eof(tmp_path, extension, delimiter):
    path = tmp_path / f"input.{extension}"
    write_table(path, [row(1, note="长文本\n" * 40_000), row(2)], delimiter=delimiter)
    result = scan_table(path, FIELDS)
    assert result.complete and result.valid and result.records == 2
    assert all(field["checked"] == 2 for field in result.fields.values())


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.01", "1.001", "1_000", ""])
def test_every_original_amount_requires_finite_nonnegative_exact_scale(tmp_path, value):
    path = tmp_path / "input.csv"
    write_table(path, [row(1, amount=value, status="cancelled"), row(2)])
    result = scan_table(path, FIELDS)
    assert result.complete and result.records == 2 and not result.valid
    assert result.errors[0]["field"] == "amount"


@pytest.mark.parametrize("width", [257, 1024])
@pytest.mark.parametrize("extension,delimiter", [("csv", ","), ("tsv", "\t")])
def test_wide_tables_validate_every_record_and_column(tmp_path, width, extension, delimiter):
    path = tmp_path / f"wide.{extension}"
    fields = {f"column_{index}": {"type": "integer"} for index in range(width)}
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(fields)
        writer.writerows([["1"] * width, ["2"] * width])
    result = scan_table(path, fields)
    assert result.complete and result.valid and result.records == 2
    assert set(result.fields) == set(fields)
    assert all(stats["checked"] == 2 for stats in result.fields.values())


def test_many_errors_are_counted_completely_but_only_bounded_samples_returned(tmp_path):
    path = tmp_path / "input.csv"
    write_table(path, [row(i, amount="oops") for i in range(2500)])
    result = scan_table(path, FIELDS)
    assert result.complete and result.records == result.invalid_records == result.error_count == 2500
    assert len(result.errors) == 20
    assert result.fields["amount"]["checked"] == result.fields["amount"]["invalid"] == 2500


def test_omitted_column_contract_is_rejected(tmp_path):
    path = tmp_path / "input.csv"
    write_table(path, [row(1)])
    with pytest.raises(ValueError, match="columns"):
        scan_table(path, {k: v for k, v in FIELDS.items() if k != "is_test"})


@pytest.mark.parametrize("header", [["x" * 10_000], [f"column_{i}" for i in range(3000)]])
def test_mismatched_large_headers_return_bounded_diagnostics(tmp_path, header):
    path = tmp_path / "input.csv"
    with path.open("w", newline="") as stream:
        csv.writer(stream).writerow(header)
    with pytest.raises(ValueError, match="columns") as caught:
        scan_table(path, {"expected": {"type": "text"}})
    assert len(str(caught.value)) < 1000


@pytest.mark.parametrize("field,rule", [
    ("status", {"type": "text"}), ("amount", {"type": "text"}),
    ("amount", {"type": "decimal"}),
    ("amount", {"type": "decimal", "minimum": "-1", "scale": 2}),
    ("amount", {"type": "decimal", "minimum": "0", "scale": 3}),
    ("revision", {"type": "integer", "minimum": "0"}),
    ("id", {"type": "text", "nullable": True}),
    ("is_test", {"type": "text"}), ("date", {"type": "text"}),
])
def test_transaction_contract_cannot_omit_or_weaken_critical_field_checks(tmp_path, field, rule):
    path = tmp_path / "transactions.csv"
    write_table(path, [row(1)])
    with pytest.raises(ValueError, match="TRANSACTION_CONTRACT"):
        scan_table(path, {**FIELDS, field: rule}, profile="transaction_v1")


def test_common_column_names_do_not_override_a_generic_ledger_contract(tmp_path):
    path = tmp_path / "ledger.csv"
    fields = {"id": {"type": "text"}, "status": {"type": "text"},
              "revision": {"type": "integer", "minimum": "0"},
              "amount": {"type": "decimal", "minimum": "-100", "scale": 4}}
    path.write_text("id,status,revision,amount\nA,draft,0,-1.2345\n")
    result = scan_table(path, fields)
    assert result.complete and result.valid and result.profile == "declared_fields"
    assert all(f["checked"] == 1 for f in result.fields.values())
    with pytest.raises(ValueError, match="TRANSACTION_CONTRACT"):
        scan_table(path, fields, profile="transaction_v1")


def test_nontransaction_tables_do_not_inherit_nonnegative_money_constraints(tmp_path):
    path = tmp_path / "measurements.csv"
    path.write_text("amount,status\n-1.234,draft\n")
    assert scan_table(path, {"amount": {"type": "decimal"}, "status": {"type": "text"}}).valid


@pytest.mark.parametrize("rule", [
    {"type": "decimal", "minimum": "NaN"}, {"type": "decimal", "scale": True},
    {"type": "enum", "values": ["paid", "paid"]}, {"type": "text", "minimum": "0"},
    {"type": "integer", "minimum": "2", "maximum": "1"},
    {"type": "text", "skip_invalid": True},
])
def test_invalid_or_unsupported_constraints_are_rejected(tmp_path, rule):
    path = tmp_path / "input.csv"
    write_table(path, [row(1)])
    with pytest.raises(ValueError):
        scan_table(path, {**FIELDS, "amount": rule})


def test_scanner_does_not_follow_replacement_symlink(tmp_path):
    target = tmp_path / "source.csv"
    write_table(target, [row(1)])
    link = tmp_path / "link.csv"
    link.symlink_to(target)
    import os
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("Platform has no O_NOFOLLOW")
    with pytest.raises(OSError):
        scan_table(link, FIELDS)


def test_structural_parse_error_never_claims_eof_coverage(tmp_path):
    path = tmp_path / "input.csv"
    write_table(path, [row(1)])
    with path.open("a") as stream:
        stream.write('"unterminated')
    result = scan_table(path, FIELDS)
    assert not result.complete and not result.valid
    assert result.records == 1 and result.errors[-1]["code"] == "parse_error"


def test_ragged_rows_are_reported_without_silently_stopping_at_first_error(tmp_path):
    path = tmp_path / "input.csv"
    write_table(path, [row(1)[:-2], row(2) + ["extra"], row(3)])
    result = scan_table(path, FIELDS)
    assert result.complete and not result.valid and result.records == 3
    assert result.invalid_records == 2


def test_duplicate_headers_are_rejected(tmp_path):
    path = tmp_path / "input.csv"
    path.write_text("amount,amount\n1,2\n")
    with pytest.raises(ValueError, match="duplicate"):
        scan_table(path, {"amount": {"type": "decimal"}})


def test_scan_honors_cooperative_cancellation(tmp_path):
    path = tmp_path / "input.csv"
    write_table(path, [row(1)])
    with pytest.raises(InterruptedError):
        scan_table(path, FIELDS, cancelled=lambda: True)
