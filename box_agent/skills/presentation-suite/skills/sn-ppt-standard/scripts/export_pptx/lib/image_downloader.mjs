import { readFileSync } from 'node:fs';

// Preserve the caller contract, but never fetch or rewrite remote media offline.
export async function downloadRemoteImages(deckDir, htmlFiles) {
  for (const htmlPath of htmlFiles) {
    const html = readFileSync(htmlPath, 'utf-8');
    const remote = /<(?:img|script|iframe|source|video|audio)\b[^>]*\bsrc\s*=\s*["'](?:https?:)?\/\//i.test(html)
      || /<link\b[^>]*\bhref\s*=\s*["'](?:https?:)?\/\//i.test(html)
      || /url\(\s*["']?(?:https?:)?\/\//i.test(html)
      || /@import\s+["'](?:https?:)?\/\//i.test(html);
    if (remote) throw new Error(`Offline package: replace remote resources with local files in ${htmlPath}`);
  }
}
