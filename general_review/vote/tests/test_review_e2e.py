"""End-to-end vote review through the real Box-Agent SDK path with a scripted LLM."""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

import review as R
import tally as T
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import FunctionCall, StreamEvent, ToolCall


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(key, value)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text("\n".join(f"line_{i} = {i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    lines = (root / "app.py").read_text(encoding="utf-8").splitlines()
    lines[9] = "line_10 = compute()  # changed"
    (root / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "change line 10")
    (tmp_path / "outside.txt").write_text("secret outside the repo", encoding="utf-8")
    return root


HUNTER_PAYLOADS = {
    "Correctness and regressions": {"findings": [
        {"file": "app.py", "line": 10, "category": "correctness", "severity": "P1",
         "title": "compute() is undefined", "trigger": "importing app.py", "impact": "NameError",
         "evidence": "app.py:10", "suggestion": "define compute"},
        {"file": "app.py", "line": 10, "category": "style", "severity": "P3", "title": "comment style"},
        {"file": "app.py", "line": 10, "category": "security", "severity": "P0",
         "title": "possible injection", "trigger": "t", "impact": "i"},
    ]},
    "Security, permissions and protocol safety": {"findings": [
        {"file": "app.py", "line": 11, "category": "correctness", "severity": "P2",
         "title": "compute() missing", "trigger": "import", "impact": "crash",
         "instances": [{"file": "app.py", "line": 10}]},
    ]},
    "Ownership and architecture boundaries": {"findings": [
        {"file": "app.py", "line": 3, "category": "architecture", "severity": "P2",
         "title": "module-level state", "trigger": "t", "impact": "i"},
    ]},
    "macOS / Windows / Linux and packaged runtime": None,  # no parseable JSON, even after the repair turn
    "History and target-branch consistency": {"findings": []},
}


class ScriptedReviewLLM:
    def __init__(self, repo):
        self.repo = repo
        self.tool_results = []
        self.calls = {"hunter": 0, "verifier": 0}

    async def generate_stream(self, **kwargs):
        messages = kwargs["messages"]
        system = str(messages[0].content)
        user = next(str(m.content) for m in messages if m.role == "user")
        tool_messages = [m for m in messages if m.role == "tool"]
        if "independent verifier" in system:
            self.calls["verifier"] += 1
            claims = json.loads(user.split("<findings>\n", 1)[1].split("\n</findings>", 1)[0])
            index = self.calls["verifier"]
            votes = []
            for claim in claims:
                if "compute()" in claim["title"]:
                    verdict = "REJECTED" if index % 3 == 0 else "CONFIRMED"
                elif "injection" in claim["title"]:
                    verdict = "CONFIRMED" if index % 3 == 1 else "REJECTED"
                else:
                    verdict = "CONFIRMED"
                votes.append({"id": claim["id"], "verdict": verdict, "severity": "P1" if "compute" in claim["title"]
                              else ("P0" if "injection" in claim["title"] else "P2"),
                              "evidence": [{"file": claim["file"], "line": claim["line"], "note": "read"}],
                              "reason": "scripted"})
            text = "Votes:\n```json\n" + json.dumps({"votes": votes}) + "\n```"
            yield StreamEvent(type="text", delta=text)
            yield StreamEvent(type="finish", finish_reason="stop")
            return

        self.calls["hunter"] += 1
        title = next(key for key in HUNTER_PAYLOADS if key in system)
        if title == "Correctness and regressions" and not tool_messages:
            diff_path = next(line.split(":")[0][2:] for line in user.splitlines() if line.endswith("full diff of the review range"))
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                ToolCall(id="r1", type="function", function=FunctionCall(name="read_file", arguments={"path": diff_path})),
                ToolCall(id="r2", type="function", function=FunctionCall(
                    name="read_file", arguments={"path": str(self.repo.parent / "outside.txt")})),
            ])
            return
        self.tool_results.extend(str(m.content) for m in tool_messages)
        payload = HUNTER_PAYLOADS[title]
        text = "no json here" if payload is None else "Findings:\n```json\n" + json.dumps(payload) + "\n```"
        yield StreamEvent(type="text", delta=text)
        yield StreamEvent(type="finish", finish_reason="stop")


def _config(repo):
    return Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(repo), max_steps=4),
        tools=ToolsConfig(
            enable_mcp=False, enable_skills=False, enable_bash=False, enable_file_tools=False,
            enable_todo=False, enable_plan=False, enable_sub_agent=False,
        ),
    )


TPR = "### Task\n\n- What changed: line 10\n\n### Proof\n\n- Tests: none\n\n### Risk\n\n- Rollback: revert\n"


