# Shell Safety

## 1. Blocked command patterns

1. Do not use `rm -rf`.
1. Do not use shell redirects for outputs such as `>` or `>>`.
1. Do not redirect to `/dev/null` for probing.
1. Do not use inline heredocs like `python3 - <<'PY'`.
1. Do not run `sed` with absolute path-like substitutions.
1. Do not run long mixed shell chains mixing `unzip`, `cat`, and inline Python.

## 2. Safer command pattern

1. Use helper scripts with explicit output arguments.
1. Pass paths through command arguments arrays.
1. Use explicit `--out` options for render and report outputs.
1. For archive reads, use Python zip helpers or argument-array process calls.

## 3. Workspace boundary

1. Keep all intermediate files under `<PRESENTATION_DIR>` selected in the
   conversation; do not add an automatic nested `output/` directory.
1. Do not write to `/tmp`, `/var/tmp`, or absolute temp paths.
1. Avoid `absolute/paths` in redirected outputs.

## 4. Temporary helpers

1. For custom logic, write a short `.py` or `.js` helper file, then execute it.
1. For render checks, call official helper scripts directly.
1. Do not implement bypass checks outside official helper scripts.
