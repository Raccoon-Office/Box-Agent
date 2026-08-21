"use strict";

// BOX_AGENT_OUTPUT_DIR is a host-owned path boundary. Roadmap writers reject
// traversal, links/reparse points, and directory-identity changes before and
// after publication. A separate process running with the same OS identity can
// already mutate host-owned files directly; callers must not intentionally
// replace the output-root topology while a synchronous write is in progress.

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

function artifactRoot() {
  const configured = String(process.env.BOX_AGENT_OUTPUT_DIR || "").trim();
  return configured ? path.resolve(configured) : process.cwd();
}

function scratchRoot() {
  const configured = String(process.env.BOX_AGENT_SCRATCH_DIR || "").trim();
  return configured ? path.resolve(configured) : null;
}

function resolveArtifactPath(filePath) {
  if (path.isAbsolute(filePath)) return path.resolve(filePath);
  return path.resolve(artifactRoot(), filePath);
}

function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (
    !relative.startsWith(`..${path.sep}`)
    && relative !== ".."
    && !path.isAbsolute(relative)
  );
}

function isMissing(error) {
  return error && error.code === "ENOENT";
}

function outputPathError(message, filePath) {
  return new Error(`${message}: ${filePath}`);
}

function rejectLink(filePath, displayPath, pathKind = "output") {
  let stats;
  try {
    stats = fs.lstatSync(filePath);
  } catch (error) {
    if (isMissing(error)) return null;
    throw error;
  }
  if (stats.isSymbolicLink()) {
    throw outputPathError(
      `${pathKind} path must not contain a symlink or reparse point`,
      displayPath,
    );
  }
  return stats;
}

function artifactFileIdentity(stats) {
  return {
    dev: stats.dev,
    ino: stats.ino,
    size: stats.size,
    mtimeMs: stats.mtimeMs,
    ctimeMs: stats.ctimeMs,
  };
}

function sameArtifactFileIdentity(left, right) {
  return left.dev === right.dev
    && left.ino === right.ino
    && left.size === right.size
    && left.mtimeMs === right.mtimeMs
    && left.ctimeMs === right.ctimeMs;
}

function sameArtifactInode(left, right) {
  return left.dev === right.dev && left.ino === right.ino;
}

function snapshotOutputDirectoryChain(resolved, displayPath) {
  const configured = String(process.env.BOX_AGENT_OUTPUT_DIR || "").trim();
  if (!configured) return null;
  const root = artifactRoot();
  const parent = path.dirname(resolved);
  const paths = [root];
  let current = root;
  for (const segment of path.relative(root, parent).split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    paths.push(current);
  }
  return paths.map((directory) => {
    const stats = rejectLink(directory, displayPath);
    if (stats === null || !stats.isDirectory()) {
      throw outputPathError("output parent path must be a directory", displayPath);
    }
    return {
      path: directory,
      identity: { dev: stats.dev, ino: stats.ino },
    };
  });
}

function assertOutputDirectoryChainUnchanged(snapshot, displayPath) {
  if (snapshot === null) return;
  for (const entry of snapshot) {
    const stats = rejectLink(entry.path, displayPath);
    if (
      stats === null
      || !stats.isDirectory()
      || stats.dev !== entry.identity.dev
      || stats.ino !== entry.identity.ino
    ) {
      throw outputPathError(
        "output directory changed during publication",
        displayPath,
      );
    }
  }
}

function removePublishedFileOnBoundaryFailure(resolved, publishedIdentity, boundaryError) {
  try {
    const stats = fs.lstatSync(resolved);
    if (stats.isFile() && sameArtifactInode(stats, publishedIdentity)) {
      fs.unlinkSync(resolved);
    }
  } catch (cleanupError) {
    if (!isMissing(cleanupError)) {
      boundaryError.message += `; failed to remove rejected output: ${cleanupError.message}`;
    }
  }
}

function snapshotFileWithinRootSync(filePath, root, boundaryName) {
  const resolved = path.resolve(filePath);
  if (!isWithin(root, resolved)) {
    throw outputPathError(
      `consumed input must stay within ${boundaryName}`,
      filePath,
    );
  }

  const rootStats = rejectLink(root, filePath, "input");
  if (rootStats === null || !rootStats.isDirectory()) {
    throw outputPathError("consumed input root must be a real directory", filePath);
  }
  let current = root;
  let targetStats = null;
  const segments = path.relative(root, resolved).split(path.sep).filter(Boolean);
  for (let index = 0; index < segments.length; index += 1) {
    current = path.join(current, segments[index]);
    const stats = rejectLink(current, filePath, "input");
    if (stats === null) {
      throw outputPathError("consumed input does not exist", filePath);
    }
    if (index < segments.length - 1 && !stats.isDirectory()) {
      throw outputPathError("consumed input parent must be a directory", filePath);
    }
    targetStats = stats;
  }
  if (segments.length === 0) {
    targetStats = rejectLink(resolved, filePath, "input");
  }
  if (targetStats === null) {
    throw outputPathError("consumed input does not exist", filePath);
  }
  if (!targetStats.isFile()) {
    throw outputPathError("consumed input must be a regular file", filePath);
  }

  const realRoot = fs.realpathSync(root);
  if (!isWithin(realRoot, fs.realpathSync(resolved))) {
    throw outputPathError(
      `consumed input resolves outside ${boundaryName}`,
      filePath,
    );
  }
  return {
    path: resolved,
    identity: artifactFileIdentity(targetStats),
    rootIdentity: artifactFileIdentity(rootStats),
  };
}

