"use strict";

// The converter picks a declared family, even when Chromium rendered a local
// fallback. Resolve the actual fonts for every controlled layout before export
// so a missing display font does not change their geometry in PowerPoint.
async function resolveExpressiveExportFonts(page) {
  // Registered layouts also contain generated text such as timeline indices
  // and page numbers, which have no editable property marker.
  const selector = '#deck-root > .slide *';
  if (await page.locator(selector).count() === 0) return { resolved: 0, warnings: [] };
  let client;
  let resolved = 0;
  const warnings = [];
  try {
    client = await page.context().newCDPSession(page);
    await client.send("DOM.enable");
    await client.send("CSS.enable");
    const { root } = await client.send("DOM.getDocument");
    const { nodeIds } = await client.send("DOM.querySelectorAll", { nodeId: root.nodeId, selector });
    for (const nodeId of nodeIds) {
      const { fonts } = await client.send("CSS.getPlatformFontsForNode", { nodeId });
      // A downloaded web font is not necessarily available in the slide viewer.
      // Keep its authored family and disclose that portability remains unproven.
      if (fonts.some(font => font.isCustomFont)) {
        warnings.push("A web font still requires installation in the PPTX viewer.");
        continue;
      }
      const families = [...new Set(fonts.filter(font => font.glyphCount > 0)
        .sort((left, right) => right.glyphCount - left.glyphCount)
        .map(font => font.familyName).filter(Boolean))];
      if (!families.length) continue;
      const { object } = await client.send("DOM.resolveNode", { nodeId });
      try {
        await client.send("Runtime.callFunctionOn", {
          objectId: object.objectId,
          functionDeclaration: "function (families) { this.style.fontFamily = families.map(name => JSON.stringify(name)).join(', '); }",
          arguments: [{ value: families }],
        });
        resolved += 1;
      } finally {
        await client.send("Runtime.releaseObject", { objectId: object.objectId });
      }
    }
  } catch (error) {
    warnings.push(`Actual font resolution unavailable: ${error.message}`);
  } finally {
    if (client) await client.detach().catch(() => {});
  }
  return { resolved, warnings: [...new Set(warnings)] };
}

module.exports = { resolveExpressiveExportFonts };
