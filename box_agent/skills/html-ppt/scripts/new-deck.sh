#!/usr/bin/env bash
# html-ppt :: new-deck.sh — scaffold a new deck from templates/deck.html
#
# Usage:
#   new-deck.sh <name> [output-parent-dir]
#
# Creates <parent>/<name>/index.html with paths rewritten to point at the
# skill's shared assets/themes/animations. Defaults to ./examples/.

set -euo pipefail

NAME="${1:-}"
if [[ -z "$NAME" ]]; then
  echo "usage: new-deck.sh <name> [parent-dir]" >&2
  exit 1
fi

PARENT="${2:-examples}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$HERE/templates/deck.html"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "error: template not found at $TEMPLATE" >&2
  exit 1
fi

OUT_DIR="$HERE/$PARENT/$NAME"
if [[ -e "$OUT_DIR" ]]; then
  echo "error: $OUT_DIR already exists" >&2
  exit 1
fi
mkdir -p "$OUT_DIR"

# Rewrite all asset references to ./assets/ so the output folder is self-contained.
sed 's|href="../assets/|href="./assets/|g; s|src="../assets/|src="./assets/|g; s|data-theme-base="../assets/|data-theme-base="./assets/|g' \
  "$TEMPLATE" > "$OUT_DIR/index.html"

# Copy the shared assets into the output folder so the deck is fully deliverable.
cp -r "$HERE/assets" "$OUT_DIR/assets"

echo "✔ created $OUT_DIR/index.html"
echo "✔ copied assets → $OUT_DIR/assets/"
echo ""
echo "next steps:"
echo "  open  $OUT_DIR/index.html"
echo "  # press T to cycle themes, ← → to navigate, O for overview"
echo ""
echo "  # render to PNG:"
echo "  $HERE/scripts/render.sh $OUT_DIR/index.html all"