function snapshotArtifactFileSync(filePath) {
  const resolved = resolveArtifactPath(filePath);
  const configured = String(process.env.BOX_AGENT_OUTPUT_DIR || "").trim();
  const root = configured ? artifactRoot() : path.parse(resolved).root;
  return snapshotFileWithinRootSync(
    resolved,
    root,
    configured ? "BOX_AGENT_OUTPUT_DIR" : "filesystem root",
  );
}

function snapshotConsumedInputFileSync(filePath) {
  const resolved = path.resolve(filePath);
  const scratch = scratchRoot();
  if (scratch !== null) {
    const segments = path.relative(scratch, resolved).split(path.sep).filter(Boolean);
    if (segments.length < 2) {
      throw outputPathError(
        "consumed input must stay in a task directory within BOX_AGENT_SCRATCH_DIR",
        filePath,
      );
    }
    const snapshot = snapshotFileWithinRootSync(
      resolved,
      scratch,
      "BOX_AGENT_SCRATCH_DIR",
    );
    const taskDirectory = path.join(scratch, segments[0]);
    const taskStats = rejectLink(taskDirectory, filePath, "input");
    if (taskStats === null || !taskStats.isDirectory()) {
      throw outputPathError("scratch task path must be a directory", filePath);
    }
    return {
      ...snapshot,
      taskDirectory,
      taskDirectoryIdentity: artifactFileIdentity(taskStats),
    };
  }
  return snapshotArtifactFileSync(resolved);
}

function consumeArtifactFileSync(filePath, expectedIdentity) {
  const current = snapshotArtifactFileSync(filePath);
  if (!sameArtifactFileIdentity(current.identity, expectedIdentity)) {
    throw outputPathError("consumed input changed before deletion", filePath);
  }
  fs.unlinkSync(current.path);
}

function consumeScratchInputFileSync(filePath, expectedSnapshot) {
  const current = snapshotConsumedInputFileSync(filePath);
  if (
    expectedSnapshot.rootIdentity
    && !sameArtifactInode(current.rootIdentity, expectedSnapshot.rootIdentity)
  ) {
    throw outputPathError("scratch root changed before deletion", filePath);
  }
  if (
    expectedSnapshot.taskDirectoryIdentity
    && !sameArtifactInode(
      current.taskDirectoryIdentity,
      expectedSnapshot.taskDirectoryIdentity,
    )
  ) {
    throw outputPathError("scratch task directory changed before deletion", filePath);
  }
  if (!sameArtifactFileIdentity(current.identity, expectedSnapshot.identity)) {
    throw outputPathError("consumed input changed before deletion", filePath);
  }
  fs.unlinkSync(current.path);
}

function cleanupScratchTaskDirectorySync(filePath, expectedSnapshot = null) {
  const scratch = scratchRoot();
  if (scratch === null) return;
  const resolved = path.resolve(filePath);
  if (!isWithin(scratch, resolved)) return;
  const segments = path.relative(scratch, resolved).split(path.sep).filter(Boolean);
  if (segments.length < 2) return;

  const rootStats = rejectLink(scratch, filePath, "input");
  if (rootStats === null || !rootStats.isDirectory()) {
    throw outputPathError("consumed input root must be a real directory", filePath);
  }
  const taskDir = path.join(scratch, segments[0]);
  const taskStats = rejectLink(taskDir, filePath, "input");
  if (taskStats === null) return;
  if (!taskStats.isDirectory()) {
    throw outputPathError("scratch task path must be a directory", filePath);
  }
  if (
    expectedSnapshot?.rootIdentity
    && !sameArtifactInode(rootStats, expectedSnapshot.rootIdentity)
  ) {
    throw outputPathError("scratch root changed before cleanup", filePath);
  }
  if (
    expectedSnapshot?.taskDirectoryIdentity
    && !sameArtifactInode(taskStats, expectedSnapshot.taskDirectoryIdentity)
  ) {
    throw outputPathError("scratch task directory changed before cleanup", filePath);
  }
  fs.rmSync(taskDir, { recursive: true });
}

