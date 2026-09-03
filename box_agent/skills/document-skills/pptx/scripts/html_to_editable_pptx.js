#!/usr/bin/env node
const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");
const { fileURLToPath, pathToFileURL } = require("url");
const {
  chromiumLaunchOptions,
  ensurePlaywrightBrowsersPath,
  officeRaccoonBrowserHostPath,
} = require("./playwright_host");
const {
  injectCaptureStyles,
  markDecorationNodes,
  captureSlideBackgrounds,
  applyDecorationFlatten,
} = require("./bg_capture");

function officeRaccoonPrefix() {
  if (process.env.BOX_AGENT_NODE_PREFIX) return process.env.BOX_AGENT_NODE_PREFIX;
  if (process.env.BOX_AGENT_RUNTIME_PREFIX) return process.env.BOX_AGENT_RUNTIME_PREFIX;
  // Use os.homedir() (HOME if set, else passwd lookup) — NOT process.env.HOME,
  // which is empty in GUI/launchd/spawn contexts where HOME is unset. Must match
  // check_html_export_env.js exactly, or the env preflight resolves playwright
  // while the real launch (different prefix) fails with "no playwright".
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

const managedNodeModules = path.join(officeRaccoonPrefix(), "node_modules");
process.env.NODE_PATH = process.env.NODE_PATH
  ? `${managedNodeModules}${path.delimiter}${process.env.NODE_PATH}`
  : managedNodeModules;
Module._initPaths();

// Single source of truth for the slide canvas contract — mirrors
// html_self_check.js. The PPTX always exports at LAYOUT_WIDE (13.333x7.5in,
// 16:9); a `.slide` authored at any other aspect ratio silently distorts on
// export, so we assert the canvas size here instead of auto-detecting it.
const CANONICAL_WIDTH = 1920;
const CANONICAL_HEIGHT = 1080;

function usage() {
  console.log(
    "Usage: html_to_editable_pptx.js deck.html output.pptx [--out slides] [--canvas WxH] [--svg-vector true|false] [--bg-capture always|never] [--allow-self-check-issues] (default: --svg-vector false for pixel fidelity, automatically true when data-pptx-diagram is present; --bg-capture always for visual fidelity; --allow-self-check-issues is a deprecated no-op)"
  );
  console.log(`  The canvas contract is ${CANONICAL_WIDTH}x${CANONICAL_HEIGHT} (16:9). Every .slide must match it exactly.`);
  console.log("  --canvas WxH overrides the contract for a deliberately non-standard deck (e.g. --canvas 1280x720).");
  console.log("  --width/--height are NOT accepted; set .slide CSS to the canvas size instead.");
}

function parseCanvas(value) {
  const m = /^(\d+)\s*[xX*]\s*(\d+)$/.exec(String(value || "").trim());
  if (!m) return null;
  const w = Number(m[1]);
  const h = Number(m[2]);
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return null;
  return { w, h };
}

function imageMimeType(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  if (ext === ".jpg" || ext === ".jpeg") return "image/jpeg";
  if (ext === ".png") return "image/png";
  if (ext === ".gif") return "image/gif";
  if (ext === ".webp") return "image/webp";
  if (ext === ".svg") return "image/svg+xml";
  if (ext === ".avif") return "image/avif";
  return "application/octet-stream";
}

function localImagePathFromSrc(src, htmlPath) {
  const value = String(src || "").trim();
  if (!value || /^(https?:\/\/|data:|blob:)/i.test(value) || /^\/\//.test(value)) {
    return null;
  }
  try {
    if (/^file:/i.test(value)) {
      return fileURLToPath(value);
    }
    if (/^[a-z][a-z0-9+.-]*:/i.test(value)) {
      return null;
    }
    const base = pathToFileURL(`${path.dirname(htmlPath)}${path.sep}`);
    return fileURLToPath(new URL(value, base));
  } catch {
    return null;
  }
}

function readLocalImageAsDataUrl(filePath) {
  if (!fs.existsSync(filePath)) {
    throw new Error(`Local image not found: ${filePath}`);
  }
  const bytes = fs.readFileSync(filePath);
  return `data:${imageMimeType(filePath)};base64,${bytes.toString("base64")}`;
}

async function inlineLocalImagesForExport(page, htmlPath) {
  const images = await page.evaluate(() =>
    Array.from(document.querySelectorAll("img")).map((img, index) => ({
      index,
      src: img.getAttribute("src") || "",
    }))
  );
  const replacements = images
    .map(image => {
      const filePath = localImagePathFromSrc(image.src, htmlPath);
      if (!filePath) return null;
      return {
        index: image.index,
        src: image.src,
        filePath,
        dataUrl: readLocalImageAsDataUrl(filePath),
      };
    })
    .filter(Boolean);
  if (!replacements.length) return [];

  await page.evaluate(items => {
    const imgs = Array.from(document.querySelectorAll("img"));
    items.forEach(item => {
      const img = imgs[item.index];
      if (!img) return;
      img.setAttribute("data-original-src", item.src);
      img.setAttribute("src", item.dataUrl);
      img.removeAttribute("srcset");
      img.removeAttribute("loading");
    });
  }, replacements);
  await page.waitForFunction(() =>
    Array.from(document.querySelectorAll("img")).every(img => img.complete && img.naturalWidth > 0)
  );
  return replacements.map(({ src, filePath }) => ({ src, filePath }));
}

function failUsage() {
  usage();
  process.exit(2);
}

function parseArgs(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    usage();
    process.exit(0);
  }
  if (argv.length < 2) failUsage();
  const opts = {
    html: argv[0],
    pptx: argv[1],
    out: "slides",
    width: CANONICAL_WIDTH,
    height: CANONICAL_HEIGHT,
    svgVector: false,
    bgCapture: "always",
  };
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = argv[i + 1];
    if (arg === "--out" && value) {
      opts.out = value;
      i += 1;
    } else if (arg === "--canvas" && value) {
      const canvas = parseCanvas(value);
      if (!canvas) {
        console.error(`Invalid --canvas value "${value}". Expected WxH, e.g. --canvas 1280x720.`);
        process.exit(2);
      }
      opts.width = canvas.w;
      opts.height = canvas.h;
      i += 1;
    } else if (arg === "--width" || arg === "--height") {
      console.error(
        `Refusing to export: ${arg} is not a valid flag for html_to_editable_pptx.js. ` +
        `The canvas contract is fixed at ${CANONICAL_WIDTH}x${CANONICAL_HEIGHT}.`
      );
      console.error(
        "Per SKILL.md §0 rule 7: set .slide { width; height } in the HTML to the canvas size. " +
        "For a deliberately non-standard deck, pass --canvas WxH instead of --width/--height."
      );
      process.exit(2);
    } else if (arg === "--svg-vector" && value) {
      opts.svgVector = value !== "false";
      i += 1;
    } else if (arg === "--bg-capture" && value) {
      if (value !== "always" && value !== "never") failUsage();
      opts.bgCapture = value;
      i += 1;
    } else if (arg === "--allow-self-check-issues") {
      // Backward-compatible no-op: editable export no longer runs HTML self-check.
    } else {
      failUsage();
    }
  }
  return opts;
}

