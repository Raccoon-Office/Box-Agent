#!/usr/bin/env node
"use strict";

// Read-only evidence for this invocation, never a search of the tool catalog.
// Session root/realpath conventions match design_response_source.js. Unlike
// design-response discovery, current-tool evidence requires exact cwd equality.
const fs = require("fs");
const path = require("path");
const os = require("os");
const { createHash } = require("crypto");

const MAX_SESSION_DIRS = 4096;
const MAX_LOG_BYTES = 64 * 1024 * 1024;
const HEADER_BYTES = 16 * 1024;
const MAX_CALL_AGE_MS = 5 * 60 * 1000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const hash = value => createHash("sha256").update(value).digest("hex");
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const unknown = reason => ({ status: "unknown", tool: "sub_agent", reason });

function canonical(value) {
  const resolved = fs.realpathSync.native(path.resolve(value));
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function shellTokens(value) {
  const tokens = [];
  if (!/[\s'"`$;&|<>\\]/.test(value)) tokens.push(value);
  if (!value.includes("'")) tokens.push(`'${value}'`);
  if (!/["`$]/.test(value)) tokens.push(`"${value}"`);
  return tokens;
}

function invocationCommands(marker) {
  const nodes = ['"$BOX_AGENT_NODE"', '"$env:BOX_AGENT_NODE"', "node", ...shellTokens(process.execPath)];
  const scripts = shellTokens(path.resolve(__filename));
  const prefixes = [""];
  for (const cwd of shellTokens(process.cwd())) {
    prefixes.push(`cd ${cwd} && `, `Set-Location -LiteralPath ${cwd} -ErrorAction Stop; `);
  }
  const commands = new Set();
  for (const prefix of prefixes) for (const node of nodes) for (const script of scripts) {
    for (const key of shellTokens(marker)) {
      commands.add(`${prefix}${node} ${script} --invocation ${key}`);
      commands.add(`${prefix}& ${node} ${script} --invocation ${key}`);
    }
  }
  return commands;
}

function readRelatedSession(file, directoryName, cwd, remainingBytes) {
  let fd;
  let related = false;
  try {
    const stat = fs.lstatSync(file);
    // A log untouched throughout the call-age window cannot bind a fresh call.
    if (!stat.isFile() || stat.mtimeMs < Date.now() - MAX_CALL_AGE_MS) return null;
    fd = fs.openSync(file, "r");
    const first = Buffer.alloc(HEADER_BYTES);
    const length = fs.readSync(fd, first, 0, first.length, 0);
    const end = first.subarray(0, length).indexOf(10);
    if (end < 0) return null;
    const header = JSON.parse(first.subarray(0, end).toString("utf8"));
    if (typeof header.cwd !== "string" || canonical(header.cwd) !== cwd) return null;
    related = true;
    if (header.type !== "session" || header.version !== 1 || typeof header.id !== "string"
      || hash(header.id) !== directoryName
      || (header.origin === "subagent" && typeof header.parentSession !== "string")) {
      return { invalid: true };
    }
    const size = fs.fstatSync(fd).size;
    if (size > remainingBytes) return { invalid: true };
    const bytes = Buffer.alloc(size);
    if (fs.readSync(fd, bytes, 0, size, 0) !== size || bytes.at(-1) !== 10) return { invalid: true };
    const records = [];
    let seq = -1;
    for (const line of bytes.toString("utf8").trimEnd().split("\n").slice(1)) {
      const record = JSON.parse(line);
      if (!Number.isInteger(record.seq) || record.seq <= seq || !object(record.data)) return { invalid: true };
      seq = record.seq;
      if (["request/header", "tool/call", "tool/result", "turn/start", "turn/end", "step/start"].includes(record.type)) {
        records.push(record);
      }
    }
    return { header, records, file, size };
  } catch (_error) {
    return related ? { invalid: true } : null;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function inspect(marker) {
  if (!UUID.test(marker || "")) return unknown("a fresh literal UUID v4 invocation marker is required");
  let cwd;
  let entries;
  const root = path.join(process.env.BOX_AGENT_HOME || path.join(os.homedir(), ".box-agent"), "sessions");
  try {
    cwd = canonical(process.env.BOX_AGENT_WORKSPACE_DIR || process.cwd());
    entries = fs.readdirSync(root, { withFileTypes: true })
      .filter(entry => entry.isDirectory() && /^[a-f0-9]{64}$/.test(entry.name));
  } catch (_error) {
    return unknown("session evidence is unavailable");
  }
  if (entries.length > MAX_SESSION_DIRS) return unknown("session scan bound exceeded");
  const commands = invocationCommands(marker);
  const matches = [];
  let incomplete = false;
  let remainingBytes = MAX_LOG_BYTES;
  for (const entry of entries) {
    const session = readRelatedSession(path.join(root, entry.name, "session.jsonl"), entry.name, cwd, remainingBytes);
    if (!session) continue;
    if (session.invalid) { incomplete = true; continue; }
    remainingBytes -= session.size;
    for (const record of session.records) {
      if (record.type === "tool/call" && record.data.name === "bash"
        && commands.has(String(record.data.arguments?.command || "").trim())) {
        matches.push({ session, call: record });
      }
    }
  }
  if (incomplete) return unknown("related session evidence is incomplete or invalid");
  if (matches.length !== 1) return unknown(matches.length ? "invocation marker is ambiguous or reused" : "no bound invocation");
  const { session, call } = matches[0];
  const { turn, step, callId } = call.data;
  if (!Number.isInteger(turn) || !Number.isInteger(step) || typeof callId !== "string"
    || !Number.isFinite(call.time) || call.time > Date.now() || Date.now() - call.time > MAX_CALL_AGE_MS) {
    return unknown("invocation is stale or invalid");
  }
  const after = session.records.filter(record => record.seq > call.seq);
  if (after.some(record => (record.type === "tool/result" && record.data.message?.tool_call_id === callId)
    || (record.type === "turn/end" && record.data.turn === turn)
    || ["turn/start", "step/start", "request/header"].includes(record.type))) {
    return unknown("invocation has completed or its turn has advanced");
  }
  const headers = session.records.filter(record => record.type === "request/header"
    && record.seq < call.seq && record.data.turn === turn && record.data.step === step);
  if (headers.length !== 1) return unknown("no unique request header for this invocation");
  const header = headers[0];
  const tools = header.data.header?.tools;
  if (!Array.isArray(tools) || tools.some(tool => !object(tool) || typeof tool.name !== "string"
    || !tool.name || !object(tool.input_schema) || tool.input_schema.type !== "object")
    || new Set(tools.map(tool => tool.name)).size !== tools.length
    || !tools.some(tool => tool.name === "bash")) {
    return unknown("request tool schema is incomplete or unknown");
  }
  const native = tools.find(tool => tool.name === "sub_agent");
  return {
    status: native ? "present" : "absent",
    tool: "sub_agent",
    reason: "bound current request header",
    invocation: marker,
    session_id: session.header.id,
    ...(session.header.parentSession ? { parent_session_id: session.header.parentSession } : {}),
    session_file: session.file,
    turn, step, call_id: callId, request_seq: header.seq,
    ...(native ? { tool_schema: native } : {}),
  };
}

if (require.main === module) {
  const args = process.argv.slice(2);
  console.log(JSON.stringify(args.length === 2 && args[0] === "--invocation"
    ? inspect(args[1]) : unknown("usage: native_delegation_status.js --invocation UUID")));
}

module.exports = { inspect };
