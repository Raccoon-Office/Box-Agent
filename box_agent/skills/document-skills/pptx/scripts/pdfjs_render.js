#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

function parseArgs(argv) {
  const args = {
    pdf: null,
    out: null,
    format: "png",
    dpi: 160,
  };

  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--out") {
      args.out = argv[++i];
    } else if (arg === "--format") {
      args.format = argv[++i];
    } else if (arg === "--dpi") {
      args.dpi = Number(argv[++i]);
    } else if (!args.pdf) {
      args.pdf = arg;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (!args.pdf || !args.out) {
    throw new Error("Usage: pdfjs_render.js input.pdf --out rendered [--format png] [--dpi 160]");
  }
  if (args.format !== "png") {
    throw new Error("pdf.js fallback currently supports PNG output only.");
  }
  if (!Number.isFinite(args.dpi) || args.dpi <= 0) {
    throw new Error("--dpi must be a positive number.");
  }

  return args;
}

async function loadPdfJs() {
  try {
    return await import("pdfjs-dist/legacy/build/pdf.mjs");
  } catch {
    return await import("pdfjs-dist/build/pdf.mjs");
  }
}

async function loadCanvas() {
  try {
    return require("@napi-rs/canvas");
  } catch {
    return require("canvas");
  }
}

async function main() {
  const args = parseArgs(process.argv);
  fs.mkdirSync(args.out, { recursive: true });

  const pdfjsLib = await loadPdfJs();
  const { createCanvas } = await loadCanvas();
  const data = new Uint8Array(fs.readFileSync(args.pdf));

  const loadingTask = pdfjsLib.getDocument({
    data,
    disableFontFace: true,
    useSystemFonts: true,
  });
  const pdf = await loadingTask.promise;
  const scale = args.dpi / 72;
  const written = [];

  for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
    const page = await pdf.getPage(pageNumber);
    const viewport = page.getViewport({ scale });
    const canvas = createCanvas(Math.ceil(viewport.width), Math.ceil(viewport.height));
    const context = canvas.getContext("2d");

    await page.render({
      canvasContext: context,
      viewport,
    }).promise;

    const output = path.join(args.out, `slide-${pageNumber}.png`);
    fs.writeFileSync(
      path.join(args.out, `.${path.basename(output)}.artifact.json`),
      '{"type":"intermediate_asset"}\n'
    );
    fs.writeFileSync(output, canvas.toBuffer("image/png"));
    written.push(output);
  }

  console.log(`Rendered slides: ${written.length}`);
  for (const file of written) {
    console.log(file);
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
