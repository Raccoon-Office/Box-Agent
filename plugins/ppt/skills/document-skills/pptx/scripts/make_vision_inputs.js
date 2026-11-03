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
    "Usage: make_vision_inputs.js image_dir --out qa/vision_inputs [--max-width 960] [--format jpg|png] [--quality 0.82]"
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
  if (argv.length < 1) usage();
  const opts = {
    imageDir: argv[0],
    out: "qa/vision_inputs",
    maxWidth: 960,
    format: "jpg",
    quality: 0.82,
  };
  for (let i = 1; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = argv[i + 1];
    if (arg === "--out" && value) {
      opts.out = value;
      i += 1;
    } else if (arg === "--max-width" && value) {
      opts.maxWidth = Number(value);
      i += 1;
    } else if (arg === "--format" && value) {
      opts.format = value.toLowerCase();
      i += 1;
    } else if (arg === "--quality" && value) {
      opts.quality = Number(value);
      i += 1;
    } else {
      usage();
    }
  }
  if (!Number.isFinite(opts.maxWidth) || opts.maxWidth < 640) usage();
  if (!["png", "jpg"].includes(opts.format)) usage();
  if (!Number.isFinite(opts.quality) || opts.quality <= 0 || opts.quality > 1) usage();
  return opts;
}

function slideNumber(filePath) {
  const base = path.basename(filePath).toLowerCase();
  const match = base.match(/slide[-_]?0*(\d+)/) || base.match(/(\d+)/);
  return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER;
}

function collectImages(imageDir) {
  if (!fs.existsSync(imageDir)) {
    throw new Error(`Image directory not found: ${imageDir}`);
  }
  return fs
    .readdirSync(imageDir)
    .filter(name => /\.(png|jpe?g)$/i.test(name))
    .map(name => path.join(imageDir, name))
    .sort((a, b) => slideNumber(a) - slideNumber(b) || a.localeCompare(b));
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const { createCanvas, loadImage } = requireModule(
    "@napi-rs/canvas",
    'Install in Office Raccoon with: ${BOX_AGENT_NPM:-npm} install --prefix "$HOME/Library/Application Support/office-raccoon" @napi-rs/canvas'
  );
  const imageDir = path.resolve(opts.imageDir);
  const outDir = path.resolve(opts.out);
  const images = collectImages(imageDir);
  if (!images.length) {
    throw new Error(`No PNG/JPG images found in ${imageDir}`);
  }

  fs.mkdirSync(outDir, { recursive: true });
  const outputs = [];
  for (const imagePath of images) {
    const image = await loadImage(imagePath);
    const scale = image.width > opts.maxWidth ? opts.maxWidth / image.width : 1;
    const width = Math.max(1, Math.round(image.width * scale));
    const height = Math.max(1, Math.round(image.height * scale));
    const canvas = createCanvas(width, height);
    const ctx = canvas.getContext("2d");
    ctx.drawImage(image, 0, 0, width, height);

    const ext = opts.format === "jpg" ? "jpg" : "png";
    const outPath = path.join(outDir, `${path.basename(imagePath, path.extname(imagePath))}.${ext}`);
    const buffer =
      opts.format === "jpg"
        ? canvas.toBuffer("image/jpeg", opts.quality)
        : canvas.toBuffer("image/png");
    fs.writeFileSync(outPath, buffer);
    outputs.push({
      source: imagePath,
      output: outPath,
      sourceSize: `${image.width}x${image.height}`,
      outputSize: `${width}x${height}`,
      bytes: buffer.length,
    });
  }

  const manifestPath = path.join(outDir, "manifest.json");
  fs.writeFileSync(
    manifestPath,
    `${JSON.stringify({ imageDir, outDir, maxWidth: opts.maxWidth, format: opts.format, quality: opts.quality, outputs }, null, 2)}\n`
  );
  console.log(JSON.stringify({ visionInputs: outDir, manifest: manifestPath, count: outputs.length, outputs }, null, 2));
}

main().catch(error => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
