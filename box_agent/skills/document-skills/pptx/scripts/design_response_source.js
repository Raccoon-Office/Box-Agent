"use strict";

// Read-only provenance adapter. Session Log remains the authority; this module
// never writes sessions or interprets provider thinking chunks.
const fs = require("fs");
const path = require("path");
const os = require("os");
const { createHash } = require("crypto");
const sha = value => createHash("sha256").update(value).digest("hex");
const normalizedPath = value => process.platform === "win32" ? path.resolve(value).toLowerCase() : path.resolve(value);
function workspaceRelated(sessionCwd, root) {
  const sessionPath = normalizedPath(sessionCwd);
  const rootPath = normalizedPath(root);
  if (sessionPath === rootPath) return true;
  const inside = (parent, child) => {
    const relative = path.relative(parent, child);
    return relative && !relative.startsWith(".." + path.sep) && relative !== ".." && !path.isAbsolute(relative);
  };
  // A child may run from the CLI's parent workspace while the deck lives in
  // a semantic subdirectory. The reverse relation would admit unrelated
  // nested sessions and is intentionally rejected.
  return inside(sessionPath, rootPath);
}
function sessionRoot() {
  return path.join(process.env.BOX_AGENT_HOME || path.join(os.homedir(), ".box-agent"), "sessions");
}
function sessionFile(id) {
  if (!/^subagent-[a-zA-Z0-9_-]+$/.test(id)) throw new Error("design receipt: invalid child session id");
  return path.join(sessionRoot(), sha(id), "session.jsonl");
}
function readResponse(file, input, root, strict = true) {
  const lines = fs.readFileSync(file, "utf8").trim().split("\n");
  const header = JSON.parse(lines[0]);
  if (header.origin !== "subagent" || !header.parentSession || !workspaceRelated(header.cwd, root)) return null;
  const requestFile = input.request_file;
  const records = lines.slice(1).map(line => JSON.parse(line));
  const task = records.find(record => record.type === "user/message")?.data?.content;
  const correctionFile = path.join(path.dirname(requestFile), "correction.json");
  if (typeof task !== "string" || (!task.includes(requestFile) && !task.includes(correctionFile))) return null;
  let correction = null;
  if (task.includes(correctionFile) && fs.existsSync(correctionFile)) {
    const candidate = JSON.parse(fs.readFileSync(correctionFile,"utf8"));
    if (candidate.requires_full_read === false && candidate.brief_file === requestFile
      && candidate.base_session_id !== header.id) correction = candidate;
  }
  const readFile = correction ? correctionFile : requestFile;
  const expectedHash = sha(fs.readFileSync(readFile));
  const reads = records.filter(record => record.type === "tool/result").map(record => record.data.result)
    .filter(result => result?.success === true && result.rawOutput?.context_resource?.resource_id
      && normalizedPath(result.rawOutput.context_resource.resource_id) === normalizedPath(readFile)
      && result.rawOutput.context_resource.content_version === expectedHash)
    .map(result => result.rawOutput.context_resource).sort((a, b) => a.start_line - b.start_line);
  let end = 0;
  for (const read of reads) if (read.start_line <= end + 1) end = Math.max(end, read.end_line);
  const readComplete = reads.length > 0 && end >= reads[0].total_lines;
  const requiredFiles = correction ? [] : JSON.parse(fs.readFileSync(requestFile, "utf8")).required_read_files || [];
  const missingFiles = requiredFiles.filter(resource => {
    if (sha(fs.readFileSync(resource.path)) !== resource.sha256) return true;
    const pages = records.filter(record => record.type === "tool/result")
      .map(record => record.data.result).filter(result => result?.success === true)
      .map(result => result.rawOutput?.context_resource).filter(item => item
        && normalizedPath(item.resource_id) === normalizedPath(resource.path)
        && item.content_version === resource.sha256).sort((a,b) => a.start_line-b.start_line);
    let covered = 0;
    for (const page of pages) if (page.start_line <= covered+1) covered = Math.max(covered,page.end_line);
    return !pages.length || covered < pages[0].total_lines;
  });
  const done = records.filter(record => record.type === "turn/end").at(-1);
  if (!done) return null;
  const criticalMissing = missingFiles.filter(item => /^pages-/.test(path.basename(item.path)));
  const advisoryMissing = missingFiles.filter(item => !criticalMissing.includes(item));
  const message = records.filter(record => record.type === "assistant/message")
    .map(record => record.data.message).filter(message => !message.tool_calls?.length && message.content).at(-1);
  const error = !readComplete ? "designer did not read the complete current brief"
    : criticalMissing.length ? `designer did not read required packets: ${criticalMissing.map(item=>path.basename(item.path)).join(", ")}`
    : done.data?.reason?.kind !== "completed" ? "designer did not complete"
      : !message || typeof message.content !== "string" ? "designer produced no final text" : null;
  if (error && strict) return null;
  const text = message?.content || "";
  return { session_id: header.id, created_at: header.createdAt, text, response_hash: sha(text), error,
    ...(advisoryMissing.length ? { warnings: [`designer did not read advisory packets: ${advisoryMissing.map(item=>path.basename(item.path)).join(", ")}`] } : {}),
    ...(correction ? { correction_base: { session_id: correction.base_session_id,
      response_hash: correction.base_response_hash }, correction_issues: correction.issues } : {}) };
}
function findResponses(input, root) {
  if (!fs.existsSync(sessionRoot())) return [];
  const results = [];
  for (const entry of fs.readdirSync(sessionRoot(), { withFileTypes: true })) {
    if (!entry.isDirectory() || !/^[a-f0-9]{64}$/.test(entry.name)) continue;
    const file = path.join(sessionRoot(), entry.name, "session.jsonl");
    try {
      // Filter by the small header before reading a session's body.
      const fd = fs.openSync(file, "r");
      const buffer = Buffer.alloc(4096);
      let length;
      try { length = fs.readSync(fd, buffer, 0, buffer.length, 0); } finally { fs.closeSync(fd); }
      const header = JSON.parse(buffer.subarray(0, length).toString("utf8").split("\n")[0]);
      if (header.origin !== "subagent" || !workspaceRelated(header.cwd, root)
        || header.createdAt < input.request_created_at) continue;
      const response = readResponse(file, input, root, false);
      if (response) results.push(response);
    } catch (_error) { /* In-progress or unrelated session records are not accepted. */ }
  }
  return results.sort((a, b) => a.created_at - b.created_at);
}
function parseDecision(text) {
  const body = text.trim();
  try {
    return JSON.parse(body);
  } catch (_error) { /* Accept one JSON object inside presentation prose. */ }
  const fenced = body.match(/```(?:json)?\s*([\s\S]*?)\s*```/i)?.[1]?.trim();
  if (fenced) {
    try { return JSON.parse(fenced); } catch (_error) { /* Continue to object extraction. */ }
  }
  const objects = [];
  let start = -1, depth = 0, quoted = false, escaped = false;
  for (let index = 0; index < body.length; index += 1) {
    const char = body[index];
    if (depth === 0) {
      if (char === "{") { start = index; depth = 1; quoted = false; escaped = false; }
      continue;
    }
    if (quoted) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') quoted = false;
    } else if (char === '"') quoted = true;
    else if (char === "{") depth += 1;
    else if (char === "}" && --depth === 0) {
      try { objects.push(JSON.parse(body.slice(start, index + 1))); } catch (_error) { /* Not a JSON object. */ }
    }
  }
  if (objects.length !== 1) throw new Error("designer response must contain exactly one unambiguous JSON object");
  return objects[0];
}

module.exports = { findResponses, readResponse, sessionFile, parseDecision, workspaceRelated };
