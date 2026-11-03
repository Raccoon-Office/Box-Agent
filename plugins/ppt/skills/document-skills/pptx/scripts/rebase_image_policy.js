#!/usr/bin/env node
"use strict";

const fs = require("fs");

const {
  readJson,
  resolveArtifactPath,
  validateAndNormalizeDeck,
} = require("./deck_spec_core.js");
const {
  createEditorProps,
  getLayout,
} = require("../layouts/registry.js");

function usage(message) {
  if (message) console.error(message);
  console.error(
    "Usage: rebase_image_policy.js deck.json --manifest assets/generated/manifest.json --policy forbidden|unavailable|retry"
  );
  process.exit(2);
}

function parseArgs(argv) {
  if (!argv[0] || argv[0] === "--help" || argv[0] === "-h") usage();
  const opts = { deck: argv[0], manifest: null, policy: null };
  for (let index = 1; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--manifest" && value) {
      opts.manifest = value;
      index += 1;
    } else if (arg === "--policy" && value) {
      opts.policy = value;
      index += 1;
    } else {
      usage(`Unknown argument: ${arg}`);
    }
  }
  if (!opts.manifest) usage("--manifest is required");
  if (!["forbidden", "unavailable", "retry"].includes(opts.policy)) {
    usage("--policy must be forbidden, unavailable, or retry");
  }
  return opts;
}

