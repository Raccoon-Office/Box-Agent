#!/usr/bin/env node
"use strict";

const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");
const {
  chromiumLaunchOptions,
  loadPlaywright: loadHostPlaywright,
  ensurePlaywrightBrowsersPath,
} = require("./playwright_host");
const { resolveArtifactPath } = require("./deck_spec_core.js");

function usage() {
  console.error("Usage: probe_deck_runtime.js index.html [--viewport WxH] [--report qa/runtime_probe.json] [--exercise-diagram-editor]");
  process.exit(2);
}

function parseViewport(value) {
  const match = /^(\d+)\s*[xX]\s*(\d+)$/.exec(String(value || ""));
  if (!match) return null;
  return { width: Number(match[1]), height: Number(match[2]) };
}

function parseArgs(argv) {
  if (!argv[0] || argv[0] === "--help" || argv[0] === "-h") usage();
  const opts = {
    html: resolveArtifactPath(argv[0]),
    viewport: { width: 1440, height: 900 },
    report: null,
    exerciseDiagramEditor: false,
  };
  for (let index = 1; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--viewport" && value) {
      const viewport = parseViewport(value);
      if (!viewport) usage();
      opts.viewport = viewport;
      index += 1;
    } else if (arg === "--report" && value) {
      opts.report = resolveArtifactPath(value);
      index += 1;
    } else if (arg === "--exercise-diagram-editor") {
      opts.exerciseDiagramEditor = true;
    } else {
      usage();
    }
  }
  return opts;
}

