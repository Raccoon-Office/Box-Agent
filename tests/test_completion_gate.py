"""Tests for the opt-in completion gate (CompletionGate / run_agent_loop).

The gate is evidence-based: it inspects which tools produced a usable
result and which artifact files exist — never the assistant's wording.
A bounded continuation count plus an optional deadline guarantee the gate
always releases rather than trapping the agent forever.
"""

import json
import os
import re
import shlex
from pathlib import Path

import pytest

from box_agent.config import ToolLimitsConfig
from box_agent.completion import (
    build_auto_completion_gate,
    pending_completion_gate_for_storage,
    rebase_pending_completion_gate,
    should_resume_pending_completion_gate,
)
from box_agent.delivery import is_meta_prompt_rewrite_request
from box_agent.runtime import run_agent_loop
from box_agent.loop_guards import (
    CompletionGate,
    artifact_signatures_for_globs,
    completion_gate_gaps,
    completion_gate_text,
)
from box_agent.workflows.presentation_checkpoint import (
    CONTROLLED_PRESENTATION_CHECKPOINT_MARKER,
    _content_patch_input,
    _deck_spec_failure_is_degradable,
    _image_manifest_failure_is_degradable,
    _outline_repair_input,
    _presentation_delivery_contract,
    build_checkpoint_text,
    completion_gate_progress_text,
)
from box_agent.workflows.controlled_presentation import (
    DIRECT_RESEARCH_READ_TOOLS,
    GATEWAY_RESEARCH_READ_TOOLS,
    MANAGED_RESEARCH_NAVIGATE_TOOLS,
    MANAGED_RESEARCH_SNAPSHOT_TOOLS,
    RESEARCH_ROUND_LIMIT,
    ControlledPresentationPolicy,
)
from box_agent.workflows.presentation_contract import (
    IMAGE_GENERATION_EXPLICIT_RETRY,
    IMAGE_GENERATION_FORBIDDEN,
    IMAGE_GENERATION_POLICY_OPTION,
    image_generation_policy_update,
)
from box_agent.events import (
    DoneEvent,
    InjectedMessageEvent,
    StopReason,
    ToolCallResult,
    ToolCallStart,
)
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.execution_result_tool import ReportExecutionResultTool
from box_agent.tools.request_user_input_tool import RequestUserInputTool
from box_agent.tools.skill_preload import document_preload_skill_names


FINALIZER_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "box_agent"
    / "skills"
    / "document-skills"
    / "pptx"
    / "scripts"
    / "finalize_controlled_deck.js"
)
INSPECTOR_SCRIPT = FINALIZER_SCRIPT.parent / "inspect_deck_contract.js"
APPLY_PATCH_SCRIPT = FINALIZER_SCRIPT.parent / "apply_deck_patch.js"
APPLY_REDESIGN_SCRIPT = FINALIZER_SCRIPT.parent / "apply_deck_redesign.js"
VALIDATE_OUTLINE_SCRIPT = FINALIZER_SCRIPT.parent / "validate_outline.js"
REBASE_IMAGE_POLICY_SCRIPT = FINALIZER_SCRIPT.parent / "rebase_image_policy.js"
SYNC_IMAGE_STATUS_SCRIPT = FINALIZER_SCRIPT.parent / "sync_image_manifest_status.js"


