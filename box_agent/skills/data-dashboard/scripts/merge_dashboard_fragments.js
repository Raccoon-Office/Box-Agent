#!/usr/bin/env node
/*
 * Assemble independently authored dashboard pages into the skill's single-file
 * template. This is intentionally deterministic: no model reads or rewrites
 * the combined HTML after the page fragments are complete.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PAGE_ID = /^P[1-9]\d*$/;
const FORBIDDEN_HTML = /<\/?\s*(?:html|head|body|style|script|link|meta|title|base|iframe|object|embed)\b/i;
const FORBIDDEN_CSS = /(?:^|[,{])\s*(?::root|html|body|\.sidebar|\.main)\b|@(?:import|keyframes)\b/i;

function usage(exitCode = 2) {
  console.error(
    "Usage: merge_dashboard_fragments.js --template assets/template.html --contract drafts/contract.json --out dashboard.html fragment-P1.json [fragment-P2.json ...]",
  );
  process.exit(exitCode);
}

function parseArgs(argv) {
  const options = { template: null, contract: null, out: null, fragments: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--template" && value) {
      options.template = value;
      index += 1;
    } else if (arg === "--contract" && value) {
      options.contract = value;
      index += 1;
    } else if (arg === "--out" && value) {
      options.out = value;
      index += 1;
    } else if (arg === "--help" || arg === "-h") {
      usage(0);
    } else if (arg.startsWith("--")) {
      usage();
    } else {
      options.fragments.push(arg);
    }
  }
  if (!options.template || !options.contract || !options.out || options.fragments.length === 0) {
    usage();
  }
  return options;
}

function readText(filePath) {
  if (!fs.existsSync(filePath)) throw new Error(`File not found: ${filePath}`);
  return fs.readFileSync(filePath, "utf8");
}

function readJson(filePath) {
  try {
    return JSON.parse(readText(filePath));
  } catch (error) {
    throw new Error(`${filePath}: invalid JSON (${error.message})`);
  }
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requireString(value, label) {
  if (typeof value !== "string" || !value.trim()) throw new Error(`${label} must be a non-empty string`);
  return value.trim();
}

function validateJavaScript(source, label) {
  try {
    // Compile only: generated renderers must never run during assembly.
    new vm.Script(source, { filename: label });
  } catch (error) {
    throw new Error(`${label}: invalid JavaScript syntax (${error.message})`);
  }
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function jsonForScript(value) {
  return JSON.stringify(value, null, 2)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

function replaceSingle(text, marker, replacement) {
  const occurrences = text.split(marker).length - 1;
  if (occurrences !== 1) throw new Error(`Template must contain exactly one ${marker} marker`);
  return text.replace(marker, replacement);
}

function replaceBetween(text, startMarker, endMarker, replacement) {
  const start = text.indexOf(startMarker);
  const end = text.indexOf(endMarker);
  if (start < 0 || end < 0 || end <= start) {
    throw new Error(`Template is missing ordered markers ${startMarker} / ${endMarker}`);
  }
  return `${text.slice(0, start + startMarker.length)}\n${replacement}\n${text.slice(end)}`;
}

function validateContract(raw, filePath) {
  if (!isPlainObject(raw)) throw new Error(`${filePath}: contract must be an object`);
  const title = requireString(raw.title, `${filePath}.title`);
  const brand = typeof raw.brand === "string" ? raw.brand.trim() : "";
  const note = typeof raw.note === "string" ? raw.note.trim() : "";
  if (!Array.isArray(raw.pages) || raw.pages.length === 0) {
    throw new Error(`${filePath}.pages must be a non-empty array`);
  }

  const ids = new Set();
  const pages = raw.pages.map((page, index) => {
    if (!isPlainObject(page)) throw new Error(`${filePath}.pages[${index}] must be an object`);
    const id = requireString(page.id, `${filePath}.pages[${index}].id`);
    if (!PAGE_ID.test(id)) throw new Error(`${filePath}.pages[${index}].id must match ${PAGE_ID}`);
    if (ids.has(id)) throw new Error(`${filePath}.pages contains duplicate id ${id}`);
    ids.add(id);
    return {
      id,
      label: requireString(page.label, `${filePath}.pages[${index}].label`),
      group: typeof page.group === "string" ? page.group.trim() : "",
    };
  });

  const initialPage = raw.initialPage == null ? pages[0].id : requireString(raw.initialPage, `${filePath}.initialPage`);
  if (!ids.has(initialPage)) throw new Error(`${filePath}.initialPage must be listed in pages`);
  return { title, brand, note, pages, initialPage };
}

function validateFragment(raw, filePath, pageIds) {
  if (!isPlainObject(raw)) throw new Error(`${filePath}: fragment must be an object`);
  const pageId = requireString(raw.pageId, `${filePath}.pageId`);
  if (!pageIds.has(pageId)) throw new Error(`${filePath}: pageId ${pageId} is not declared by the contract`);
  if (!isPlainObject(raw.data)) throw new Error(`${filePath}.data must be an object`);
  const html = requireString(raw.html, `${filePath}.html`);
  const renderer = requireString(raw.renderer, `${filePath}.renderer`);
  const css = raw.css == null ? "" : requireString(raw.css, `${filePath}.css`);

  if (FORBIDDEN_HTML.test(html)) throw new Error(`${filePath}.html contains a forbidden document-level tag`);
  const root = new RegExp(
    `^\\s*<section\\b(?=[^>]*\\bclass=["'][^"']*\\bpage\\b[^"']*["'])(?=[^>]*\\bid=["']page-${pageId}["'])[^>]*>[\\s\\S]*</section>\\s*$`,
    "i",
  );
  if (!root.test(html)) {
    throw new Error(`${filePath}.html must be exactly one <section class="page" id="page-${pageId}">`);
  }
  const sectionOpenings = html.match(/<section\b/gi) || [];
  const sectionClosings = html.match(/<\/section\s*>/gi) || [];
  if (sectionOpenings.length !== 1 || sectionClosings.length !== 1) {
    throw new Error(`${filePath}.html must contain exactly one page section`);
  }
  const ids = new Set();
  for (const match of html.matchAll(/\bid\s*=\s*["']([^"']+)["']/gi)) {
    const id = match[1];
    if (ids.has(id)) throw new Error(`${filePath}.html contains duplicate id "${id}"`);
    ids.add(id);
    if (id !== `page-${pageId}` && !id.startsWith(`${pageId}-`)) {
      throw new Error(`${filePath}.html id "${id}" must be prefixed with "${pageId}-"`);
    }
  }
  if (!/^(?:function\b|(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>)/.test(renderer)) {
    throw new Error(`${filePath}.renderer must be a function expression, without the page key`);
  }
  if (/<\/script/i.test(renderer)
      || /(?:\bconst\s+(?:D|renderers)\b|\b(?:D|renderers)(?:\.[\w$]+|\[[^\]]+\])?\s*=)/.test(renderer)) {
    throw new Error(`${filePath}.renderer must not declare shared dashboard state`);
  }
  validateJavaScript(`(${renderer}\n)`, `${filePath}.renderer`);
  if (css && (FORBIDDEN_CSS.test(css) || !css.startsWith(`#page-${pageId}`))) {
    throw new Error(`${filePath}.css must start with #page-${pageId} and cannot change shared selectors`);
  }
  return { pageId, data: raw.data, html, renderer, css };
}

function buildNav(contract) {
  let previousGroup = null;
  return contract.pages.map(page => {
    const group = page.group && page.group !== previousGroup
      ? `<div class="nav-group">${escapeHtml(page.group)}</div>\n    `
      : "";
    previousGroup = page.group;
    const active = page.id === contract.initialPage ? " active" : "";
    return `${group}<a href="#" data-page="${page.id}" class="${active.trim()}">${escapeHtml(page.label)}</a>`;
  }).join("\n    ");
}

function merge(template, contract, fragments) {
  const byPage = new Map(fragments.map(fragment => [fragment.pageId, fragment]));
  const ordered = contract.pages.map(page => byPage.get(page.id));
  if (ordered.some(fragment => !fragment)) {
    const missing = contract.pages.filter(page => !byPage.has(page.id)).map(page => page.id);
    throw new Error(`Missing fragments for contract pages: ${missing.join(", ")}`);
  }

  let output = template;
  output = replaceSingle(output, "<!-- FILL: 报告标题 -->", escapeHtml(contract.title));
  output = replaceSingle(output, "<!-- DASHBOARD_FRAGMENT:BRAND -->", escapeHtml(contract.brand));
  output = replaceSingle(output, "/* DASHBOARD_FRAGMENT:EXTRA_CSS */", ordered.map(fragment => fragment.css).filter(Boolean).join("\n\n"));
  output = replaceBetween(output, "<!-- DASHBOARD_FRAGMENT:NAV:START -->", "<!-- DASHBOARD_FRAGMENT:NAV:END -->", buildNav(contract));
  output = replaceBetween(output, "<!-- DASHBOARD_FRAGMENT:PAGES:START -->", "<!-- DASHBOARD_FRAGMENT:PAGES:END -->", ordered.map(fragment => fragment.html).join("\n\n"));
  output = replaceSingle(output, "<!-- DASHBOARD_FRAGMENT:NOTE -->", contract.note ? `<div class="note">${escapeHtml(contract.note)}</div>` : "");
  output = replaceBetween(
    output,
    "/* DASHBOARD_FRAGMENT:DATA:START */",
    "/* DASHBOARD_FRAGMENT:DATA:END */",
    ordered.map(fragment => `  ${fragment.pageId}: ${jsonForScript(fragment.data)},`).join("\n"),
  );
  output = replaceBetween(
    output,
    "/* DASHBOARD_FRAGMENT:RENDERERS:START */",
    "/* DASHBOARD_FRAGMENT:RENDERERS:END */",
    ordered.map(fragment => `  ${fragment.pageId}: ${fragment.renderer},`).join("\n"),
  );
  return replaceSingle(output, "/* DASHBOARD_FRAGMENT:INITIAL_PAGE */", `showPage(${jsonForScript(contract.initialPage)});`)
    .replace("showPage('P1');", "");
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  const contract = validateContract(readJson(options.contract), options.contract);
  const pageIds = new Set(contract.pages.map(page => page.id));
  const fragments = options.fragments.map(filePath => validateFragment(readJson(filePath), filePath, pageIds));
  const fragmentIds = new Set();
  for (const fragment of fragments) {
    if (fragmentIds.has(fragment.pageId)) throw new Error(`Duplicate fragment for ${fragment.pageId}`);
    fragmentIds.add(fragment.pageId);
  }
  if (fragments.length !== contract.pages.length) {
    throw new Error(`Expected ${contract.pages.length} fragments, received ${fragments.length}`);
  }
  const output = merge(readText(options.template), contract, fragments);
  for (const [index, match] of Array.from(output.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi)).entries()) {
    validateJavaScript(match[1], `${options.out}.script-${index + 1}`);
  }
  fs.mkdirSync(path.dirname(path.resolve(options.out)), { recursive: true });
  fs.writeFileSync(options.out, output, "utf8");
  console.log(`Merged ${fragments.length} dashboard page(s) into ${options.out}`);
}

try {
  main();
} catch (error) {
  console.error(error && error.message ? error.message : String(error));
  process.exit(1);
}
