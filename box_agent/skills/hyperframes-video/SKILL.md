---
name: hyperframes-video
description: HyperFrames MP4/GIF videos; motion graphics; explainer clips; 视频、短片、动图、动画成片。
keywords: [video, videos, animation, mp4, gif, hyperframes, motion-graphics, explainer-clip, 视频, 动画, 短片, 动图]
related_skills: [html-templates]
---

# HyperFrames Video

Use the host-provided HyperFrames runtime for video deliverables when it is available in the environment context.

## Non-Negotiables

- Do not say there is no video tool when the environment context says HyperFrames is `available=true`.
- Do not use `npx`, `npx --yes`, `npm install`, global installs, or a user-machine HyperFrames CLI.
- Prefer `$BOX_AGENT_NODE "$HYPERFRAMES_RUNNER_PATH"` for HyperFrames commands. The runner treats StaticGuard contract messages, browser page errors, missing local assets, and timeline registration timeouts as failures; it also finds the actual rendered MP4, copies it to the requested output path, and runs ffprobe validation.
- If `HYPERFRAMES_RUNNER_PATH` is missing, fall back to `$BOX_AGENT_NODE "$HYPERFRAMES_CLI_PATH"` and manually enforce the same checks.
- Pass the project directory (`.`) to `inspect` and `render`, not `index.html`. For `render`, omit `--composition` unless rendering a separate composition HTML file; `--composition main` is wrong because `main` is an id, not a file path.
- Keep exactly one root HTML file with `data-composition-id` in the project root. Do not leave `template-index.html`, backups, or reference copies with composition metadata beside `index.html`; put references outside the project or save them as `.txt` without composition attributes.
- Start from `$HYPERFRAMES_TEMPLATE_DIR` when it exists; it contains the stable composition contract. Treat it as the template root itself, not a templates parent. It may already end in `templates/basic-composition`, so copy it with `cp -R "$HYPERFRAMES_TEMPLATE_DIR"/. <project-dir>/` and use vendor assets from `"$HYPERFRAMES_TEMPLATE_DIR/vendor"`; do not append another `/basic-composition`.
- Write generated videos under the selected task directory or unchanged session cwd, unless the user named another path; do not infer a separate output/artifact root.
- Render with `--strict` first. If strict fails, fix the composition and retry rather than handing raw CLI errors to the user.

## Existing HTML / Webpage to Video

Use this branch when the user asks to convert an existing HTML file, folder, webpage, landing page, or "这里面的 html" into video. The goal is fidelity, not redesign.

- Preserve the original page's text, layout, assets, colors, and existing CSS/JS animation. Do not rewrite the page into a new creative composition, change button copy, invent new titles, or create a lookalike page.
- First inspect the real entry file (`index.html`, linked CSS/JS, and local assets), then open that exact page in a browser at the target viewport. If the original page renders, capture that page; do not rebuild it from memory.
- Prefer recording or screenshotting the original page with the managed Chromium/Playwright environment and encoding frames with the managed ffmpeg. This is the right fallback for normal CSS transitions, DOM animations, and non-seekable JavaScript effects.
- For direct capture, use the host-managed Node/Playwright already exposed through `NODE_PATH`; do not install anything. Launch Chromium with `process.env.BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || process.env.HYPERFRAMES_BROWSER_PATH`, record the source `file://.../index.html` with Playwright `recordVideo`, then convert the recorded WebM to MP4 with `$HYPERFRAMES_FFMPEG_PATH`.
- Use HyperFrames only as a thin capture wrapper when it preserves the original page. If an iframe/wrapper capture shows a blank page, missing fonts/images, wrong text, or only a background color, treat it as a failed render and switch to direct browser frame capture of the original HTML.
- Keep generated files small and purposeful: final MP4, a contact sheet, and minimal capture scaffolding. Do not copy an entire site tree into the final artifact unless relative asset paths require a local staging copy.
- Before reporting success, compare sampled frames against the original browser rendering. Key text, logo, buttons, hero image, and visible animation state must match the source page. A readable MP4 with changed copy/layout, blank frames, or a fake recreated scene is a failure.
- If the user explicitly asks to create new video effects from scratch, remix a still image, or design a motion graphic rather than convert an existing page, use the composition workflow below.

## Artifact Size Guardrails

- Keep the first renderable `index.html` under 5500 characters so it fits in one `write_file` call with safety margin.
- Prefer compact CSS plus JS-generated scenes from arrays over spelling out many repeated HTML blocks.
- Do not attempt a 9-scene or long-form visual treatment until a small strict-rendered MP4 exists.
- Do not copy or append text that says `[Full tool-call argument omitted from model history]`; that is a history placeholder, not file content.
- Use `append_file` only with fresh, literal content that is visibly present in the current tool call. If append fails or the file becomes partial, rewrite a smaller complete file from scratch.
- Never append after `</html>`. A HyperFrames project should have one complete `<!doctype html> ... </html>` document.

## Required Composition Contract

The renderable HTML must have one root composition element with:

```html
data-composition-id="main"
data-duration="5"
data-width="1920"
data-height="1080"
data-start="0"
```

Use a single, stable composition id unless the user needs multiple outputs. Keep the root width, height, duration, and start aligned with the rendered video. Do not omit `data-start`; strict/static guard failures for this field are blocking, even when layout sampling says there are 0 issues.

The animation timeline must be seekable:

```js
window.__timelines = window.__timelines || {};
window.__timelines.main = timeline;
window.hyperframesReady = Promise.all(
  [...document.images].map(img =>
    img.complete && img.naturalWidth > 0
      ? img.decode().catch(() => undefined)
      : new Promise((resolve, reject) => {
          img.onload = () => resolve();
          img.onerror = () => reject(new Error(`Image failed to load: ${img.src}`));
        }),
  ),
);
window.seekHyperframes = async (time) => {
  timeline.pause(time);
};
```

