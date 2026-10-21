"""ACP 展示元数据的唯一维护入口，不改变任务执行策略或原有正文。

跟踪一次请求中的普通输出、重试和补写：messageId 标识逻辑回答，
outputId 标识单次模型输出；最外层请求结束后才发布最终回答归属。
老客户端可以忽略这些扩展字段，继续消费原来的 ACP 文本事件。
"""

from uuid import uuid4

from box_agent.events import (
    ContentEvent, DoneEvent, InjectedMessageEvent, LLMOutputEvent, StopReason,
)
from box_agent.injections import InjectionKind


class MessageDisplay:
    """Track one prompt, including multiple internal runs and text continuations.

    Chunks are deliberately unclassified while streaming: providers often decide
    to call a tool only after emitting text. Only the outer prompt response may
    publish a final receipt, after autopilot and producer cleanup have settled.
    """

    def __init__(self) -> None:
        self.begin_run()

    def begin_run(self) -> None:
        # Autopilot starts another run within the same prompt. Earlier answers
        # remain trace entries; UUIDs keep their identities distinct across runs.
        self.message_id: str | None = None
        # message_id 用于拼接同一回答；output_id 区分每次模型输出，补写也必须重新分配。
        # 这样客户端既能接回被截断的正文，也不会把补写快照误判成旧快照。
        self.output_id: str | None = None
        self.text_parts: list[str] = []
        self.last_text = ""
        self.answer_ids: list[str] = []
        self.final_ids: list[str] = []

    def observe(self, event: object) -> dict | None:
        if isinstance(event, ContentEvent) and not event._header and event.content:
            if self.message_id is None:
                self.message_id = uuid4().hex
            if self.output_id is None:
                self.output_id = uuid4().hex
            self.text_parts.append(event.content)
            return {"version": 1, "messageId": self.message_id, "outputId": self.output_id}
        if isinstance(event, LLMOutputEvent):
            identifier = self.message_id or uuid4().hex
            output_id = self.output_id or uuid4().hex
            # A no-tool output is only a candidate, never a final-answer signal.
            if event.tool_calls or not event.content:
                self.answer_ids = []
            else:
                self.answer_ids = [identifier]
            self.last_text = event.content
            self.message_id = None
            self.output_id = None
            self.text_parts = []
            return {"version": 1, "messageId": identifier, "outputId": output_id}
        if isinstance(event, InjectedMessageEvent):
            # Text continuation completes a truncated answer. Task continuation,
            # recovery and user steering produce a new answer instead.
            # Reuse the logical message ID for truncation: a continuation may
            # start in the middle of a word, table row or Markdown code fence.
            # Recovery keeps the failed partial text under a separate old ID.
            if event.kind == InjectionKind.TEXT_CONTINUATION.value:
                self.message_id = self.answer_ids[-1] if self.answer_ids else None
            elif event.kind not in {
                InjectionKind.BUDGET.value, InjectionKind.NO_PROGRESS.value,
            }:
                self.message_id = None
            # Advisory wrap-up nudges may arrive after the continuation request;
            # they do not revoke its identity or guarantee a rewritten answer.
            self.output_id = None
            self.text_parts = []
        if isinstance(event, DoneEvent) and event.stop_reason == StopReason.END_TURN:
            text = "".join(self.text_parts)
            if self.message_id and text == event.final_content and text.strip():
                self.final_ids = [self.message_id]
            elif self.last_text == event.final_content and self.last_text.strip():
                self.final_ids = self.answer_ids.copy()
        return None

    def receipt(self, *, completed: bool, duration_ms: int) -> dict:
        # Failure, cancellation, waiting and budget exhaustion must not hide the
        # partial reply. Clients only fold tagged text when final IDs are present.
        return {
            "version": 1,
            "finalMessageIds": self.final_ids if completed else [],
            "durationMs": max(0, duration_ms),
        }
