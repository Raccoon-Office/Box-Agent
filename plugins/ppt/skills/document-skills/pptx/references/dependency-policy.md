# Dependency Policy

## 1. Scope

1. This document is for runtime setup and dependency installation decisions in this skill.
2. `scripts/setup_check.py` is diagnostic only and never a gate to install globally.

## 2. Hard constraints

1. No Homebrew/system package installation unless explicitly requested.
2. No global npm/pip/package-manager install for this workflow by default.
3. No per-session `mnt/<session-id>` dependency installs unless temporary execution explicitly requires it.
4. Keep all installs inside Office Raccoon managed prefixes.

## 3. NPM / Python policy

1. Install `pptxgenjs` only in managed Office Raccoon prefix when native path is required.
2. For HTML export, do not install `dom-to-pptx`; use bundled `scripts/dom-to-pptx.bundle.js`.
3. Preferred install targets are managed Office Raccoon prefixes in `${BOX_AGENT_NODE_PREFIX}` or `${BOX_AGENT_RUNTIME_PREFIX}` (platform default is supplied by environment/runtime; do not hardcode one OS).
4. Use `pip`/Python packages only through managed environment path when missing helpers are required.

## 4. Fallback behavior

1. If `soffice` is missing, still run package and text checks.
2. Report Quick Look limitations explicitly when rendering is partial.
3. For HTML export blockers, do not fallback to weaker generators; use route confirmation.

## 5. QA implications

1. Installing dependencies does not replace package validation.
2. Installing render libraries does not replace `scripts/render_pptx.py`.
3. Dependency missing state must be reported with command examples and exact blocker.
