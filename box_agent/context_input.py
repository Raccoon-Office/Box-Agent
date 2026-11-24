"""Run-scoped assembly of model input over borrowed session capabilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from contextlib import nullcontext
from datetime import date
import inspect
import logging
from time import perf_counter
from typing import Any, Callable

from .kernel.context_engine import (
    ContextTokenCalibration,
    context_text_counter,
    request_input_tokens, skill_reference_budget_chars,
)
from .kernel.context_types import ContextCompactionState, InitialContextTransition
from .schema import Message
from .session_log import run_on_session_loop, run_session_write
from .llm.capabilities import bound_context_token_limit
from .connector_context import restore_connector_status
from .input_context import project_user_message
from .skill_context import (
    SkillReferenceContext, effective_system_prompt, project_legacy_system, verified_system_suffixes,
)
from .tools.base import ToolResult
from .tools.engine.contracts import PreparedTools

_log = logging.getLogger(__name__)


def _latest_current_date(messages: list[Message]) -> Message | None:
    return next((message for message in reversed(messages) if message.role == "user"
                 and message.source == "runtime" and message.name == "current_date"), None)


def _current_date_update(messages: list[Message]) -> tuple[Message, ...]:
    """Stage a runtime date only when the effective history needs an update."""
    if (not messages or messages[0].role != "system" or not isinstance(messages[0].content, str)
            or "`<current_date>`" not in messages[0].content):
        return ()
    content = f"<current_date>{date.today().isoformat()}</current_date>"
    previous = _latest_current_date(messages)
    if previous is not None and previous.content == content:
        return ()
    return (Message(role="user", source="runtime", name="current_date", content=content),)


@dataclass(frozen=True, slots=True)
class PreparedContext:
    """One request projection; durable history and tool admission stay external."""

    messages: list[Message]
    context_messages: list[Message]
    references: tuple[dict[str, Any], ...] = ()
    input_tokens: int = 0
    request_only_input_tokens: int = 0
    blocked_reason: str | None = None
    on_committed: Callable[[], None] | None = None
    budget_blocked: bool = False
    on_response: Callable[[], None] | None = None
    reader_required: bool = False
    image_context: dict[str, Any] | None = None
    image_notices: tuple[str, ...] = ()
    required_reference_tokens: int = 0
    input_context: dict[str, Any] | None = None
    materialized_skill_messages: tuple[Message, ...] = ()
    # Complete request cost used by the guard; None preserves older contexts.
    total_input_tokens: int | None = None
    history_updates: tuple[Message, ...] = ()


class ModelInputGuard(ABC):
    """Optional capability checks on final input, without editing durable history."""

    @abstractmethod
    def bind_context(self, context: DefaultContextEngine) -> None:
        """Borrow the exact run's schemas, history and input budget."""

    @abstractmethod
    def prepare_context(self, projection: PreparedContext) -> PreparedContext:
        """Compose delivery callbacks or reject a projection before dispatch."""

    def project_messages(self, messages: list[Message], *, tools: PreparedTools, token_limit: int,
                         tail: tuple[Message, ...] = (), reserve_tokens: int = 0) -> list[Message]:
        """Bound owned optional input before references and shared budget checks."""
        return messages


