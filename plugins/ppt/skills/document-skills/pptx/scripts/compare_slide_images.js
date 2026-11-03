#!/usr/bin/env node
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");

function officeRaccoonPrefix() {
  if (process.env.BOX_AGENT_NODE_PREFIX) return process.env.BOX_AGENT_NODE_PREFIX;
  if (process.env.BOX_AGENT_RUNTIME_PREFIX) return process.env.BOX_AGENT_RUNTIME_PREFIX;
  // os.homedir() (HOME if set, else passwd lookup) — process.env.HOME is empty
  // in GUI/launchd/spawn contexts. Platform-branch so Windows/Linux resolve the
  // same managed prefix the export scripts use, instead of a macOS-only path.
  const home = os.homedir();
  if (process.platform === "darwin") {
    return path.join(home, "Library", "Application Support", "office-raccoon");
  }
  if (process.platform === "win32") {
    return path.join(process.env.APPDATA || home, "office-raccoon");
  }
  return path.join(home, ".config", "office-raccoon");
}

const managedNodeModules = path.join(officeRaccoonPrefix(), "node_modules");
process.env.NODE_PATH = process.env.NODE_PATH
  ? `${managedNodeModules}${path.delimiter}${process.env.NODE_PATH}`
  : managedNodeModules;
Module._initPaths();

function usage() {
  console.error(
    "Usage: compare_slide_images.js expected_dir actual_dir --out qa/diff [--threshold 0.08] [--max-diff 0.01]"
  );
  process.exit(2);
}

function requireModule(name, installHint) {
  try {
    return require(name);
  } catch (error) {
    if (error && error.code === "MODULE_NOT_FOUND") {
      console.error(`Missing dependency: ${name}`);
      console.error(installHint);
      process.exit(1);
    }
    throw error;
  }
}

function parseArgs(argv) {
  if (argv.length < 2) usage();
  const opts = {
    expectedDir: argv[0],
    actualDir: argv[1],
    out: "qa/diff",
    threshold: 0.08,
    maxDiff: 0.01,
  };
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = argv[i + 1];
    if (arg === "--out" && value) {
      opts.out = value;
      i += 1;
    } else if (arg === "--threshold" && value) {
      opts.threshold = Number(value);
      i += 1;
    } else if (arg === "--max-diff" && value) {
      opts.maxDiff = Number(value);
      i += 1;
    } else {
      usage();
    }
  }
  if (!Number.isFinite(opts.threshold) || !Number.isFinite(opts.maxDiff)) usage();
  return opts;
}

function slideNumber(filePath) {
  const base = path.basename(filePath).toLowerCase();
  const match = base.match(/slide[-_]?0*(\d+)/) || base.match(/(\d+)/);
  return match ? Number(match[1]) : null;
}

function collectSlides(dir) {
  if (!fs.existsSync(dir)) {
    throw new Error(`Directory not found: ${dir}`);
  }
  const slides = new Map();
  for (const name of fs.readdirSync(dir)) {
    if (!/\.png$/i.test(name)) continue;
    const filePath = path.join(dir, name);
    const number = slideNumber(filePath);
    if (number === null) continue;
    slides.set(number, filePath);
  }
  return slides;
}

