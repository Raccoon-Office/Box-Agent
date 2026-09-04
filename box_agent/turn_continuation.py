"""Bounded recovery for assistant replies that announce work but do not act.

The agent loop owns only one integration point: it presents a candidate final
response to :class:`TurnContinuationController`.  Detection rules, retry state,
and the injected continuation prompt live here so the policy can be extended or
moved without spreading language heuristics through ``core.py``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final


log = logging.getLogger(__name__)

DEFAULT_MAX_CONTINUATIONS: Final[int] = 2
MAX_CANDIDATE_CHARS: Final[int] = 600

_NORMAL_FINISH_REASONS: Final[frozenset[str | None]] = frozenset(
    {None, "stop", "end_turn"}
)
_CONDITIONAL_OR_DELAYED_PREFIX_RE = re.compile(
    r"^\s*(?:如果|若(?:是)?|如需|需要时|等你|等您|"
    r"if\b|when\b|once\s+you\b|should\s+you\b)",
    re.IGNORECASE,
)
_USER_INPUT_REQUEST_RE = re.compile(
    r"(?:请|麻烦)(?:你|您)?(?:先)?(?:告诉|提供|选择|确认|上传|补充)|"
    r"(?:等待|需要)[^。！？!?\n]{0,24}(?:你|您|用户)[^。！？!?\n]{0,24}"
    r"(?:提供|确认|选择|上传|补充)|"
    r"\b(?:please|need|wait(?:ing)?)\b[^.!?\n]{0,40}"
    r"\b(?:provide|confirm|choose|select|upload|clarify)\b|"
    r"[?？]\s*$",
    re.IGNORECASE,
)
_LEADING_ACTION_INTENT_RE = re.compile(
    r"^\s*(?:(?:好的|可以|明白|收到|好)[，,。.!！:\s]*)?"
    r"(?:我(?:会|将|来|先|现在|接下来|随后)|"
    r"(?:接下来|下一步|现在)我(?:会|将|来|先)?|"
    r"(?:okay|sure|got\s+it)[,!.:\s]*(?:i(?:['’]ll|\s+will)|let\s+me)|"
    r"i(?:['’]ll|\s+will|\s+am\s+going\s+to)|let\s+me)",
    re.IGNORECASE,
)
_TRAILING_ACTION_INTENT_RE = re.compile(
    r"(?:接下来|下一步)[，,：:\s]*(?:我)?(?:会|将|来|先)[^。！？!?\n]{0,100}"
    r"[。！？!?…:]?\s*$|"
    r"现在(?:我)?(?:来|开始|会|将)[^。！？!?\n]{0,100}[。！？!?…:]?\s*$|"
    r"(?:\blet\s+me\s+now\b|\bi(?:['’])?ll\s+now\b|"
    r"\bi\s+will\s+now\b|\bnow\s+i(?:['’]ll|\s+will)\b|"
    r"\bnext[,:]\s+i\b)[^.!?\n]{0,100}[.!?…:]?\s*$",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"创建|新建|写入|修改|编辑|运行|启动|检查|验证|测试|搜索|查找|读取|"
    r"打开|生成|实现|修复|调试|分析|构建|打包|部署|提交|推送|安装|下载|"
    r"\b(?:create|write|edit|modify|run|start|launch|check|verify|test|"
    r"search|find|read|open|generate|implement|fix|debug|analy[sz]e|build|"
    r"package|deploy|commit|push|install|download)\b",
    re.IGNORECASE,
)

_CONTINUATION_PROMPT: Final[str] = (
    "[System continuation: Your previous response only announced the next action "
    "but did not issue a tool call. Continue now and execute the announced work. "
    "Do not repeat the plan or promise future work. Return a final answer only after "
    "the work and its necessary verification are complete.]"
)


def announces_unfinished_action(text: str) -> bool:
    """Return whether ``text`` is a short, actionable continuation promise."""
    candidate = (text or "").strip()
    if not candidate or len(candidate) > MAX_CANDIDATE_CHARS:
        return False
    if _CONDITIONAL_OR_DELAYED_PREFIX_RE.search(candidate):
        return False
    if _USER_INPUT_REQUEST_RE.search(candidate):
        return False
    if not _ACTION_RE.search(candidate):
        return False
    return bool(
        _LEADING_ACTION_INTENT_RE.search(candidate)
        or _TRAILING_ACTION_INTENT_RE.search(candidate)
    )


@dataclass(frozen=True)
class TurnContinuationRequest:
    """One bounded request to keep the current agent turn running."""

    prompt: str
    reason: str
    attempt: int
    max_attempts: int


class TurnContinuationController:
    """Own per-turn continuation policy and its bounded retry counter."""

    def __init__(self, *, max_continuations: int = DEFAULT_MAX_CONTINUATIONS) -> None:
        self._max_continuations = max(0, max_continuations)
        self._continuations = 0

    def evaluate(
        self,
        *,
        content: str,
        finish_reason: str | None,
        tools_available: bool,
        step: int,
        max_steps: int,
        cancelled: bool,
        session_id: str = "",
    ) -> TurnContinuationRequest | None:
        """Return a continuation request for a candidate premature stop."""
        if (
            cancelled
            or not tools_available
            or finish_reason not in _NORMAL_FINISH_REASONS
            or step + 1 >= max_steps
            or self._continuations >= self._max_continuations
            or not announces_unfinished_action(content)
        ):
            return None

        self._continuations += 1
        request = TurnContinuationRequest(
            prompt=_CONTINUATION_PROMPT,
            reason="announced_action_without_tool_call",
            attempt=self._continuations,
            max_attempts=self._max_continuations,
        )
        log.info(
            "turn_continuation/injected session_id=%s reason=%s attempt=%d/%d",
            session_id or "-",
            request.reason,
            request.attempt,
            request.max_attempts,
        )
        return request