class DefaultContextEngine:
    """Own request coverage and budgets, borrowing the final resolved services.

    The factory takes no services. Composition binds this run only after
    plugin replacement and Skill/tool source validation have completed.
    """

    def __init__(self, *, system_prompt_projector: Callable[
        [str, str | tuple[str, ...]], str,
    ] = effective_system_prompt, image_context=None, llm=None, thinking_enabled=False,
        initial_context_transition: InitialContextTransition | None = None,
        token_calibration: ContextTokenCalibration | None = None,
        compaction_state: ContextCompactionState | None = None,
    ) -> None:
        self._system_prompt_projector = system_prompt_projector
        self.image_context = image_context
        self.llm = llm
        self.thinking_enabled = thinking_enabled
        self.initial_context_transition = initial_context_transition
        self.token_calibration = token_calibration if token_calibration is not None else ContextTokenCalibration()
        self.compaction_state = compaction_state if compaction_state is not None else ContextCompactionState()
        self._memory_lookup = None
        self._input_guard: ModelInputGuard | None = None
        self.references: SkillReferenceContext | None = None
        self.prepared_tools: PreparedTools | None = None
        self._history: list[Message] = []
        self._extra_messages: tuple[Message, ...] = ()
        self._token_limit = 113400
        self._output_tokens = 0
        self._request_reference_tokens = 0
        self._transient_tokens = 0
        self._transient_reserve_tokens = 0
        self._pending_followup_blocks: list[dict[str, Any]] = []
        self._input_adoptions: set[str] = set()
        self._input_settled: set[str] = set()
        self._session_store = None

    def configure_run(self, *, skill_engine: Any = None, session_store: Any = None) -> None:
        self._session_store = session_store
        self._input_adoptions = set()
        self._input_settled = set()
        for event in getattr(session_store, "events", ()):
            if event["type"] == "input/adopted":
                self._input_adoptions.add(event["data"]["id"])
            elif event["type"] == "assistant/message" and not event["data"].get("surfaceDerived"):
                self._input_settled.update(self._input_adoptions)
        if self.image_context is None:
            from .image_context import ImageContext
            self.image_context = ImageContext(session_store)
        elif self.image_context.session_log is not session_store:
            raise ValueError("Image context and final SessionStore must belong to the same session")
        self.references = (SkillReferenceContext(skill_engine, session_store=session_store)
                           if skill_engine is not None else None)
        self.prepared_tools = None
        self._history = []

    def bind_model(self, llm: Any, *, thinking_enabled: bool = False) -> None:
        self.llm = llm
        self.thinking_enabled = thinking_enabled

    def commit_response(self, message: Message) -> None:
        if self.image_context is not None:
            self.image_context.mark_response(message)

    def prepare_summary(self, messages: list[Message], llm: Any) -> list[Message]:
        return self._prepare_summary(messages, llm, preview=False)

    def preview_summary(self, messages: list[Message], llm: Any) -> list[Message]:
        return self._prepare_summary(messages, llm, preview=True)

    def _prepare_summary(self, messages: list[Message], llm: Any, *, preview: bool) -> list[Message]:
        projected = [project_user_message(message) for message in messages]
        messages = restore_connector_status(messages if all(a is b for a, b in zip(projected, messages)) else projected)
        if self.image_context is None or not self.image_context.images:
            return messages
        prepare = self.image_context.preview if preview else self.image_context.prepare
        projection = prepare(messages, llm=llm, purpose="summary")
        if not preview and self.image_context.session_log is not None:
            self.image_context.session_log.append("request/context", {"imageContext": projection.manifest})
            self.image_context.session_log.flush()
        if projection.blocked_reason:
            raise ValueError(projection.blocked_reason)
        return projection.messages

    def bind_memory(self, memory_lookup: Any) -> None:
        """Bind optional memory after run capabilities have been resolved."""
        self._memory_lookup = memory_lookup
        guard = getattr(memory_lookup, "model_input_guard", None)
        self._input_guard = guard if isinstance(guard, ModelInputGuard) else None
        if self._input_guard is not None:
            self._input_guard.bind_context(self)

    def bind_history(self, messages: list[Message]) -> None:
        """Observe exact tool history before Kernel may compact it; never edit it."""
        self._history = messages
        if self.references is not None:
            self.references.bind_history(messages)

    async def abind_history(self, messages: list[Message]) -> None:
        """Bind text now; preparation observes its sources before compaction."""
        if getattr(self.bind_history, "__func__", None) is not DefaultContextEngine.bind_history:
            runtime = self.references.runtime if self.references is not None else None
            validate = getattr(runtime, "avalidate_references", None)
            validation = (await validate(self.references.source_names(messages)) if callable(validate) else None)
            with runtime.reference_scope(validation) if validation is not None else nullcontext():
                self.bind_history(messages)
            return
        self._history = messages
        if self.references is not None:
            self.references._messages = messages

    def project_history(self, messages: list[Message]) -> list[Message]:
        """Effective rules for budgeting/compaction, without reference delivery."""
        suffix = verified_system_suffixes(self.references.runtime) if self.references is not None else ()
        return restore_connector_status(
            project_legacy_system([project_user_message(message) for message in messages], suffix, self._system_prompt_projector),
        )

    @property
    def tool_reader(self):
        return self._read_reference if self.references is not None else None

    def compact_history(
        self, original: list[Message], compacted: list[Message], *, token_limit: int | None = None,
        max_skill_bodies: int | None = None, defer_skill_budget: bool = False,
    ) -> list[Message]:
        """Apply resource retention only after the existing compaction decision."""
        if self.references is not None:
            definitions = self.prepared_tools.definitions if self.prepared_tools is not None else ()
            limit = self._token_limit if token_limit is None else min(self._token_limit, token_limit)

            def budget(messages: list[Message]) -> int:
                return skill_reference_budget_chars(messages, definitions, limit, llm=self.llm) // 4

            kwargs = {} if defer_skill_budget else {"budget_tokens": budget}
            compacted = self.references.compact_history(
                original, compacted, count_text_tokens=context_text_counter(self.llm),
                max_skill_bodies=max_skill_bodies, **kwargs,
            )
        latest_date = _latest_current_date(original)
        if latest_date is not None and _latest_current_date(compacted) != latest_date:
            return [*compacted, latest_date]
        return compacted

    async def acompact_history(
        self, original: list[Message], compacted: list[Message], *, token_limit: int | None = None,
        max_skill_bodies: int | None = None, defer_skill_budget: bool = False,
    ) -> list[Message]:
        """Keep source reads bounded and off the event loop on the compaction path."""
        runtime = self.references.runtime if self.references is not None else None
        validate = getattr(runtime, "avalidate_references", None)
        names = self.references.source_names(original) if self.references is not None else ()
        validation = None
        if names and callable(validate):
            kwargs = {"allow_stale": False} if "allow_stale" in inspect.signature(validate).parameters else {}
            validation = await validate(names, **kwargs)
        with runtime.reference_scope(validation) if validation is not None else nullcontext():
            parameters = inspect.signature(self.compact_history).parameters
            kwargs = {}
            if "token_limit" in parameters:
                kwargs["token_limit"] = token_limit
            if "max_skill_bodies" in parameters:
                kwargs["max_skill_bodies"] = max_skill_bodies
            if "defer_skill_budget" in parameters:
                kwargs["defer_skill_budget"] = defer_skill_budget
            return self.compact_history(original, compacted, **kwargs)

    def compaction_source(self, messages: list[Message]) -> list[Message]:
        """Keep method bodies out of both generated and deterministic summaries."""
        return self.references.compaction_source(messages) if self.references is not None else messages


    def reserve_followup(self, blocks: list[dict[str, Any]]) -> None:
        """Reserve already accepted request-only material during a serial batch."""
        self._pending_followup_blocks.extend(blocks)

    def _can_page(self, names: tuple[str, ...]) -> bool:
        from .tools.skill_tool import GetSkillTool

        assert self.prepared_tools is not None
        for name, target in self.prepared_tools.targets.items():
            if not isinstance(target, GetSkillTool) or self.prepared_tools.validate_call(name) is not None:
                continue
            validation = getattr(self.references.runtime, "_reference_validation", None) if self.references else None
            if all(target.check_access(skill_name, catalog=validation.catalog if validation else None,
                                       refresh=validation is None) is None for skill_name in names):
                return True
        return False

    def _read_reference(self, name: str, **arguments: Any) -> ToolResult:
        assert self.references is not None
        self.references.bind_history([*self._history, *self._extra_messages])
        # Tool arguments and earlier committed replies may have been added
        # since preparation. Keep the exact offered schema/admission snapshot.
        calls = next((message.tool_calls for message in reversed(self._history)
                      if message.role == "assistant" and message.tool_calls), ())
        committed = {message.tool_call_id for message in self._history if message.role == "tool"}
        envelopes = [Message(role="tool", name=call.function.name, tool_call_id=call.id, content="")
                     for call in calls if call.id not in committed]
        definitions = self.prepared_tools.definitions if self.prepared_tools is not None else ()
        pending = ([Message(role="user", source="runtime", content=list(self._pending_followup_blocks))]
                   if self._pending_followup_blocks else [])
        context_messages = self.project_history([*self._history, *envelopes, *self._extra_messages, *pending])
        # Budget the same optional input projection as the next model request.
        if self._input_guard is not None and self.prepared_tools is not None:
            context_messages = self._input_guard.project_messages(
                context_messages, tools=self.prepared_tools, token_limit=self._token_limit,
                reserve_tokens=self._transient_reserve_tokens,
            )
        available = skill_reference_budget_chars(
            context_messages, definitions,
            self._token_limit - self._transient_reserve_tokens, self._output_tokens, llm=self.llm,
            calibration=self.token_calibration,
        )
        return self.references.read(name, **arguments, budget_chars=max(
            0, available - self._request_reference_tokens * 4,
        ))

    def _source_names(self, messages: list[Message], arguments: dict[str, Any]) -> tuple[str, ...]:
        extra = arguments.get("extra_messages", ())
        transient = arguments.get("transient_message")
        return self.references.source_names([*messages, *extra, *((transient,) if transient is not None else ())])

    async def aprepare_request(self, messages: list[Message], **arguments: Any) -> PreparedContext:
        runtime = self.references.runtime if self.references is not None else None
        validate = getattr(runtime, "avalidate_references", None)
        if not callable(validate):
            return await run_session_write(self._session_store, self.prepare_request, messages, **arguments)
        started = perf_counter()
        names = self._source_names(messages, arguments)
        validation = None
        try:
            validation = await validate(names)
            with runtime.reference_scope(validation):
                return await run_session_write(self._session_store, self.prepare_request, messages, **arguments)
        finally:
            _log.info("context/prepare duration_ms=%.3f unique_skills=%d validation_timeouts=%d",
                      (perf_counter() - started) * 1000, len(names), int(getattr(validation, "timed_out", False)))

    async def apreview_request(self, messages: list[Message], **arguments: Any) -> PreparedContext:
        return await self.aprepare_request(messages, defer_image_changes=True, **arguments)

    def prepare_request(
        self, messages: list[Message], *, prepared_tools: PreparedTools,
        token_limit: int, output_tokens: int = 0,
        extra_messages: tuple[Message, ...] = (),
        transient_message: Message | None = None, transient_tokens: int = 0,
        defer_image_changes: bool = False,
    ) -> PreparedContext:
        arguments = dict(prepared_tools=prepared_tools, token_limit=token_limit, output_tokens=output_tokens,
                         extra_messages=extra_messages, transient_message=transient_message,
                         transient_tokens=transient_tokens, defer_image_changes=defer_image_changes)
        runtime = self.references.runtime if self.references is not None else None
        validate = getattr(runtime, "validate_references", None)
        validation = getattr(runtime, "_reference_validation", None)
        if validation is None and callable(validate):
            validation = validate(self._source_names(messages, arguments))
        with runtime.reference_scope(validation) if validation is not None else nullcontext():
            return self._prepare_request(messages, **arguments)

    def _prepare_request(
        self, messages: list[Message], *, prepared_tools: PreparedTools,
        token_limit: int, output_tokens: int = 0,
        extra_messages: tuple[Message, ...] = (),
        transient_message: Message | None = None, transient_tokens: int = 0,
        defer_image_changes: bool = False,
    ) -> PreparedContext:
        """Project ordinary reference material using the offered tools unchanged."""
        token_limit = bound_context_token_limit(self.llm, token_limit)
        restore = getattr(self.references.runtime, "restore_deferred_records", None) if self.references else None
        if callable(restore):
            restore()
        self.bind_history(messages)
        self._pending_followup_blocks = []
        self.prepared_tools = prepared_tools
        history_updates = _current_date_update(messages)
        history = [*messages, *history_updates] if history_updates else messages
        request_tail = (transient_message,) if transient_message is not None else ()
        from .correction import prepare_correction_context
        available = skill_reference_budget_chars(
            self.project_history([*history, *extra_messages, *request_tail]),
            prepared_tools.definitions, max(0, token_limit - max(0, transient_tokens)), output_tokens, llm=self.llm,
            calibration=self.token_calibration,
        )
        correction_context = prepare_correction_context(
            self._memory_lookup, prepared_tools.targets,
            self.references.runtime if self.references is not None else None, messages,
            budget_chars=min(1800, max(0, available)),
        )
        if correction_context is not None:
            extra_messages = (*extra_messages, correction_context)
        self._extra_messages = (*extra_messages, *request_tail)
        self._token_limit, self._output_tokens = token_limit, output_tokens
        transient_input_tokens = (request_input_tokens([transient_message], {}, llm=self.llm)
                                  if transient_message is not None else 0)
        self._transient_tokens = (max(transient_tokens, transient_input_tokens)
                                  if transient_message is not None else 0)
        self._transient_reserve_tokens = (max(
            0, self._transient_tokens - transient_input_tokens,
        ) if transient_message is not None else 0)
        context_messages = self.project_history([*history, *extra_messages])
        if self._input_guard is not None:
            context_messages = self._input_guard.project_messages(
                context_messages, tools=prepared_tools, token_limit=token_limit,
                tail=request_tail,
                reserve_tokens=self._transient_reserve_tokens,
            )
        references: tuple[dict[str, Any], ...] = ()
        materialized_skill_messages: tuple[Message, ...] = ()
        self._request_reference_tokens = 0
        blocked_reason = None
        budget_blocked = False
        reader_required = False
        required_reference_tokens = 0
        on_committed = None
        on_response = None
        full_request = [*context_messages, *request_tail] if request_tail else context_messages
        base_input_tokens = (request_input_tokens(
            full_request, prepared_tools.definitions, llm=self.llm, calibration=self.token_calibration,
        )
                             + self._transient_reserve_tokens)
        if self.references is not None:
            projection = self.references.prepare_request(
                context_messages,
                can_page=self._can_page,
                defer_delivery=True,
                count_text_tokens=context_text_counter(self.llm),
                project_system=False,
                budget_chars=skill_reference_budget_chars(
                    full_request, prepared_tools.definitions,
                    token_limit - self._transient_reserve_tokens, output_tokens, llm=self.llm,
                    calibration=self.token_calibration,
                ),
            )
            context_messages, references = projection.messages, projection.references
            materialized_skill_messages = projection.materialized_messages
            if materialized_skill_messages:
                # Ephemeral request extras must follow the persisted tail so
                # the next request retains the same historical prefix.
                count = len(materialized_skill_messages)
                tail = context_messages[-count:]
                rest = context_messages[:-count]
                insertion = min(len(history), len(rest))
                context_messages = [*rest[:insertion], *tail, *rest[insertion:]]
            blocked_reason = projection.blocked_reason
            budget_blocked = projection.budget_blocked
            reader_required = projection.reader_required
            required_reference_tokens = projection.required_reference_tokens
            on_committed = projection.on_committed
            on_response = projection.on_response
            self._request_reference_tokens = projection.input_tokens
            # Materialized Skill messages are durable history, but their
            # delivery facts must not be acknowledged until the kernel has
            # committed the request/context record.  This keeps ACP usage
            # attribution correct while leaving cancelled or blocked requests
            # unacknowledged.
            acknowledge = getattr(
                self.references.runtime,
                "acknowledge_pending_materialized",
                None,
            )
            if callable(acknowledge):
                previous_on_committed = on_committed

                def commit_materialized() -> None:
                    if previous_on_committed is not None:
                        previous_on_committed()
                    acknowledge()

                on_committed = commit_materialized
        actual_input_tokens = base_input_tokens + self._request_reference_tokens
        if getattr(self.references.runtime if self.references else None, "append_only_context", False):
            actual_input_tokens = request_input_tokens(
                [*context_messages, *request_tail],
                prepared_tools.definitions, llm=self.llm, calibration=self.token_calibration,
            ) + self._transient_reserve_tokens
        total_input_tokens = max(base_input_tokens + self._request_reference_tokens, actual_input_tokens)
        if not blocked_reason and total_input_tokens > token_limit:
            blocked_reason = "Model input exceeds the safe context budget. Compact context before retrying."
            budget_blocked = True
            on_committed = None
            on_response = None
        provider_messages = [*context_messages, *request_tail] if request_tail else context_messages
        image_projection = None
        if self.image_context is not None:
            prepare_images = self.image_context.preview if defer_image_changes else self.image_context.prepare
            image_projection = prepare_images(
                provider_messages, llm=self.llm, tools=list(prepared_tools.definitions),
                thinking_enabled=self.thinking_enabled,
            )
            provider_messages = image_projection.messages
            total_input_tokens = max(total_input_tokens, request_input_tokens(
                provider_messages, prepared_tools.definitions, llm=self.llm, calibration=self.token_calibration,
            ) + self._transient_reserve_tokens)
            if image_projection.blocked_reason:
                blocked_reason = image_projection.blocked_reason
                budget_blocked = defer_image_changes
            elif not blocked_reason and total_input_tokens > token_limit:
                blocked_reason = "Model input exceeds the safe context budget. Compact context before retrying."
                budget_blocked = True
            if image_projection.on_committed is not None:
                commit_images = image_projection.on_committed
                commit_references = on_committed

                def commit_projection() -> None:
                    if commit_references is not None:
                        commit_references()
                    commit_images()

                on_committed = commit_projection
            if blocked_reason:
                on_committed = on_response = None
        from .input_context import requires_input_contract
        input_records = [message.input_facts for message in messages
                         if message.input_facts and requires_input_contract(message.input_facts)]
        input_manifest = None
        if input_records:
            from .input_resources import verify_input_versions
            pending = [record for record in input_records if record["id"] not in self._input_settled]
            input_manifest = {"phase": "candidate", "inputs": [
                {"id": record["id"], "fingerprint": record["fingerprint"],
                 "files": record["context"].get("files", [])} for record in input_records
            ]}
            try:
                for record in pending:
                    verify_input_versions(record["context"].get("files", []), record["workspace"])
                    selected_images = {item["imageId"] for item in
                                       (image_projection.manifest.get("selected", []) if image_projection else [])}
                    if any(ref.get("eligible", True) and ref["imageId"] not in selected_images
                           for ref in record.get("images", [])):
                        raise ValueError("INPUT_IMAGE_NOT_ADOPTED:required_image_unavailable_or_over_budget")
            except ValueError as exc:
                blocked_reason = str(exc)
            if blocked_reason:
                on_committed = on_response = None
            else:
                commit_previous = on_committed

                def commit_inputs() -> None:
                    for record in pending:
                        verify_input_versions(record["context"].get("files", []), record["workspace"])
                    if commit_previous is not None:
                        commit_previous()
                    for record in pending:
                        if self._session_store is not None and record["id"] not in self._input_adoptions:
                            self._session_store.append("input/adopted", {
                                "id": record["id"], "fingerprint": record["fingerprint"],
                                "files": record["context"].get("files", []),
                            })
                    if self._session_store is not None:
                        self._session_store.flush()
                    self._input_adoptions.update(record["id"] for record in pending)

                response_previous = on_response

                def settle_inputs() -> None:
                    if response_previous is not None:
                        response_previous()
                    self._input_settled.update(record["id"] for record in pending)

                on_committed = commit_inputs
                on_response = settle_inputs
        projection = PreparedContext(
            provider_messages, context_messages, references,
            self._request_reference_tokens,
            self._request_reference_tokens + self._transient_tokens
            + (image_projection.input_tokens if image_projection else 0),
            blocked_reason, on_committed, budget_blocked, on_response,
            reader_required,
            image_projection.manifest if image_projection else None,
            image_projection.notices if image_projection else (),
            required_reference_tokens, input_manifest, materialized_skill_messages,
            total_input_tokens=total_input_tokens,
            history_updates=history_updates,
        )
        if self._input_guard is not None:
            return run_on_session_loop(self._input_guard.prepare_context, projection)
        return projection
