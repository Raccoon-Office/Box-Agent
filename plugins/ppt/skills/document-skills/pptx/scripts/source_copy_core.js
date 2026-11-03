"use strict";

function normalizedCopy(value) {
  return String(value || "").normalize("NFKC").toLowerCase()
    .replace(/[\s\p{P}\p{S}]+/gu, "");
}

// Recover original wording, never accept the paraphrase as new evidence. The
// narrow comparison tolerates at most two grammatical characters (的 / 到的),
// without substituting numbers, negation or other words.
function resolveSourceCopy(candidate, sourceText) {
  const normalized = normalizedCopy(candidate);
  const key = normalized.replace(/到?的/g, "");
  if (Array.from(key).length < 4 || normalized.length - key.length > 2) return null;
  const source = String(sourceText || "");
  const entries = [];
  let offset = 0;
  for (const char of source) {
    for (const value of normalizedCopy(char)) {
      entries.push({ value, start: offset, end: offset + char.length });
    }
    offset += char.length;
  }
  const filtered = entries.filter((entry, index) => entry.value !== "的"
    && !(entry.value === "到" && entries[index + 1]?.value === "的"));
  const sourceKey = filtered.map(entry => entry.value).join("");
  const matchOffset = sourceKey.indexOf(key);
  if (matchOffset < 0) return null;
  const start = Array.from(sourceKey.slice(0, matchOffset)).length;
  const match = filtered.slice(start, start + Array.from(key).length);
  let end = match[match.length - 1].end;
  const begin = match[0].start;
  const prefix = normalizedCopy(source.slice(0, begin));
  if (/(?:不|未|非|无|没|没有|未经|待)$/.test(prefix)
    || (/[\dA-Za-z]$/.test(prefix) && /^[\dA-Za-z]/.test(key))) return null;
  // Include a closing bracket only when its opening bracket is in the span.
  for (const [opening, closing] of [["（", "）"], ["(", ")"], ["【", "】"]]) {
    const span = source.slice(begin, end);
    if (source[end] === closing && span.split(opening).length > span.split(closing).length) end += 1;
  }
  const original = source.slice(begin, end);
  const originalNormalized = normalizedCopy(original);
  if (originalNormalized.replace(/到?的/g, "") !== key
    || normalized.length + originalNormalized.length - 2 * key.length > 2) return null;
  return original;
}

module.exports = { resolveSourceCopy };