function prepareOutputPath(filePath) {
  const resolved = resolveOutputPath(filePath);
  const configured = String(process.env.BOX_AGENT_OUTPUT_DIR || "").trim();
  const parent = path.dirname(resolved);

  if (!configured) {
    fs.mkdirSync(parent, { recursive: true });
    rejectLink(resolved, filePath);
    return resolved;
  }

  const root = artifactRoot();
  fs.mkdirSync(root, { recursive: true });
  const realRoot = fs.realpathSync(root);
  const relativeParent = path.relative(root, parent);
  let current = root;
  for (const segment of relativeParent.split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    let stats = rejectLink(current, filePath);
    if (stats === null) {
      try {
        fs.mkdirSync(current);
      } catch (error) {
        if (error?.code !== "EEXIST") throw error;
      }
      stats = rejectLink(current, filePath);
    }
    if (!stats?.isDirectory()) {
      throw outputPathError("output parent path must be a directory", filePath);
    }
  }
  if (!isWithin(realRoot, fs.realpathSync(parent))) {
    throw outputPathError(
      "output path resolves outside BOX_AGENT_OUTPUT_DIR",
      filePath,
    );
  }
  const targetStats = rejectLink(resolved, filePath);
  if (targetStats !== null && !targetStats.isFile()) {
    throw outputPathError("output target must be a regular file", filePath);
  }
  return resolved;
}

function writeOutputFileSync(filePath, value, options = {}) {
  const resolved = prepareOutputPath(filePath);
  const exclusive = options.exclusive === true;
  if (exclusive && fs.existsSync(resolved)) {
    const error = new Error(`EEXIST: output already exists, open '${resolved}'`);
    error.code = "EEXIST";
    throw error;
  }

  const directory = path.dirname(resolved);
  const temporaryPath = path.join(
    directory,
    `.${path.basename(resolved)}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
  );
  let descriptor = null;
  let publishedIdentity = null;
  try {
    descriptor = fs.openSync(temporaryPath, "wx", 0o666);
    fs.writeFileSync(descriptor, value, { encoding: options.encoding || "utf8" });
    fs.fsyncSync(descriptor);
    publishedIdentity = artifactFileIdentity(fs.fstatSync(descriptor));
    fs.closeSync(descriptor);
    descriptor = null;

    // Re-check immediately before replacement so an existing target link is
    // never followed, even if it appeared after the initial validation.
    prepareOutputPath(resolved);
    const directorySnapshot = snapshotOutputDirectoryChain(resolved, filePath);
    assertOutputDirectoryChainUnchanged(directorySnapshot, filePath);
    if (exclusive && fs.existsSync(resolved)) {
      const error = new Error(`EEXIST: output already exists, rename '${resolved}'`);
      error.code = "EEXIST";
      throw error;
    }
    if (exclusive) {
      // link(2) publishes the fully written inode without replacing a path
      // another concurrent builder may have won since the last check.
      fs.linkSync(temporaryPath, resolved);
      fs.unlinkSync(temporaryPath);
    } else {
      fs.renameSync(temporaryPath, resolved);
    }
    try {
      assertOutputDirectoryChainUnchanged(directorySnapshot, filePath);
    } catch (boundaryError) {
      removePublishedFileOnBoundaryFailure(
        resolved,
        publishedIdentity,
        boundaryError,
      );
      throw boundaryError;
    }
  } catch (error) {
    if (descriptor !== null) fs.closeSync(descriptor);
    try {
      fs.unlinkSync(temporaryPath);
    } catch (cleanupError) {
      if (!isMissing(cleanupError)) throw cleanupError;
    }
    throw error;
  }
  return resolved;
}

function resolveOutputPath(filePath) {
  const root = artifactRoot();
  const resolved = path.isAbsolute(filePath)
    ? path.resolve(filePath)
    : path.resolve(root, filePath);
  const configured = String(process.env.BOX_AGENT_OUTPUT_DIR || "").trim();
  if (!configured) return resolved;
  if (!isWithin(root, resolved)) {
    throw new Error(`output path must stay within BOX_AGENT_OUTPUT_DIR: ${filePath}`);
  }
  fs.mkdirSync(root, { recursive: true });
  const realRoot = fs.realpathSync(root);
  let existingParent = path.dirname(resolved);
  while (!fs.existsSync(existingParent)) {
    const parent = path.dirname(existingParent);
    if (parent === existingParent) break;
    existingParent = parent;
  }
  if (!isWithin(realRoot, fs.realpathSync(existingParent))) {
    throw new Error(`output path resolves outside BOX_AGENT_OUTPUT_DIR: ${filePath}`);
  }
  return resolved;
}

module.exports = {
  artifactRoot,
  cleanupScratchTaskDirectorySync,
  consumeArtifactFileSync,
  consumeScratchInputFileSync,
  resolveArtifactPath,
  resolveOutputPath,
  snapshotConsumedInputFileSync,
  snapshotArtifactFileSync,
  writeOutputFileSync,
};
