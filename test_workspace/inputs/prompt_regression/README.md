# Prompt regression evaluation

Run cases through `test_workspace/quality_baseline.py run --pack
test_workspace/inputs/prompt_regression --catalog-model raccoonwork-auto --case-id CASE`.
Use `compare --pack ... --baseline RUN... --candidate RUN...` for repeated runs.
Real model runs consume quota; start with the case affected by a change.

Comparison requires matching model configuration, runner and oracle hashes, case
pack bytes, Python/platform, profile configuration and captured environment.
Agent source may differ. Captures without the new runner fingerprint are retained
as historical evidence but cannot supply a verified comparison context.

Exit codes are 0 for comparable cases without regressions, 1 for a lower verified
pass rate, and 2 for an inconclusive case or incompatible/missing context. A side
with no verified attempts is inconclusive, not a zero-percent pass rate.
Provider failures do not excuse forbidden tool actions already captured. Missing
final output after a provider failure remains unverified. Before scoring, fixture
bytes (including workspace-only and dirty files) must match the capture. Inputs
are preserved unless explicitly listed in `mutable_inputs`.

The JSONL oracle preserves the selected record text, allowing a final newline
but rejecting reformatting of JSON. The image oracle requires a valid nonempty `cat.png`. The arithmetic fixture is
checked structurally: `add(a, b)` must return `a + b`, and `multiply(a, b)` must
return `a * b`, with no extra executable module statements or defaults,
annotations, or decorators on any module function. This deliberately
narrow oracle does not execute model-generated Python and may reject equivalent
rewrites outside the requested minimal bug fix.

The initial six cases mostly pass on main. They detect regressions but do not
demonstrate an improvement. Infrastructure errors and small sample counts must
remain visible in any reported conclusion.
