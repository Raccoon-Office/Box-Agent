#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const { buildManifest, MANIFEST_PATH } = require("./deck_spec_core.js");

function parseArgs(argv) {
  const opts = { out: MANIFEST_PATH, check: false };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--out" && argv[index + 1]) {
      opts.out = path.resolve(argv[index + 1]);
      index += 1;
    } else if (arg === "--check") {
      opts.check = true;
    } else if (arg === "--help" || arg === "-h") {
      console.log("Usage: build_layout_manifest.js [--out manifest.json] [--check]");
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return opts;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const output = `${JSON.stringify(buildManifest(), null, 2)}\n`;
  if (opts.check) {
    if (!fs.existsSync(opts.out) || fs.readFileSync(opts.out, "utf8") !== output) {
      console.error(`Layout manifest is stale: ${opts.out}`);
      process.exit(1);
    }
    console.log(`Layout manifest is current: ${opts.out}`);
    return;
  }
  fs.mkdirSync(path.dirname(opts.out), { recursive: true });
  fs.writeFileSync(opts.out, output, "utf8");
  console.log(`Wrote ${buildManifest().layouts.length} layout(s) to ${opts.out}`);
}

try {
  main();
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
