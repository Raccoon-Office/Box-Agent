#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { createHash } = require("crypto");

const { createEditorProps, getLayout } = require("../layouts/registry.js");
const {
  createDeckDesign,
  getTheme,
  mergeDefaults,
  readJson,
  resolveArtifactPath,
  validateAndNormalizeDeck,
} = require("./deck_spec_core.js");
const { outlineIntentRecord } = require("./outline_layout_contract.js");
const { validateOutlineBinding } = require("./validate_deck_spec.js");
const {
  mergeStyleOverrides,
  withModelPaletteContract,
} = require("./design_contract_core.js");

function usage(exitCode = 2) {
  console.log("Usage: apply_deck_redesign.js deck.json deck.redesign.json");
  process.exit(exitCode);
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function writeJsonAtomic(filePath, value) {
  const tempPath = `${filePath}.tmp-${process.pid}`;
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(tempPath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  fs.renameSync(tempPath, filePath);
}

function readRedesign(filePath) {
  const resolved = resolveArtifactPath(filePath);
  if (!fs.existsSync(resolved)) throw new Error(`File not found: ${resolved}`);
  let value;
  try {
    value = JSON.parse(fs.readFileSync(resolved, "utf8"));
  } catch (error) {
    throw new Error(`Invalid JSON in ${resolved}: ${error.message}`);
  }
  if (!isPlainObject(value)) throw new Error("Deck redesign must be an object");
  return { resolved, value };
}

function backfillBoundOutlineIntent(deck, deckPath, changes) {
  const outlinePath = path.join(path.dirname(deckPath), "outline.json");
  const contractPath = path.join(path.dirname(deckPath), "qa", "deck_contract.json");
  if (!fs.existsSync(outlinePath) || !fs.existsSync(contractPath)) return;
  let outline;
  let contract;
  try {
    outline = readJson(outlinePath);
    contract = readJson(contractPath);
  } catch (_error) {
    return;
  }
  if (!contract.outline_binding || !Array.isArray(outline.slides)) return;
  deck.slides.forEach((slide, index) => {
    const outlineIndex = Number.isInteger(slide.source_outline_page)
      ? slide.source_outline_page - 1
      : index;
    const outlineSlide = outline.slides[outlineIndex];
    if (!outlineSlide) return;
    const expected = outlineIntentRecord(outlineSlide);
    if (JSON.stringify(slide.outline_intent || null) === JSON.stringify(expected)) return;
    slide.outline_intent = expected;
    changes.push(`slides.${slide.id}.outline_intent: restored from bound outline page`);
  });
}

function applyTheme(deck, themeId, changes) {
  if (themeId === undefined) return false;
  if (typeof themeId !== "string" || !themeId.trim()) {
    throw new Error("theme_id redesign must be a registered non-empty theme id");
  }
  const theme = getTheme(themeId.trim());
  if (!theme) throw new Error(`Unknown redesign theme_id: ${JSON.stringify(themeId)}`);
  if (theme.id === deck.theme_id) return false;
  changes.push(`theme_id: ${deck.theme_id} -> ${theme.id}`);
  deck.theme_id = theme.id;
  return true;
}

function applyDesign(deck, designPatch, changes, resetFamily = false) {
  if (designPatch === undefined) return;
  if (!isPlainObject(designPatch)) throw new Error("design redesign must be an object");
  const unknown = Object.keys(designPatch).filter(key => !["family", "seed"].includes(key));
  if (unknown.length) {
    throw new Error(
      `Unknown design redesign field(s): ${unknown.join(", ")}; use family and/or seed`
    );
  }
  const theme = getTheme(deck.theme_id);
  if (!theme) throw new Error(`Unknown deck theme_id: ${JSON.stringify(deck.theme_id)}`);
  const family = designPatch.family
    || (!resetFamily && deck.design && deck.design.family)
    || null;
  const seed = designPatch.seed || (deck.design && deck.design.seed) || null;
  const nextDesign = createDeckDesign(theme, seed, family);
  if (JSON.stringify(nextDesign) !== JSON.stringify(deck.design || null)) {
    changes.push(
      `design: ${deck.design && deck.design.family || "default"} -> ${nextDesign.family} / ` +
      nextDesign.variant
    );
  }
  deck.design = nextDesign;
}

function applyPalette(deck, palettePatch, changes) {
  if (palettePatch === undefined) return;
  if (!isPlainObject(palettePatch)) {
    throw new Error("palette redesign must be an object");
  }
  const allowed = [
    "background",
    "text",
    "primary",
    "accent",
    "secondary",
    "accent_usage",
    "identity_basis",
  ];
  const unknown = Object.keys(palettePatch).filter(key => !allowed.includes(key));
  if (unknown.length) {
    throw new Error(`Unknown palette redesign field(s): ${unknown.join(", ")}`);
  }
  const issues = [];
  const next = withModelPaletteContract(
    deck.design_contract || null,
    palettePatch,
    palettePatch.identity_basis || "user-requested palette redesign",
    issues,
    "explicit",
  );
  if (issues.length) {
    throw new Error(`Palette redesign is invalid:\n${issues.join("\n")}`);
  }
  deck.design_contract = next;
  changes.push("design_contract.palette: updated during controlled redesign");
}

function applyStyleOverrides(deck, stylePatch, changes) {
  if (stylePatch === undefined) return;
  if (!isPlainObject(stylePatch)) {
    throw new Error("style_overrides redesign must be an object");
  }
  const existing = deck.design_contract || { version: 1 };
  const merged = mergeStyleOverrides(
    existing.style_overrides,
    stylePatch,
    { explicit: true },
  );
  deck.design_contract = { ...existing };
  if (merged) deck.design_contract.style_overrides = merged;
  else delete deck.design_contract.style_overrides;
  changes.push("design_contract.style_overrides: updated during controlled redesign");
}

function applySlideRedesign(deck, slidePatches, changes) {
  if (slidePatches === undefined) return [];
  if (!isPlainObject(slidePatches)) {
    throw new Error("deck.redesign.json slides must be an object keyed by existing slide id");
  }
  const slidesById = new Map(deck.slides.map(slide => [slide.id, slide]));
  const redesigned = [];
  Object.entries(slidePatches).forEach(([slideId, slidePatch]) => {
    const slide = slidesById.get(slideId);
    if (!slide) throw new Error(`Unknown slide id in redesign: ${slideId}`);
    if (!isPlainObject(slidePatch)) throw new Error(`slides.${slideId}: expected object`);
    const unknown = Object.keys(slidePatch)
      .filter(key => !["layout_id", "props", "background"].includes(key));
    if (unknown.length) {
      throw new Error(
        `slides.${slideId}: only layout_id/props/background may be redesigned; rejected ` +
        unknown.join(", ")
      );
    }
    const source = clone(slide);
    const targetLayoutId = slidePatch.layout_id || source.layout_id;
    if (!getLayout(targetLayoutId)) {
      throw new Error(`slides.${slideId}.layout_id: unknown layout ${targetLayoutId}`);
    }
    if (slidePatch.props !== undefined && !isPlainObject(slidePatch.props)) {
      throw new Error(`slides.${slideId}.props: expected object`);
    }

    if (targetLayoutId !== source.layout_id) {
      const drafts = isPlainObject(source.layout_drafts) ? clone(source.layout_drafts) : {};
      drafts[source.layout_id] = clone(source.props);
      const restored = isPlainObject(drafts[targetLayoutId])
        ? clone(drafts[targetLayoutId])
        : null;
      delete drafts[targetLayoutId];
      const mapped = restored || createEditorProps(targetLayoutId, source);
      if (!mapped) throw new Error(`Cannot create editor props for ${targetLayoutId}`);
      slide.layout_id = targetLayoutId;
      slide.props = slidePatch.props === undefined
        ? mapped
        : mergeDefaults(mapped, slidePatch.props);
      if (Object.keys(drafts).length) slide.layout_drafts = drafts;
      else delete slide.layout_drafts;
      changes.push(`slides.${slideId}.layout_id: ${source.layout_id} -> ${targetLayoutId}`);
    } else if (slidePatch.props !== undefined) {
      slide.props = mergeDefaults(source.props, slidePatch.props);
      changes.push(`slides.${slideId}.props: updated during redesign`);
    }

    if (slidePatch.background === null) {
      if (slide.background !== undefined) changes.push(`slides.${slideId}.background: removed`);
      delete slide.background;
    } else if (slidePatch.background !== undefined) {
      slide.background = clone(slidePatch.background);
      changes.push(`slides.${slideId}.background: replaced`);
    }
    redesigned.push(slideId);
  });
  return redesigned;
}

function updateContractReport(deckPath, deck, redesignPath, redesignedSlides) {
  const contractPath = path.join(path.dirname(deckPath), "qa", "deck_contract.json");
  if (!fs.existsSync(contractPath)) return;
  let contract;
  try {
    contract = JSON.parse(fs.readFileSync(contractPath, "utf8"));
  } catch (_error) {
    return;
  }
  if (!isPlainObject(contract)) return;
  contract.contract_version = Math.max(2, Number(contract.contract_version || 1));
  contract.theme_id = deck.theme_id;
  contract.design = clone(deck.design);
  contract.design_contract = clone(deck.design_contract || null);
  contract.design_selection = {
    source: "controlled_redesign",
    family: deck.design.family,
    variant: deck.design.variant,
  };
  contract.layout_plan = deck.slides.map(slide => slide.layout_id);
  const history = Array.isArray(contract.redesign_history) ? contract.redesign_history : [];
  history.push({
    redesign_file: redesignPath,
    slides: redesignedSlides,
    theme_id: deck.theme_id,
    design: clone(deck.design),
    design_contract: clone(deck.design_contract || null),
    layout_plan: [...contract.layout_plan],
  });
  contract.redesign_history = history.slice(-10);
  contract.contract_hash = createHash("sha256").update(JSON.stringify({
    contract_version: contract.contract_version,
    theme_id: deck.theme_id,
    design: deck.design,
    design_contract: deck.design_contract || null,
    layout_plan: contract.layout_plan,
    outline_binding: contract.outline_binding || null,
    truth_contract: deck.truth_contract || null,
  })).digest("hex");
  writeJsonAtomic(contractPath, contract);
}

function updateImageManifestDesign(deckPath, deck) {
  const manifestPath = path.join(
    path.dirname(deckPath),
    "assets",
    "generated",
    "manifest.json"
  );
  if (!fs.existsSync(manifestPath)) return;
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch (_error) {
    return;
  }
  if (!isPlainObject(manifest) || !isPlainObject(manifest.deck)) return;
  manifest.deck.theme_id = deck.theme_id;
  manifest.deck.design = clone(deck.design);
  if (deck.design_contract) manifest.deck.design_contract = clone(deck.design_contract);
  else delete manifest.deck.design_contract;
  writeJsonAtomic(manifestPath, manifest);
}

function main() {
  const argv = process.argv.slice(2);
  if (argv[0] === "--help" || argv[0] === "-h") usage(0);
  if (argv.length !== 2) usage();

  const deckPath = resolveArtifactPath(argv[0]);
  const { resolved: redesignPath, value: redesign } = readRedesign(argv[1]);
  const unknownTopLevel = Object.keys(redesign)
    .filter(key => ![
      "theme_id",
      "design",
      "palette",
      "style_overrides",
      "slides",
    ].includes(key));
  if (unknownTopLevel.length) {
    throw new Error(`Unknown deck redesign field(s): ${unknownTopLevel.join(", ")}`);
  }
  const deck = readJson(deckPath);
  const changes = [];
  backfillBoundOutlineIntent(deck, deckPath, changes);
  const themeChanged = applyTheme(deck, redesign.theme_id, changes);
  applyDesign(
    deck,
    redesign.design === undefined && themeChanged ? {} : redesign.design,
    changes,
    themeChanged,
  );
  applyPalette(deck, redesign.palette, changes);
  applyStyleOverrides(deck, redesign.style_overrides, changes);
  const redesignedSlides = applySlideRedesign(deck, redesign.slides, changes);

  const validation = validateAndNormalizeDeck(deck);
  if (!validation.ok) {
    throw new Error(`Redesigned deck is invalid:\n${validation.issues.join("\n")}`);
  }
  const outlineBinding = validateOutlineBinding(deckPath, validation.normalized);
  if (!outlineBinding.ok) {
    throw new Error(
      "Redesigned deck violates its bound outline/layout contract:\n" +
      outlineBinding.issues.join("\n")
    );
  }

  writeJsonAtomic(deckPath, validation.normalized);
  updateContractReport(deckPath, validation.normalized, redesignPath, redesignedSlides);
  updateImageManifestDesign(deckPath, validation.normalized);
  console.log(JSON.stringify({
    ok: true,
    deck: deckPath,
    redesign: redesignPath,
    redesigned_slides: redesignedSlides,
    theme_id: validation.normalized.theme_id,
    design: validation.normalized.design || null,
    layout_plan: validation.normalized.slides.map(slide => slide.layout_id),
    changes,
  }, null, 2));
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error));
    process.exit(1);
  }
}
