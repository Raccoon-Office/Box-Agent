(function () {
  "use strict";

  const root = document.getElementById("deck-root");
  if (!root) return;
  const NS = "http://www.w3.org/2000/svg";
  const FRAME_SELECTOR = [
    ".content-card", ".comparison-column", ".kpi-card", ".timeline-step",
    ".text-section", ".proof-stack", ".image-feature-media", ".image-hero-media",
    ".technical-diagram-stage", ".chart-body", ".data-table-wrap",
  ].join(",");

  function addPath(svg, d, role, color, width = 3, opacity = 1) {
    const path = document.createElementNS(NS, "path");
    Object.entries({ d, fill: "none", stroke: color, "stroke-width": width,
      "stroke-linecap": "round", "stroke-linejoin": "round", opacity,
      "data-sketch-role": role }).forEach(([key, value]) => path.setAttribute(key, value));
    svg.appendChild(path);
  }

  function refresh() {
    const slides = document.querySelectorAll("#deck-root > .slide, .deck-thumbnail-canvas > .slide");
    slides.forEach(slide => {
      slide.querySelectorAll(":scope > .sketch-overlay").forEach(node => node.remove());
      slide.removeAttribute("data-sketch-ready");
    });
    if (document.body.dataset.deckVoice !== "sketch"
      || document.body.dataset.deckStyleDecorations === "off") return;

    slides.forEach(slide => {
      const bounds = slide.getBoundingClientRect();
      const width = slide.offsetWidth;
      const height = slide.offsetHeight;
      if (!width || !height || !bounds.width || !bounds.height) return;
      const tokens = getComputedStyle(slide);
      const ink = tokens.getPropertyValue("--deck-text").trim() || "#27333A";
      const marker = tokens.getPropertyValue("--deck-primary-soft").trim() || "#F7EBAC";
      const accent = tokens.getPropertyValue("--deck-primary").trim() || ink;
      const svg = document.createElementNS(NS, "svg");
      svg.setAttribute("class", "sketch-overlay");
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("data-pptx-decoration", "");

      const rect = element => {
        const box = element.getBoundingClientRect();
        return { x: (box.left - bounds.left) * width / bounds.width,
          y: (box.top - bounds.top) * height / bounds.height,
          w: box.width * width / bounds.width, h: box.height * height / bounds.height };
      };
      slide.querySelectorAll(FRAME_SELECTOR).forEach((element, index) => {
        const b = rect(element);
        if (slide.classList.contains("open-structured") && element.matches(".kpi-card, .technical-diagram-stage, .chart-body, .data-table-wrap")) return;
        if (b.w < 20 || b.h < 20) return;
        // Fixed offsets keep every refresh, editor save and export reproducible.
        [0, 1.6].forEach((offset, pass) => {
          const x = b.x + 4 + offset, y = b.y + 4 + offset;
          const w = b.w - 9, h = b.h - 9;
          const bend = index % 2 ? -5 : 5;
          addPath(svg, `M ${x + 2} ${y + 1} Q ${x + w * .45} ${y + bend} ${x + w - 2} ${y}
            Q ${x + w + bend} ${y + h * .45} ${x + w} ${y + h - 2}
            Q ${x + w * .55} ${y + h + bend} ${x + 1} ${y + h}
            Q ${x - bend} ${y + h * .55} ${x + 2} ${y + 1}`,
          "frame", ink, pass ? 1.4 : 2.8, pass ? .55 : .9);
        });
      });

      const title = slide.querySelector("h1, .slide-header h2, .open-header h2");
      if (title) {
        const b = rect(title);
        const end = b.x + Math.min(b.w * .8, 420);
        addPath(svg, `M ${b.x + 3} ${b.y + b.h + 8} Q ${(b.x + end) / 2} ${b.y + b.h + 3} ${end} ${b.y + b.h + 7}`,
          "highlight", marker, 14, .8);
        addPath(svg, `M ${b.x + 4} ${b.y + b.h + 14} Q ${(b.x + end) / 2} ${b.y + b.h + 11} ${end - 12} ${b.y + b.h + 15}`,
          "underline", accent, 2);
      }
      slide.querySelectorAll(".card-index, .timeline-marker, .open-route-dot").forEach(element => {
        const b = rect(element), cx = b.x + b.w / 2, cy = b.y + b.h / 2;
        if (!b.w || !b.h) return;
        const rx = b.w / 2 + 5, ry = b.h / 2 + 3;
        addPath(svg, `M ${cx - rx} ${cy} C ${cx - rx - 2} ${cy - ry} ${cx + rx} ${cy - ry - 2} ${cx + rx} ${cy}
          C ${cx + rx + 2} ${cy + ry} ${cx - rx} ${cy + ry + 2} ${cx - rx} ${cy}`,
        "circle", accent, 2);
      });
      slide.querySelectorAll(".comparison-arrow").forEach(element => {
        const b = rect(element), x = b.x + 10, y = b.y + b.h / 2, end = b.x + b.w - 10;
        if (b.w < 20 || !b.h) return;
        addPath(svg, `M ${x} ${y + 3} Q ${(x + end) / 2} ${y - 8} ${end} ${y}
          M ${end - 13} ${y - 10} L ${end} ${y} L ${end - 15} ${y + 9}`, "arrow", ink, 3);
      });
      slide.querySelectorAll(".open-route-rail").forEach(element => {
        const b = rect(element);
        if (!b.w || !b.h) return;
        const step = element.closest(".open-route-step");
        const startDot = step?.querySelector(".open-route-dot");
        const endDot = step?.nextElementSibling?.querySelector(".open-route-dot");
        if (!startDot || !endDot) return;
        const first = rect(startDot), last = rect(endDot);
        const start = first.x + first.w + 6, end = last.x - 8;
        addPath(svg, `M ${start} ${b.y} Q ${(start + end) / 2} ${b.y - 4} ${end} ${b.y + 1}
          M ${end - 12} ${b.y - 7} L ${end} ${b.y + 1} L ${end - 12} ${b.y + 8}`, "route", ink, 2);
      });
      // Annotations follow the content's role. They carry no invented scores,
      // completion state or decorative relationships between parallel concepts.
      slide.querySelectorAll(".open-point-title, .open-side-title, .open-action-title").forEach(element => {
        if (!element.textContent.trim() || !element.offsetWidth) return;
        const range = document.createRange();
        range.selectNodeContents(element);
        const box = range.getBoundingClientRect();
        const b = { x: (box.left - bounds.left) * width / bounds.width,
          y: (box.top - bounds.top) * height / bounds.height,
          w: box.width * width / bounds.width, h: box.height * height / bounds.height };
        if (!b.w || !b.h || b.y + b.h > height - 48) return;
        if (element.classList.contains("open-point-title") && Array.from(element.textContent).length <= 12) {
          addPath(svg, `M ${b.x - 8} ${b.y + b.h * .55} C ${b.x - 8} ${b.y - 8} ${b.x + b.w + 12} ${b.y - 6} ${b.x + b.w + 10} ${b.y + b.h * .5}
            C ${b.x + b.w + 14} ${b.y + b.h + 8} ${b.x - 12} ${b.y + b.h + 10} ${b.x - 8} ${b.y + b.h * .55}`, "concept-circle", accent, 2.2);
        } else {
          const end = b.x + Math.min(b.w, 560);
          addPath(svg, `M ${b.x} ${b.y + b.h + 4} Q ${(b.x + end) / 2} ${b.y + b.h + 9} ${end} ${b.y + b.h + 3}`, "content-underline", accent, 2.5);
        }
      });
      // A direct slide child survives the existing decoration-only background capture.
      slide.appendChild(svg);
      slide.setAttribute("data-sketch-ready", "true");
    });
  }

  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; refresh(); });
  }
  window.addEventListener("box-agent:deck-change", schedule);
  window.addEventListener("box-agent:deck-present", schedule);
  window.addEventListener("box-agent:deck-present-slide", schedule);
  window.addEventListener("resize", schedule);
  root.addEventListener("load", event => { if (event.target.tagName === "IMG") schedule(); }, true);
  refresh();
  if (document.fonts) document.fonts.ready.then(schedule);
})();
