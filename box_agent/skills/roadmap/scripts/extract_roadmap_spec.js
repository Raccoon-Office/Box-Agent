#!/usr/bin/env node
"use strict";

const fs = require("fs");

const {
  pendingQuestionsForRoadmapSpec,
  validateAndNormalizeRoadmapSpec,
} = require("./roadmap_contract_core.js");
const {
  resolveArtifactPath,
  resolveOutputPath,
  writeOutputFileSync,
} = require("./roadmap_io.js");

function parseArgs(argv) {
  if (!argv[0] || argv[0] === "--help" || argv[0] === "-h") {
    console.log("Usage: extract_roadmap_spec.js roadmap.html --out roadmap-spec.json");
    process.exit(argv[0] ? 0 : 2);
  }
  const options = { html: argv[0], out: null };
  for (let index = 1; index < argv.length; index += 1) {
    if (argv[index] === "--out" && argv[index + 1]) {
      options.out = argv[index + 1];
      index += 1;
    } else {
      throw new Error(`Unknown argument: ${argv[index]}`);
    }
  }
  if (!options.out) throw new Error("--out is required");
  return options;
}

function extractRoadmapSpec(html) {
  const match = String(html).match(
    /<script\b(?=[^>]*\bid=["']deck-document["'])(?=[^>]*\btype=["']application\/json["'])[^>]*>([\s\S]*?)<\/script\s*>/i,
  );
  if (!match) throw new Error("Roadmap HTML is missing #deck-document application/json source");
  const parsed = JSON.parse(match[1]);
  const result = validateAndNormalizeRoadmapSpec(parsed);
  if (!result.ok) {
    const error = new Error(`Embedded RoadmapSpec validation failed with ${result.issues.length} issue(s)`);
    error.issues = result.issues;
    throw error;
  }
  return result.normalized;
}

function extractRoadmapArtifactState(html) {
  const source = String(html);
  const spec = extractRoadmapSpec(source);
  const match = source.match(
    /<script\b(?=[^>]*\bid=["']roadmap-pending-questions["'])(?=[^>]*\btype=["']application\/json["'])[^>]*>([\s\S]*?)<\/script\s*>/i,
  );
  const persistedQuestions = match ? JSON.parse(match[1]) : [];
  const questionResult = pendingQuestionsForRoadmapSpec(spec, persistedQuestions);
  if (!questionResult.ok) {
    const error = new Error(`Embedded pending question validation failed with ${questionResult.issues.length} issue(s)`);
    error.issues = questionResult.issues;
    throw error;
  }
  return { spec, pendingQuestions: questionResult.pending_questions };
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  const htmlPath = resolveArtifactPath(options.html);
  const outputPath = resolveOutputPath(options.out);
  const spec = extractRoadmapSpec(fs.readFileSync(htmlPath, "utf8"));
  writeOutputFileSync(outputPath, `${JSON.stringify(spec, null, 2)}\n`);
  console.log(JSON.stringify({
    source: htmlPath,
    output: outputPath,
    schema_version: spec.schema_version,
    source_of_truth: "#deck-document",
  }, null, 2));
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    for (const issue of Array.isArray(error?.issues) ? error.issues : []) console.error(`- ${issue}`);
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
  }
}

module.exports = { extractRoadmapArtifactState, extractRoadmapSpec, parseArgs };
