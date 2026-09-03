#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const { createHash } = require("crypto");
const { resolveArtifactPath } = require("./deck_spec_core.js");

function usage() {
  console.error(
    "Usage: validate_image_manifest.js assets/generated/manifest.json [--mode creative_image_mode] [--min-generated 1] [--deck deck.json] [--report qa/image_manifest.json]",
  );
  process.exit(2);
}

function parseArgs(argv) {
  if (argv.length < 1) usage();
  const opts = {
    manifest: argv[0],
    mode: null,
    minGenerated: 0,
    deck: null,
    report: null,
  };
  for (let i = 1; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = argv[i + 1];
    if (arg === "--mode" && value) {
      opts.mode = value;
      i += 1;
    } else if (arg === "--min-generated" && value) {
      opts.minGenerated = Number(value);
      i += 1;
    } else if (arg === "--report" && value) {
      opts.report = value;
      i += 1;
    } else if (arg === "--deck" && value) {
      opts.deck = value;
      i += 1;
    } else {
      usage();
    }
  }
  if (!Number.isInteger(opts.minGenerated) || opts.minGenerated < 0) usage();
  return opts;
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function imagePlanFromManifest(manifest) {
  if (Array.isArray(manifest)) return manifest;
  if (Array.isArray(manifest.image_plan)) return manifest.image_plan;
  return [];
}

function resolveOutputPath(outputPath, manifestPath) {
  if (typeof outputPath !== "string" || !outputPath.trim()) return false;
  const normalized = outputPath.trim();
  const candidates = [
    resolveArtifactPath(normalized),
    path.resolve(path.dirname(manifestPath), normalized),
    path.resolve(path.dirname(path.dirname(manifestPath)), normalized),
    path.resolve(path.dirname(path.dirname(path.dirname(manifestPath))), normalized),
  ];
  return candidates.find(candidate => fs.existsSync(candidate) && fs.statSync(candidate).isFile()) || null;
}

function isSuccessfulGenerate(item, manifestPath) {
  if (!item || item.decision !== "generate") return false;
  const status = typeof item.status === "string" ? item.status.toLowerCase() : "";
  if (["blocked", "failed", "error", "skipped"].includes(status)) return false;
  return Boolean(resolveOutputPath(item.output_path, manifestPath));
}

function isSuccessfulExisting(item, manifestPath) {
  if (!item || item.decision !== "use_existing") return false;
  const status = typeof item.status === "string" ? item.status.toLowerCase() : "";
  if (["blocked", "failed", "error", "skipped"].includes(status)) return false;
  return Boolean(resolveOutputPath(item.output_path, manifestPath));
}

function fileHash(filePath) {
  return createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
}

function collectDeckMedia(deck) {
  const refs = [];
  function visit(value) {
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (!value || typeof value !== "object") return;
    if (typeof value.src === "string" && value.src.trim()) {
      refs.push({ src: value.src.trim(), origin: value.origin || null });
    }
    Object.values(value).forEach(visit);
  }
  (deck.slides || []).forEach(visit);
  return refs;
}

function resolveDeckMediaPath(src, deckPath) {
  if (!src || src.startsWith("data:") || /^[a-z]+:\/\//i.test(src)) return null;
  return path.resolve(path.dirname(deckPath), src);
}

function writeReport(reportPath, payload) {
  if (!reportPath) return;
  const resolved = resolveArtifactPath(reportPath);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });
  fs.writeFileSync(resolved, JSON.stringify(payload, null, 2), "utf8");
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const manifestPath = resolveArtifactPath(opts.manifest);
  const issues = [];
  const warnings = [];

  if (!fs.existsSync(manifestPath)) {
    issues.push(`manifest not found: ${manifestPath}`);
    const payload = { ok: false, manifest: manifestPath, issues, warnings };
    writeReport(opts.report, payload);
    console.error(JSON.stringify(payload, null, 2));
    process.exit(1);
  }

  const manifest = readJson(manifestPath);
  const imagePlan = imagePlanFromManifest(manifest);
  const successfulGenerated = imagePlan.filter(item => isSuccessfulGenerate(item, manifestPath));
  const successfulExisting = imagePlan.filter(item => isSuccessfulExisting(item, manifestPath));
  const successfulSourced = successfulExisting.filter(
    item => item && (item.resolved_via === "web" || item.origin === "sourced")
  );
  const unresolvedGenerated = imagePlan.filter(
    item => item && item.decision === "generate" && !isSuccessfulGenerate(item, manifestPath)
  );
  const unresolvedRequired = imagePlan.filter(item => {
    if (!item || item.required !== true || item.decision === "generate") return false;
    return !isSuccessfulExisting(item, manifestPath);
  });
  const generationForbidden = manifest.generation_forbidden === true;
  const imageService = manifest.image_service;

  if (!imagePlan.length) {
    issues.push("manifest.image_plan is missing or empty.");
  }

  imagePlan.forEach(item => {
    if (!item || item.acquire_via === undefined) return;
    const acquireVia = item.acquire_via;
    if (!["ai", "web", "user", "none"].includes(acquireVia)) {
      issues.push(
        `slide ${item.slide || "?"}: acquire_via must be ai, web, user, or none.`
      );
      return;
    }
    if (acquireVia === "none" && item.decision !== "skip") {
      issues.push(`slide ${item.slide || "?"}: acquire_via none requires decision skip.`);
    }
    if (acquireVia === "user" && item.decision !== "use_existing") {
      issues.push(
        `slide ${item.slide || "?"}: acquire_via user requires decision use_existing.`
      );
    }
    if (acquireVia === "ai" && item.decision === "generate" && item.resolved_via === "web") {
      issues.push(`slide ${item.slide || "?"}: acquire_via ai cannot resolve via web.`);
    }
    if (acquireVia !== "web") return;
    const search = item.search;
    if (!search || typeof search !== "object" || Array.isArray(search)) {
      issues.push(`slide ${item.slide || "?"}: acquire_via web requires a search object.`);
      return;
    }
    if (search.tier !== "free") {
      issues.push(`slide ${item.slide || "?"}: initial web search tier must be free.`);
    }
    if (!Array.isArray(search.providers) || search.providers.length === 0) {
      issues.push(`slide ${item.slide || "?"}: free web search requires providers.`);
    }
    if (search.fallback !== "generate") {
      issues.push(`slide ${item.slide || "?"}: free web search fallback must be generate.`);
    }
    if (item.decision === "use_existing") {
      if (item.resolved_via !== "web" || search.status !== "sourced") {
        issues.push(
          `slide ${item.slide || "?"}: sourced web image must resolve via web with search.status sourced.`
        );
      }
      if (
        !item.source
        || typeof item.source !== "object"
        || item.source.license_tier !== "no-attribution"
        || !String(item.source.provider || "").trim()
        || !String(item.source.source_page_url || "").trim()
      ) {
        issues.push(
          `slide ${item.slide || "?"}: sourced web image requires provider, source page, and no-attribution license provenance.`
        );
      }
    } else if (item.decision === "generate") {
      if (!["exhausted", "unavailable"].includes(search.status)) {
        issues.push(
          `slide ${item.slide || "?"}: web-first generation fallback requires free search to be exhausted or unavailable.`
        );
      }
      if (item.resolved_via && item.resolved_via !== "ai") {
        issues.push(`slide ${item.slide || "?"}: web-first fallback must resolve via ai.`);
      }
    }
  });

  if (opts.mode && manifest.mode !== opts.mode) {
    issues.push(`manifest.mode must be "${opts.mode}", got ${JSON.stringify(manifest.mode)}.`);
  }
  if (!["auto", "creative_image_mode"].includes(manifest.mode)) {
    issues.push(`manifest.mode must be "auto" or "creative_image_mode", got ${JSON.stringify(manifest.mode)}.`);
  }

  if (imageService !== undefined) {
    if (!imageService || typeof imageService !== "object" || Array.isArray(imageService)) {
      issues.push("manifest.image_service must be an object when present.");
    } else if (!["ready", "blocked"].includes(imageService.status)) {
      issues.push('manifest.image_service.status must be "ready" or "blocked".');
    } else if (
      imageService.status === "blocked"
      && imageService.reason !== "authorization_401"
    ) {
      issues.push(
        'blocked manifest.image_service.reason must be "authorization_401".'
      );
    }
  }

  if (generationForbidden) {
    const forbiddenEntries = imagePlan.filter(
      item => item && (item.decision === "generate" || item.required === true)
    );
    if (forbiddenEntries.length || successfulGenerated.length) {
      issues.push(
        "manifest.generation_forbidden is true, but generated or required image " +
        "entries remain: " + forbiddenEntries.map(item => item.slide || "?").join(", ")
      );
    }
  }

  unresolvedGenerated.forEach(item => {
    issues.push(
      `generate entry is unresolved for slide ${item.slide || "?"}: ` +
      `${item.output_path || "missing output_path"}`
    );
  });
  unresolvedRequired.forEach(item => {
    issues.push(
      `required image entry is unresolved for slide ${item.slide || "?"}: ` +
      `${item.decision || "missing decision"}; generate it or bind an existing asset`
    );
  });

  const effectiveMode = opts.mode || manifest.mode;
  const minimumGenerated = effectiveMode === "creative_image_mode"
    ? Math.max(opts.minGenerated, 1)
    : opts.minGenerated;
  if (successfulGenerated.length < minimumGenerated) {
    issues.push(
      `expected at least ${minimumGenerated} successful generated image(s), found ${successfulGenerated.length}.`,
    );
  }

  if (effectiveMode === "creative_image_mode") {
    const blocked = imagePlan.filter(item => item && item.decision === "blocked");
    if (blocked.length && successfulGenerated.length === 0) {
      warnings.push("creative_image_mode has blocked image-plan entries and no successful generated assets.");
    }
  }

  const generatedAssetRecords = successfulGenerated.map(item => {
    const resolvedPath = resolveOutputPath(item.output_path, manifestPath);
    return {
      item,
      resolvedPath,
      hash: resolvedPath ? fileHash(resolvedPath) : null,
    };
  });
  const existingAssetRecords = successfulExisting.map(item => {
    const resolvedPath = resolveOutputPath(item.output_path, manifestPath);
    return {
      item,
      resolvedPath,
      hash: resolvedPath ? fileHash(resolvedPath) : null,
    };
  });
  const duplicateGroups = new Map();
  generatedAssetRecords.forEach(record => {
    if (!record.hash) return;
    const group = duplicateGroups.get(record.hash) || [];
    group.push(record);
    duplicateGroups.set(record.hash, group);
  });
  for (const records of duplicateGroups.values()) {
    if (records.length < 2) continue;
    const reuseGroups = new Set(
      records.map(record => record.item.reuse_group).filter(value => typeof value === "string" && value.trim())
    );
    if (reuseGroups.size === 1 && records.every(record => record.item.reuse_group)) continue;
    issues.push(
      "generated image content is reused across multiple image-plan entries without one explicit reuse_group: " +
      records.map(record => `${record.item.slide || "?"}:${record.item.output_path}`).join(", "),
    );
  }

  if (opts.deck) {
    const deckPath = resolveArtifactPath(opts.deck);
    if (!fs.existsSync(deckPath)) {
      issues.push(`deck not found: ${deckPath}`);
    } else {
      const deck = readJson(deckPath);
      const deckMedia = collectDeckMedia(deck).map(ref => ({
        ...ref,
        resolvedPath: resolveDeckMediaPath(ref.src, deckPath),
      }));
      const deckPaths = new Set(
        deckMedia.map(ref => ref.resolvedPath).filter(Boolean).map(filePath => path.resolve(filePath))
      );
      const manifestPaths = new Set(
        generatedAssetRecords.map(record => record.resolvedPath).filter(Boolean).map(filePath => path.resolve(filePath))
      );
      [...generatedAssetRecords, ...existingAssetRecords].forEach(record => {
        if (record.resolvedPath && !deckPaths.has(path.resolve(record.resolvedPath))) {
          issues.push(`planned image asset is not referenced by deck.json: ${record.item.output_path}`);
        }
      });
      deckMedia
        .filter(ref => ref.origin === "generated" && ref.resolvedPath)
        .forEach(ref => {
          if (!manifestPaths.has(path.resolve(ref.resolvedPath))) {
            issues.push(`deck generated media is missing from manifest.image_plan: ${ref.src}`);
          }
        });
    }
  }

  const payload = {
    ok: issues.length === 0,
    manifest: manifestPath,
    mode: manifest.mode || null,
    generationForbidden,
    imageService: imageService || null,
    imagePlanCount: imagePlan.length,
    requiredImageCount: imagePlan.filter(item => item && item.required === true).length,
    successfulGeneratedCount: successfulGenerated.length,
    successfulExistingCount: successfulExisting.length,
    successfulSourcedCount: successfulSourced.length,
    successfulGenerated: successfulGenerated.map(item => ({
      slide: item.slide || null,
      kind: item.kind || null,
      output_path: item.output_path || null,
    })),
    deck: opts.deck ? resolveArtifactPath(opts.deck) : null,
    issues,
    warnings,
  };
  writeReport(opts.report, payload);
  console.log(JSON.stringify(payload, null, 2));
  process.exit(payload.ok ? 0 : 1);
}

main();
