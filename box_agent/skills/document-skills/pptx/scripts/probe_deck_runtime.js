#!/usr/bin/env node
"use strict";

const fs = require("fs");
const Module = require("module");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");
const {
  chromiumLaunchOptions,
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
  return require("playwright");
}

async function readEditorState(page, viewport) {
  return page.evaluate(({ width, height }) => {
    function rgb(value) {
      const match = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(value || "");
      if (match) return match.slice(1, 4).map(Number);
      const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(value || "").trim());
      if (!hex) return null;
      const normalized = hex[1].length === 3
        ? hex[1].split("").map(channel => channel + channel).join("")
        : hex[1];
      return [0, 2, 4].map(index => parseInt(normalized.slice(index, index + 2), 16));
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
      const left = luminance(rgb(foreground));
      const right = luminance(rgb(background));
      if (left == null || right == null) return null;
      return (Math.max(left, right) + 0.05) / (Math.min(left, right) + 0.05);
    }

    const firstSlide = document.querySelector("#deck-root > .slide");
    const toolbar = document.querySelector(".deck-toolbar");
    const statement = document.querySelector(".statement-poster");
    const diagram = document.querySelector("#deck-root > .slide [data-pptx-diagram]");
    const interactionRoot = document.querySelector("[data-deck-interaction-target]");
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
    const statementStyle = statement && getComputedStyle(statement);
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
    return {
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
        background: statementStyle.backgroundColor,
        color: statementStyle.color,
        contrast: contrast(statementStyle.color, statementStyle.backgroundColor),
      } : null,
      diagram: diagram ? {
        state: diagram.getAttribute("data-diagram-render-state"),
        svgRoots: diagram.querySelectorAll(":scope > svg").length,
        nodes: diagram.querySelectorAll("[data-diagram-node-id]").length,
        edges: diagram.querySelectorAll("[data-diagram-edge-id]").length,
      } : null,
      interaction: {
        requiredMode: document.body.getAttribute("data-deck-interaction-mode"),
        mode: interactionRoot
          ? interactionRoot.getAttribute("data-deck-interaction-target")
          : null,
        ready: interactionRoot
          ? interactionRoot.getAttribute("data-interaction-ready") === "true"
          : false,
        planetCount: interactionRoot
          ? interactionRoot.querySelectorAll(".deck-planet").length
          : 0,
        spinVisualCount: interactionRoot
          ? interactionRoot.querySelectorAll(".deck-spin-stage img, .deck-spin-placeholder").length
          : 0,
      },
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
      if (window.__deckInteractionReady && typeof window.__deckInteractionReady.then === "function") {
        await window.__deckInteractionReady;
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
    if (editor.statement && editor.statement.contrast < 4.5) {
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
    if (editor.diagram && (
      editor.diagram.state !== "ready" ||
      editor.diagram.svgRoots !== 1 ||
      editor.diagram.nodes < 2
    )) {
      issues.push("Technical diagram runtime did not produce one ready inline SVG graph");
    }
    if (editor.interaction.requiredMode && (
      !editor.interaction.ready
      || editor.interaction.mode !== editor.interaction.requiredMode
    )) {
      issues.push("Required deck interaction runtime did not initialize");
    }
    if (
      editor.interaction.requiredMode === "solar_orbit"
      && editor.interaction.planetCount !== 8
    ) {
      issues.push(
        `Solar interaction rendered ${editor.interaction.planetCount} planets instead of 8`
      );
    }
    if (
      editor.interaction.requiredMode === "spin_360"
      && editor.interaction.spinVisualCount !== 1
    ) {
      issues.push("360 interaction does not contain exactly one rotatable visual");
    }
    (editor.diagrams || []).forEach((diagram, index) => {
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

    const report = { ok: issues.length === 0, issues, editor, export: exported };
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
