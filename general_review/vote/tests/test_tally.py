import json
from pathlib import Path

import pytest

import tally as T


def _finding(**overrides):
    values = dict(
        id="F1", file="box_agent/core.py", line=10, category="correctness", severity="P1",
        title="Cancellation leaks the task", trigger="cancel during stream", impact="task leak",
        evidence="box_agent/core.py:10", suggestion="await the task", lenses=["correctness"],
    )
    values.update(overrides)
    return T.Finding(**values)


def _vote(verdict, severity="P1", voter="v1", evidence=True):
    return T.Vote(
        finding_id="F1", voter=voter, verdict=verdict, severity=severity,
        evidence=({"file": "box_agent/core.py", "line": 10, "note": ""},) if evidence else (),
        reason="",
    )


@pytest.mark.parametrize("voters, required", [(1, 1), (3, 2), (5, 4)])
def test_required_confirmations_is_two_thirds_rounded_up(voters, required):
    assert T.required_confirmations(voters) == required


def test_two_of_three_confirmations_post_the_finding():
    outcome = T.tally_finding(
        _finding(), [_vote("CONFIRMED"), _vote("CONFIRMED"), _vote("REJECTED")],
        voters=3, pre_existing=False,
    )
    assert outcome.status == T.POSTED
    assert outcome.blocking
    assert outcome.label == "Important"


def test_missing_votes_count_against_the_threshold():
    # Two verifiers failed: one confirmation out of three requested is not enough.
    outcome = T.tally_finding(_finding(severity="P2"), [_vote("CONFIRMED", "P2")], voters=3, pre_existing=False)
    assert outcome.status == T.DROPPED


def test_four_of_five_required_for_deep():
    votes = [_vote("CONFIRMED", voter=f"v{i}") for i in range(3)] + [_vote("REJECTED", voter="v4")]
    assert T.tally_finding(_finding(), votes, voters=5, pre_existing=False).status != T.POSTED
    votes.append(_vote("CONFIRMED", voter="v5"))
    assert T.tally_finding(_finding(), votes, voters=5, pre_existing=False).status == T.POSTED


def test_high_severity_below_threshold_is_disputed_not_dropped():
    outcome = T.tally_finding(
        _finding(severity="P0"), [_vote("CONFIRMED", "P0"), _vote("REJECTED"), _vote("UNSURE")],
        voters=3, pre_existing=False,
    )
    assert outcome.status == T.DISPUTED
    assert not outcome.blocking


def test_low_severity_below_threshold_is_dropped():
    outcome = T.tally_finding(
        _finding(severity="P2"), [_vote("CONFIRMED", "P2"), _vote("REJECTED"), _vote("REJECTED")],
        voters=3, pre_existing=False,
    )
    assert outcome.status == T.DROPPED


def test_severity_is_verifier_median_with_ties_toward_more_severe():
    outcome = T.tally_finding(
        _finding(severity="P0"), [_vote("CONFIRMED", "P1"), _vote("CONFIRMED", "P3"), _vote("REJECTED")],
        voters=3, pre_existing=False,
    )
    assert outcome.severity == "P1"
    outcome = T.tally_finding(
        _finding(severity="P0"),
        [_vote("CONFIRMED", "P3", voter="a"), _vote("CONFIRMED", "P2", voter="b"), _vote("CONFIRMED", "P3", voter="c")],
        voters=3, pre_existing=False,
    )
    assert outcome.severity == "P3"
    assert outcome.label == "Nit"
    assert not outcome.blocking


def test_pre_existing_only_blocks_at_p0():
    p1 = T.tally_finding(_finding(), [_vote("CONFIRMED")] * 3, voters=3, pre_existing=True)
    p0 = T.tally_finding(_finding(severity="P0"), [_vote("CONFIRMED", "P0")] * 3, voters=3, pre_existing=True)
    assert p1.label == "Pre-existing" and not p1.blocking
    assert p0.blocking


def test_votes_without_evidence_or_unknown_ids_do_not_count():
    payload = {"votes": [
        {"id": "F1", "verdict": "CONFIRMED", "severity": "P1", "evidence": []},
        {"id": "F9", "verdict": "CONFIRMED", "evidence": [{"file": "a.py", "line": 1}]},
        {"id": "F2", "verdict": "maybe", "evidence": [{"file": "a.py", "line": 1}]},
        {"id": "F2", "verdict": "CONFIRMED", "evidence": [{"file": "a.py", "line": 1}]},
    ]}
    votes, errors = T.parse_votes(payload, voter="v1", finding_ids=["F1", "F2"])
    assert {vote.finding_id: vote.verdict for vote in votes} == {"F1": "UNSURE", "F2": "UNSURE"}
    assert len(errors) == 2


