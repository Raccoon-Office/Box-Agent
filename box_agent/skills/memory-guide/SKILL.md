---
name: memory-guide
description: Guides the agent to proactively save user information to long-term memory and search context when needed. Load this skill to understand when and how to use memory_write, memory_read, and memory_search.
keywords: [memory, remember, 记忆, 记住, 保存, 长期记忆, 偏好]
---

# Memory Guide

Use the memory tools exposed in the current session. Check the tool schema before choosing how to save.

## Path-Based Memory Tools

Use this workflow when `memory_read` and `memory_write` require `path`.

- `user.md` holds stable user identity, preferences, and collaboration habits supported by explicit user statements.
- `memory.md` holds confirmed long-term rules, project context the user asks to keep, and confirmed decisions that will affect future work.

These are virtual memory paths. Access them through the memory tools.

Read the same path before editing and follow the rules returned by the read. For an existing path, prefer `memory_edit` to append, replace, or delete individual entries. Each non-empty `old_text` or `new_text` must contain complete `<mem>...</mem>` entries with non-empty bodies and plain tags. For replacement or deletion, copy the exact bodies of the existing consecutive entries into `old_text` and match them uniquely. Leave `old_text` empty to append or `new_text` empty to delete. Respect protected entries.

Example for an existing `memory.md`:

```python
memory_read(path="memory.md")
memory_edit(path="memory.md", old_text="", new_text="<mem>The user confirmed that status reports should include a risks section.</mem>")
memory_read(path="memory.md")
```

If the read reports `exists=false`, create that path with `memory_write(path="memory.md", content="<mem>Confirmed long-term rule</mem>")`. A full rewrite requires all entries and is allowed only when the read rules permit it. After a successful write or edit, read the same path again to verify the result and before making another change.

On `CORE_READ_REQUIRED`, call `memory_read` for the same virtual path and review the result before retrying. On a format error, fix the offending argument using a complete `<mem>body</mem>` entry before retrying.

## Category-Based Memory Tools

When the active `memory_write` schema exposes `category`, use `core` for explicit user identity, preferences, and durable behavioral rules. Use `context` for long-term project context, recurring task patterns, and verified decisions. Follow the active tool schema for the write.

## When to Search

Call `memory_search` when the current request may depend on context that is not in core memory:

```
# Before writing a weekly report
memory_search(query="weekly report")
memory_search(query="Q2")

# Before analyzing a document
memory_search(query="document format")
```

Also search for requests that may depend on saved user preferences, prior decisions, repo conventions, previous pitfalls, specific paths/errors, or recurring workflows. Skip memory for one-off simple questions, trivial rewrites, current time/date, or requests fully answered by the visible conversation.

When you search, do not pass the full user sentence if it contains action words like "send", "write", "help", "发我", or "帮我". Extract 1-3 stable noun phrases and search those first: project/product names, repo/module paths, exact errors, workflows, artifact types, or prior decisions. For example, "排产平台融资 ppt 的演讲稿发我" should search "排产平台", "融资路演", or "ppt" instead of the full sentence.

## When NOT to Save

- Ephemeral task details ("read file X", "fix this bug")
- Code patterns derivable from the codebase
- Secrets, credentials, tokens, or private keys
- Anything already in memory (check with `memory_read` first when uncertain)
- Inferences about the user that they did not explicitly confirm
