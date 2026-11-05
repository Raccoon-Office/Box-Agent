(function () {
  "use strict";

  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  const selector = ".slide [data-deck-text-fit]";

  function textLines(element) {
    const lines = [];
    const scale = element.getBoundingClientRect().width / element.offsetWidth || 1;
    const tolerance = parseFloat(getComputedStyle(element).fontSize) * scale * 0.45;
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      let offset = 0;
      for (const char of node.textContent) {
        const range = document.createRange();
        range.setStart(node, offset);
        offset += char.length;
        range.setEnd(node, offset);
        if (!char.trim()) continue;
        const rect = range.getBoundingClientRect();
        if (!rect.width || !rect.height) continue;
        const center = (rect.top + rect.bottom) / 2;
        let line = lines.find(item => Math.abs(item.center - center) < tolerance);
        if (!line) {
          line = { center, text: "", left: rect.left, right: rect.right,
            top: rect.top, bottom: rect.bottom };
          lines.push(line);
        }
        line.text += char;
        line.left = Math.min(line.left, rect.left);
        line.right = Math.max(line.right, rect.right);
        line.top = Math.min(line.top, rect.top);
        line.bottom = Math.max(line.bottom, rect.bottom);
      }
    }
    return lines.sort((a, b) => a.center - b.center);
  }

  function isOrphan(lines) {
    if (lines.length < 2) return false;
    const last = lines[lines.length - 1];
    const visible = Array.from(last.text.replace(/[\s\p{P}]/gu, ""));
    return visible.length < 2;
  }

  function fits(element, lines) {
    const box = element.getBoundingClientRect();
    let parentElement = element.parentElement;
    while (parentElement && getComputedStyle(parentElement).display === "contents") {
      parentElement = parentElement.parentElement;
    }
    const slide = element.closest(".slide").getBoundingClientRect();
    const scale = box.width / element.offsetWidth || 1;
    const style = getComputedStyle(element);
    const right = Math.min(box.right - parseFloat(style.paddingRight) * scale, slide.right);
    // Font ascent boxes can extend above the CSS line box without clipping.
    // Use layout overflow for capacity, not that harmless ascent difference.
    if (element.scrollWidth > element.clientWidth + 1
      || element.scrollHeight > element.clientHeight + 12
      || (parentElement && element.offsetHeight > parentElement.clientHeight + 12)) return false;
    return lines.every(line => line.left >= slide.left - scale * 2
      && line.right <= right + scale
      && line.top >= slide.top - scale * 2
      && line.bottom <= slide.bottom + scale);
  }

  function fit(element) {
    if (!element.offsetWidth || !element.textContent.trim()) return;
    // Restore the theme's preferred size before each measurement, including
    // after reopening a saved HTML file or replacing long copy with short copy.
    element.removeAttribute("data-deck-fit-applied");
    const profile = element.dataset.deckFontProfile;
    const profileSize = window.__deckLayoutRegistry?.preferredTextSize(element.textContent, profile);
    if (profileSize) element.style.setProperty(
      element.dataset.deckTextFit === "metric" ? "--expressive-value-size" : "--expressive-title-size",
      `${profileSize}px`,
    );
    const style = getComputedStyle(element);
    const preferred = parseFloat(style.fontSize);
    const minimum = Math.min(preferred, element.dataset.deckTextFit === "metric" ? 24
      : element.dataset.deckTextFit === "copy" ? 20 : 32);
    const width = element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    if (!context || width <= 0 || !Number.isFinite(preferred)) return;
    context.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
    const text = element.textContent.replace(/\s+/g, " ").trim();
    const spacing = parseFloat(style.letterSpacing) || 0;
    const measured = context.measureText(text).width + spacing * Math.max(0, Array.from(text).length - 1);
    const singleLine = preferred * width / Math.max(1, measured) * 0.98;
    let size = preferred;
    // A modest reduction keeps short titles intact. Longer headings retain
    // multiple balanced lines instead of being compressed into tiny type.
    if (element.dataset.deckTextFit === "metric" || singleLine >= preferred * 0.8) {
      size = Math.min(preferred, singleLine);
    }
    size = Math.max(minimum, Math.floor(size * 4) / 4);
    let lines = [];
    for (let attempt = 0; attempt < 40; attempt += 1) {
      element.style.setProperty("--deck-fit-font-size", `${size}px`);
      element.setAttribute("data-deck-fit-applied", "");
      lines = textLines(element);
      if (fits(element, lines) && !isOrphan(lines)) break;
      if (size <= minimum) break;
      size = Math.max(minimum, Math.floor(size * 0.94 * 4) / 4);
    }
    element.dataset.deckTextFitState = fits(element, lines) && !isOrphan(lines) ? "fit" : "overflow";
  }

  function refresh(scope = document) {
    scope.querySelectorAll(selector).forEach(fit);
  }

  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; refresh(); });
  }

  window.__deckTextFit = { refresh, textLines };
  refresh();
  window.__deckTextReady = Promise.resolve(document.fonts && document.fonts.ready).then(() => refresh());
  if (document.fonts) document.fonts.addEventListener("loadingdone", schedule);
  window.addEventListener("resize", schedule);
  window.addEventListener("box-agent:deck-present", schedule);
})();
