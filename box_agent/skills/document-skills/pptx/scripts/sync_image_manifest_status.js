#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const { resolveArtifactPath } = require("./deck_spec_core.js");

function usage(exitCode = 2) {
  console.error(
    "Usage: sync_image_manifest_status.js assets/generated/manifest.json",
  );
  process.exit(exitCode);
}

function resolveOutputPath(outputPath, manifestPath) {
  if (typeof outputPath !== "string" || !outputPath.trim()) return null;
  const normalized = outputPath.trim();
  const candidates = [
    resolveArtifactPath(normalized),
    path.resolve(path.dirname(manifestPath), normalized),
    path.resolve(path.dirname(path.dirname(manifestPath)), normalized),
    path.resolve(path.dirname(path.dirname(path.dirname(manifestPath))), normalized),
  ];
  return candidates.find(candidate => {
    try {
      return fs.statSync(candidate).isFile() && fs.statSync(candidate).size > 0;
    } catch (_error) {
      return false;
    }
  }) || null;
}

function main() {
  const argv = process.argv.slice(2);
  if (argv[0] === "--help" || argv[0] === "-h") usage(0);
  if (argv.length !== 1) usage();

  const manifestPath = resolveArtifactPath(argv[0]);
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const imagePlan = manifest && Array.isArray(manifest.image_plan)
    ? manifest.image_plan
    : null;
  if (!imagePlan) throw new Error("manifest.image_plan must be an array");

  let changed = 0;
  const unresolved = [];
  imagePlan.forEach(entry => {
    if (!entry || !["generate", "use_existing"].includes(entry.decision)) return;
    let entryChanged = false;
    const resolved = resolveOutputPath(entry.output_path, manifestPath);
    if (!resolved) {
      if (entry.decision === "generate") {
        unresolved.push(entry.output_path || `slide ${entry.slide || "?"}`);
      }
      return;
    }
    const desiredStatus = entry.decision === "generate" ? "generated" : "ready";
    if (entry.status !== desiredStatus) {
      entry.status = desiredStatus;
      entryChanged = true;
    }
    const desiredResolvedVia = entry.decision === "generate"
      ? "ai"
      : entry.origin === "sourced"
        ? "web"
        : "user";
    if (entry.resolved_via !== desiredResolvedVia) {
      entry.resolved_via = desiredResolvedVia;
      entryChanged = true;
    }
    if (
      entry.decision === "generate"
      && entry.acquire_via === "web"
      && entry.fallback_used !== true
    ) {
      entry.fallback_used = true;
      entryChanged = true;
    }
    if (entryChanged) changed += 1;
  });

  if (unresolved.length) {
    throw new Error(
      `Cannot mark unresolved generated image(s) ready: ${unresolved.join(", ")}`,
    );
  }
  const generatedReady = imagePlan.some(
    entry => entry && entry.decision === "generate" && entry.status === "generated"
  );
  if (
    generatedReady
    && manifest.image_service
    && manifest.image_service.status === "blocked"
  ) {
    manifest.image_service = { status: "ready" };
    changed += 1;
  }
  if (changed) {
    const temporaryPath = `${manifestPath}.tmp`;
    fs.writeFileSync(temporaryPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
    fs.renameSync(temporaryPath, manifestPath);
  }
  console.log(JSON.stringify({ ok: true, manifest: manifestPath, changed }, null, 2));
}

try {
  main();
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