async def test_vote_review_end_to_end_through_sdk(repo, tmp_path):
    tpr_file = tmp_path / "tpr.md"
    tpr_file.write_text(TPR, encoding="utf-8")
    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=tpr_file, out_dir=None)
    assert ctx.files == ["app.py"]
    assert ctx.changes == {"app.py": {10}}
    assert ctx.out_dir == (repo / "workspace" / "review-vote" / ctx.head_sha[:12]).resolve()

    llm = ScriptedReviewLLM(repo)
    runner = R.make_sdk_runner(
        config=_config(repo), llms={"hunter": llm, "verifier": llm}, repo=repo, max_steps=4, timeout=60,
    )
    report = await R.run_review(ctx, depth="standard", tier="medium", runner=runner, preflight="success")

    # Read-only tools work inside the repository and refuse paths outside it.
    assert any("compute()" in result for result in llm.tool_results)
    assert not any("secret outside the repo" in result for result in llm.tool_results)
    assert llm.calls["hunter"] == 7  # five lenses, one turn after the tool call, one repair turn
    assert llm.calls["verifier"] == 3

    assert report["verifiers_per_finding"] == 3
    assert [item["agent"] for item in report["agent_failures"]] == ["hunter-cross-platform"]
    assert [item["reason"] for item in report["filtered"]] == ["category 'style' is not reviewed"]
    by_title = {item["finding"]["title"]: item for item in report["outcomes"]}
    assert set(by_title) == {"compute() is undefined", "possible injection", "module-level state"}

    merged = by_title["compute() is undefined"]
    assert merged["status"] == T.POSTED and merged["blocking"]
    assert merged["confirmations"] == 2 and merged["severity"] == "P1"
    assert merged["finding"]["lenses"] == ["correctness", "security"]

    disputed = by_title["possible injection"]
    assert disputed["status"] == T.DISPUTED and disputed["confirmations"] == 1

    pre_existing = by_title["module-level state"]
    assert pre_existing["status"] == T.POSTED and pre_existing["label"] == "Pre-existing"
    assert not pre_existing["blocking"]

    assert report["tpr"] == "complete"
    assert report["tpr_details"]["Proof"] == "unavailable"  # standard depth runs no proof lens
    assert report["verdict"] == "REQUEST_CHANGES"
    assert report["coverage"]["lenses_failed"] == ["cross-platform"]
    assert not report["coverage"]["complete"]

    markdown = R.render_markdown(report)
    assert "Verdict: REQUEST_CHANGES" in markdown
    assert f"Reviewed SHA: {ctx.head_sha}" in markdown
    assert "- [P1] compute() is undefined — `app.py:10`" in markdown
    assert "投票：2/3 CONFIRMED（需 2）" in markdown
    assert "## 存疑·需人工复核" in markdown and "possible injection" in markdown
    assert "hunter-cross-platform" in markdown
    assert "Coverage: incomplete（4/5 个视角完成；未完成视角：cross-platform" in markdown
    assert "本次审查不完整，不能作为 G2 结论" in markdown
    assert "Proof: unavailable（PR 已填写" in markdown

    # The repository itself is untouched apart from the git-ignored-by-convention context pack.
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo, capture_output=True, text=True,
    ).stdout
    assert status == ""


async def test_failed_preflight_starts_no_agents(repo):
    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=None)

    async def runner(*_):
        raise AssertionError("no agent may start when preflight failed")

    report = await R.run_review(ctx, depth="quick", tier="medium", runner=runner, preflight="failure")
    assert report["verdict"] == "REQUEST_CHANGES"
    assert "preflight" in report["skipped_reason"]
    assert "投票审查未启动" in R.render_markdown(report)


async def test_round_cap_asks_to_step_back(repo):
    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=None)

    async def runner(*_):
        raise AssertionError("no agent may start past the round cap")

    report = await R.run_review(
        ctx, depth="quick", tier="medium", runner=runner, preflight="success", round_number=4,
    )
    assert report["verdict"] == "COMMENT"
    assert "reconsider the approach" in report["skipped_reason"]


def test_context_requires_the_reviewed_head_checked_out_and_clean(repo):
    with pytest.raises(R.ReviewError, match="is not the review head"):
        R.build_context(repo, base="main", head="main", since=None, tpr_file=None, out_dir=None)
    (repo / "app.py").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(R.ReviewError, match="uncommitted changes"):
        R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=None)


def test_context_rejects_out_dir_outside_repo(repo, tmp_path):
    with pytest.raises(R.ReviewError, match="inside the repository"):
        R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=tmp_path / "elsewhere")