def test_parse_findings_rejects_incomplete_items_and_normalizes_paths():
    payload = {"findings": [
        {"file": ".\\box_agent\\cli.py", "line": "12", "severity": "p2", "title": "x", "category": "Security"},
        {"file": "a.py", "line": 0, "severity": "P1", "title": "bad line"},
        {"file": "a.py", "line": 3, "severity": "P9", "title": "bad severity"},
        "not an object",
    ]}
    findings, errors = T.parse_findings(payload, lens="security", id_prefix="s-")
    assert [(f.file, f.line, f.severity, f.category) for f in findings] == [("box_agent/cli.py", 12, "P2", "security")]
    assert len(errors) == 3
    assert T.parse_findings({"oops": []}, lens="x", id_prefix="x-") == ([], ["x: payload has no 'findings' list"])


def test_filter_drops_style_and_previously_posted():
    style = _finding(id="a", category="style")
    known = _finding(id="b", title="Already reported")
    fresh = _finding(id="c", title="New issue")
    kept, dropped = T.filter_findings([style, known, fresh], previous_fingerprints=[known.fingerprint()])
    assert [item.id for item in kept] == ["c"]
    assert [item["id"] for item in dropped] == ["a", "b"]


def test_merge_keeps_most_severe_and_unions_lenses():
    a = _finding(id="a", line=10, severity="P2", lenses=["correctness"], instances=[{"file": "x.py", "line": 1}])
    b = _finding(id="b", line=12, severity="P1", lenses=["security"])
    c = _finding(id="c", line=40, lenses=["correctness"])
    other_category = _finding(id="d", line=11, category="security")
    merged = T.merge_findings([a, b, c, other_category])
    primary = next(item for item in merged if item.id == "b")
    assert primary.severity == "P1"
    assert primary.lenses == ["correctness", "security"]
    assert primary.instances == [{"file": "x.py", "line": 1}]
    assert sorted(item.id for item in merged) == ["b", "c", "d"]
    assert a.lenses == ["correctness"]  # inputs are not mutated


def test_changed_lines_tracks_additions_modifications_and_deletion_points():
    diff = "\n".join([
        "diff --git a/a.py b/a.py",
        "--- a/a.py",
        "+++ b/a.py",
        "@@ -3,0 +4,2 @@",
        "+x",
        "+y",
        "@@ -10 +12 @@",
        "-old",
        "+new",
        "@@ -20,2 +21,0 @@",
        "-gone",
        "-gone",
        "diff --git a/old.py b/old.py",
        "--- a/old.py",
        "+++ /dev/null",
        "@@ -1 +0,0 @@",
        "-bye",
    ])
    assert T.changed_lines(diff) == {"a.py": {4, 5, 12, 21}}
    assert not T.is_pre_existing(_finding(file="a.py", line=5), {"a.py": {4, 5}})
    assert T.is_pre_existing(_finding(file="a.py", line=6), {"a.py": {4, 5}})


def test_repository_tier_rules():
    rules = json.loads((Path(T.__file__).parent / "tiers.json").read_text(encoding="utf-8"))
    assert T.classify_tier(["docs/A.md", "README_CN.md"], rules) == "low"
    assert T.classify_tier(["docs/A.md", "box_agent/tools/bash_tool.py"], rules) == "medium"
    assert T.classify_tier(["box_agent/skills/pptx/README.md"], rules) == "medium"
    assert T.classify_tier(["tests/test_x.py", "box_agent/acp/__init__.py"], rules) == "high"
    assert T.classify_tier([".github/workflows/tests.yml"], rules) == "high"
    assert T.classify_tier(["some/unknown/file.txt"], rules) == "medium"
    assert T.classify_tier([], rules) == "medium"


def test_tpr_status():
    template = "## TPR\n\n### Task\n\n- What changed:\n- Why:\n\n### Proof\n\n- Tests or checks run:\n\n### Risk\n\n- Rollback:\n"
    assert T.tpr_status(None)[0] == "not provided by runtime"
    status, details = T.tpr_status(template)
    assert status == "incomplete"
    assert details == {"Task": "incomplete", "Proof": "insufficient", "Risk": "incomplete"}
    filled = template.replace("- What changed:", "- What changed: add vote review").replace(
        "- Tests or checks run:", "- Tests or checks run: pytest").replace("- Rollback:", "- Rollback: revert")
    assert T.tpr_status(filled) == ("complete", {"Task": "complete", "Proof": "provided", "Risk": "complete"})


