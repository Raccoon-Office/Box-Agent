"""End-to-end vote review through the real Box-Agent SDK path with a scripted LLM."""

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
    "macOS / Windows / Linux and packaged runtime": None,  # no parseable JSON
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
    assert llm.calls["hunter"] == 5  # four lenses, one extra turn after the tool call
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
    assert report["verdict"] == "REQUEST_CHANGES"

    markdown = R.render_markdown(report)
    assert "Verdict: REQUEST_CHANGES" in markdown
    assert f"Reviewed SHA: {ctx.head_sha}" in markdown
    assert "- [P1] compute() is undefined — `app.py:10`" in markdown
    assert "投票：2/3 CONFIRMED（需 2）" in markdown
    assert "## 存疑·需人工复核" in markdown and "possible injection" in markdown
    assert "hunter-cross-platform" in markdown

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
