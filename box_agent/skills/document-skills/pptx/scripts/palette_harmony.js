"use strict";

const presets = require("./premium_palettes.json");

function normalizeHex(value) {
  const text = String(value || "").trim().toUpperCase();
  return /^#[0-9A-F]{6}$/.test(text) ? text : null;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function hexToHsl(value) {
  const hex = normalizeHex(value);
  if (!hex) return null;
  const channels = [1, 3, 5].map(index => parseInt(hex.slice(index, index + 2), 16) / 255);
  const max = Math.max(...channels);
  const min = Math.min(...channels);
  const lightness = (max + min) / 2;
  if (max === min) return { h: 0, s: 0, l: lightness };
  const delta = max - min;
  const saturation = lightness > 0.5 ? delta / (2 - max - min) : delta / (max + min);
  let hue;
  if (max === channels[0]) hue = ((channels[1] - channels[2]) / delta) % 6;
  else if (max === channels[1]) hue = (channels[2] - channels[0]) / delta + 2;
  else hue = (channels[0] - channels[1]) / delta + 4;
  return { h: (hue * 60 + 360) % 360, s: saturation, l: lightness };
}

function hslToHex({ h, s, l }) {
  const hue = ((Number(h) % 360) + 360) % 360 / 360;
  const saturation = clamp(Number(s), 0, 1);
  const lightness = clamp(Number(l), 0, 1);
  const chroma = (1 - Math.abs(2 * lightness - 1)) * saturation;
  const x = chroma * (1 - Math.abs((hue * 6) % 2 - 1));
  const match = lightness - chroma / 2;
  const section = hue * 6;
  const rgb = section < 1 ? [chroma, x, 0]
    : section < 2 ? [x, chroma, 0]
      : section < 3 ? [0, chroma, x]
        : section < 4 ? [0, x, chroma]
          : section < 5 ? [x, 0, chroma]
            : [chroma, 0, x];
  return `#${rgb.map(channel => Math.round((channel + match) * 255).toString(16).padStart(2, "0")).join("").toUpperCase()}`;
}

function contrastRatio(left, right) {
  const luminance = value => {
    const hex = normalizeHex(value);
    if (!hex) return null;
    const channels = [1, 3, 5].map(index => parseInt(hex.slice(index, index + 2), 16) / 255)
      .map(channel => channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  };
  const a = luminance(left); const b = luminance(right);
  if (a == null || b == null) return null;
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

function readableText(background) {
  return contrastRatio("#FFFFFF", background) >= contrastRatio("#111111", background) ? "#FFFFFF" : "#111111";
}

function deriveHarmony(primary, options = {}) {
  const base = normalizeHex(primary);
  if (!base) throw new Error("palette_harmony: primary must be a #RRGGBB color");
  const hsl = hexToHsl(base);
  const dark = hsl.l < 0.45;
  const background = options.background && normalizeHex(options.background)
    ? normalizeHex(options.background)
    : (dark ? "#101418" : "#F7F5EF");
  const text = options.text && normalizeHex(options.text) ? normalizeHex(options.text) : readableText(background);
  const accent = hslToHex({ h: hsl.h + 32, s: clamp(Math.max(hsl.s, 0.42), 0, 0.8), l: dark ? 0.66 : 0.46 });
  const secondary = hslToHex({ h: hsl.h + 190, s: clamp(Math.max(hsl.s * 0.72, 0.28), 0, 0.68), l: dark ? 0.62 : 0.42 });
  return {
    background, text, primary: base, accent, secondary,
    accent_usage: options.accent_usage || "sparse",
    source: "color-harmony",
    requested: [base],
    contrast_checked: contrastRatio(background, text) >= 4.5,
  };
}

function findPremiumPalette(query) {
  const text = String(query || "").trim().toLowerCase();
  if (!text) return null;
  return presets.find(preset => preset.id.toLowerCase() === text)
    || presets.find(preset => [...preset.mood, preset.name].some(value => String(value).toLowerCase().includes(text)))
    || null;
}

function listPremiumPalettes() {
  return presets.map(item => ({ ...item }));
}

module.exports = { contrastRatio, deriveHarmony, findPremiumPalette, hexToHsl, hslToHex, listPremiumPalettes, normalizeHex };
