"use strict";

const fs = require("fs");
const path = require("path");
const { randomUUID } = require("crypto");

function writeDeclaration(target, content) {
  const temporary = `${target}.${randomUUID()}.tmp`;
  try {
    fs.writeFileSync(temporary, content, { flag: "wx" });
    fs.renameSync(temporary, target);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}

// Portable producer contract: scope creation precedes work; delivery registration
// follows successful construction. Per-file registrations avoid merge races.
function declareScope(root) {
  fs.mkdirSync(root, { recursive: true });
  const target = path.join(root, ".artifact-delivery.json");
  writeDeclaration(target, '{"schema_version":1,"default":"intermediate"}\n');
}

function publishArtifact(filename) {
  const target = path.resolve(filename);
  if (!fs.statSync(target).isFile()) throw new Error(`Not a delivery file: ${target}`);
  writeDeclaration(path.join(path.dirname(target), `.${path.basename(target)}.artifact.json`),
    '{"type":"artifact"}\n');
}

module.exports = { declareScope, publishArtifact };

if (require.main === module) {
  const [action, ...files] = process.argv.slice(2);
  if (action !== "publish" || !files.length) throw new Error("Usage: artifact_delivery.js publish FILE [FILE ...]");
  files.forEach(filename => {
    publishArtifact(filename);
    console.log(`[${path.resolve(filename)}]`);
  });
}