async function exerciseTechnicalDiagramEditor(page) {
  const initial = await page.evaluate(() => {
    const root = document.querySelector("[data-pptx-diagram]");
    if (!root) return null;
    const spec = JSON.parse(root.getAttribute("data-diagram-spec") || "{}");
    const slide = root.closest(".slide");
    const slideIndex = Array.from(document.querySelectorAll("#deck-root > .slide")).indexOf(slide);
    return { nodes: spec.nodes.length, edges: spec.edges.length, slideIndex, kind: spec.kind };
  });
  if (!initial) return null;

  await page.evaluate(index => {
    document.querySelector(`[data-thumbnail-index="${index}"]`)?.click();
  }, initial.slideIndex);
  await page.waitForFunction(index =>
    document.querySelectorAll("#deck-root > .slide")[index]?.classList.contains("is-current-slide"),
  initial.slideIndex);

  await page.evaluate(() => document.querySelector('[data-action="adjust"]')?.click());
  await page.waitForFunction(() => {
    const panel = document.querySelector("#deck-layout-controls");
    return panel && !panel.hidden && panel.querySelector('[data-control-action="add-diagram-node"]');
  });
  await page.locator('[data-control-action="add-diagram-node"]').click();
  await page.waitForFunction(expected => {
    const root = document.querySelector("[data-pptx-diagram]");
    return root && root.getAttribute("data-diagram-render-state") === "ready" &&
      root.querySelectorAll("[data-diagram-node-id]").length === expected;
  }, initial.nodes + 1);

  const labelPath = `nodes.${initial.nodes}.label`;
  await page.locator(`[data-control-action="set-data-value"][data-control-path="${labelPath}"]`).evaluate(input => {
    input.value = "已编辑节点";
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.waitForFunction(() => {
    const root = document.querySelector("[data-pptx-diagram]");
    return root && root.getAttribute("data-diagram-render-state") === "ready" &&
      (root.textContent || "").includes("已编辑节点");
  });
  const editedNodeObserved = await page.evaluate(() =>
    (document.querySelector("[data-pptx-diagram]")?.textContent || "").includes("已编辑节点")
  );

  await page.locator('[data-control-action="add-diagram-edge"]').click();
  await page.waitForFunction(expected => {
    const root = document.querySelector("[data-pptx-diagram]");
    return root && root.getAttribute("data-diagram-render-state") === "ready" &&
      root.querySelectorAll("[data-diagram-edge-id]").length === expected;
  }, initial.edges + 1);
  await page.locator(
    `[data-control-action="delete-diagram-edge"][data-control-index="${initial.edges}"]`
  ).click();
  await page.waitForFunction(expected => {
    const root = document.querySelector("[data-pptx-diagram]");
    return root && root.getAttribute("data-diagram-render-state") === "ready" &&
      root.querySelectorAll("[data-diagram-edge-id]").length === expected;
  }, initial.edges);
  await page.locator(
    `[data-control-action="delete-diagram-node"][data-control-index="${initial.nodes}"]`
  ).click();
  await page.waitForFunction(expected => {
    const root = document.querySelector("[data-pptx-diagram]");
    return root && root.getAttribute("data-diagram-render-state") === "ready" &&
      root.querySelectorAll("[data-diagram-node-id]").length === expected;
  }, initial.nodes);
  await page.locator('[data-control-action="relayout-diagram"]').click();
  await page.evaluate(async () => {
    if (window.__deckDiagramReady && typeof window.__deckDiagramReady.then === "function") {
      await window.__deckDiagramReady;
    }
  });

  const final = await page.evaluate(expected => {
    const root = document.querySelector("[data-pptx-diagram]");
    const spec = JSON.parse(root.getAttribute("data-diagram-spec") || "{}");
    return {
      initial: expected,
      final: {
        nodes: spec.nodes.length,
        edges: spec.edges.length,
        slideIndex: expected.slideIndex,
        kind: expected.kind,
      },
      state: root.getAttribute("data-diagram-render-state"),
      svgRoots: root.querySelectorAll(":scope > svg").length,
      layoutStrategy: root.getAttribute("data-diagram-layout-strategy"),
    };
  }, initial);
  return { ...final, editedNodeObserved };
}

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

function loadPlaywright() {
  ensurePlaywrightBrowsersPath();
  const managedNodeModules = path.join(officeRaccoonPrefix(), "node_modules");
  process.env.NODE_PATH = process.env.NODE_PATH
    ? `${managedNodeModules}${path.delimiter}${process.env.NODE_PATH}`
    : managedNodeModules;
  Module._initPaths();
  return loadHostPlaywright();
}

async function readEditorState(page, viewport) {
  return page.evaluate(({ width, height }) => {
    const colorCanvas = document.createElement("canvas");
    colorCanvas.width = colorCanvas.height = 1;
    const colorContext = colorCanvas.getContext("2d", { willReadFrequently: true });
    const colorCache = new Map();
    function rgba(value) {
      if (!value) return null;
      if (colorCache.has(value)) return colorCache.get(value);
      colorContext.clearRect(0, 0, 1, 1);
      colorContext.fillStyle = "transparent";
      colorContext.fillStyle = value;
      colorContext.fillRect(0, 0, 1, 1);
      const pixel = colorContext.getImageData(0, 0, 1, 1).data;
      const color = [pixel[0], pixel[1], pixel[2], pixel[3] / 255];
      colorCache.set(value, color);
      return color;
    }
    function rgb(value) {
      const color = rgba(value);
      return color && color[3] > 0 ? color.slice(0, 3) : null;
    }
    function composite(front, back) {
      return [0, 1, 2].map(index => front[index] * front[3] + back[index] * (1 - front[3])).concat(1);
    }
    function luminance(color) {
      if (!color) return null;
      const channels = color.map(value => {
        const normalized = value / 255;
        return normalized <= 0.03928
          ? normalized / 12.92
          : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2]);
    }
    function contrast(foreground, background) {
      const front = rgba(foreground), back = rgba(background);
      if (!front || !back || !front[3] || back[3] < 1) return null;
      const left = luminance(composite(front, back).slice(0, 3));
      const right = luminance(back.slice(0, 3));
      return (Math.max(left, right) + 0.05) / (Math.min(left, right) + 0.05);
    }
    function effectiveBackground(element) {
      const layers = [];
      let current = element;
      while (current) {
        const style = getComputedStyle(current);
        const imageLayer = current.matches(".slide") && current.querySelector(":scope > .slide-background");
        if (imageLayer) {
          const wash = getComputedStyle(imageLayer, "::after");
          const washColor = rgba(wash.backgroundColor);
          if (wash.display === "none" || !washColor || wash.backgroundImage !== "none") return null;
          const overlay = [...washColor];
          overlay[3] *= Number(wash.opacity);
          // Bound the backing image by black/white; no image model is required.
          // Include intermediate values so text inside the luminance interval
          // cannot accidentally pass on two contrasting endpoints.
          const foreground = getComputedStyle(element).color;
          const candidates = Array.from({ length: 17 }, (_, index) => {
            const shade = index * 255 / 16;
            const base = composite(overlay, [shade, shade, shade, 1]);
            const color = [...layers].reverse().reduce((back, front) => composite(front, back), base);
            return `rgb(${color.slice(0, 3).map(Math.round).join(", ")})`;
          });
          return candidates.sort((a, b) => contrast(foreground, a) - contrast(foreground, b))[0];
        }
        const background = rgba(style.backgroundColor);
        if (background && background[3] > 0) layers.push(background);
        // A transparent gradient/image has no single known backing color.
        // Report it as unmeasured instead of interpreting transparency as black.
        if (style.backgroundImage !== "none") return null;
        if (background && background[3] === 1) break;
        current = current.parentElement;
      }
      const color = layers.reverse().reduce((back, front) => composite(front, back), [255, 255, 255, 1]);
      return `rgb(${color.slice(0, 3).map(Math.round).join(", ")})`;
    }

    const firstSlide = document.querySelector("#deck-root > .slide");
    const toolbar = document.querySelector(".deck-toolbar");
    const statement = document.querySelector(".statement-poster");
    const diagram = document.querySelector("#deck-root > .slide [data-pptx-diagram]");
    const diagrams = Array.from(document.querySelectorAll("#deck-root > .slide [data-pptx-diagram]")).map(root => {
      let spec = {};
      try {
        spec = JSON.parse(root.getAttribute("data-diagram-spec") || "{}");
      } catch (_) {
        spec = {};
      }
      const slide = root.closest(".slide");
      const header = slide?.querySelector(".slide-header");
      const slideRect = slide?.getBoundingClientRect();
      const rootRect = root.getBoundingClientRect();
      const headerRect = header?.getBoundingClientRect();
      const scale = slideRect ? slideRect.width / 1920 : 1;
      const labelSizes = Array.from(root.querySelectorAll('[data-diagram-text="label"]')).map(text => {
        const matrix = text.getScreenCTM();
        return matrix ? parseFloat(getComputedStyle(text).fontSize) * Math.hypot(matrix.a, matrix.b) / scale : 0;
      });
      const nodeRects = Array.from(root.querySelectorAll("[data-diagram-node-id]"))
        .map(node => node.getBoundingClientRect());
      const renderedNodeIds = Array.from(root.querySelectorAll("[data-diagram-node-id]"))
        .map(node => node.getAttribute("data-diagram-node-id"));
      const labelRects = Array.from(root.querySelectorAll("[data-diagram-edge-label-id]"))
        .map(label => label.getBoundingClientRect());
      const overlaps = (left, right) => (
        Math.min(left.right, right.right) - Math.max(left.left, right.left) > 1
        && Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top) > 1
      );
      let labelNodeOverlapCount = 0;
      labelRects.forEach(labelRect => {
        nodeRects.forEach(nodeRect => {
          if (overlaps(labelRect, nodeRect)) labelNodeOverlapCount += 1;
        });
      });
      let labelLabelOverlapCount = 0;
      labelRects.forEach((labelRect, labelIndex) => {
        labelRects.slice(labelIndex + 1).forEach(otherRect => {
          if (overlaps(labelRect, otherRect)) labelLabelOverlapCount += 1;
        });
      });
      const nodeSpread = nodeRects.length && slideRect ? {
        width: (
          Math.max(...nodeRects.map(rect => rect.right))
          - Math.min(...nodeRects.map(rect => rect.left))
        ) / scale,
        height: (
          Math.max(...nodeRects.map(rect => rect.bottom))
          - Math.min(...nodeRects.map(rect => rect.top))
        ) / scale,
      } : null;
      return {
        kind: root.getAttribute("data-diagram-kind"),
        state: root.getAttribute("data-diagram-render-state"),
        strategy: root.getAttribute("data-diagram-layout-strategy"),
        svgRoots: root.querySelectorAll(":scope > svg").length,
        nodes: root.querySelectorAll("[data-diagram-node-id]").length,
        specNodes: Array.isArray(spec.nodes) ? spec.nodes.length : 0,
        uniqueNodeIds: new Set(renderedNodeIds).size,
        edges: root.querySelectorAll("[data-diagram-edge-id]").length,
        edgeLabels: labelRects.length,
        labelNodeOverlapCount,
        labelLabelOverlapCount,
        nodeSpread,
        minimumLabelSize: labelSizes.length ? Math.min(...labelSizes) : null,
        box: slideRect ? {
          top: (rootRect.top - slideRect.top) / scale,
          bottom: (rootRect.bottom - slideRect.top) / scale,
          height: rootRect.height / scale,
          headerBottom: headerRect ? (headerRect.bottom - slideRect.top) / scale : null,
        } : null,
      };
    });
    const firstRect = firstSlide && firstSlide.getBoundingClientRect();
    const toolbarRect = toolbar && toolbar.getBoundingClientRect();
    const statementText = statement && (statement.querySelector("h1") || statement);
    const statementStyle = statementText && getComputedStyle(statementText);
    const statementBackground = statement && effectiveBackground(statementText, statement);
    const rootStyle = getComputedStyle(document.documentElement);
    const coreColors = {
      background: rootStyle.getPropertyValue("--deck-bg").trim(),
      text: rootStyle.getPropertyValue("--deck-text").trim(),
      primary: rootStyle.getPropertyValue("--deck-primary").trim(),
      inverse: rootStyle.getPropertyValue("--deck-inverse").trim(),
    };
    const normalizedCoreColors = Object.values(coreColors)
      .map(value => rgb(value))
      .filter(Boolean)
      .map(value => value.join(","));
    const contrastSamples = [];
    const unresolvedBackgrounds = [];
    document.querySelectorAll("#deck-root > .slide").forEach((slide, slideIndex) => {
      slide.querySelectorAll(
        "h1,h2,h3,p,li,td,th,strong,.card-index,.timeline-marker,.timeline-number,.kpi-value,.eyebrow"
      ).forEach(element => {
        const rect = element.getBoundingClientRect();
        const content = String(element.textContent || "").trim();
        if (!content || rect.width < 1 || rect.height < 1) return;
        const foreground = getComputedStyle(element).color;
        const background = effectiveBackground(element, slide);
        const ratio = contrast(foreground, background);
        if (ratio == null) {
          if (background === null) unresolvedBackgrounds.push({ slide: slideIndex + 1, text: content.slice(0, 80) });
          return;
        }
        contrastSamples.push({
          slide: slideIndex + 1,
          element: element.tagName.toLowerCase(),
          className: String(element.className || "").slice(0, 120),
          foreground,
          background,
          ratio,
          text: content.replace(/\s+/g, " ").slice(0, 80),
        });
      });
    });
    const componentContrastFailures = contrastSamples
      .filter(sample => sample.ratio < 4.5)
      .sort((left, right) => left.ratio - right.ratio);
    const documentData = JSON.parse(document.querySelector("#deck-document")?.textContent || "{}");
    const fixedPalette = documentData.design_contract?.palette;
    const paletteCompliance = { enforced: fixedPalette?.version === 2, sampled: 0, failures: [] };
    if (paletteCompliance.enforced) {
      const allowed = Object.values(fixedPalette.tokens).flat().filter(value => typeof value === "string").map(rgba).filter(Boolean);
      const permitted = value => {
        // Canvas premultiplication quantizes RGB at low alpha. Compare the
        // declared channels directly rather than rejecting a valid faint tint.
        const isSrgb = /^color\(srgb\s/.test(value);
        const components = /^rgba?\([\d.,\s/]+\)$/.test(value) || isSrgb ? value.match(/[\d.]+/g)?.map(Number) : null;
        if (isSrgb && components) components.splice(0, 3, ...components.slice(0, 3).map(channel => channel * 255));
        const color = components?.length >= 3 ? [...components.slice(0, 3), components[3] ?? 1] : rgba(value);
        return !color || color[3] === 0 || allowed.some(candidate => color.slice(0, 3).every((channel, i) => Math.round(channel) === Math.round(candidate[i])));
      };
      document.querySelectorAll("#deck-root > .slide").forEach((slide, index) => {
        for (const element of [slide, ...slide.querySelectorAll("*")]) {
          if (element.closest(".slide-background") || ["SCRIPT", "STYLE", "IMG"].includes(element.tagName)) continue;
          const rect = element.getBoundingClientRect();
          if (element.namespaceURI === "http://www.w3.org/2000/svg") {
            const tag = element.tagName.toLowerCase();
            if (!/^(path|rect|circle|ellipse|line|polygon|polyline|text|tspan|stop)$/.test(tag) || element.closest("clipPath, mask")) continue;
            if (tag !== "stop" && rect.width < 1 && rect.height < 1) continue;
            const style = getComputedStyle(element);
            if (parseFloat(style.opacity) === 0) continue;
            const properties = tag === "stop" ? ["stopColor"] : tag === "line" ? ["stroke"] : ["fill", "stroke"];
            for (const property of properties) {
              if (property === "fill" && (rect.width === 0 || rect.height === 0 || style.fill === "none" || parseFloat(style.fillOpacity) === 0)) continue;
              if (property === "stroke" && (style.stroke === "none" || parseFloat(style.strokeWidth) === 0 || parseFloat(style.strokeOpacity) === 0)) continue;
              if (property === "stopColor" && parseFloat(style.stopOpacity) === 0) continue;
              paletteCompliance.sampled += 1;
              if (!permitted(style[property])) paletteCompliance.failures.push({ slide: index + 1,
                element: element.getAttribute("data-diagram-node-id") || tag, property, color: style[property] });
            }
            continue;
          }
          if (rect.width < 1 || rect.height < 1) continue;
          const style = getComputedStyle(element);
          for (const property of ["color", "backgroundColor", "borderTopColor", "borderRightColor", "borderBottomColor", "borderLeftColor"]) {
            if (property.startsWith("border") && parseFloat(style[property.replace("Color", "Width")]) === 0) continue;
            paletteCompliance.sampled += 1;
            if (!permitted(style[property])) paletteCompliance.failures.push({ slide: index + 1,
              element: element.getAttribute("data-prop-path") || element.className || element.tagName,
              property, color: style[property] });
          }
          const role = element.getAttribute("data-deck-text-role");
          const expectedInk = /^(H1|H2)$/.test(element.tagName) ? fixedPalette.tokens.heading
            : ["body", "lead"].includes(role) ? style.getPropertyValue("--deck-content-text").trim() || fixedPalette.tokens.text : null;
          if (expectedInk && rgba(style.color)?.join() !== rgba(expectedInk)?.join()) {
            paletteCompliance.failures.push({ slide: index + 1, element: element.getAttribute("data-prop-path") || element.tagName,
              property: "color", color: style.color, expected: expectedInk, role: /^(H1|H2)$/.test(element.tagName) ? "heading" : role });
          }
          for (const pseudo of ["", "::before", "::after"]) {
            const extra = pseudo ? getComputedStyle(element, pseudo) : style;
            if (pseudo && ["none", "normal"].includes(extra.content)) continue;
            const properties = pseudo ? ["color", "backgroundColor", "borderColor", "backgroundImage", "boxShadow", "textShadow"] : ["backgroundImage", "boxShadow", "textShadow"];
            for (const property of properties) {
              for (const color of extra[property].match(/rgba?\([^)]*\)/g) || []) {
                paletteCompliance.sampled += 1;
                if (!permitted(color)) paletteCompliance.failures.push({ slide: index + 1,
                  element: `${element.className || element.tagName}${pseudo}`, property, color });
              }
            }
          }
        }
        if (rgba(getComputedStyle(slide).backgroundColor)?.slice(0, 3).join() !== rgba(fixedPalette.tokens.background)?.slice(0, 3).join()) {
          paletteCompliance.failures.push({ slide: index + 1, property: "backgroundColor", expected: fixedPalette.tokens.background });
        }
      });
    }
    return {
      paletteCompliance,
      viewport: { width, height },
      bodyOverflowX: getComputedStyle(document.body).overflowX,
      thumbnailsVisible: document.body.classList.contains("deck-thumbnails-visible"),
      editorScale: Number(rootStyle.getPropertyValue("--deck-editor-scale")) || 1,
      primary: coreColors.primary,
      inverse: coreColors.inverse,
      palette: {
        ...coreColors,
        distinctCoreColors: new Set(normalizedCoreColors).size,
        textOnBackgroundContrast: contrast(coreColors.text, coreColors.background),
      },
      componentContrast: {
        sampled: contrastSamples.length,
        unresolvedCount: unresolvedBackgrounds.length,
        unresolvedBackgrounds: unresolvedBackgrounds.slice(0, 8),
        minimum: contrastSamples.length
          ? Math.min(...contrastSamples.map(sample => sample.ratio))
          : null,
        failureCount: componentContrastFailures.length,
        affectedSlides: [...new Set(componentContrastFailures.map(sample => sample.slide))].sort((a, b) => a - b),
        failures: componentContrastFailures.slice(0, 8),
      },
      firstSlide: firstRect ? {
        left: firstRect.left,
        right: firstRect.right,
        top: firstRect.top,
        bottom: firstRect.bottom,
        width: firstRect.width,
        height: firstRect.height,
      } : null,
      toolbarTop: toolbarRect ? toolbarRect.top : null,
      toolbar: toolbarRect ? {
        left: toolbarRect.left,
        right: toolbarRect.right,
        width: toolbarRect.width,
        clientWidth: toolbar.clientWidth,
        scrollWidth: toolbar.scrollWidth,
        overflowX: getComputedStyle(toolbar).overflowX,
        hasOverflow: toolbar.scrollWidth > toolbar.clientWidth + 1,
      } : null,
      statement: statementStyle ? {
        background: statementBackground,
        color: statementStyle.color,
        contrast: contrast(statementStyle.color, statementBackground),
      } : null,
      diagram: diagram ? {
        state: diagram.getAttribute("data-diagram-render-state"),
        svgRoots: diagram.querySelectorAll(":scope > svg").length,
        nodes: diagram.querySelectorAll("[data-diagram-node-id]").length,
        edges: diagram.querySelectorAll("[data-diagram-edge-id]").length,
      } : null,
      diagrams,
    };
  }, viewport);
}

async function probeToolbarMenuTrajectory(page, menuName) {
  const group = page.locator(`[data-toolbar-menu="${menuName}"]`);
  const trigger = group.locator("[data-toolbar-menu-trigger]");
  const menu = group.locator("[role=menu]");
  if (await group.count() === 0 || await trigger.count() === 0 || await menu.count() === 0) {
    return { available: false, open: false, expanded: false };
  }

  await page.mouse.move(8, 8);
  await page.waitForTimeout(220);
  await trigger.hover();
  // Sample the resting menu bounds, not its animated opening position.
  await menu.evaluate(async element => {
    await Promise.all(element.getAnimations().map(animation => animation.finished));
  });
  const triggerBox = await trigger.boundingBox();
  const menuBox = await menu.boundingBox();
  if (!triggerBox || !menuBox) {
    return { available: true, open: false, expanded: false };
  }

  const start = {
    x: triggerBox.x + (triggerBox.width / 2),
    y: triggerBox.y + 2,
  };
  const end = {
    x: menuBox.x + 12,
    y: menuBox.y + menuBox.height - 2,
  };
  for (let step = 0; step <= 14; step += 1) {
    const ratio = step / 14;
    await page.mouse.move(
      start.x + ((end.x - start.x) * ratio),
      start.y + ((end.y - start.y) * ratio)
    );
    await page.waitForTimeout(20);
  }
  await page.waitForTimeout(40);

  return group.evaluate(element => {
    const menuTrigger = element.querySelector("[data-toolbar-menu-trigger]");
    return {
      available: true,
      open: element.classList.contains("is-open"),
      expanded: menuTrigger && menuTrigger.getAttribute("aria-expanded") === "true",
    };
  });
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (!fs.existsSync(opts.html)) throw new Error(`HTML file not found: ${opts.html}`);
  const { chromium } = loadPlaywright();
  const launch = chromiumLaunchOptions(chromium, { headless: true });
  const browser = await chromium.launch(launch.options);
  try {
    const context = await browser.newContext({ viewport: opts.viewport });
    await context.addInitScript(() => {
      Object.defineProperty(navigator, "webdriver", { configurable: true, get: () => false });
    });
    const page = await context.newPage();
    const editorUrl = pathToFileURL(opts.html).href;
    await page.goto(editorUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => Boolean(window.__deckRuntime));
    await page.evaluate(async () => {
      if (window.__deckDiagramReady && typeof window.__deckDiagramReady.then === "function") {
        await window.__deckDiagramReady;
      }
    });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const editor = await readEditorState(page, opts.viewport);
    editor.toolbarMenus = {
      design: await probeToolbarMenuTrajectory(page, "design"),
      page: await probeToolbarMenuTrajectory(page, "page"),
    };
    if (opts.exerciseDiagramEditor) {
      editor.diagramExercise = await exerciseTechnicalDiagramEditor(page);
    }
    await context.close();

    const exportPage = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    const exportUrl = new URL(pathToFileURL(opts.html).href);
    exportUrl.searchParams.set("mode", "export");
    await exportPage.goto(exportUrl.href, { waitUntil: "domcontentloaded" });
    await exportPage.evaluate(async () => {
      if (window.__deckDiagramReady && typeof window.__deckDiagramReady.then === "function") {
        await window.__deckDiagramReady;
      }
    });
    const exported = await exportPage.evaluate(() => {
      const slide = document.querySelector("#deck-root > .slide");
      const style = slide && getComputedStyle(slide);
      const rect = slide && slide.getBoundingClientRect();
      return slide ? {
        cssWidth: parseFloat(style.width),
        cssHeight: parseFloat(style.height),
        renderedWidth: rect.width,
        renderedHeight: rect.height,
      } : null;
    });
    await exportPage.close();

    const issues = [];
    const warnings = [];
    if (!editor.firstSlide) issues.push("No slide found in editor mode");
    if (editor.firstSlide && (
      editor.firstSlide.left < -1 || editor.firstSlide.right > opts.viewport.width + 1
    )) {
      issues.push("Editor slide exceeds the horizontal viewport");
    }
    if (editor.firstSlide && editor.toolbarTop != null && editor.firstSlide.bottom > editor.toolbarTop + 1) {
      issues.push("Editor slide is obscured by the bottom toolbar");
    }
    if (editor.toolbar && (
      editor.toolbar.left < -1 || editor.toolbar.right > opts.viewport.width + 1
    )) {
      issues.push("Editor toolbar exceeds the horizontal viewport");
    }
    if (editor.toolbar && editor.toolbar.hasOverflow) {
      issues.push(
        `Editor toolbar overflows horizontally: ${editor.toolbar.scrollWidth}px > ${editor.toolbar.clientWidth}px`
      );
    }
    Object.entries(editor.toolbarMenus || {}).forEach(([menuName, state]) => {
      if (state.available && (!state.open || !state.expanded)) {
        issues.push(`Toolbar ${menuName} menu closes during pointer transition`);
      }
    });
    if (editor.statement && editor.statement.contrast != null && editor.statement.contrast < 4.5) {
      issues.push(`Statement contrast is too low: ${editor.statement.contrast.toFixed(2)}`);
    }
    if (editor.palette && editor.palette.distinctCoreColors === 1) {
      issues.push("Core deck colors collapse to one value");
    }
    if (editor.palette && editor.palette.textOnBackgroundContrast < 4.5) {
      issues.push(
        `Deck text/background contrast is too low: ${editor.palette.textOnBackgroundContrast.toFixed(2)}`
      );
    }
    (editor.componentContrast && editor.componentContrast.failures || [])
      .forEach(failure => {
        warnings.push(
          `Slide ${failure.slide} local text contrast is ${failure.ratio.toFixed(2)}:1 ` +
          `for ${failure.element}${failure.className ? `.${failure.className.split(/\s+/)[0]}` : ""} ` +
          `(${failure.foreground} on ${failure.background}): ${failure.text}`
        );
      });
    if (editor.diagram && (
      editor.diagram.state !== "ready" ||
      editor.diagram.svgRoots !== 1 ||
      editor.diagram.nodes < 2
    )) {
      issues.push("Technical diagram runtime did not produce one ready inline SVG graph");
    }
    (editor.diagrams || []).forEach((diagram, index) => {
      if (diagram.minimumLabelSize !== null && diagram.minimumLabelSize < 24) {
        warnings.push(`Technical diagram ${index + 1} labels render at ${diagram.minimumLabelSize.toFixed(1)}px on the 1920px canvas; use fewer nodes or a roomier layout.`);
      }
      if (diagram.nodes !== diagram.specNodes || diagram.uniqueNodeIds !== diagram.nodes) {
        issues.push(
          `Technical diagram ${index + 1} rendered ${diagram.nodes} nodes (${diagram.uniqueNodeIds} unique) for ${diagram.specNodes} DiagramSpec nodes`
        );
      }
    });
    if (opts.exerciseDiagramEditor && (!editor.diagramExercise ||
        editor.diagramExercise.state !== "ready" ||
        editor.diagramExercise.svgRoots !== 1 ||
        !editor.diagramExercise.editedNodeObserved ||
        editor.diagramExercise.final.nodes !== editor.diagramExercise.initial.nodes ||
        editor.diagramExercise.final.edges !== editor.diagramExercise.initial.edges)) {
      issues.push("Technical diagram editor add/edit/delete/re-layout exercise failed");
    }
    if (!exported || exported.cssWidth !== 1920 || exported.cssHeight !== 1080) {
      issues.push("Export mode does not preserve the 1920x1080 CSS canvas");
    }
    if (!exported || exported.renderedWidth !== 1920 || exported.renderedHeight !== 1080) {
      issues.push("Export mode unexpectedly scales the slide canvas");
    }

    if (editor.componentContrast.unresolvedCount) {
      warnings.push(`${editor.componentContrast.unresolvedCount} text contrast sample(s) need visual inspection: transparent gradient/image background.`);
    }
    if (editor.paletteCompliance?.failures.length) {
      editor.paletteCompliance.failures.slice(0, 12).forEach(failure => issues.push(
        `Frozen palette mismatch on slide ${failure.slide}: ${failure.element || "slide"}.${failure.property} ${failure.color || ""}`
      ));
    }
    const report = { ok: issues.length === 0, issues, warnings, editor, export: exported };
    const output = `${JSON.stringify(report, null, 2)}\n`;
    if (opts.report) {
      fs.mkdirSync(path.dirname(opts.report), { recursive: true });
      fs.writeFileSync(opts.report, output, "utf8");
    }
    process.stdout.write(output);
    if (!report.ok) process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
