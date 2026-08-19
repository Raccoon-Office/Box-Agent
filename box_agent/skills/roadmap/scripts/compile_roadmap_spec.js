#!/usr/bin/env node
"use strict";

const fs = require("fs");
const {
  resolveArtifactPath,
  resolveOutputPath,
  writeOutputFileSync,
} = require("./roadmap_io.js");
const { compileRoadmapInput } = require("./roadmap_contract_core.js");

function usage() {
  console.error("Usage: compile_roadmap_spec.js roadmap-draft.json --out roadmap-spec.json [--report qa/roadmap_contract.json]");
  process.exit(2);
}

function parseArgs(argv) {
  if (!argv[0] || argv[0].startsWith("-")) usage();
  const opts = { input: resolveArtifactPath(argv[0]), out: null, report: null };
  for (let index = 1; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--out" && value) {
      opts.out = resolveOutputPath(value);
      index += 1;
    } else if (arg === "--report" && value) {
      opts.report = resolveOutputPath(value);
      index += 1;
    } else usage();
  }
  if (!opts.out) usage();
  return opts;
}

function writeJson(filePath, value) {
  writeOutputFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const input = JSON.parse(fs.readFileSync(opts.input, "utf8"));
  const result = compileRoadmapInput(input);
  const report = {
    ok: result.ok,
    input: opts.input,
    input_kind: result.input_kind,
    output: result.ok ? opts.out : null,
    issues: result.issues,
    warnings: result.warnings,
    pending_questions: result.pending_questions,
  };
  if (result.ok) writeJson(opts.out, result.spec);
  if (opts.report) writeJson(opts.report, report);
  console.log(JSON.stringify({ ...report, ...(result.ok ? { spec: result.spec } : {}) }));
  if (!result.ok) process.exit(1);
}

try {
  main();
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
