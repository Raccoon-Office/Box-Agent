#!/usr/bin/env node
"use strict";

const path = require("path");

const fs = require("fs");
const {
  resolveArtifactPath,
  resolveOutputPath,
  writeOutputFileSync,
} = require("./roadmap_io.js");
const {
  ROADMAP_LAYOUT_ID,
  ROADMAP_MIME_TYPE,
  ROADMAP_RENDERER_VERSION,
  renderRoadmapHtml,
} = require("./roadmap_html_core.js");

function parseArgs(argv) {
  if (!argv[0] || argv[0] === "--help" || argv[0] === "-h") {
    console.log("Usage: render_roadmap_html.js roadmap-spec.json --out roadmap.html [--viewport 1440x900]");
    process.exit(argv[0] ? 0 : 2);
  }
  const options = { spec: argv[0], out: null, viewport: { width: 1440, height: 900 } };
  for (let index = 1; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--out" && value) {
      options.out = value;
      index += 1;
    } else if (arg === "--viewport" && value) {
      const match = /^(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)$/.exec(value);
      if (!match) throw new Error("--viewport must be WIDTHxHEIGHT");
      options.viewport = { width: Number(match[1]), height: Number(match[2]) };
      index += 1;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (!options.out) throw new Error("--out is required");
  return options;
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  const sourcePath = resolveArtifactPath(options.spec);
  const outputPath = resolveOutputPath(options.out);
  const source = JSON.parse(fs.readFileSync(sourcePath, "utf8"));
  const rendered = renderRoadmapHtml(source, { viewport: options.viewport });
  writeOutputFileSync(outputPath, rendered.html);
  console.log(JSON.stringify({
    path: outputPath,
    filename: path.basename(outputPath),
    mime_type: ROADMAP_MIME_TYPE,
    layout_id: ROADMAP_LAYOUT_ID,
    schema_version: rendered.spec.schema_version,
    geometry_version: rendered.geometry.schema_version,
    renderer_version: ROADMAP_RENDERER_VERSION,
    editable: true,
    diagnostics: rendered.diagnostics,
  }, null, 2));
}

try {
  main();
} catch (error) {
  const issues = Array.isArray(error?.issues) ? error.issues : [];
  for (const issue of issues) console.error(`- ${issue}`);
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
