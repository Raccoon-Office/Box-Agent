#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { createHash } = require("crypto");

const { resolveArtifactPath } = require("./deck_spec_core.js");

function usage(exitCode = 2) {
  console.log(
    "Usage: design_review_receipt.js deck.json record --verdict accepted|revised|unavailable " +
    "--reason REASON | validate --report qa/design_review_check.json"
  );
  process.exit(exitCode);
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function reviewedSurface(deck) {
  return {
    theme_id: deck.theme_id || null,
    design: deck.design || null,
    palette: deck.design_contract && deck.design_contract.palette || null,
    style_overrides: deck.design_contract
      && deck.design_contract.style_overrides || null,
    slides: (Array.isArray(deck.slides) ? deck.slides : []).map(slide => ({
      id: slide.id,
      layout_id: slide.layout_id,
      chart_style: slide.props && slide.props.chart_style || null,
    })),
  };
}

function surfaceHash(deck) {
  return createHash("sha256")
    .update(JSON.stringify(reviewedSurface(deck)))
    .digest("hex");
}

function parseOptions(argv) {
  const opts = { verdict: null, reason: null, report: null };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--verdict" && value) {
      opts.verdict = value;
      index += 1;
    } else if (arg === "--reason" && value) {
      opts.reason = value;
      index += 1;
    } else if (arg === "--report" && value) {
      opts.report = value;
      index += 1;
    } else {
      usage();
    }
  }
  return opts;
}

function main() {
  const [deckArg, action, ...rest] = process.argv.slice(2);
  if (!deckArg || !["record", "validate"].includes(action)) usage();
  const deckPath = resolveArtifactPath(deckArg);
  const deck = readJson(deckPath);
  const artifactRoot = path.dirname(deckPath);
  const receiptPath = path.join(artifactRoot, "qa", "design_review.json");
  const opts = parseOptions(rest);

  if (action === "record") {
    if (!["accepted", "revised", "unavailable"].includes(opts.verdict)) usage();
    if (!String(opts.reason || "").trim() || String(opts.reason).length > 240) usage();
    const receipt = {
      schema_version: 1,
      verdict: opts.verdict,
      reason: String(opts.reason).trim(),
      count: 1,
      surface_hash: surfaceHash(deck),
      reviewed_surface: reviewedSurface(deck),
    };
    writeJson(receiptPath, receipt);
    console.log(JSON.stringify({ ok: true, receipt: receiptPath, ...receipt }, null, 2));
    return;
  }

  const reportPath = opts.report
    ? resolveArtifactPath(opts.report)
    : path.join(artifactRoot, "qa", "design_review_check.json");
  const warnings = [];
  const reviewRequired = Boolean(
    deck.design_contract
    && deck.design_contract.palette
    && deck.design_contract.palette.source === "inferred"
  );
  let receipt = null;
  try {
    receipt = readJson(receiptPath);
  } catch (_error) {
    if (!reviewRequired) {
      const report = {
        ok: true,
        issues: [],
        warnings: [],
        required: false,
        receipt: receiptPath,
        verdict: null,
        count: 0,
        surface_hash: surfaceHash(deck),
      };
      writeJson(reportPath, report);
      console.log(JSON.stringify(report, null, 2));
      return;
    }
    warnings.push("exactly-one semantic design review receipt is missing");
  }
  if (receipt) {
    if (receipt.count !== 1) warnings.push("design review receipt count must be exactly 1");
    if (!["accepted", "revised", "unavailable"].includes(receipt.verdict)) {
      warnings.push("design review receipt verdict is invalid");
    }
    if (receipt.surface_hash !== surfaceHash(deck)) {
      warnings.push("design changed after semantic review; receipt no longer covers the final deck");
    }
    if (receipt.verdict === "unavailable") {
      warnings.push("semantic design review was unavailable; final design is unreviewed");
    }
  }
  const report = {
    ok: true,
    issues: [],
    warnings,
    required: reviewRequired,
    receipt: receiptPath,
    verdict: receipt && receipt.verdict || null,
    count: receipt && receipt.count || 0,
    surface_hash: surfaceHash(deck),
  };
  writeJson(reportPath, report);
  console.log(JSON.stringify(report, null, 2));
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error));
    process.exit(1);
  }
}

module.exports = { reviewedSurface, surfaceHash };