def test_since_limits_hunter_diff_but_not_pre_existing_baseline(repo):
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "extra.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "extra.py")
    _git(repo, "commit", "-q", "-m", "round 2")
    ctx = R.build_context(repo, base="main", head="HEAD", since=first, tpr_file=None, out_dir=None)
    assert ctx.files == ["extra.py"]
    assert ctx.changes == {"app.py": {10}, "extra.py": {1}}

    _git(repo, "checkout", "-q", "-b", "side", "main")
    (repo / "side.py").write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", "side.py")
    _git(repo, "commit", "-q", "-m", "side")
    side = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    _git(repo, "checkout", "-q", "feature")
    with pytest.raises(R.ReviewError, match="not an ancestor"):
        R.build_context(repo, base="main", head="HEAD", since=side, tpr_file=None, out_dir=None)


def _advance_main(repo, path="connector.py", text="snapshot = True\n"):
    """Move the target branch past the merge-base, the way main moves while a PR is open."""
    _git(repo, "checkout", "-q", "main")
    (repo / path).write_text(text, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", f"target: touch {path}")
    _git(repo, "checkout", "-q", "feature")


def _rev(repo, ref="HEAD"):
    return subprocess.run(["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True).stdout.strip()


def test_context_shows_the_merge_result_for_files_the_pr_never_touched(repo):
    _advance_main(repo)
    head, refs_before = _rev(repo), subprocess.run(
        ["git", "for-each-ref"], cwd=repo, capture_output=True, text=True).stdout

    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=None)

    assert ctx.files == ["app.py"]  # the PR does not touch connector.py
    assert ctx.target.ahead == 1 and ctx.target.merge_check == "clean"
    assert ctx.target.changed == ["connector.py"] and ctx.target.overlap == []
    assert (ctx.out_dir / "merged" / "connector.py").read_text(encoding="utf-8") == "snapshot = True\n"
    assert "+snapshot = True" in (ctx.out_dir / "target.patch").read_text(encoding="utf-8")
    assert "target: touch connector.py" in (ctx.out_dir / "target-commits.txt").read_text(encoding="utf-8")

    message = R._context_message(ctx)
    assert "1 commits ahead of the merge-base" in message and "Merge check: clean." in message
    assert f"{ctx.pack['merged']}/<repo-relative path>" in message
    assert "target: touch connector.py" in message

    # Only unreachable objects are written: same head, same refs, clean tree.
    assert _rev(repo) == head
    assert subprocess.run(["git", "for-each-ref"], cwd=repo, capture_output=True, text=True).stdout == refs_before
    assert not (repo / "connector.py").exists()


def test_merged_view_carries_both_sides_of_a_shared_file(repo):
    _git(repo, "checkout", "-q", "main")
    lines = (repo / "app.py").read_text(encoding="utf-8").splitlines()
    lines[18] = "line_19 = 'target'"
    (repo / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "target: line 19")
    _git(repo, "checkout", "-q", "feature")

    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=None)

    merged = (ctx.out_dir / "merged" / "app.py").read_text(encoding="utf-8")
    assert "compute()" in merged and "line_19 = 'target'" in merged
    assert ctx.target.overlap == ["app.py"]
    assert "compute()" not in (ctx.out_dir / "target.patch").read_text(encoding="utf-8").split("\n@@")[0]


def test_context_without_target_commits_adds_nothing_and_clears_a_stale_pack(repo):
    _advance_main(repo)
    first = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=None)
    assert (first.out_dir / "merged").is_dir()

    # Same head reviewed against a base that has not moved: the old merged view must not survive.
    ctx = R.build_context(repo, base="HEAD~1", head="HEAD", since=None, tpr_file=None, out_dir=None)
    assert ctx.target.ahead == 0 and ctx.target.merge_check == "up-to-date"
    assert "merged" not in ctx.pack and "target_diff" not in ctx.pack
    assert not (ctx.out_dir / "merged").exists() and not (ctx.out_dir / "target.patch").exists()
    assert "no commits beyond the merge-base" in R._context_message(ctx)


def test_conflicts_with_the_target_are_listed_and_block_approval(repo, tmp_path):
    _advance_main(repo, path="app.py", text="rewritten = True\n")
    tpr_file = tmp_path / "tpr.md"
    tpr_file.write_text(TPR, encoding="utf-8")
    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=tpr_file, out_dir=None)
    assert ctx.target.merge_check == "conflicts" and ctx.target.conflicts == ["app.py"]
    assert "Conflicted files:" in R._context_message(ctx)

    async def runner(role, _system, _message, _agent):
        return '{"findings": []}'

    report = asyncio.run(R.run_review(ctx, depth="quick", tier="low", runner=runner, preflight="success"))
    assert report["verdict"] == "COMMENT"
    assert "1 个文件有冲突" in R.render_markdown(report)


