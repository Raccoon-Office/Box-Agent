"""Schedule Tool - 让 agent 在对话中触发"创建定时任务"弹窗。

设计要点：
- 本工具**不落库**。它的作用是把一份"定时任务草稿"通过 ``ToolResult.raw_output``
  随 ACP ``tool_call_update.rawOutput`` 推给桌面端（officev3）；渲染层监听到
  ``kind == "officev3_schedule_draft"`` 后弹出预填好的创建窗口，由用户最终确认保存。
- 这是 fire-and-forget：工具只负责"弹窗"，**无法**得知用户最终保存还是取消。
  因此调用后只应告知"窗口已弹出，请核对保存"，不要宣称"已创建成功"。
- 校验从严、不静默纠错：空 cron 才回退默认值；非法 cron / once 缺 fire_at 一律返回失败，
  让模型重新与用户确认，避免把"每周一"误落成"每天 9 点"。

契约（与 officev3 渲染层约定）::

    raw_output = {
        "kind": "officev3_schedule_draft",
        "draft": {
            "name": str,
            "prompt": str,
            "cron_expr": str | None,      # trigger_type == "cron" 时有效
            "trigger_type": "cron" | "once",
            "fire_at": str | None,        # trigger_type == "once" 时为 ISO 8601
        },
    }
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import Tool, ToolResult

# officev3 渲染层据此识别本工具产生的草稿事件，勿改。
SCHEDULE_DRAFT_KIND = "officev3_schedule_draft"

# 与 officev3 `src/lib/schedule/cronConfig.ts` 的 DEFAULT_CRON_EXPR 保持一致。
DEFAULT_CRON_EXPR = "0 9 * * *"

_VALID_TRIGGER_TYPES = ("cron", "once")

# 标准 5 段 cron 各字段的取值范围（含 dow 7 == 周日的兼容）。
_CRON_FIELD_RANGES = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day-of-month
    (1, 12),  # month
    (0, 7),   # day-of-week (0 与 7 均表示周日)
)


def _validate_cron_field(field: str, low: int, high: int) -> bool:
    """校验单个 cron 字段：支持 ``*`` / ``a`` / ``a-b`` / ``*/n`` / ``a/n`` / ``a-b/n`` 及逗号列表。"""
    field = field.strip()
    if not field:
        return False

    for part in field.split(","):
        part = part.strip()
        if not part:
            return False

        # 步进语法 base/step
        if "/" in part:
            base, _, step = part.partition("/")
            if not step.isdigit() or int(step) <= 0:
                return False
            part = base.strip()
            if part == "*":
                continue  # */n 合法

        if part == "*":
            continue

        # 区间 a-b
        if "-" in part:
            start, _, end = part.partition("-")
            if not (start.isdigit() and end.isdigit()):
                return False
            s, e = int(start), int(end)
            if s > e or s < low or e > high:
                return False
            continue

        # 单值
        if not part.isdigit():
            return False
        if not (low <= int(part) <= high):
            return False

    return True


def _validate_cron(expr: str) -> tuple[bool, str]:
    """校验标准 5 段 cron 表达式。返回 (ok, error_message)。"""
    parts = expr.strip().split()
    if len(parts) != 5:
        return (
            False,
            f"cron_expr 必须是标准 5 段表达式（分 时 日 月 周），收到 {len(parts)} 段：{expr!r}",
        )
    for value, (low, high) in zip(parts, _CRON_FIELD_RANGES):
        if not _validate_cron_field(value, low, high):
            return False, f"cron_expr 字段非法：{value!r}（合法范围 {low}-{high}）于 {expr!r}"
    minutes: set[int] = set()
    for part in parts[0].split(","):
        base, separator, step_text = part.partition("/")
        step = int(step_text) if separator else 1
        if base == "*":
            start, end = 0, 59
        elif "-" in base:
            start, end = map(int, base.split("-"))
        else:
            start = int(base)
            end = 59 if separator else start
        minutes.update(range(start, end + 1, step))
    # Multiple minutes repeat within every matching hour, less than 60 minutes apart.
    if len(minutes) != 1:
        return False, "周期任务的触发间隔至少为 60 分钟，分钟字段只能匹配一个固定分钟；例如 0 * * * *（每小时）。"
    return True, ""


def _validate_fire_at(value: str) -> tuple[bool, str]:
    """校验一次性触发时间（ISO 8601）。返回 (ok, error_message)。"""
    raw = (value or "").strip()
    if not raw:
        return False, "trigger_type 为 'once' 时必须提供 fire_at（ISO 8601 时间）。"
    # datetime.fromisoformat 不接受末尾 'Z'，做一次兼容替换。
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False, f"fire_at 不是合法的 ISO 8601 时间：{value!r}"
    return True, ""


class CreateScheduledTaskTool(Tool):
    """在桌面端弹出"预填好的定时任务创建窗口"，由用户最终确认保存。"""

    parallel_safe = False

    @property
    def name(self) -> str:
        return "create_scheduled_task"

    @property
    def description(self) -> str:
        return (
            "在桌面客户端弹出一个【预填好的定时任务创建窗口】，由用户最终核对并点击保存来真正创建任务。\n"
            "\n"
            "硬性前置条件：调用本工具前，你必须已经在对话中与用户确认过三要素——\n"
            "  ① 做什么（任务内容）② 多久一次（频率）③ 具体时间（几点 / 周几）。\n"
            "三要素未确认齐全时，先在对话里追问，不要调用本工具。\n"
            "\n"
            "重要：这是一次性的弹窗动作，本工具无法得知用户最终是否保存。调用成功后只能告诉用户"
            "'创建窗口已弹出，请核对后点击保存'，绝不要宣称'已创建成功'。\n"
            "\n"
            "参数说明：\n"
            "- name：简短的任务名（展示用）。\n"
            "- prompt：每次定时触发时下发给 agent 的【完整可独立执行指令】，要把内容、产出格式、"
            "数据来源都写清楚，使其脱离当前对话也能照做。\n"
            "- cron_expr：标准 5 段 cron（分 时 日 月 周）。周期任务的触发间隔至少为 60 分钟，"
            "分钟字段只能匹配一个固定分钟。例：每小时=`0 * * * *`；每天 9 点=`0 9 * * *`；"
            "每周一 10 点=`0 10 * * 1`；每个工作日 16 点=`0 16 * * 1-5`。\n"
            "- trigger_type：'cron'（周期，默认）或 'once'（一次性）。\n"
            "- fire_at：trigger_type 为 'once' 时必填，ISO 8601 时间（如 `2026-06-20T09:00:00`）。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "简短的任务名，用于展示。",
                },
                "prompt": {
                    "type": "string",
                    "description": "每次定时触发时下发给 agent 的完整、可独立执行的指令。",
                },
                "cron_expr": {
                    "type": "string",
                    "description": (
                        "标准 5 段 cron（分 时 日 月 周）。trigger_type 为 'cron' 时使用；"
                        "触发间隔至少为 60 分钟，分钟字段只能匹配一个固定分钟（如 0 * * * *）；"
                        f"留空则默认 {DEFAULT_CRON_EXPR}（每天 9 点）。"
                    ),
                },
                "trigger_type": {
                    "type": "string",
                    "enum": list(_VALID_TRIGGER_TYPES),
                    "description": "触发方式：'cron'（周期，默认）或 'once'（一次性）。",
                },
                "fire_at": {
                    "type": "string",
                    "description": "一次性触发时间（ISO 8601），trigger_type 为 'once' 时必填。",
                },
            },
            "required": ["name", "prompt"],
        }

    async def execute(
        self,
        name: str,
        prompt: str,
        cron_expr: str | None = None,
        trigger_type: str = "cron",
        fire_at: str | None = None,
    ) -> ToolResult:
        name = (name or "").strip()
        prompt = (prompt or "").strip()
        if not name:
            return ToolResult(success=False, error="name 不能为空。")
        if not prompt:
            return ToolResult(success=False, error="prompt 不能为空。")

        trigger_type = (trigger_type or "cron").strip() or "cron"
        if trigger_type not in _VALID_TRIGGER_TYPES:
            return ToolResult(
                success=False,
                error=f"trigger_type 必须是 'cron' 或 'once'，收到 {trigger_type!r}。",
            )

        draft_cron: str | None = None
        draft_fire_at: str | None = None

        if trigger_type == "cron":
            expr = (cron_expr or "").strip()
            if not expr:
                # 仅"空值"兜底默认；非法值绝不静默纠错。
                expr = DEFAULT_CRON_EXPR
            else:
                ok, err = _validate_cron(expr)
                if not ok:
                    return ToolResult(success=False, error=err)
            draft_cron = expr
        else:  # once
            ok, err = _validate_fire_at(fire_at or "")
            if not ok:
                return ToolResult(success=False, error=err)
            draft_fire_at = (fire_at or "").strip()

        draft = {
            "name": name,
            "prompt": prompt,
            "cron_expr": draft_cron,
            "trigger_type": trigger_type,
            "fire_at": draft_fire_at,
        }

        return ToolResult(
            success=True,
            content=(
                f"已为你弹出定时任务创建窗口「{name}」，请核对内容后点击保存来完成创建。"
                "（窗口已弹出，是否最终保存由用户决定。）"
            ),
            raw_output={"kind": SCHEDULE_DRAFT_KIND, "draft": draft},
        )
