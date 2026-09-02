# Box-Agent Pull Request Review Standard

This standard applies to every non-trivial Box-Agent pull request and to reviews
produced by automated Review Agents. Merge decisions must be based on reproducible
evidence while preserving ownership boundaries across Core, CLI, ACP, Tools,
Skills, providers, workflows, and packaged runtimes.

`Must` defines a merge gate. A maintainer may make an exception to `should` only
when the reason and residual risk are recorded in the PR.

## 1. Roles and responsibilities

| Role | Responsibility | Must not |
| --- | --- | --- |
| PR author | Supply one scoped change, TPR, tests, and risk evidence | Delegate missing proof or scope discovery to the reviewer |
| Local Preflight CI | Run deterministic install, compile, test, and build commands at the exact Head | Judge design, modify code, or replace review |
| Review Agent | Find concrete issues from the diff, source, tests, documentation, and history | Continue full review when Preflight failed; modify the PR |
| Human maintainer | Verify high-severity findings, ownership, proof, and residual risk | Merge solely because an Agent returned `APPROVE` |

Review Agents provide decision input. Human maintainers retain final merge
authority.

## 2. Merge gates

Pull requests pass these gates in order:

1. **G0 — PR metadata:** title, scope, TPR, design impact, and target-branch
   consistency fields are complete.
2. **G1 — Local Preflight CI:** required status `teamwork/local-ci` is `success`
   for the current Head SHA.
3. **G2 — Review Agents:** no unresolved P0, P1, or unaccepted P2 finding.
4. **G3 — Human maintainer:** ownership, proof, risk, and residual risk are
   accepted.
5. **G4 — GitHub protection:** all required checks and reviews pass.

The repository-owned command is:

```bash
bash general_review/ci/preflight.sh
```

The generic review engine runs it in a detached temporary worktree at the exact
PR Head. A failure or timeout publishes `failure` and blocks Review Agents. An
infrastructure error publishes `error` and may retry according to policy. A new
Head SHA or review-profile revision requires a new result.

The current executor is only for trusted internal pull requests. Worktrees and
environment filtering are not a container or operating-system security boundary.

## 3. TPR and PR evidence

Every non-trivial PR must include:

### Task

- Observable behavior changed and why.
- Affected entry points, modules, and callers.
- Explicit out-of-scope work.

### Proof

- Exact commands and actual results.
- Focused regression evidence for the changed behavior.
- Broader tests, probes, screenshots, logs, manifests, or runtime checks required
  by the impact.
- Explicit reasons and impact for every skipped verification.

### Risk

- Compatibility, configuration, secrets, data, protocol, and migration impact.
- Source-only versus packaged-runtime impact.
- Rollback and cross-repository follow-up.
- Known accepted limitations and residual risk.

The PR must also identify affected architecture layers and relevant target-branch
changes after the merge base. Missing Task, Proof, or Risk blocks deep review.

## 4. Automated Review workflow

The Root Review Agent must:

1. Record the reviewed Head SHA and verify the configured change ref.
2. Review the complete merge-base diff against the target ref.
3. Read repository guidance and map requirements to changes, proof, and risk.
4. Use `.understand-anything/` only as an index, check its baseline, and verify
   conclusions against current source and Git history.
5. Invoke `design-review` and `history-consistency-review` for every full review.
6. Invoke `security-review` when security boundaries are touched.
7. Check correctness, security, compatibility, ownership, tests, runtime impact,
   and documentation before style.
8. Report only findings with a trigger, impact, evidence, and repair direction.
9. Follow the output format in section 8.

All automated roles are read-only. Unless a separate authorized repair task says
otherwise, they must not modify code, push branches, comment on the PR, or change
GitHub state.

## 5. Ownership boundaries

- Shared loop invariants, events, scheduling, cancellation, tool-call closure,
  goals, and Session Log persistence belong to stable shared contracts.
- CLI owns terminal UX and local rendering; ACP owns protocol translation and
  must keep stdout protocol-clean.
- Provider wire behavior belongs in `box_agent/llm/`.
- Tool semantics belong in `box_agent/tools/` and return structured results.
- Skill loading belongs in `box_agent/skill_loader.py`, `box_agent/skills/`, and
  `_manifest.json`.
- Stateful product workflows belong in self-contained Skills or plugins and
  must not introduce a second durable session-state source.
- PPT/document behavior remains Skill-driven unless a host-neutral shared
  contract changes.
- officev3 and standalone runtime behavior require runtime evidence; source edits
  alone do not prove packaged behavior.

Changes to the stable kernel, protocols, security enforcement points, or packaged
runtime require the responsible human owner.

## 6. Minimum proof by change type

