---
name: research-synthesis
description: >
  Investigate substantial questions using permitted web, internal, or supplied sources and write clear, substantive research reports in connected prose. Supply research material for documents and presentations when requested. Use for deep research, market or company analysis, technical investigations, and multi-source synthesis; not for casual conversation, simple lookups, or ordinary code changes.
keywords: [
  deep-research, research, synthesis, summary, summarization, report, market, industry, competitor,
  policy, company, evidence, sources, cross-verification, source-backed, market-research,
  industry-analysis, competitive-research, policy-analysis, company-research, risk-assessment, technical-research,
  parallel-research, 深度总结, 总结, 研究, 行业研究, 行业分析, 市场研究, 竞品分析,
  政策分析, 公司研究, 资料综述, 报告, 证据, 来源, 交叉验证,
  并行研究, 深度调研
]
metadata:
  short-description: Deep research and clear prose reports
---

# Deep Research

When acting as a subagent, complete only the assigned research and write only the requested outputs within `write_scope`. The shared plan, report-wide summary and length requirements, and final delivery belong to the lead agent; do not create a separate plan or final report unless explicitly assigned.

## Critical: User Needs and Opening Conclusions

Before the first search or delegation, save a research plan identifying the user's information needs, priorities, and research questions. Keep it within scope and mark uncertain inferences as assumptions.

Immediately after the title, the report MUST give 3–5 concise unordered bullets answering those needs with core conclusions, implications, and essential uncertainty. Insert no heading, metadata, or introduction between the title and bullets; do not substitute topics or research activities for answers.

This required summary overrides the minimal-list preference. Develop the rest in connected paragraphs with restrained formatting.

## Scope and Planning

Honor the user's source, time, cost, language, and deliverable constraints.

Ask only for blocking information that cannot be inferred, using `request_user_input(question, missing_fields, reason)` or `request_user_decision` for finite choices; use chat if the tool is unavailable. Ask for at most three essential facts, end the turn and resume after the reply; otherwise proceed with stated assumptions without repeating answered questions or granted permissions.

Resolve one absolute `{output_dir}` from the current conversation. Use a model-created task directory when one exists; otherwise use the unchanged session cwd. Do not infer an output root from environment variables or legacy host metadata. This choice does not change the session cwd or workspace. Keep every research artifact under `{output_dir}/research/` and use absolute paths for file operations.

Choose a stable lowercase, hyphenated `{topic}` slug and save the research plan using the path specified under Delivery. Include:

- User needs, assumptions, timeframe, permitted sources, and expected deliverable.
- Research questions, search keywords, and verification methods.
- Task assignments, dependencies, available tools, and budgets with capacity reserved for verification and synthesis.

Keep findings, source links, and open questions in the plan or optional notes. Update the plan when the scope changes and reread it with relevant notes after context compression.

## Retrieval and Source Access

Use only tools exposed in the current session and follow their actual parameter schemas. Do not assume a tool, integration, or specialist skill is installed. For web research, use `web_search` and `web_extract` when available; if retrieval is unavailable, use permitted supplied evidence and disclose the remaining gap.

For file-only requests, use only supplied files and do not search the web or internal integrations. Otherwise inspect provided material first, then use relevant sources within the user's authorized scope. For current claims, check the current date/time and make search windows explicit.

Use available internal integrations in read-only mode, checking their coverage first and prioritizing them for organization-specific questions.

Use the runtime's shared **Web Access Routing** policy; this skill does not define another channel priority. When that policy selects an available browser, use its exposed tools and finish each navigation/page-read pair before navigating again. Navigation metadata alone is not a failed body read; inspect the returned page content before using it in the report.

## Delegation

When research can be divided into relatively independent directions, prefer parallel research with `sub_agent`. Handle simple queries, lightweight tasks, and work that cannot usefully be split directly. If delegation is not authorized or the tool is unavailable, conduct the research directly and parallelize tool calls where possible.

### Task Decomposition and Execution

- Start no more than 8 subagents in total, including failed attempts and retries; reserve capacity for recovery.
- Start independent tasks in parallel early; execute dependent tasks in sequence.
- Avoid duplicate research unless needed to cross-check important conclusions.
- Add tasks to address evidence gaps while respecting the task cap and runtime resource limits.

### Child Task Configuration

Provide a short `title` and a self-contained `task` covering the objective, context, scope, output, and constraints. Frame unverified leads as questions, not facts. Request core findings, specific evidence, source links, uncertainties, and open questions; assign a separate file when detailed notes are needed.

Every `task` must explicitly include this instruction:

