# Editing PPTX Office Open XML

Use direct OOXML editing only when normal libraries cannot preserve or express the requested change. A `.pptx` file is a zip package. Make a copy, inspect relationships carefully, and validate after each structural change.

## Important Package Parts

| Path | Purpose |
|---|---|
| `[Content_Types].xml` | Declares package part content types |
| `ppt/presentation.xml` | Slide order and global presentation settings |
| `ppt/_rels/presentation.xml.rels` | Relationships from the presentation to slides, masters, themes, and metadata |
| `ppt/slides/slideN.xml` | Slide content |
| `ppt/slides/_rels/slideN.xml.rels` | Slide relationships to layouts, images, charts, notes, hyperlinks, and media |
| `ppt/slideMasters/` | Master slide definitions |
| `ppt/slideLayouts/` | Layout definitions used by slides |
| `ppt/media/` | Images and media |
| `ppt/charts/` | Native chart XML |
| `ppt/notesSlides/` | Speaker notes |

## Safe Editing Rules

- Never edit the original file in place.
- Preserve namespaces exactly. Use XML-aware tools such as `lxml` or `defusedxml` for parsing.
- When adding or deleting slides, update all of:
  - `ppt/presentation.xml`
  - `ppt/_rels/presentation.xml.rels`
  - `[Content_Types].xml`
  - any slide relationships and referenced media/chart/notes parts
- Never emit negative geometry extents for line-like shapes. Normalize start/end coordinates first and write non-negative `a:off`/`a:ext` dimensions (`w = abs(x2-x1)`, `h = abs(y2-y1)`, `x = min(x1,x2)`, `y = min(y1,y2)`).
- Relationship IDs are local to the `.rels` file that contains them. Do not assume `rId5` has the same meaning in another file.
- Remove unused media and chart parts only after confirming no remaining `.rels` file references them.
- Treat notes and comments as user content. Preserve them unless deletion is requested.

## Text Replacement

Text usually lives in `a:t` nodes inside slide XML. Replace only exact target text, preserving run properties when possible.

- For simple replacement, modify text node values without changing surrounding `a:rPr` formatting.
- For paragraphs or lists, preserve paragraph properties (`a:pPr`) and run properties (`a:rPr`) from neighboring content.
- Watch for text split across multiple runs. Extract text first, then inspect the XML around the target.

## Validation

After repacking:

1. Run `scripts/validate_pptx_package.py`.
2. Open or render with LibreOffice when available.
3. Extract text and compare expected slide order.
4. Inspect rendered images for visual regressions when rendering is available.
   If not available, report visual verification as blocked.

If PowerPoint repairs the file on open, the package is not healthy even if a zip check passes.
