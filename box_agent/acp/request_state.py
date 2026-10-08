"""Request-scoped ACP bookkeeping without a connection-long payload archive."""

import asyncio
from typing import Any

from acp.task.sender import MessageSender
from acp.task.state import IncomingMessage, InMemoryMessageStateStore, OutgoingMessage
from acp.task.supervisor import TaskSupervisor


class RequestStateStore(InMemoryMessageStateStore):
    """Keep live outgoing reply correlation, not completed request history.

    The dispatcher owns each incoming record until its request task settles.
    No connection-owned reference is needed, including when cancellation skips
    the SDK's complete/fail callbacks. Conversation state lives in BoxACPAgent
    and SessionLog; these records are not used to restore or route sessions.
    """

    def register_outgoing(self, request_id: int, method: str) -> asyncio.Future[Any]:
        future = super().register_outgoing(request_id, method)
        future.add_done_callback(lambda completed: self._release_outgoing(request_id, completed))
        return future

    def _release_outgoing(self, request_id: int, future: asyncio.Future[Any]) -> None:
        record = self._outgoing.get(request_id)
        if record is not None and record.future is future:
            self._outgoing.pop(request_id)

    def _abandon_outgoing(self, record: OutgoingMessage) -> None:
        self._release_outgoing(record.request_id, record.future)
        if not record.future.done():
            record.future.cancel()
        elif not record.future.cancelled():
            # A host error can arrive before sending finishes. If the send then
            # fails, send_request never awaits this future; retrieve its error.
            record.future.exception()

    def create_sender(self, writer: asyncio.StreamWriter, supervisor: TaskSupervisor) -> MessageSender:
        return _RequestMessageSender(writer, supervisor, self)

    def begin_incoming(self, method: str, params: Any) -> IncomingMessage:
        return IncomingMessage(method=method, params=params)

    def complete_incoming(self, record: IncomingMessage, result: Any) -> None:
        # Connection sends the response before the dispatcher invokes this hook.
        record.status = "completed"
        self._release_payloads(record)

    def fail_incoming(self, record: IncomingMessage, error: Any) -> None:
        # Error delivery/logging belongs to Connection and TaskSupervisor. Keeping
        # the exception here would also retain its traceback and request locals.
        record.status = "failed"
        self._release_payloads(record)

    @staticmethod
    def _release_payloads(record: IncomingMessage) -> None:
        record.params = None
        record.result = None
        record.error = None


class _RequestMessageSender(MessageSender):
    """Retire a request when SDK send_request exits before awaiting its reply."""

    def __init__(
        self, writer: asyncio.StreamWriter, supervisor: TaskSupervisor, store: RequestStateStore,
    ) -> None:
        super().__init__(writer, supervisor)
        self._store = store

    async def send(self, payload: dict[str, Any]) -> None:
        # Responses can share an ID with an unrelated outgoing request.
        record = self._store._outgoing.get(payload.get("id")) if "method" in payload else None
        try:
            await super().send(payload)
        except BaseException:
            # During serialization, queueing or drain the reply future is not
            # awaited yet, so cancellation will not propagate to it by itself.
            if record is not None:
                self._store._abandon_outgoing(record)
            raise
