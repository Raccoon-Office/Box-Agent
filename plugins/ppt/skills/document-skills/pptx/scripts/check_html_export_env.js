#!/usr/bin/env node
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");
const {
  loadPlaywright,
  ensurePlaywrightBrowsersPath,
  officeRaccoonBrowserHostPath,
  resolveChromiumExecutablePath,
} = require("./playwright_host");

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

ensurePlaywrightBrowsersPath();

function addManagedNodePath(prefix) {
  const managedNodeModules = path.join(prefix, "node_modules");
  process.env.NODE_PATH = process.env.NODE_PATH
    ? `${managedNodeModules}${path.delimiter}${process.env.NODE_PATH}`
    : managedNodeModules;
  Module._initPaths();
  return managedNodeModules;
}

function checkPlaywright() {
  try {
    const playwright = loadPlaywright();
    return { ok: true, playwright };
  } catch (error) {
    if (error && error.code === "MODULE_NOT_FOUND") {
      return { ok: false, reason: "Missing npm package: playwright" };
    }
    return { ok: false, reason: error.message || String(error) };
  }
}

function checkChromium(playwright) {
  const resolved = resolveChromiumExecutablePath(playwright);
  if (resolved.ok) {
    return { ok: true, path: resolved.path, source: resolved.source };
  }
  return {
    ok: false,
    reason: resolved.expectedPath
      ? `Chromium executable not found at ${resolved.expectedPath}`
      : `Chromium executable not found under ${resolved.browsersPath}`,
  };
}

function main() {
  const prefix = officeRaccoonPrefix();
  const managedNodeModules = addManagedNodePath(prefix);
  const bundlePath = path.join(__dirname, "dom-to-pptx.bundle.js");

  const bundleOk = fs.existsSync(bundlePath);
  const playwrightResult = checkPlaywright();
  const chromiumResult = playwrightResult.ok
    ? checkChromium(playwrightResult.playwright)
    : { ok: false, reason: "Skipped because Playwright is missing" };

  console.log("HTML editable PPTX export environment:");
  console.log(`  ${bundleOk ? "ok  " : "miss"} bundled converter -> ${bundlePath}`);
  console.log(`  ${playwrightResult.ok ? "ok  " : "miss"} playwright (${playwrightResult.reason || "available"})`);
  console.log(`  ${chromiumResult.ok ? "ok  " : "miss"} playwright chromium${chromiumResult.path ? ` -> ${chromiumResult.path}${chromiumResult.source ? ` (${chromiumResult.source})` : ""}` : ` (${chromiumResult.reason})`}`);
  console.log(`  info managed node_modules -> ${managedNodeModules}`);

  if (bundleOk && playwrightResult.ok && chromiumResult.ok) {
    console.log("\nResult: OK. Continue with HTML-first editable export.");
    return 0;
  }

  console.log("\nResult: BLOCKED for CLI HTML-to-editable-PPTX export.");
  if (!bundleOk) {
    console.log("Missing bundled converter. This skill install is incomplete; do not switch generators silently.");
  } else {
    const browsersPath = process.env.PLAYWRIGHT_BROWSERS_PATH || officeRaccoonBrowserHostPath();
    console.log("Missing browser export environment. Ask the user to choose:");
    console.log("  HTML: deliver deck.html now; editable PPTX export can run after Playwright/Chromium or a host renderer is available.");
    console.log("  PPTX: switch to native PptxGenJS and create a directly editable PPTX with different HTML/CSS fidelity tradeoffs.");
    console.log("Install in Office Raccoon: Settings -> Plugins -> Web automation (Playwright) -> Download Chromium and enable.");
    console.log(`Expected browser host under: ${browsersPath}`);
    console.log("If Playwright itself is missing, reinstall or repair Office Raccoon's managed runtime.");
  }
  return 1;
}

process.exit(main());
