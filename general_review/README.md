# Box-Agent review assets

This directory contains repository-owned executable assets used by review and
CI infrastructure. It does not contain a deployment configuration for
`teamwork_review_agents`.

- `ci/preflight.sh` is the deterministic Box-Agent CI command.
- `../docs/PR_REVIEW_STANDARD.md` and
  `../docs/PR_REVIEW_STANDARD_CN.md` define the repository-specific review
  standard.

Run the CI command from the repository root:

```bash
bash general_review/ci/preflight.sh
```

For only the full test suite, use the same entry point as GitHub CI:

```bash
bash general_review/ci/test.sh
```

The full suite uses pytest-xdist CPU autodetection with at most four workers.
Tests in each file stay on one worker; different files can run concurrently.
The runner prints the 20 slowest phases and preserves the existing unreachable
MCP timeout exclusion. The install, compile, test and build gates still stop on
the first failure. To debug the full suite serially with the same selection:

```bash
PYTEST_XDIST_AUTO_NUM_WORKERS=0 bash general_review/ci/test.sh
```

Keep targeted iteration on direct pytest commands, which stay serial by default:

```bash
uv run pytest tests/test_web_extract.py -q
```

On the same macOS ARM64 checkout, the serial baseline was 576.17 seconds and the
four-worker run was 180.83 seconds, both with 9,238 passed and 305 skipped. These
are local measurements; platform/dependency skips and host load affect results.
The shared entry was then rerun in 173.99 seconds with 9,245 passed and the same
305 skipped, including seven new runner contract tests.
ASR packet test fixtures also fix gzip timestamps, so worker collection remains
identical across different wall-clock times; the test selection stays intact.

Provider credentials, Agent prompts, scanner rules, SQLite data, logs, and
other deployment state belong to the external `teamwork_review_agents`
installation and must not be committed here.
