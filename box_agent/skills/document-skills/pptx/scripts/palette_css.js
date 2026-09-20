"use strict";

// Compile legacy theme color literals onto the frozen palette. Geometry,
// typography and selectors remain theme-owned; image pixels are untouched.
const colorPattern = /#[\da-f]{8}\b|#[\da-f]{6}\b|#[\da-f]{4}\b|#[\da-f]{3}\b|rgba?\([^)]*\)|\b(?:white|black)\b/gi;
function rgb(value) {
  if (/^white$/i.test(value)) return [255, 255, 255, 1];
  if (/^black$/i.test(value)) return [0, 0, 0, 1];
  if (value[0] === "#") {
    let hex = value.slice(1);
    if (hex.length <= 4) hex = [...hex].map(c => c + c).join("");
    return [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16))
      .concat(hex.length === 8 ? parseInt(hex.slice(6), 16) / 255 : 1);
  }
  const values = value.match(/[\d.]+%?/g) || [];
  if (values.length < 3) return null;
  return values.map((part, i) => parseFloat(part) * (part.endsWith("%") ? (i === 3 ? 0.01 : 2.55) : 1))
    .concat(values.length === 3 ? [1] : []);
}
function distance(a, b) { return a.slice(0, 3).reduce((sum, x, i) => sum + (x - b[i]) ** 2, 0); }
function bindMixes(value, replacement) {
  let start;
  while ((start = value.indexOf("color-mix(")) !== -1) {
    let depth = 1, end = start + "color-mix(".length;
    while (end < value.length && depth) {
      if (value[end] === "(") depth += 1;
      if (value[end] === ")") depth -= 1;
      end += 1;
    }
    if (depth) break;
    value = value.slice(0, start) + replacement + value.slice(end);
  }
  return value;
}
function bindPaletteCss(css, theme, contract) {
  if (contract?.palette?.version !== 2) return css;
  const tokens = contract.palette.tokens;
  const allowed = [...new Set(Object.values(tokens).flat().filter(x => typeof x === "string" && x.startsWith("#")))];
  const pairs = Object.entries(theme.palette).flatMap(([role, source]) => {
    const from = Array.isArray(source) ? source : [source];
    const to = Array.isArray(tokens[role]) ? tokens[role] : [tokens[role]];
    return from.map((color, i) => ({ from: typeof color === "string" ? rgb(color) : null, to: to[i] })).filter(x => x.from && x.to);
  });
  const mapColor = (value, property) => {
    const source = rgb(value);
    if (!source || source[3] === 0) return value;
    const exact = pairs.filter(pair => distance(source, pair.from) === 0);
    const preferred = property === "color" ? ["text", "primary_text", "accent_text"] : ["background", "surface", "primary"];
    const role = preferred.find(key => typeof theme.palette[key] === "string" && distance(source, rgb(theme.palette[key])) === 0);
    const target = role && tokens[role] || exact[0]?.to
      || allowed.reduce((best, color) => distance(source, rgb(color)) < distance(source, rgb(best)) ? color : best, allowed[0]);
    const channels = rgb(target);
    return source[3] < 1 ? `rgba(${channels.slice(0, 3).join(",")},${source[3]})` : target;
  };
  return css.replace(/([^{}]+)\{([^{}]*)\}/g, (rule, selector, declarations) => {
    if (/toolbar|(?:deck|chart-data|diagram|layout)-editor\b|picker|controls|thumbnail|dialog/.test(selector)) return rule;
    const bound = declarations.replace(/([\w-]+)\s*:\s*([^;{}]+)(;|$)/g, (declaration, property, value, end) => {
      if (!/^(?:--|color$|background|border|outline|box-shadow|text-shadow|fill$|stroke$)/.test(property)) return declaration;
      const aliases = { bg: "background", "base-bg": "background", "poster-bg": "background",
        "base-text": "text", "poster-text": "text", "base-muted": "muted",
        "focus-text": "primary_text", "base-emphasis": "primary_text", "accent-color": "accent" };
      const variable = property.replace(/^--deck-/, "");
      const token = aliases[variable] || variable.replace(/-/g, "_");
      if (property.startsWith("--deck-") && typeof tokens[token] === "string") return `${property}: ${tokens[token]}${end}`;
      // Preserve url() contents, including SVG/data-URI images.
      const urls = [];
      const masked = value.replace(/url\((?:"[^"]*"|'[^']*'|[^)])*\)/gi, url => `__PALETTE_URL_${urls.push(url) - 1}__`);
      const mapped = bindMixes(masked.replace(colorPattern, color => mapColor(color, property)),
        property === "color" ? tokens.muted : /border|outline/.test(property) ? tokens.border : tokens.surface)
        .replace(/__PALETTE_URL_(\d+)__/g, (_, index) => urls[Number(index)]);
      return `${property}: ${mapped}${end}`;
    });
    return `${selector}{${bound}}`;
  });
}
module.exports = { bindPaletteCss };
