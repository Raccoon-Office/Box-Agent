#!/usr/bin/env node
// Capture one permitted page as observed DOM text and state, without an LLM.
const fs = require("node:fs/promises");
const path = require("node:path");

function validateUrl(value) {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
    throw new Error("Use an authorized HTTP(S) URL without embedded credentials.");
  }
  return url.href;
}

async function capturePage(page, requestedUrl) {
  const url = validateUrl(requestedUrl);
  const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
  if (!response || response.status() >= 400) {
    throw new Error(`Page read failed (HTTP ${response ? response.status() : "unknown"}).`);
  }
  validateUrl(response.url());
  await page.locator("body").waitFor({ state: "visible", timeout: 10000 });
  let networkIdle = true;
  try {
    await page.waitForLoadState("networkidle", { timeout: 5000 });
  } catch {
    networkIdle = false;
  }
  const observed = await page.evaluate(() => {
    const visible = el => el.getClientRects().length > 0
      && getComputedStyle(el).visibility !== "hidden";
    const label = el => {
      const labelledBy = (el.getAttribute("aria-labelledby") || "").split(/\s+/)
        .map(id => document.getElementById(id)?.textContent || "").join(" ").trim();
      return labelledBy || el.getAttribute("aria-label")
        || Array.from(el.labels || []).map(item => item.innerText).join(" ").trim()
        || el.innerText?.trim() || "";
    };
    const controls = Array.from(document.querySelectorAll(
      'input[type="radio"],input[type="checkbox"],[role="radio"],[role="checkbox"],'
      + '[role="switch"],select,[aria-pressed],[aria-selected]'
    )).filter(el => visible(el) || Array.from(el.labels || []).some(visible)).map(el => ({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute("role") || el.getAttribute("type") || el.tagName.toLowerCase(),
      label: label(el),
      checked: el.matches('input[type="radio"],input[type="checkbox"]')
        ? String(el.checked) : el.getAttribute("aria-checked"),
      pressed: el.getAttribute("aria-pressed"),
      selected: el.getAttribute("aria-selected"),
      selected_options: el.tagName === "SELECT"
        ? Array.from(el.selectedOptions).map(option => option.textContent.trim()) : [],
    }));
    const links = Array.from(document.querySelectorAll("a[href]"))
      .filter(visible).map(el => ({ text: el.innerText.trim(), url: el.href }))
      .filter(link => {
        try {
          const target = new URL(link.url);
          return ["http:", "https:"].includes(target.protocol) && !target.username && !target.password;
        } catch {
          return false;
        }
      });
    const text = document.body.innerText;
    return {
      document_url: location.href,
      title: document.title,
      language: document.documentElement.lang,
      text: text.slice(0, 200000),
      text_truncated: text.length > 200000,
      controls: controls.slice(0, 500),
      controls_truncated: controls.length > 500,
      links: links.slice(0, 500),
      links_truncated: links.length > 500,
    };
  });
  const { document_url: documentUrl, ...snapshot } = observed;
  return {
    schema_version: 1,
    reader: "managed_browser_dom",
    evidence_basis: "page_body",
    requested_url: url,
    final_url: validateUrl(documentUrl),
    captured_at: new Date().toISOString(),
    navigation_url: response.url(),
    navigation_status: response.status(),
    network_idle: networkIdle,
    ...snapshot,
  };
}

async function main(args) {
  if (args.length !== 3 || args[1] !== "--out") {
    throw new Error("Usage: capture_source.js <URL> --out <new-capture.json>");
  }
  const url = validateUrl(args[0]);
  const output = path.resolve(args[2]);
  try {
    await fs.access(output);
    throw new Error("Capture already exists; choose a new path to preserve the observation.");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const modulePath = process.env.BOX_AGENT_PLAYWRIGHT_MODULE_PATH;
  const executablePath = process.env.BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  if (!modulePath || !executablePath) {
    throw new Error("Host Playwright runtime is unavailable. Use an authorized raw reader/browser; do not install another runtime.");
  }
  const { chromium } = require(modulePath);
  const browser = await chromium.launch({ executablePath, headless: true });
  try {
    const page = await browser.newPage();
    const capture = await capturePage(page, url);
    await fs.mkdir(path.dirname(output), { recursive: true });
    await fs.writeFile(output, JSON.stringify(capture, null, 2) + "\n", { flag: "wx" });
    process.stdout.write(JSON.stringify({
      output, final_url: capture.final_url, evidence_basis: capture.evidence_basis,
      text_chars: capture.text.length, text_truncated: capture.text_truncated,
      controls: capture.controls.length, network_idle: capture.network_idle,
    }) + "\n");
  } finally {
    await browser.close();
  }
}

module.exports = { validateUrl, capturePage, main };
if (require.main === module) {
  main(process.argv.slice(2)).catch(error => {
    process.stderr.write(error.message + "\n");
    process.exitCode = 1;
  });
}
