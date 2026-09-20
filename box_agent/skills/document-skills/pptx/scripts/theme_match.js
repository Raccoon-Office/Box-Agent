"use strict";

const presentation = require("../runtime/presentation-system.js");
const FALLBACK = "plain-neutral";
const OPTIONS = {
  canvas: ["any", ...presentation.STYLE_VALUES.canvas],
  heading: ["any", ...presentation.STYLE_VALUES.heading],
  display_font: ["any", "sans-serif", "serif", "monospace", "cursive"],
  body_font: ["any", "sans-serif", "serif", "monospace", "cursive"],
  shadow: ["any", ...presentation.STYLE_VALUES.shadow],
};
function fontClass(stack) {
  return String(stack || "").split(",").at(-1).trim().replace(/["']/g, "") || "unknown";
}
function traits(theme) {
  const { style } = presentation.resolveTheme(theme);
  return { canvas: style.canvas, heading: style.heading, shadow: style.shadow,
    display_font: fontClass(theme.typography?.display), body_font: fontClass(theme.typography?.body) };
}
function conflicts(theme, requirements) {
  const actual = traits(theme);
  return Object.keys(OPTIONS).filter(key => requirements[key] !== "any" && requirements[key] !== actual[key]);
}
function select(themes, requested, requirements, lockedTheme = null) {
  if (!requirements || typeof requirements !== "object" || Array.isArray(requirements)) {
    throw new Error("design_plan.visual_requirements: expected structured visual requirements");
  }
  for (const key of Object.keys(requirements)) {
    if (!(key in OPTIONS) && key !== "allow_plain_fallback") throw new Error(`design_plan.visual_requirements.${key}: unknown field`);
  }
  for (const [key, values] of Object.entries(OPTIONS)) {
    if (!values.includes(requirements[key])) throw new Error(`design_plan.visual_requirements.${key}: expected ${values.join(" | ")}`);
  }
  if (typeof requirements.allow_plain_fallback !== "boolean") throw new Error("design_plan.visual_requirements.allow_plain_fallback: expected boolean; false for user-required visual features");
  const theme = themes.find(item => item.id === String(requested).split("@")[0]);
  if (!theme) throw new Error("design_plan.theme_id: choose an exact registered theme or preset id");
  const mismatches = conflicts(theme, requirements);
  if (!mismatches.length) return { theme_id: requested, status: "matched", requested_theme: requested, conflicts: [] };
  const candidates = themes.filter(item => !conflicts(item, requirements).length);
  if (candidates.length) throw new Error(`design_plan.theme_id: conflicts with visual_requirements.${mismatches.join(", ")}; matching themes: ${candidates.map(item => item.id).join(", ")}. Preserve the palette and requirements; change only theme_id.`);
  if (lockedTheme || !requirements.allow_plain_fallback) throw new Error(`design_plan.theme_id: no theme matches required visual features (${mismatches.join(", ")}); cannot silently relax user constraints`);
  if (!themes.some(item => item.id === FALLBACK)) throw new Error("design_plan.theme_id: plain fallback is unavailable");
  return { theme_id: FALLBACK, status: "plain_fallback", requested_theme: requested,
    conflicts: conflicts(themes.find(item => item.id === FALLBACK), requirements) };
}
module.exports = { OPTIONS, FALLBACK, traits, conflicts, select };
