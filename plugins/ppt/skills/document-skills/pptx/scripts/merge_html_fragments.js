#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

function usage(exitCode = 2) {
  console.error(
    [
      "Usage: merge_html_fragments.js --css drafts/common.css --out deck.html [--title Title] [--classes class1,class2] fragment.html ...",
      "",
      "Merges sub-agent-authored <section class=\"slide\"> fragments into one HTML deck.",
      "Fragments must not contain <html>, <head>, <body>, <style>, or <script>.",
    ].join("\n")
  );
  process.exit(exitCode);
}

function parseArgs(argv) {
  const opts = {
    css: null,
    out: "deck.html",
    title: "Deck",
    classes: null,
    fragments: [],
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = argv[i + 1];
    if (arg === "--css" && value) {
      opts.css = value;
      i += 1;
    } else if (arg === "--out" && value) {
      opts.out = value;
      i += 1;
    } else if (arg === "--title" && value) {
      opts.title = value;
      i += 1;
    } else if (arg === "--classes" && value) {
      opts.classes = new Set(value.split(",").map(item => item.trim()).filter(Boolean));
      i += 1;
    } else if (arg === "--help" || arg === "-h") {
      usage(0);
    } else if (arg.startsWith("--")) {
      usage();
    } else {
      opts.fragments.push(arg);
    }
  }
  if (!opts.css || opts.fragments.length === 0) usage();
  return opts;
}

function readFile(filePath) {
  if (!fs.existsSync(filePath)) {
    throw new Error(`File not found: ${filePath}`);
  }
  return fs.readFileSync(filePath, "utf8");
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function attrValue(attrs, name) {
  const pattern = new RegExp(`${name}\\s*=\\s*["']([^"']+)["']`, "i");
  const match = attrs.match(pattern);
  return match ? match[1] : "";
}

function classNames(attrs) {
  return attrValue(attrs, "class").split(/\s+/).filter(Boolean);
}

function allClassNames(html) {
  const names = new Set();
  const re = /\bclass\s*=\s*["']([^"']+)["']/gi;
  let match;
  while ((match = re.exec(html)) !== null) {
    for (const name of match[1].split(/\s+/).filter(Boolean)) {
      names.add(name);
    }
  }
  return [...names];
}

function validateFragmentText(filePath, text) {
  const forbidden = /<\/?\s*(html|head|body|style|script|link|meta|title|base|iframe|object|embed)\b/i;
  const match = text.match(forbidden);
  if (match) {
    throw new Error(`${filePath}: forbidden tag in fragment: ${match[0]}`);
  }
}

function extractSections(filePath, text, allowedClasses) {
  validateFragmentText(filePath, text);
  const sections = [];
  const re = /<section\b([^>]*)>([\s\S]*?)<\/section>/gi;
  let match;
  while ((match = re.exec(text)) !== null) {
    const attrs = match[1] || "";
    const html = match[0];
    const classes = classNames(attrs);
    if (!classes.includes("slide")) {
      throw new Error(`${filePath}: every section must include class="slide"`);
    }
    if (allowedClasses) {
      const unknown = allClassNames(html).filter(name => !allowedClasses.has(name));
      if (unknown.length > 0) {
        throw new Error(`${filePath}: unknown class(es): ${unknown.join(", ")}`);
      }
    }
    const rawSlide = attrValue(attrs, "data-slide");
    if (!/^\d+$/.test(rawSlide)) {
      throw new Error(`${filePath}: section is missing numeric data-slide`);
    }
    sections.push({ number: Number(rawSlide), html });
  }
  if (sections.length === 0) {
    throw new Error(`${filePath}: no <section class="slide"> fragments found`);
  }
  return sections;
}

function validateSlideOrder(sections) {
  const seen = new Map();
  for (const section of sections) {
    if (seen.has(section.number)) {
      throw new Error(`Duplicate data-slide="${String(section.number).padStart(2, "0")}"`);
    }
    seen.set(section.number, true);
  }
  sections.sort((a, b) => a.number - b.number);
  for (let i = 0; i < sections.length; i += 1) {
    const expected = i + 1;
    if (sections[i].number !== expected) {
      throw new Error(
        `Slides must be continuous from 01; expected ${String(expected).padStart(2, "0")}, got ${String(sections[i].number).padStart(2, "0")}`
      );
    }
  }
}

function buildDeck({ title, css, sections }) {
  return [
    "<!doctype html>",
    "<html>",
    "<head>",
    '  <meta charset="utf-8" />',
    `  <title>${escapeHtml(title)}</title>`,
    "  <style>",
    css.trim(),
    "  </style>",
    "</head>",
    "<body>",
    ...sections.map(section => section.html.trim()),
    "</body>",
    "</html>",
    "",
  ].join("\n");
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const css = readFile(opts.css);
  const sections = [];
  for (const fragmentPath of opts.fragments) {
    sections.push(...extractSections(fragmentPath, readFile(fragmentPath), opts.classes));
  }
  validateSlideOrder(sections);
  const deck = buildDeck({ title: opts.title, css, sections });
  fs.mkdirSync(path.dirname(path.resolve(opts.out)), { recursive: true });
  fs.writeFileSync(opts.out, deck, "utf8");
  console.log(`Merged ${sections.length} slide(s) into ${opts.out}`);
}

try {
  main();
} catch (error) {
  console.error(error && error.message ? error.message : String(error));
  process.exit(1);
}
