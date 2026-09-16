# Presentation execution and delivery

The bundled public `pptx` Skill owns mode selection, semantic routing, stage
handoff and delivery instructions. Agent and ACP have no PPT runtime, intent
classifier, domain lifecycle, task-event store or dedicated delivery tool.
Generic supplied `RunLifecyclePort` implementations remain supported. Ordinary
`request_user_decision` calls pause the turn; ACP places normalized host replies
in the regular user message. The Skill compares a reply with the actual pending
card and user conversation; model-authored host-looking text is not evidence.

Discovery keeps the actual source text separate from host wrappers and Plan
routing. Courseware can recall `pptx`; HTML, a chart or animation alone does not
make a presentation a dashboard. Discovery is a candidate hint, never a user's
mode selection.

## Methods and task continuity

`get_skill` adopts a method with `usage="use"` (default); optional documentation
uses `usage="reference"`. `replace=[old_names]` retires completed methods only
after a successful new read; `usage="release"` explicitly releases a method.
Only a truly independent task uses `new_task=True`. Replies, continued work,
edits, stage transitions and recovery retain the whole task and its corrections.
The public `pptx` method stays adopted alongside fast or design backends until
final artifact/receipt verification and delivery. Only finished internal stages
are replaced during handoff, so compaction restores the public delivery duties
even when the fast backend is unchanged. These are generic Skill contracts with
no PPT names in Context or Kernel.

The model follows the Skill to restore the original request, attachments, user
choices, corrections, page/format requirements, directory and finished stages
from available conversation, generic task context and actual work files. A
`task_pack.json` is work data; its defaults never establish user selection.
Unavailable or conflicting evidence requires clarification, not an inferred
static route. There is no hidden replacement classifier or Skill state machine.

Affirmative requests for movement during playback, change over time or controls
that change demonstrated content route courseware through Entry, Story and
Dazzle. Topic words such as dynamic programming, vivid style, comparisons,
negation and ordinary navigation do not. Explicit fast plus dynamic conflicts
need clarification; a current clear correction supersedes an earlier choice.
PowerPoint-native animation also needs a format discussion because Dazzle
produces animated HTML. This semantic interpretation is a Skill obligation;
source tests and scenario fixtures do not prove real-model compliance.

## Stateless formal finalization

Run the public Skill's copied `scripts/finalize.py` with explicit `--workspace`,
`--deck-dir`, `--requirements` and `--task-pack`. `requirements.json` contains
`mode`, `output`, `required_formats`, `expected_pages` (positive integer or null)
and `revision`. The task pack preserves authoring data and supplies the same
absolute `deck_dir`, `choices.output`, and design `ppt_mode`/`choices.static_postprocess`.
See `box_agent/skills/pptx/SKILL.md` for a complete command and JSON example.
Both files are model-authored work data, not permission or proof of user choice.
The CLI checks consistency, actual resources and outputs; it does not interpret
conversation or create another task-state authority.

| Route | Required default output | Operation |
| --- | --- | --- |
| Fast | Existing HTML; preserve explicit PPTX request | Verify original finalizer/QA evidence and required existing files |
| Static design | `present.html` and PPTX | Standard build, audit, original exporter and real package inspection |
| Explicit static HTML only | `present.html` | Standard build and audit |
| Dynamic design | `deck.html` | Verify current whole-deck render manifest and local PNG coverage |

The support modules live beside the CLI, and resolve the existing Standard and
Dazzle scripts from the sibling bundled Skills. They do not import `box_agent`.
Python/Pillow/psutil, Node, browser and exporter dependencies come from the managed
shell environment. The CLI preserves managed browser variables and disables
automatic exporter installs. Existing renderer ownership, bounded process cleanup,
font handling, SVG/OPC media validation and artifact publication are retained.
Normal shell permissions govern invocation; the removed tool has no separate
permission or runtime-state bypass.

On POSIX, the CLI passes a parent-liveness pipe to an invocation-scoped helper
that reuses the existing renderer supervisor, worker and browser ownership
protocol. This helper survives the outer shell's short kill grace long enough
to observe EOF and reap registered command/browser groups, including after
CLI SIGKILL; it exits with the invocation and is not a background service.
Exporter browser launches join that same existing browser-guard protocol.
CLI SIGTERM/SIGINT cancel the pending command and publish a final receipt.

JSON stdout and `_trace/finalize-receipt.json` report status, concrete artifacts,
input/output hashes, page coverage, steps and warnings. Exit 0 means complete
technical checks; failure/partial exits 1. Required files and work-data changes
invalidate old evidence and require another invocation. Fast receipts bind deck,
HTML and QA hashes; dynamic evidence binds current HTML and whole-deck PNGs.
Terminal recovery reports take precedence over older normal-finalizer receipts;
missing or stale HTML bindings cannot revive an earlier success. Capacity failures
may report the safely retained HTML as a partial artifact, never complete delivery
of the current content. Nonterminal design acceptance keeps the normal finalizer path.
Missing Review files or changes during build remain disclosed. Technical checks
do not prove visual or factual quality; the model still reviews and reports it.
Before reading inputs or starting commands, the CLI replaces old success with
`in_progress`. A hard kill may leave this nonterminal receipt; it never proves
completion, and requires a fresh invocation. Relative `--workspace` resolves
against shell cwd; relative `--deck-dir` resolves inside that workspace.
The published Skill command uses absolute paths for every task input.

The Skill tells the model to run formal finalization and provide actual links.
The host no longer auto-runs PPT finalization or overrides a final answer with
PPT-specific state. Missing artifacts require the model to continue the selected
workflow or disclose partial delivery, preserving the original files and formats.

## Verification boundaries

Tests cover no automatic PPT lifecycle/classifier, the real builtin reader and
ToolEngine with scripted model decisions, normal ACP reply messages, explicit
generic lifecycle preservation, and the stateless formal/artifact functions.
A copied Skill CLI runs in Python without an installed `box_agent` package, using
real local HTML/PPTX files and a fixture exporter. That proves the standalone
interface and validation, not real browser or model production quality.

The semantic regression corpus is `tests/fixtures/presentation_workflow_cases.json`;
Skill pressure checks cover dynamic continuation, standalone finishing and
reference-only reads. Packaging, installed client behavior, host restart and a
fresh real-model PPT delivery require separate verification.
