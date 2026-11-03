"use strict";

// Confirmed render defects are different from unavailable optional inspection.
function renderQuality(html, runtime) {
  const issues = [];
  for (const failure of runtime?.editor?.componentContrast?.failures || []) {
    if (failure.ratio >= 3) continue;
    issues.push({ page: failure.slide, kind: "unreadable_text",
      detail: `${failure.element}: contrast ${failure.ratio.toFixed(2)}:1 — ${failure.text}` });
  }
  for (const warning of new Set([...(html?.issues || []), ...(html?.warnings || [])])) {
    const match = /^slide-(\d+) .*(text\/content overflow detected|visible content extends outside the slide bounds)/.exec(warning);
    if (match) issues.push({ page: Number(match[1]),
      kind: match[2].startsWith("visible") ? "content_overflow" : "text_overflow", detail: warning });
  }
  return { ok: issues.length === 0, issues,
    affected_pages: [...new Set(issues.map(issue => issue.page))].sort((a, b) => a - b) };
}

function correctionNext(quality) {
  return quality.ok ? null
    : `Run design_plan.js correct design_input.json --pages ${quality.affected_pages.join(",")} with these visual defects, then accept, apply and finalize once. If correction is unavailable, already used, or still fails, report partial and retain an explicitly degraded draft. Do not report completed or visual checks passed.`;
}

module.exports = { renderQuality, correctionNext };
