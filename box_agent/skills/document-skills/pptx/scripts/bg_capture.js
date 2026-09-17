"use strict";

const CAPTURE_CSS = `
/* Hide every non-decoration element during capture so the bitmap contains
 * only the slide-level background and pure decoration (no HTML text, no <img>,
 * no text containers / cards / pills). */
.pptx-capture-mode [data-pptx-non-decoration] {
  visibility: hidden !important;
}
/* A decoration SVG is flattened as a complete graphic, including its labels.
 * Content ancestors are hidden above; restore only SVG elements that were
 * visible before capture so their inherited visibility cannot erase the
 * graphic. Authored hidden elements, opacity, display and clipping stay intact. */
.pptx-capture-mode [data-pptx-decoration][data-pptx-capture-visible] {
  visibility: visible !important;
}
`;

const EXPORT_CSS = `
/* After capture: remove decoration nodes from the DOM-to-PPTX export tree,
 * because their visuals already live in the background bitmap. The decoration
 * node's pseudo-elements (::before / ::after) are removed implicitly with the
 * host element. We do NOT globally disable ::before/::after, because
 * dom-to-pptx reads ::before content strings as inline icon text on
 * non-decoration (text-bearing) elements. */
.pptx-export-flat [data-pptx-decoration]:not(.pptx-bg) {
  display: none !important;
}
/* The slide's own background is in the bitmap; make sure dom-to-pptx
 * does not paint it again. */
.pptx-export-flat .slide {
  background: transparent !important;
  background-image: none !important;
}
/* Background image must be visible, sized in absolute pixels, and below
 * every text/image element. */
.pptx-export-flat img.pptx-bg {
  display: block !important;
  visibility: visible !important;
  position: absolute !important;
  left: 0 !important;
  top: 0 !important;
  margin: 0 !important;
  padding: 0 !important;
  border: 0 !important;
  pointer-events: none !important;
  opacity: 1 !important;
  /* Controlled decks give every direct non-background child z-index: 1.
   * The injected capture image is also a direct child, so without this
   * explicit override it sorts above authored background/hero <img> nodes in
   * dom-to-pptx and can cover them in PowerPoint. Keep the capture below every
   * authored layer regardless of the source theme's stacking rules. */
  z-index: -100000 !important;
}
`;

async function injectCaptureStyles(page) {
  await page.addStyleTag({ content: CAPTURE_CSS + EXPORT_CSS });
}