Register the timeline object itself, not a factory function. Do not use `window.__timelines.main = () => timeline` or a function that creates and returns a timeline unless the runtime contract explicitly asks for that shape.

If the composition uses local images, screenshots, videos, fonts, or other media, `window.hyperframesReady` must wait for those assets to load/decode. `Promise.resolve()` is only acceptable for a composition with no external assets.

When changing the composition id, change the timeline key to exactly match it.

## Workflow

1. Create a project folder, usually `hyperframes-video` or another clear task path under the session cwd or the user's explicit destination.
2. Copy the template:

```bash
mkdir -p hyperframes-video
cp -R "$HYPERFRAMES_TEMPLATE_DIR"/. hyperframes-video/
```

3. Edit the copied HTML/CSS/JS for the user's creative brief. Keep all `data-*` composition fields and timeline registration intact. For the first successful render, favor a compact 4-6 scene composition that can be written as one complete file.
4. Run checks from inside the project folder:

```bash
cd hyperframes-video
"$BOX_AGENT_NODE" "$HYPERFRAMES_RUNNER_PATH" inspect .
```

If the visible tool result is empty or unclear, immediately rerun the check with stderr included, for example:

```bash
"$BOX_AGENT_NODE" "$HYPERFRAMES_RUNNER_PATH" inspect . 2>&1 | head -120
```

If you must use the raw CLI, inspect stderr as well as the exit code. Any `[StaticGuard] Invalid HyperFrame contract` output is a failure that must be fixed before rendering.

5. Render through the runner and name the desired final MP4:

```bash
"$BOX_AGENT_NODE" "$HYPERFRAMES_RUNNER_PATH" render . --strict --quality draft --output ../video.mp4
```

The runner reports `final_output=...` after copying the actual HyperFrames render into the requested path. Use that path in the final answer; do not guess from the `--out` argument if the runner reports a different location.

6. Verify the output with ffprobe when the runner is unavailable:

```bash
"${HYPERFRAMES_FFPROBE_PATH:-ffprobe}" -v error -show_entries stream=codec_name,width,height,r_frame_rate -show_entries format=duration,size -of json ../video.mp4
```

7. Extract a small contact sheet and visually check it before reporting success. This is required even when ffprobe passes, because a readable MP4 can still be black, white, or missing its main asset. Do not use `rm -rf` to clean frame folders; write sampled frames into a fresh subdirectory or overwrite exact known frame files so the client does not stop for destructive-command approval:

```bash
frames_dir="frames/contact-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$frames_dir"
"${HYPERFRAMES_FFMPEG_PATH:-ffmpeg}" -v error -y -i ../video.mp4 \
  -vf "select='eq(n,0)+eq(n,60)+eq(n,150)+eq(n,240)'" -vsync 0 "$frames_dir/frame-%02d.png"
"${HYPERFRAMES_FFMPEG_PATH:-ffmpeg}" -v error -y -framerate 1 -i "$frames_dir/frame-%02d.png" \
  -vf "scale=240:-1,tile=4x1" -frames:v 1 "$frames_dir/contact.png"
```

If sampled frames do not show the requested main visual, key text, and end state, fix the composition and render again. Do not report "验证通过" from ffprobe alone.

Report the final MP4 path, duration, resolution, and any fallback used.

## Common Fixes

- Missing composition: restore the root `data-composition-id`, `data-duration`, `data-width`, `data-height`, and `data-start`.
- Timeline not seekable: register `window.__timelines[compositionId]` and implement `seekHyperframes`.
- Wrong project argument: use `inspect .` and `render .`, not `inspect index.html`.
- Wrong composition flag: `render --composition` expects an HTML file path such as `compositions/intro.html`; do not pass a composition id like `main`.
- Multiple root compositions: remove extra HTML files such as `template-index.html` that still contain `data-composition-id`; strict render must pass before reporting completion.
- StaticGuard in stderr: treat it as blocking even if the command exit code is 0. Fix the exact contract issue and re-run inspect before render.
- Browser/page errors, local 404s, or timeline registration timeouts in render output: treat them as blocking even if an MP4 exists and ffprobe can read it.
- Missed ad hoc replacement: read the root composition line first, then patch the actual attribute value. Do not assume duration or id values when applying a one-line fix.
- Render path mismatch: use the runner `final_output` line, or parse the actual `Rendering ...` MP4 path and copy it to the user-facing artifact path before reporting completion.
- `FILE_TOOL_ARGUMENT_TOO_LARGE`: simplify the composition to one complete HTML file under 5500 characters, usually by generating repeated scene markup from JS arrays. Do not split into append chunks unless every chunk is freshly regenerated literal content.
- Placeholder append failure: stop appending, read the current file, then rewrite a smaller complete file. The placeholder itself must never be written.
- Duplicate or partial HTML: overwrite the whole file. Do not keep any old prefix, repeated `</body></html>`, or half-written script.
- Empty diagnostic code output: when using `execute_code` to inspect images, JSON, paths, or media metadata, print the result explicitly. An expression without `print(...)` may return `(No output)`.
- Strict font warning: use a deterministic font stack such as `Arial, sans-serif`, or bundled/local font assets. Do not rely on system fonts such as `PingFang SC` unless they are declared with `@font-face`.
- Transform drift: avoid mixing CSS `transform` on the same element with GSAP transform animation. Do not set initial CSS transforms on stickers, badges, tags, or stage elements that GSAP will animate; use `gsap.set(...)` or a wrapper instead.
- Black/white video or missing poster/image: make `window.hyperframesReady` wait for `document.images` decode, confirm local asset paths exist, render again, then inspect sampled frames. Do not accept ffprobe-only validation for visual deliverables.
