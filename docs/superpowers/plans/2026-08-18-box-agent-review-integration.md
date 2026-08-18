# Box-Agent Automated Review Integration Implementation Plan

> 2026-08-18 更新：本文记录的是最初的双 CI 实施计划。现行方案已经移除
> GitHub Actions CI，只保留 Teamwork 本地 Preflight + Review Agent；运行来源以
> `general_review/review.config.yaml`、`general_review/ci/preflight.sh` 和
> `docs/AUTOMATED_REVIEW.md` 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Box-Agent own its CI, review roles, trigger rules, and repository-specific review contract while `teamwork_review_agents` remains a reusable review engine.

**Architecture:** Commit a complete, secret-free review profile under `general_review/` and launch the generic engine with that profile. The existing `teamwork_review_agents/prompts/general-review.md` is maintained once and resolved through `TEAMWORK_REVIEW_AGENTS_ROOT`; Box-Agent never duplicates or modifies it. The profile runs one Box-Agent-owned preflight script, then starts one read-only reviewer whose existing Prompt covers correctness, security, design, history, and target-branch consistency. Existing repository guidance, PR metadata, documentation, GitHub Actions, and branch protection are aligned around the same contract.

**Tech Stack:** YAML, Markdown, Bash, GitHub Actions, Python/uv, Codex CLI, `teamwork_review_agents`.

**Spec:** `docs/AUTOMATED_REVIEW.md`

## Global Constraints

- `teamwork_review_agents` contains only reusable Prompt policy; it must not contain Box-Agent-specific CI commands, paths, or review rules.
- Box-Agent-owned configuration must contain no literal credentials.
- Preflight and all review agents are limited to trusted internal pull requests in the current deployment model.
- Review agents remain read-only and human maintainers retain final merge authority.
- `teamwork/local-ci` is the external required status context.
- Review covers the full merge-base diff, not only the latest commit.
- The single general reviewer covers design, history/target-branch consistency, and security in every full review.

---

### Task 1: Repository-owned review profile

**Files:**
- Create: `general_review/review.config.yaml`
- Create: `general_review/README.md`
- Modify in generic repository: `prompts/general-review.md`

**Interfaces:**
- Consumes: `teamwork-review-agents -c <path>` and the existing event context fields `change_ref`, `target_ref`, and `MR_HEAD_SHA`.
- Produces: one validated AppConfig with a single reviewer that consumes the existing generic Prompt unchanged.

- [x] Create a secret-free complete YAML profile rooted at `general_review/`.
- [x] Configure the repository workspace as `..`, resolved from the profile directory.
- [x] Reference the existing generic Prompt through `TEAMWORK_REVIEW_AGENTS_ROOT`; do not copy it into Box-Agent.
- [x] Use the existing Prompt sections for design, history consistency, and security review.
- [x] Keep the reviewer on `read-only` with empty `write_scopes` and `allowed_sub_agents`.
- [x] Validate the profile with the generic engine CLI.

### Task 2: Single Box-Agent CI contract

**Files:**
- Create: `general_review/ci/preflight.sh`
- Create: `.gitattributes`
- Modify: `.github/workflows/tests.yml`
- Modify: `.gitignore`
- Modify: `.understand-anything/.understandignore`

**Interfaces:**
- Consumes: `uv`, Bash, `pyproject.toml`, `uv.lock`, the `box_agent/` package, and `tests/`.
- Produces: one fail-fast command used by local preflight and GitHub Actions.

- [x] Add the ordered install, compile, test, and build commands to the script.
- [x] Verify the script parses with `bash -n` before executing it.
- [x] Point GitHub Actions at the same script and preserve the Python matrix.
- [x] Force LF line endings for shell scripts.
- [x] Ignore only runtime state, not the committed profile.

### Task 3: Repository policy and review documentation

**Files:**
- Create: `docs/AUTOMATED_REVIEW.md`
- Administrator follow-up: create `.github/CODEOWNERS` after real GitHub Team/User mappings are confirmed.
- Modify: `.github/pull_request_template.md`
- Modify: `AGENTS.md`
- Modify: `CONTRIBUTING.md`
- Modify: `CONTRIBUTING_CN.md`
- Modify: `docs/README.md`
- Modify: `docs/REVIEW_GUIDE.md`
- Modify: `docs/LOCAL_CI_VALIDATION.md`

**Interfaces:**
- Consumes: the profile and CI script from Tasks 1-2.
- Produces: one documented G0-G4 merge contract shared by authors, agents, and maintainers.

- [x] Document generic-engine versus target-repository ownership.
- [x] Add PR design and target-branch consistency evidence fields.
- [x] Point repository instructions and contribution guides to the committed profile.
- [x] Synchronize the English review guide with the Chinese gate and finding model.
- [x] Mark the existing CI validation report as point-in-time evidence, not live configuration.
- [x] Record the missing CODEOWNERS mapping without inventing a GitHub Team slug.

### Task 4: Integration verification

**Files:**
- Verify all files changed by Tasks 1-3.

**Interfaces:**
- Consumes: the checked-in profile, Bash script, generic engine CLI, and Git repository.
- Produces: reproducible validation evidence and an explicit list of remaining upstream blockers.

- [x] Run `bash -n general_review/ci/preflight.sh` in WSL/Bash.
- [x] Run `teamwork-review-agents validate -c general_review/review.config.yaml` from the preflight-enabled generic-engine checkout.
- [x] Run `git diff --check`.
- [x] Run the profile CI script or the widest safe equivalent and record the exact result (manual run reached 36%, then an existing sandbox `pip install` remained blocked until the run was stopped).
- [x] Review `git diff --stat` and `git status --short` before handoff.
