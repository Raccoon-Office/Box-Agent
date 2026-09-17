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
    "Usage: make_contact_sheet.js image_dir --out qa/vision-contact-sheet.png [--cols 2] [--thumb-width 640]"
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
    out: "qa/vision-contact-sheet.png",
    cols: 2,
    thumbWidth: 640,
  };
  for (let i = 1; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = argv[i + 1];
    if (arg === "--publish-artifact") {
      opts.publishArtifact = true;
    } else if (arg === "--out" && value) {
      opts.out = value;
      i += 1;
    } else if (arg === "--cols" && value) {
      opts.cols = Number(value);
      i += 1;
    } else if (arg === "--thumb-width" && value) {
      opts.thumbWidth = Number(value);
      i += 1;
    } else {
      usage();
    }
  }
  if (!Number.isFinite(opts.cols) || opts.cols < 1) usage();
  if (!Number.isFinite(opts.thumbWidth) || opts.thumbWidth < 160) usage();
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
    .filter(name => /\.png$/i.test(name))
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
  const outPath = path.resolve(opts.out);
  const images = collectImages(imageDir);
  if (!images.length) {
    throw new Error(`No PNG images found in ${imageDir}`);
  }

  const loaded = [];
  for (const imagePath of images) {
    const image = await loadImage(imagePath);
    loaded.push({ imagePath, image });
  }

  const labelHeight = 34;
  const gap = 18;
  const margin = 24;
  const thumbWidth = Math.round(opts.thumbWidth);
  const firstAspect = loaded[0].image.height / loaded[0].image.width;
  const thumbHeight = Math.round(thumbWidth * firstAspect);
  const cols = Math.min(Math.round(opts.cols), loaded.length);
  const rows = Math.ceil(loaded.length / cols);
  const canvasWidth = margin * 2 + cols * thumbWidth + (cols - 1) * gap;
  const canvasHeight = margin * 2 + rows * (thumbHeight + labelHeight) + (rows - 1) * gap;
  const canvas = createCanvas(canvasWidth, canvasHeight);
  const ctx = canvas.getContext("2d");

  ctx.fillStyle = "#f4f4f5";
  ctx.fillRect(0, 0, canvasWidth, canvasHeight);
  ctx.font = "18px Arial";
  ctx.textBaseline = "middle";

  loaded.forEach(({ imagePath, image }, index) => {
    const row = Math.floor(index / cols);
    const col = index % cols;
    const x = margin + col * (thumbWidth + gap);
    const y = margin + row * (thumbHeight + labelHeight + gap);
    const label = path.basename(imagePath);

    ctx.fillStyle = "#ffffff";
    ctx.fillRect(x, y, thumbWidth, thumbHeight + labelHeight);
    ctx.strokeStyle = "#d4d4d8";
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, thumbWidth, thumbHeight + labelHeight);
    ctx.drawImage(image, x, y, thumbWidth, thumbHeight);
    ctx.fillStyle = "#18181b";
    ctx.fillText(label, x + 12, y + thumbHeight + labelHeight / 2);
  });

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, canvas.toBuffer("image/png"));
  const promptPath = path.join(path.dirname(outPath), "vision-review-prompt.txt");
  fs.writeFileSync(
    promptPath,
    [
      "Review this slide contact sheet for a PPT QA pass.",
      "Visual review is optional. If this model cannot read images, return UNVERIFIED and continue delivery; do not retry, switch models or repair the environment.",
      "For each slide, return PASS or ISSUE.",
      "Check whether charts/bars are visible, text is readable, no content is clipped or overlapping, images are present, contrast is acceptable, and the slide is not blank.",
      "Mention the slide number for each issue.",
      "If any slide is too small to inspect in the contact sheet, inspect the corresponding individual slide PNG before giving PASS.",
      "",
      `Contact sheet: ${outPath}`,
      "Individual slide images:",
      ...images.map(imagePath => `- ${imagePath}`),
      "",
    ].join("\n")
  );
  if (opts.publishArtifact) require("./artifact_delivery.js").publishArtifact(outPath);
  console.log(
    JSON.stringify(
      {
        contactSheet: outPath,
        prompt: promptPath,
        imageDir,
        images,
        count: images.length,
        visualInspectionStatus: "UNVERIFIED: contact sheet created; optional visual review must not block delivery",
      },
      null,
      2
    )
  );
}

main().catch(error => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
