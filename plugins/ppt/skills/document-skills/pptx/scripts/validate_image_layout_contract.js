#!/usr/bin/env node
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");
const { chromiumLaunchOptions, loadPlaywright } = require("./playwright_host");
const { resolveArtifactPath } = require("./deck_spec_core.js");

function officeRaccoonPrefix() {
  if (process.env.BOX_AGENT_NODE_PREFIX) return process.env.BOX_AGENT_NODE_PREFIX;
  if (process.env.BOX_AGENT_RUNTIME_PREFIX) return process.env.BOX_AGENT_RUNTIME_PREFIX;
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
  console.error("Usage: validate_image_layout_contract.js deck.html assets/generated/manifest.json [--tolerance 24] [--report qa/image_layout_contract.json]");
  process.exit(2);
}

function parseArgs(argv) {
  if (argv.length < 2) usage();
  const opts = {
    html: argv[0],
    manifest: argv[1],
    tolerance: 24,
    report: null,
  };
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = argv[i + 1];
    if (arg === "--tolerance" && value) {
      opts.tolerance = Number(value);
      i += 1;
    } else if (arg === "--report" && value) {
      opts.report = value;
      i += 1;
    } else {
      usage();
    }
  }
  if (!Number.isFinite(opts.tolerance) || opts.tolerance < 0) usage();
  return opts;
}

function requireModule(name) {
  try {
    return name === "playwright" ? loadPlaywright() : require(name);
  } catch (error) {
    if (error && error.code === "MODULE_NOT_FOUND") {
      console.error(`Missing dependency: ${name}`);
      console.error("Use scripts/check_html_export_env.js to verify the Office Raccoon managed Node prefix.");
      process.exit(1);
    }
    throw error;
  }
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function imagePlanFromManifest(manifest) {
  if (Array.isArray(manifest)) return manifest;
  if (Array.isArray(manifest.image_plan)) return manifest.image_plan;
  return [];
}

function isFullSlideGeneratedBackground(item) {
  if (!item || item.decision !== "generate") return false;
  const text = `${item.kind || ""} ${item.placement || ""} ${item.purpose || ""}`.toLowerCase();
  return /\b(background|full-bleed|full bleed|full-slide|full slide|entire slide|page background|slide background)\b/.test(text);
}

function overlaps(a, b) {
  return (
    a.x < b.x + b.width &&
    a.x + a.width > b.x &&
    a.y < b.y + b.height &&
    a.y + a.height > b.y
  );
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const htmlPath = resolveArtifactPath(opts.html);
  const manifestPath = resolveArtifactPath(opts.manifest);
  const manifest = readJson(manifestPath);
  const imagePlan = imagePlanFromManifest(manifest);
  const issues = [];
  const warnings = [];

  if (!imagePlan.length) {
    issues.push("manifest.image_plan is missing or empty.");
  }

  const contractItems = [];
  for (const item of imagePlan) {
    const slide = String(item.slide || "").padStart(2, "0");
    const contract = item.layout_contract;
    if (isFullSlideGeneratedBackground(item) && !contract) {
      issues.push(`slide-${slide}: generated full-slide background is missing layout_contract.`);
      continue;
    }
    if (!contract) continue;
    if (!contract.slide_size || contract.slide_size.width !== 1920 || contract.slide_size.height !== 1080) {
      issues.push(`slide-${slide}: layout_contract.slide_size must be 1920x1080.`);
    }
    if (!Array.isArray(contract.text_regions) || contract.text_regions.length === 0) {
      issues.push(`slide-${slide}: layout_contract.text_regions is missing or empty.`);
    }
    if (Array.isArray(contract.text_regions) && Array.isArray(contract.visual_focus_regions)) {
      for (const textRegion of contract.text_regions) {
        for (const visualRegion of contract.visual_focus_regions) {
          if (overlaps(textRegion, visualRegion)) {
            issues.push(`slide-${slide}: text region "${textRegion.name}" overlaps visual focus region "${visualRegion.name}".`);
          }
        }
      }
    }
    contractItems.push({ slide, contract });
  }

  if (contractItems.length) {
    const { chromium } = requireModule("playwright");
    const launch = chromiumLaunchOptions(chromium, { headless: true });
    const browser = await chromium.launch(launch.options);
    try {
      const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
      await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });

      const domReport = await page.evaluate(({ contractItems, tolerance }) => {
        const issues = [];
        const warnings = [];
        const delta = (a, b) => Math.abs(Math.round(a) - Math.round(b));
        const selectorForSlide = slide => `.slide[data-slide="${slide}"], .slide[data-slide="${Number(slide)}"]`;
        const regionSelector = name => `[data-layout-region="${CSS.escape(name)}"]`;

        for (const { slide, contract } of contractItems) {
          const slideEl = document.querySelector(selectorForSlide(slide));
          if (!slideEl) {
            issues.push(`slide-${slide}: matching .slide[data-slide] not found.`);
            continue;
          }
          const slideRect = slideEl.getBoundingClientRect();
          for (const region of contract.text_regions || []) {
            const name = String(region.name || "");
            if (!name) {
              issues.push(`slide-${slide}: text region is missing name.`);
              continue;
            }
            const el = slideEl.querySelector(regionSelector(name));
            if (!el) {
              issues.push(`slide-${slide}: missing element with data-layout-region="${name}".`);
              continue;
            }
            const rect = el.getBoundingClientRect();
            const actual = {
              x: rect.left - slideRect.left,
              y: rect.top - slideRect.top,
              width: rect.width,
              height: rect.height,
            };
            const mismatches = [];
            for (const key of ["x", "y", "width", "height"]) {
              if (delta(actual[key], region[key]) > tolerance) {
                mismatches.push(`${key} expected ${region[key]}, actual ${Math.round(actual[key])}`);
              }
            }
            if (mismatches.length) {
              issues.push(`slide-${slide} region "${name}": ${mismatches.join("; ")}.`);
            }
          }
        }
        return { issues, warnings };
      }, { contractItems, tolerance: opts.tolerance });

      issues.push(...domReport.issues);
      warnings.push(...domReport.warnings);
    } finally {
      await browser.close();
    }
  }

  const report = {
    ok: issues.length === 0,
    html: htmlPath,
    manifest: manifestPath,
    tolerance: opts.tolerance,
    checkedContracts: contractItems.length,
    issues,
    warnings,
  };

  if (opts.report) {
    const reportPath = resolveArtifactPath(opts.report);
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  }

  if (!report.ok) {
    console.error(JSON.stringify(report, null, 2));
    process.exit(1);
  }
  console.log(JSON.stringify(report, null, 2));
}

main().catch(error => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