def test_git_without_merge_tree_falls_back_to_the_target_diff(repo, monkeypatch):
    _advance_main(repo)
    monkeypatch.setattr(R, "_merge_tree", lambda *_: (None, []))
    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=None)
    assert ctx.target.merge_check == "unavailable" and "merged" not in ctx.pack
    assert "+snapshot = True" in (ctx.out_dir / "target.patch").read_text(encoding="utf-8")
    assert "git could not compute a merge result" in R._context_message(ctx)


def test_stale_local_base_is_flagged(repo):
    _advance_main(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", _rev(repo, "main"))
    _git(repo, "update-ref", "refs/heads/main", _rev(repo, "main~1"))
    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=None)
    assert ctx.target.remote_differs == "origin/main"


def _scripted_runner(findings, votes=None):
    async def runner(role, _system, _message, agent_id):
        if role == "hunter":
            return json.dumps({"findings": findings if "correctness" in agent_id else []})
        return json.dumps({"votes": votes or []})
    return runner


async def test_target_interaction_finding_blocks_outside_changed_lines(repo):
    _advance_main(repo)
    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=None)
    finding = {
        "file": f"{ctx.pack['merged']}/connector.py", "line": 1, "category": T.TARGET_INTERACTION,
        "severity": "P1", "title": "snapshot breaks matching", "trigger": "after merge", "impact": "no evidence",
    }
    vote = {"id": "F1", "verdict": "CONFIRMED", "severity": "P1", "reachability": "normal",
            "evidence": [{"file": "connector.py", "line": 1, "note": "read"}]}
    report = await R.run_review(
        ctx, depth="quick", tier="low", runner=_scripted_runner([finding], [vote]), preflight="success",
    )
    outcome = report["outcomes"][0]
    assert outcome["finding"]["file"] == "connector.py"  # pack prefix removed
    assert outcome["pre_existing"] is False and outcome["blocking"] is True
    assert report["verdict"] == "REQUEST_CHANGES"


async def test_wrong_json_shape_is_a_failure_not_an_empty_lens(repo):
    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=None, out_dir=None)

    async def runner(*_):
        return '{"result": "looks fine"}'

    report = await R.run_review(ctx, depth="quick", tier="low", runner=runner, preflight="success")
    assert report["coverage"]["lenses_completed"] == []
    assert "no 'findings' list" in report["agent_failures"][0]["error"]
    assert report["verdict"] == "COMMENT"


async def test_complete_coverage_with_proof_lens_marks_proof_sufficient(repo, tmp_path):
    tpr_file = tmp_path / "tpr.md"
    tpr_file.write_text(TPR, encoding="utf-8")
    ctx = R.build_context(repo, base="main", head="HEAD", since=None, tpr_file=tpr_file, out_dir=None)
    report = await R.run_review(ctx, depth="deep", tier="high", runner=_scripted_runner([]), preflight="success")
    assert report["coverage"]["complete"] and report["tpr_details"]["Proof"] == "sufficient"
    assert report["verdict"] == "APPROVE"
    markdown = R.render_markdown(report)
    assert "Coverage: complete（6/6 个视角完成）" in markdown and "本次审查不完整" not in markdown
    assert "- Proof: sufficient" in markdown


@pytest.mark.parametrize("text, expected", [
    ('Done.\n```json\n{"findings": []}\n```', {"findings": []}),
    ('```json\n{"findings": []}```', {"findings": []}),
    ('I checked {not json} and: {"findings": [{"file": "a"}]} as promised.', {"findings": [{"file": "a"}]}),
    ('I will report one P3 finding.', None),
])
def test_extract_json(text, expected):
    assert R.extract_json(text) == expected


class _RateLimitError(Exception):
    status_code = 429


class _FlakyLLM:
    """Fails the first ``failures`` requests, then answers the role's payload."""

    def __init__(self, failures, error=None, final='```json\n{"findings": []}\n```', first_answer=None):
        self.failures = failures
        self.error = error or _RateLimitError("Error code: 429 - rpm exhausted")
        self.final = final
        self.first_answer = first_answer
        self.requests = 0

    async def generate_stream(self, *_args, **_kwargs):
        self.requests += 1
        if self.requests <= self.failures:
            raise self.error
        text = self.first_answer if self.first_answer and self.requests == self.failures + 1 else self.final
        yield StreamEvent(type="text", delta=text)
        yield StreamEvent(type="finish", finish_reason="stop")


