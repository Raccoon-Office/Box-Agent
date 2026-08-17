---
name: research-to-deck-outline
description: Convert deep research, long-form source text, reports, notes, or collected evidence into a PPT/deck outline that can be progressively expanded. Use when the user wants a standalone outline, page-by-page deck structure, speaking-prompt outline, or full speaker-script outline from research material. Do not use for creating PPTX files, visual design, slide rendering, or template layout work.
keywords: [deck outline, slide outline, ppt outline, presentation outline, page-by-page, speaker script, speaking prompts, 大纲, PPT大纲, 页面结构, 逐页大纲, 逐页内容, 讲解提示, 讲解版, 演讲稿, 逐字稿]
---

# Research to Deck Outline

## Purpose

Turn source material into a slide-by-slide narrative outline. The skill's job is to decide what the deck should say, in what order, and at what speaking depth. It must not generate PPTX files, design slides, create images, or choose detailed layouts.

The output should feel like a useful content plan, not a rigid consulting schema. Prefer natural Markdown over JSON unless the user explicitly asks for machine-readable output.

## Output Depth

Choose the output depth from the user's wording:

- **简版大纲 / brief outline**: page-level outline only.
- **讲解版大纲 / guided outline**: page-level outline plus speaking prompts for each slide. Use this as the default when the user does not specify depth.
- **演讲稿版大纲 / scripted outline**: page-level outline plus a complete speaker script for each slide.

Treat phrases like "只要大纲", "页面结构", or "简版" as brief outline.
Treat phrases like "带讲解提示", "能照着讲", "讲解版", or "每页讲什么" as guided outline.
Treat phrases like "完整演讲稿", "可以直接上台讲", "逐字稿", or "演讲稿版" as scripted outline.

Keep the same slide order and slide intent across all depths. Guided and scripted outputs expand the outline; they must not quietly invent a different deck.

## Workflow

1. **Read the source material**
   - Identify the topic, important facts, recurring claims, useful examples, and any explicit citations or source labels.
   - Do not preserve the source order by default. Research order is usually not presentation order.

2. **Infer or state the communication setup**
   - Identify audience, purpose, expected outcome, and likely time or slide-count constraints.
   - If the user did not provide these, use a light assumption and state it briefly near the top, for example: "默认按面向非纯技术决策者、目标是讲清结论和建议来组织。"
   - Ask only when the missing setup makes a good outline impossible.

3. **Extract candidate messages**
   - Pull out 5-9 candidate messages from the material before writing slides.
   - A candidate message is a claim or takeaway, not a topic label.
   - Drop interesting details that do not support the main communication goal.

4. **Choose one deck thesis**
   - Write one sentence that the whole deck is trying to prove or explain.
   - Every slide should support, qualify, or make actionable this thesis.

5. **Choose a narrative path**
   - Pick the simplest path that fits the material:
     - Problem-solution: background -> pain -> cause -> solution -> benefit -> action.
     - Research report: question -> method/source basis -> findings -> interpretation -> conclusion.
     - Decision recommendation: current state -> options -> comparison -> recommendation -> risks -> next steps.
     - Trend analysis: change -> drivers -> impact -> opportunity -> recommendation.
     - Explainer: concept -> why it matters -> how it works -> examples -> implications.
   - Adapt the path; do not force every deck into a template.

6. **Build the slide sequence**
   - Derive the slide count from the independent narrative beats and how much content each beat can carry clearly. Do not stretch thin material or merge distinct conclusions to hit a preset count.
   - Treat 8-12 slides as a common outcome for a normal amount of material, not as a default, minimum, or maximum. Short focused material may need fewer slides; complex material may need more.
   - A concrete page count or range explicitly stated by the user is a hard constraint.
   - Each slide must have one main idea. If a slide contains two independent conclusions, split it.
   - Titles should be message-like when possible, not generic labels.
   - Page content should stay short enough to become a slide: usually 3-5 bullets.
   - Put nuance, transitions, and examples in speaking prompts or scripts rather than crowding the slide content.

7. **Attach source support lightly**
   - Include "可用素材 / 依据" when the source contains specific evidence, examples, data points, quotes, or citations.
   - Keep this lightweight. Do not build a formal evidence map unless asked.
   - If a claim is useful but not supported by the supplied material, mark it as "需要补充依据" rather than presenting it as sourced.
   - When the source bundle contains a generic presentation handoff, consume
     only `delivery_mode`, `verified_facts`, `gaps`, and `quality_summary`. Use
     all verified facts for `full`, only that verified subset for `partial`, and
     create a placeholder-only factual structure for `framework`. Do not depend
     on the producing research skill's validator name or internal status fields.

8. **Expand to the requested depth**
   - For brief outline, output only the deck setup and slide outline.
   - For guided outline, add a short "讲解提示" per slide.
   - For scripted outline, add "完整演讲稿" per slide in natural spoken language.

9. **Run a quiet quality pass**
   - Before finalizing, revise the outline against the quality checks below.
   - Do not output a separate checklist unless the user asks for diagnosis.

## Quality Checks

Use these checks before returning the final outline:

- The deck has one clear thesis.
- The slide order creates a natural listening path.
- Every slide has a reason to exist.
- Every slide expresses one main idea.
- There are no duplicate slides wearing different titles.
- The outline reorganizes the research for presentation, instead of summarizing paragraph by paragraph.
- Slide bullets are concise; details belong in prompts or scripts.
- Unsupported claims are marked or removed.
- The requested output depth is followed exactly.
- The output can be expanded into a speech without rereading the original research.

## Output Format

Use the user's language. For Chinese requests, write the outline in Chinese unless the source or user asks otherwise.

For brief outline:

```markdown
# PPT 大纲：{标题}

## 组织假设
{audience/purpose assumptions, only if needed}

## 整体讲述思路
这份 PPT 要讲清楚：{one-sentence thesis}

建议页数：{n} 页

## 第 1 页：{message-like title}

**这一页要讲清楚**
{one main idea}

**页面内容**
- {bullet}
- {bullet}
- {bullet}

**可用素材 / 依据**
- {source detail, example, or "需要补充依据"}
```

For guided outline, add:

```markdown
**讲解提示**
{how to verbally introduce, connect, or explain this slide}
```

For scripted outline, add:

```markdown
**完整演讲稿**
{speaker-ready script in natural spoken language}
```

## Style Rules

- Keep the structure easy to read.
- Prefer concrete page titles over abstract nouns.
- Avoid overly professional labels such as "evidence_map", "narrative_type", or "confidence" unless the user asks for a structured expert-team artifact.
- Do not include visual design instructions beyond simple content hints like "这里适合用对比表" when it materially helps the next workflow step.
- Do not create or modify files unless the user explicitly asks for saved artifacts.