async function markDecorationNodes(page) {
  await page.evaluate(() => {
    const DECORATION_TAGS = new Set(["SVG", "HR", "CANVAS"]);
    const CHART_SELECTOR = [
      "[data-pptx-chart]",
      "[data-chart-spec]",
      "[data-chart-spec-src]",
      "[_echarts_instance_]",
      ".echarts",
      ".echarts-for-pptx",
    ].join(",");
    const DIAGRAM_SELECTOR = "[data-pptx-diagram]";

    function hasContent(el) {
      if (el.querySelector("img")) return true;
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = walker.nextNode())) {
        if (n.textContent.trim()) return true;
      }
      return false;
    }

    function isChartElement(el) {
      return Boolean(el.matches(CHART_SELECTOR) || el.closest(CHART_SELECTOR));
    }

    function isDiagramElement(el) {
      return Boolean(el.matches(DIAGRAM_SELECTOR) || el.closest(DIAGRAM_SELECTOR));
    }

    Array.from(document.querySelectorAll(".slide")).forEach(slide => {
      const all = slide.querySelectorAll("*");
      all.forEach(el => {
        if (el === slide) return;
        if (el.classList.contains("pptx-bg")) return;
        // Technical diagrams are authored as recoverable DiagramSpec + inline
        // SVG. Keep the root and every descendant out of the background PNG
        // so dom-to-pptx can export the SVG as a separate vector picture.
        if (isDiagramElement(el)) {
          el.removeAttribute("data-pptx-decoration");
          el.setAttribute("data-pptx-non-decoration", "");
          return;
        }
        // ECharts/data-chart previews must not be baked into the background
        // screenshot. They need a separate native-chart/data-preserving path.
        if (isChartElement(el)) {
          el.setAttribute("data-pptx-non-decoration", "");
          return;
        }
        // <img> is always a native picture (never a decoration node).
        if (el.tagName === "IMG") {
          el.setAttribute("data-pptx-non-decoration", "");
          return;
        }
        // Treat SVG / HR / CANVAS as decoration even if they contain text.
        if (DECORATION_TAGS.has(el.tagName)) {
          el.setAttribute("data-pptx-decoration", "");
          return;
        }
        // Anything inside a decoration SVG follows the SVG root.
        if (el.closest("svg")) {
          el.setAttribute("data-pptx-decoration", "");
          return;
        }
        if (hasContent(el)) {
          el.setAttribute("data-pptx-non-decoration", "");
        } else {
          el.setAttribute("data-pptx-decoration", "");
        }
      });
      // Snapshot before capture-mode hides text-bearing containers. Do not
      // blanket-show SVG descendants: visibility:hidden can be authored on an
      // SVG group while one of its children explicitly overrides it to visible.
      // Marked diagrams and charts are non-decoration and stay on their own
      // export paths rather than being duplicated in the background bitmap.
      slide.querySelectorAll("svg[data-pptx-decoration], svg[data-pptx-decoration] *")
        .forEach(el => {
          el.removeAttribute("data-pptx-capture-visible");
          if (el.hasAttribute("data-pptx-decoration") &&
              !el.hasAttribute("data-pptx-non-decoration") &&
              getComputedStyle(el).visibility === "visible") {
            el.setAttribute("data-pptx-capture-visible", "");
          }
        });
    });
  });
}

async function captureSlideBackgrounds({ page, slideHandles }) {
  await page.evaluate(() => {
    document.documentElement.classList.add("pptx-capture-mode");
  });
  await page.evaluate(
    () => new Promise(resolve => {
      requestAnimationFrame(() => requestAnimationFrame(resolve));
    })
  );

  const captures = [];
  for (let i = 0; i < slideHandles.length; i += 1) {
    const buffer = await slideHandles[i].screenshot({ type: "png" });
    captures.push({
      index: i,
      filename: `slide-${String(i + 1).padStart(2, "0")}.png`,
      dataUrl: `data:image/png;base64,${buffer.toString("base64")}`,
    });
  }

  await page.evaluate(() => {
    document.documentElement.classList.remove("pptx-capture-mode");
  });

  return captures;
}

async function applyDecorationFlatten({ page, captures, width, height }) {
  const items = captures.map(capture => ({
    index: capture.index,
    dataUrl: capture.dataUrl,
  }));

  await page.evaluate(
    payload => {
      const slides = Array.from(document.querySelectorAll(".slide"));
      payload.items.forEach(item => {
        const slide = slides[item.index];
        if (!slide) return;
        const existing = slide.querySelector(":scope > img.pptx-bg");
        if (existing) existing.remove();
        const bg = document.createElement("img");
        bg.className = "pptx-bg";
        bg.src = item.dataUrl;
        bg.setAttribute("alt", "");
        bg.setAttribute("aria-hidden", "true");
        bg.setAttribute("data-pptx-bg-capture", "true");
        // Explicit pixel sizing so dom-to-pptx never sees a 0x0 rect.
        bg.style.width = payload.width + "px";
        bg.style.height = payload.height + "px";
        slide.insertBefore(bg, slide.firstChild);
      });
      document.documentElement.classList.add("pptx-export-flat");
    },
    { items, width, height }
  );

  await page.waitForFunction(() => {
    const bgs = Array.from(document.querySelectorAll("img.pptx-bg"));
    if (!bgs.length) return false;
    return bgs.every(img => img.complete && img.naturalWidth > 0);
  });
}

module.exports = {
  injectCaptureStyles,
  markDecorationNodes,
  captureSlideBackgrounds,
  applyDecorationFlatten,
};