> You are a subagent responsible for a specific research task. Complete the assigned research directly. Do not delegate recursively or create other subagents.

Prefer omitting `required_tools` to use the runtime's default subset of the parent's available tools, not full inheritance. An explicit list replaces those defaults; `[]` requests no tools. If you specify a list for web research with file output, include at least `read_file`, `write_file`, `web_search`, and `web_extract`. Keep `read_file` to access saved tool results.

This example uses default tools and assumes a session cwd of `/work/project`; adapt the question, paths, and permitted sources to the actual task:

```json
{
  "title": "Training costs",
  "task": "You are a subagent responsible for a specific research task. Complete the assigned research directly. Do not delegate recursively or create other subagents. Investigate how Looped Transformer training costs compare with standard Transformers using public research. Return findings, sources, uncertainties, and unresolved questions. Save findings only to /work/project/research/child-01/findings.md; do not create a separate plan or final report or edit shared files.",
  "skills": ["research-synthesis"],
  "write_scope": ["research/child-01/findings.md"],
  "budget": {"max_tool_calls": 48}
}
```

Set each child's `budget.max_tool_calls` to at least 48. With default tools, `write_scope` enables available scoped write tools; explicitly requesting `write_file` also requires `write_scope`. Use session-cwd-relative paths and disjoint scopes for parallel children. For file-only or response-only tasks, explicitly select only the needed tools and omit `write_scope` when no file-write tools are requested.

### Result Synthesis

The lead agent owns overall coordination, verification of key evidence, conflict resolution, gap filling, synthesis, and final delivery. Avoid unnecessarily repeating research already completed by subagents.

Present corrected facts without turning your own mistaken leads into user misconceptions. Ask the user only about gaps that block their original request.

## Stopping Conditions

For HTTP 403/429, CAPTCHA, permission denials, timeouts, or unreadable content, allow one materially different permitted recovery, then stop that path if the same access barrier persists. Tell the user what is blocked and unsupported; do not bypass restrictions or retry equivalent variants. Use independently accessible sources when available.

Read saved notes before repeating external requests and reserve time for synthesis. Stop when the questions have enough support or further research is unlikely to change the answer; near budget limits, synthesize available findings and state remaining gaps. A budget limit does not prove completion, and a missing result does not prove that no public evidence exists.

## Writing and Delivery

### Priorities

Follow **Critical: User Needs and Opening Conclusions** as the highest writing priority. Fully address the user's research needs, beginning with a descriptive title and 3–5 concise, evidence-based conclusions.

Writing style, structure, and length must serve the user's needs rather than constrain the content.

### Writing Style

Write in the user's language using connected paragraphs and restrained formatting. Organize comparisons and explanations around research questions, not paper-by-paper lists. Use headings and lists only when requested or needed for clarity.

Develop and substantiate the opening conclusions with evidence and analysis. Check important numbers, dates, names, and differing accounts; use concrete examples where useful. Explain patterns, implications, and uncertainties, distinguish facts from interpretation, and never invent facts, quotations, or sources.

Avoid unnecessary background, repetition, excessive formatting, formulaic closing summaries, and emojis.

### Tables and Visualizations

Proactively use Markdown tables for comparisons and structured data, Mermaid diagrams for architectures, workflows, interactions, and decisions, and charts for quantitative trends supported by reliable data.

Plan visuals during research planning and check for missed opportunities before delivery. Place them in relevant sections with cited evidence. Paragraph-first writing does not limit tables or figures. Never fabricate data or add decorative visuals to meet a quota.

### Length

Unless otherwise requested, use the following approximate ranges:

| Question shape | English words / Chinese characters |
| --- | --- |
| Straightforward | 500–1,000 |
| Depth-first | 1,500–3,000 |
| Breadth-first | 2,000–4,000 |

Adjust length to the user's needs and research complexity. Do not pad the report or omit important content to meet these ranges. References and working notes are excluded.

### Sources

Support important findings with readable inline citations or links. End with a numbered list of useful sources actually used, using clickable Markdown links `[title](url)` for web sources and file names with relevant pages or sections for supplied files.

### Delivery

Write files with `write_file(path, content)`, following its ordered-chunk schema for large files:

- Research plan: `{output_dir}/research/research_plan_{topic}.md`
- Final report: `{output_dir}/research/{topic}_final.md`

Only the plan and final report are required; other notes are optional. Do not create evidence ledgers or run legacy validation scripts.

Register the report with `publish_artifact(path=...)` when available, then return its path and a brief summary. For another requested format, pass the report, sources, and relevant notes to the available writing skill and follow its input contract; do not claim an unperformed validation.