function serialize(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function writeAtomic(filePath, content) {
  const tempPath = `${filePath}.tmp-${process.pid}`;
  fs.writeFileSync(tempPath, content, "utf8");
  fs.renameSync(tempPath, filePath);
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function restoreUnavailablePolicy(deckPath, manifestPath, deckBefore, manifestBefore, manifest) {
  const recovery = manifest.image_unavailable_recovery;
  if (
    !recovery
    || recovery.schema_version !== 1
    || !recovery.deck
    || typeof recovery.deck !== "object"
    || !Array.isArray(recovery.image_plan)
  ) {
    throw new Error("No recoverable unavailable image policy state was found.");
  }
  const validation = validateAndNormalizeDeck(cloneJson(recovery.deck));
  if (!validation.ok) {
    throw new Error(`Recovered image deck is invalid:\n${validation.issues.join("\n")}`);
  }
  manifest.image_plan = cloneJson(recovery.image_plan);
  if (recovery.has_mode) manifest.mode = recovery.mode;
  else delete manifest.mode;
  if (recovery.has_generation_forbidden) {
    manifest.generation_forbidden = recovery.generation_forbidden;
  } else {
    delete manifest.generation_forbidden;
  }
  delete manifest.image_generation_unavailable;
  delete manifest.image_unavailable_recovery;
  delete manifest.image_service;

  const deckAfter = serialize(validation.normalized);
  const manifestAfter = serialize(manifest);
  const changed = deckAfter !== deckBefore || manifestAfter !== manifestBefore;
  if (deckAfter !== deckBefore) writeAtomic(deckPath, deckAfter);
  if (manifestAfter !== manifestBefore) writeAtomic(manifestPath, manifestAfter);
  console.log(JSON.stringify({
    ok: true,
    changed,
    policy: "retry",
    restored: true,
    deck: deckPath,
    manifest: manifestPath,
  }));
}

function deletePropPath(target, propPath) {
  const parts = String(propPath || "").split(".").filter(Boolean);
  if (!parts.length) return;
  let parent = target;
  for (const part of parts.slice(0, -1)) {
    if (!parent || typeof parent !== "object") return;
    parent = parent[part];
  }
  if (parent && typeof parent === "object") delete parent[parts[parts.length - 1]];
}

function stripLayoutMedia(slide, layout) {
  const slots = layout && layout.mediaSlots && Array.isArray(layout.mediaSlots.slots)
    ? layout.mediaSlots.slots
    : [];
  slots.forEach(slot => deletePropPath(slide.props, slot && slot.propPath));
  const backgroundPath = layout && layout.mediaSlots && layout.mediaSlots.background
    ? layout.mediaSlots.background.path
    : null;
  deletePropPath(slide, backgroundPath);
  deletePropPath(slide.props, backgroundPath);
  delete slide.background;
  if (slide.props && typeof slide.props === "object") delete slide.props.background;
}

function rebaseSlide(slide) {
  const layout = getLayout(slide.layout_id);
  if (!layout) throw new Error(`Unknown layout_id: ${slide.layout_id}`);
  const requiredSlots = layout.mediaSlots && Array.isArray(layout.mediaSlots.slots)
    ? layout.mediaSlots.slots.filter(slot => slot && slot.required === true)
    : [];
  if (!requiredSlots.length) {
    stripLayoutMedia(slide, layout);
    return false;
  }
  const fallbackId = layout.noImageFallbackLayoutId;
  const fallback = fallbackId ? getLayout(fallbackId) : null;
  if (!fallback) {
    throw new Error(
      `Layout ${layout.id} requires media and has no registered no-image fallback.`
    );
  }
  const props = createEditorProps(fallbackId, slide);
  if (!props) throw new Error(`Cannot create fallback props for ${fallbackId}`);
  slide.layout_id = fallbackId;
  slide.props = props;
  stripLayoutMedia(slide, fallback);
  return true;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const deckPath = resolveArtifactPath(opts.deck);
  const manifestPath = resolveArtifactPath(opts.manifest);
  if (!fs.existsSync(deckPath)) throw new Error(`deck not found: ${deckPath}`);
  if (!fs.existsSync(manifestPath)) throw new Error(`manifest not found: ${manifestPath}`);

  const deckBefore = fs.readFileSync(deckPath, "utf8");
  const manifestBefore = fs.readFileSync(manifestPath, "utf8");
  const deck = readJson(deckPath);
  const manifest = readJson(manifestPath);
  if (opts.policy === "retry") {
    restoreUnavailablePolicy(
      deckPath,
      manifestPath,
      deckBefore,
      manifestBefore,
      manifest
    );
    return;
  }
  const imagePlan = Array.isArray(manifest.image_plan) ? manifest.image_plan : [];
  if (opts.policy === "unavailable" && !manifest.image_unavailable_recovery) {
    manifest.image_unavailable_recovery = {
      schema_version: 1,
      deck: cloneJson(deck),
      image_plan: cloneJson(imagePlan),
      has_mode: Object.prototype.hasOwnProperty.call(manifest, "mode"),
      mode: manifest.mode,
      has_generation_forbidden: Object.prototype.hasOwnProperty.call(
        manifest,
        "generation_forbidden"
      ),
      generation_forbidden: manifest.generation_forbidden,
    };
  }
  const replacedSlides = [];

  (deck.slides || []).forEach(slide => {
    if (rebaseSlide(slide)) replacedSlides.push(slide.id);
  });
  if (deck.design_contract && deck.design_contract.slides) {
    replacedSlides.forEach(slideId => delete deck.design_contract.slides[slideId]);
  }

  manifest.mode = "auto";
  manifest.generation_forbidden = opts.policy === "forbidden";
  if (opts.policy === "unavailable") {
    manifest.image_generation_unavailable = true;
  } else {
    delete manifest.image_generation_unavailable;
    delete manifest.image_unavailable_recovery;
  }
  const slidesById = new Map((deck.slides || []).map(slide => [slide.id, slide]));
  const decisionReason = opts.policy === "unavailable"
    ? "image generation service unavailable"
    : "the user explicitly forbids images for this presentation";
  imagePlan.forEach(entry => {
    if (!entry || typeof entry !== "object") return;
    const slide = slidesById.get(entry.slide_id);
    if (slide) entry.layout_id = slide.layout_id;
    entry.required = false;
    entry.decision = "skip";
    entry.status = "skipped";
    entry.decision_reason = decisionReason;
    entry.prompt = "";
    entry.output_path = null;
    entry.allowed_strategies = ["skip"];
    delete entry.origin;
    delete entry.asset_hash;
    delete entry.reuse_group;
  });

  const validation = validateAndNormalizeDeck(deck);
  if (!validation.ok) {
    throw new Error(`No-image deck rebase is invalid:\n${validation.issues.join("\n")}`);
  }
  const deckAfter = serialize(validation.normalized);
  const manifestAfter = serialize(manifest);
  const changed = deckAfter !== deckBefore || manifestAfter !== manifestBefore;
  if (deckAfter !== deckBefore) writeAtomic(deckPath, deckAfter);
  if (manifestAfter !== manifestBefore) writeAtomic(manifestPath, manifestAfter);

  console.log(JSON.stringify({
    ok: true,
    changed,
    policy: opts.policy,
    replaced_slides: replacedSlides,
    deck: deckPath,
    manifest: manifestPath,
  }));
}

try {
  main();
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