def test_verdict_rules():
    posted = T.tally_finding(_finding(), [_vote("CONFIRMED")] * 3, voters=3, pre_existing=False)
    disputed = T.tally_finding(_finding(severity="P0"), [_vote("CONFIRMED", "P0")], voters=3, pre_existing=False)
    nit = T.tally_finding(_finding(severity="P3"), [_vote("CONFIRMED", "P3")] * 3, voters=3, pre_existing=False)
    verdict = T.decide_verdict
    assert verdict(preflight="failure", tpr="complete", outcomes=[], agent_failures=0) == "REQUEST_CHANGES"
    assert verdict(preflight="success", tpr="incomplete", outcomes=[], agent_failures=0) == "REQUEST_CHANGES"
    assert verdict(preflight="success", tpr="complete", outcomes=[posted], agent_failures=0) == "REQUEST_CHANGES"
    assert verdict(preflight="success", tpr="complete", outcomes=[nit], agent_failures=0) == "APPROVE"
    assert verdict(preflight="success", tpr="complete", outcomes=[nit], agent_failures=1) == "COMMENT"
    assert verdict(preflight="success", tpr="complete", outcomes=[disputed], agent_failures=0) == "COMMENT"
    assert verdict(preflight="success", tpr="not provided by runtime", outcomes=[], agent_failures=0) == "COMMENT"
    assert verdict(preflight="success", tpr="complete", outcomes=[nit], agent_failures=0, merge_conflicts=1) == "COMMENT"
    assert verdict(preflight="success", tpr="complete", outcomes=[posted], agent_failures=0, merge_conflicts=1) == "REQUEST_CHANGES"


@pytest.mark.parametrize("proof, lens_completed, blocked, expected", [
    ("provided", False, False, "unavailable"),
    ("provided", True, False, "sufficient"),
    ("provided", True, True, "insufficient"),
    ("insufficient", True, False, "insufficient"),
    ("unavailable", True, False, "unavailable"),
])
def test_filled_proof_section_is_not_sufficient_by_itself(proof, lens_completed, blocked, expected):
    assert T.assess_proof(proof, lens_completed=lens_completed, blocked=blocked) == expected


def _reachable(reach, severity="P2"):
    return T.Vote(
        finding_id="F1", voter="v", verdict="CONFIRMED", severity=severity,
        evidence=({"file": "box_agent/core.py", "line": 10, "note": ""},), reason="", reachability=reach,
    )


def test_contrived_majority_caps_severity_and_stops_blocking():
    votes = [_reachable("contrived")] * 3 + [_reachable("edge")] * 2
    outcome = T.tally_finding(_finding(severity="P2"), votes, voters=5, pre_existing=False)
    assert outcome.status == T.POSTED and outcome.severity == "P3" and not outcome.blocking
    assert outcome.reachability_capped and outcome.to_dict()["reachability_capped"] is True


def test_contrived_minority_or_missing_reachability_keeps_severity():
    votes = [_reachable("contrived")] * 2 + [_reachable("edge")] * 3
    assert T.tally_finding(_finding(severity="P2"), votes, voters=5, pre_existing=False).severity == "P2"
    unlabelled = T.tally_finding(_finding(severity="P2"), [_reachable(None)] * 3, voters=3, pre_existing=False)
    assert unlabelled.severity == "P2" and unlabelled.blocking and not unlabelled.reachability_capped


def test_contrived_does_not_cap_p0():
    outcome = T.tally_finding(_finding(severity="P0"), [_reachable("contrived", "P0")] * 3, voters=3, pre_existing=False)
    assert outcome.severity == "P0" and outcome.blocking and not outcome.reachability_capped


def test_parse_votes_keeps_only_known_reachability():
    payload = {"votes": [
        {"id": "F1", "verdict": "CONFIRMED", "severity": "P2", "reachability": "Contrived",
         "evidence": [{"file": "a.py", "line": 1}]},
        {"id": "F2", "verdict": "CONFIRMED", "severity": "P2", "reachability": "maybe",
         "evidence": [{"file": "a.py", "line": 1}]},
    ]}
    votes, _ = T.parse_votes(payload, voter="v", finding_ids=["F1", "F2"])
    assert [vote.reachability for vote in votes] == ["contrived", None]


def test_target_interaction_is_not_pre_existing_only_when_the_target_moved():
    finding = _finding(category=T.TARGET_INTERACTION, file="box_agent/agent.py", line=900)
    assert T.is_pre_existing(finding, {}, target_ahead=True) is False
    assert T.is_pre_existing(finding, {}, target_ahead=False) is True
    assert T.is_pre_existing(_finding(file="box_agent/agent.py", line=900), {}, target_ahead=True) is True
