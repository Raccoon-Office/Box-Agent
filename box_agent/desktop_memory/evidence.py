"""Fixed, redacted views from this user turn's canonical Session Log suffix."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from ..connector_context import matches_connector_snapshot
from ..llm.debug_logging import sanitize_for_logging
from ..session_log import SessionLog
from ..turn_hints import with_turn_hints


VERSION = "desktop-experience-v1"
MAX_BYTES = 64 * 1024
MAX_EVENTS = 2048
MAX_VIEWS = 12
EXCERPT_BYTES = 4096
_SENSITIVE_FIELD = re.compile(
    r"(?i)(?!(?:[\w.-]+[_.-])?(?:total|input|output|prompt|completion)[_.-]token$)"
    r"(?:[\w.-]+[_.-])?(?:authorization|(?:set[_.-]?)?cookie|password\d*|pgpassword|passwd|pwd|pass|passphrase|credentials|"
    r"(?:api|access|private|secret|db|encryption|signing|ssh)[_.-]?key(?:[_.-]?id)?|"
    r"(?:(?:access|refresh|session|auth|bearer|api)[_.-]?)?token|"
    r"(?:client[_.-]?)?secret|secret[_.-]?access[_.-]?key|密码|密钥|令牌)"
)
_ASSIGNMENT = re.compile(r"(?<![\w.-])([\w.-]+)[\\\"']*\s*[:=：]")
_SECRET_TEXT = re.compile(
    r"(?i)\bbearer\s+(?!token\b)\S+|\b(?:api|access|private|secret)\s+key\s*[:=：]|"
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
    r"glpat-[A-Za-z0-9_-]+|xox[baprs]-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]{12,}|"
    r"sk_live_[A-Za-z0-9_]+|hf_[A-Za-z0-9_]+|npm_[A-Za-z0-9_]+|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b|"
    r"(?<![\w-])(?:--(?:password|passwd|passphrase|token|api-key|credentials)(?=[\s=\\\"'])|-p(?=\S))"
)
_CURL = re.compile(r"(?i)\bcurl\b")
_USER_ARGUMENT = re.compile(r"(?<![\w-])(?:-u|--user)(?=[\s=\\\"'])")
# Do not split userinfo with two overlapping quantifiers: colon-heavy input
# without '@' otherwise causes quadratic backtracking on the event loop.
_URL_USERINFO = re.compile(r"://([^/\s@]*)@")
_ATTACHMENT = re.compile(r"(?i)data:(?:image|audio|video|application)/[^;,\s]+;base64,[A-Za-z0-9+/_=\r\n-]+")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact(value: Any) -> Any:
    value = sanitize_for_logging(value)
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SENSITIVE_FIELD.fullmatch(str(key)) else _redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        # Do not guess where an untrusted quoted/multiline credential ends.
        # Omit the whole suspect value rather than retaining a secret suffix.
        if (_SECRET_TEXT.search(value)
                or _CURL.search(value) and _USER_ARGUMENT.search(value)
                or any(":" in match[1] for match in _URL_USERINFO.finditer(value))
                or any(_SENSITIVE_FIELD.fullmatch(match[1]) for match in _ASSIGNMENT.finditer(value))):
            return "[REDACTED]"
        return _ATTACHMENT.sub("[OMITTED_ATTACHMENT]", value)
    return value


def no_evidence(session_id: str, turn_id: str, event_id: str, reason: str) -> dict[str, Any]:
    return {"schema": VERSION, "session_id": session_id, "turn_id": turn_id,
            "event_id": event_id, "status": "no_evidence", "reason": reason, "views": []}


def _is_turn_user(event: dict[str, Any], variants: tuple[str, ...]) -> bool:
    if event["type"] != "user/message":
        return False
    data = event["data"]
    content = data.get("content")
    return data.get("source") == "user" and (
        content in variants
        or matches_connector_snapshot(content, variants, data.get("host_connector_status"))
    )


@dataclass(frozen=True, repr=False)
class EvidenceCursor:
    log: SessionLog
    start: int
    source_id: str
    user_variants: tuple[str, ...] = ()

    @classmethod
    def begin(cls, log: Any, user: str = "", *, wrapped_user: str | None = None,
              user_seq: int | None = None) -> EvidenceCursor | None:
        if not isinstance(log, SessionLog) or log.failed:
            return None
        start = log.event_count
        variants = tuple(dict.fromkeys([user, with_turn_hints(user), *([wrapped_user] if wrapped_user is not None else [])]))
        if user_seq is not None:
            if type(user_seq) is not int or not 0 <= user_seq < start:
                raise ValueError("invalid current user sequence")
            event = log.event_page(user_seq, limit=1)[0]
            if not _is_turn_user(event, variants):
                raise ValueError("current user event does not match")
            start = user_seq
        return cls(log, start, "session-log:" + _hash(str(log.header["id"])), variants)

    def capture(self, *, session_id: str, turn_id: str, event_id: str,
                user: str, assistant: str, end: int) -> dict[str, Any]:
        missing = lambda reason: no_evidence(session_id, turn_id, event_id, reason)
        if self.log.failed or not self.start <= end <= self.log.event_count:
            return missing("log_unavailable")
        if end - self.start > MAX_EVENTS:
            return missing("event_budget")
        # No provider call or historical replay. A failure never supplies evidence.
        self.log.flush()
        events = []
        cursor = self.start
        while cursor < end:
            page = self.log.event_page(cursor, limit=min(64, end - cursor))
            if not page:
                return missing("log_unavailable")
            events.extend(page)
            cursor += len(page)
        completed = [e for e in events if e["type"] == "turn/end"
                     and e["data"].get("reason", {}).get("kind") == "completed"]
        variants = self.user_variants if user in self.user_variants else (user, with_turn_hints(user))
        explicit = [e for e in events if _is_turn_user(e, variants)]
        final = [e for e in events if e["type"] == "assistant/message"
                 and e["data"].get("message", {}).get("content") == assistant]
        if not completed or not explicit or not final:
            return missing("incomplete_turn_evidence")
        # Final answer belongs before the final completed boundary, never to a later turn.
        if not explicit[0]["seq"] < final[-1]["seq"] < completed[-1]["seq"]:
            return missing("incomplete_turn_evidence")
        selected = [(explicit[0], {"content": user}, None),
                    (final[-1], {"content": assistant, "claim_only": True}, None),
                    (completed[-1], {"reason": "completed"}, None)]
        calls = {e["data"].get("callId"): e for e in events if e["type"] == "tool/call"
                 and isinstance(e["data"].get("callId"), str)}
        duplicated = {key for key in calls if sum(e["type"] == "tool/call" and
                      e["data"].get("callId") == key for e in events) > 1}
        omitted = 0
        for event in events:
            if event["type"] != "tool/result":
                continue
            data = event["data"]
            call_id = data.get("message", {}).get("tool_call_id")
            call = calls.get(call_id)
            result = data.get("result")
            name = call["data"].get("name") if call else None
            if isinstance(name, str) and name.startswith("memory_"):
                omitted += 1
                continue
            if (not call or call_id in duplicated or not isinstance(result, dict) or type(result.get("success")) is not bool
                    or not explicit[0]["seq"] < call["seq"] < event["seq"] < completed[-1]["seq"]):
                omitted += 1
                continue
            if len(selected) + 2 > MAX_VIEWS:
                omitted += 1
                continue
            selected.extend([(call, {"name": call["data"].get("name"),
                                      "arguments": call["data"].get("arguments")}, call_id),
                             (event, {"success": result["success"],
                                      "content": result.get("content"), "error": result.get("error")}, call_id)])
        views = []
        for event, payload, call_id in selected:
            text = _json(_redact(payload))
            encoded = text.encode("utf-8")
            truncated = len(encoded) > EXCERPT_BYTES
            if truncated:
                text = encoded[:EXCERPT_BYTES].decode("utf-8", errors="ignore")
            coverage = "excerpt" if truncated or event["type"] == "user/message" and event["data"].get("content") != user else "full"
            ref = {"evidence_id": "ev:" + _hash(_json([event_id, event["seq"], _hash(text)])),
                   "source_id": self.source_id, "session_id": session_id,
                   "event_seqs": [event["seq"]], "event_type": event["type"], "call_id": call_id,
                   "coverage": coverage,
                   "view_spec": {"format_version": VERSION, "excerpt_limit": EXCERPT_BYTES if coverage == "excerpt" else None},
                   "visible_sha256": _hash(text)}
            views.append({"text": text, "source_ref": ref})
        bundle = {"schema": VERSION, "session_id": session_id, "turn_id": turn_id,
                  "event_id": event_id, "status": "captured", "views": views, "omitted": omitted}
        if len(_json(bundle).encode("utf-8")) > MAX_BYTES:
            return missing("view_budget")
        return bundle
