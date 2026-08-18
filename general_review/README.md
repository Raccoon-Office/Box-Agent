# Box-Agent automated review profile

This directory is the repository-owned contract consumed by the generic
`teamwork_review_agents` service. The service implements scanning, event
detection, preflight execution, locking, agent orchestration, and audit logs;
Box-Agent owns everything that is specific to this repository:

- `review.config.yaml`: repository, CI, agent, and trigger configuration;
- `ci/preflight.sh`: the deterministic CI gate;
- repository documentation referenced by the generic review roles.

The reusable review prompt is maintained only as
`teamwork_review_agents/prompts/general-review.md`; do not copy it into this
repository. `review.config.yaml` resolves it through the required
`TEAMWORK_REVIEW_AGENTS_ROOT` service environment variable. The generic Prompt
is consumed unchanged; this profile does not define a second Prompt mode.

Do not put credentials or generated runtime state in this directory. Provider
credentials come from the service environment. SQLite data and logs are written
under the ignored `general_review/runtime/` directory.

## Local validation

Run the generic service from its own checkout and pass this profile explicitly:

```bash
export TEAMWORK_REVIEW_AGENTS_ROOT=/mnt/d/code/teamwork_review_agents
teamwork-review-agents validate \
  -c /mnt/d/code/Box-Agent/general_review/review.config.yaml
```

Validation and startup intentionally fail if `TEAMWORK_REVIEW_AGENTS_ROOT` is
missing or points at a checkout that does not contain the generic prompt pack.
The service also requires a WSL/Linux-native `codex` executable; a Windows npm
shim exposed through `/mnt/c` is not a valid runtime for Linux agents.

Preflight and the Review Agents are currently intended only for pull requests
from trusted internal contributors. A temporary worktree and filtered
environment variables are not a security boundary for executing untrusted code.
