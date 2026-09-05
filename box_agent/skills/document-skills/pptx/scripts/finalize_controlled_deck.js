#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");
const { createHash } = require("crypto");

const { resolveArtifactPath } = require("./deck_spec_core.js");
const { reconcileReadyManifestMedia } = require("./apply_deck_patch.js");

function usage() {
  console.error(
    "Usage: finalize_controlled_deck.js deck.json --out index.html " +
    "[--manifest assets/generated/manifest.json]"
  );
  process.exit(2);
}

function parseArgs(argv) {
  if (!argv[0] || argv[0] === "--help" || argv[0] === "-h") usage();
  const opts = { deck: argv[0], out: null, manifest: null };
  for (let index = 1; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--out" && value) {
      opts.out = value;
      index += 1;
    } else if (arg === "--manifest" && value) {
      opts.manifest = value;
      index += 1;
    } else {
      usage();
    }
  }
  if (!opts.out) usage();
  return opts;
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function hashJson(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function refreshDeckContractReport(deckPath, reportPath, deckSpecReport) {
  const artifactRoot = path.dirname(deckPath);
  const outlinePath = path.join(artifactRoot, "outline.json");
  let previous = {};
  try {
    const value = readJson(reportPath);
    if (value && typeof value === "object" && !Array.isArray(value)) previous = value;
  } catch (_error) {
    // A structurally valid deck can reconstruct the current contract receipt.
  }
  const deck = readJson(deckPath);
  const slides = Array.isArray(deck.slides) ? deck.slides : [];
  let outline = null;
  try {
    outline = readJson(outlinePath);
  } catch (_error) {
    outline = null;
  }
  const outlineSlides = outline && Array.isArray(outline.slides) ? outline.slides : [];
  const outlineIssues = deckSpecReport
    && deckSpecReport.outlineBinding
    && Array.isArray(deckSpecReport.outlineBinding.issues)
      ? deckSpecReport.outlineBinding.issues
      : [];
  const priorOutlineBinding = previous.outline_binding
    && typeof previous.outline_binding === "object"
    && !Array.isArray(previous.outline_binding)
      ? previous.outline_binding
      : {};
  const outlineBinding = outline
    ? {
      ...priorOutlineBinding,
      outline_file: outlinePath,
      outline_hash: hashJson(outline),
      source_mode: outline.source_mode || priorOutlineBinding.source_mode || null,
      page_count: outlineSlides.length,
      ok: outlineIssues.length === 0,
      issues: outlineIssues,
    }
    : null;
  const contractVersion = Math.max(2, Number(previous.contract_version || 1));
  const refreshed = {
    ...previous,
    ok: true,
    contract_version: contractVersion,
    deck_file: deckPath,
    slide_count: slides.length,
    layout_plan: slides.map(slide => slide.layout_id),
    deck_hash: hashJson(deck),
    outline_binding: outlineBinding,
    refreshed_by: "finalize_controlled_deck",
    issues: [],
    warnings: Array.isArray(previous.warnings) ? previous.warnings : [],
  };
  refreshed.contract_hash = hashJson({
    contract_version: contractVersion,
    theme_id: deck.theme_id || null,
    design: deck.design || null,
    layout_plan: refreshed.layout_plan,
    outline_binding: outlineBinding,
    truth_contract: deck.truth_contract || null,
  });
  writeJson(reportPath, refreshed);
  console.log(
    `${outlineIssues.length ? "FINALIZE_ADVISORY" : "FINALIZE_PASS"} `
    + `stage=deck_contract warnings=${outlineIssues.length}`
  );
  return refreshed;
}

function reportSummary(reportPath) {
  try {
    const report = readJson(reportPath);
    const issues = Array.isArray(report.issues)
      ? report.issues
      : Array.isArray(report.errors)
        ? report.errors
        : [];
    const warnings = Array.isArray(report.warnings)
      ? report.warnings
      : Number.isInteger(report.warnings)
        ? Array.from({ length: report.warnings }, () => "warning")
        : [];
    return { ok: report.ok === true, issues, warnings };
  } catch (_error) {
    return null;
  }
}

function tail(value, limit = 4000) {
  const text = String(value || "").trim();
  return text.length > limit ? text.slice(-limit) : text;
}

function fail(stage, result, reportPath = null) {
  const summary = reportPath ? reportSummary(reportPath) : null;
  console.error(`FINALIZE_STOP stage=${stage}`);
  if (summary && summary.issues.length) {
    console.error(JSON.stringify({ issues: summary.issues, warnings: summary.warnings }, null, 2));
  } else {
    const diagnostic = tail(`${result.stdout || ""}\n${result.stderr || ""}`);
    if (diagnostic) console.error(diagnostic);
  }
  process.exit(Number.isInteger(result.status) && result.status !== 0 ? result.status : 1);
}

function runStage(stage, scriptName, args, reportPath = null) {
  const result = spawnSync(
    process.execPath,
    [path.join(__dirname, scriptName), ...args],
    {
      cwd: process.cwd(),
      env: process.env,
      encoding: "utf8",
      maxBuffer: 16 * 1024 * 1024,
    }
  );
  if (result.error || result.status !== 0) fail(stage, result, reportPath);
  const summary = reportPath ? reportSummary(reportPath) : null;
  if (reportPath && (!summary || !summary.ok)) fail(stage, result, reportPath);
  console.log(
    `FINALIZE_PASS stage=${stage}` +
    (summary ? ` warnings=${summary.warnings.length}` : "")
  );
}

function runDeckSpecStage(deckPath, reportPath) {
  const result = spawnSync(
    process.execPath,
    [
      path.join(__dirname, "validate_deck_spec.js"),
      deckPath,
      "--report",
      reportPath,
    ],
    {
      cwd: process.cwd(),
      env: process.env,
      encoding: "utf8",
      maxBuffer: 16 * 1024 * 1024,
    }
  );
  let report = null;
  try {
    report = readJson(reportPath);
  } catch (_error) {
    report = null;
  }
  if (!result.error && result.status === 0 && report && report.ok === true) {
    console.log(
      `FINALIZE_PASS stage=deck_spec warnings=${Array.isArray(report.warnings) ? report.warnings.length : 0}`
    );
    return report;
  }

  const structuralIssues = report && Array.isArray(report.structuralIssues)
    ? report.structuralIssues
    : null;
  const outlineIssues = report
    && report.outlineBinding
    && Array.isArray(report.outlineBinding.issues)
      ? report.outlineBinding.issues
      : [];
  const semanticOnly = Boolean(
    !result.error
    && result.status === 1
    && report
    && structuralIssues
    && structuralIssues.length === 0
    && outlineIssues.length > 0
    && (!report.designContract || report.designContract.ok !== false)
  );
  const allowOutlineBindingDraft =
    process.env.BOX_AGENT_ALLOW_DEGRADED_OUTLINE_BINDING === "1";
  if (!semanticOnly || !allowOutlineBindingDraft) {
    fail("deck_spec", result, reportPath);
  }

  const normalized = {
    ...report,
    ok: true,
    advisory: true,
    delivery_policy: "allow_outline_binding_draft",
    degraded_reason: "outline_binding",
    issues: [],
    warnings: [
      ...new Set([
        ...(Array.isArray(report.warnings) ? report.warnings : []),
        ...outlineIssues,
      ]),
    ],
  };
  writeJson(reportPath, normalized);
  console.log(
    `FINALIZE_ADVISORY stage=deck_spec warnings=${normalized.warnings.length}`
  );
  return normalized;
}

function runAdvisoryStage(stage, scriptName, args, reportPath) {
  let previousMtime = null;
  try {
    previousMtime = fs.statSync(reportPath).mtimeMs;
  } catch (_error) {
    // The advisory may be running for the first time.
  }
  const result = spawnSync(
    process.execPath,
    [path.join(__dirname, scriptName), ...args],
    {
      cwd: process.cwd(),
      env: process.env,
      encoding: "utf8",
      maxBuffer: 16 * 1024 * 1024,
    }
  );
  let report = null;
  let reportIsFresh = false;
  try {
    report = readJson(reportPath);
    reportIsFresh = previousMtime === null
      || fs.statSync(reportPath).mtimeMs !== previousMtime;
  } catch (_error) {
    report = null;
  }
  const summary = reportIsFresh ? reportSummary(reportPath) : null;
  const diagnostic = tail(
    result.error
      ? result.error.message
      : `${result.stdout || ""}\n${result.stderr || ""}`
  );
  const warnings = [
    ...(summary ? summary.warnings : []),
    ...(summary ? summary.issues : []),
  ];
  if ((result.error || result.status !== 0 || !summary) && diagnostic) {
    warnings.push(`${stage} advisory could not complete cleanly: ${diagnostic}`);
  }
  const normalized = {
    ...(report && typeof report === "object" && !Array.isArray(report) ? report : {}),
    ok: true,
    advisory: true,
    issues: [],
    warnings: [...new Set(warnings)],
  };
  writeJson(reportPath, normalized);
  console.log(`FINALIZE_ADVISORY stage=${stage} warnings=${normalized.warnings.length}`);
  return normalized;
}

function runPostRenderStage(stage, scriptName, args, reportPath) {
  let previousMtime = null;
  try {
    previousMtime = fs.statSync(reportPath).mtimeMs;
  } catch (_error) {
    // The post-render check may be running for the first time.
  }
  const result = spawnSync(
    process.execPath,
    [path.join(__dirname, scriptName), ...args],
    {
      cwd: process.cwd(),
      env: process.env,
      encoding: "utf8",
      maxBuffer: 16 * 1024 * 1024,
    }
  );
  let report = null;
  let reportIsFresh = false;
  try {
    report = readJson(reportPath);
    reportIsFresh = previousMtime === null || fs.statSync(reportPath).mtimeMs !== previousMtime;
  } catch (_error) {
    report = null;
  }
  const summary = reportIsFresh ? reportSummary(reportPath) : null;
  if (!result.error && result.status === 0 && summary && summary.ok) {
    console.log(`FINALIZE_PASS stage=${stage} warnings=${summary.warnings.length}`);
    return report;
  }

  const diagnostic = tail(
    result.error
      ? result.error.message
      : `${result.stdout || ""}\n${result.stderr || ""}`
  );
  const warnings = [
    ...(summary ? summary.warnings : []),
    ...(summary ? summary.issues : []),
  ];
  if (!summary) {
    warnings.push(
      diagnostic || `${stage} did not produce a fresh QA report`
    );
  }
  const normalized = {
    ...(report && typeof report === "object" && !Array.isArray(report) ? report : {}),
    ok: true,
    advisory: true,
    degraded_reason: stage,
    issues: [],
    warnings: [...new Set(warnings)],
  };
  writeJson(reportPath, normalized);
  console.log(`FINALIZE_ADVISORY stage=${stage} warnings=${normalized.warnings.length}`);
  return normalized;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const deckPath = resolveArtifactPath(opts.deck);
  const outputPath = resolveArtifactPath(opts.out);
  const artifactRoot = path.dirname(deckPath);
  const manifestPath = opts.manifest
    ? resolveArtifactPath(opts.manifest)
    : path.join(artifactRoot, "assets", "generated", "manifest.json");
  const reportDir = path.join(artifactRoot, "qa");
  const reports = {
    contract: path.join(reportDir, "deck_contract.json"),
    review: path.join(reportDir, "design_review_check.json"),
    spec: path.join(reportDir, "deck_spec.json"),
    truth: path.join(reportDir, "truth_check.json"),
    image: path.join(reportDir, "image_manifest.json"),
    html: path.join(reportDir, "html_self_check.json"),
    runtime: path.join(reportDir, "runtime_probe.json"),
  };
  fs.mkdirSync(reportDir, { recursive: true });

  // Assets can finish after the content patch. Bind them in this same compile
  // invocation, before validation and rendering, without replacing chosen media.
  const mediaBindings = [];
  let deck = null;
  try {
    deck = readJson(deckPath);
  } catch (_error) {
    // The existing deck-spec stage owns invalid/missing document diagnostics.
  }
  if (deck && Array.isArray(deck.slides)) {
    reconcileReadyManifestMedia(deck, deckPath, mediaBindings, {
      manifestPath,
      onlyMissing: true,
    });
    if (mediaBindings.length) writeJson(deckPath, deck);
  }

  const deckSpecReport = runDeckSpecStage(deckPath, reports.spec);
  refreshDeckContractReport(deckPath, reports.contract, deckSpecReport);
  const designReviewReport = runAdvisoryStage(
    "design_review",
    "design_review_receipt.js",
    [deckPath, "validate", "--report", reports.review],
    reports.review,
  );
  let manifestMode = "auto";
  try {
    const manifest = readJson(manifestPath);
    if (manifest && manifest.mode === "creative_image_mode") {
      manifestMode = "creative_image_mode";
    }
  } catch (_error) {
    // The manifest validator below writes the actionable missing/invalid report.
  }
  const imageArgs = [manifestPath];
  if (manifestMode === "creative_image_mode") {
    imageArgs.push("--mode", "creative_image_mode", "--min-generated", "1");
  }
  imageArgs.push("--deck", deckPath, "--report", reports.image);
  const imageReport = runAdvisoryStage(
    "image_manifest",
    "validate_image_manifest.js",
    imageArgs,
    reports.image
  );

  runStage("render", "render_deck_html.js", [deckPath, "--out", outputPath]);
  if (!fs.existsSync(outputPath) || fs.statSync(outputPath).size === 0) {
    fail("render", { status: 1, stdout: "", stderr: `Missing output: ${outputPath}` });
  }
  const htmlReport = runPostRenderStage(
    "html_self_check",
    "html_self_check.js",
    [
      outputPath,
      "--dom-to-pptx",
      "--allow-local-images",
      "--report",
      reports.html,
    ],
    reports.html
  );
  runAdvisoryStage(
    "truth",
    "validate_deck_truth.js",
    [deckPath, "--report", reports.truth],
    reports.truth
  );
  const runtimeReport = runPostRenderStage(
    "runtime_probe",
    "probe_deck_runtime.js",
    [outputPath, "--viewport", "1440x900", "--report", reports.runtime],
    reports.runtime
  );

  const warningCount = Object.values(reports)
    .map(reportSummary)
    .filter(Boolean)
    .reduce((total, report) => total + report.warnings.length, 0);
  const degradedStages = [
    deckSpecReport.advisory === true ? "deck_spec" : null,
    imageReport.warnings.length > 0 ? "image_manifest" : null,
    designReviewReport.warnings.length > 0 ? "design_review" : null,
    htmlReport.advisory === true ? "html_self_check" : null,
    runtimeReport.advisory === true
      || (runtimeReport.editor?.componentContrast?.failures?.length || 0) > 0
      ? "runtime_probe" : null,
  ].filter(Boolean);
  const degraded = degradedStages.length > 0;
  console.log(
    JSON.stringify({
      ok: true,
      deck: deckPath,
      html: outputPath,
      media_bindings: mediaBindings,
      qa_reports: Object.values(reports),
      warnings: warningCount,
      degraded,
      degraded_stages: degradedStages,
      delivery_status: degraded ? "degraded" : "complete",
    })
  );
}

try {
  main();
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
