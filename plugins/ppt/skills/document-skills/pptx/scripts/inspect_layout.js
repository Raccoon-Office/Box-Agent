#!/usr/bin/env node
"use strict";

const { getLayout, normalizeLayoutId } = require("./deck_spec_core.js");
const { manifestRecord } = require("../layouts/registry.js");

function main() {
  const [layoutId] = process.argv.slice(2);
  if (!layoutId || layoutId === "--help" || layoutId === "-h") {
    console.log("Usage: inspect_layout.js LAYOUT_ID");
    process.exit(layoutId ? 0 : 2);
  }
  const layout = getLayout(normalizeLayoutId(layoutId));
  if (!layout) {
    console.error(`Unknown layout: ${layoutId}`);
    process.exit(1);
  }
  console.log(JSON.stringify(manifestRecord(layout), null, 2));
}

main();