function requireModule(name, installHint) {
  try {
    return require(name);
  } catch (error) {
    if (error && error.code === "MODULE_NOT_FOUND") {
      console.error(`Missing dependency: ${name}`);
      console.error(installHint);
      console.error("Without a browser host, ask the user to choose HTML delivery or native PptxGenJS PPTX.");
      process.exit(1);
    }
    throw error;
  }
}

function printBrowserInstallHint() {
  const browsersPath = process.env.PLAYWRIGHT_BROWSERS_PATH || officeRaccoonBrowserHostPath();
  console.error("Playwright Chromium is not available.");
  console.error("Install it in Office Raccoon: Settings -> Plugins -> Web automation (Playwright) -> Download Chromium and enable.");
  console.error(`Expected browser host under: ${browsersPath}`);
  console.error("If Playwright itself is missing, reinstall or repair Office Raccoon's managed runtime.");
  console.error("Without a browser host, ask the user to choose HTML delivery or native PptxGenJS PPTX.");
}

async function waitForDiagramLayout(page) {
  await page.evaluate(async () => {
    const pending = window.__deckDiagramReady;
    if (!pending || typeof pending.then !== "function") return;
    try {
      await pending;
    } catch {
      // Keep exporting the remaining DOM when an optional diagram render fails.
    }
  });
}

