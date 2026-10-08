"use strict";

// Confirmed render defects are different from unavailable optional inspection.
function renderQuality(html, runtime) {
  const issues = [];
  for (const failure of runtime?.editor?.componentContrast?.failures || []) {
    if (failure.ratio >= 3) continue;
    issues.push({ page: failure.slide, kind: "unreadable_text",
      detail: `${failure.element}: contrast ${failure.ratio.toFixed(2)}:1 — ${failure.text}` });
  }
  for (const warning of html?.warnings || []) {
    const match = /^slide-(\d+) .*text\/content overflow detected/.exec(warning);
    if (match) issues.push({ page: Number(match[1]), kind: "text_overflow", detail: warning });
  }
  return { ok: issues.length === 0, issues,
    affected_pages: [...new Set(issues.map(issue => issue.page))].sort((a, b) => a - b) };
}

module.exports = { renderQuality };
