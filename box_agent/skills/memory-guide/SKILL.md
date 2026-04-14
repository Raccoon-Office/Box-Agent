---
name: memory-guide
description: Guides the agent to proactively save user information to long-term memory using the memory_write tool. Load this skill to understand when and how to record user preferences, personal info, and project context.
---

# Memory Guide

You have access to `memory_write` and `memory_read` tools for persistent cross-session memory. Use them proactively.

## When to Save (call `memory_write`)

**Immediately save** when the user shares:

1. **Personal info** — name, role, team, company, expertise
   - "I'm a frontend engineer" -> save
   - "My name is Zhang San" -> save

2. **Preferences** — language, style, tools, workflows
   - "I prefer Chinese responses" -> save
   - "Always use uv instead of pip" -> save

3. **Behavioral feedback** — corrections, complaints, compliments
   - "Don't summarize at the end" -> save
   - "I like how you structured that" -> save

4. **Project context** — goals, constraints, deadlines, stakeholders
   - "We're launching next Friday" -> save
   - "The auth rewrite is driven by legal compliance" -> save

## When NOT to Save

- Ephemeral task details ("read file X", "fix this bug")
- Code patterns derivable from the codebase
- Git history or file paths
- Anything already in memory (check with `memory_read` first)

## How to Save

```
memory_write(content="- User prefers Chinese responses\n- User is a frontend engineer")
```

- Use markdown bullet points
- Be concise but specific
- Include context ("why") when non-obvious
- Check existing memory with `memory_read` before writing to avoid duplicates

## Memory Structure

Memory is organized into sections. `memory_write` writes to the **Manual Memory** section. There is also an **Auto Memory** section managed by the system. Both are recalled at the start of each session.
