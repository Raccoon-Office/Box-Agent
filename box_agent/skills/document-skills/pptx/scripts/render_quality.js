"use strict";

// Confirmed render defects are different from unavailable optional inspection.
function renderQuality(html, runtime) {
  const issues = [];
  for (const failure of runtime?.editor?.componentContrast?.failures || []) {
    if (failure.ratio >= 3) continue;
    issues.push({ page: failure.slide, kind: "unreadable_text",
      detail: `${failure.element}: contrast ${failure.ratio.toFixed(2)}:1 — ${failure.text}` });
  }
  for (const diagram of runtime?.editor?.diagrams || []) {
    for (const loss of diagram.textLosses || []) {
      issues.push({ page: diagram.page, kind: "truncated_diagram_text",
        detail: `${loss.id} ${loss.field}: rendered ${JSON.stringify(loss.rendered)} instead of ${JSON.stringify(loss.expected)}` });
    }
  }
  for (const warning of new Set([...(html?.issues || []), ...(html?.warnings || [])])) {
    const match = /^slide-(\d+) .*(text\/content overflow detected|visible content extends outside the slide bounds)/.exec(warning);
    if (match) issues.push({ page: Number(match[1]),
      kind: match[2].startsWith("visible") ? "content_overflow" : "text_overflow", detail: warning });
  }
  return { ok: issues.length === 0, issues,
    affected_pages: [...new Set(issues.map(issue => issue.page))].sort((a, b) => a - b) };
}

function correctionNext(quality, correctionUsed = false) {
  return quality.ok ? null
    : correctionUsed ? "The design correction was already used or failed for this plan. Do not retry or manually debug the layout; retain the existing draft and report the affected pages as partial."
    : `If wording alone can resolve these defects without losing meaning or conditions, apply one content-only patch and finalize again. Otherwise run design_plan.js correct design_input.json --pages ${quality.affected_pages.join(",")} with these visual defects, then accept, apply, refill affected content against the outline and finalize once. If correction is unavailable, already used, or still fails, report partial and retain an explicitly degraded draft. Do not report completed or visual checks passed.`;
}

module.exports = { renderQuality, correctionNext };
