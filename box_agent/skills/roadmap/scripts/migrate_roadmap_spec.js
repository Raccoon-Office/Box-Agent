#!/usr/bin/env node
"use strict";

const fs = require("fs");
const {
  resolveArtifactPath,
  resolveOutputPath,
  writeOutputFileSync,
} = require("./roadmap_io.js");

const CURRENT_ROADMAP_SCHEMA_VERSION = 1;

function migrateRoadmapSpec(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("$: roadmap spec must be an object");
  }
  if (value.kind !== "roadmap-spec") {
    throw new Error("kind: expected roadmap-spec");
  }
  if (!Number.isInteger(value.schema_version)) {
    throw new Error("schema_version: required integer; migration never guesses a version");
  }
  if (value.schema_version !== CURRENT_ROADMAP_SCHEMA_VERSION) {
    throw new Error(
      `schema_version: unsupported ${value.schema_version}; expected ${CURRENT_ROADMAP_SCHEMA_VERSION}`,
    );
  }
  return JSON.parse(JSON.stringify(value));
}

function usage() {
  console.error("Usage: migrate_roadmap_spec.js roadmap-spec.json --out roadmap-spec-v1.json");
  process.exit(2);
}

function parseArgs(argv) {
  if (!argv[0] || argv[0].startsWith("-")) usage();
  const opts = { input: resolveArtifactPath(argv[0]), out: null };
  for (let index = 1; index < argv.length; index += 1) {
    if (argv[index] === "--out" && argv[index + 1]) {
      opts.out = resolveOutputPath(argv[index + 1]);
      index += 1;
    } else usage();
  }
  if (!opts.out) usage();
  return opts;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const migrated = migrateRoadmapSpec(JSON.parse(fs.readFileSync(opts.input, "utf8")));
  writeOutputFileSync(opts.out, `${JSON.stringify(migrated, null, 2)}\n`);
  console.log(JSON.stringify({ ok: true, schema_version: migrated.schema_version, output: opts.out }));
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error));
    process.exit(1);
  }
}

module.exports = {
  CURRENT_ROADMAP_SCHEMA_VERSION,
  migrateRoadmapSpec,
};
