"use strict";

const { selectionIntentText, inferPreferences, selectionText, CLASSICAL_POETRY_RE } = require("./theme_selection_core.js");

function inferSubjectProfile(context) {
  const text = inferPreferences(selectionIntentText(context)).positive_text;
  if (!CLASSICAL_POETRY_RE.test(text)) return null;
  if (/(?:不要|不用|不使用|避免|禁用|拒绝|do\s+not|don't|without|no)\s*(?:使用|采用|use|a|an)?\s*(?:国风|水墨|传统风格|Chinese\s+ink|ink(?:[- ]landscape|[- ]painting)?)/i.test(selectionText(context))) return null;
  const sourceIntent = inferPreferences(context.source_text || "").positive_text;
  if (/(?:漫画|像素风|赛博朋克|comic|pixel[- ]art|cyberpunk)/i.test(sourceIntent)) return null;
  return {
    id: "classical-poetry",
    semantic_tags: ["中国古典诗歌", "人文", "诗意"],
    preferred_themes: ["soft-editorial", "grove", "research-notebook"],
    motifs: ["ink-landscape"],
    asset_style: "Conceptual Chinese ink landscape, moon, river and distant sail; quiet text-safe space, no embedded text.",
    provenance: "Inferred from the presentation subject, not a user-mandated style or an official identity.",
  };
}

function profileCss(profile, decorationsOff = false, textColor = "#111111") {
  if (decorationsOff || !profile?.motifs?.includes("ink-landscape")) return "";
  // A small landscape in the footer preserves the theme's fonts, palette and
  // text geometry. CSS also survives editor layout switches without DOM patches.
  const ink = /^#[0-9a-f]{6}$/i.test(textColor) ? textColor : "#111111";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="90" viewBox="0 0 1920 90"><path fill="${ink}" fill-opacity=".10" d="M0 90V76L100 56 190 72 290 25 390 65 470 46 590 78 760 61 900 83 1140 70 1300 80 1460 48 1550 68 1690 16 1770 59 1840 39 1920 72V90Z"/><path fill="${ink}" fill-opacity=".08" d="M0 90V83L180 69 350 84 580 64 800 87 1110 76 1380 85 1600 57 1750 78 1920 68V90Z"/></svg>`;
  return `body[data-deck-profile-motifs~="ink-landscape"] #deck-root > .slide::after {
    content: ""; display: block; position: absolute; left: 0; right: 0; bottom: 0; height: 90px;
    background: url("data:image/svg+xml,${encodeURIComponent(svg)}") bottom / 100% 90px no-repeat;
    pointer-events: none; z-index: 1;
  }`;
}

module.exports = { inferSubjectProfile, profileCss };
