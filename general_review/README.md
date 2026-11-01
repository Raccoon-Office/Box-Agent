# Box-Agent review assets

This directory contains repository-owned executable assets used by review and
CI infrastructure. It does not contain a deployment configuration for
`teamwork_review_agents`.

- `ci/preflight.sh` is the deterministic Box-Agent CI command.
- `vote/` is the optional G2 vote review: parallel read-only hunters,
  independent verifier votes, and a deterministic tally. See
  `vote/README.md`.
- `../docs/PR_REVIEW_STANDARD.md` and
  `../docs/PR_REVIEW_STANDARD_CN.md` define the repository-specific review
  standard.

Run the CI command from the repository root:

```bash
bash general_review/ci/preflight.sh
```

Provider credentials, Agent prompts, scanner rules, SQLite data, logs, and
other deployment state belong to the external `teamwork_review_agents`
installation and must not be committed here.
