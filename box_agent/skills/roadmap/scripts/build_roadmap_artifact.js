#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const { compileRoadmapInput } = require("./roadmap_contract_core.js");
const {
  extractRoadmapArtifactState,
} = require("./extract_roadmap_spec.js");
const { migrateRoadmapSpec } = require("./migrate_roadmap_spec.js");
const { renderRoadmapHtml } = require("./roadmap_html_core.js");
const {
  cleanupScratchTaskDirectorySync,
  consumeArtifactFileSync,
  consumeScratchInputFileSync,
  resolveArtifactPath,
  resolveOutputPath,
  snapshotConsumedInputFileSync,
  snapshotArtifactFileSync,
  writeOutputFileSync,
} = require("./roadmap_io.js");

const MAX_ARTIFACT_VERSION = Number.MAX_SAFE_INTEGER - 1;
const MAX_VERSION_ALLOCATION_ATTEMPTS = 100;

function usage() {
  console.error(
    "Usage: build_roadmap_artifact.js roadmap-draft.json|roadmap-spec.json|roadmap.html --out roadmap.html [--viewport 1440x900] [--consume-input] [--debug-dir DIR]"
  );
  process.exit(2);
}

function parseArgs(argv) {
  if (!argv[0] || argv[0].startsWith("-")) usage();
  const options = {
    input: resolveArtifactPath(argv[0]),
    out: null,
    viewport: { width: 1440, height: 900 },
    consumeInput: false,
    debugDir: null,
  };
  for (let index = 1; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--out" && value) {
      options.out = resolveOutputPath(value);
      index += 1;
    } else if (arg === "--viewport" && value) {
      const match = /^(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)$/.exec(value);
      if (!match) throw new Error("--viewport must be WIDTHxHEIGHT");
      options.viewport = { width: Number(match[1]), height: Number(match[2]) };
      index += 1;
    } else if (arg === "--consume-input") {
      options.consumeInput = true;
    } else if (arg === "--debug-dir" && value) {
      options.debugDir = resolveArtifactPath(value);
      index += 1;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (!options.out) usage();
  if (path.extname(options.out).toLowerCase() !== ".html") {
    throw new Error("--out must use an .html filename");
  }
  if (options.consumeInput && path.extname(options.input).toLowerCase() === ".html") {
    throw new Error("--consume-input is only allowed for generated JSON input");
  }
  return options;
}

function readRoadmapSpec(inputPath) {
  const extension = path.extname(inputPath).toLowerCase();
  if (extension === ".html" || extension === ".htm") {
    const state = extractRoadmapArtifactState(fs.readFileSync(inputPath, "utf8"));
    return {
      inputKind: "roadmap-html",
      spec: state.spec,
      warnings: [],
      pendingQuestions: state.pendingQuestions,
    };
  }
  const source = JSON.parse(fs.readFileSync(inputPath, "utf8"));
  const compiled = compileRoadmapInput(source);
  if (!compiled.ok || !compiled.spec) {
    const error = new Error(`Roadmap compilation failed with ${compiled.issues.length} issue(s)`);
    error.issues = compiled.issues;
    error.pendingQuestions = compiled.pending_questions;
    throw error;
  }
  return {
    inputKind: compiled.input_kind,
    spec: migrateRoadmapSpec(compiled.spec),
    warnings: compiled.warnings,
    pendingQuestions: compiled.pending_questions,
  };
}

function versionedOutputCandidate(requestedPath) {
  const directory = path.dirname(requestedPath);
  const extension = path.extname(requestedPath);
  const requestedStem = path.basename(requestedPath, extension);
  const stem = requestedStem.replace(/-v\d+$/i, "");
  const legacyPath = path.join(directory, `${stem}${extension}`);
  let maximumVersion = fs.existsSync(legacyPath) ? 1 : 0;
  if (fs.existsSync(directory)) {
    const matcher = new RegExp(`^${stem.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}-v(\\d+)\\${extension}$`, "i");
    for (const name of fs.readdirSync(directory)) {
      const match = matcher.exec(name);
      if (match) {
        const version = Number(match[1]);
        if (!Number.isSafeInteger(version) || version > MAX_ARTIFACT_VERSION) {
          throw new Error(`Roadmap artifact version is outside the supported range: ${name}`);
        }
        maximumVersion = Math.max(maximumVersion, version);
      }
    }
  }
  if (maximumVersion >= MAX_ARTIFACT_VERSION) {
    throw new Error(`Roadmap artifact version limit reached for ${requestedPath}`);
  }
  const version = maximumVersion + 1;
  return {
    baseFilename: `${stem}${extension}`,
    path: path.join(directory, `${stem}-v${version}${extension}`),
    version,
  };
}

function writeDebugArtifacts(debugDir, rendered, report) {
  if (!debugDir) return;
  const files = [
    ["roadmap-spec-v1.json", rendered.spec],
    ["roadmap-geometry.json", rendered.geometry],
    ["roadmap-build-report.json", report],
  ];
  for (const [filename, value] of files) {
    writeOutputFileSync(
      path.join(debugDir, filename),
      `${JSON.stringify(value, null, 2)}\n`,
    );
  }
}

function buildRoadmapArtifactOnce(options, consumedInputSnapshot) {
  const input = readRoadmapSpec(options.input);
  if (options.consumeInput && input.inputKind !== "roadmap-draft") {
    throw new Error("--consume-input is only allowed for RoadmapDraft input");
  }
  let candidate;
  let rendered;
  let written = false;
  for (let attempt = 0; attempt < MAX_VERSION_ALLOCATION_ATTEMPTS; attempt += 1) {
    candidate = versionedOutputCandidate(options.out);
    rendered = renderRoadmapHtml(input.spec, {
      viewport: options.viewport,
      generationVersion: candidate.version,
      pendingQuestions: input.pendingQuestions,
    });
    try {
      writeOutputFileSync(candidate.path, rendered.html, { exclusive: true });
      written = true;
      break;
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
    }
  }
  if (!written || !candidate || !rendered) {
    throw new Error(
      `Unable to allocate a unique Roadmap artifact after ${MAX_VERSION_ALLOCATION_ATTEMPTS} attempts`,
    );
  }
  const writtenArtifactSnapshot = snapshotArtifactFileSync(candidate.path);
  const extracted = extractRoadmapArtifactState(fs.readFileSync(candidate.path, "utf8"));
  if (
    JSON.stringify(extracted.spec) !== JSON.stringify(rendered.spec)
    || JSON.stringify(extracted.pendingQuestions) !== JSON.stringify(rendered.pendingQuestions)
  ) {
    consumeArtifactFileSync(candidate.path, writtenArtifactSnapshot.identity);
    throw new Error("Roadmap HTML self-check failed: embedded #deck-document drifted from rendered spec");
  }
  const report = {
    ok: true,
    path: candidate.path,
    filename: path.basename(candidate.path),
    base_filename: candidate.baseFilename,
    generation_version: candidate.version,
    input_kind: input.inputKind,
    mime_type: "text/html",
    layout_id: "roadmap-swimlane-v1",
    palette_id: "roadmap-default-v1",
    editable: true,
    status: rendered.pendingQuestions.length ? "preview" : "final",
    warnings: input.warnings,
    pending_questions: rendered.pendingQuestions,
    diagnostics: rendered.diagnostics,
  };
  writeDebugArtifacts(options.debugDir, rendered, report);
  if (consumedInputSnapshot) {
    consumeScratchInputFileSync(options.input, consumedInputSnapshot);
  }
  return report;
}

function buildRoadmapArtifact(options) {
  let consumedInputSnapshot = null;
  let buildError = null;
  try {
    consumedInputSnapshot = options.consumeInput
      ? snapshotConsumedInputFileSync(options.input)
      : null;
    return buildRoadmapArtifactOnce(options, consumedInputSnapshot);
  } catch (error) {
    buildError = error;
    throw error;
  } finally {
    if (options.consumeInput) {
      try {
        cleanupScratchTaskDirectorySync(options.input, consumedInputSnapshot);
      } catch (cleanupError) {
        if (buildError instanceof Error) {
          buildError.message += `; failed to clean scratch task: ${cleanupError.message}`;
        } else {
          throw cleanupError;
        }
      }
    }
  }
}

function main() {
  const report = buildRoadmapArtifact(parseArgs(process.argv.slice(2)));
  console.log(JSON.stringify(report, null, 2));
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    for (const issue of Array.isArray(error?.issues) ? error.issues : []) console.error(`- ${issue}`);
    for (const question of Array.isArray(error?.pendingQuestions) ? error.pendingQuestions : []) {
      console.error(`? ${question.prompt}`);
    }
    console.error(error instanceof Error ? error.message : String(error));
    console.log(JSON.stringify({
      ok: false,
      error: error instanceof Error ? error.message : String(error),
      issues: Array.isArray(error?.issues) ? error.issues : [],
      pending_questions: Array.isArray(error?.pendingQuestions) ? error.pendingQuestions : [],
    }, null, 2));
    process.exit(1);
  }
}

module.exports = {
  buildRoadmapArtifact,
  MAX_ARTIFACT_VERSION,
  MAX_VERSION_ALLOCATION_ATTEMPTS,
  parseArgs,
  readRoadmapSpec,
  versionedOutputCandidate,
};
