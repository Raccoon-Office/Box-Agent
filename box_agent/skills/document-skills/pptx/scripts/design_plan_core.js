"use strict";

const fs = require("fs");
const path = require("path");
const { createHash } = require("crypto");
const registry = require("../layouts/registry.js");
const composition = require("./composition_core.js");
const themeMatch = require("./theme_match.js");
const { inferDesignContract, withModelPaletteContract, mergePaletteDecision, frozenPaletteContract } = require("./design_contract_core.js");

const clone = value => JSON.parse(JSON.stringify(value));
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
function boundedReason(value) {
  const text = typeof value === "string" && value.trim() ? value.trim() : "Independent visual design";
  return text.length <= 240 ? text : `${text.slice(0, 239)}…`;
}
function hash(value) {
  const canonical = item => Array.isArray(item) ? item.map(canonical)
    : object(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, canonical(item[key])])) : item;
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
}
function visualFields(layout) {
  return Object.fromEntries(Object.entries(layout.fields).filter(([, field]) => field.type === "enum"));
}
function fieldSummary(field) {
  if (!object(field)) return field;
  const result = {};
  for (const [key, value] of Object.entries(field)) {
    if (key === "editor" || key === "role" || (key === "required" && value === true)) continue;
    result[key] = object(value) ? fieldSummary(value) : value;
  }
  return result;
}
function catalog() {
  const core = require("./deck_spec_core.js");
  const paletteHarmony = require("./palette_harmony.js");
  const harmonyRules = require("./color_harmony_rules.json");
  return {
    themes: core.listThemes().map(theme => ({
      id: theme.id, name: theme.name, description: theme.description,
      selection: theme.selection,
      palette: {
        ...Object.fromEntries(["background", "text", "primary"].map(key => [key, theme.palette[key]])),
        accent: theme.palette.accent || theme.palette.primary,
        secondary: theme.palette.secondary || theme.palette.chart?.[1] || theme.palette.primary,
        chart: theme.palette.chart,
      },
      style: theme.style, typography: theme.typography, visual_traits: themeMatch.traits(theme),
      presets: composition.allowedFamiliesForTheme(theme).flatMap(family => (
        composition.COMPOSITION_FAMILIES[family].map(variant => ({
          id: `${theme.id}@${variant}`,
        }))
      )),
    })),
    layouts: registry.layouts.map(layout => ({
      id: layout.id, label: layout.label, roles: layout.roles, density: layout.density,
      content_shape: layout.contentShape, fields: fieldSummary(layout.fields),
      visual_options: Object.fromEntries(Object.entries(visualFields(layout)).map(([key, field]) => [key, field.values])),
      media_slots: { min: layout.mediaSlots.min, max: layout.mediaSlots.max,
        slots: layout.mediaSlots.slots,
        background: { supported: layout.mediaSlots.background?.supported, required: layout.mediaSlots.background?.required } },
    })),
    palettes: paletteHarmony.listPremiumPalettes(),
    harmony_rules: harmonyRules,
  };
}
function resolveThemePreset(id) {
  if (typeof id !== "string") return null;
  const [themeId, variant, ...extra] = id.split("@");
  const theme = require("./deck_spec_core.js").getTheme(themeId);
  if (!theme || extra.length || (id.includes("@") && !variant)) return null;
  try {
    const family = variant ? composition.allowedFamiliesForTheme(theme).find(
      candidate => composition.COMPOSITION_FAMILIES[candidate].includes(variant)
    ) : composition.familyForTheme(theme);
    if (!family) return null;
    return { theme, design: composition.createDeckDesign(theme, variant || null, family) };
  } catch (_error) { return null; }
}
function makeInput(outline, title, sourceText = "") {
  const directory = catalog();
  const context = { title, outline, source_text: sourceText };
  const layoutHintsOnly = Boolean(sourceText);
  const hardSlides = outline.slides.map((slide, index) => {
    const requirement = slide.hard_requirements || "";
    if (requirement && (typeof requirement !== "string"
      || (sourceText && !sourceText.replace(/\s/g, "").includes(requirement.replace(/\s/g, ""))))) {
      throw new Error(`outline.slides.${index}.hard_requirements: must quote the actual user request verbatim`);
    }
    return { visual: requirement };
  });
  const constraints = layoutHintsOnly
    ? inferDesignContract({ source_text: sourceText }, hardSlides) || null
    : inferDesignContract(context, outline.slides) || null;
  const contentOutline = layoutHintsOnly ? {
    ...outline, design_requirements: undefined,
    slides: outline.slides.map(({ layout, visual, ...content }) => content),
  } : outline;
  const catalogHash = hash({ catalog: directory,
    themes: require("./deck_spec_core.js").listThemes(),
    registry: fs.readFileSync(require.resolve("../layouts/registry.js"), "utf8"),
  });
  return {
    schema_version: 1,
    protocol_version: 2,
    palette_contract_version: 2,
    visual_contract_version: 1,
    input_hash: hash({ protocol_version: 2, brief_version: 8, outline: contentOutline, source_text: sourceText, title, constraints, catalog_hash: catalogHash }),
    catalog_hash: catalogHash,
    title, outline: clone(outline), user_constraints: constraints,
    source_text: sourceText, layout_hints_only: layoutHintsOnly,
    catalog: directory,
  };
}
function defaultBindings(layout) {
  const fields = layout.fields;
  const titleField = ["title", "eyebrow", "statement", "body"].find(key => fields[key]?.type !== "enum" && fields[key]);
  const bindings = titleField ? { [titleField]: ["title"] } : {};
  const messageField = ["insight", "subtitle", "statement", "body"].find(key => fields[key]);
  const collection = ["items", "steps", "actions", "sections", "proofs", "metrics", "rows", "nodes", "tags", "layers", "systems", "stages", "lanes", "levels", "causes", "stations", "zones", "series"].find(key => fields[key]);
  const panels = Object.keys(fields).filter(key => fields[key].type === "object");
  if (!messageField && !collection && panels.length) {
    panels.forEach(key => { bindings[key] = ["message", "bullets"]; });
    return bindings;
  }
  for (const [target, source] of [[messageField || collection || titleField, "message"],
    [collection || messageField || titleField, "bullets"]]) {
    if (target) bindings[target] = [...(bindings[target] || []), source];
  }
  if (fields.categories && fields.series) bindings.categories = ["bullets"];
  return bindings;
}
function canonicalPlan(decision, input) {
  if (!object(decision)) throw new Error("designer response: expected an object");
  const issues = [];
  // Metadata supplied accidentally by a model never replaces program bindings.
  unknownFields(decision, ["theme_id", "profile_id", "visual_profile", "palette", "visual_requirements", "slides", "reason", "schema_version", "input_hash", "catalog_hash"], "designer", issues);
  const plan = { schema_version: 1, layout_hints_only: input.layout_hints_only, input_hash: input.input_hash, catalog_hash: input.catalog_hash,
    theme_id: decision.theme_id,
    ...(decision.profile_id ? { profile_id: decision.profile_id } : {}),
    ...(decision.visual_profile ? { visual_profile: normalizeVisualProfile(decision.visual_profile) } : {}),
    ...(decision.visual_requirements ? { visual_requirements: clone(decision.visual_requirements) } : {}),
    ...(input.palette_contract_version === 2
      ? { palette: mergePaletteDecision(decision.palette, input.user_constraints?.palette) }
      : decision.palette && input.user_constraints?.palette?.source !== "explicit" ? { palette: decision.palette } : {}),
    reason: boundedReason(decision.reason),
    slides: Array.isArray(decision.slides) ? decision.slides.map((slide, index) => {
      if (!object(slide)) return slide;
      unknownFields(slide, ["layout_id", "visual_options", "page"], `designer.slides.${index}`, issues);
      const layout = registry.getLayout(slide.layout_id);
      return { page: index + 1, layout_id: slide.layout_id, visual_options: slide.visual_options || {},
        content_bindings: layout ? defaultBindings(layout) : {} };
    }) : [],
  };
  if (input.visual_contract_version === 1) {
    if (!resolveThemePreset(decision.theme_id)) throw new Error("design_plan.theme_id: choose an exact registered theme or preset id");
    const requested = input.outline.design_requirements?.theme_id;
    const locked = !input.source_text || (requested && input.source_text.includes(requested)) ? requested : null;
    plan.theme_match = themeMatch.select(require("./deck_spec_core.js").listThemes(), plan.theme_id, plan.visual_requirements, locked);
    plan.theme_id = plan.theme_match.theme_id;
  }
  if (plan.profile_id && !(input.catalog.brand_profiles || []).some(profile => profile.id === plan.profile_id)) {
    issues.push(`design_plan.profile_id: unknown profile ${plan.profile_id}`);
  }
  validateVisualProfile(plan.visual_profile, issues);
  if (issues.length) throw new Error(issues.join("\n"));
  const result = validatePlan(plan, input);
  if (!result.ok) throw new Error(result.issues.join("\n"));
  return plan;
}
function verifyResponseReceipt(plan, input, root) {
  if (input.protocol_version !== 2) throw new Error("design_input: run prepare for the current independent-response protocol");
  const receiptPath = path.join(path.dirname(input.request_file), "accepted.json");
  let receipt;
  try { receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8")); }
  catch (_error) { throw new Error("design_plan: no accepted independent response; run design_plan.js accept, do not write a plan manually"); }
  const source = require("./design_response_source.js");
  const response = source.readResponse(source.sessionFile(receipt.session_id), input, root);
  const originals = receipt.sources?.map(item => {
    const original = source.readResponse(source.sessionFile(item.session_id), input, root, false);
    if (!original || original.response_hash !== item.response_hash) throw new Error("design_plan: independent child response changed");
    return original;
  });
  const decision = originals ? require("./design_recovery.js").decisionFromResponses(originals, input, module.exports).decision
    : source.parseDecision(response?.text || "");
  if (!response || response.response_hash !== receipt.response_hash || hash(plan) !== receipt.plan_hash
    || hash(canonicalPlan(decision, input)) !== hash(plan)) {
    throw new Error("design_plan: differs from the independent child response; main-agent replacement is not accepted");
  }
}
function unknownFields(value, allowed, prefix, issues) {
  Object.keys(value).filter(key => !allowed.includes(key)).forEach(key => {
    issues.push(`${prefix}.${key}: unknown field`);
  });
}
function validateVisualProfile(profile, issues) {
  if (profile === undefined) return;
  if (!object(profile)) {
    issues.push("design_plan.visual_profile: expected object");
    return;
  }
  const allowed = ["id", "semantic_tags", "preferred_themes", "direction", "palette_preset", "color_roles", "motifs", "geometry", "typography", "composition", "asset_style", "decoration_density", "hard_constraints", "provenance", "modules"];
  unknownFields(profile, allowed, "design_plan.visual_profile", issues);
  if (profile.id !== undefined && (typeof profile.id !== "string" || !profile.id.trim())) issues.push("design_plan.visual_profile.id: expected non-empty string");
  for (const key of ["semantic_tags", "preferred_themes", "motifs", "geometry", "composition", "hard_constraints", "modules"]) {
    if (profile[key] !== undefined && (!Array.isArray(profile[key]) || profile[key].some(value => typeof value !== "string"))) {
      issues.push(`design_plan.visual_profile.${key}: expected an array of strings`);
    }
  }
  for (const key of ["direction", "color_roles", "typography"]) {
    if (profile[key] !== undefined && !object(profile[key]) && typeof profile[key] !== "string") issues.push(`design_plan.visual_profile.${key}: expected object or string`);
  }
}
function normalizeVisualProfile(profile) {
  if (!object(profile)) return profile;
  const normalized = clone(profile);
  for (const key of ["semantic_tags", "preferred_themes", "motifs", "geometry", "composition", "hard_constraints", "modules"]) {
    if (typeof normalized[key] === "string") normalized[key] = [normalized[key]];
  }
  return normalized;
}
function validatePlan(plan, input) {
  const issues = [];
  if (!object(plan)) return { ok: false, issues: ["design_plan: expected object"] };
  unknownFields(plan, ["schema_version", "input_hash", "catalog_hash", "theme_id", "profile_id", "visual_profile", "palette", "slides", "reason", "layout_hints_only", "visual_requirements", "theme_match"], "design_plan", issues);
  if (plan.profile_id && !(input.catalog.brand_profiles || []).some(profile => profile.id === plan.profile_id)) {
    issues.push(`design_plan.profile_id: unknown profile ${plan.profile_id}`);
  }
  validateVisualProfile(plan.visual_profile, issues);
  if (plan.schema_version !== 1) issues.push("design_plan.schema_version: expected 1");
  if (plan.input_hash !== input.input_hash) issues.push("design_plan.input_hash: content or constraints changed; use the current design input");
  if (plan.catalog_hash !== input.catalog_hash) issues.push("design_plan.catalog_hash: design catalog changed");
  if (typeof plan.reason !== "string" || !plan.reason.trim() || plan.reason.length > 240) {
    issues.push("design_plan.reason: provide 1-240 characters");
  }
  const preset = resolveThemePreset(plan.theme_id);
  if (!preset) issues.push("design_plan.theme_id: choose an exact registered theme or preset id");
  const requestedTheme = input.outline.design_requirements?.theme_id;
  const lockedTheme = !input.source_text || (typeof requestedTheme === "string" && input.source_text.includes(requestedTheme)) ? requestedTheme : null;
  if (lockedTheme && plan.theme_id !== lockedTheme) {
    issues.push("design_plan.theme_id: conflicts with the user's selected theme");
  }
  if (input.visual_contract_version === 1) {
    try {
      const match = themeMatch.select(require("./deck_spec_core.js").listThemes(),
        plan.theme_match?.requested_theme || plan.theme_id, plan.visual_requirements, lockedTheme);
      if (match.theme_id !== plan.theme_id || hash(match) !== hash(plan.theme_match)) issues.push("design_plan.theme_match: differs from program-selected match");
    } catch (error) { issues.push(error.message); }
  }
  let designContract = clone(input.user_constraints);
  if (input.palette_contract_version === 2) {
    if (object(plan.palette)) unknownFields(plan.palette, ["background", "text", "primary", "accent", "secondary", "heading", "accent_usage"], "design_plan.palette", issues);
    const paletteIssues = [];
    designContract = frozenPaletteContract(designContract, plan.palette, plan.reason, paletteIssues);
    issues.push(...paletteIssues.map(issue => `design_plan.${issue}`));
  } else if (plan.palette !== undefined) {
    if (!object(plan.palette)) issues.push("design_plan.palette: expected object");
    else {
      unknownFields(plan.palette, ["background", "text", "primary", "accent", "secondary", "accent_usage"], "design_plan.palette", issues);
      const paletteIssues = [];
      // Validate the proposal even when an explicit user palette takes precedence.
      withModelPaletteContract(null, plan.palette, plan.reason, paletteIssues);
      issues.push(...paletteIssues.map(issue => `design_plan.palette: ${issue}`));
      const explicit = input.user_constraints?.palette;
      if (explicit?.source === "explicit") {
        for (const role of ["background", "text", "primary", "accent", "secondary"]) {
          if (explicit[role]?.value && plan.palette[role]?.toUpperCase() !== explicit[role].value.toUpperCase()) {
            issues.push(`design_plan.palette.${role}: conflicts with explicit user color ${explicit[role].value}`);
          }
        }
        if (explicit.accent_usage && (plan.palette.accent_usage || "balanced") !== explicit.accent_usage) {
          issues.push("design_plan.palette.accent_usage: conflicts with explicit user accent usage");
        }
      }
      if (!paletteIssues.length) designContract = withModelPaletteContract(designContract, plan.palette, plan.reason, []);
    }
  }
  if (!Array.isArray(plan.slides) || plan.slides.length !== input.outline.slides.length) {
    issues.push("design_plan.slides: preserve the validated outline page count");
  }
  (Array.isArray(plan.slides) ? plan.slides : []).forEach((slide, index) => {
    const prefix = `design_plan.slides.${index}`;
    if (!object(slide)) { issues.push(`${prefix}: expected object`); return; }
    unknownFields(slide, ["page", "layout_id", "visual_options", "content_bindings"], prefix, issues);
    if (slide.page !== index + 1) issues.push(`${prefix}.page: expected ${index + 1}`);
    const layout = registry.getLayout(slide.layout_id);
    if (!layout) { issues.push(`${prefix}.layout_id: unknown registered layout`); return; }
    if (!object(slide.visual_options)) issues.push(`${prefix}.visual_options: expected object, use {} for defaults`);
    else for (const [key, value] of Object.entries(slide.visual_options)) {
      const field = visualFields(layout)[key];
      if (!field || !field.values.includes(value)) {
        issues.push(`${prefix}.visual_options.${key}: choose a declared visual enum of ${layout.id}`);
      }
    }
    if (!object(slide.content_bindings) || !Object.keys(slide.content_bindings).length) {
      issues.push(`${prefix}.content_bindings: map content fields to outline title/message/bullets/evidence`);
    } else for (const [key, refs] of Object.entries(slide.content_bindings)) {
      if (!layout.fields[key] || visualFields(layout)[key]) issues.push(`${prefix}.content_bindings.${key}: expected a layout content field`);
      if (!Array.isArray(refs) || !refs.length) { issues.push(`${prefix}.content_bindings.${key}: expected nonempty reference array`); continue; }
      for (const ref of refs) {
        const match = typeof ref === "string" && /^(title|message|bullets|evidence)(?:\.(\d+))?$/.exec(ref);
        const source = input.outline.slides[index];
        if (!match || !source || source[match[1]] === undefined || (match[2] !== undefined
          && (!Array.isArray(source[match[1]]) || source[match[1]][Number(match[2])] === undefined))) {
          issues.push(`${prefix}.content_bindings.${key}: invalid outline reference ${JSON.stringify(ref)}`);
        }
      }
    }
    if (object(slide.content_bindings)) {
      const refs = new Set(Object.values(slide.content_bindings).filter(Array.isArray).flat());
      if (layout.fields.title && (!Array.isArray(slide.content_bindings.title)
        || !slide.content_bindings.title.includes("title"))) {
        issues.push(`${prefix}.content_bindings.title: bind the layout title to the outline title; use another field for the message`);
      }
      const missing = ["title", "message"].filter(ref => !refs.has(ref));
      if (!refs.has("bullets")) {
        (input.outline.slides[index]?.bullets || []).forEach((_, item) => {
          if (!refs.has(`bullets.${item}`)) missing.push(`bullets.${item}`);
        });
      }
      if (missing.length) issues.push(`${prefix}.content_bindings: missing source content ${missing.join(", ")}`);
    }
  });
  return { ok: issues.length === 0, issues, preset, design_contract: designContract };
}
function readInput(inputPath) {
  const core = require("./deck_spec_core.js");
  const inputFile = core.resolveArtifactPath(inputPath);
  const input = JSON.parse(fs.readFileSync(inputFile, "utf8"));
  const outlineFile = path.resolve(path.dirname(inputFile), input.outline_file || "outline.json");
  const outline = JSON.parse(fs.readFileSync(outlineFile, "utf8"));
  const source = core.runtimeSourceBinding();
  const fresh = makeInput(outline, input.title, source.available ? source.source_text : input.source_text);
  if (fresh.input_hash !== input.input_hash || fresh.catalog_hash !== input.catalog_hash) {
    throw new Error("design_input: stale outline, constraints or catalog; run design_plan.js prepare again");
  }
  const root = path.dirname(outlineFile);
  const requestFile = path.join(root, "qa", "design", input.input_hash, "brief.json");
  if (input.protocol_version !== 2 || input.request_file !== requestFile) {
    throw new Error("design_input: run prepare for the current independent-response protocol");
  }
  return { input: { ...fresh, request_file: input.request_file,
    request_created_at: input.request_created_at }, outlineFile, root };
}
function readValidatedPlan(planPath, inputPath) {
  const core = require("./deck_spec_core.js");
  const { input, outlineFile, root } = readInput(inputPath);
  const fresh = input;
  const plan = JSON.parse(fs.readFileSync(core.resolveArtifactPath(planPath), "utf8"));
  const result = validatePlan(plan, fresh);
  if (!result.ok) throw new Error(result.issues.join("\n"));
  verifyResponseReceipt(plan, input, path.dirname(outlineFile));
  return { plan, input: { ...fresh, protocol_version: input.protocol_version,
    request_file: input.request_file, request_created_at: input.request_created_at }, outline_file: outlineFile, ...result };
}
function bindingFor(plan) {
  return { schema_version: 1, input_hash: plan.input_hash, catalog_hash: plan.catalog_hash, plan_hash: hash(plan), layout_hints_only: plan.layout_hints_only === true,
    ...(plan.profile_id ? { profile_id: plan.profile_id } : {}),
    ...(plan.visual_profile ? { visual_profile: clone(plan.visual_profile) } : {}) };
}
function validateBinding(binding, issues) {
  if (binding === undefined) return null;
  if (!object(binding)) { issues.push("design_plan: expected binding object"); return null; }
  unknownFields(binding, ["schema_version", "input_hash", "catalog_hash", "plan_hash", "user_edited", "layout_hints_only", "profile_id", "visual_profile"], "design_plan", issues);
  if (binding.schema_version !== 1) issues.push("design_plan.schema_version: expected 1");
  for (const key of ["input_hash", "catalog_hash", "plan_hash"]) {
    if (!/^[a-f0-9]{64}$/.test(binding[key] || "")) issues.push(`design_plan.${key}: expected SHA-256`);
  }
  if (binding.layout_hints_only !== undefined && typeof binding.layout_hints_only !== "boolean") issues.push("design_plan.layout_hints_only: expected boolean");
  if (binding.user_edited !== undefined && typeof binding.user_edited !== "boolean") issues.push("design_plan.user_edited: expected boolean");
  if (binding.profile_id !== undefined && typeof binding.profile_id !== "string") issues.push("design_plan.profile_id: expected string");
  validateVisualProfile(binding.visual_profile, issues);
  return clone(binding);
}
function assertContentPatch(deck, slide, props) {
  if (!deck.design_plan) return;
  for (const key of Object.keys(visualFields(registry.getLayout(slide.layout_id)))) {
    if (Object.prototype.hasOwnProperty.call(props, key) && hash(props[key]) !== hash(slide.props[key] ?? null)) {
      throw new Error(`slides.${slide.id}.props.${key}: locked design field; request a design-plan revision instead of a content patch`);
    }
  }
}
module.exports = { hash, catalog, visualFields, resolveThemePreset, makeInput, validatePlan,
  readInput, readValidatedPlan, bindingFor, validateBinding, assertContentPatch, canonicalPlan, verifyResponseReceipt, defaultBindings };