def _fast_gate():
    """A gate on a fake clock, so backoff is instant and the waits still add up."""
    now = {"t": 0.0}

    async def sleep(seconds):
        now["t"] += seconds

    return R.RateGate(sleep=sleep, clock=lambda: now["t"])


def _sdk_runner(repo, llm, **kwargs):
    return R.make_sdk_runner(
        config=_config(repo), llms={"hunter": llm, "verifier": llm}, repo=repo, max_steps=4, timeout=60, **kwargs,
    )


async def test_rate_limit_waits_and_keeps_the_agent_running(repo):
    gate = _fast_gate()
    llm = _FlakyLLM(failures=2)
    answer = await _sdk_runner(repo, R.PacedLLM(llm, gate))("hunter", "system", "go", "hunter-x")
    assert R.extract_json(answer) == {"findings": []}
    assert llm.requests == 3 and gate.rate_limited == 2


async def test_exhausted_rate_limit_reports_the_real_cause(repo):
    gate = _fast_gate()
    llm = _FlakyLLM(failures=99)
    with pytest.raises(RuntimeError, match="429"):
        await _sdk_runner(repo, R.PacedLLM(llm, gate))("hunter", "system", "go", "hunter-x")
    assert llm.requests == R.RATE_LIMIT_RETRIES + 1


async def test_other_errors_are_not_retried_and_keep_their_cause(repo):
    llm = _FlakyLLM(failures=99, error=ValueError("upstream rejected the request"))
    with pytest.raises(RuntimeError, match="upstream rejected the request"):
        await _sdk_runner(repo, R.PacedLLM(llm, _fast_gate()))("hunter", "system", "go", "hunter-x")
    assert llm.requests == 1


async def test_rate_limit_after_output_started_is_not_retried():
    class Partial:
        requests = 0

        async def generate_stream(self):
            self.requests += 1
            yield StreamEvent(type="text", delta="partial")
            raise _RateLimitError("429")

    client = Partial()
    with pytest.raises(_RateLimitError):
        async for _ in R.PacedLLM(client, _fast_gate()).generate_stream():
            pass
    assert client.requests == 1


async def test_gate_pause_holds_every_request_and_rpm_spaces_them():
    now = {"t": 100.0}
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        now["t"] += seconds

    gate = R.RateGate(30, sleep=sleep, clock=lambda: now["t"])
    await gate.acquire()
    await gate.acquire()
    assert slept == [2.0]
    gate.pause(45)
    await gate.acquire()
    assert slept[-1] == pytest.approx(45)


def test_retry_after_header_sets_the_delay():
    class Response:
        headers = {"retry-after": "7"}

    error = _RateLimitError("429")
    error.response = Response()
    assert R._rate_limit_delay(error, 0) == 7.0
    assert R._rate_limit_delay(_RateLimitError("429"), 0) == R.RATE_LIMIT_DELAY
    assert R._rate_limit_delay(_RateLimitError("429"), 9) == R.RATE_LIMIT_MAX_DELAY


async def test_missing_json_gets_one_repair_turn(repo):
    stats = R.RunnerStats()
    llm = _FlakyLLM(failures=0, first_answer="I'll report the finding as a P3.")
    answer = await _sdk_runner(repo, llm, stats=stats)("hunter", "system", "go", "hunter-x")
    assert R.extract_json(answer) == {"findings": []}
    assert llm.requests == 2 and stats.repaired == ["hunter-x"]


async def test_repair_turn_is_not_used_when_the_answer_is_valid(repo):
    stats = R.RunnerStats()
    llm = _FlakyLLM(failures=0)
    await _sdk_runner(repo, llm, stats=stats)("hunter", "system", "go", "hunter-x")
    assert llm.requests == 1 and stats.repaired == []


@pytest.mark.parametrize("arguments, message", [
    (["--tpr-file", "missing.md"], "--tpr-file is not readable"),
    (["--previous", "missing.json"], "--previous is not a usable"),
    (["--previous", "bad.json"], "--previous is not a usable"),
    (["--previous", "wrong_shape.json"], "--previous is not a usable"),
    (["--config", "bad.yaml"], "config is not usable"),
    (["--config", "missing.yaml"], "config is not usable"),
])
def test_invalid_input_exits_2_with_a_message(repo, tmp_path, monkeypatch, capsys, arguments, message):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "wrong_shape.json").write_text('{"outcomes": [{"status": "posted"}]}', encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("llm: [unclosed\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert R.main(["--repo", str(repo), "--preflight", "success", *arguments]) == 2
    assert message in capsys.readouterr().err
