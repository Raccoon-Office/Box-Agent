(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.__deckPresentation = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // A theme declares its identity; this shared system supplies the scale and
  // spatial behavior. New themes inherit these rules without an id-specific CSS.
  const STYLE_VALUES = Object.freeze({
    canvas: ["solid", "grid", "dots", "paper", "pixel", "gradient", "window"],
    surface: ["soft", "outline", "hard", "pill", "paper", "window", "note"],
    shadow: ["none", "soft", "hard", "glow"],
    heading: ["standard", "editorial", "poster", "condensed", "italic", "pixel", "handwritten", "stencil"],
    label: ["plain", "pill", "boxed", "mono", "tape"],
    accent: ["line", "block", "underline", "bracket", "dot"],
    alternation: ["none", "section"],
  });
  const STYLE_DEFAULTS = Object.freeze({ canvas: "solid", surface: "soft", shadow: "none",
    heading: "standard", label: "plain", accent: "line", alternation: "none" });

  function resolveTheme(theme, selectedFamily = null) {
    const style = {};
    Object.entries(STYLE_VALUES).forEach(([key, allowed]) => {
      style[key] = allowed.includes(theme?.style?.[key]) ? theme.style[key] : STYLE_DEFAULTS[key];
    });
    const family = selectedFamily || theme?.composition?.default_family || "institutional-grid";
    const voice = style.heading === "handwritten" ? "sketch"
      : style.heading === "pixel" ? "digital"
        : ["poster", "condensed", "stencil"].includes(style.heading) ? "bold"
          : ["editorial", "italic"].includes(style.heading) ? "editorial"
            : family === "playful-collage" ? "playful"
              : family === "technical-schematic" ? "technical" : "measured";
    const rhythm = ["editorial-spread", "literary-minimal"].includes(family) ? "editorial"
      : ["technical-schematic", "analytical-exhibit"].includes(family) ? "analytical"
        : ["poster-asymmetric", "brutalist-frame", "cinematic-canvas"].includes(family) ? "impact"
          : ["retro-interface", "product-showcase"].includes(family) ? "interface"
            : family === "playful-collage" ? "collage" : "balanced";
    return { version: 1, voice, rhythm, style };
  }

  function fieldAtPath(fields, path) {
    let field = { type: "object", shape: fields };
    for (const key of String(path).split(".")) {
      field = field?.type === "array" ? field.itemShape : (field?.shape || field)?.[key];
      if (!field) return null;
    }
    return field;
  }

  function preferredComposition(layout) {
    const values = layout?.fields?.composition?.values || [];
    return ["open", "editorial", "poster"].find(value => values.includes(value))
      || layout?.defaultProps?.composition;
  }

  function measureContent(props, fields) {
    let total = 0, longestBody = 0, longestHeading = 0, longestItem = 0, longestItemHeading = 0, collectionSize = 0;
    function visit(value, field, path = "") {
      if (field?.type === "text" && typeof value === "string") {
        // Identifiers are not presentation copy, and media are counted as areas.
        if (/(?:^|\.)(?:id|src|source|target)$/.test(path)) return 0;
        const size = Array.from(value.replace(/\s+/g, "")).length;
        if (["heading", "display"].includes(field.role)) longestHeading = Math.max(longestHeading, size);
        if (field.role === "heading" && /\.\d+\./.test(path)) longestItemHeading = Math.max(longestItemHeading, size);
        if (["body", "lead"].includes(field.role)) longestBody = Math.max(longestBody, size);
        total += size;
        return size;
      }
      if (Array.isArray(value) && field?.type === "array") {
        collectionSize = Math.max(collectionSize, value.length);
        return value.reduce((sum, item, index) => {
          const size = visit(item, field.itemShape, `${path}.${index}`);
          if (item && typeof item === "object" && !Array.isArray(item)) longestItem = Math.max(longestItem, size);
          return sum + size;
        }, 0);
      }
      if (value && typeof value === "object" && field && field.type !== "media") {
        const shape = field.shape || field;
        return Object.entries(value).reduce((sum, [key, item]) => sum + visit(item, shape[key], `${path}.${key}`), 0);
      }
      return 0;
    }
    visit(props, fields);
    const headerCharacters = ["title", "statement", "subtitle", "eyebrow"]
      .reduce((sum, key) => sum + Array.from(String(props?.[key] || "").replace(/\s+/g, "")).length, 0);
    // Multi-row metrics need room for label, value and detail together, even
    // when their copy is short. The sparse type scale exceeds that row height.
    const multiRowMetrics = fields?.items?.itemShape?.value?.role === "metric" && collectionSize >= 4;
    const density = !multiRowMetrics && total <= 240 && longestBody <= 72 && longestHeading <= 28 && longestItem <= 90 && collectionSize <= 6
      ? "sparse"
      : total <= 520 && longestBody <= 120 && longestHeading <= 40 && longestItem <= 120 && collectionSize <= 7
        ? "regular" : "dense";
    return { density, characters: total, headerCharacters, longestBody, longestHeading, longestItem, collectionSize,
      shortItems: longestItemHeading <= 12 && longestItem <= 64,
      briefItems: collectionSize === 3 && headerCharacters <= 72 && longestItemHeading <= 6 && longestItem <= 24 };
  }

  return { VERSION: 1, STYLE_VALUES, resolveTheme, fieldAtPath, measureContent, preferredComposition };
});