function comparePngs(PNG, expectedPath, actualPath, diffPath, threshold) {
  const expected = PNG.sync.read(fs.readFileSync(expectedPath));
  const actual = PNG.sync.read(fs.readFileSync(actualPath));
  const width = Math.min(expected.width, actual.width);
  const height = Math.min(expected.height, actual.height);
  const dimensionsMatch = expected.width === actual.width && expected.height === actual.height;
  const diff = new PNG({ width, height });
  let diffPixels = 0;
  const channelThreshold = Math.max(0, Math.min(1, threshold)) * 255;

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const expectedIdx = (expected.width * y + x) << 2;
      const actualIdx = (actual.width * y + x) << 2;
      const diffIdx = (width * y + x) << 2;
      const dr = Math.abs(expected.data[expectedIdx] - actual.data[actualIdx]);
      const dg = Math.abs(expected.data[expectedIdx + 1] - actual.data[actualIdx + 1]);
      const db = Math.abs(expected.data[expectedIdx + 2] - actual.data[actualIdx + 2]);
      const da = Math.abs(expected.data[expectedIdx + 3] - actual.data[actualIdx + 3]);
      const changed = Math.max(dr, dg, db, da) > channelThreshold;
      if (changed) {
        diffPixels += 1;
        diff.data[diffIdx] = 255;
        diff.data[diffIdx + 1] = 48;
        diff.data[diffIdx + 2] = 48;
        diff.data[diffIdx + 3] = 255;
      } else {
        const gray =
          expected.data[expectedIdx] * 0.299 +
          expected.data[expectedIdx + 1] * 0.587 +
          expected.data[expectedIdx + 2] * 0.114;
        diff.data[diffIdx] = gray;
        diff.data[diffIdx + 1] = gray;
        diff.data[diffIdx + 2] = gray;
        diff.data[diffIdx + 3] = 80;
      }
    }
  }

  if (!dimensionsMatch) {
    diffPixels += Math.abs(expected.width * expected.height - actual.width * actual.height);
  }

  fs.writeFileSync(diffPath, PNG.sync.write(diff));
  const denominator = Math.max(expected.width * expected.height, actual.width * actual.height);
  return {
    expected: expectedPath,
    actual: actualPath,
    diff: diffPath,
    expectedSize: `${expected.width}x${expected.height}`,
    actualSize: `${actual.width}x${actual.height}`,
    dimensionsMatch,
    diffPixels,
    diffRatio: denominator > 0 ? diffPixels / denominator : 1,
  };
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const PNG = requireModule(
    "pngjs",
    'Install in Office Raccoon with: ${BOX_AGENT_NPM:-npm} install --prefix "$HOME/Library/Application Support/office-raccoon" pngjs'
  ).PNG;
  const expectedDir = path.resolve(opts.expectedDir);
  const actualDir = path.resolve(opts.actualDir);
  const outDir = path.resolve(opts.out);
  fs.mkdirSync(outDir, { recursive: true });

  const expectedSlides = collectSlides(expectedDir);
  const actualSlides = collectSlides(actualDir);
  const numbers = Array.from(new Set([...expectedSlides.keys(), ...actualSlides.keys()])).sort(
    (a, b) => a - b
  );
  const results = [];
  const issues = [];

  for (const number of numbers) {
    const expectedPath = expectedSlides.get(number);
    const actualPath = actualSlides.get(number);
    if (!expectedPath || !actualPath) {
      issues.push(
        `slide-${String(number).padStart(2, "0")}: missing ${expectedPath ? "actual" : "expected"} image`
      );
      continue;
    }
    const diffPath = path.join(outDir, `slide-${String(number).padStart(2, "0")}-diff.png`);
    const result = comparePngs(PNG, expectedPath, actualPath, diffPath, opts.threshold);
    results.push({ slide: number, ...result });
    if (!result.dimensionsMatch) {
      issues.push(
        `slide-${String(number).padStart(2, "0")}: size mismatch ${result.expectedSize} vs ${result.actualSize}`
      );
    }
    if (result.diffRatio > opts.maxDiff) {
      issues.push(
        `slide-${String(number).padStart(2, "0")}: diff ratio ${(result.diffRatio * 100).toFixed(2)}% exceeds ${(opts.maxDiff * 100).toFixed(2)}%`
      );
    }
  }

  const report = {
    ok: issues.length === 0,
    threshold: opts.threshold,
    maxDiff: opts.maxDiff,
    expectedDir,
    actualDir,
    outDir,
    comparedSlides: results.length,
    issues,
    results,
  };
  console.log(JSON.stringify(report, null, 2));
  if (!report.ok) {
    process.exit(1);
  }
}

try {
  main();
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
