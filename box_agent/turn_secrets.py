"""Per-turn credential notice for user messages that contain secrets.

The system prompt forbids repeating user-provided credentials, yet evals showed
the model echoing a pasted token while warning the user about it, putting a
pasted DB password in plain text into a config file, and quoting a full API
key in a format check. A notice beside the message names the detected values
in masked form only, so the model can refer to them without repeating them.

Detection is deliberately narrow (well-known key prefixes, bearer tokens,
JWTs, "password: …" phrases). Generic long hex strings are not matched; they
are usually commit SHAs or hashes.
"""

from __future__ import annotations

import re

_MAX_NOTICE_CREDENTIALS = 8

_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{15,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})"),

)


_PASSWORD = re.compile(
    r'''(?i)(?<![A-Za-z0-9_])['"]?(?:password|passwd|pwd|密码|口令)['"]?\s*(?:[:=：]|是|为)\s*(?:"([^"\n]{6,})"|'([^'\n]{6,})'|([^\s，。,;；'"`]{6,}))'''
)
_ENV_REFERENCE = re.compile(r"(?:\$\{[A-Za-z_]\w*\}|\$[A-Za-z_]\w*|%[A-Za-z_]\w*%|process\.env\.[A-Za-z_]\w*|os\.environ.*)")


def _password_values(text: str) -> list[tuple[int, str]]:
    values = []
    for match in _PASSWORD.finditer(text):
        index = next(i for i, value in enumerate(match.groups(), 1) if value is not None)
        value = match.group(index)
        if not _ENV_REFERENCE.fullmatch(value):
            values.append((match.start(index), value))
    return values


def detect_credentials(text: str) -> list[str]:
    """Up to eight credential examples, in appearance order, without duplicates."""
    found: list[tuple[int, str]] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1) if match.groups() else match.group(0)
            if _ENV_REFERENCE.fullmatch(value):
                continue
            start = match.start(1) if match.groups() else match.start()
            found.append((start, value))
    found.extend(_password_values(text))
    values: list[str] = []
    for _, value in sorted(found, key=lambda item: (item[0], -len(item[1]))):
        if value not in values and not any(value in other or other in value for other in values):
            values.append(value)
            if len(values) >= _MAX_NOTICE_CREDENTIALS:
                break
    return values


def mask_credential(value: str, *, password: bool = False) -> str:
    """Keep enough to identify a long prefixed key; reveal nothing of short secrets."""
    if not password and len(value) >= 24 and re.match(r"(?:sk-|gh[pousr]_|github_pat_|xox[abprs]-)", value):
        return f"{value[:8]}…{value[-4:]}"
    return "****"


def credential_notice(text: str) -> str | None:
    values = detect_credentials(text)
    if not values:
        return None
    passwords = {value for _, value in _password_values(text)}
    masked = "、".join(f"`{mask_credential(value, password=any(value in password or password in value for password in passwords))}`" for value in values)
    return (
        f"[敏感信息：本条消息含疑似凭据（{masked}）。以上仅为脱敏示例；"
        "回复、写入的文件和执行的命令中都不要出现任何凭据的完整值："
        "回复里需要指代时只用上面的脱敏形式；代码和配置改为从环境变量或配置项读取（如 `${DB_PASSWORD}`）；"
        "不要把它写进命令参数，需要实际调用时请用户在本地执行。]"
    )


__all__ = ["credential_notice", "detect_credentials", "mask_credential"]
