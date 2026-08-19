#!/usr/bin/env node
"use strict";

const fs = require("fs");
const {
  resolveArtifactPath,
  resolveOutputPath,
  writeOutputFileSync,
} = require("./roadmap_io.js");
const { layoutRoadmap } = require("./roadmap_geometry_core.js");

function usage() {
  console.error("Usage: layout_roadmap.js roadmap-spec.json --out roadmap-geometry.json [--viewport WxH]");
  process.exit(2);
}

function parseArgs(argv) {
  if (!argv[0] || argv[0].startsWith("-")) usage();
  const opts = {
    input: resolveArtifactPath(argv[0]),
    out: null,
    viewport: { width: 1440, height: 900 },
  };
  for (let index = 1; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--out" && value) {
      opts.out = resolveOutputPath(value);
      index += 1;
    } else if (arg === "--viewport" && value) {
      const match = /^(\d+)\s*[xX]\s*(\d+)$/.exec(value);
      if (!match) usage();
      opts.viewport = { width: Number(match[1]), height: Number(match[2]) };
      index += 1;
    } else usage();
  }
  if (!opts.out) usage();
  return opts;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const spec = JSON.parse(fs.readFileSync(opts.input, "utf8"));
  const geometry = layoutRoadmap(spec, opts.viewport);
  writeOutputFileSync(opts.out, `${JSON.stringify(geometry, null, 2)}\n`);
  console.log(JSON.stringify({ ok: true, output: opts.out, geometry }));
}

try {
  main();
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
