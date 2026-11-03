"use strict";

const fs = require("fs");
const path = require("path");
const { createHash, randomUUID } = require("crypto");

// Resolve the existing prefix too, so nonexistent preview outputs and symlinked
// output directories use the same canonical identity as Python's Path.resolve.
function canonical(filename) {
  let current = path.resolve(filename);
  const missing = [];
  while (!fs.existsSync(current)) {
    const parent = path.dirname(current);
    if (parent === current) break;
    missing.unshift(path.basename(current));
    current = parent;
  }
  return path.join(fs.realpathSync(current), ...missing);
}

function isSymbolicLink(filename) {
  try { return fs.lstatSync(filename).isSymbolicLink(); }
  catch (error) { if (error.code === "ENOENT") return false; throw error; }
}

function enclosingWorkspace(start) {
  for (let current = start; ; current = path.dirname(current)) {
    if (fs.existsSync(path.join(current, ".box-agent")) || isSymbolicLink(path.join(current, ".box-agent"))
        || fs.existsSync(path.join(current, ".git"))) return current;
    if (path.dirname(current) === current) return null;
  }
}

function workspaceRoot(target, workspace, directory) {
  if (workspace || process.env.BOX_AGENT_WORKSPACE_DIR) return canonical(workspace || process.env.BOX_AGENT_WORKSPACE_DIR);
  return inferredWorkspace(target, directory);
}

function inferredWorkspace(target, directory) {
  const parent = directory ? target : path.dirname(target);
  const enclosing = enclosingWorkspace(parent);
  if (enclosing) return enclosing;
  const cwd = canonical(process.cwd());
  const relative = path.relative(cwd, target);
  if (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative)) {
    const fromCwd = enclosingWorkspace(cwd);
    if (fromCwd) return fromCwd;
  }
  return parent;
}

function ensureInternalDirectory(root, kind, create = true) {
  if (create) fs.mkdirSync(root, { recursive: true });
  let current = root;
  for (const segment of [".box-agent", "artifact-metadata", kind]) {
    current = path.join(current, segment);
    if (create) {
      try { fs.mkdirSync(current); } catch (error) { if (error.code !== "EEXIST") throw error; }
    }
    let stat;
    try { stat = fs.lstatSync(current); }
    catch (error) { if (!create && error.code === "ENOENT") return null; throw error; }
    if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error(`Unsafe artifact metadata directory: ${current}`);
  }
  return current;
}

function readJson(filename, limit) {
  try {
    const stat = fs.statSync(filename);
    if (!stat.isFile() || stat.size > limit) return {};
    const fd = fs.openSync(filename, "r");
    try {
      const buffer = Buffer.alloc(limit + 1);
      const size = fs.readSync(fd, buffer, 0, buffer.length, 0);
      if (size > limit) return {};
      const value = JSON.parse(buffer.subarray(0, size).toString("utf8"));
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } finally { fs.closeSync(fd); }
  } catch (error) {
    if (error.code === "ENOENT" || error instanceof SyntaxError) return {};
    throw error;
  }
}

function excludeInternalDirectory(root) {
  for (let current = root; ; current = path.dirname(current)) {
    const marker = path.join(current, ".git");
    if (isSymbolicLink(marker)) return;
    if (fs.existsSync(marker)) {
      let gitDir = marker;
      if (!fs.statSync(marker).isDirectory()) {
        const text = fs.readFileSync(marker, "utf8").trim();
        if (!text.startsWith("gitdir:")) return;
        gitDir = path.resolve(current, text.slice(7).trim());
      }
      const common = path.join(gitDir, "commondir");
      if (fs.existsSync(common)) gitDir = path.resolve(gitDir, fs.readFileSync(common, "utf8").trim());
      const info = path.join(gitDir, "info");
      if (isSymbolicLink(info)) return;
      fs.mkdirSync(info, { recursive: true });
      const exclude = path.join(info, "exclude");
      if (isSymbolicLink(exclude)) return;
      const existing = fs.existsSync(exclude) ? fs.readFileSync(exclude, "utf8") : "";
      if (!existing.split(/\r?\n/).includes(".box-agent/")) {
        fs.appendFileSync(exclude, (existing && !existing.endsWith("\n") ? "\n" : "") + ".box-agent/\n");
      }
      return;
    }
    if (path.dirname(current) === current) return;
  }
}

function retainedRecord(directory, digest, target) {
  if (!directory) return false;
  const record = path.join(directory, `${digest}.json`);
  if (isSymbolicLink(record)) throw new Error(`Unsafe artifact metadata record: ${record}`);
  const existing = readJson(record, 16384);
  return existing.path === target && existing.metadata && existing.metadata.retain_for_chat === true
    && Buffer.byteLength(JSON.stringify(existing.metadata), "utf8") <= 4096;
}

function ancestorRetainedRecord(target, root, digest) {
  // Target-local standalone stores remain readable for external attachments.
  // Only inspect exact-path records; all writes still use the selected root.
  for (let current = path.dirname(target); ; current = path.dirname(current)) {
    if (current !== root) {
      const directory = ensureInternalDirectory(current, "files", false);
      if (retainedRecord(directory, digest, target)) return true;
    }
    if (path.dirname(current) === current) break;
  }
  return false;
}

function writeMetadata(filename, metadata, workspace, kind = "files") {
  const target = canonical(filename);
  const root = workspaceRoot(target, workspace, kind === "scopes");
  const directory = ensureInternalDirectory(root, kind);
  const digest = createHash("sha256").update(target, "utf8").digest("hex");
  const record = path.join(directory, `${digest}.json`);
  try {
    if (fs.lstatSync(record).isSymbolicLink()) throw new Error(`Unsafe artifact metadata record: ${record}`);
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  if (kind === "files") {
    const legacy = readJson(path.join(path.dirname(target), `.${path.basename(target)}.artifact.json`), 4096);
    if (retainedRecord(directory, digest, target) || ancestorRetainedRecord(target, root, digest)
        || legacy.retain_for_chat === true) metadata = { ...metadata, retain_for_chat: true };
  }
  const temporary = path.join(directory, `.${digest}.${randomUUID()}.tmp`);
  try {
    fs.writeFileSync(temporary, JSON.stringify({ path: target, metadata }) + "\n", { flag: "wx", mode: 0o600 });
    ensureInternalDirectory(root, kind);
    fs.renameSync(temporary, record);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
  try { excludeInternalDirectory(root); }
  catch { /* Optional Git administration must not discard a durable record. */ }
}

// Scope creation precedes work; delivery registration follows successful
// construction. Independent records avoid a shared mutable index.
function declareScope(root, workspace) {
  fs.mkdirSync(root, { recursive: true });
  writeMetadata(root, { schema_version: 1, default: "intermediate" }, workspace, "scopes");
}

function markIntermediate(filename, workspace) {
  writeMetadata(filename, { type: "intermediate_asset" }, workspace);
}

function publishArtifact(filename, workspace) {
  if (!fs.statSync(filename).isFile()) throw new Error(`Not a delivery file: ${filename}`);
  writeMetadata(filename, { type: "artifact" }, workspace);
}

module.exports = { declareScope, markIntermediate, publishArtifact, writeMetadata };

if (require.main === module) {
  const [action, ...files] = process.argv.slice(2);
  if (action !== "publish" || !files.length) throw new Error("Usage: artifact_delivery.js publish FILE [FILE ...]");
  files.forEach(filename => {
    publishArtifact(filename);
    console.log(`[${path.resolve(filename)}]`);
  });
}