function resolveBrowserBundle() {
  const bundlePath = path.join(__dirname, "dom-to-pptx.bundle.js");
  if (!fs.existsSync(bundlePath)) {
    console.error(`Missing bundled converter: ${bundlePath}`);
    console.error("This skill requires scripts/dom-to-pptx.bundle.js for HTML editable export.");
    process.exit(1);
  }
  return bundlePath;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const htmlPath = path.resolve(opts.html);
  const pptxPath = path.resolve(opts.pptx);
  const outDir = path.resolve(opts.out);

  if (!fs.existsSync(htmlPath)) {
    console.error(`HTML file not found: ${htmlPath}`);
    process.exit(1);
  }

  const { chromium } = requireModule(
    "playwright",
    "Repair Office Raccoon's managed runtime, then install Chromium in Settings -> Plugins -> Web automation (Playwright)."
  );
  const bundlePath = resolveBrowserBundle();

  fs.mkdirSync(path.dirname(pptxPath), { recursive: true });
  fs.mkdirSync(outDir, { recursive: true });

  let browser;
  try {
    const launch = chromiumLaunchOptions(chromium, { headless: true });
    browser = await chromium.launch(launch.options);
  } catch (error) {
    printBrowserInstallHint();
    throw error;
  }

  // The canvas size is fixed by the contract (canonical 1920x1080, or an
  // explicit --canvas override). We probe at that size, then assert the actual
  // .slide CSS matches it before exporting — exporting a mismatched .slide would
  // silently distort it into the 16:9 LAYOUT_WIDE PPTX frame.
  const expectedWidth = opts.width;
  const expectedHeight = opts.height;
  const probeViewport = { width: expectedWidth, height: expectedHeight };
  let page = await browser.newPage({
    viewport: probeViewport,
    deviceScaleFactor: 1,
  });

  await page.goto(`file://${htmlPath}`, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
  await page.evaluate(() => document.fonts && document.fonts.ready);

  const detected = await page.evaluate(() => {
    const s = document.querySelector(".slide");
    if (!s) return null;
    const cs = getComputedStyle(s);
    return { w: parseFloat(cs.width) || 0, h: parseFloat(cs.height) || 0 };
  });

  const cssW = detected && detected.w > 0 ? detected.w : null;
  const cssH = detected && detected.h > 0 ? detected.h : null;
  const mismatchW = cssW !== null && Math.abs(expectedWidth - cssW) > 2;
  const mismatchH = cssH !== null && Math.abs(expectedHeight - cssH) > 2;
  if (cssW === null || cssH === null) {
    console.error("Refusing to export: no .slide element with a CSS size was found.");
    await browser.close();
    process.exit(1);
  }
  if (mismatchW || mismatchH) {
    console.error(
      `Refusing to export: .slide CSS size is ${Math.round(cssW)}x${Math.round(cssH)}, ` +
      `but the canvas contract is ${expectedWidth}x${expectedHeight}.`
    );
    console.error(
      `Set .slide { width: ${expectedWidth}px; height: ${expectedHeight}px; } in the HTML. ` +
      "For a deliberately non-standard deck, pass --canvas WxH to match the HTML."
    );
    await browser.close();
    process.exit(1);
  }

  const detectedWidth = expectedWidth;
  const detectedHeight = expectedHeight;

  const needsResize =
    Math.abs(probeViewport.width - detectedWidth) > 2 ||
    Math.abs(probeViewport.height - detectedHeight) > 2;
  if (needsResize) {
    await page.close();
    page = await browser.newPage({
      viewport: { width: detectedWidth, height: detectedHeight },
      deviceScaleFactor: 2,
    });
    await page.exposeFunction("__writePptxBase64", base64 => {
      fs.writeFileSync(pptxPath, Buffer.from(base64, "base64"));
    });
    await page.goto(`file://${htmlPath}`, { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
    await page.evaluate(() => document.fonts && document.fonts.ready);
  } else {
    await page.setViewportSize({ width: detectedWidth, height: detectedHeight });
    await page.exposeFunction("__writePptxBase64", base64 => {
      fs.writeFileSync(pptxPath, Buffer.from(base64, "base64"));
    });
  }

  await waitForDiagramLayout(page);

  const controlledSlideCount = await page.locator("#deck-root > .slide").count();
  const slideSelector = controlledSlideCount ? "#deck-root > .slide" : ".slide";
  const diagramSelector = controlledSlideCount
    ? "#deck-root > .slide [data-pptx-diagram]"
    : "[data-pptx-diagram]";
  const nativeChartSelector = controlledSlideCount
    ? '#deck-root > .slide [data-pptx-chart][data-native-chart="true"]'
    : '[data-pptx-chart][data-native-chart="true"]';
  const diagramCount = await page.locator(diagramSelector).count();
  const effectiveSvgVector = opts.svgVector || diagramCount > 0;
  if (diagramCount > 0 && !opts.svgVector) {
    console.log(
      `Detected ${diagramCount} data-pptx-diagram element(s); forcing svgAsVector: true.`
    );
  }

  const inlinedImages = await inlineLocalImagesForExport(page, htmlPath);
  if (inlinedImages.length) {
    console.log(`Inlined ${inlinedImages.length} local image(s) for PPTX export.`);
  }

  const slideHandles = await page.locator(slideSelector).elementHandles();
  if (!slideHandles.length) {
    await browser.close();
    console.error("No .slide elements found in HTML.");
    process.exit(1);
  }

  // Source previews must reflect the authored HTML before background capture
  // mutates stacking and visibility for the editable PPTX export tree.
  const previews = [];
  for (let i = 0; i < slideHandles.length; i += 1) {
    const imagePath = path.join(outDir, `slide-${String(i + 1).padStart(2, "0")}.png`);
    await slideHandles[i].screenshot({ path: imagePath });
    previews.push(imagePath);
  }

  let bgCaptures = [];
  if (opts.bgCapture === "always") {
    await injectCaptureStyles(page);
    await markDecorationNodes(page);
    bgCaptures = await captureSlideBackgrounds({
      page,
      slideHandles,
    });
    await applyDecorationFlatten({
      page,
      captures: bgCaptures,
      width: detectedWidth,
      height: detectedHeight,
    });
    console.log(`Captured ${bgCaptures.length} background image(s) in-memory.`);
  }

  await page.addScriptTag({ path: bundlePath });

  const exportResult = await page.evaluate(
    async ({
      fileName,
      svgVector,
      slideSelector,
      diagramSelector,
      nativeChartSelector,
    }) => {
      const api = window.domToPptx;
      if (!api || typeof api.exportToPptx !== "function") {
        throw new Error("dom-to-pptx browser API was not loaded.");
      }
      const slideElements = Array.from(document.querySelectorAll(slideSelector));
      const diagramElements = document.querySelectorAll(diagramSelector);
      const blob = await api.exportToPptx(slideElements, {
        fileName,
        skipDownload: true,
        autoEmbedFonts: true,
        svgAsVector: svgVector,
        layout: "LAYOUT_WIDE",
        width: 13.333,
        height: 7.5,
      });
      const base64 = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = () => reject(reader.error || new Error("Failed to read PPTX blob."));
        reader.readAsDataURL(blob);
      });
      await window.__writePptxBase64(base64);
      return {
        slideCount: slideElements.length,
        bytes: blob.size,
        diagramCount: diagramElements.length,
        diagramVectorExport: diagramElements.length > 0 && svgVector,
        nativeChartCount: document.querySelectorAll(nativeChartSelector).length,
      };
    },
    {
      fileName: path.basename(pptxPath),
      svgVector: effectiveSvgVector,
      slideSelector,
      diagramSelector,
      nativeChartSelector,
    }
  );

  await browser.close();

  console.log(
    JSON.stringify(
      {
        pptx: pptxPath,
        slideCount: exportResult.slideCount,
        bytes: exportResult.bytes,
        diagramCount: exportResult.diagramCount,
        diagramVectorExport: exportResult.diagramVectorExport,
        nativeChartCount: exportResult.nativeChartCount,
        previews,
        editableExport: "dom-to-pptx",
        localImagesInlinedForExport: inlinedImages,
        bgCapture: {
          mode: opts.bgCapture,
          slides: bgCaptures.map(c => c.filename),
        },
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