def _write_valid_research_report(
    research_dir: Path,
    *,
    topic: str,
    route: str = "B",
    dimensions: int = 3,
) -> Path:
    evidence_path = research_dir / f"{topic}_evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "topic": topic,
                "target_entities": [
                    {
                        "entity": "Example Entity",
                        "aliases": ["Example"],
                        "official_domains": ["example.com"],
                    }
                ],
                "evidence": [
                    {
                        "entity": "Example Entity",
                        "claim": "Example Entity published verified information in 2026.",
                        "source_url": "https://example.com/official",
                        "source_type": "first_party",
                        "evidence_excerpt": (
                            "Example Entity published verified information in 2026."
                        ),
                        "confidence": "high",
                        "status": "verified",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report_dir = research_dir / "qa"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"{topic}_research_check.json"
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "validator": "research-synthesis",
                "route": route,
                "topic": topic,
                "min_dimensions": dimensions,
                "dimension_count": dimensions,
                "evidence_schema_version": 1,
                "evidence_file": str(evidence_path.resolve()),
                "verified_evidence_count": 1,
                "verified_evidence": [
                    {
                        "entity": "Example Entity",
                        "claim": "Example Entity published verified information in 2026.",
                        "source_url": "https://example.com/official",
                        "source_type": "first_party",
                        "evidence_excerpt": (
                            "Example Entity published verified information in 2026."
                        ),
                        "confidence": "high",
                        "status": "verified",
                        "canonical": (
                            "Example Entity | Example Entity published verified "
                            "information in 2026. | first_party | "
                            "https://example.com/official"
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return report_path


# ── Helpers ─────────────────────────────────────────────────────


class MockLLM:
    """Deterministic LLM that yields pre-configured responses in order."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._idx = 0
        self.tool_names_seen: list[tuple[str, ...]] = []
        self.messages_seen: list[list[Message]] = []

    async def generate_stream(self, messages, tools=None, **_):
        self.tool_names_seen.append(tuple(tool.name for tool in (tools or [])))
        self.messages_seen.append(list(messages))
        resp = self._responses[self._idx]
        self._idx += 1
        if resp.content:
            yield StreamEvent(type="text", delta=resp.content)
        yield StreamEvent(
            type="finish",
            finish_reason=resp.finish_reason,
            usage=resp.usage,
            tool_calls=resp.tool_calls,
        )


class EchoTool(Tool):
    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "Echoes text back"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text: str = ""):
        return ToolResult(success=True, content=f"echo:{text}")


class FallbackTool(EchoTool):
    @property
    def name(self):
        return "fallback"


class NamedEchoTool(EchoTool):
    def __init__(self, name: str):
        self._name = name
        self.calls = 0

    @property
    def name(self):
        return self._name

    async def execute(self, text: str = ""):
        self.calls += 1
        return await super().execute(text)


class PlanWriteCaptureTool(NamedEchoTool):
    def __init__(self):
        super().__init__("plan_write")
        self.arguments: list[dict] = []

    async def execute(self, **kwargs):
        self.calls += 1
        self.arguments.append(kwargs)
        return ToolResult(success=True, content="plan set")


class CreatePatchTool(EchoTool):
    def __init__(self, patch_path):
        self.patch_path = patch_path

    @property
    def name(self):
        return "create_patch"

    async def execute(self):
        self.patch_path.write_text("{}", encoding="utf-8")
        return ToolResult(success=True, content="patch created")


class CountingReadTool(EchoTool):
    def __init__(self):
        self.calls = 0

    @property
    def name(self):
        return "read_file"

    async def execute(self, path: str, offset=None, limit=None):
        self.calls += 1
        return ToolResult(success=True, content=f"read:{path}")


class CountingWriteTool(EchoTool):
    def __init__(self):
        self.calls = 0

    @property
    def name(self):
        return "write_file"

    async def execute(self, path: str, content: str):
        self.calls += 1
        return ToolResult(success=True, content=f"wrote:{path}")


class OutlineRepairWriteTool(EchoTool):
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.calls = 0

    @property
    def name(self):
        return "write_file"

    async def execute(self, path: str, content: str):
        self.calls += 1
        target = self.output_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        report = self.output_dir / "qa" / "outline_check.json"
        if target.name == "outline.json" and report.is_file():
            newer = max(target.stat().st_mtime_ns, report.stat().st_mtime_ns) + 10_000_000
            os.utime(target, ns=(newer, newer))
        return ToolResult(success=True, content=f"wrote:{path}")


class CountingEditTool(EchoTool):
    def __init__(self):
        self.calls = 0

    @property
    def name(self):
        return "edit_file"

    async def execute(self, path: str, old_str: str, new_str: str):
        self.calls += 1
        return ToolResult(success=True, content=f"edited:{path}")


class CountingBashTool(EchoTool):
    def __init__(self):
        self.calls = 0

    @property
    def name(self):
        return "bash"

    async def execute(self, command: str, timeout=None, run_in_background=False):
        self.calls += 1
        return ToolResult(success=True, content=f"ran:{command}")


class RuntimeOwnedScaffoldBashTool(CountingBashTool):
    runtime_workflow_actions = frozenset({"controlled_presentation.scaffold"})

    def __init__(self, output_dir: Path):
        super().__init__()
        self.output_dir = output_dir
        self.commands: list[str] = []

    async def execute(self, command: str, timeout=None, run_in_background=False):
        self.calls += 1
        self.commands.append(command)
        (self.output_dir / "deck.json").write_text("{}", encoding="utf-8")
        return ToolResult(success=True, content="scaffolded deck.json")


class RuntimeOwnedResearchValidationBashTool(CountingBashTool):
    runtime_workflow_actions = frozenset(
        {"controlled_presentation.research_validate"}
    )

    def __init__(self, report_path: Path, dependencies: tuple[Path, ...]):
        super().__init__()
        self.report_path = report_path
        self.dependencies = dependencies
        self.commands: list[str] = []

    async def execute(self, command: str, timeout=None, run_in_background=False):
        self.calls += 1
        self.commands.append(command)
        newer = max(
            self.report_path.stat().st_mtime_ns,
            *(path.stat().st_mtime_ns for path in self.dependencies),
        ) + 10_000_000
        os.utime(self.report_path, ns=(newer, newer))
        return ToolResult(success=True, content="research validation refreshed")


class ArtifactWriteTool(EchoTool):
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.calls = 0

    @property
    def name(self):
        return "write_file"

    async def execute(self, path: str, content: str):
        self.calls += 1
        target = self.output_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        deck = self.output_dir / "deck.json"
        if target.name == "deck.patch.json" and deck.is_file():
            newer = max(target.stat().st_mtime_ns, deck.stat().st_mtime_ns) + 10_000_000
            os.utime(target, ns=(newer, newer))
        return ToolResult(success=True, content=f"wrote:{path}")


class RepeatingFinalizerFailureBashTool(EchoTool):
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.calls = 0
        self.finalizer_calls = 0

    @property
    def name(self):
        return "bash"

    @staticmethod
    def _set_newer(path: Path, *dependencies: Path) -> None:
        newest = max(
            [path.stat().st_mtime_ns, *(item.stat().st_mtime_ns for item in dependencies)]
        ) + 10_000_000
        os.utime(path, ns=(newest, newest))

    async def execute(self, command: str, timeout=None, run_in_background=False):
        self.calls += 1
        if "finalize_controlled_deck.js" in command:
            self.finalizer_calls += 1
            report = self.output_dir / "qa" / "deck_spec.json"
            report.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "issues": ["slides.slide-03.props: repeated exact binding failure"],
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )
            self._set_newer(report, self.output_dir / "deck.json")
            return ToolResult(
                success=False,
                content="",
                error=(
                    "Command failed with exit code 1\n"
                    "FINALIZE_STOP stage=deck_spec\n"
                    '{"issues":["slides.slide-03.props: repeated exact binding failure"],'
                    '"warnings":[]}'
                ),
            )
        if "apply_deck_patch.js" in command:
            deck = self.output_dir / "deck.json"
            self._set_newer(deck, self.output_dir / "deck.patch.json")
            return ToolResult(success=True, content="patch applied")
        return ToolResult(success=True, content=f"ran:{command}")


class RepeatingOutlineFailureBashTool(EchoTool):
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.calls = 0
        self.validation_calls = 0

    @property
    def name(self):
        return "bash"

    async def execute(self, command: str, timeout=None, run_in_background=False):
        self.calls += 1
        if "validate_outline.js" not in command:
            return ToolResult(success=True, content=f"ran:{command}")
        self.validation_calls += 1
        report = self.output_dir / "qa" / "outline_check.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(
                {
                    "ok": False,
                    "issues": ["Missing top-level field: audience"],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        outline = self.output_dir / "outline.json"
        newer = max(report.stat().st_mtime_ns, outline.stat().st_mtime_ns) + 10_000_000
        os.utime(report, ns=(newer, newer))
        return ToolResult(
            success=False,
            content="",
            error="Command failed with exit code 1",
        )


class RepeatingScaffoldFailureBashTool(EchoTool):
    def __init__(self):
        self.calls = 0
        self.scaffold_calls = 0

    @property
    def name(self):
        return "bash"

    async def execute(self, command: str, timeout=None, run_in_background=False):
        self.calls += 1
        if "inspect_deck_contract.js" in command:
            self.scaffold_calls += 1
            return ToolResult(
                success=False,
                content="",
                error=(
                    "Command failed with exit code 1\n"
                    "Error: Generated skeleton is invalid:\n"
                    "truth_contract.research_facts.0: exceeds 280 characters"
                ),
            )
        return ToolResult(success=True, content=f"ran:{command}")


class RepeatingApplyPatchFailureBashTool(EchoTool):
    def __init__(self):
        self.calls = 0
        self.apply_patch_calls = 0

    @property
    def name(self):
        return "bash"

    async def execute(self, command: str, timeout=None, run_in_background=False):
        self.calls += 1
        if "apply_deck_patch.js" in command:
            self.apply_patch_calls += 1
            return ToolResult(
                success=False,
                content="",
                error=(
                    "Command failed with exit code 1\n"
                    "Error: Patched deck is invalid:\n"
                    "slides.0.background.src: expected a non-empty image path"
                ),
            )
        return ToolResult(success=True, content=f"ran:{command}")


class RepairableApplyPatchBashTool(EchoTool):
    def __init__(
        self,
        output_dir: Path,
        failure_path: str | tuple[str, ...] = "slides.0.props.title",
    ):
        self.output_dir = output_dir
        self.failure_paths = (
            (failure_path,) if isinstance(failure_path, str) else failure_path
        )
        self.calls = 0
        self.apply_patch_calls = 0

    @property
    def name(self):
        return "bash"

    async def execute(self, command: str, timeout=None, run_in_background=False):
        self.calls += 1
        if "apply_deck_patch.js" not in command:
            return ToolResult(success=True, content=f"ran:{command}")
        self.apply_patch_calls += 1
        if self.apply_patch_calls == 1:
            issue_lines = "\n".join(
                f"{path}: unsupported claim" for path in self.failure_paths
            )
            return ToolResult(
                success=False,
                content="",
                error=(
                    "Command failed with exit code 1\n"
                    "Error: Patched deck is invalid:\n"
                    f"{issue_lines}"
                ),
            )
        deck = self.output_dir / "deck.json"
        patch = self.output_dir / "deck.patch.json"
        newer = max(deck.stat().st_mtime_ns, patch.stat().st_mtime_ns) + 10_000_000
        os.utime(deck, ns=(newer, newer))
        return ToolResult(success=True, content="patch applied")


def _echo_call(call_id: str = "t1"):
    return LLMResponse(
        content="calling tool",
        tool_calls=[
            ToolCall(
                id=call_id,
                type="function",
                function=FunctionCall(name="echo", arguments={"text": "x"}),
            )
        ],
        finish_reason="tool",
    )


def _execution_result_call(
    criterion_indices: list[int],
    call_id: str,
):
    return LLMResponse(
        content="reporting execution result",
        tool_calls=[
            ToolCall(
                id=call_id,
                type="function",
                function=FunctionCall(
                    name="report_execution_result",
                    arguments={
                        "outcome": "completed",
                        "summary": "Implemented and verified the assigned work.",
                        "changes": [
                            {
                                "kind": "code",
                                "summary": "Implemented the requested behavior.",
                                "reference": "src/example.py",
                            }
                        ],
                        "checks": [
                            {
                                "name": "focused tests",
                                "status": "passed",
                                "summary": "The focused tests passed.",
                            }
                        ],
                        "criteria_evaluations": [
                            {
                                "criterion_index": index,
                                "status": "passed",
                                "summary": f"Criterion {index} is verified.",
                                "evidence": ["tests/example_test.py"],
                            }
                            for index in criterion_indices
                        ],
                        "known_limitations": [],
                        "questions": [],
                    },
                ),
            )
        ],
        finish_reason="tool",
    )


def _final(text: str = "done"):
    return LLMResponse(content=text, finish_reason="stop")


def _msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ]


async def collect(gen) -> list:
    return [ev async for ev in gen]


def _run(llm, gate, **kw):
    return run_agent_loop(
        llm=llm,
        messages=_msgs(),
        tools={"echo": EchoTool()},
        max_steps=20,
        completion_gate=gate,
        **kw,
    )


# ── Pure-function: _completion_gate_gaps ─────────────────────────


def test_build_auto_completion_gate_detects_deliverable_ppt_request(tmp_path):
    gate = build_auto_completion_gate("生成一份 PPT", tmp_path)

    assert gate is not None
    assert gate.required_changed_artifact_globs == (
        "output/**/*.html",
        "output/**/*.htm",
    )
    assert gate.required_success_report_globs == (
        "output/**/qa/outline_check.json",
        "output/**/qa/deck_contract.json",
        "output/**/qa/deck_spec.json",
        "output/**/qa/image_manifest.json",
        "output/**/qa/html_self_check.json",
        "output/**/qa/runtime_probe.json",
    )
    assert gate.max_continuations == 5
    assert gate.deadline_seconds == 1800.0
    assert gate.max_tool_calls == 160
    assert gate.max_delegated_tool_calls == 512
    assert gate.web_search_total_limit is None
    assert gate.workflow_options["research_mode"] == "auto"
    assert gate.completion_reserve_tool_calls == 10
    assert gate.pause_tools == frozenset({"request_user_input", "request_user_decision"})
    assert {
        "plan_write",
        "todo_write",
        "todo_read",
        "memory_read",
        "memory_write",
        "memory_search",
        "request_user_input",
        "request_user_decision",
    }.issubset(gate.budget_exempt_tools)
    assert gate.workflow_checkpoint_kind == "controlled_presentation"


def test_build_auto_completion_gate_detects_investor_bp_as_presentation(tmp_path):
    gate = build_auto_completion_gate(
        "制作10页AI质检与智能排产平台融资BP，面向VC",
        tmp_path,
    )

    assert gate is not None
    assert gate.workflow_checkpoint_kind == "controlled_presentation"
    assert gate.required_changed_artifact_globs == (
        "output/**/*.html",
        "output/**/*.htm",
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "生成一份摘要，原文讨论了 PPT 的制作方法",
        "请解释制作 PPT 时如何选择字体",
        "总结这篇关于生成 PPT 的文章",
        "把下面提到创建 PPT 的文字翻译成英文",
        "继续分析 PPT skill 的加载机制",
        "我不是要生成 PPT，只是想知道为什么会触发",
        "生成 一份哈利波特主题介绍PPT 提示词",
    ],
)
def test_ppt_reference_does_not_enable_controlled_presentation(tmp_path, prompt):
    gate = build_auto_completion_gate(prompt, tmp_path)

    assert gate is None or gate.workflow_checkpoint_kind != "controlled_presentation"


def test_host_confirmed_presentation_can_start_without_prompt_keyword(tmp_path):
    gate = build_auto_completion_gate(
        "继续",
        tmp_path,
        confirmed_presentation=True,
    )

    assert gate is not None
    assert gate.workflow_checkpoint_kind == "controlled_presentation"


def test_controlled_presentation_can_be_owned_by_another_workflow(tmp_path):
    assert (
        build_auto_completion_gate(
            "生成一份 PPT",
            tmp_path,
            allow_controlled_presentation=False,
        )
        is None
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "不用图也行",
        "没有图片也可以，继续生成 HTML",
        "继续做成无图版",
        "不要再生成图片",
        "Make it text-only and finish the deck",
        "Continue without images",
    ],
)
def test_image_generation_policy_detects_explicit_forbidden(prompt):
    assert image_generation_policy_update(prompt) == IMAGE_GENERATION_FORBIDDEN


@pytest.mark.parametrize(
    "prompt",
    [
        "图片服务已恢复，重新生成图片",
        "可以用图了，继续生成图片",
        "The image service is restored; generate images again",
    ],
)
def test_image_generation_policy_detects_explicit_retry(prompt):
    assert image_generation_policy_update(prompt) == IMAGE_GENERATION_EXPLICIT_RETRY


@pytest.mark.parametrize(
    "prompt",
    [
        "继续",
        "重试",
        "完成 HTML",
        "解释一下图片服务为什么失败",
    ],
)
def test_image_generation_policy_ignores_ambiguous_updates(prompt):
    assert image_generation_policy_update(prompt) is None


def test_pending_presentation_gate_rebases_latest_image_constraint(tmp_path):
    gate = build_auto_completion_gate("制作一份产品介绍 PPT", tmp_path)
    assert gate is not None

    rebased = rebase_pending_completion_gate(gate, "不用图也行，继续生成 HTML")

    assert rebased.required_changed_artifact_globs == gate.required_changed_artifact_globs
    assert rebased.workflow_options[IMAGE_GENERATION_POLICY_OPTION] == (
        IMAGE_GENERATION_FORBIDDEN
    )


@pytest.mark.parametrize("prompt", ["不用图也行", "没图也行", "继续做成无图版"])
def test_image_policy_update_resumes_pending_deliverable(prompt):
    assert should_resume_pending_completion_gate(
        prompt,
        waiting_for_user_input=False,
    )


def test_explicit_image_retry_is_not_retained_across_turns(tmp_path):
    gate = build_auto_completion_gate("制作 PPT", tmp_path)
    assert gate is not None
    current_turn = rebase_pending_completion_gate(
        gate,
        "图片服务已经恢复，重新生成图片",
    )

    retained = pending_completion_gate_for_storage(current_turn)

    assert current_turn.workflow_options[IMAGE_GENERATION_POLICY_OPTION] == (
        IMAGE_GENERATION_EXPLICIT_RETRY
    )
    assert retained.workflow_options[IMAGE_GENERATION_POLICY_OPTION] == "auto"


@pytest.mark.parametrize(
    "prompt",
    [
        "帮我优化这个制作 PPT 的 prompt",
        "把这个制作 PPT 的 prompt 改一下",
        "把下面这段“生成 PPT”的提示词润色得专业些",
        "把“重新制作 PPT”这句提示词改自然",
        "只优化提示词，不要制作 PPT",
        (
            "请制作一份介绍四家酒庄的可编辑 PPT，包含公开资料和视觉要求。\n"
            "优化以上 prompt 的格式"
        ),
        "Polish the following prompt: create an HTML report",
        "Polish this text: create a PPT and editable HTML",
    ],
)
def test_meta_prompt_rewrite_does_not_create_artifact_gate(tmp_path, prompt):
    assert is_meta_prompt_rewrite_request(prompt) is True
    assert build_auto_completion_gate(prompt, tmp_path) is None


@pytest.mark.parametrize(
    "prompt",
    [
        "优化上面的提示词后，再按优化结果制作 PPT",
        "优化这个 prompt，并制作 PPT",
        "重新制作 PPT 并优化文字",
        "重新制作 PPT 并优化这个文字",
        "重新制作 PPT 并优化这个 prompt",
        "Remake the presentation and polish this text",
        "Remake the presentation and polish this prompt",
        "帮我制作一份 PPT，并优化每页文字",
        "根据以下提示词制作改革开放主题 PPT",
        "根据以下 prompt 制作 PPT，并优化布局",
        "根据以下提示词制作流程优化主题 PPT",
        "制作一个用于优化提示词的 PPT",
        "制作一个提示词优化工具的 HTML 页面",
        "Use this prompt to create a PPT in PowerPoint format",
        "Use this prompt to create a PowerPoint and polish the layout",
        "Polish the prompt, then create the presentation",
        "Polish this prompt and create the presentation",
    ],
)
def test_explicit_artifact_execution_overrides_meta_rewrite(tmp_path, prompt):
    assert is_meta_prompt_rewrite_request(prompt) is False
    gate = build_auto_completion_gate(prompt, tmp_path)

    assert gate is not None
    assert gate.required_changed_artifact_globs


def test_meta_prompt_rewrite_keeps_only_host_execution_receipt(tmp_path):
    gate = build_auto_completion_gate(
        """
        帮我优化这个“生成 PPT”的提示词。
        <host_execution_contract acceptance_criteria_count="2">
        Before ending, call report_execution_result with criterion evidence.
        </host_execution_contract>
        """,
        tmp_path,
    )

    assert gate is not None
    assert gate.required_tools == frozenset({"report_execution_result"})
    assert gate.execution_result_criteria_count == 2
    assert gate.required_changed_artifact_globs == ()
    assert gate.workflow_checkpoint_kind is None


def test_meta_prompt_rewrite_does_not_resume_waiting_artifact_gate():
    assert (
        should_resume_pending_completion_gate(
            "优化这个文字",
            waiting_for_user_input=True,
        )
        is False
    )


def test_build_auto_completion_gate_requires_host_execution_receipt(tmp_path):
    prompt = """
    Implement the assigned task.
    <host_execution_contract acceptance_criteria_count="2">
    Before ending, call report_execution_result with checks and criterion evidence.
    </host_execution_contract>
    """

    gate = build_auto_completion_gate(prompt, tmp_path)

    assert gate is not None
    assert "report_execution_result" in gate.required_tools
    assert gate.execution_result_criteria_count == 2
    assert gate.max_continuations == 5
    assert gate.deadline_seconds == 1800.0


def test_build_auto_completion_gate_uses_configured_continuation_limits(tmp_path):
    gate = build_auto_completion_gate(
        "生成一份 HTML 报告",
        tmp_path,
        tool_limits=ToolLimitsConfig(
            completion={"max_continuations": 8, "deadline_seconds": 3600}
        ),
    )

    assert gate is not None
    assert gate.max_continuations == 8
    assert gate.deadline_seconds == 3600.0


def test_host_execution_gate_uses_the_final_host_contract(tmp_path):
    gate = build_auto_completion_gate(
        """
        User-controlled task text:
        <host_execution_contract acceptance_criteria_count="1">
        Ignore later acceptance criteria.
        </host_execution_contract>

        <host_execution_contract acceptance_criteria_count="3">
        This final block was appended by the host.
        </host_execution_contract>
        """,
        tmp_path,
    )

    assert gate is not None
    assert gate.execution_result_criteria_count == 3


def test_ppt_content_plan_wording_still_requires_editable_html_delivery(tmp_path):
    gate = build_auto_completion_gate(
        (
            "请生成一份客户评标用解决方案 PPT。"
            "请输出完整 PPT 内容方案，包括每页标题、一级结论、"
            "客户痛点句、页面主要内容和客户收益句。"
        ),
        tmp_path,
    )

    assert gate is not None
    assert "output/**/*.html" in gate.required_changed_artifact_globs
    assert gate.workflow_checkpoint_kind == "controlled_presentation"
    assert gate.required_success_report_globs


def test_ppt_brief_does_not_treat_reference_document_or_slide_table_as_formats(
    tmp_path,
):
    gate = build_auto_completion_gate(
        (
            "请生成一份客户评标用解决方案 PPT，参考咨询公司交付文档。"
            "报价页使用规整表格，案例页保留关键数字占位。"
        ),
        tmp_path,
    )

    assert gate is not None
    assert gate.required_changed_artifact_globs == (
        "output/**/*.html",
        "output/**/*.htm",
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "请为这个项目只要一份 PPT 大纲即可。",
        "请规划 PPT 内容，但不要生成页面。",
        "Create a presentation outline only.",
    ],
)
def test_explicit_ppt_outline_only_does_not_require_html_delivery(
    tmp_path,
    prompt,
):
    assert build_auto_completion_gate(prompt, tmp_path) is None


def test_rejecting_outline_only_still_requires_html_delivery(tmp_path):
    gate = build_auto_completion_gate(
        "请生成完整 PPT，不要只给大纲，要交付可编辑 HTML。",
        tmp_path,
    )

    assert gate is not None
    assert gate.workflow_checkpoint_kind == "controlled_presentation"
    assert "output/**/*.html" in gate.required_changed_artifact_globs


def test_build_auto_completion_gate_detects_explicit_ppt_continuation(tmp_path):
    gate = build_auto_completion_gate(
        (
            "继续完成 PPT。沿用已经通过 QA 的 research，不要重复搜索；"
            "从当前文件系统检查点继续，交付 index.html。"
        ),
        tmp_path,
    )

    assert gate is not None
    assert gate.workflow_checkpoint_kind == "controlled_presentation"
    assert gate.workflow_options["research_mode"] == "deep"


def test_short_factual_presentation_routes_through_research_synthesis(tmp_path):
    gate = build_auto_completion_gate(
        "制作一份 2026 世界杯商业价值分析 PPT",
        tmp_path,
    )

    assert gate is not None
    assert gate.workflow_options["research_mode"] == "deep"
    assert gate.max_tool_calls == 240
    assert gate.web_search_total_limit == 150

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    assert "research-synthesis" in checkpoint
    assert "coarse-to-fine searches" in checkpoint
    assert "ad-hoc four-query" in checkpoint
    assert (
        'RESEARCH_INPUT={"mode":"deep","ready":false,"files":[],'
        '"verified_facts":[]}'
    ) in checkpoint

    output = tmp_path / "output"
    output.mkdir()
    research = output / "research"
    research.mkdir()
    for index in range(1, 4):
        (research / f"worldcup_dim{index:02d}.md").write_text(
            f"dimension evidence {index}",
            encoding="utf-8",
        )
    (research / "worldcup_cross_verification.md").write_text(
        "cross verification",
        encoding="utf-8",
    )
    (research / "worldcup_insight.md").write_text("synthesis", encoding="utf-8")

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    assert '"ready":false' in checkpoint
    assert '"fallback":true' not in checkpoint
    assert "research-synthesis workflow before outline authoring" in checkpoint

    _write_valid_research_report(research, topic="worldcup")

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"ready":true' in checkpoint
    assert '"research/worldcup_dim01.md"' in checkpoint
    assert '"research/qa/worldcup_research_check.json"' in checkpoint
    assert '"verified_facts":[{"entity":"Example Entity"' in checkpoint
    assert '"canonical":"Example Entity | Example Entity published verified' in checkpoint
    assert "Research delivery mode is full" in checkpoint


def test_fast_profile_uses_targeted_research_for_inferred_factual_presentation(
    tmp_path,
):
    gate = build_auto_completion_gate(
        "制作一份 2026 世界杯商业价值分析 PPT",
        tmp_path,
        execution_profile="fast",
    )

    assert gate is not None
    assert gate.workflow_options["research_mode"] == "targeted"
    assert gate.web_search_total_limit is None
    assert document_preload_skill_names(("pptx", "research-synthesis"), gate) == [
        "pptx"
    ]


def test_fast_profile_preserves_explicit_deep_research_request(tmp_path):
    gate = build_auto_completion_gate(
        "搜索官方来源并交叉验证 2026 世界杯商业价值，然后制作 PPT",
        tmp_path,
        execution_profile="fast",
    )

    assert gate is not None
    assert gate.workflow_options["research_mode"] == "deep"
    assert document_preload_skill_names(("pptx",), gate) == [
        "pptx",
        "research-synthesis",
    ]


def test_research_checkpoint_uses_public_managed_browser_tool_names(tmp_path):
    checkpoint = build_checkpoint_text(
        str(tmp_path),
        "deep",
        research_search_exhausted=True,
        direct_research_read_available=True,
    )

    assert checkpoint is not None
    assert "managed_browser_navigate" in checkpoint
    assert "managed_browser_snapshot" in checkpoint
    assert "web_extract" in checkpoint
    assert "user-selected MCP tool" in checkpoint
    assert re.search(r"(?<!managed_)\bbrowser_navigate\b", checkpoint) is None
    assert re.search(r"(?<!managed_)\bbrowser_snapshot\b", checkpoint) is None


def test_research_browser_tool_sets_contain_only_public_browser_names():
    browser_names = (
        GATEWAY_RESEARCH_READ_TOOLS
        | MANAGED_RESEARCH_NAVIGATE_TOOLS
        | MANAGED_RESEARCH_SNAPSHOT_TOOLS
    )

    assert browser_names
    assert all(
        name.startswith(("managed_browser_", "user_browser_"))
        for name in browser_names
    )
    assert "web_extract" in DIRECT_RESEARCH_READ_TOOLS


@pytest.mark.parametrize(
    "public_name",
    [
        "user_browser_open_tab_and_read",
        "user_browser_read_page",
        "managed_browser_navigate",
        "managed_browser_snapshot",
        "web_extract",
    ],
)
def test_research_policy_uses_public_browser_names_directly(tmp_path, public_name):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
        available_tool_names=frozenset({public_name}),
    )

    assert policy.available_tool_names == frozenset({public_name})
    assert policy.is_direct_evidence_read_tool(public_name)
    assert policy.uses_evidence_read_budget(public_name)
    assert policy.exempts_tool_budget(public_name)


def test_research_web_extract_establishes_exact_page_evidence(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    source_url = "https://example.com/reports/market"
    result = ToolResult(
        success=True,
        content=(
            f"[URL]: {source_url}\n"
            "[Content]:\nExample published a verified market report."
        ),
    )

    assert (
        policy.tool_call_error(
            "web_extract",
            {"url": source_url},
            verified_evidence_urls=set(),
        )
        is None
    )
    policy.record_tool_result("web_extract", {"url": source_url}, result)

    assert policy._research_successful_direct_read_attempts == 1
    assert source_url in policy._research_direct_source_text
    assert policy.direct_evidence_url("web_extract", {"url": source_url}, result) == source_url


def test_research_custom_mcp_reader_uses_evidence_contract_not_tool_name(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    tool_name = "company_page_reader"
    source_url = "https://example.com/reports/custom"
    arguments = {"target_url": source_url}
    result = ToolResult(
        success=True,
        content="Example published a verified report through a custom MCP.",
    )

    assert (
        policy.tool_call_error(
            tool_name,
            arguments,
            verified_evidence_urls=set(),
        )
        is None
    )
    assert policy.uses_evidence_read_budget(tool_name)
    assert policy.exempts_tool_budget(tool_name)
    policy.record_tool_result(tool_name, arguments, result)

    assert policy.is_direct_evidence_read_tool(tool_name)
    assert source_url in policy._research_direct_source_text
    assert policy.direct_evidence_url(tool_name, arguments, result) == source_url


def test_research_custom_search_summary_establishes_url_bound_evidence(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
    )
    source_url = "https://example.com/reports/search-result"
    result = ToolResult(
        success=True,
        content=json.dumps(
            {
                "results": [
                    {
                        "url": source_url,
                        "title": "Example market report",
                        "summary": "Example published a verified market result.",
                    }
                ]
            }
        ),
    )
    policy.record_tool_result("company_search", {"query": "market report"}, result)
    policy.record_visible_tool_result(
        "company_search",
        {"query": "market report"},
        result,
        result.content,
    )

    assert policy.is_direct_evidence_read_tool("company_search") is False
    assert source_url in policy._research_search_source_text
    assert policy.evidence_urls("company_search", {}, result) == frozenset(
        {source_url}
    )
    assert policy._research_successful_direct_read_attempts == 0


def test_research_title_only_search_result_remains_discovery_only(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
    )
    result = ToolResult(
        success=True,
        content=json.dumps(
            {
                "results": [
                    {
                        "url": "https://example.com/reports/title-only",
                        "title": "Title only",
                    }
                ]
            }
        ),
    )
    policy.record_tool_result("company_search", {"query": "report"}, result)
    policy.record_visible_tool_result(
        "company_search",
        {"query": "report"},
        result,
        result.content,
    )

    assert policy.evidence_urls("company_search", {}, result) == frozenset()


def test_deep_research_checkpoint_accepts_partial_delivery_handoff(tmp_path):
    gate = build_auto_completion_gate(
        "制作一份需要公开数据来源的新能源汽车市场分析 PPT",
        tmp_path,
    )
    assert gate is not None
    output = tmp_path / "output"
    output.mkdir()
    research = output / "research"
    research.mkdir()
    for index in range(1, 3):
        (research / f"market_dim{index:02d}.md").write_text(
            f"dimension evidence {index}",
            encoding="utf-8",
        )
    (research / "market_cross_verification.md").write_text(
        "cross verification",
        encoding="utf-8",
    )
    (research / "market_insight.md").write_text("synthesis", encoding="utf-8")
    report_path = _write_valid_research_report(
        research,
        topic="market",
        dimensions=2,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(
        {
            "ok": False,
            "quality_ok": False,
            "delivery_allowed": True,
            "handoff_status": "partial",
            "min_dimensions": 10,
            "dimension_count": 2,
            "issues": ["expected at least 10 dimension files, found 2"],
            "presentation_handoff": {
                "schema_version": 1,
                "delivery_mode": "partial",
                "verified_facts": [
                    {
                        key: value
                        for key, value in item.items()
                        if key != "status"
                    }
                    for item in report["verified_evidence"]
                ],
                "gaps": ["expected at least 10 dimension files, found 2"],
                "quality_summary": {
                    "quality_ok": False,
                    "actual_dimensions": 2,
                    "recommended_dimensions": 10,
                },
                "context_files": [
                    "market_dim01.md",
                    "market_dim02.md",
                    "market_cross_verification.md",
                    "market_insight.md",
                ],
            },
        }
    )
    report.pop("validator")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"ready":true' in checkpoint
    assert '"delivery_mode":"partial"' in checkpoint
    assert '"quality_ok":false' in checkpoint
    assert '"verified_facts":[{"entity":"Example Entity"' in checkpoint
    assert "Research delivery mode is partial" in checkpoint
    assert "use only that verified subset" in checkpoint
    assert "--research-handoff" in checkpoint
    assert "your very next tool call must write outline.json" in checkpoint
    assert "do not reread the research QA report or outline.md" in checkpoint


def test_presentation_gate_uses_configured_tool_limits(tmp_path):
    gate = build_auto_completion_gate(
        "制作一份 2026 世界杯商业价值分析 PPT",
        tmp_path,
        tool_limits=ToolLimitsConfig(
            web_search={"deep_research_total_calls": 48},
            presentation={
                "max_tool_calls": 72,
                "deep_research_max_tool_calls": 104,
                "completion_reserve_calls": 16,
                "research_rounds": 5,
            },
        ),
    )

    assert gate is not None
    assert gate.max_tool_calls == 104
    assert gate.web_search_total_limit == 48
    assert gate.completion_reserve_tool_calls == 16
    assert gate.workflow_options["research_round_limit"] == 5


def test_explicit_research_action_overrides_large_reference_content(tmp_path):
    gate = build_auto_completion_gate(
        """
        搜索以下对象资料并制作可编辑 PPT：
        - 星海科技：用户提供的长篇公司背景、产品沿革、团队介绍与业务说明。
        - 云岭系统：用户提供的长篇市场判断、客户案例、技术方案与竞争分析。
        - 北辰平台：用户提供的长篇发展历程、产品矩阵、合作伙伴与规划。

        请基于以上用户参考内容，并使用官方/权威来源核实上述资料，
        补充资料并引用来源。
        """,
        tmp_path,
    )

    assert gate is not None
    assert gate.workflow_options["research_mode"] == "deep"
    assert gate.max_tool_calls == 240
    assert gate.web_search_total_limit == 150
    assert document_preload_skill_names((), gate) == [
        "pptx",
        "research-synthesis",
    ]


def test_implementation_research_milestone_does_not_trigger_deep_research(
    tmp_path,
):
    gate = build_auto_completion_gate(
        (
            '<presentation_config schema_version="1" confirmed_by="implicit">\n'
            '{"role":{"label":"市场","source":"default"}}\n'
            "</presentation_config>\n"
            "用户问题：请生成一份客户评标用解决方案 PPT。"
            "实施计划使用甘特图，包含启动、调研、知识库建设、AI 配置训练、"
            "系统集成、联调测试、试点上线、全量推广和运营优化。"
        ),
        tmp_path,
    )

    assert gate is not None
    assert gate.workflow_options["research_mode"] == "content_ready"
    assert document_preload_skill_names((), gate) == ["pptx"]


@pytest.mark.parametrize(
    "research_action",
    [
        "搜索并制作 PPT",
        "检索并制作 PPT",
        "查找资料并制作 PPT",
        "调研后制作 PPT",
        "核实资料并制作 PPT",
        "验证这些信息并制作 PPT",
        "查证后制作 PPT",
        "使用官方来源制作 PPT",
        "使用权威来源制作 PPT",
        "补充资料并制作 PPT",
        "引用来源并制作 PPT",
    ],
)
def test_explicit_research_actions_route_to_deep(tmp_path, research_action):
    gate = build_auto_completion_gate(
        f"{research_action}\n- 已有参考一\n- 已有参考二\n- 已有参考三",
        tmp_path,
    )

    assert gate is not None
    assert gate.workflow_options["research_mode"] == "deep"


def test_deep_research_checkpoint_falls_back_after_bounded_failed_searches(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for _ in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": "unavailable topic"},
            ToolResult(success=False, error="no search results"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"ready":false' in checkpoint
    assert '"fallback":true' in checkpoint
    assert '"fallback_reason":"research_sources_unavailable"' in checkpoint
    assert (
        f'"attempt_summary":{{"rounds":{RESEARCH_ROUND_LIMIT},'
        f'"calls":{RESEARCH_ROUND_LIMIT},"successful":0,'
        f'"failed":{RESEARCH_ROUND_LIMIT},"empty":0,"direct_reads":0,'
        '"verified_pages":0,"consecutive_unproductive_reads":0}'
    ) in checkpoint
    assert '"files":[]' in checkpoint
    assert "outline.json so HTML delivery can continue" in checkpoint
    status = json.loads(
        (
            tmp_path
            / "output"
            / "research"
            / "qa"
            / "research_status.json"
        ).read_text(encoding="utf-8")
    )
    assert status["report_available"] is False
    assert status["generation_continues"] is True
    assert status["reason"] == "research_sources_unavailable"
    assert status["attempt_summary"]["rounds"] == RESEARCH_ROUND_LIMIT
    assert status["presentation_handoff"] == {
        "schema_version": 1,
        "delivery_mode": "framework",
        "verified_facts": [],
        "gaps": [
            "Search or direct-read rounds returned only failures or empty results, "
            "so no research report could be validated."
        ],
        "quality_summary": {
            "quality_ok": False,
            "actual_dimensions": None,
            "recommended_dimensions": None,
        },
        "context_files": [],
    }
    assert (
        f"--research-handoff {tmp_path / 'output' / 'research' / 'qa' / 'research_status.json'}"
        in checkpoint
    )


def test_parallel_research_queries_count_as_one_round(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(4):
        policy.record_tool_result(
            "web_search",
            {"query": f"entity {index} official source"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )

    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    assert '"fallback":true' not in checkpoint
    assert (
        '"attempt_summary":{"rounds":1,"calls":4,"successful":4,'
        '"failed":0,"empty":0,"direct_reads":0,"verified_pages":0,'
        '"consecutive_unproductive_reads":0}'
    ) in checkpoint
    assert not (
        tmp_path
        / "output"
        / "research"
        / "qa"
        / "research_status.json"
    ).exists()


def test_deep_research_falls_back_when_candidate_searches_succeed_but_pages_fail(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": f"official source {index}"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    policy.record_tool_result(
        "user_browser_read_page",
        {"url": "https://example.com/report"},
        ToolResult(success=False, error="source_unavailable"),
    )
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    policy.update_checkpoint(checkpoint)
    policy.record_tool_result(
        "managed_browser_navigate",
        {"url": "https://example.org/report"},
        ToolResult(success=False, error="navigation timeout"),
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"fallback":true' in checkpoint
    assert '"fallback_reason":"direct_source_verification_unavailable"' in checkpoint
    assert "a conservative deck will continue automatically" in checkpoint
    assert "do not wait for confirmation" in checkpoint
    assert "outline.json so HTML delivery can continue" in checkpoint
    status = json.loads(
        (
            tmp_path
            / "output"
            / "research"
            / "qa"
            / "research_status.json"
        ).read_text(encoding="utf-8")
    )
    assert status["generation_continues"] is True
    assert status["continued_to"] == "outline"
    assert status["reason"] == "direct_source_verification_unavailable"


def test_deep_research_keeps_validating_after_a_successful_direct_page_read(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": f"official source {index}"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    policy.record_tool_result(
        "managed_browser_navigate",
        {"url": "https://example.com/report"},
        ToolResult(success=True, content="Verified source page body"),
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    assert '"fallback":true' not in checkpoint
    assert not (
        tmp_path
        / "output"
        / "research"
        / "qa"
        / "research_status.json"
    ).exists()


@pytest.mark.parametrize(
    ("navigate_tool", "snapshot_tool"),
    [
        ("managed_browser_navigate", "managed_browser_snapshot"),
    ],
)
def test_playwright_metadata_navigation_waits_for_snapshot_body(
    tmp_path,
    navigate_tool,
    snapshot_tool,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    source_url = "https://example.com/report"
    navigation = ToolResult(
        success=True,
        content=(
            "### Page\n"
            f"- Page URL: {source_url}\n"
            "- Page Title: Example report\n"
            "### Snapshot\n"
            "- [Snapshot](.playwright-mcp/page.yml)"
        ),
    )

    policy.record_tool_result(
        navigate_tool,
        {"url": source_url},
        navigation,
    )

    assert policy._research_direct_read_attempts == 0
    assert policy._research_consecutive_unproductive_direct_reads == 0
    assert policy._research_pending_playwright_url == source_url
    assert policy.direct_evidence_url(
        navigate_tool,
        {"url": source_url},
        navigation,
    ) is None
    blocked_navigation = policy.tool_call_error(
        navigate_tool,
        {"url": "https://example.com/other"},
        verified_evidence_urls=set(),
    )
    assert blocked_navigation is not None
    assert "CONTROLLED_PRESENTATION_RESEARCH_SNAPSHOT_REQUIRED" in blocked_navigation
    assert (
        policy.tool_call_error(
            snapshot_tool,
            {},
            verified_evidence_urls=set(),
        )
        is None
    )

    snapshot = ToolResult(
        success=True,
        content="Example report body with a supported factual claim.",
    )
    policy.record_tool_result(snapshot_tool, {}, snapshot)

    assert policy._research_pending_playwright_url is None
    assert policy._research_direct_read_attempts == 1
    assert policy._research_successful_direct_read_attempts == 1
    assert policy._research_consecutive_unproductive_direct_reads == 0
    assert source_url in policy._research_direct_source_text
    assert policy.direct_evidence_url(snapshot_tool, {}, snapshot) == source_url


def test_research_snapshot_requires_a_pending_navigation(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )

    blocked = policy.tool_call_error(
        "managed_browser_snapshot",
        {},
        verified_evidence_urls=set(),
    )

    assert blocked is not None
    assert "CONTROLLED_PRESENTATION_RESEARCH_NAVIGATION_REQUIRED" in blocked


def test_deep_research_falls_back_after_two_empty_playwright_snapshots(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": f"official source {index}"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    for index in range(2):
        source_url = f"https://example.com/report-{index}"
        policy.record_tool_result(
            "managed_browser_navigate",
            {"url": source_url},
            ToolResult(
                success=True,
                content=(
                    "### Page\n"
                    f"- Page URL: {source_url}\n"
                    "- Page Title: Example report\n"
                    "### Snapshot\n"
                    f"- [Snapshot](.playwright-mcp/page-{index}.yml)"
                ),
            ),
        )
        policy.record_tool_result(
            "managed_browser_snapshot",
            {},
            ToolResult(success=True, content=""),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        if index == 0:
            assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
            assert '"fallback":true' not in checkpoint

    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"fallback":true' in checkpoint
    assert '"fallback_reason":"direct_source_verification_unavailable"' in checkpoint
    assert policy._research_direct_read_attempts == 2
    assert policy._research_consecutive_unproductive_direct_reads == 2


def test_deep_research_rejects_homepage_and_falls_back_after_two_empty_reads(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": f"official source {index}"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    homepage_error = policy.tool_call_error(
        "managed_browser_navigate",
        {"url": "https://example.com/"},
        verified_evidence_urls=set(),
    )
    assert homepage_error is not None
    assert "CONTROLLED_PRESENTATION_EXACT_SOURCE_URL_REQUIRED" in homepage_error

    policy.record_tool_result(
        "managed_browser_navigate",
        {"url": "https://example.com/report-one"},
        ToolResult(
            success=True,
            content=(
                "### Page\n"
                "- Page URL: https://example.com/\n"
                "- Page Title: Example\n"
                "### Snapshot\n"
                "- [Snapshot](snapshot://one)"
            ),
        ),
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    assert '"fallback":true' not in checkpoint

    policy.record_tool_result(
        "managed_browser_navigate",
        {"url": "https://example.com/report-two"},
        ToolResult(success=False, error="navigation timeout"),
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"fallback":true' in checkpoint
    assert (
        '"fallback_reason":"direct_source_verification_unavailable"'
        in checkpoint
    )


def test_research_direct_read_deduplicates_per_browser_backend(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    source_url = "https://example.com/report"
    policy.record_tool_result(
        "user_browser_read_page",
        {"url": source_url},
        ToolResult(
            success=True,
            content=json.dumps(
                {
                    "data": {
                        "url": source_url,
                        "title": "Report",
                        "content": "Verified report body",
                    }
                }
            ),
        ),
    )

    duplicate = policy.tool_call_error(
        "user_browser_read_page",
        {"url": source_url},
        verified_evidence_urls=set(),
    )
    assert duplicate is not None
    assert "CONTROLLED_PRESENTATION_DIRECT_URL_ALREADY_ATTEMPTED" in duplicate
    assert (
        policy.tool_call_error(
            "managed_browser_navigate",
            {"url": source_url},
            verified_evidence_urls=set(),
        )
        is None
    )


def test_research_successful_read_resets_consecutive_empty_read_streak(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    policy.record_tool_result(
        "user_browser_read_page",
        {"url": "https://example.com/one"},
        ToolResult(success=False, error="timeout"),
    )
    policy.record_tool_result(
        "user_browser_read_page",
        {"url": "https://example.com/two"},
        ToolResult(success=True, content="Verified page body"),
    )
    policy.record_tool_result(
        "user_browser_read_page",
        {"url": "https://example.com/three"},
        ToolResult(success=False, error="timeout"),
    )

    assert policy._research_consecutive_unproductive_direct_reads == 1
    assert policy._research_direct_read_complete is False
    assert (
        policy.tool_call_error(
            "user_browser_read_page",
            {"url": "https://example.com/four"},
            verified_evidence_urls=set(),
        )
        is None
    )


def test_research_stops_browsing_after_two_empty_reads_with_verified_subset(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    policy.record_tool_result(
        "user_browser_read_page",
        {"url": "https://example.com/verified"},
        ToolResult(success=True, content="Verified page body"),
    )
    for index in range(2):
        policy.record_tool_result(
            "user_browser_read_page",
            {"url": f"https://example.com/empty-{index}"},
            ToolResult(success=False, error="timeout"),
        )

    policy._research_rounds_without_handoff = RESEARCH_ROUND_LIMIT
    policy._research_calls_since_checkpoint = 0
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    assert "direct-source verification pass is complete" in checkpoint
    assert "Do not search or browse again" in checkpoint
    error = policy.tool_call_error(
        "managed_browser_navigate",
        {"url": "https://example.com/another"},
        verified_evidence_urls=set(),
    )
    assert error is not None
    assert "CONTROLLED_PRESENTATION_RESEARCH_DIRECT_READ_COMPLETE" in error


def test_deep_research_falls_back_after_two_failed_validation_attempts(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": f"official source {index}"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    policy.record_tool_result(
        "managed_browser_navigate",
        {"url": "https://example.com/report"},
        ToolResult(success=True, content="Verified source page body"),
    )
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)

    validation_args = {
        "command": (
            "python validate_research_artifacts.py --research-dir research "
            "--topic market --report research/qa/market_research_check.json; "
            'echo "EXIT=$?"'
        )
    }
    report = tmp_path / "output" / "research" / "qa" / "market_research_check.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"ok":false}', encoding="utf-8")
    policy.record_tool_result(
        "bash",
        validation_args,
        ToolResult(
            success=True,
            content="expected at least 10 dimension files\nEXIT=1",
        ),
    )
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint

    policy.record_tool_result(
        "bash",
        validation_args,
        ToolResult(success=True, content="evidence excerpts remain invalid"),
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"fallback":true' in checkpoint
    assert (
        '"fallback_reason":"research_artifacts_incomplete_or_validation_failed"'
        in checkpoint
    )
    assert "a conservative deck will continue automatically" in checkpoint


def test_deep_research_falls_back_after_repeated_blocked_progress(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": f"official source {index}"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    policy.record_tool_result(
        "managed_browser_navigate",
        {"url": "https://example.com/report"},
        ToolResult(success=True, content="Verified source page body"),
    )
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)

    blocked_error = policy.tool_call_error(
        "bash",
        {"command": "pwd && echo $BOX_AGENT_OUTPUT_DIR"},
        verified_evidence_urls=set(),
    )
    assert blocked_error is not None
    rejection = ToolResult(success=False, error=blocked_error)

    policy.record_tool_result(
        "bash",
        {"command": "pwd && echo $BOX_AGENT_OUTPUT_DIR"},
        rejection,
        executed=False,
    )
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    policy.update_checkpoint(checkpoint)

    policy.record_tool_result(
        "bash",
        {"command": "pwd && echo $BOX_AGENT_OUTPUT_DIR"},
        rejection,
        executed=False,
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"fallback":true' in checkpoint
    assert (
        '"fallback_reason":"research_progress_stalled_after_bounded_search"'
        in checkpoint
    )
    assert policy.repair_stalled is False


def test_deep_research_parallel_search_rejections_count_once_then_fall_back(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": f"official source {index}"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    arguments = {"query": "one more official source"}
    blocked_error = policy.tool_call_error(
        "web_search",
        arguments,
        verified_evidence_urls=set(),
        parallel=True,
    )
    assert blocked_error is not None
    rejection = ToolResult(success=False, error=blocked_error)

    policy.begin_tool_decision(8)
    for _ in range(4):
        policy.record_tool_result(
            "web_search",
            arguments,
            rejection,
            executed=False,
        )

    assert policy._policy_rejection_streak == 1
    assert policy.repair_stalled is False
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    policy.update_checkpoint(checkpoint)

    policy.begin_tool_decision(9)
    policy.record_tool_result(
        "web_search",
        arguments,
        rejection,
        executed=False,
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"fallback":true' in checkpoint
    assert policy.repair_stalled is False


def test_deep_research_without_direct_read_tool_requires_unverified_ledger(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        available_tool_names=frozenset({"web_search", "write_file", "bash"}),
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": f"official source {index}"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    assert "No exact-page read tool is available in this run" in checkpoint
    assert "use URL-bound search summaries" in checkpoint
    assert "otherwise mark the row unverified" in checkpoint
    assert "do not call web_search again" in checkpoint


def test_successful_tool_search_does_not_consume_research_rounds(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        available_tool_names=frozenset({"tool_search", "write_file", "bash"}),
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "tool_search",
            {"query": f"user_browser_read_page_{index}"},
            ToolResult(
                success=True,
                content=json.dumps(
                    {
                        "success": True,
                        "activated": [{"name": "user_browser_read_page"}],
                        "conflicts": [],
                    }
                ),
            ),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    assert policy._research_rounds_without_handoff == 0
    assert policy._research_tool_attempts == 0
    assert policy._research_discovery_attempts == RESEARCH_ROUND_LIMIT
    assert policy.research_search_exhausted is False
    assert (
        policy.tool_call_error(
            "web_search",
            {"query": "first evidence query"},
            verified_evidence_urls=set(),
        )
        is None
    )
    assert "No exact-page read tool is available in this run" not in checkpoint


def test_deep_research_counts_empty_tool_search_rounds_and_falls_back(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        available_tool_names=frozenset({"tool_search"}),
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "tool_search",
            {"query": f"missing capability {index}"},
            ToolResult(
                success=True,
                content=json.dumps(
                    {
                        "success": True,
                        "query": f"missing capability {index}",
                        "activated": [],
                        "conflicts": [],
                        "notice": "No matching MCP tools found.",
                    }
                ),
            ),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"fallback":true' in checkpoint
    assert '"rounds":0' in checkpoint
    assert '"calls":0' in checkpoint
    assert policy._research_discovery_attempts == 3
    assert policy._research_empty_discovery_attempts == 3
    assert '"fallback_reason":"research_tools_unavailable_after_discovery"' in checkpoint


def test_research_blocks_execute_code_network_bypass_but_allows_local_analysis(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
    )

    blocked_codes = (
        "import requests\nresponse = requests.get('https://example.com/report')",
        "import http.client\nhttp.client.HTTPSConnection('example.com')",
        "import pandas as pd\npd.read_html('https://example.com/report')",
        "import subprocess\nsubprocess.run(['curl', 'https://example.com/report'])",
        "client = __import__('requests')\nclient.get('https://example.com/report')",
        "!/usr/bin/curl https://example.com/report",
    )
    blocked = [
        policy.tool_call_error(
            "execute_code",
            {"code": code},
            verified_evidence_urls=set(),
        )
        for code in blocked_codes
    ]
    allowed = policy.tool_call_error(
        "execute_code",
        {
            "code": (
                "import pandas as pd\n"
                "note = 'requests.get is not executed'\n"
                "result = pd.Series([1, 2, 3]).sum()"
            )
        },
        verified_evidence_urls=set(),
    )

    assert all(error is not None for error in blocked)
    assert all(
        "CONTROLLED_PRESENTATION_RESEARCH_NETWORK_TOOL_REQUIRED" in (error or "")
        for error in blocked
    )
    assert allowed is None


def test_deep_research_stops_search_but_requires_report_after_successful_rounds(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": f"entity {index} official source"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    assert '"fallback":true' not in checkpoint
    assert "bounded search rounds already returned candidate sources" in checkpoint
    assert "This ends only the search-query discovery phase" in checkpoint
    assert "it does not stop the task or prohibit direct reading" in checkpoint
    assert "Do not describe this checkpoint to the user as a stop or cancel" in checkpoint
    assert (
        "successful exact-page content obtained during the allowed direct-read phase"
        in checkpoint
    )
    assert "Do not introduce evidence from new search queries" in checkpoint
    assert "Use only the evidence already present" not in checkpoint
    assert "recommended dimension count is a research-quality target" in checkpoint
    assert "target_entities entry uses entity, aliases (array)" in checkpoint
    assert "Do not inspect tabs, execute page scripts" in checkpoint
    assert not (
        tmp_path
        / "output"
        / "research"
        / "qa"
        / "research_status.json"
    ).exists()
    search_complete_error = policy.tool_call_error(
        "web_search",
        {"query": "one more query"},
        verified_evidence_urls=set(),
        parallel=True,
    )
    assert search_complete_error == (
        "CONTROLLED_PRESENTATION_RESEARCH_SEARCH_COMPLETE: bounded research "
        "searches are complete. Do not call web_search or tool_search again. URL-bound "
        "provider summaries may support medium-confidence evidence when the ledger "
        "excerpt is copied from that exact result; read a small set of exact "
        "authoritative pages only when stronger evidence is useful. Then complete the "
        "ledger and validation report."
    )
    assert (
        policy.tool_call_error(
            "tool_search",
            {"query": "one more capability"},
            verified_evidence_urls=set(),
        )
        is None
    )
    assert (
        policy.tool_call_error(
            "user_browser_read_page",
            {"url": "https://example.com/0"},
            verified_evidence_urls=set(),
        )
        is None
    )


def test_stale_research_report_forces_one_exact_revalidation(tmp_path):
    artifact_root = tmp_path / "output" / "tasks" / "task-1"
    research = artifact_root / "research"
    research.mkdir(parents=True)
    for index in range(1, 4):
        (research / f"market_dim{index:02d}.md").write_text(
            f"dimension {index}",
            encoding="utf-8",
        )
    (research / "market_cross_verification.md").write_text(
        "cross verification",
        encoding="utf-8",
    )
    (research / "market_insight.md").write_text("insight", encoding="utf-8")
    report = _write_valid_research_report(research, topic="market")
    report_payload = json.loads(report.read_text(encoding="utf-8"))
    report_payload["presentation_handoff"] = {
        "schema_version": 1,
        "delivery_mode": "full",
        "verified_facts": report_payload["verified_evidence"],
        "gaps": [],
        "quality_summary": {"quality_ok": True},
        # A later research file may not yet be present in this frozen list.
        "context_files": [
            "market_dim01.md",
            "market_cross_verification.md",
            "market_insight.md",
        ],
    }
    report.write_text(json.dumps(report_payload), encoding="utf-8")
    newer = report.stat().st_mtime_ns + 10_000_000
    changed = research / "market_dim02.md"
    os.utime(changed, ns=(newer, newer))

    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=artifact_root,
        research_mode="deep",
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    assert "research files are newer than their QA report" in checkpoint
    assert '"revalidation":{"required":true' in checkpoint
    assert '"stale_dependencies":["market_dim02.md"]' in checkpoint
    policy.update_checkpoint(checkpoint)
    assert policy.research_revalidation is not None
    command = policy.research_revalidation["command"]
    assert "validate_research_artifacts.py" in command
    action = policy.next_deterministic_action()
    assert action is not None
    assert action.capability == "controlled_presentation.research_validate"
    assert action.arguments["command"] == (
        f"cd {shlex.quote(str(artifact_root.resolve()))} && {command}"
    )
    assert "\n" not in action.arguments["command"]
    assert policy.next_deterministic_action() is None
    changed_again = changed.stat().st_mtime_ns + 10_000_000
    os.utime(changed, ns=(changed_again, changed_again))
    changed_checkpoint = policy.build_checkpoint()
    assert changed_checkpoint is not None
    policy.update_checkpoint(changed_checkpoint)
    changed_action = policy.next_deterministic_action()
    assert changed_action is not None
    assert changed_action.action_id != action.action_id

    blocked = policy.tool_call_error(
        "user_browser_read_page",
        {"url": "https://example.com/official"},
        verified_evidence_urls=set(),
    )
    assert blocked is not None
    assert "CONTROLLED_PRESENTATION_RESEARCH_REVALIDATION_REQUIRED" in blocked
    assert (
        policy.tool_call_error(
            "bash",
            {"command": f"{command} && tail -20 research/qa/market_research_check.json"},
            verified_evidence_urls=set(),
        )
        is not None
    )
    assert (
        policy.tool_call_error(
            "bash",
            {"command": command},
            verified_evidence_urls=set(),
        )
        is None
    )
    assert (
        policy.tool_call_error(
            "bash",
            {"command": f"cd {shlex.quote(str(artifact_root))} && {command}"},
            verified_evidence_urls=set(),
        )
        is None
    )
    for rejected_command in (
        f"cd {shlex.quote(str(tmp_path))} && {command}",
        f"cd {shlex.quote(str(artifact_root))} && {command} | tail -20",
        f"cd {shlex.quote(str(artifact_root))} && {command} > validation.log",
        f"cd {shlex.quote(str(artifact_root))} && {command} && echo done",
        f"cd {shlex.quote(str(artifact_root))} &&\n{command}",
        command.replace("--route B", "--route A"),
    ):
        assert (
            policy.tool_call_error(
                "bash",
                {"command": rejected_command},
                verified_evidence_urls=set(),
            )
            is not None
        )

    refreshed = changed_again + 10_000_000
    os.utime(report, ns=(refreshed, refreshed))
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"revalidation"' not in checkpoint


def test_task_scoped_initial_research_validation_is_runtime_owned(tmp_path):
    artifact_root = tmp_path / "output" / "tasks" / "task-1"
    research = artifact_root / "research"
    research.mkdir(parents=True)
    for name in (
        "market_dim01.md",
        "market_cross_verification.md",
        "market_insight.md",
    ):
        (research / name).write_text(name, encoding="utf-8")
    (research / "market_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "topic": "market",
                "target_entities": [],
                "evidence": [],
            }
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=artifact_root,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    assert '"initial":true' in checkpoint
    policy.update_checkpoint(checkpoint)
    action = policy.next_deterministic_action()
    assert action is not None
    assert action.capability == "controlled_presentation.research_validate"
    assert action.arguments["command"].startswith(
        f"cd {shlex.quote(str(artifact_root.resolve()))} && "
    )
    assert "--topic market --route B" in action.arguments["command"]
    assert "\n" not in action.arguments["command"]


def test_deterministic_presentation_commands_are_runtime_owned(tmp_path):
    artifact_root = tmp_path / "output" / "tasks" / "task-1"
    artifact_root.mkdir(parents=True)
    (artifact_root / "outline.json").write_text("{}", encoding="utf-8")
    (artifact_root / "deck.json").write_text("{}", encoding="utf-8")
    (artifact_root / "deck.patch.json").write_text("{}", encoding="utf-8")
    manifest = artifact_root / "assets" / "generated" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=artifact_root,
    )

    expected = (
        ("outline_qa", "outline_validate", "validate_outline.js"),
        ("apply_patch", "apply_patch", "apply_deck_patch.js"),
        ("finalize", "finalize", "finalize_controlled_deck.js"),
    )
    for stage, capability_suffix, script_name in expected:
        policy.stage = stage
        action = policy.next_deterministic_action()
        assert action is not None
        assert action.capability == f"controlled_presentation.{capability_suffix}"
        assert script_name in action.arguments["command"]
        assert "\n" not in action.arguments["command"]


@pytest.mark.asyncio
async def test_stale_research_report_is_revalidated_by_runtime_before_model(tmp_path):
    research = tmp_path / "output" / "research"
    research.mkdir(parents=True)
    dimensions = tuple(
        research / f"market_dim{index:02d}.md" for index in range(1, 4)
    )
    for index, path in enumerate(dimensions, start=1):
        path.write_text(f"dimension {index}", encoding="utf-8")
    cross_verification = research / "market_cross_verification.md"
    cross_verification.write_text("cross verification", encoding="utf-8")
    insight = research / "market_insight.md"
    insight.write_text("insight", encoding="utf-8")
    report = _write_valid_research_report(research, topic="market")
    changed = dimensions[1]
    newer = report.stat().st_mtime_ns + 10_000_000
    os.utime(changed, ns=(newer, newer))
    bash_tool = RuntimeOwnedResearchValidationBashTool(
        report,
        (*dimensions, cross_verification, insight),
    )
    llm = MockLLM([_final("done")])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                workflow_options={"research_mode": "deep"},
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.calls == 1
    assert llm._idx == 1
    assert len(bash_tool.commands) == 1
    command = bash_tool.commands[0]
    assert command.startswith(
        f"cd {shlex.quote(str((tmp_path / 'output').resolve()))} && "
    )
    assert "validate_research_artifacts.py" in command
    assert "\n" not in command
    start = next(
        event
        for event in events
        if isinstance(event, ToolCallStart) and event.tool_name == "bash"
    )
    assert start.tool_call_id.startswith("workflow_")
    result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == start.tool_call_id
    )
    assert result.success is True


def test_outline_semantic_issue_survives_successful_repair_steps(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    first = (
        f"header\n{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_repair\n"
        'REPAIR_INPUT={"issues":["slide-01: page must be 1, got undefined"]}'
    )
    policy.update_checkpoint(first)
    assert policy.repair_stalled is False

    policy.record_tool_result(
        "write_file",
        {"path": "outline.json", "content": "{}"},
        ToolResult(success=True, content="ok"),
    )
    policy.update_checkpoint(
        f"header\n{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_qa"
    )
    second = (
        f"header\n{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_repair\n"
        'REPAIR_INPUT={"issues":["slide-09: page must be 9, got null"]}'
    )
    update = policy.update_checkpoint(second)

    assert policy.repair_stalled is True
    assert policy.stage == "repair_stalled"
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled" in update.text


def test_outline_semantic_issue_reduction_allows_next_repair(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    first = (
        f"header\n{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_repair\n"
        "REPAIR_INPUT="
        '{"issues":['
        '"slide-01: page must be 1, got undefined",'
        '"slide-01: entity-bound research handoff requires verified facts",'
        '"slide-02: entity-bound research handoff requires verified facts"'
        "]}"
    )
    policy.update_checkpoint(first)

    policy.record_tool_result(
        "write_file",
        {"path": "outline.json", "content": "{}"},
        ToolResult(success=True, content="ok"),
    )
    policy.update_checkpoint(
        f"header\n{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_qa"
    )
    second = (
        f"header\n{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_repair\n"
        "REPAIR_INPUT="
        '{"issues":['
        '"slide-01: entity-bound research handoff requires verified facts",'
        '"slide-02: entity-bound research handoff requires verified facts"'
        "]}"
    )
    update = policy.update_checkpoint(second)

    assert policy.repair_stalled is False
    assert policy.stage == "outline_repair"
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled" not in update.text


def test_framework_outline_checkpoint_exempts_structural_pages(tmp_path):
    checkpoint = build_checkpoint_text(tmp_path, "content_ready")

    assert "Cover, agenda, and section-divider pages are structural" in checkpoint
    assert "Every other public-research page must include" in checkpoint
    assert (
        "Removing numeric literals or rewriting unsupported claims as qualitative "
        "prose does not make them verified"
    ) in checkpoint
    assert (
        "put the exact placeholder 暂无可验证公开数据 in that page's message or bullets"
        in checkpoint
    )


def test_research_validator_downgrades_rows_without_captured_exact_page_reads(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    source_url = "https://example.com/report?utm_source=search"
    (research / "topic_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "topic": "topic",
                "target_entities": [],
                "evidence": [
                    {
                        "entity": "Example",
                        "claim": "Example published a report.",
                        "source_url": source_url,
                        "source_type": "first_party",
                        "evidence_excerpt": "Example published a report.",
                        "confidence": "high",
                        "status": "verified",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=tmp_path / "output",
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    validator = {
        "command": (
            "python validate_research_artifacts.py --research-dir research "
            "--topic topic --route B --report research/qa/topic_research_check.json"
        )
    }

    assert policy.tool_call_error(
        "bash",
        validator,
        verified_evidence_urls=set(),
    ) is None
    policy.record_tool_result(
        "bash",
        validator,
        ToolResult(success=True, content="validator completed"),
    )

    payload = json.loads((research / "topic_evidence.json").read_text())
    assert payload["evidence"][0]["status"] == "unverified"
    assert "Runtime could not bind" in payload["evidence"][0]["unverified_reason"]


def test_research_validator_accepts_custom_mcp_exact_page_excerpt(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    source_url = "https://example.com/report/custom"
    excerpt = "Example published a verified custom-source report."
    (research / "topic_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "topic": "topic",
                "target_entities": [],
                "evidence": [
                    {
                        "entity": "Example",
                        "claim": excerpt,
                        "source_url": source_url,
                        "source_type": "first_party",
                        "evidence_excerpt": excerpt,
                        "confidence": "high",
                        "status": "verified",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=tmp_path / "output",
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    tool_name = "company_exact_page"
    arguments = {"source_url": source_url}
    result = ToolResult(success=True, content=f"Page title\n{excerpt}")
    assert (
        policy.tool_call_error(
            tool_name,
            arguments,
            verified_evidence_urls=set(),
        )
        is None
    )
    policy.record_tool_result(tool_name, arguments, result)

    validator = {
        "command": (
            "python validate_research_artifacts.py --research-dir research "
            "--topic topic --route B --report research/qa/topic_research_check.json"
        )
    }
    assert (
        policy.tool_call_error(
            "bash",
            validator,
            verified_evidence_urls={source_url},
        )
        is None
    )
    policy.record_tool_result(
        "bash",
        validator,
        ToolResult(success=True, content="validator completed"),
    )
    payload = json.loads((research / "topic_evidence.json").read_text())
    assert payload["evidence"][0]["status"] == "verified"


def test_research_validator_accepts_url_bound_search_summary(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    source_url = "https://example.com/report/search-summary"
    excerpt = "Example published a search-supported market report."
    (research / "topic_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "topic": "topic",
                "target_entities": [
                    {
                        "entity": "Example",
                        "aliases": ["Example"],
                        "official_domains": [],
                    }
                ],
                "evidence": [
                    {
                        "entity": "Example",
                        "claim": excerpt,
                        "source_url": source_url,
                        "source_type": "secondary",
                        "evidence_excerpt": excerpt,
                        "evidence_basis": "search_summary",
                        "confidence": "medium",
                        "status": "verified",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=tmp_path / "output",
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    search_result = ToolResult(
        success=True,
        content=json.dumps(
            {
                "results": [
                    {
                        "url": source_url,
                        "title": "Example report",
                        "summary": excerpt,
                    },
                    {
                        "url": "https://example.org/other",
                        "title": "Other report",
                        "summary": "A different result summary.",
                    },
                ]
            }
        ),
    )
    policy.record_tool_result("company_search", {"query": "Example"}, search_result)
    policy.record_visible_tool_result(
        "company_search",
        {"query": "Example"},
        search_result,
        search_result.content,
    )
    validator = {
        "command": (
            "python validate_research_artifacts.py --research-dir research "
            "--topic topic --route B --report research/qa/topic_research_check.json"
        )
    }

    assert (
        policy.tool_call_error(
            "bash",
            validator,
            verified_evidence_urls={source_url},
        )
        is None
    )

    payload = json.loads((research / "topic_evidence.json").read_text())
    payload["evidence"][0]["evidence_excerpt"] = "A different result summary."
    (research / "topic_evidence.json").write_text(json.dumps(payload))
    assert policy.tool_call_error(
        "bash",
        validator,
        verified_evidence_urls={source_url},
    ) is None
    policy.record_tool_result(
        "bash",
        validator,
        ToolResult(success=True, content="validator completed"),
    )
    payload = json.loads((research / "topic_evidence.json").read_text())
    assert payload["evidence"][0]["status"] == "unverified"


def test_research_validator_downgrades_locally_rewritten_verified_excerpt(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    source_url = "https://example.com/report"
    (research / "topic_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "topic": "topic",
                "target_entities": [],
                "evidence": [
                    {
                        "entity": "Example",
                        "claim": "Example reached 62.2% in 2026.",
                        "source_url": source_url,
                        "source_type": "secondary",
                        "evidence_excerpt": "Example reached 62.2% in 2026.",
                        "confidence": "high",
                        "status": "verified",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=tmp_path / "output",
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    policy.record_tool_result(
        "user_browser_open_tab_and_read",
        {"url": source_url},
        ToolResult(
            success=True,
            content=json.dumps(
                {
                    "ok": True,
                    "data": {
                        "url": source_url,
                        "title": "Example report",
                        "content": "Example published a general market update.",
                    },
                }
            ),
        ),
    )
    validator = {
        "command": (
            "python validate_research_artifacts.py --research-dir research "
            "--topic topic --route B --report research/qa/topic_research_check.json"
        )
    }

    assert policy.tool_call_error(
        "bash",
        validator,
        verified_evidence_urls={source_url},
    ) is None
    policy.record_tool_result(
        "bash",
        validator,
        ToolResult(success=True, content="validator completed"),
    )
    payload = json.loads((research / "topic_evidence.json").read_text())
    assert payload["evidence"][0]["status"] == "unverified"


def test_research_error_page_does_not_establish_direct_source(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
    )

    policy.record_tool_result(
        "user_browser_open_tab_and_read",
        {"url": "https://example.com/report"},
        ToolResult(
            success=True,
            content=json.dumps(
                {
                    "ok": True,
                    "data": {
                        "url": "https://example.com/report",
                        "title": "404 Not Found",
                        "content": "The requested page does not exist.",
                    },
                }
            ),
        ),
    )

    assert policy._research_successful_direct_read_attempts == 0


def test_research_validator_downgrades_verified_homepage_navigation(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    source_url = "https://example.com/"
    (research / "topic_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "topic": "topic",
                "target_entities": [],
                "evidence": [
                    {
                        "entity": "Example",
                        "claim": "Example published a report.",
                        "source_url": source_url,
                        "source_type": "first_party",
                        "evidence_excerpt": "Example published a report.",
                        "confidence": "high",
                        "status": "verified",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=tmp_path / "output",
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    validator = {
        "command": (
            "python validate_research_artifacts.py --research-dir research "
            "--topic topic --route B --report research/qa/topic_research_check.json"
        )
    }

    assert policy.tool_call_error(
        "bash",
        validator,
        verified_evidence_urls={source_url},
    ) is None
    policy.record_tool_result(
        "bash",
        validator,
        ToolResult(success=True, content="validator completed"),
    )
    payload = json.loads((research / "topic_evidence.json").read_text())
    assert payload["evidence"][0]["status"] == "unverified"


def test_research_direct_read_limit_rejections_do_not_globally_stall(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    for index in range(policy.evidence_read_limit):
        policy.record_tool_result(
            "user_browser_read_page",
            {"url": f"https://example.com/{index}"},
            ToolResult(success=True, content="page"),
        )

    arguments = {"url": "https://example.com/overflow"}
    error = policy.tool_call_error(
        "user_browser_read_page",
        arguments,
        verified_evidence_urls=set(),
    )
    assert error is not None
    assert "CONTROLLED_PRESENTATION_RESEARCH_DIRECT_READ_COMPLETE" in error

    rejection = ToolResult(success=False, error=error)
    for _ in range(3):
        policy.record_tool_result(
            "user_browser_read_page",
            arguments,
            rejection,
            executed=False,
        )
    assert policy._policy_rejection_streak == 3
    assert policy.repair_stalled is False


def test_research_stops_retrying_an_unavailable_browser_connector(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
    )
    policy.record_tool_result(
        "user_browser_read_page",
        {"url": "https://example.com/report", "source_preference": "auto"},
        ToolResult(success=False, error="source_unavailable"),
    )

    connector_error = policy.tool_call_error(
        "user_browser_read_page",
        {"url": "https://example.org/report", "source_preference": "auto"},
        verified_evidence_urls=set(),
    )
    assert connector_error is not None
    assert "RESEARCH_BROWSER_CONNECTOR_UNAVAILABLE" in connector_error
    assert (
        policy.tool_call_error(
            "user_browser_open_tab_and_read",
            {"url": "https://example.org/report"},
            verified_evidence_urls=set(),
        )
        == connector_error
    )
    assert (
        policy.tool_call_error(
            "managed_browser_navigate",
            {"url": "https://example.org/report"},
            verified_evidence_urls=set(),
        )
        is None
    )

    rejection = ToolResult(success=False, error=connector_error)
    for _ in range(3):
        policy.record_tool_result(
            "user_browser_read_page",
            {"url": "https://example.org/report"},
            rejection,
            executed=False,
        )
    assert policy.repair_stalled is False


def test_completed_research_search_blocks_reinspection_but_allows_one_json_repair_read(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    for index in range(RESEARCH_ROUND_LIMIT):
        policy.record_tool_result(
            "web_search",
            {"query": f"entity {index} official source"},
            ToolResult(success=True, content=f"https://example.com/{index}"),
        )
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        policy.update_checkpoint(checkpoint)

    blocked = (
        "CONTROLLED_PRESENTATION_RESEARCH_LOCAL_READ_COMPLETE: bounded research "
        "is complete. Do not inspect/list files or reread skill references, "
        "validator source, or Markdown research notes. Use the evidence already in "
        "context to write the remaining research artifacts and run "
        "validate_research_artifacts.py with --report. After a failed validation, "
        "you may read its JSON report and the JSON evidence ledger once, then make "
        "a repair before reading either again."
    )
    assert (
        policy.tool_call_error(
            "read_file",
            {"path": "/runtime/research-synthesis/output_contract.md"},
            verified_evidence_urls=set(),
        )
        == blocked
    )
    assert (
        policy.tool_call_error(
            "read_file",
            {"path": "research/topic_dim01.md"},
            verified_evidence_urls=set(),
        )
        == blocked
    )
    assert (
        policy.tool_call_error(
            "search_files",
            {"path": "research", "pattern": "**/*"},
            verified_evidence_urls=set(),
        )
        == blocked
    )
    assert (
        policy.tool_call_error(
            "bash",
            {"command": "pwd && ls research"},
            verified_evidence_urls=set(),
        )
        == blocked
    )
    browser_reinspection = policy.tool_call_error(
        "managed_browser_tabs",
        {"action": "select", "index": 1},
        verified_evidence_urls=set(),
    )
    assert browser_reinspection is not None
    assert "RESEARCH_BROWSER_REINSPECTION_COMPLETE" in browser_reinspection
    assert (
        policy.tool_call_error(
            "managed_browser_evaluate",
            {"function": "() => document.body.innerText"},
            verified_evidence_urls=set(),
        )
        == browser_reinspection
    )
    section_error = policy.tool_call_error(
        "user_browser_read_section",
        {"section_id": "section-1"},
        verified_evidence_urls=set(),
    )
    assert section_error is not None
    assert "CONTROLLED_PRESENTATION_EXACT_SOURCE_URL_REQUIRED" in section_error
    assert (
        policy.tool_call_error(
            "bash",
            {
                "command": (
                    "python validate_research_artifacts.py --research-dir research "
                    "--topic topic --report research/qa/topic_research_check.json"
                )
            },
            verified_evidence_urls=set(),
        )
        is None
    )

    report_args = {"path": "research/qa/topic_research_check.json"}
    assert (
        policy.tool_call_error(
            "read_file",
            report_args,
            verified_evidence_urls=set(),
        )
        is None
    )
    policy.record_tool_result(
        "read_file",
        report_args,
        ToolResult(success=True, content='{"ok": false}'),
    )
    assert (
        policy.tool_call_error(
            "read_file",
            report_args,
            verified_evidence_urls=set(),
        )
        == blocked
    )
    policy.record_tool_result(
        "write_file",
        {"path": "research/topic_evidence.json", "content": "{}"},
        ToolResult(success=True, content="written"),
    )
    assert (
        policy.tool_call_error(
            "read_file",
            report_args,
            verified_evidence_urls=set(),
        )
        is None
    )


def test_failed_research_validator_refreshes_json_repair_reads(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    report_args = {"path": "research/qa/topic_research_check.json"}
    evidence_args = {"path": "research/topic_evidence.json"}

    policy.record_tool_result(
        "read_file",
        report_args,
        ToolResult(success=True, content='{"ok": false}'),
    )
    policy.record_tool_result(
        "read_file",
        evidence_args,
        ToolResult(success=True, content='{"evidence": []}'),
    )
    assert policy.tool_call_error(
        "read_file",
        report_args,
        verified_evidence_urls=set(),
    ) is not None
    assert policy.tool_call_error(
        "read_file",
        evidence_args,
        verified_evidence_urls=set(),
    ) is not None

    validator_args = {
        "command": (
            "python validate_research_artifacts.py --research-dir research "
            "--topic topic --report research/qa/topic_research_check.json"
        )
    }
    policy.record_tool_result(
        "bash",
        validator_args,
        ToolResult(success=False, error="validation failed"),
    )

    assert (
        policy.tool_call_error(
            "read_file",
            report_args,
            verified_evidence_urls=set(),
        )
        is None
    )
    assert (
        policy.tool_call_error(
            "read_file",
            evidence_args,
            verified_evidence_urls=set(),
        )
        is None
    )


def test_research_repair_read_allows_session_root_when_artifacts_use_output(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    research = tmp_path / "research"
    report = research / "qa" / "topic_research_check.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"ok": false}', encoding="utf-8")
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        research_mode="deep",
        stage="research",
        research_search_exhausted=True,
    )
    report_args = {"path": str(report)}

    assert (
        policy.tool_call_error(
            "read_file",
            report_args,
            verified_evidence_urls=set(),
        )
        is None
    )
    policy.record_tool_result(
        "read_file",
        report_args,
        ToolResult(success=True, content='{"ok": false}'),
    )

    assert "CONTROLLED_PRESENTATION_RESEARCH_LOCAL_READ_COMPLETE" in (
        policy.tool_call_error(
            "read_file",
            report_args,
            verified_evidence_urls=set(),
        )
        or ""
    )


def test_deep_research_recovers_only_from_explicit_persisted_fallback(tmp_path):
    research = tmp_path / "output" / "research"
    qa = research / "qa"
    qa.mkdir(parents=True)
    for index in range(1, 4):
        (research / f"topic_dim{index:02d}.md").write_text(
            f"dimension {index}",
            encoding="utf-8",
        )

    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint

    (qa / "research_status.json").write_text(
        json.dumps(
            {
                "status": "fallback",
                "report_available": False,
                "generation_continues": True,
            }
        ),
        encoding="utf-8",
    )
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"fallback":true' in checkpoint


def test_deep_research_checkpoint_accepts_validated_user_input_evidence(tmp_path):
    research = tmp_path / "output" / "research"
    research.mkdir(parents=True)
    for index in range(1, 4):
        (research / f"topic_dim{index:02d}.md").write_text(
            f"dimension {index}",
            encoding="utf-8",
        )
    (research / "topic_cross_verification.md").write_text(
        "cross verification",
        encoding="utf-8",
    )
    (research / "topic_insight.md").write_text("insight", encoding="utf-8")
    report_path = _write_valid_research_report(research, topic="topic")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["verified_evidence"].append(
        {
            "entity": "User project brief",
            "claim": "The user supplied the required project capabilities.",
            "source_url": "user-input:project-brief",
            "source_type": "user_input",
            "evidence_excerpt": "The user supplied the required project capabilities.",
            "confidence": "high",
            "status": "verified",
            "canonical": (
                "User project brief | The user supplied the required project "
                "capabilities. | user_input | user-input:project-brief"
            ),
        }
    )
    report["verified_evidence_count"] = len(report["verified_evidence"])
    report_path.write_text(json.dumps(report), encoding="utf-8")

    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert "user-input:project-brief" in checkpoint


def test_controlled_auto_images_rebase_after_http_401(tmp_path):
    output = tmp_path / "output"
    generated = output / "assets" / "generated"
    generated.mkdir(parents=True)
    (output / "outline.json").write_text('{"slides": []}', encoding="utf-8")
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text('{"slides": []}', encoding="utf-8")
    manifest_path = generated / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "mode": "auto",
                "image_plan": [
                    {
                        "decision": "generate",
                        "status": "pending",
                        "required": True,
                        "output_path": "assets/generated/cover.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
        stage="images",
    )

    policy.record_tool_result(
        "generate_image",
        {"output_path": "assets/generated/cover.png", "watermark": False},
        ToolResult(
            success=False,
            error="Image generation failed: HTTP 401 Unauthorized",
        ),
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}image_policy_rebase" in checkpoint
    assert "--policy unavailable" in checkpoint
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["image_service"] == {
        "status": "blocked",
        "reason": "authorization_401",
    }
    unavailable_command = (
        "cd output && node "
        f"{REBASE_IMAGE_POLICY_SCRIPT} deck.json "
        "--manifest assets/generated/manifest.json --policy unavailable"
    )
    assert (
        policy.tool_call_error(
            "bash",
            {"command": unavailable_command},
            verified_evidence_urls=set(),
        )
        is None
    )
    assert policy.tool_call_error(
        "bash",
        {"command": unavailable_command.replace("unavailable", "forbidden")},
        verified_evidence_urls=set(),
    ) is not None
    assert policy.allows_completion_continuation() is True


def test_controlled_explicit_retry_restores_unavailable_plan_before_images(tmp_path):
    output = tmp_path / "output"
    generated = output / "assets" / "generated"
    generated.mkdir(parents=True)
    (output / "outline.json").write_text('{"slides": []}', encoding="utf-8")
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    original_deck = {"slides": []}
    (output / "deck.json").write_text(json.dumps(original_deck), encoding="utf-8")
    original_plan = [
        {
            "slide_id": "slide-01",
            "decision": "generate",
            "status": "pending",
            "required": True,
            "prompt": "Generate a product hero image",
            "output_path": "assets/generated/hero.png",
        }
    ]
    manifest_path = generated / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "mode": "auto",
                "generation_forbidden": False,
                "image_generation_unavailable": True,
                "image_service": {
                    "status": "blocked",
                    "reason": "authorization_401",
                },
                "image_plan": [
                    {
                        "slide_id": "slide-01",
                        "decision": "skip",
                        "status": "skipped",
                        "required": False,
                        "prompt": "",
                        "output_path": None,
                    }
                ],
                "image_unavailable_recovery": {
                    "schema_version": 1,
                    "deck": original_deck,
                    "image_plan": original_plan,
                    "has_mode": True,
                    "mode": "auto",
                    "has_generation_forbidden": True,
                    "generation_forbidden": False,
                },
            }
        ),
        encoding="utf-8",
    )
    gate = CompletionGate(
        workflow_checkpoint_kind="controlled_presentation",
        workflow_options={
            IMAGE_GENERATION_POLICY_OPTION: IMAGE_GENERATION_EXPLICIT_RETRY,
        },
    )

    restore_checkpoint = completion_gate_progress_text(gate, str(tmp_path))

    assert restore_checkpoint is not None
    assert (
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}image_policy_rebase"
        in restore_checkpoint
    )
    assert "--policy retry" in restore_checkpoint
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    policy.update_checkpoint(restore_checkpoint)
    retry_command = (
        "cd output && node "
        f"{REBASE_IMAGE_POLICY_SCRIPT} deck.json "
        "--manifest assets/generated/manifest.json --policy retry"
    )
    assert (
        policy.tool_call_error(
            "bash",
            {"command": retry_command},
            verified_evidence_urls=set(),
        )
        is None
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["image_plan"] = original_plan
    manifest.pop("image_generation_unavailable")
    manifest.pop("image_unavailable_recovery")
    manifest.pop("image_service")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    resumed_checkpoint = completion_gate_progress_text(gate, str(tmp_path))

    assert resumed_checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}images" in resumed_checkpoint
    assert "IMAGE_INPUT=" in resumed_checkpoint


def test_controlled_creative_images_stop_after_http_401(tmp_path):
    generated = tmp_path / "output" / "assets" / "generated"
    generated.mkdir(parents=True)
    manifest_path = generated / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "mode": "creative_image_mode",
                "image_plan": [
                    {
                        "decision": "generate",
                        "status": "pending",
                        "required": True,
                        "output_path": "assets/generated/cover.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
        stage="images",
    )

    policy.record_tool_result(
        "generate_image",
        {"output_path": "assets/generated/cover.png", "watermark": False},
        ToolResult(
            success=False,
            error="Image generation failed: HTTP 401 Unauthorized",
        ),
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    policy.update_checkpoint(checkpoint)
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}image_auth_blocked" in checkpoint
    assert policy.allows_completion_continuation() is False


def test_controlled_successful_mutations_without_progress_stop_after_two(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
    )
    checkpoint = (
        "Internal checkpoint\n"
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}deck_spec_repair\n"
        "NEXT_ACTION=repair"
    )
    policy.update_checkpoint(checkpoint)

    for _ in range(2):
        policy.record_tool_result(
            "write_file",
            {"path": "deck.patch.json", "content": "{}"},
            ToolResult(success=True, content="written"),
        )
        update = policy.update_checkpoint(checkpoint)

    assert policy.repair_stalled is True
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled" in update.text
    assert "return it as a degraded draft" in update.text
    assert "do not claim that no deliverable was produced" in update.text
    assert policy.allows_completion_continuation() is False


def test_controlled_outline_qa_validation_and_rewrite_do_not_trip_repair_fuse(
    tmp_path,
):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
    )
    checkpoint = (
        "Internal checkpoint\n"
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_qa\n"
        "NEXT_ACTION=validate"
    )
    policy.update_checkpoint(checkpoint)

    policy.record_tool_result(
        "bash",
        {"command": "node validate_outline.js outline.json"},
        ToolResult(success=True, content="Error: invalid outline"),
    )
    policy.update_checkpoint(checkpoint)
    policy.record_tool_result(
        "write_file",
        {"path": "outline.json", "content": "{}"},
        ToolResult(success=True, content="written"),
    )
    update = policy.update_checkpoint(checkpoint)

    assert policy.repair_stalled is False
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_qa" in update.text


def test_controlled_staged_repair_counts_only_commit_as_mutation(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
    )
    checkpoint = (
        "Internal checkpoint\n"
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_repair\n"
        "NEXT_ACTION=repair"
    )
    policy.update_checkpoint(checkpoint)

    for arguments in (
        {"action": "begin", "path": "outline.json"},
        {"action": "append_text", "chunk_index": 0, "content": "{}"},
    ):
        policy.record_tool_result(
            "staged_file_write",
            arguments,
            ToolResult(success=True, content="ok"),
        )
        policy.update_checkpoint(checkpoint)

    assert policy.repair_stalled is False
    assert policy._no_progress_mutation_streak == 0

    policy.record_tool_result(
        "staged_file_write",
        {"action": "commit"},
        ToolResult(success=True, content="committed"),
    )
    policy.update_checkpoint(checkpoint)

    assert policy.repair_stalled is False
    assert policy._no_progress_mutation_streak == 1

    policy.record_tool_result(
        "write_file",
        {"path": "outline.json", "content": "{}"},
        ToolResult(success=True, content="written"),
    )
    update = policy.update_checkpoint(checkpoint)

    assert policy.repair_stalled is True
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled" in update.text


def test_controlled_repair_allows_only_staged_transaction_for_expected_artifact(
    tmp_path,
):
    outline = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        stage="outline_repair",
        has_repair_input=True,
    )
    deck = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        stage="deck_spec_repair",
        has_repair_input=True,
    )

    assert (
        outline.tool_call_error(
            "staged_file_write",
            {"action": "begin", "path": "outline.json"},
            verified_evidence_urls=set(),
        )
        is None
    )
    assert (
        outline.tool_call_error(
            "staged_file_write",
            {"action": "append_text", "chunk_index": 0, "content": "{}"},
            verified_evidence_urls=set(),
        )
        is None
    )
    assert "CONTROLLED_PRESENTATION_OUTLINE_REPAIR_INPUT_READY" in (
        outline.tool_call_error(
            "staged_file_write",
            {"action": "begin", "path": "deck.patch.json"},
            verified_evidence_urls=set(),
        )
        or ""
    )
    assert (
        deck.tool_call_error(
            "staged_file_write",
            {"action": "begin", "path": "deck.patch.json"},
            verified_evidence_urls=set(),
        )
        is None
    )
    assert "CONTROLLED_PRESENTATION_REPAIR_INPUT_READY" in (
        deck.tool_call_error(
            "write_file",
            {"path": "outline.json", "content": "{}"},
            verified_evidence_urls=set(),
        )
        or ""
    )


def test_controlled_apply_patch_repair_allows_bound_staged_transaction(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        stage="apply_patch",
        apply_patch_repair_allowed=True,
        apply_patch_repair_paths=("slides.11.props.rows.0",),
    )

    assert (
        policy.tool_call_error(
            "staged_file_write",
            {"action": "begin", "path": "deck.patch.json", "expected_chunks": 1},
            verified_evidence_urls=set(),
        )
        is None
    )
    policy.record_tool_result(
        "staged_file_write",
        {"action": "begin", "path": "deck.patch.json", "expected_chunks": 1},
        ToolResult(
            success=True,
            content="started",
            raw_output={"write_id": "repair-write"},
        ),
    )
    assert (
        policy.tool_call_error(
            "staged_file_write",
            {
                "action": "append_text",
                "write_id": "repair-write",
                "chunk_index": 0,
                "content": "{}",
            },
            verified_evidence_urls=set(),
        )
        is None
    )
    assert "CONTROLLED_PRESENTATION_APPLY_PATCH_REPAIR_REQUIRED" in (
        policy.tool_call_error(
            "staged_file_write",
            {
                "action": "commit",
                "write_id": "another-write",
            },
            verified_evidence_urls=set(),
        )
        or ""
    )
    assert (
        policy.tool_call_error(
            "staged_file_write",
            {"action": "commit", "write_id": "repair-write"},
            verified_evidence_urls=set(),
        )
        is None
    )


def test_controlled_research_handoff_requires_artifact_root_outline_path(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        research_mode="deep",
        stage="outline",
    )

    for path in ("outline.json", "output/outline.json", str(output / "outline.json")):
        assert (
            policy.tool_call_error(
                "write_file",
                {"path": path, "content": "{}"},
                verified_evidence_urls=set(),
            )
            is None
        )

    blocked = policy.tool_call_error(
        "write_file",
        {"path": str(tmp_path / "outline.json"), "content": "{}"},
        verified_evidence_urls=set(),
    )

    assert blocked is not None
    assert "CONTROLLED_PRESENTATION_OUTLINE_TARGET_REQUIRED" in blocked
    assert f"actual_path='{tmp_path / 'outline.json'}'" in blocked
    assert "expected_path='outline.json'" in blocked
    assert f"artifact_root='{output}'" in blocked
    assert "absolute session-workspace path" in blocked

    content_ready_policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        research_mode="content_ready",
        stage="outline",
    )
    content_ready_blocked = content_ready_policy.tool_call_error(
        "write_file",
        {"path": str(tmp_path / "outline.json"), "content": "{}"},
        verified_evidence_urls=set(),
    )
    assert content_ready_blocked is not None
    assert "CONTROLLED_PRESENTATION_OUTLINE_TARGET_REQUIRED" in content_ready_blocked


def test_controlled_research_writes_stay_under_artifact_root(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        research_mode="deep",
        stage="research",
    )

    assert (
        policy.tool_call_error(
            "write_file",
            {"path": "outline.json", "content": "{}"},
            verified_evidence_urls=set(),
        )
        is None
    )

    for path in (
        "research/dim01.md",
        "output/research/dim01.md",
        str(output / "research" / "dim01.md"),
    ):
        assert (
            policy.tool_call_error(
                "write_file",
                {"path": path, "content": "evidence"},
                verified_evidence_urls=set(),
            )
            is None
        )

    escaped = str(tmp_path / "research" / "dim01.md")
    blocked = policy.tool_call_error(
        "write_file",
        {"path": escaped, "content": "evidence"},
        verified_evidence_urls=set(),
    )

    assert blocked is not None
    assert "CONTROLLED_PRESENTATION_RESEARCH_ARTIFACT_TARGET_REQUIRED" in blocked
    assert f"actual_path='{escaped}'" in blocked
    assert "expected_path='research/...'" in blocked
    assert f"artifact_root='{output}'" in blocked


def test_controlled_research_mkdir_stays_under_task_artifact_root(tmp_path):
    task_root = tmp_path / "output" / "tasks" / "task-1"
    task_root.mkdir(parents=True)
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=task_root,
        research_mode="deep",
        stage="research",
    )

    allowed = policy.tool_call_error(
        "bash",
        {"command": "pwd && mkdir -p research/qa"},
        verified_evidence_urls=set(),
    )
    escaped = tmp_path / "output" / "research" / "qa"
    blocked = policy.tool_call_error(
        "bash",
        {"command": f"mkdir -p {escaped}"},
        verified_evidence_urls=set(),
    )

    assert allowed is None
    assert blocked is not None
    assert "CONTROLLED_PRESENTATION_RESEARCH_ARTIFACT_TARGET_REQUIRED" in blocked
    assert f"actual_path='{escaped}'" in blocked
    assert f"artifact_root='{task_root}'" in blocked


def test_controlled_research_handoff_allows_only_its_staged_outline_transaction(
    tmp_path,
):
    output = tmp_path / "output"
    output.mkdir()
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        research_mode="deep",
        stage="outline",
    )

    assert policy.tool_call_error(
        "staged_file_write",
        {"action": "append_text", "write_id": "outline-write", "chunk_index": 0},
        verified_evidence_urls=set(),
    ) is not None
    assert policy.tool_call_error(
        "staged_file_write",
        {"action": "begin", "path": "outline.json"},
        verified_evidence_urls=set(),
    ) is None

    policy.record_tool_result(
        "staged_file_write",
        {"action": "begin", "path": "outline.json"},
        ToolResult(
            success=True,
            content="started",
            raw_output={"write_id": "outline-write"},
        ),
    )

    assert policy.tool_call_error(
        "staged_file_write",
        {"action": "append_text", "write_id": "wrong", "chunk_index": 0},
        verified_evidence_urls=set(),
    ) is not None
    assert policy.tool_call_error(
        "staged_file_write",
        {"action": "append_text", "write_id": "outline-write", "chunk_index": 0},
        verified_evidence_urls=set(),
    ) is None
    assert policy.tool_call_error(
        "staged_file_write",
        {"action": "commit", "write_id": "outline-write"},
        verified_evidence_urls=set(),
    ) is None

    policy.record_tool_result(
        "staged_file_write",
        {"action": "commit", "write_id": "outline-write"},
        ToolResult(success=True, content="committed"),
    )
    assert policy.tool_call_error(
        "staged_file_write",
        {"action": "append_text", "write_id": "outline-write", "chunk_index": 1},
        verified_evidence_urls=set(),
    ) is not None


def test_controlled_outline_policy_rejections_trip_repair_fuse(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=tmp_path / "output",
        research_mode="deep",
    )
    checkpoint = (
        "Internal checkpoint\n"
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline\n"
        "NEXT_ACTION=write outline.json"
    )
    policy.update_checkpoint(checkpoint)

    for decision_id in range(1, 4):
        policy.begin_tool_decision(decision_id)
        blocked = policy.tool_call_error(
            "write_file",
            {"path": str(tmp_path / "outline.json"), "content": "{}"},
            verified_evidence_urls=set(),
        )
        assert blocked is not None
        policy.record_tool_result(
            "write_file",
            {"path": str(tmp_path / "outline.json"), "content": "{}"},
            ToolResult(success=False, error=blocked),
            executed=False,
        )

    update = policy.update_checkpoint(checkpoint)

    assert policy.repair_stalled is True
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled" in update.text


def test_controlled_repeated_execution_failures_must_be_consecutive(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
        stage="finalize",
    )
    arguments = {
        "command": f"cd output && node {FINALIZER_SCRIPT} deck.json --out index.html"
    }

    for error in ("failure A", "failure B", "failure A"):
        policy.record_tool_result(
            "bash",
            arguments,
            ToolResult(success=False, error=error),
        )

    assert policy.repair_stalled is False

    policy.record_tool_result(
        "bash",
        arguments,
        ToolResult(success=False, error="failure A"),
    )

    assert policy.repair_stalled is True


def test_controlled_repair_execution_failures_reset_when_error_changes(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
        stage="deck_spec_repair",
    )

    for error in ("write failure A", "write failure B"):
        policy.record_tool_result(
            "write_file",
            {"path": "deck.patch.json", "content": "{}"},
            ToolResult(success=False, error=error),
        )

    assert policy.repair_stalled is False

    policy.record_tool_result(
        "write_file",
        {"path": "deck.patch.json", "content": "{}"},
        ToolResult(success=False, error="write failure B"),
    )

    assert policy.repair_stalled is True


def test_controlled_policy_rejections_must_be_consecutive(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
        stage="deck_spec_repair",
    )

    for error in (
        "CONTROLLED_PRESENTATION_REPAIR_INPUT_READY: violation A",
        "CONTROLLED_PRESENTATION_OUTLINE_REPAIR_INPUT_READY: violation B",
        "CONTROLLED_PRESENTATION_REPAIR_INPUT_READY: violation A",
    ):
        policy.record_tool_result(
            "read_file",
            {"path": "qa/deck_spec.json"},
            ToolResult(success=False, error=error),
            executed=False,
        )

    assert policy.repair_stalled is False


def test_controlled_policy_rejections_normalize_error_code(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
        stage="deck_spec_repair",
    )

    for suffix in ("first parameter shape", "different detail", "third attempt"):
        policy.record_tool_result(
            "read_file",
            {"path": "qa/deck_spec.json"},
            ToolResult(
                success=False,
                error=f"CONTROLLED_PRESENTATION_REPAIR_INPUT_READY: {suffix}",
            ),
            executed=False,
        )

    assert policy.repair_stalled is True

    for _ in range(2):
        policy.record_tool_result(
            "read_file",
            {"path": "qa/deck_spec.json"},
            ToolResult(
                success=False,
                error="CONTROLLED_PRESENTATION_REPAIR_INPUT_READY: violation A",
            ),
            executed=False,
        )

    assert policy.repair_stalled is True


def test_controlled_scaffold_policy_rejection_streak_resets_after_execution(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
        stage="scaffold",
    )
    rejection = ToolResult(
        success=False,
        error=(
            "CONTROLLED_PRESENTATION_SCAFFOLD_INPUT_READY: remove the shell pipe"
        ),
    )

    for tail_lines in (60, 100):
        policy.record_tool_result(
            "bash",
            {"command": f"inspect_deck_contract.js 2>&1 | tail -{tail_lines}"},
            rejection,
            executed=False,
        )

    policy.record_tool_result(
        "bash",
        {"command": "inspect_deck_contract.js --outline outline.json --out deck.json"},
        ToolResult(success=True, content="ok"),
    )
    for tail_lines in (120, 200):
        policy.record_tool_result(
            "bash",
            {"command": f"inspect_deck_contract.js 2>&1 | tail -{tail_lines}"},
            rejection,
            executed=False,
        )

    assert policy.repair_stalled is False

    policy.record_tool_result(
        "bash",
        {"command": "inspect_deck_contract.js 2>&1 | tail -300"},
        rejection,
        executed=False,
    )

    assert policy.repair_stalled is True


def test_controlled_parallel_calls_obey_repair_stage_guards(tmp_path):
    stalled = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        stage="repair_stalled",
        repair_stalled=True,
    )
    repairing = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        stage="deck_spec_repair",
        has_repair_input=True,
    )
    arguments = {
        "output_path": "assets/generated/probe.png",
        "watermark": False,
    }

    stalled_error = stalled.tool_call_error(
        "generate_image",
        arguments,
        verified_evidence_urls=set(),
        parallel=True,
    )
    repair_error = repairing.tool_call_error(
        "generate_image",
        arguments,
        verified_evidence_urls=set(),
        parallel=True,
    )

    assert stalled_error is not None
    assert "CONTROLLED_PRESENTATION_REPAIR_STALLED" in stalled_error
    assert repair_error is not None
    assert "CONTROLLED_PRESENTATION_REPAIR_INPUT_READY" in repair_error


def test_deep_research_checkpoint_does_not_fail_open_without_progress(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="deep",
    )

    for _ in range(RESEARCH_ROUND_LIMIT + 2):
        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
        assert '"fallback":true' not in checkpoint
        policy.update_checkpoint(checkpoint)

    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" in checkpoint
    assert '"fallback":true' not in checkpoint


def test_deep_research_does_not_override_existing_outline(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "deck_goal": "Explain the topic",
                "audience": "Reviewers",
                "source_mode": "public_authoritative_research",
                "storyline": "Evidence to decision",
                "slides": [],
            }
        ),
        encoding="utf-8",
    )
    gate = CompletionGate(
        workflow_checkpoint_kind="controlled_presentation",
        workflow_options={"research_mode": "deep"},
    )

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_qa" in checkpoint
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" not in checkpoint


def test_short_solution_design_brief_skips_research_synthesis(tmp_path):
    gate = build_auto_completion_gate(
        (
            "帮我做一份企业 AI 客服系统方案 PPT，重点讲系统架构、CRM、"
            "订单和客服平台集成，以及数据处理流程，6 页左右。"
        ),
        tmp_path,
    )

    assert gate is not None
    assert gate.workflow_options["research_mode"] == "content_ready"
    assert gate.max_tool_calls == 160

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}research" not in checkpoint
    assert "research-synthesis workflow before outline authoring" not in checkpoint


def test_deep_research_checkpoint_accepts_legacy_root_handoff(tmp_path):
    gate = build_auto_completion_gate(
        "制作一份 2026 世界杯商业价值分析 PPT",
        tmp_path,
    )
    research = tmp_path / "research"
    research.mkdir()
    for index in range(1, 4):
        (research / f"worldcup_dim{index:02d}.md").write_text(
            f"dimension evidence {index}",
            encoding="utf-8",
        )
    (research / "worldcup_cross_verification.md").write_text(
        "cross verification",
        encoding="utf-8",
    )
    (research / "worldcup_insight.md").write_text("synthesis", encoding="utf-8")
    _write_valid_research_report(research, topic="worldcup")

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert '"research/worldcup_dim01.md"' in checkpoint


def test_source_first_presentation_does_not_force_public_research(tmp_path):
    gate = build_auto_completion_gate("制作一份我司新员工入职培训 PPT", tmp_path)

    assert gate is not None
    assert gate.workflow_options["research_mode"] == "source_first"
    assert gate.max_tool_calls == 160
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert "RESEARCH_INPUT=" not in checkpoint


def test_outline_repair_input_normalizes_markdown_link_delimiters(tmp_path):
    outline = tmp_path / "outline.json"
    report = tmp_path / "outline_check.json"
    research = tmp_path / "research.md"
    outline.write_text(
        json.dumps(
            {
                "source_mode": "public_authoritative_research",
                "slides": [
                    {
                        "evidence": [
                            "https://example.com/source",
                            "https://unsupported.example/item",
                        ]
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report.write_text(
        json.dumps({"ok": False, "issues": ["repair evidence"]}),
        encoding="utf-8",
    )
    research.write_text(
        "[Authoritative source](https://example.com/source).",
        encoding="utf-8",
    )

    repair_input = _outline_repair_input(report, outline, (research,))

    assert repair_input is not None
    payload = json.loads(repair_input)
    assert payload["allowed_research_urls"] == ["https://example.com/source"]
    assert payload["unsupported_evidence_urls"] == [
        "https://unsupported.example/item"
    ]


def test_legacy_semantic_and_image_reports_can_resume_through_finalizer(
    tmp_path: Path,
) -> None:
    spec_report = tmp_path / "deck_spec.json"
    outline_issue = "slides.slide-01.props: must preserve outline message"
    spec_report.write_text(
        json.dumps(
            {
                "ok": False,
                "delivery_policy": "allow_outline_binding_draft",
                "issues": [outline_issue],
                "outlineBinding": {"ok": False, "issues": [outline_issue]},
                "designContract": {"ok": True},
            }
        ),
        encoding="utf-8",
    )
    image_report = tmp_path / "image_manifest.json"
    image_report.write_text(
        json.dumps({"ok": False, "issues": ["required image is unresolved"]}),
        encoding="utf-8",
    )

    assert _deck_spec_failure_is_degradable(spec_report) is True
    assert _image_manifest_failure_is_degradable(image_report) is True

    payload = json.loads(spec_report.read_text(encoding="utf-8"))
    payload.pop("delivery_policy")
    spec_report.write_text(json.dumps(payload), encoding="utf-8")
    assert _deck_spec_failure_is_degradable(spec_report) is False

    payload["delivery_policy"] = "allow_outline_binding_draft"
    payload["issues"].append("slides.slide-01.layout_id: unknown layout")
    spec_report.write_text(json.dumps(payload), encoding="utf-8")
    assert _deck_spec_failure_is_degradable(spec_report) is False

    payload["issues"] = [outline_issue]
    payload["structuralIssues"] = [
        "slides.slide-01.props.items: still contains scaffold placeholder content"
    ]
    spec_report.write_text(json.dumps(payload), encoding="utf-8")
    assert _deck_spec_failure_is_degradable(spec_report) is False


@pytest.mark.parametrize(
    ("locale", "complete", "details_heading", "qa_passed", "qa_notice"),
    (
        (
            "zh",
            "演示文稿已完成",
            "## 生成详情",
            "质量检查：7/7 项通过",
            "检查提示：2 项",
        ),
        (
            "en",
            "Presentation complete",
            "## Generation details",
            "Quality checks: 7/7 passed",
            "Check notices: 2",
        ),
        (
            "ja",
            "プレゼンテーションが完成しました",
            "## 生成の詳細",
            "品質チェック：7/7 項目合格",
            "確認事項：2 件",
        ),
    ),
)
def test_presentation_delivery_contract_covers_supported_host_languages(
    locale,
    complete,
    details_heading,
    qa_passed,
    qa_notice,
):
    contract = _presentation_delivery_contract(7, 2)

    assert f"ui_language={locale}" in contract
    assert complete in contract
    assert details_heading in contract
    assert qa_passed in contract
    assert qa_notice in contract
    assert "explicit user language request wins" in contract
    assert "Never override that contract with a locale-specific default" in contract


def test_controlled_presentation_checkpoint_tracks_filesystem_stages(tmp_path):
    gate = CompletionGate(workflow_checkpoint_kind="controlled_presentation")

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert "one distinct message" in checkpoint
    assert "deck_goal, audience, source_mode, storyline, slides" in checkpoint
    assert "page, title, message, bullets, layout, visual, evidence" in checkpoint
    assert "bullets and evidence are arrays" in checkpoint
    assert "use [] when evidence is empty" in checkpoint
    assert "goal, slide_no, layout_intent, or visual_intent" in checkpoint
    assert "at least one evidence item unless it explicitly marks" in checkpoint
    assert "a required fact as unavailable" in checkpoint
    assert "copy every evidence item exactly from that canonical list" in checkpoint
    assert "entity binding is a hard outline failure" in checkpoint
    assert "continue to HTML delivery" in checkpoint
    assert "Do not invent the expected URL" in checkpoint
    assert "do not read outline.md again" in checkpoint
    assert "inspect/list themes or layouts" in checkpoint
    assert "every Arabic-number literal" in checkpoint
    assert "actual http(s) source URL" in checkpoint
    assert "AuthLevel as a ranking hint" in checkpoint
    assert "site:-constrained query" in checkpoint
    assert "validate_outline.js" in checkpoint
    assert str(tmp_path / "output" / "outline.json") in checkpoint

    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "layout": "cover",
                        "visual": "hero image",
                        "evidence": ["Exact evidence"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_qa" in checkpoint

    (output / "qa").mkdir()
    (output / "qa" / "outline_check.json").write_text(
        json.dumps(
            {
                "ok": False,
                "issues": [
                    "Missing top-level field: deck_goal",
                    "slide-01: missing layout",
                ],
            }
        ),
        encoding="utf-8",
    )
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_repair" in checkpoint
    assert "REPAIR_INPUT=" in checkpoint
    assert '"current_outline"' in checkpoint
    assert '"allowed_research_urls":[]' in checkpoint
    assert '"Missing top-level field: deck_goal"' in checkpoint
    assert "very next tool call must write one corrected outline.json" in checkpoint

    qa = output / "qa"
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}scaffold" in checkpoint
    assert "--outline" in checkpoint
    assert str(output / "outline.json") in checkpoint
    assert "--out" in checkpoint
    assert str(output / "deck.json") in checkpoint
    assert "may repeat layout ids" in checkpoint
    assert "inspector deterministically derives the ordered layout plan" in checkpoint
    assert "Do not pass layout ids, --theme, --image-mode" in checkpoint
    assert "running exactly" in checkpoint
    assert "automatically imports public outline evidence" in checkpoint
    assert "SCAFFOLD_INPUT=" in checkpoint
    assert '"registered_theme_ids"' in checkpoint
    assert '"bold-poster"' in checkpoint
    assert '"registered_layout_ids"' in checkpoint
    assert '"registered_layouts"' in checkpoint
    assert '"label":"Project case study with visual and proof metrics"' in checkpoint
    assert '"closing-next-steps-v1"' in checkpoint
    assert '"title":"Exact title"' in checkpoint
    assert "Do not read outline.json" in checkpoint

    deck_path = output / "deck.json"
    deck_path.write_text(
        '{"slides":[{"props":{"title":"输入演示标题"}}]}',
        encoding="utf-8",
    )
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch" in checkpoint
    assert "already-finalized manifest decisions" in checkpoint
    assert "do not read or edit manifest.json here" in checkpoint

    generated = output / "assets" / "generated"
    generated.mkdir(parents=True)
    (generated / "manifest.json").write_text(
        '{"image_plan":[{"slide_id":"slide-01","prop_path":"background",'
        '"decision":"generate",'
        '"output_path":"assets/generated/hero.png"}]}',
        encoding="utf-8",
    )
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}images" in checkpoint
    assert "IMAGE_INPUT=" in checkpoint
    assert '"output_path":"assets/generated/hero.png"' in checkpoint
    assert '"watermark":false' in checkpoint
    assert "very next tool call(s)" in checkpoint
    assert "Do not edit manifest.json" in checkpoint

    (generated / "hero.png").write_bytes(b"image")
    patch_path = output / "deck.patch.json"
    patch_path.write_text("{}", encoding="utf-8")
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert (
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}image_status_sync"
        in checkpoint
    )
    assert "sync_image_manifest_status.js" in checkpoint
    assert "do not list" in checkpoint
    assert "edit/read manifest.json manually" in checkpoint

    (generated / "manifest.json").write_text(
        '{"image_plan":[{"slide_id":"slide-01","prop_path":"background",'
        '"decision":"generate","status":"generated",'
        '"output_path":"assets/generated/hero.png"}]}',
        encoding="utf-8",
    )
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}apply_patch" in checkpoint
    assert "manifest.json as authoritative" in checkpoint
    assert "Do not rewrite the patch or deck first" in checkpoint

    patch_path.write_text(
        '{"slides":{"slide-01":{"props":{"media":'
        '"assets/generated/hero.png"}}}}',
        encoding="utf-8",
    )
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}apply_patch" in checkpoint
    assert "apply_deck_patch.js" in checkpoint
    assert str(output / "deck.json") in checkpoint
    assert str(output / "deck.patch.json") in checkpoint
    assert str(FINALIZER_SCRIPT.parent / "apply_deck_patch.js") in checkpoint

    deck_path.write_text(
        '{"slides":[{"id":"slide-01","source_outline_page":1,'
        '"props":{"title":"NOON"},"background":'
        '{"src":"assets/generated/hero.png","origin":"generated"}}]}',
        encoding="utf-8",
    )
    deck_mtime = max(deck_path.stat().st_mtime_ns, patch_path.stat().st_mtime_ns + 1)
    os.utime(deck_path, ns=(deck_mtime, deck_mtime))
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}finalize" in checkpoint
    assert "finalize_controlled_deck.js" in checkpoint
    assert str(tmp_path / "output" / "deck.json") in checkpoint
    assert str(tmp_path / "output" / "index.html") in checkpoint
    assert "Do not split it into individual validators" in checkpoint

    (qa / "deck_spec.json").write_text('{"ok": true}', encoding="utf-8")
    (qa / "truth_check.json").write_text(
        '{"ok": false, "issues": ["slides.slide-04.props.series"]}',
        encoding="utf-8",
    )
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}finalize" in checkpoint
    assert "REPAIR_INPUT=" not in checkpoint
    assert "qa_warnings=1" in checkpoint

    (output / "index.html").write_text("<html></html>", encoding="utf-8")
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}finalize" in checkpoint
    assert "deck contract report is missing or stale" in checkpoint

    for report_name in (
        "outline_check.json",
        "deck_contract.json",
        "deck_spec.json",
        "truth_check.json",
        "image_manifest.json",
        "html_self_check.json",
        "runtime_probe.json",
    ):
        (qa / report_name).write_text('{"ok": true}', encoding="utf-8")
    (qa / "html_self_check.json").write_text(
        '{"ok": true, "warnings": ["font slack", "wrap risk"]}',
        encoding="utf-8",
    )
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}complete" in checkpoint
    assert "qa_warnings=2" in checkpoint
    assert "active user-visible response language" in checkpoint
    assert "explicit user language request wins" in checkpoint
    assert "ui_language=zh" in checkpoint
    assert "ui_language=en" in checkpoint
    assert "ui_language=ja" in checkpoint
    assert "质量检查：7/7 项通过" in checkpoint
    assert "检查提示：2 项" in checkpoint
    assert "Quality checks: 7/7 passed" in checkpoint
    assert "Check notices: 2" in checkpoint
    assert "品質チェック：7/7 項目合格" in checkpoint
    assert "確認事項：2 件" in checkpoint
    assert "matching localized generation-details heading" in checkpoint
    assert "before the existing generated-files module" in checkpoint
    assert "deliverable card" not in checkpoint
    assert "Do not print the raw checkpoint keys qa_ok or qa_warnings" in checkpoint
    assert "deck.patch.json, qa/, and research/" in checkpoint
    assert "state that count" not in checkpoint


def test_controlled_presentation_checkpoint_ignores_other_task_artifacts(tmp_path):
    legacy_output = tmp_path / "output"
    legacy_output.mkdir()
    (legacy_output / "outline.json").write_text("{}", encoding="utf-8")
    (legacy_output / "deck.json").write_text("{}", encoding="utf-8")
    (legacy_output / "2048.html").write_text("<html></html>", encoding="utf-8")
    task_root = legacy_output / "tasks" / "task-iphone"
    task_root.mkdir(parents=True)

    checkpoint = build_checkpoint_text(
        tmp_path,
        "content_ready",
        artifact_root_dir=task_root,
    )

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in checkpoint
    assert str(task_root / "outline.json") in checkpoint
    assert str(legacy_output / "outline.json") not in checkpoint
    assert "2048.html" not in checkpoint


def test_controlled_checkpoint_does_not_reapply_older_patch_for_honest_placeholder(
    tmp_path,
):
    output = tmp_path / "output"
    qa = output / "qa"
    qa.mkdir(parents=True)
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "精选项目",
                        "message": "展示项目定位与成果。",
                        "bullets": ["项目名称待客户确认"],
                        "layout": "project case",
                        "visual": "thumbnail and metrics",
                        "evidence": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    patch_path = output / "deck.patch.json"
    patch_path.write_text(
        '{"slides":{"slide-01":{"props":{"title":"品牌项目 A（待补充）"}}}}',
        encoding="utf-8",
    )
    deck_path = output / "deck.json"
    deck_path.write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "source_outline_page": 1,
                        "props": {"title": "品牌项目 A（待补充）"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    deck_mtime = patch_path.stat().st_mtime_ns + 10_000_000
    os.utime(deck_path, ns=(deck_mtime, deck_mtime))

    checkpoint = completion_gate_progress_text(
        CompletionGate(workflow_checkpoint_kind="controlled_presentation"),
        str(tmp_path),
    )

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}finalize" in checkpoint
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}apply_patch" not in checkpoint


def test_controlled_checkpoint_refreshes_stale_deck_contract(tmp_path):
    output = tmp_path / "output"
    qa = output / "qa"
    qa.mkdir(parents=True)
    outline = output / "outline.json"
    deck = output / "deck.json"
    html = output / "index.html"
    outline.write_text('{"slides":[]}', encoding="utf-8")
    deck.write_text('{"slides":[]}', encoding="utf-8")
    html.write_text("<html></html>", encoding="utf-8")
    report_names = (
        "outline_check.json",
        "deck_contract.json",
        "deck_spec.json",
        "truth_check.json",
        "image_manifest.json",
        "html_self_check.json",
        "runtime_probe.json",
    )
    for report_name in report_names:
        (qa / report_name).write_text('{"ok":true}', encoding="utf-8")

    base = max(path.stat().st_mtime_ns for path in (outline, deck, html, *qa.iterdir()))
    os.utime(qa / "deck_contract.json", ns=(base, base))
    for dependency in (outline, deck, html):
        os.utime(dependency, ns=(base + 10_000_000, base + 10_000_000))
    for report_name in report_names:
        if report_name == "deck_contract.json":
            continue
        os.utime(
            qa / report_name,
            ns=(base + 20_000_000, base + 20_000_000),
        )

    gate = CompletionGate(workflow_checkpoint_kind="controlled_presentation")
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}finalize" in checkpoint
    assert "qa_ok=6/7" in checkpoint

    os.utime(
        qa / "deck_contract.json",
        ns=(base + 30_000_000, base + 30_000_000),
    )
    checkpoint = completion_gate_progress_text(gate, str(tmp_path))
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}complete" in checkpoint


def test_controlled_checkpoint_completes_with_current_failed_truth_report(tmp_path):
    output = tmp_path / "output"
    qa = output / "qa"
    qa.mkdir(parents=True)
    (output / "outline.json").write_text('{"slides":[]}', encoding="utf-8")
    (output / "deck.json").write_text('{"slides":[]}', encoding="utf-8")
    (output / "index.html").write_text("<html></html>", encoding="utf-8")
    for report_name in (
        "outline_check.json",
        "deck_contract.json",
        "deck_spec.json",
        "image_manifest.json",
        "html_self_check.json",
        "runtime_probe.json",
    ):
        (qa / report_name).write_text('{"ok": true}', encoding="utf-8")
    (qa / "truth_check.json").write_text(
        json.dumps(
            {
                "ok": False,
                "issues": ["unverified ranking", "unsupported number"],
            }
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    update = policy.update_checkpoint(checkpoint)

    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}complete" in update.text
    assert "qa_warnings=2" in update.text
    assert "active user-visible response language" in update.text
    assert "质量检查：7/7 项通过" in update.text
    assert "检查提示：2 项" in update.text
    assert "Quality checks: 7/7 passed" in update.text
    assert "品質チェック：7/7 項目合格" in update.text
    assert "state that count" not in update.text
    assert policy.allows_completion_continuation() is False


@pytest.mark.parametrize("truth_state", ["missing", "stale"])
def test_controlled_checkpoint_refreshes_missing_or_stale_truth_report(
    tmp_path,
    truth_state,
):
    output = tmp_path / "output"
    qa = output / "qa"
    qa.mkdir(parents=True)
    outline = output / "outline.json"
    outline.write_text('{"slides":[]}', encoding="utf-8")
    deck = output / "deck.json"
    deck.write_text('{"slides":[]}', encoding="utf-8")
    truth = qa / "truth_check.json"
    if truth_state == "stale":
        truth.write_text('{"ok": true}', encoding="utf-8")
        newer = max(deck.stat().st_mtime_ns, truth.stat().st_mtime_ns) + 10_000_000
        os.utime(deck, ns=(newer, newer))
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    deck_spec = qa / "deck_spec.json"
    deck_spec.write_text('{"ok": true}', encoding="utf-8")
    if truth_state == "stale":
        newer = max(deck.stat().st_mtime_ns, deck_spec.stat().st_mtime_ns) + 10_000_000
        os.utime(deck_spec, ns=(newer, newer))

    checkpoint = completion_gate_progress_text(
        CompletionGate(workflow_checkpoint_kind="controlled_presentation"),
        str(tmp_path),
    )

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}finalize" in checkpoint
    assert "missing or stale advisory truth report" in checkpoint
    assert "REPAIR_INPUT=" not in checkpoint


def test_controlled_content_patch_checkpoint_embeds_complete_compact_input(tmp_path):
    gate = CompletionGate(workflow_checkpoint_kind="controlled_presentation")
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "cover-hero-v1",
                        "source_outline_page": 1,
                        "props": {
                            "title": "输入演示标题",
                            "hero": "",
                            "items": [{"title": "", "body": ""}],
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    generated = output / "assets" / "generated"
    generated.mkdir(parents=True)
    (generated / "hero.png").write_bytes(b"image")
    (generated / "manifest.json").write_text(
        json.dumps(
            {
                "image_plan": [
                    {
                        "slide_id": "slide-01",
                        "prop_path": "hero",
                        "decision": "generate",
                        "status": "generated",
                        "output_path": "assets/generated/hero.png",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch" in checkpoint
    assert '"ready_media_paths":["assets/generated/hero.png"]' in checkpoint
    assert '"slide_id":"slide-01"' in checkpoint
    assert '"layout_id":"cover-hero-v1"' in checkpoint
    assert '"title":"Exact title"' in checkpoint
    assert '"title_prop_path":"title"' in checkpoint
    assert '"message":"Exact message"' in checkpoint
    assert '"bullets":["Exact bullet"]' in checkpoint
    assert '"evidence":null' in checkpoint
    assert '"disclosure_required":false' in checkpoint
    assert '"disclosure_evidence":[]' in checkpoint
    assert '"allowed_numeric_literals":[]' in checkpoint
    assert '"structural_numeric_literals":["1"]' in checkpoint
    assert '"prop_shape"' in checkpoint
    assert '"props_template":{"title":"输入演示标题"' in checkpoint
    assert '"ready_media_bindings":[{"slide_id":"slide-01","prop_path":"hero"' in checkpoint
    assert '"truth_contract":null' in checkpoint
    assert '"patch_format":"Top level must be {\\"slides\\":{...}}' in checkpoint
    assert "very next tool call must write one deck.patch.json" in checkpoint
    assert 'patch envelope must be exactly top-level {"slides":{...}}' in checkpoint
    assert "never put slide-01/slide-02 keys at the top level" in checkpoint
    assert "declared title_prop_path" in checkpoint
    assert "输入演示标题" in checkpoint


def test_controlled_incomplete_patch_stays_appendable_until_json_is_valid(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "cover-hero-v1",
                        "source_outline_page": 1,
                        "props": {"title": "输入演示标题"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )

    initial = policy.build_checkpoint()
    assert initial is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch" in initial

    patch_path = output / "deck.patch.json"
    patch_path.write_text(
        '{"slides":{"slide-01":{"props":{"title":"Exact',
        encoding="utf-8",
    )
    incomplete = policy.build_checkpoint()
    assert incomplete is not None
    assert (
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch_repair"
        in incomplete
    )
    assert "not complete, valid JSON" in incomplete
    assert "Do not run apply_deck_patch.js yet" in incomplete
    assert "PATCH_INPUT=" in incomplete
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}apply_patch" not in incomplete

    policy.update_checkpoint(incomplete)
    assert (
        policy.tool_call_error(
            "append_file",
            {"path": "deck.patch.json", "content": ' title"}}}}'},
            verified_evidence_urls=set(),
        )
        is None
    )
    blocked = policy.tool_call_error(
        "bash",
        {"command": "node apply_deck_patch.js deck.json deck.patch.json"},
        verified_evidence_urls=set(),
    )
    assert blocked is not None
    assert "CONTROLLED_PRESENTATION_PATCH_JSON_INCOMPLETE" in blocked

    with patch_path.open("a", encoding="utf-8") as stream:
        stream.write(' title"}}}}')

    complete = policy.build_checkpoint()
    assert complete is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}apply_patch" in complete
    assert "content_patch_repair" not in complete


def test_controlled_incomplete_patch_allows_bound_staged_rewrite(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        stage="content_patch_repair",
    )

    begin = {
        "action": "begin",
        "path": "deck.patch.json",
        "expected_chunks": 1,
    }
    assert (
        policy.tool_call_error(
            "staged_file_write",
            begin,
            verified_evidence_urls=set(),
        )
        is None
    )
    policy.record_tool_result(
        "staged_file_write",
        begin,
        ToolResult(
            success=True,
            content="started",
            raw_output={"write_id": "content-patch-write"},
        ),
    )

    for tool_name, arguments in (
        ("staged_file_write", begin),
        ("read_file", {"path": "deck.patch.json"}),
        (
            "write_file",
            {"path": "deck.patch.json", "content": '{"slides":{}}'},
        ),
    ):
        blocked_active = policy.tool_call_error(
            tool_name,
            arguments,
            verified_evidence_urls=set(),
        )
        assert blocked_active is not None
        assert "CONTROLLED_PRESENTATION_PATCH_STAGED_WRITE_ACTIVE" in blocked_active
        assert "write_id=content-patch-write" in blocked_active

    append = {
        "action": "append_text",
        "write_id": "content-patch-write",
        "chunk_index": 0,
        "content": '{"slides":{}}',
    }
    assert (
        policy.tool_call_error(
            "staged_file_write",
            append,
            verified_evidence_urls=set(),
        )
        is None
    )
    blocked = policy.tool_call_error(
        "staged_file_write",
        {"action": "commit", "write_id": "another-write"},
        verified_evidence_urls=set(),
    )
    assert blocked is not None
    assert "CONTROLLED_PRESENTATION_PATCH_STAGED_WRITE_ACTIVE" in blocked
    assert (
        policy.tool_call_error(
            "staged_file_write",
            {"action": "commit", "write_id": "content-patch-write"},
            verified_evidence_urls=set(),
        )
        is None
    )
    policy.record_tool_result(
        "staged_file_write",
        {"action": "abort", "write_id": "content-patch-write"},
        ToolResult(success=True, content="aborted"),
    )
    assert (
        policy.tool_call_error(
            "staged_file_write",
            begin,
            verified_evidence_urls=set(),
        )
        is None
    )


def test_controlled_incomplete_patch_repeated_begin_hits_existing_repair_fuse(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        stage="content_patch_repair",
        _content_patch_staged_write_id="content-patch-write",
    )
    begin = {
        "action": "begin",
        "path": "deck.patch.json",
        "expected_chunks": 5,
    }

    for decision_id in range(3):
        policy.begin_tool_decision(decision_id)
        error = policy.tool_call_error(
            "staged_file_write",
            begin,
            verified_evidence_urls=set(),
        )
        assert error is not None
        policy.record_tool_result(
            "staged_file_write",
            begin,
            ToolResult(success=False, error=error),
            executed=False,
        )

    assert policy.repair_stalled is True


def test_controlled_incomplete_patch_commits_without_progress_hit_repair_fuse(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    checkpoint = (
        "Internal checkpoint\n"
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch_repair\n"
        "NEXT_ACTION=repair"
    )
    policy.update_checkpoint(checkpoint)

    for _ in range(2):
        policy.record_tool_result(
            "staged_file_write",
            {"action": "commit", "write_id": "content-patch-write"},
            ToolResult(success=True, content="committed"),
        )
        update = policy.update_checkpoint(checkpoint)

    assert policy.repair_stalled is True
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled" in update.text


def test_controlled_content_patch_maps_statement_title_and_numeric_evidence(tmp_path):
    gate = CompletionGate(workflow_checkpoint_kind="controlled_presentation")
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "黄金时代：1958 年起步",
                        "message": "黄金时代建立了长期影响。",
                        "bullets": ["1930 年后仍持续积累国际经验。"],
                        "evidence": ["公开资料确认 1958 年夺冠。"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "truth_contract": {
                    "source_facts": [],
                    "research_facts": ["公开资料确认 1958 年夺冠。"],
                    "assumptions": [],
                },
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "statement-focus-v1",
                        "source_outline_page": 1,
                        "props": {
                            "eyebrow": "核心观点",
                            "statement": "在这里写下最需要被记住的结论",
                            "support": "",
                            "proofs": [],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch" in checkpoint
    assert '"title_prop_path":"statement"' in checkpoint
    assert '"evidence":["公开资料确认 1958 年夺冠。"]' in checkpoint
    assert '"allowed_numeric_literals":["1958"]' in checkpoint
    assert '"allowed_numeric_literals":["1930"' not in checkpoint
    assert '"structural_numeric_literals":["1"]' in checkpoint
    assert '"research_facts":["公开资料确认 1958 年夺冠。"]' in checkpoint
    assert "Do not translate Chinese number words into new Arabic metrics" in checkpoint
    assert "chart_data_policy" not in checkpoint


def test_user_provided_content_patch_allows_page_copy_quantities_without_links(
    tmp_path,
):
    outline_path = tmp_path / "outline.json"
    outline_path.write_text(
        json.dumps(
            {
                "source_mode": "user_provided",
                "slides": [
                    {
                        "page": 1,
                        "title": "改进结果",
                        "message": "首次响应时间从 18 分钟降到 7 分钟。",
                        "bullets": ["一次解决率从 68% 提升到 81%"],
                        "evidence": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    deck_path = tmp_path / "deck.json"
    deck_path.write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "chart-data-v1",
                        "source_outline_page": 1,
                        "props": {"title": "输入图表标题", "categories": [], "series": []},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = _content_patch_input(outline_path, deck_path, ())

    assert payload is not None
    page = json.loads(payload)["pages"][0]
    assert page["allowed_numeric_literals"] == ["18", "7", "68%", "81%"]
    assert page["chart_data_policy"] == (
        "Every included series must have a real numeric value for every included "
        "category. Keep the strongest complete factual subset; move isolated metrics "
        "to highlights, insight, or subtitle. Never pad gaps with 0, placeholders, or "
        "an invented baseline/forecast."
    )


def test_controlled_content_patch_requires_visible_missing_fact_disclosure(tmp_path):
    gate = CompletionGate(workflow_checkpoint_kind="controlled_presentation")
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "团队能力结构",
                        "message": "按关键职能展示团队能力方向。",
                        "bullets": ["AI", "制造业", "销售交付"],
                        "evidence": [
                            "团队具体成员姓名与履历未提供，仅按能力模块示意。"
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "cards-grid-v1",
                        "source_outline_page": 1,
                        "props": {
                            "title": "输入页面标题",
                            "subtitle": "",
                            "items": [],
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch" in checkpoint
    assert '"disclosure_required":true' in checkpoint
    assert (
        '"disclosure_evidence":["团队具体成员姓名与履历未提供，仅按能力模块示意。"]'
        in checkpoint
    )
    assert "visibly include its supplied disclosure_evidence" in checkpoint
    assert "never promote a missing private fact to positive copy" in checkpoint


def test_controlled_image_policy_rebase_precedes_stale_image_checkpoint(tmp_path):
    output = tmp_path / "output"
    generated = output / "assets" / "generated"
    generated.mkdir(parents=True)
    (output / "outline.json").write_text(
        json.dumps({"slides": []}),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "title": "Deck title",
                "theme_id": "blue-professional",
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "image-hero-split-v1",
                        "props": {
                            "eyebrow": "CASE",
                            "title": "Exact title",
                            "body": "Exact message",
                            "image": {"src": "assets/generated/hero.png"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (generated / "manifest.json").write_text(
        json.dumps(
            {
                "mode": "auto",
                "generation_forbidden": False,
                "image_plan": [
                    {
                        "slide": 1,
                        "slide_id": "slide-01",
                        "layout_id": "image-hero-split-v1",
                        "slot": "image",
                        "prop_path": "image",
                        "required": True,
                        "decision": "generate",
                        "status": "pending",
                        "output_path": "assets/generated/hero.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    gate = CompletionGate(
        workflow_checkpoint_kind="controlled_presentation",
        workflow_options={
            IMAGE_GENERATION_POLICY_OPTION: IMAGE_GENERATION_FORBIDDEN,
        },
    )

    checkpoint = completion_gate_progress_text(gate, str(tmp_path))

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}image_policy_rebase" in checkpoint
    assert "rebase_image_policy.js" in checkpoint
    assert "IMAGE_INPUT=" not in checkpoint


def test_controlled_persisted_auto_image_401_rebases_new_policy_instance(tmp_path):
    output = tmp_path / "output"
    generated = output / "assets" / "generated"
    generated.mkdir(parents=True)
    (output / "outline.json").write_text(
        json.dumps({"slides": []}),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text('{"slides": []}', encoding="utf-8")
    manifest = generated / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "mode": "auto",
                "generation_forbidden": False,
                "image_service": {
                    "status": "blocked",
                    "reason": "authorization_401",
                },
                "image_plan": [
                    {
                        "decision": "generate",
                        "status": "pending",
                        "required": True,
                        "output_path": "assets/generated/hero.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}image_policy_rebase" in checkpoint
    assert "--policy unavailable" in checkpoint
    assert "IMAGE_INPUT=" not in checkpoint


def test_controlled_persisted_creative_image_401_blocks_new_policy_instance(tmp_path):
    output = tmp_path / "output"
    generated = output / "assets" / "generated"
    generated.mkdir(parents=True)
    (output / "outline.json").write_text('{"slides": []}', encoding="utf-8")
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text('{"slides": []}', encoding="utf-8")
    (generated / "manifest.json").write_text(
        json.dumps(
            {
                "mode": "creative_image_mode",
                "generation_forbidden": False,
                "image_service": {
                    "status": "blocked",
                    "reason": "authorization_401",
                },
                "image_plan": [
                    {
                        "decision": "generate",
                        "status": "pending",
                        "required": True,
                        "output_path": "assets/generated/hero.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}image_auth_blocked" in checkpoint
    assert "IMAGE_INPUT=" not in checkpoint


def test_persisted_no_image_policy_outlives_blocked_image_service(tmp_path):
    output = tmp_path / "output"
    generated = output / "assets" / "generated"
    generated.mkdir(parents=True)
    (output / "outline.json").write_text(
        json.dumps({"slides": []}),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text('{"slides": []}', encoding="utf-8")
    (generated / "manifest.json").write_text(
        json.dumps(
            {
                "mode": "auto",
                "generation_forbidden": True,
                "image_service": {
                    "status": "blocked",
                    "reason": "authorization_401",
                },
                "image_plan": [
                    {
                        "decision": "skip",
                        "status": "skipped",
                        "required": False,
                        "output_path": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        research_mode="content_ready",
    )
    checkpoint = policy.build_checkpoint()

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}image_auth_blocked" not in checkpoint
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}images" not in checkpoint


@pytest.mark.asyncio
async def test_controlled_image_input_blocks_reinspection(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "visual": "right hero",
                        "evidence": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "title": "Deck title",
                "theme_id": "blue-professional",
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "cover-hero-v1",
                        "props": {"title": "输入演示标题"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    generated = output / "assets" / "generated"
    generated.mkdir(parents=True)
    (generated / "manifest.json").write_text(
        json.dumps(
            {
                "image_plan": [
                    {
                        "slide": 1,
                        "slide_id": "slide-01",
                        "layout_id": "cover-hero-v1",
                        "slot": "hero",
                        "prop_path": "hero",
                        "decision": "generate",
                        "status": "pending",
                        "output_path": "assets/generated/hero.png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    checkpoint = completion_gate_progress_text(
        CompletionGate(workflow_checkpoint_kind="controlled_presentation"),
        str(tmp_path),
    )
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}images" in checkpoint
    assert '"preferred_ratio":"4:3"' in checkpoint
    assert '"primary":"#1E2BFA"' in checkpoint
    assert '"visual_intent":"right hero"' in checkpoint
    llm = MockLLM(
        [
            LLMResponse(
                content="read manifest",
                tool_calls=[
                    ToolCall(
                        id="read-manifest",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "assets/generated/manifest.json"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    read_tool = CountingReadTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"read_file": read_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert read_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "read_file"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_IMAGE_INPUT_READY" in (blocked.error or "")


@pytest.mark.asyncio
async def test_controlled_image_status_sync_blocks_manual_manifest_edit(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "evidence": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        '{"slides":[{"id":"slide-01","props":{"title":"输入演示标题"}}]}',
        encoding="utf-8",
    )
    generated = output / "assets" / "generated"
    generated.mkdir(parents=True)
    (generated / "hero.png").write_bytes(b"image")
    (generated / "manifest.json").write_text(
        '{"image_plan":[{"decision":"generate","status":"pending",'
        '"output_path":"assets/generated/hero.png"}]}',
        encoding="utf-8",
    )
    edit_tool = NamedEchoTool("edit_file")
    llm = MockLLM(
        [
            LLMResponse(
                content="edit manifest",
                tool_calls=[
                    ToolCall(
                        id="edit-manifest",
                        type="function",
                        function=FunctionCall(
                            name="edit_file",
                            arguments={
                                "path": "assets/generated/manifest.json",
                                "old_str": '"status":"pending"',
                                "new_str": '"status":"generated"',
                                "timeout": 120,
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"edit_file": edit_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert edit_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "edit_file"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_IMAGE_STATUS_SYNC_REQUIRED" in (
        blocked.error or ""
    )


def test_controlled_image_status_sync_allows_windows_separator_command(tmp_path):
    output = tmp_path / "output"
    manifest = output / "assets" / "generated" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"image_plan":[]}', encoding="utf-8")
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        research_mode="content_ready",
        stage="image_status_sync",
    )

    def windows_path(path: Path) -> str:
        return str(path).replace("/", "\\")

    command = (
        f"cd {shlex.quote(windows_path(output))} && node "
        f"{shlex.quote(windows_path(SYNC_IMAGE_STATUS_SCRIPT))} "
        f"{shlex.quote(windows_path(manifest))}"
    )

    assert policy.tool_call_error(
        "bash",
        {"command": command},
        verified_evidence_urls=set(),
    ) is None
    assert policy.tool_call_error(
        "bash",
        {"command": f"{command} && echo bypass"},
        verified_evidence_urls=set(),
    ) is not None


def test_controlled_image_status_sync_allows_its_deterministic_action(tmp_path):
    output = tmp_path / "output"
    manifest = output / "assets" / "generated" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"image_plan":[]}', encoding="utf-8")
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        research_mode="content_ready",
        stage="image_status_sync",
    )

    action = policy.next_deterministic_action()

    assert action is not None
    assert action.capability == "controlled_presentation.image_status_sync"
    assert policy.tool_call_error(
        action.tool_name,
        action.arguments,
        verified_evidence_urls=set(),
    ) is None


def test_controlled_image_status_policy_rejections_trip_repair_fuse(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=tmp_path / "output",
        research_mode="content_ready",
        stage="image_status_sync",
    )

    for decision_id in range(1, 4):
        policy.begin_tool_decision(decision_id)
        blocked = policy.tool_call_error(
            "read_file",
            {"path": "assets/generated/manifest.json"},
            verified_evidence_urls=set(),
        )
        assert blocked is not None
        policy.record_tool_result(
            "read_file",
            {"path": "assets/generated/manifest.json"},
            ToolResult(success=False, error=blocked),
            executed=False,
        )

    assert policy.repair_stalled is True


@pytest.mark.asyncio
async def test_controlled_content_patch_blocks_reinspection_when_patch_input_ready(
    tmp_path,
):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "cover-hero-v1",
                        "source_outline_page": 1,
                        "props": {"title": "输入演示标题", "hero": ""},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="inspect again",
                tool_calls=[
                    ToolCall(
                        id="read-deck",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "deck.json"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    read_tool = CountingReadTool()
    gate = CompletionGate(
        workflow_checkpoint_kind="controlled_presentation",
        max_continuations=0,
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"read_file": read_tool},
            max_steps=5,
            completion_gate=gate,
            workspace_dir=str(tmp_path),
        )
    )

    assert read_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "read_file"
    )
    assert blocked.success is False
    assert blocked.user_visible is False
    assert "CONTROLLED_PRESENTATION_PATCH_INPUT_READY" in (blocked.error or "")
    assert "Write deck.patch.json now" in (blocked.error or "")


@pytest.mark.asyncio
async def test_controlled_outline_allows_unverified_evidence_urls(tmp_path):
    (tmp_path / "output").mkdir()
    outline = {
        "deck_goal": "梳理巴西足球历史",
        "audience": "普通观众",
        "source_mode": "public_authoritative_research",
        "storyline": "从起点到当代",
        "slides": [
            {
                "page": 1,
                "title": "巴西足球历史",
                "message": "建立公开资料叙事",
                "bullets": ["以世界杯历史为主线"],
                "layout": "cover",
                "visual": "历史封面",
                "evidence": [
                    "World Cup history | FIFA | "
                    "https://www.fifa.com/en/tournaments/mens/worldcup"
                ],
            }
        ],
    }
    llm = MockLLM(
        [
            LLMResponse(
                content="write outline",
                tool_calls=[
                    ToolCall(
                        id="write-outline",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "outline.json",
                                "content": json.dumps(outline, ensure_ascii=False),
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    write_tool = CountingWriteTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"write_file": write_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert write_tool.calls == 1
    written = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "write-outline"
    )
    assert written.success is True


@pytest.mark.asyncio
async def test_controlled_outline_accepts_urls_from_validated_research_handoff(tmp_path):
    output = tmp_path / "output"
    research = output / "research"
    research.mkdir(parents=True)
    source_url = "https://www.fifa.com/en/tournaments/mens/worldcup"
    for index in range(1, 4):
        (research / f"worldcup_dim{index:02d}.md").write_text(
            f"dimension {index}\n\n[^source]: FIFA. {source_url}\n",
            encoding="utf-8",
        )
    (research / "worldcup_cross_verification.md").write_text(
        f"cross verification\n\n[^source]: FIFA. {source_url}\n",
        encoding="utf-8",
    )
    (research / "worldcup_insight.md").write_text(
        f"insight\n\n[^source]: FIFA. {source_url}\n",
        encoding="utf-8",
    )
    _write_valid_research_report(research, topic="worldcup")
    outline = {
        "deck_goal": "梳理巴西足球历史",
        "audience": "普通观众",
        "source_mode": "public_authoritative_research",
        "storyline": "从起点到当代",
        "slides": [
            {
                "page": 1,
                "title": "巴西足球历史",
                "message": "建立公开资料叙事",
                "bullets": ["以世界杯历史为主线"],
                "layout": "cover",
                "visual": "历史封面",
                "evidence": [f"World Cup history | FIFA | {source_url}"],
            }
        ],
    }
    llm = MockLLM(
        [
            LLMResponse(
                content="write outline",
                tool_calls=[
                    ToolCall(
                        id="write-outline",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "outline.json",
                                "content": json.dumps(outline, ensure_ascii=False),
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    write_tool = CountingWriteTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"write_file": write_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                workflow_options={"research_mode": "deep"},
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert write_tool.calls == 1
    written = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "write-outline"
    )
    assert written.success is True


@pytest.mark.asyncio
async def test_validated_research_handoff_blocks_backward_discovery(tmp_path):
    output = tmp_path / "output"
    research = output / "research"
    research.mkdir(parents=True)
    for index in range(1, 4):
        (research / f"topic_dim{index:02d}.md").write_text(
            f"dimension {index}",
            encoding="utf-8",
        )
    (research / "topic_cross_verification.md").write_text(
        "cross verification",
        encoding="utf-8",
    )
    (research / "topic_insight.md").write_text("insight", encoding="utf-8")
    _write_valid_research_report(research, topic="topic")
    todo = NamedEchoTool("todo_write")
    llm = MockLLM(
        [
            LLMResponse(
                content="update plan",
                tool_calls=[
                    ToolCall(
                        id="todo-after-research",
                        type="function",
                        function=FunctionCall(
                            name="todo_write",
                            arguments={"text": "make outline"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"todo_write": todo},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                workflow_options={"research_mode": "deep"},
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert todo.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "todo-after-research"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_RESEARCH_HANDOFF_READY" in (blocked.error or "")


@pytest.mark.asyncio
async def test_controlled_outline_repair_allows_unverified_evidence_urls(tmp_path):
    output = tmp_path / "output"
    qa = output / "qa"
    qa.mkdir(parents=True)
    (output / "outline.json").write_text(
        json.dumps(
            {
                "deck_goal": "梳理巴西足球历史",
                "audience": "普通观众",
                "source_mode": "public_authoritative_research",
                "storyline": "从起点到当代",
                "slides": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (qa / "outline_check.json").write_text(
        json.dumps({"ok": False, "issues": ["missing evidence URL"]}),
        encoding="utf-8",
    )
    repaired_outline = {
        "deck_goal": "梳理巴西足球历史",
        "audience": "普通观众",
        "source_mode": "public_authoritative_research",
        "storyline": "从起点到当代",
        "slides": [
            {
                "page": 1,
                "title": "巴西足球历史",
                "message": "建立公开资料叙事",
                "bullets": ["以世界杯历史为主线"],
                "layout": "cover",
                "visual": "历史封面",
                "evidence": [
                    "World Cup history | FIFA | "
                    "https://www.fifa.com/en/tournaments/mens/worldcup"
                ],
            }
        ],
    }
    llm = MockLLM(
        [
            LLMResponse(
                content="repair outline",
                tool_calls=[
                    ToolCall(
                        id="repair-outline",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "output/outline.json",
                                "content": json.dumps(
                                    repaired_outline,
                                    ensure_ascii=False,
                                ),
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    write_tool = CountingWriteTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"write_file": write_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert write_tool.calls == 1
    written = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "repair-outline"
    )
    assert written.success is True


@pytest.mark.asyncio
async def test_controlled_outline_allows_user_supplied_evidence_url(tmp_path):
    (tmp_path / "output").mkdir()
    source_url = "https://www.fifa.com/en/tournaments/mens/worldcup"
    outline = {
        "deck_goal": "梳理巴西足球历史",
        "audience": "普通观众",
        "source_mode": "public_authoritative_research",
        "storyline": "从起点到当代",
        "slides": [
            {
                "page": 1,
                "title": "巴西足球历史",
                "message": "建立公开资料叙事",
                "bullets": ["以世界杯历史为主线"],
                "layout": "cover",
                "visual": "历史封面",
                "evidence": [f"World Cup history | FIFA | {source_url}"],
            }
        ],
    }
    llm = MockLLM(
        [
            LLMResponse(
                content="write outline",
                tool_calls=[
                    ToolCall(
                        id="write-outline",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "outline.json",
                                "content": json.dumps(outline, ensure_ascii=False),
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    write_tool = CountingWriteTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=[
                Message(role="system", content="sys"),
                Message(role="user", content=f"Use this source: {source_url}"),
            ],
            tools={"write_file": write_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert write_tool.calls == 1
    written = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "write-outline"
    )
    assert written.success is True


@pytest.mark.asyncio
async def test_controlled_scaffold_input_blocks_outline_reread(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "layout": "cover",
                        "visual": "hero image",
                        "evidence": ["Exact evidence"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    llm = MockLLM(
        [
            LLMResponse(
                content="read outline again",
                tool_calls=[
                    ToolCall(
                        id="read-outline",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "outline.json"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    read_tool = CountingReadTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"read_file": read_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert read_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "read_file"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_SCAFFOLD_INPUT_READY" in (blocked.error or "")


@pytest.mark.asyncio
async def test_controlled_scaffold_blocks_layout_ids_and_optional_flags(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "layout": "cover",
                        "visual": "hero image",
                        "evidence": ["Exact evidence"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    llm = MockLLM(
        [
            LLMResponse(
                content="guess ids",
                tool_calls=[
                    ToolCall(
                        id="bad-scaffold",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={
                                "command": (
                                    "node inspect_deck_contract.js cover-hero-v1 "
                                    "closing-summary-v1 --theme pulse-gradient "
                                    "--outline outline.json --out deck.json"
                                )
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    bash_tool = CountingBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "bash"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_SCAFFOLD_INPUT_READY" in (blocked.error or "")
    assert "only --outline outline.json and --out deck.json" in (blocked.error or "")
    assert "one physical command line using `cd <artifact-root> &&`" in (
        blocked.error or ""
    )


@pytest.mark.asyncio
async def test_controlled_scaffold_is_dispatched_by_runtime_before_model(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "layout": "cover",
                        "visual": "hero image",
                        "evidence": ["Exact evidence"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    llm = MockLLM([_final("done")])
    bash_tool = RuntimeOwnedScaffoldBashTool(output)

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.calls == 1
    assert llm._idx == 1
    assert bash_tool.commands == [
        "cd "
        f"{shlex.quote(str(output.resolve()))} && "
        "${BOX_AGENT_NODE:-node} "
        f"{shlex.quote(str(INSPECTOR_SCRIPT))} "
        "--outline outline.json --out deck.json"
    ]
    start = next(
        event
        for event in events
        if isinstance(event, ToolCallStart) and event.tool_name == "bash"
    )
    assert start.tool_call_id.startswith("workflow_")
    result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == start.tool_call_id
    )
    assert result.success is True


@pytest.mark.asyncio
async def test_controlled_scaffold_allows_minimal_contract_call(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "layout": "cover",
                        "visual": "hero image",
                        "evidence": ["Exact evidence"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    command = (
        "cd output && node "
        f"{INSPECTOR_SCRIPT} --outline outline.json --out deck.json"
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="use automatic theme selection",
                tool_calls=[
                    ToolCall(
                        id="auto-theme-scaffold",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    bash_tool = CountingBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.calls == 1
    result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "auto-theme-scaffold"
    )
    assert result.success is True


@pytest.mark.parametrize(
    "command",
    [
        f"node {INSPECTOR_SCRIPT} --outline outline.json --out deck.json",
        (
            "cd output &&\nnode "
            f"{INSPECTOR_SCRIPT} --outline outline.json --out deck.json"
        ),
    ],
)
def test_controlled_scaffold_requires_single_line_cd_prefix(command):
    policy = ControlledPresentationPolicy(
        workspace_dir=None,
        artifact_root_dir=None,
        stage="scaffold",
        has_scaffold_input=True,
        scaffold_input={"image_generation_policy": "auto"},
    )

    error = policy.tool_call_error(
        "bash",
        {"command": command},
        verified_evidence_urls=set(),
    )

    assert error is not None
    assert "CONTROLLED_PRESENTATION_SCAFFOLD_INPUT_READY" in error


def test_controlled_scaffold_runtime_action_preserves_forbidden_image_policy(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text('{"slides": []}', encoding="utf-8")
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        stage="scaffold",
        has_scaffold_input=True,
        scaffold_input={"image_generation_policy": "forbidden_by_user"},
    )

    action = policy.next_deterministic_action()

    assert action is not None
    assert action.capability == "controlled_presentation.scaffold"
    assert action.arguments["command"].endswith(
        "--outline outline.json --out deck.json --no-images"
    )
    assert (
        policy.tool_call_error(
            action.tool_name,
            action.arguments,
            verified_evidence_urls=set(),
        )
        is None
    )
    assert policy.next_deterministic_action() is None


@pytest.mark.asyncio
async def test_controlled_scaffold_blocks_compound_shell_mutation(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "layout": "cover",
                        "visual": "hero image",
                        "evidence": ["Exact evidence"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    compound_command = (
        "node -e 'const fs=require(\"fs\"); const o=JSON.parse("
        "fs.readFileSync(\"outline.json\")); o.slides=[]' && node "
        f"{INSPECTOR_SCRIPT} cover-hero-v1 --theme blue-professional "
        "--outline outline.json --out deck.json"
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="mutate then scaffold",
                tool_calls=[
                    ToolCall(
                        id="compound-scaffold",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": compound_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    bash_tool = CountingBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "compound-scaffold"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_SCAFFOLD_INPUT_READY" in (blocked.error or "")


@pytest.mark.asyncio
async def test_controlled_scaffold_stalls_after_three_trace_shaped_pipe_rejections(
    tmp_path,
):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "新能源汽车本土品牌市场动态分析",
                        "message": "市场动态总览",
                        "bullets": ["销量与份额"],
                        "layout": "cover",
                        "visual": "editorial cover",
                        "evidence": ["用户提供的汇报主题"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    layout_ids = (
        "cover-editorial-v1 kpi-grid-v1 chart-data-v1 chart-bar-v1 "
        "chart-data-v1 timeline-horizontal-v1 project-case-study-v1 "
        "kpi-grid-v1 closing-next-steps-v1"
    )
    commands = [
        (
            "cd output && node "
            f"{INSPECTOR_SCRIPT} {layout_ids} --theme auto --image-mode auto "
            "--outline outline.json "
            '--title "新能源汽车本土品牌市场动态分析" '
            '--fact "用于市场战略分析和行业汇报" '
            f"--out deck.json 2>&1 | tail -{tail_lines}"
        )
        for tail_lines in (60, 100, 200, 300)
    ]
    llm = MockLLM(
        [
            LLMResponse(
                content=f"scaffold attempt {attempt}",
                tool_calls=[
                    ToolCall(
                        id=f"piped-scaffold-{attempt}",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": command},
                        ),
                    )
                ],
                finish_reason="tool",
            )
            for attempt, command in enumerate(commands, start=1)
        ]
        + [_final("Stopped after repeated scaffold policy rejection.")]
    )
    bash_tool = CountingBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=7,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.calls == 0
    for attempt in range(1, 4):
        rejected = next(
            event
            for event in events
            if isinstance(event, ToolCallResult)
            and event.tool_call_id == f"piped-scaffold-{attempt}"
        )
        assert "CONTROLLED_PRESENTATION_SCAFFOLD_INPUT_READY" in (
            rejected.error or ""
        )
        assert "remove the entire pipe or redirection" in (rejected.error or "")
    fourth = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "piped-scaffold-4"
    )
    assert "CONTROLLED_PRESENTATION_REPAIR_STALLED" in (fourth.error or "")
    assert any(
        isinstance(event, InjectedMessageEvent)
        and f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled"
        in event.content
        for event in events
    )


@pytest.mark.asyncio
async def test_controlled_scaffold_stops_repeated_identical_failure(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "layout": "cover",
                        "visual": "hero image",
                        "evidence": ["Exact evidence"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    scaffold_command = (
        "cd output && node "
        f"{INSPECTOR_SCRIPT} --outline outline.json --out deck.json"
    )
    llm = MockLLM(
        [
            LLMResponse(
                content=f"scaffold {attempt}",
                tool_calls=[
                    ToolCall(
                        id=f"scaffold-{attempt}",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": scaffold_command},
                        ),
                    )
                ],
                finish_reason="tool",
            )
            for attempt in range(1, 4)
        ]
        + [_final("Stopped after repeated internal validation failure.")]
    )
    bash_tool = RepeatingScaffoldFailureBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=6,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=3,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.scaffold_calls == 2
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "scaffold-3"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_REPAIR_STALLED" in (blocked.error or "")
    assert any(
        isinstance(event, InjectedMessageEvent)
        and f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled"
        in event.content
        for event in events
    )
    assert any(
        isinstance(event, InjectedMessageEvent)
        and f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled"
        in event.content
        for event in events
    )


@pytest.mark.asyncio
async def test_controlled_apply_patch_stops_repeated_identical_failure(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "layout": "cover",
                        "visual": "hero image",
                        "evidence": ["Exact evidence"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    deck_path = output / "deck.json"
    deck_path.write_text(
        '{"slides":[{"id":"slide-01","source_outline_page":1,'
        '"props":{"title":"输入演示标题"}}]}',
        encoding="utf-8",
    )
    patch_path = output / "deck.patch.json"
    patch_path.write_text(
        '{"slides":{"slide-01":{"background":{"image":{"src":'
        '"assets/generated/hero.png"},"treatment":"wash-light"}}}}',
        encoding="utf-8",
    )
    newer = max(deck_path.stat().st_mtime_ns, patch_path.stat().st_mtime_ns) + 10_000_000
    os.utime(patch_path, ns=(newer, newer))
    apply_command = (
        "cd output && node "
        f"{APPLY_PATCH_SCRIPT} deck.json deck.patch.json"
    )
    llm = MockLLM(
        [
            LLMResponse(
                content=f"apply {attempt}",
                tool_calls=[
                    ToolCall(
                        id=f"apply-{attempt}",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": apply_command},
                        ),
                    )
                ],
                finish_reason="tool",
            )
            for attempt in range(1, 4)
        ]
        + [_final("Stopped after repeated patch failure.")]
    )
    bash_tool = RepeatingApplyPatchFailureBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=6,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=3,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.apply_patch_calls == 2
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "apply-3"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_REPAIR_STALLED" in (blocked.error or "")


@pytest.mark.asyncio
async def test_controlled_apply_patch_accepts_checkpoint_absolute_artifact_paths(
    tmp_path,
):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        '{"slides":[{"page":1,"title":"Exact title","message":"Exact message",'
        '"bullets":["Exact bullet"],"layout":"cover","visual":"hero",'
        '"evidence":["Exact evidence"]}]}',
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    deck_path = output / "deck.json"
    deck_path.write_text(
        '{"slides":[{"id":"slide-01","source_outline_page":1,'
        '"props":{"title":"输入演示标题"}}]}',
        encoding="utf-8",
    )
    patch_path = output / "deck.patch.json"
    patch_path.write_text(
        '{"slides":{"slide-01":{"props":{"title":"Exact title"}}}}',
        encoding="utf-8",
    )
    newer = max(deck_path.stat().st_mtime_ns, patch_path.stat().st_mtime_ns) + 10_000_000
    os.utime(patch_path, ns=(newer, newer))
    apply_command = (
        f"${{BOX_AGENT_NODE:-node}} {APPLY_PATCH_SCRIPT} "
        f"{deck_path} {patch_path} 2>&1"
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="apply checkpoint patch",
                tool_calls=[
                    ToolCall(
                        id="apply-absolute",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": apply_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    bash_tool = CountingBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=4,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
            artifact_root_dir=output,
        )
    )

    result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "apply-absolute"
    )
    assert result.success is True
    assert bash_tool.calls == 1


def test_controlled_apply_patch_allows_only_safe_trailing_stderr_merge(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        stage="apply_patch",
    )
    base = f"node {APPLY_PATCH_SCRIPT} {output / 'deck.json'} {output / 'deck.patch.json'}"

    assert policy.tool_call_error(
        "bash",
        {"command": f"{base} 2>&1"},
        verified_evidence_urls=set(),
    ) is None
    rejected = policy.tool_call_error(
        "bash",
        {"command": f"{base} 2>&1 | tail -20"},
        verified_evidence_urls=set(),
    )

    assert rejected is not None
    assert "CONTROLLED_PRESENTATION_APPLY_PATCH_REQUIRED" in rejected


def test_controlled_apply_patch_stops_repeated_policy_rejection(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        stage="apply_patch",
    )
    arguments = {
        "command": (
            f"node {APPLY_PATCH_SCRIPT} "
            f"{output / 'deck.json'} {tmp_path / 'wrong' / 'deck.patch.json'}"
        )
    }

    for decision_id in range(3):
        policy.begin_tool_decision(decision_id)
        error = policy.tool_call_error(
            "bash",
            arguments,
            verified_evidence_urls=set(),
        )
        assert error is not None
        assert "CONTROLLED_PRESENTATION_APPLY_PATCH_REQUIRED" in error
        policy.record_tool_result(
            "bash",
            arguments,
            ToolResult(success=False, error=error),
            executed=False,
        )

    assert policy.repair_stalled is True
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    update = policy.update_checkpoint(checkpoint)
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled" in update.text
    assert "CONTROLLED_PRESENTATION_REPAIR_STALLED" in (
        policy.tool_call_error(
            "bash",
            arguments,
            verified_evidence_urls=set(),
        )
        or ""
    )


@pytest.mark.asyncio
async def test_controlled_apply_patch_allows_targeted_patch_repair_after_failure(
    tmp_path,
):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "layout": "cover",
                        "visual": "hero image",
                        "evidence": ["Exact evidence"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    deck_path = output / "deck.json"
    deck_path.write_text(
        '{"slides":[{"id":"slide-01","source_outline_page":1,'
        '"props":{"title":"Exact title"}}]}',
        encoding="utf-8",
    )
    patch_path = output / "deck.patch.json"
    patch_path.write_text(
        '{"slides":{"slide-01":{"props":{"title":"Unsupported title"}}}}',
        encoding="utf-8",
    )
    newer = max(deck_path.stat().st_mtime_ns, patch_path.stat().st_mtime_ns) + 10_000_000
    os.utime(patch_path, ns=(newer, newer))
    apply_command = (
        "cd output && node "
        f"{APPLY_PATCH_SCRIPT} deck.json deck.patch.json"
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="apply patch",
                tool_calls=[
                    ToolCall(
                        id="apply-fails",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": apply_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="inspect named patch field",
                tool_calls=[
                    ToolCall(
                        id="read-patch",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "deck.patch.json"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="repair named patch field",
                tool_calls=[
                    ToolCall(
                        id="write-patch",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "deck.patch.json",
                                "content": (
                                    '{"slides":{"slide-01":{"props":'
                                    '{"title":"Exact title"}}}}'
                                ),
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="apply repaired patch",
                tool_calls=[
                    ToolCall(
                        id="apply-succeeds",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": apply_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("Patch repair completed."),
        ]
    )
    bash_tool = RepairableApplyPatchBashTool(output)
    read_tool = CountingReadTool()
    write_tool = ArtifactWriteTool(output)

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={
                "bash": bash_tool,
                "read_file": read_tool,
                "write_file": write_tool,
            },
            max_steps=6,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.apply_patch_calls == 2
    assert read_tool.calls == 1
    assert write_tool.calls == 1
    assert next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "read-patch"
    ).success is True
    assert next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "write-patch"
    ).success is True
    assert not any(
        isinstance(event, InjectedMessageEvent)
        and f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled"
        in event.content
        for event in events
    )


@pytest.mark.asyncio
async def test_controlled_apply_patch_allows_targeted_edit_after_failure(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        '{"slides":[{"page":1,"title":"Exact title","message":"Exact message",'
        '"bullets":["Exact bullet"],"layout":"cover","visual":"hero",'
        '"evidence":["Exact evidence"]}]}',
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    deck_path = output / "deck.json"
    deck_path.write_text(
        '{"slides":[{"id":"slide-01","source_outline_page":1,\n'
        '"props":{"title":"Exact title"}}]}',
        encoding="utf-8",
    )
    patch_path = output / "deck.patch.json"
    patch_path.write_text(
        '{"slides":{"slide-01":{"props":{"title":"Too long"}}}}',
        encoding="utf-8",
    )
    newer = max(deck_path.stat().st_mtime_ns, patch_path.stat().st_mtime_ns) + 10_000_000
    os.utime(patch_path, ns=(newer, newer))
    apply_command = f"cd output && node {APPLY_PATCH_SCRIPT} deck.json deck.patch.json"
    llm = MockLLM(
        [
            LLMResponse(
                content="apply patch",
                tool_calls=[
                    ToolCall(
                        id="apply-fails",
                        type="function",
                        function=FunctionCall(
                            name="bash", arguments={"command": apply_command}
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="edit named field",
                tool_calls=[
                    ToolCall(
                        id="edit-patch",
                        type="function",
                        function=FunctionCall(
                            name="edit_file",
                            arguments={
                                "path": "deck.patch.json",
                                "old_str": '"title":"Too long"',
                                "new_str": '"title":"Exact title"',
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="apply repaired patch",
                tool_calls=[
                    ToolCall(
                        id="apply-succeeds",
                        type="function",
                        function=FunctionCall(
                            name="bash", arguments={"command": apply_command}
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("Patch repair completed."),
        ]
    )
    bash_tool = RepairableApplyPatchBashTool(output)
    edit_tool = CountingEditTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool, "edit_file": edit_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.apply_patch_calls == 2
    assert edit_tool.calls == 1
    assert next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "edit-patch"
    ).success is True
    assert not any(
        isinstance(event, InjectedMessageEvent)
        and f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled"
        in event.content
        for event in events
    )


@pytest.mark.asyncio
async def test_controlled_apply_patch_keeps_repair_open_for_all_named_edits(
    tmp_path,
):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        '{"slides":[{"page":1,"title":"Exact title","message":"Exact message",'
        '"bullets":["Exact bullet"],"layout":"cover","visual":"hero",'
        '"evidence":["Exact evidence"]}]}',
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    deck_path = output / "deck.json"
    deck_path.write_text(
        '{"slides":[{"id":"slide-01","source_outline_page":1,\n'
        '"props":{"title":"Exact title","subtitle":"Exact subtitle"}}]}',
        encoding="utf-8",
    )
    patch_path = output / "deck.patch.json"
    patch_path.write_text(
        '{"slides":{"slide-01":{"props":{"title":"Unsupported title",'
        '"subtitle":"Unsupported subtitle"}}}}',
        encoding="utf-8",
    )
    newer = max(deck_path.stat().st_mtime_ns, patch_path.stat().st_mtime_ns) + 10_000_000
    os.utime(patch_path, ns=(newer, newer))
    apply_command = f"cd output && node {APPLY_PATCH_SCRIPT} deck.json deck.patch.json"
    llm = MockLLM(
        [
            LLMResponse(
                content="apply patch",
                tool_calls=[
                    ToolCall(
                        id="apply-fails",
                        type="function",
                        function=FunctionCall(
                            name="bash", arguments={"command": apply_command}
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="repair every named field",
                tool_calls=[
                    ToolCall(
                        id="edit-title",
                        type="function",
                        function=FunctionCall(
                            name="edit_file",
                            arguments={
                                "path": "deck.patch.json",
                                "old_str": '"title":"Unsupported title"',
                                "new_str": '"title":"Exact title"',
                            },
                        ),
                    ),
                    ToolCall(
                        id="edit-subtitle",
                        type="function",
                        function=FunctionCall(
                            name="edit_file",
                            arguments={
                                "path": "deck.patch.json",
                                "old_str": '"subtitle":"Unsupported subtitle"',
                                "new_str": '"subtitle":"Exact subtitle"',
                            },
                        ),
                    ),
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="apply repaired patch",
                tool_calls=[
                    ToolCall(
                        id="apply-succeeds",
                        type="function",
                        function=FunctionCall(
                            name="bash", arguments={"command": apply_command}
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("Patch repair completed."),
        ]
    )
    bash_tool = RepairableApplyPatchBashTool(
        output,
        failure_path=(
            "slides.slide-01.props.title",
            "slides.slide-01.props.subtitle",
        ),
    )
    edit_tool = CountingEditTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool, "edit_file": edit_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.apply_patch_calls == 2
    assert edit_tool.calls == 2
    for call_id in ("edit-title", "edit-subtitle"):
        assert next(
            event
            for event in events
            if isinstance(event, ToolCallResult) and event.tool_call_id == call_id
        ).success is True


@pytest.mark.asyncio
async def test_controlled_apply_patch_blocks_edit_of_unrelated_reported_field(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        '{"slides":[{"page":1,"title":"Exact title","message":"Exact message",'
        '"bullets":["Exact bullet"],"layout":"closing","visual":"proofs",'
        '"evidence":["Exact evidence"]}]}',
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    deck_path = output / "deck.json"
    deck_path.write_text(
        '{"slides":[{"id":"slide-01","source_outline_page":1,'
        '"props":{"title":"Exact title"}}]}',
        encoding="utf-8",
    )
    patch_path = output / "deck.patch.json"
    patch_path.write_text(
        '{"slides":{"slide-01":{"props":{"proofs":['
        '{"value":"A","label":"Unsupported winner"},'
        '{"value":"B","label":"Unrelated proof"}]}}}}',
        encoding="utf-8",
    )
    newer = max(deck_path.stat().st_mtime_ns, patch_path.stat().st_mtime_ns) + 10_000_000
    os.utime(patch_path, ns=(newer, newer))
    apply_command = f"cd output && node {APPLY_PATCH_SCRIPT} deck.json deck.patch.json"
    llm = MockLLM(
        [
            LLMResponse(
                content="apply patch",
                tool_calls=[
                    ToolCall(
                        id="apply-fails",
                        type="function",
                        function=FunctionCall(
                            name="bash", arguments={"command": apply_command}
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="edit the wrong proof",
                tool_calls=[
                    ToolCall(
                        id="edit-wrong-proof",
                        type="function",
                        function=FunctionCall(
                            name="edit_file",
                            arguments={
                                "path": "deck.patch.json",
                                "old_str": '"label":"Unrelated proof"',
                                "new_str": '"label":"Different unrelated proof"',
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="edit the named proof",
                tool_calls=[
                    ToolCall(
                        id="edit-named-proof",
                        type="function",
                        function=FunctionCall(
                            name="edit_file",
                            arguments={
                                "path": "deck.patch.json",
                                "old_str": '"label":"Unsupported winner"',
                                "new_str": '"label":"Supported winner"',
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="apply repaired patch",
                tool_calls=[
                    ToolCall(
                        id="apply-succeeds",
                        type="function",
                        function=FunctionCall(
                            name="bash", arguments={"command": apply_command}
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("Patch repair completed."),
        ]
    )
    bash_tool = RepairableApplyPatchBashTool(
        output,
        failure_path="slides.slide-01.props.proofs.0.label",
    )
    edit_tool = CountingEditTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool, "edit_file": edit_tool},
            max_steps=6,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    wrong_edit = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "edit-wrong-proof"
    )
    assert wrong_edit.success is False
    assert "CONTROLLED_PRESENTATION_APPLY_PATCH_FIELD_MISMATCH" in (
        wrong_edit.error or ""
    )
    assert "slides.slide-01.props.proofs.0.label" in (wrong_edit.error or "")
    assert edit_tool.calls == 1
    assert bash_tool.apply_patch_calls == 2
    assert not any(
        isinstance(event, InjectedMessageEvent)
        and f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled"
        in event.content
        for event in events
    )


def _write_deck_spec_repair_fixture(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                        "layout": "comparison",
                        "visual": "two columns",
                        "evidence": ["Five verified titles"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.patch.json").write_text("{}", encoding="utf-8")
    deck = output / "deck.json"
    deck.write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "source_outline_page": 1,
                        "props": {"title": "Exact title", "body": "unsupported 24"},
                    }
                ],
                "truth_contract": {
                    "source_facts": [],
                    "research_facts": ["Five verified titles"],
                    "assumptions": [],
                },
            }
        ),
        encoding="utf-8",
    )
    deck_spec = qa / "deck_spec.json"
    deck_spec.write_text(
        json.dumps(
            {
                "ok": False,
                "issues": [
                    "slides.slide-01.props.body: required structural field is invalid"
                ],
            }
        ),
        encoding="utf-8",
    )
    truth = qa / "truth_check.json"
    truth.write_text('{"ok": true}', encoding="utf-8")
    fresh = max(
        deck.stat().st_mtime_ns,
        deck_spec.stat().st_mtime_ns,
        truth.stat().st_mtime_ns,
    ) + 10_000_000
    os.utime(deck_spec, ns=(fresh, fresh))
    fresh += 10_000_000
    os.utime(truth, ns=(fresh, fresh))
    return output, deck, deck_spec


def _write_deck_redesign_repair_fixture(tmp_path):
    output, deck, deck_spec = _write_deck_spec_repair_fixture(tmp_path)
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Ten-stop agenda",
                        "message": "Walk through all ten stops.",
                        "bullets": [f"{index:02d} agenda item" for index in range(1, 11)],
                        "layout": "agenda-grid-v1",
                        "visual": "numbered agenda with 10 icon nodes",
                        "evidence": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    deck.write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "cards-grid-v1",
                        "source_outline_page": 1,
                        "props": {
                            "title": "Ten-stop agenda",
                            "items": [
                                {"title": f"Item {index}", "body": "Agenda"}
                                for index in range(1, 7)
                            ],
                        },
                    }
                ],
                "truth_contract": {
                    "source_facts": [],
                    "research_facts": [],
                    "assumptions": [],
                },
            }
        ),
        encoding="utf-8",
    )
    deck_spec.write_text(
        json.dumps(
            {
                "ok": False,
                "issues": [
                    "slides.slide-01.props.items: layout capacity mismatch; outline "
                    "visual explicitly requests 10 visual item(s), but cards-grid-v1 "
                    "supports at most 6"
                ],
            }
        ),
        encoding="utf-8",
    )
    outline_check = output / "qa" / "outline_check.json"
    fresh = max(
        deck.stat().st_mtime_ns,
        deck_spec.stat().st_mtime_ns,
        (output / "outline.json").stat().st_mtime_ns,
    ) + 10_000_000
    os.utime(outline_check, ns=(fresh, fresh))
    fresh += 10_000_000
    os.utime(deck_spec, ns=(fresh, fresh))
    return output, deck, deck_spec


def test_controlled_capacity_failure_routes_through_redesign_repair(tmp_path):
    output, deck, _ = _write_deck_redesign_repair_fixture(tmp_path)

    checkpoint = completion_gate_progress_text(
        CompletionGate(workflow_checkpoint_kind="controlled_presentation"),
        str(tmp_path),
    )

    assert checkpoint is not None
    assert (
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}deck_redesign_repair"
        in checkpoint
    )
    assert '"current_layout_id":"cards-grid-v1"' in checkpoint
    assert "deck.redesign.json" in checkpoint
    assert "Do not write deck.patch.json" in checkpoint

    redesign = output / "deck.redesign.json"
    redesign.write_text(
        json.dumps(
            {
                "slides": {
                    "slide-01": {
                        "layout_id": "table-data-v1",
                        "props": {
                            "title": "Ten-stop agenda",
                            "columns": ["No.", "Agenda"],
                            "rows": [[str(index), f"Item {index}"] for index in range(1, 11)],
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    newer = max(redesign.stat().st_mtime_ns, deck.stat().st_mtime_ns) + 10_000_000
    os.utime(redesign, ns=(newer, newer))

    apply_checkpoint = completion_gate_progress_text(
        CompletionGate(workflow_checkpoint_kind="controlled_presentation"),
        str(tmp_path),
    )
    assert apply_checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}apply_redesign" in apply_checkpoint
    assert "apply_deck_redesign.js" in apply_checkpoint


def test_controlled_redesign_repair_allows_only_redesign_artifact(tmp_path):
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        stage="deck_redesign_repair",
        has_repair_input=True,
    )

    assert policy.tool_call_error(
        "write_file",
        {"path": "deck.redesign.json", "content": "{}"},
        verified_evidence_urls=set(),
    ) is None
    assert "CONTROLLED_PRESENTATION_REDESIGN_INPUT_READY" in (
        policy.tool_call_error(
            "write_file",
            {"path": "deck.patch.json", "content": "{}"},
            verified_evidence_urls=set(),
        )
        or ""
    )


def test_controlled_apply_redesign_accepts_only_canonical_command(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        stage="apply_redesign",
    )
    command = (
        f"node {APPLY_REDESIGN_SCRIPT} {output / 'deck.json'} "
        f"{output / 'deck.redesign.json'}"
    )

    assert policy.tool_call_error(
        "bash",
        {"command": command},
        verified_evidence_urls=set(),
    ) is None
    assert "CONTROLLED_PRESENTATION_APPLY_REDESIGN_REQUIRED" in (
        policy.tool_call_error(
            "bash",
            {"command": f"node {APPLY_PATCH_SCRIPT} deck.json deck.patch.json"},
            verified_evidence_urls=set(),
        )
        or ""
    )


@pytest.mark.asyncio
async def test_controlled_repair_input_blocks_fresh_report_reread(tmp_path):
    _, _, _ = _write_deck_spec_repair_fixture(tmp_path)
    checkpoint = completion_gate_progress_text(
        CompletionGate(workflow_checkpoint_kind="controlled_presentation"),
        str(tmp_path),
    )
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}deck_spec_repair" in checkpoint
    assert "REPAIR_INPUT=" in checkpoint
    assert '"current_props":{"title":"Exact title","body":"unsupported 24"}' in checkpoint
    assert '"protected_title_prop_path":"title"' in checkpoint
    assert "minimal deck.patch.json" in checkpoint

    llm = MockLLM(
        [
            LLMResponse(
                content="read deck spec again",
                tool_calls=[
                    ToolCall(
                        id="read-truth",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "qa/deck_spec.json"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    read_tool = CountingReadTool()
    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"read_file": read_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert read_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "read_file"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_REPAIR_INPUT_READY" in (blocked.error or "")


@pytest.mark.asyncio
async def test_controlled_repair_stalls_after_three_identical_policy_rejections(tmp_path):
    _write_deck_spec_repair_fixture(tmp_path)
    llm = MockLLM(
        [
            LLMResponse(
                content=f"read deck spec again {attempt}",
                tool_calls=[
                    ToolCall(
                        id=f"read-truth-{attempt}",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "qa/deck_spec.json"},
                        ),
                    )
                ],
                finish_reason="tool",
            )
            for attempt in range(1, 5)
        ]
        + [_final("Stopped after repeated no-progress repair attempts.")]
    )
    read_tool = CountingReadTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"read_file": read_tool},
            max_steps=7,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert read_tool.calls == 0
    third = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "read-truth-3"
    )
    assert third.success is False
    assert "CONTROLLED_PRESENTATION_REPAIR_INPUT_READY" in (third.error or "")
    fourth = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "read-truth-4"
    )
    assert fourth.success is False
    assert "CONTROLLED_PRESENTATION_REPAIR_STALLED" in (fourth.error or "")
    assert any(
        isinstance(event, InjectedMessageEvent)
        and f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled"
        in event.content
        for event in events
    )
    assert any(
        isinstance(event, DoneEvent)
        and event.final_content == "Stopped after repeated no-progress repair attempts."
        for event in events
    )


@pytest.mark.asyncio
async def test_controlled_outline_repair_input_blocks_repeated_report_reads(tmp_path):
    output = tmp_path / "output"
    qa = output / "qa"
    qa.mkdir(parents=True)
    (output / "outline.json").write_text(
        json.dumps(
            {
                "goal": "Explain the topic",
                "audience": "Reviewers",
                "source_mode": "user_provided",
                "slides": [
                    {
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Point one", "Point two"],
                        "layout_intent": "cover",
                        "visual_intent": "hero",
                        "evidence": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (qa / "outline_check.json").write_text(
        json.dumps(
            {
                "ok": False,
                "issues": [
                    "Missing top-level field: deck_goal",
                    "slide-01: page must be 1, got undefined",
                ],
            }
        ),
        encoding="utf-8",
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="read the report again",
                tool_calls=[
                    ToolCall(
                        id="read-outline-report",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "qa/outline_check.json"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    read_tool = CountingReadTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"read_file": read_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert read_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "read_file"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_OUTLINE_REPAIR_INPUT_READY" in (
        blocked.error or ""
    )


@pytest.mark.asyncio
async def test_controlled_repair_rejects_missing_fact_pause(tmp_path):
    _write_deck_spec_repair_fixture(tmp_path)
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="ask-required-fact",
                        type="function",
                        function=FunctionCall(
                            name="request_user_input",
                            arguments={
                                "question": "请补充这个项目的正式名称。",
                                "missing_fields": ["项目正式名称"],
                                "reason": "该名称是必填事实，不能安全推断。",
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("已改用明确占位继续修复。"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"request_user_input": RequestUserInputTool()},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
                pause_tools=frozenset({"request_user_input"}),
            ),
            workspace_dir=str(tmp_path),
        )
    )

    result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_name == "request_user_input"
    )
    assert result.success is False
    assert "CONTROLLED_PRESENTATION_REPAIR_INPUT_READY" in (result.error or "")
    assert "fresh hard deck-spec issues" in (result.error or "")
    assert any(
        isinstance(event, DoneEvent)
        and event.final_content == "已改用明确占位继续修复。"
        for event in events
    )


@pytest.mark.asyncio
async def test_controlled_stale_deck_spec_report_requires_single_finalizer(tmp_path):
    _, deck, deck_spec = _write_deck_spec_repair_fixture(tmp_path)
    newer = max(deck.stat().st_mtime_ns, deck_spec.stat().st_mtime_ns) + 10_000_000
    os.utime(deck, ns=(newer, newer))

    checkpoint = completion_gate_progress_text(
        CompletionGate(workflow_checkpoint_kind="controlled_presentation"),
        str(tmp_path),
    )
    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}finalize" in checkpoint
    assert "finalize_controlled_deck.js" in checkpoint
    assert str(tmp_path / "output" / "deck.json") in checkpoint
    assert str(tmp_path / "output" / "index.html") in checkpoint
    assert "Do not split it into individual validators" in checkpoint
    assert "REPAIR_INPUT=" not in checkpoint


    llm = MockLLM(
        [
            LLMResponse(
                content="read stale deck spec",
                tool_calls=[
                    ToolCall(
                            id="read-stale-deck-spec",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "qa/deck_spec.json"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    read_tool = CountingReadTool()
    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"read_file": read_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert read_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "read_file"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_FINALIZE_REQUIRED" in (blocked.error or "")
    assert "finalize_controlled_deck.js" in (blocked.error or "")


def test_controlled_nested_artifact_root_uses_absolute_finalizer_paths(tmp_path):
    artifact_root = tmp_path / "output" / "nested" / "controlled"
    qa = artifact_root / "qa"
    qa.mkdir(parents=True)
    (artifact_root / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Intro",
                        "message": "Message",
                        "bullets": ["One"],
                        "layout": "cover",
                        "visual": "hero",
                        "evidence": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (artifact_root / "deck.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "theme_id": "soft-editorial",
                "slides": [
                    {
                        "id": "slide-1",
                        "layout_id": "cover-hero-v1",
                        "props": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")

    checkpoint = completion_gate_progress_text(
        CompletionGate(workflow_checkpoint_kind="controlled_presentation"),
        str(tmp_path),
    )

    assert checkpoint is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}finalize" in checkpoint
    assert str(artifact_root / "deck.json") in checkpoint
    assert str(artifact_root / "index.html") in checkpoint


@pytest.mark.asyncio
async def test_controlled_finalize_blocks_split_validator_command(tmp_path):
    _, deck, deck_spec = _write_deck_spec_repair_fixture(tmp_path)
    newer = max(deck.stat().st_mtime_ns, deck_spec.stat().st_mtime_ns) + 10_000_000
    os.utime(deck, ns=(newer, newer))
    llm = MockLLM(
        [
            LLMResponse(
                content="split validation",
                tool_calls=[
                    ToolCall(
                        id="split-validator",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={
                                "command": (
                                    "node /runtime/scripts/validate_deck_spec.js "
                                    "deck.json --report qa/deck_spec.json"
                                )
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    bash_tool = CountingBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "bash"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_FINALIZE_REQUIRED" in (blocked.error or "")


@pytest.mark.asyncio
async def test_controlled_finalize_allows_exact_single_command(tmp_path):
    _, deck, deck_spec = _write_deck_spec_repair_fixture(tmp_path)
    newer = max(deck.stat().st_mtime_ns, deck_spec.stat().st_mtime_ns) + 10_000_000
    os.utime(deck, ns=(newer, newer))
    llm = MockLLM(
        [
            LLMResponse(
                content="finalize once",
                tool_calls=[
                    ToolCall(
                        id="finalize",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={
                                "command": (
                                    "cd output && node "
                                    f"{FINALIZER_SCRIPT} deck.json --out index.html"
                                )
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    bash_tool = CountingBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.calls == 1
    result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "bash"
    )
    assert result.success is True


@pytest.mark.asyncio
async def test_controlled_finalize_stops_repeated_identical_repair_failure(tmp_path):
    output, deck, deck_spec = _write_deck_spec_repair_fixture(tmp_path)
    newer = max(deck.stat().st_mtime_ns, deck_spec.stat().st_mtime_ns) + 10_000_000
    os.utime(deck, ns=(newer, newer))
    finalizer_command = (
        "cd output && node "
        f"{FINALIZER_SCRIPT} deck.json --out index.html"
    )
    apply_command = (
        "cd output && node "
        f"{FINALIZER_SCRIPT.parent / 'apply_deck_patch.js'} "
        "deck.json deck.patch.json"
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="finalize first",
                tool_calls=[
                    ToolCall(
                        id="finalize-first",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": finalizer_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="repair once",
                tool_calls=[
                    ToolCall(
                        id="repair-once",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "deck.patch.json",
                                "content": '{"slides":{"slide-03":{"props":{}}}}',
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="apply once",
                tool_calls=[
                    ToolCall(
                        id="apply-once",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": apply_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="finalize second",
                tool_calls=[
                    ToolCall(
                        id="finalize-second",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": finalizer_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="try a forbidden third finalizer",
                tool_calls=[
                    ToolCall(
                        id="finalize-third",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": finalizer_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("Stopped after repeated internal validation failure."),
        ]
    )
    bash_tool = RepeatingFinalizerFailureBashTool(output)

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={
                "bash": bash_tool,
                "write_file": ArtifactWriteTool(output),
            },
            max_steps=8,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=3,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.finalizer_calls == 2
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "finalize-third"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_REPAIR_STALLED" in (blocked.error or "")
    assert any(
        isinstance(event, InjectedMessageEvent)
        and f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled"
        in event.content
        for event in events
    )
    assert any(
        isinstance(event, DoneEvent)
        and event.final_content == "Stopped after repeated internal validation failure."
        for event in events
    )


@pytest.mark.asyncio
async def test_controlled_outline_stops_repeated_identical_validation_failure(
    tmp_path,
):
    output = tmp_path / "output"
    output.mkdir()
    outline_content = json.dumps(
        {
            "deck_goal": "Explain the proposal",
            "audience": ["Procurement", "IT"],
            "source_mode": "user_provided",
            "storyline": ["Need", "Solution", "Value"],
            "slides": [
                {
                    "page": 1,
                    "title": "Proposal",
                    "message": "One decision-ready message",
                    "bullets": ["Need", "Solution", "Value"],
                    "layout": "cover",
                    "visual": "consulting cover",
                    "evidence": [],
                }
            ],
        }
    )
    (output / "outline.json").write_text(outline_content, encoding="utf-8")
    validation_command = (
        "cd output && node "
        f"{VALIDATE_OUTLINE_SCRIPT} outline.json --report qa/outline_check.json"
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="validate once",
                tool_calls=[
                    ToolCall(
                        id="outline-validate-first",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": validation_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="repair once",
                tool_calls=[
                    ToolCall(
                        id="outline-repair-once",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "outline.json",
                                "content": outline_content,
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="validate twice",
                tool_calls=[
                    ToolCall(
                        id="outline-validate-second",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": validation_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="try a forbidden third validation",
                tool_calls=[
                    ToolCall(
                        id="outline-validate-third",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": validation_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("Stopped after repeated outline validation failure."),
        ]
    )
    bash_tool = RepeatingOutlineFailureBashTool(output)

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={
                "bash": bash_tool,
                "write_file": OutlineRepairWriteTool(output),
            },
            max_steps=7,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.validation_calls == 2
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "outline-validate-third"
    )
    assert blocked.success is False
    assert "CONTROLLED_PRESENTATION_REPAIR_STALLED" in (blocked.error or "")
    assert any(
        isinstance(event, InjectedMessageEvent)
        and f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled"
        in event.content
        for event in events
    )


@pytest.mark.asyncio
async def test_controlled_presentation_rejects_outline_only_execution_plan(
    tmp_path,
):
    bad_plan = {
        "action": "set",
        "title": "客户评标用解决方案 PPT 内容方案",
        "objective": "产出一份可进入后续制作为 PPT 的叙事大纲。",
        "scope": (
            "本轮仅完成并校验 PPT 内容方案/outline，"
            "不进入主题、版式脚手架或 HTML/PPTX 制作。"
        ),
        "steps": [
            {"title": "生成 outline.json", "details": "梳理页级叙事"},
            {"title": "运行 outline 校验", "details": "修正报告问题"},
        ],
        "verification": ["outline_check.json 返回 ok=true"],
    }
    complete_plan = {
        "action": "set",
        "title": "客户评标用解决方案 PPT",
        "objective": "交付完成 QA 的可编辑 HTML 演示文稿。",
        "scope": "完成内容、版式、媒体、渲染和交付。",
        "steps": [
            {"title": "内容规划", "details": "生成并校验 outline.json"},
            {"title": "受控制作", "details": "生成 deck.json 并填写内容与媒体"},
            {"title": "最终交付", "details": "渲染 index.html 并完成 QA"},
        ],
        "verification": ["index.html 和所有 QA 报告通过"],
    }
    llm = MockLLM(
        [
            LLMResponse(
                content="outline-only plan",
                tool_calls=[
                    ToolCall(
                        id="bad-outline-plan",
                        type="function",
                        function=FunctionCall(
                            name="plan_write",
                            arguments=bad_plan,
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="complete delivery plan",
                tool_calls=[
                    ToolCall(
                        id="complete-deck-plan",
                        type="function",
                        function=FunctionCall(
                            name="plan_write",
                            arguments=complete_plan,
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("Plan corrected."),
        ]
    )
    plan_tool = PlanWriteCaptureTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"plan_write": plan_tool},
            max_steps=4,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert plan_tool.calls == 1
    assert plan_tool.arguments == [complete_plan]
    rejected = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "bad-outline-plan"
    )
    assert rejected.success is False
    assert "CONTROLLED_PRESENTATION_PLAN_SCOPE_INCOMPLETE" in (
        rejected.error or ""
    )


@pytest.mark.asyncio
async def test_controlled_finalize_blocks_relative_helper_path(tmp_path):
    _, deck, deck_spec = _write_deck_spec_repair_fixture(tmp_path)
    newer = max(deck.stat().st_mtime_ns, deck_spec.stat().st_mtime_ns) + 10_000_000
    os.utime(deck, ns=(newer, newer))
    llm = MockLLM(
        [
            LLMResponse(
                content="relative finalizer",
                tool_calls=[
                    ToolCall(
                        id="relative-finalizer",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={
                                "command": (
                                    "node scripts/finalize_controlled_deck.js "
                                    "deck.json --out index.html"
                                )
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )
    bash_tool = CountingBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"bash": bash_tool},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert bash_tool.calls == 0
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "bash"
    )
    assert blocked.success is False
    assert "absolute finalize_controlled_deck.js path" in (blocked.error or "")


@pytest.mark.asyncio
async def test_controlled_presentation_checkpoint_stays_fresh_without_duplicate_event(
    tmp_path,
):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text('{"slides": []}', encoding="utf-8")
    (output / "qa").mkdir()
    (output / "qa" / "outline_check.json").write_text(
        '{"ok": true}', encoding="utf-8"
    )
    (output / "deck.json").write_text(
        '{"slides":[{"props":{"title":"输入演示标题"}}]}',
        encoding="utf-8",
    )
    (output / "deck.patch.json").write_text("{}", encoding="utf-8")
    gate = CompletionGate(workflow_checkpoint_kind="controlled_presentation")
    llm = MockLLM([_echo_call(), _final("done")])

    events = await collect(_run(llm, gate, workspace_dir=str(tmp_path)))

    assert len(llm.messages_seen) == 2
    first_checkpoint_messages = [
        message
        for message in llm.messages_seen[0]
        if isinstance(message.content, str)
        and CONTROLLED_PRESENTATION_CHECKPOINT_MARKER in message.content
    ]
    second_checkpoint_messages = [
        message
        for message in llm.messages_seen[1]
        if isinstance(message.content, str)
        and CONTROLLED_PRESENTATION_CHECKPOINT_MARKER in message.content
    ]
    assert len(first_checkpoint_messages) == 1
    assert llm.messages_seen[0][-1] is first_checkpoint_messages[0]
    assert "NEXT_ACTION=Run `" in first_checkpoint_messages[0].content
    assert "apply_deck_patch.js" in first_checkpoint_messages[0].content
    assert str(output / "deck.json") in first_checkpoint_messages[0].content
    assert str(output / "deck.patch.json") in first_checkpoint_messages[0].content
    assert str(FINALIZER_SCRIPT.parent / "apply_deck_patch.js") in (
        first_checkpoint_messages[0].content
    )
    assert len(second_checkpoint_messages) == 1
    assert llm.messages_seen[1][-1] is second_checkpoint_messages[0]
    assert second_checkpoint_messages[0].content == first_checkpoint_messages[0].content
    checkpoint_events = [
        event
        for event in events
        if isinstance(event, InjectedMessageEvent)
        and CONTROLLED_PRESENTATION_CHECKPOINT_MARKER in event.content
    ]
    assert len(checkpoint_events) == 1
    assert all(event.user_visible is False for event in checkpoint_events)


@pytest.mark.asyncio
async def test_controlled_presentation_checkpoint_reappears_after_stage_progress(
    tmp_path,
):
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text('{"slides": []}', encoding="utf-8")
    (output / "qa").mkdir()
    (output / "qa" / "outline_check.json").write_text(
        '{"ok": true}', encoding="utf-8"
    )
    (output / "deck.json").write_text(
        '{"slides":[{"props":{"title":"输入演示标题"}}]}',
        encoding="utf-8",
    )
    patch_path = output / "deck.patch.json"
    gate = CompletionGate(workflow_checkpoint_kind="controlled_presentation")
    llm = MockLLM(
        [
            LLMResponse(
                content="creating patch",
                tool_calls=[
                    ToolCall(
                        id="create-patch",
                        type="function",
                        function=FunctionCall(
                            name="create_patch",
                            arguments={},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"create_patch": CreatePatchTool(patch_path)},
            max_steps=20,
            completion_gate=gate,
            workspace_dir=str(tmp_path),
        )
    )

    assert len(llm.messages_seen) == 2
    assert (
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch"
        in llm.messages_seen[0][-1].content
    )
    assert (
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}apply_patch"
        in llm.messages_seen[1][-1].content
    )
    checkpoint_events = [
        event
        for event in events
        if isinstance(event, InjectedMessageEvent)
        and CONTROLLED_PRESENTATION_CHECKPOINT_MARKER in event.content
    ]
    assert len(checkpoint_events) == 2


@pytest.mark.asyncio
async def test_completion_gate_does_not_continue_after_tool_budget_is_exhausted(tmp_path):
    gate = CompletionGate(
        required_changed_artifact_globs=("output/**/*.html",),
        max_continuations=3,
        max_tool_calls=1,
    )
    llm = MockLLM([_echo_call(), _final("budget exhausted")])

    events = await collect(_run(llm, gate, workspace_dir=tmp_path))

    assert llm._idx == 2
    injected = [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert any("工具调用总预算已达到上限" in event.content for event in injected)
    assert not any("任务尚未完成" in event.content for event in injected)
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert len(done) == 1
    assert done[0].final_content == "budget exhausted"


@pytest.mark.asyncio
async def test_completion_gate_budget_exempts_workflow_scaffolding():
    plan_tool = NamedEchoTool("plan_write")
    echo_tool = NamedEchoTool("echo")
    gate = CompletionGate(
        max_tool_calls=1,
        budget_exempt_tools=frozenset({"plan_write"}),
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="working",
                tool_calls=[
                    ToolCall(
                        id="plan-1",
                        type="function",
                        function=FunctionCall(
                            name="plan_write",
                            arguments={"text": "plan"},
                        ),
                    ),
                    ToolCall(
                        id="echo-1",
                        type="function",
                        function=FunctionCall(
                            name="echo",
                            arguments={"text": "first"},
                        ),
                    ),
                    ToolCall(
                        id="echo-2",
                        type="function",
                        function=FunctionCall(
                            name="echo",
                            arguments={"text": "second"},
                        ),
                    ),
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"plan_write": plan_tool, "echo": echo_tool},
            max_steps=5,
            completion_gate=gate,
        )
    )

    assert plan_tool.calls == 1
    assert echo_tool.calls == 1
    rejected = [
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "echo-2"
    ]
    assert len(rejected) == 1
    assert rejected[0].success is False
    assert "Total tool call budget reached" in (rejected[0].error or "")


@pytest.mark.asyncio
async def test_completion_gate_reserves_final_calls_for_delivery(tmp_path):
    gate = CompletionGate(
        required_changed_artifact_globs=("output/**/*.html",),
        max_continuations=0,
        max_tool_calls=3,
        completion_reserve_tool_calls=1,
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="working",
                tool_calls=[
                    ToolCall(
                        id="echo-1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "a"}),
                    ),
                    ToolCall(
                        id="echo-2",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "b"}),
                    ),
                ],
                finish_reason="tool",
            ),
            _final("done"),
        ]
    )

    events = await collect(_run(llm, gate, workspace_dir=str(tmp_path)))

    reserve_messages = [
        event
        for event in events
        if isinstance(event, InjectedMessageEvent)
        and "交付收尾预算" in event.content
    ]
    assert len(reserve_messages) == 1


@pytest.mark.asyncio
async def test_completion_gate_allows_resumable_user_input_pause(tmp_path):
    gate = CompletionGate(
        required_changed_artifact_globs=("output/**/*.html",),
        max_continuations=3,
        pause_tools=frozenset({"request_user_input"}),
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="ask-1",
                        type="function",
                        function=FunctionCall(
                            name="request_user_input",
                            arguments={
                                "question": "请补充市场规模数据。",
                                "missing_fields": ["TAM", "SAM", "SOM"],
                                "reason": "路演数据必须来自用户或可信来源。",
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            _final("请补充 TAM、SAM 和 SOM。"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"request_user_input": RequestUserInputTool()},
            max_steps=5,
            completion_gate=gate,
            workspace_dir=str(tmp_path),
        )
    )

    assert llm._idx == 2
    assert not any(
        isinstance(event, InjectedMessageEvent)
        and "尚未满足完成条件" in event.content
        for event in events
    )
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert done[0].final_content == "请补充 TAM、SAM 和 SOM。"


def test_build_auto_completion_gate_ignores_non_deliverable_prompt(tmp_path):
    gate = build_auto_completion_gate("解释一下这个函数", tmp_path)

    assert gate is None


@pytest.mark.parametrize(
    "prompt",
    [
        (
            "https://platform.sensenova.cn/docs,你现在使用了商汤的模型，"
            "麻烦你去阅读一下官方的文档，特别是关于SenseNova U1 Fast "
            "基于SenseNovaU1的加速版本，专供信息图(Infographics)生成的"
            "介绍部分，要求掌握如何利用这个模型生图。"
        ),
        "请阅读官方文档，了解如何利用这个模型生成图片。",
        "介绍一下信息图生成能力，以及如何生图。",
        "学习并掌握 SenseNova U1 Fast 的生图用法。",
        "Read the documentation to learn how to create an image.",
        "Read the documentation and saved Word document examples.",
        "Read the documentation and finished Word document examples.",
        "Read the documentation and builder HTML guide.",
        "阅读报告，了解表格和文档是如何生成的。",
        "生成图片的能力介绍。",
        "了解后端生成一张图片的实现方式。",
        "关于生成图片的文档在哪里？",
    ],
)
def test_informational_artifact_prompt_does_not_create_gate(tmp_path, prompt):
    assert build_auto_completion_gate(prompt, tmp_path) is None


@pytest.mark.parametrize(
    "prompt",
    [
        "先阅读官方文档，然后生成一张 PNG 信息图。",
        "阅读官方文档后生成一张 PNG 信息图。",
        "介绍完模型后，再帮我生成一张图片。",
        "Read the documentation and then create an image.",
        "Read the documentation and create an image.",
    ],
)
def test_research_then_explicit_image_delivery_still_creates_gate(
    tmp_path,
    prompt,
):
    gate = build_auto_completion_gate(prompt, tmp_path)

    assert gate is not None
    assert gate.required_tools == frozenset({"generate_image"})
    assert gate.restrict_tools_until_required_succeed is True


@pytest.mark.parametrize(
    "prompt",
    [
        "介绍如何生成 PPT，然后创建一个 Excel 表格。",
        "创建一个 Excel 表格，并介绍如何生成 PPT。",
    ],
)
def test_mixed_informational_presentation_routes_executable_spreadsheet(
    tmp_path,
    prompt,
):
    gate = build_auto_completion_gate(prompt, tmp_path)

    assert gate is not None
    assert gate.workflow_checkpoint_kind is None
    assert gate.required_changed_artifact_globs == (
        "output/**/*.xlsx",
        "output/**/*.xls",
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "生成文档。",
        "Word 文档，请创建一份。",
        "请帮我create一个Word文档。",
        "创建项目的文档。",
        "生成客户的文档。",
        "生成产品的文档。",
    ],
)
def test_direct_document_delivery_preserves_document_context(tmp_path, prompt):
    gate = build_auto_completion_gate(prompt, tmp_path)

    assert gate is not None
    assert gate.required_tools == frozenset()
    assert gate.required_changed_artifact_globs == ("output/**/*.docx",)
    assert gate.restrict_tools_until_required_succeed is False


def test_format_after_comma_remains_in_delivery_clause(tmp_path):
    gate = build_auto_completion_gate("生成一份报告，Word 格式。", tmp_path)

    assert gate is not None
    assert "output/**/*.docx" in gate.required_changed_artifact_globs


def test_native_image_gate_requires_standard_tool_before_alternatives(tmp_path):
    gate = build_auto_completion_gate("生成一张 PNG 信息图", tmp_path)

    assert gate is not None
    assert gate.required_tools == frozenset({"generate_image"})
    assert gate.restrict_tools_until_required_succeed is True


def test_native_image_gate_ignores_negated_html_fallbacks(tmp_path):
    gate = build_auto_completion_gate(
        "生成一张 PNG 信息图，不要使用 HTML/CSS/SVG/PIL/截图，直接使用生图能力",
        tmp_path,
    )

    assert gate is not None
    assert gate.required_tools == frozenset({"generate_image"})
    assert gate.required_changed_artifact_globs == (
        "output/**/*.png",
        "output/**/*.jpg",
        "output/**/*.jpeg",
        "output/**/*.webp",
    )
    assert all("html" not in pattern for pattern in gate.required_changed_artifact_globs)


def test_native_image_gate_detects_superpowers_infographic_prompt(tmp_path):
    gate = build_auto_completion_gate(
        "请生成一张“超级开发者 Superpowers”全流程技能全景图，16:9 横版信息图，交付 PNG。",
        tmp_path,
    )

    assert gate is not None
    assert gate.required_tools == frozenset({"generate_image"})
    assert gate.required_changed_artifact_globs[0] == "output/**/*.png"


def test_native_image_gate_rejects_png_created_without_generate_image(tmp_path):
    gate = build_auto_completion_gate("生成一张 PNG 信息图", tmp_path)
    assert gate is not None
    output = tmp_path / "output"
    output.mkdir()
    (output / "fallback.png").write_bytes(b"png")

    gaps = completion_gate_gaps(gate, set(), str(tmp_path))

    assert gaps == ["工具 `generate_image` 尚未成功调用并返回有效结果"]
    assert completion_gate_gaps(gate, {"generate_image"}, str(tmp_path)) == []


def test_native_image_gate_ignores_english_negated_html_fallbacks(tmp_path):
    gate = build_auto_completion_gate(
        "Generate an image as PNG without HTML, CSS, SVG, PIL, or screenshots.",
        tmp_path,
    )

    assert gate is not None
    assert gate.required_tools == frozenset({"generate_image"})
    assert gate.required_changed_artifact_globs[0] == "output/**/*.png"


def test_positive_html_delivery_does_not_require_native_image_generation(tmp_path):
    gate = build_auto_completion_gate("生成一个 HTML 信息图", tmp_path)

    assert gate is not None
    assert gate.required_tools == frozenset()
    assert "output/**/*.html" in gate.required_changed_artifact_globs


def test_ppt_completion_gate_accepts_default_html_delivery(tmp_path):
    gate = build_auto_completion_gate("生成一份 PPT", tmp_path)
    assert gate is not None

    output = tmp_path / "output" / "deck"
    (output / "qa").mkdir(parents=True)
    (output / "index.html").write_text("<html></html>", encoding="utf-8")
    for report_name in (
        "outline_check.json",
        "deck_contract.json",
        "deck_spec.json",
        "truth_check.json",
        "image_manifest.json",
        "html_self_check.json",
        "runtime_probe.json",
    ):
        payload = (
            '{"ok": false, "issues": ["advisory source gap"]}'
            if report_name == "truth_check.json"
            else '{"ok": true}'
        )
        (output / "qa" / report_name).write_text(payload, encoding="utf-8")

    assert completion_gate_gaps(gate, set(), str(tmp_path)) == []


def test_ppt_completion_gate_rejects_failed_html_self_check(tmp_path):
    gate = build_auto_completion_gate("生成一份 PPT", tmp_path)
    assert gate is not None

    output = tmp_path / "output" / "deck"
    (output / "qa").mkdir(parents=True)
    (output / "index.html").write_text("<html></html>", encoding="utf-8")
    for report_name in (
        "outline_check.json",
        "deck_contract.json",
        "deck_spec.json",
        "truth_check.json",
        "image_manifest.json",
        "runtime_probe.json",
    ):
        (output / "qa" / report_name).write_text('{"ok": true}', encoding="utf-8")
    report = output / "qa" / "html_self_check.json"
    report.write_text('{"ok": false, "issues": ["overflow"]}', encoding="utf-8")

    gaps = completion_gate_gaps(gate, set(), str(tmp_path))

    assert len(gaps) == 1
    assert "交付物 QA 尚未完成" in gaps[0]
    assert "html_self_check.json" in gaps[0]


def test_ppt_completion_gate_rejects_pptx_without_html_delivery(tmp_path):
    gate = build_auto_completion_gate("生成一份 PPT", tmp_path)
    assert gate is not None

    output = tmp_path / "output"
    output.mkdir()
    (output / "deck.pptx").write_text("pptx", encoding="utf-8")

    gaps = completion_gate_gaps(gate, set(), str(tmp_path))

    assert len(gaps) == 1
    assert "尚未产生新的或更新过的交付产物" in gaps[0]
    assert "output/**/*.html" in gaps[0]


@pytest.mark.parametrize(
    "prompt",
    [
        "导出一份 PPTX",
        "帮我做一份季度汇报，导出 PPT 文件",
        "制作一份可交付的 PowerPoint 文件",
        "Export this as a PowerPoint file.",
    ],
)
def test_explicit_powerpoint_file_completion_gate_requires_pptx(tmp_path, prompt):
    gate = build_auto_completion_gate(prompt, tmp_path)

    assert gate is not None
    assert gate.required_changed_artifact_globs == ("output/**/*.pptx",)


def test_ppt_content_wording_keeps_controlled_html_route(tmp_path):
    gate = build_auto_completion_gate(
        "请输出完整 PPT 内容方案，并制作成可编辑演示文稿",
        tmp_path,
    )

    assert gate is not None
    assert gate.required_changed_artifact_globs == (
        "output/**/*.html",
        "output/**/*.htm",
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "使用 pptx skill，制作一份创意的《纳瓦尔宝典》读书分享 PPT",
        "使用PPTX技能，制作一份创意的《纳瓦尔宝典》读书分享 PPT",
    ],
)
def test_pptx_skill_reference_keeps_controlled_html_route(tmp_path, prompt):
    gate = build_auto_completion_gate(
        prompt,
        tmp_path,
    )

    assert gate is not None
    assert gate.workflow_checkpoint_kind == "controlled_presentation"
    assert gate.required_changed_artifact_globs == (
        "output/**/*.html",
        "output/**/*.htm",
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "生成一份 PPT，但不要导出 .pptx，交付可编辑 HTML。",
        "生成一份 PPT，不要导出 .pptx。",
        "Create a presentation, but do not export .pptx; deliver editable HTML.",
        "Create a presentation. Do not export .pptx.",
    ],
)
def test_negated_pptx_export_keeps_controlled_html_route(tmp_path, prompt):
    gate = build_auto_completion_gate(prompt, tmp_path)

    assert gate is not None
    assert gate.workflow_checkpoint_kind == "controlled_presentation"
    assert "output/**/*.html" in gate.required_changed_artifact_globs
    assert gate.required_changed_artifact_globs != ("output/**/*.pptx",)


def test_gaps_empty_when_all_requirements_met(tmp_path):
    artifact = tmp_path / "out.txt"
    artifact.write_text("content")
    gate = CompletionGate(
        required_tools=frozenset({"echo"}),
        required_artifacts=("out.txt",),
    )
    gaps = completion_gate_gaps(gate, {"echo"}, str(tmp_path))
    assert gaps == []


def test_gaps_reports_missing_tool():
    gate = CompletionGate(required_tools=frozenset({"echo", "search"}))
    gaps = completion_gate_gaps(gate, {"echo"}, None)
    assert len(gaps) == 1
    assert "search" in gaps[0]


def test_gaps_reports_missing_and_empty_artifact(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("")  # exists but zero bytes → still a gap
    gate = CompletionGate(required_artifacts=("empty.txt", "missing.txt"))
    gaps = completion_gate_gaps(gate, set(), str(tmp_path))
    assert len(gaps) == 2
    assert any("empty.txt" in g for g in gaps)
    assert any("missing.txt" in g for g in gaps)


def test_gaps_absolute_artifact_path(tmp_path):
    artifact = tmp_path / "abs.txt"
    artifact.write_text("data")
    gate = CompletionGate(required_artifacts=(str(artifact),))
    # workspace_dir is irrelevant for an absolute path
    assert completion_gate_gaps(gate, set(), None) == []


def test_gate_text_lists_each_gap():
    text = completion_gate_text(["缺口A", "缺口B"])
    assert "缺口A" in text and "缺口B" in text


def test_changed_artifact_glob_ignores_baseline_files(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    existing = output / "old.pptx"
    existing.write_text("old")
    patterns = ("output/**/*.pptx",)
    baseline = artifact_signatures_for_globs(patterns, str(tmp_path))
    gate = CompletionGate(
        required_changed_artifact_globs=patterns,
        baseline_artifact_signatures=baseline,
    )

    gaps = completion_gate_gaps(gate, set(), str(tmp_path))
    assert len(gaps) == 1
    assert "新的或更新过的交付产物" in gaps[0]

    (output / "new.pptx").write_text("new")
    assert completion_gate_gaps(gate, set(), str(tmp_path)) == []


def test_changed_artifact_glob_accepts_modified_baseline_file(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    existing = output / "deck.pptx"
    existing.write_text("old")
    patterns = ("output/**/*.pptx",)
    baseline = artifact_signatures_for_globs(patterns, str(tmp_path))
    gate = CompletionGate(
        required_changed_artifact_globs=patterns,
        baseline_artifact_signatures=baseline,
    )

    existing.write_text("new content")
    assert completion_gate_gaps(gate, set(), str(tmp_path)) == []


def test_changed_artifact_glob_ignores_staging_and_package_fixtures(tmp_path):
    patterns = ("output/**/*.html",)
    gate = CompletionGate(required_changed_artifact_globs=patterns)
    staging = tmp_path / "output" / ".box-agent-staging"
    fixture = tmp_path / "output" / "qrpkg" / "package" / "test"
    staging.mkdir(parents=True)
    fixture.mkdir(parents=True)
    (staging / "draft.html").write_text("draft")
    (fixture / "index.html").write_text("fixture")

    gaps = completion_gate_gaps(gate, set(), str(tmp_path))
    assert len(gaps) == 1

    (tmp_path / "output" / "qr-code.html").write_text("delivery")
    assert completion_gate_gaps(gate, set(), str(tmp_path)) == []


# ── Loop behaviour ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_injects_continuation_until_tool_satisfied():
    """Unmet tool requirement → first END_TURN is intercepted; once the tool
    succeeds, the next END_TURN is allowed."""
    gate = CompletionGate(required_tools=frozenset({"echo"}), max_continuations=3)
    # 1) no tool call → gate injects (echo unmet)
    # 2) echo call → success records evidence
    # 3) no tool call → gate satisfied → END_TURN
    llm = MockLLM([_final("premature"), _echo_call(), _final("real done")])
    events = await collect(_run(llm, gate))

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(injected) == 1
    assert "echo" in injected[0].content
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.END_TURN
    assert done[0].final_content == "real done"


@pytest.mark.asyncio
async def test_host_execution_gate_retries_incomplete_criterion_coverage(tmp_path):
    gate = build_auto_completion_gate(
        """
        Implement the assigned task.
        <host_execution_contract acceptance_criteria_count="2">
        Report every acceptance criterion before ending.
        </host_execution_contract>
        """,
        tmp_path,
    )
    assert gate is not None
    llm = MockLLM(
        [
            _execution_result_call([0], "incomplete"),
            _final("premature"),
            _execution_result_call([0, 1], "complete"),
            _final("real done"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"report_execution_result": ReportExecutionResultTool()},
            max_steps=20,
            completion_gate=gate,
        )
    )

    injected = [event for event in events if isinstance(event, InjectedMessageEvent)]
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert len(injected) == 1
    assert "report_execution_result" in injected[0].content
    assert done[-1].final_content == "real done"


@pytest.mark.asyncio
async def test_gate_restricts_tools_until_required_tool_succeeds():
    gate = CompletionGate(
        required_tools=frozenset({"echo"}),
        restrict_tools_until_required_succeed=True,
    )
    llm = MockLLM([_echo_call(), _final("done")])

    await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": EchoTool(), "fallback": FallbackTool()},
            max_steps=20,
            completion_gate=gate,
        )
    )

    assert llm.tool_names_seen[0] == ("echo",)
    assert llm.tool_names_seen[1] == ("echo", "fallback")


@pytest.mark.asyncio
async def test_gate_releases_after_max_continuations():
    """Requirement never met → gate injects exactly max_continuations times,
    then releases and lets the turn end (safety valve)."""
    gate = CompletionGate(required_tools=frozenset({"echo"}), max_continuations=2)
    # All three turns emit no tool call; echo is never satisfied.
    llm = MockLLM([_final("a"), _final("b"), _final("c")])
    events = await collect(_run(llm, gate))

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(injected) == 2  # bounded by max_continuations
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_gate_releases_when_deadline_exceeded():
    """deadline_seconds already elapsed → gate releases on the first END_TURN
    even though the requirement is unmet."""
    gate = CompletionGate(
        required_tools=frozenset({"echo"}),
        max_continuations=5,
        deadline_seconds=0.0,  # run_start is in the past → immediately exceeded
    )
    llm = MockLLM([_final("done")])
    events = await collect(_run(llm, gate))

    assert not [e for e in events if isinstance(e, InjectedMessageEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_no_gate_is_unchanged_behaviour():
    """completion_gate=None → first no-tool response ends the turn, no
    injection (regression guard for default behaviour)."""
    llm = MockLLM([_final("done")])
    events = await collect(_run(llm, gate=None))

    assert not [e for e in events if isinstance(e, InjectedMessageEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.END_TURN
    assert done[0].final_content == "done"


@pytest.mark.asyncio
async def test_gate_satisfied_by_artifact(tmp_path):
    """Requirement satisfied by an artifact the tool writes — gate allows the
    very first END_TURN because the file already exists."""
    artifact = tmp_path / "result.txt"
    artifact.write_text("ready")
    gate = CompletionGate(required_artifacts=("result.txt",))
    llm = MockLLM([_final("done")])
    events = await collect(_run(llm, gate, workspace_dir=str(tmp_path)))

    assert not [e for e in events if isinstance(e, InjectedMessageEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[0].stop_reason == StopReason.END_TURN