| Change type | Minimum proof |
| --- | --- |
| Shared loop, events, cancellation, goals, completion | Focused regression plus related Core/ACP tests |
| CLI-only | Focused CLI test/output and confirmation that ACP behavior was not duplicated |
| ACP/runtime | ACP test or real probe and stdout/stderr boundary check |
| Tool | Success, important failure, workspace, and permission-boundary tests |
| Provider | Wire-format, error mapping, timeout/retry, and secret exposure tests |
| MCP loading/config | Loader test or explicit connection/configuration probe |
| Memory | Focused memory test and applicable configuration-gating check |
| Built-in Skill | Focused Skill test, manifest regeneration, and manifest diff |
| Recommended/on-demand Skill | Manifest exclusion and host recommendation impact |
| PPT/document | Skill contract tests and visual/runtime probe when required |
| Packaged runtime | Build/install/probe or explicit source-only limitation |
| Config/schema | Defaults, invalid input, compatibility, and migration evidence |
| Docs-only | Link/path/command checks and `git diff --check` |

Run the smallest command that proves the claim, then broaden according to impact.
Passing tests do not replace design, security, ownership, or risk review.

## 7. Finding severity

| Severity | Definition | Merge handling |
| --- | --- | --- |
| P0 Critical | Credential disclosure, arbitrary execution, irreversible data loss, permission bypass, or widespread outage | Must fix and fully re-review |
| P1 High | Definite functional defect, protocol break, unusable CI/runtime, race, or major compatibility regression | Must fix with regression proof |
| P2 Medium | Realistic conditional defect, resource leak, error-handling gap, or significant maintenance risk | Blocks by default; maintainer may explicitly accept |
| P3 Low | Local clarity, performance, or consistency improvement without correctness impact | Non-blocking follow-up |

Every finding includes the smallest useful location, trigger, impact, evidence,
and repair direction. Style preference, unsupported speculation, naming without
behavior impact, and risks excluded by existing proof are not blocking findings.

## 8. Review output

```markdown
# PR Review Result

Verdict: APPROVE | REQUEST_CHANGES | COMMENT
Reviewed SHA: <40-character head sha>
Preflight: success | failure | error | missing
TPR: complete | incomplete | not provided by runtime

## TPR check
- Task: complete | incomplete | unavailable
- Proof: sufficient | insufficient | unavailable
- Risk: complete | incomplete | unavailable

## Blocking findings
- [P1] Title — `path/to/file.py:123`
  - Trigger: ...
  - Impact: ...
  - Evidence: ...
  - Direction: ...

## Non-blocking suggestions
- [P3] ...

## Verification evidence
- `<actual command>` — passed/failed/not run

## Residual risk
- ...
```

Write `None` when a finding section is empty. Never claim an unexecuted command
passed.

## 9. Verdict rules

| Condition | Verdict |
| --- | --- |
| Preflight missing, failed, or errored | `REQUEST_CHANGES`; no full Review Agent should run |
| TPR is demonstrably missing or proof cannot establish behavior | `REQUEST_CHANGES` |
| Runtime does not provide TPR data and no blocking defect is proven | `COMMENT` unless a human supplies the missing evidence |
| Unresolved P0/P1 | `REQUEST_CHANGES` |
| P2 not explicitly accepted | `REQUEST_CHANGES` |
| Only P3 or no findings and every gate is verified | `APPROVE` |
| Clarification needed without a proven blocker | `COMMENT` |

After a new Head SHA, rerun Preflight and verify original findings against the
new behavior. Do not reuse an older success or review.

## 10. Mandatory request-changes conditions

- Missing Task, Proof, or Risk when the PR body is available.
- `teamwork/local-ci` does not succeed for the reviewed Head SHA.
- Proof does not cover the behavior or critical failure path.
- Shared behavior is duplicated across CLI and ACP without a strong reason.
- Runtime-sensitive behavior is claimed without runtime evidence.
- Credential exposure, path escape, injection, or permission regression.
- Built-in Skill change without regenerated and reviewed `_manifest.json`.
- User-visible, protocol, or contributor-facing change without documentation.
- Unrelated refactor, formatting noise, local config, logs, workspace state, or
  invalid `.understand-anything` generated state in the diff.

## 11. Common commands

```bash
git diff --check
git diff --merge-base <target-ref> <change-ref>
git log --oneline <merge-base>..<target-ref> -- <relevant-paths>
bash general_review/ci/preflight.sh
uv run pytest tests/test_core.py -q
uv run pytest tests/test_acp.py -q
uv run pytest tests/test_memory.py -q
uv run python scripts/generate_skills_manifest.py
uv run box-agent-build-runtime
```

Preflight proves only that deterministic commands passed. It does not
automatically approve architecture, behavior, security, or residual risk.
