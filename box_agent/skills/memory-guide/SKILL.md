---
name: memory-guide
description: Guides the agent to proactively save user information to long-term memory and search context when needed. Load this skill to understand when and how to use memory_write, memory_read, and memory_search.
---

# Memory Guide

You have access to `memory_write`, `memory_read`, and `memory_search` tools for persistent cross-session memory.

## Two Types of Memory

**Core memory** (`MEMORY.md`) — always recalled at session start:
- User identity: name, role, team, company
- Explicit preferences: language, writing style, tools
- Durable behavioral rules: "don't summarize at the end"

**IMPORTANT**: Core is ONLY for what the user explicitly tells you. Never write your own summaries, inferences, or conclusions to core.

**Context memory** (`CONTEXT.md`) — searchable on demand:
- Project context: goals, deadlines, team info
- Task patterns: report formats, document templates
- Historical notes: last week's report highlights
- Key decisions or constraints that may matter in future sessions

Context writes are model-merged with existing context when an LLM is available. The model decides whether to add, replace, drop, or no-op; code applies only safe exact-match operations and falls back to append-with-dedup if planning fails.

## When to Save

### Core (`category="core"`) — ONLY when user explicitly states:

1. **Personal info** — "I'm a product manager" → save as core
2. **Preferences** — "I prefer Chinese, formal tone" → save as core
3. **Behavioral feedback** — "Don't add emoji" → save as core

### Context (`category="context"`) — save when user mentions:

4. **Project context** — "Q2 goal is launching the data dashboard" → save as context
5. **Task templates** — "Weekly report should have progress/issues/next week" → save as context
6. **Key results** — "Last week we completed the API integration" → save as context
7. **Durable constraints** — "For this project, never use dependency X" → save as context

## How to Save

```
# Core — user identity and preferences
memory_write(content="- User: Zhang San, Product Dept, Product Manager", category="core")

# Context — project info and patterns; model-merged when possible
memory_write(content="- Q2 goal: launch data dashboard by 6/30", category="context")
```

## When to Search

Call `memory_search` when you need context that isn't in core memory:

```
# Before writing a weekly report
memory_search(query="weekly report")
memory_search(query="Q2")

# Before analyzing a document
memory_search(query="document format")
```

## When NOT to Save

- Ephemeral task details ("read file X", "fix this bug")
- Code patterns derivable from the codebase
- Secrets, credentials, tokens, or private keys
- Anything already in memory (check with `memory_read` first when uncertain)
- Inferences about the user that they did not explicitly confirm
