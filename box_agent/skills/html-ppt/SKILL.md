---
name: html-ppt
description: HTML PPT Studio — author professional static HTML presentations using CDN-hosted themes, layouts, animations, and runtime assets. Use when the user explicitly asks to create, generate, design, package, or deliver a slide-based output, such as PPT, slides, deck, keynote-style presentation, HTML presentation, reveal-style deck, slideshow, 幻灯片, 做一份 PPT, 做一份 slides, 技术分享 PPT, pitch deck, report deck, or multi-page 小红书图文. Do not use this skill when the user only asks to polish text, rewrite a speech script, summarize content, or brainstorm an outline without requesting a slide deliverable.
---

---

# html-ppt — HTML PPT Studio

Author professional HTML presentations as static files. The deck is delivered as a folder containing `index.html`, with all shared assets loaded from the official jsDelivr CDN.

One theme file = one visual style.
One layout pattern = one page type.
One animation class = one entry effect.
All pages share a token-based design system from `assets/base.css`.

---

## ⚠️ Mandatory Generation Protocol

Every generated deck MUST follow this exact protocol.

### Step 1 — Create a deck directory

Output MUST be:

```text
<output-dir>/index.html
```

Never create a bare `.html` file as the final deliverable.

Good:

```text
my-talk/index.html
```

Bad:

```text
my-talk.html
```

---

### Step 2 — Use CDN assets only

The generated HTML MUST load all shared assets from:

```text
https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main
```

Do not copy local `assets/`.
Do not use `./assets/`, `../assets/`, or `../../assets/`.
Do not use any other CDN provider.

Use this `<head>` asset block:

```html
<link
  rel="stylesheet"
  href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/fonts.css"
/>
<link
  rel="stylesheet"
  href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/base.css"
/>
<link
  rel="stylesheet"
  id="theme-link"
  href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/themes/THEME_NAME.css"
/>
<link
  rel="stylesheet"
  href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/animations/animations.css"
/>
```

Replace `THEME_NAME` with the selected theme, for example:

```text
minimal-white
tokyo-night
aurora
corporate-clean
pitch-deck-vc
```

At the end of `<body>`, include:

```html
<script src="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/runtime.js"></script>
```

If canvas FX animations are used, also include this before `runtime.js` or before the closing `</body>`:

```html
<script src="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/animations/fx-runtime.js"></script>
```

---

### Step 3 — Put theme switching data on `<body>`

The `<body>` tag MUST include:

```html
<body
  data-themes="THEME1,THEME2,THEME3"
  data-theme-base="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/themes/"
></body>
```

Rules:

- Put `data-themes` and `data-theme-base` on `<body>`, not on `<html>`.
- `data-theme-base` MUST use the jsDelivr CDN path above.
- `data-themes` should include 2–5 suitable themes for keyboard theme cycling.
- The first theme in `data-themes` should match the theme used by `#theme-link`.

Example:

```html
<body
  data-themes="tokyo-night,aurora,corporate-clean"
  data-theme-base="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/themes/"
></body>
```

```html
<link
  rel="stylesheet"
  id="theme-link"
  href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/themes/tokyo-night.css"
/>
```

---

### Step 4 — Do not verify or start servers

This skill produces static HTML only.

Forbidden:

- Do not use Reveal.js.
- Do not use import maps.
- Do not use `es-module-shims`.
- Do not use `<script type="module">` imports.
- Do not import third-party packages such as `marked`, `dompurify`, `hammerjs`, `animate.css`, or `reveal.js` from npm CDNs.
- Do not start an HTTP server.
- Do not start a dev server.
- Do not open a preview server.
- Do not run `fix-deck.sh`.
- Do not copy local assets.
- Do not spend extra steps verifying screenshots unless the user explicitly asks.

Just write the static HTML file, zip the deck directory, and provide the output path.

---

### Step 5 — Zip the output folder

After writing `<output-dir>/index.html`, zip the whole directory:

```bash
cd <output-dir>/.. && zip -r <name>.zip <name>/
```

The zip is the primary deliverable.

After zipping, tell the user the download path to the `.zip` file.

Example:

```text
下载链接：/path/to/my-talk.zip
```

---

## Output Contract

Every generated deck must contain:

```text
<output-dir>/
└── index.html
```

The final response to the user must include:

```text
下载链接：<path-to-zip>
```

Optional but recommended:

```text
HTML 文件：<output-dir>/index.html
```

---

## When to Use

Use this skill when the user asks for a slide-based deliverable, including:

- PPT
- slides
- deck
- keynote-style presentation
- keyboard-navigable HTML deck
- slideshow
- 幻灯片
- 演讲 PPT
- 技术分享 PPT
- pitch deck
- report deck
- 小红书图文 with multiple pages/cards

Do not use this skill when the user only asks for:

- rewriting text
- polishing a speech script
- summarizing content
- brainstorming an outline
- analyzing an existing deck without asking for a new slide deliverable

If the user's intent is ambiguous, infer from context. If they clearly want a presentable slide output, use this skill.

---

## Before Authoring

If the user has not provided enough information, ask for the minimum missing details:

1. Topic / content
2. Audience
3. Approximate slide count
4. Style preference

If the user already provided rich content or the goal is obvious, do not block on questions. Choose sensible defaults and proceed.

Recommended defaults:

- Technical sharing: `tokyo-night`, `aurora`, `blueprint`
- Business report: `corporate-clean`, `swiss-grid`, `minimal-white`
- Investor pitch: `pitch-deck-vc`, `corporate-clean`, `magazine-bold`
- 小红书图文: `xiaohongshu-white`, `soft-pastel`, `rainbow-gradient`
- Academic/report: `academic-paper`, `editorial-serif`, `minimal-white`
- Cyber/launch style: `cyberpunk-neon`, `vaporwave`, `y2k-chrome`

---

## Recommended Authoring Workflow

1. Understand the user's goal, audience, and output format.
2. Choose a suitable theme.
3. Choose a suitable full-deck template or single-page layout pattern.
4. Write audience-facing slide content.
5. Add speaker notes only if the user asks for 演讲稿, 逐字稿, speaker notes, presenter view, or technical sharing.
6. Save as `<output-dir>/index.html`.
7. Zip the output folder.
8. Return the zip path.

---

## HTML Skeleton

Use this structure for generated decks:

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Deck Title</title>

    <link
      rel="stylesheet"
      href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/fonts.css"
    />
    <link
      rel="stylesheet"
      href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/base.css"
    />
    <link
      rel="stylesheet"
      id="theme-link"
      href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/themes/tokyo-night.css"
    />
    <link
      rel="stylesheet"
      href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/animations/animations.css"
    />

    <style>
      /* Deck-specific styles go here. */
    </style>
  </head>
  <body
    data-themes="tokyo-night,aurora,corporate-clean"
    data-theme-base="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/themes/"
  >
    <main class="deck">
      <section class="slide is-active">
        <div class="deck-header">Deck Header</div>
        <div class="slide-content">
          <h1>Slide Title</h1>
          <p>Slide content.</p>
        </div>
        <div class="deck-footer">Footer</div>
        <div class="slide-number">1 / 1</div>
        <aside class="notes">Speaker notes go here.</aside>
      </section>
    </main>

    <script src="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/runtime.js"></script>
  </body>
</html>
```

---

## Slide Structure Rules

- One `.slide` per logical page.
- The first slide should have `.is-active`.
- Slides should contain only audience-facing content.
- Presenter-only explanations must go into `<aside class="notes">`.
- Do not put text like “这一页展示了……” or “Speaker: ……” visibly on the slide.
- Keep the slide clean, visual, and readable.
- Use `deck-header`, `deck-footer`, and `slide-number` when appropriate.
- Keep the content suitable for 16:9 presentation unless the user asks otherwise.

---

## Speaker Notes / Presenter Mode

If the user mentions any of these:

- 演讲
- 分享
- 讲稿
- 逐字稿
- speaker notes
- presenter view
- 演讲者视图
- 提词器
- 技术分享

Then include speaker notes using:

```html
<aside class="notes">这里写给演讲者看的提示或逐字稿。</aside>
```

Speaker notes rules:

1. Notes are for the speaker, not the audience.
2. Use conversational language.
3. For a normal talk deck, each slide can have 150–300 Chinese characters of speaker notes.
4. Use paragraph breaks for rhythm.
5. Highlight transitions and key points with clear wording.

Do not put speaker notes in visible slide elements.

Presenter mode is provided by `runtime.js`. Keyboard shortcut:

```text
S = open presenter mode
```

---

## Theme Rules

Available themes include:

```text
minimal-white
editorial-serif
soft-pastel
sharp-mono
arctic-cool
sunset-warm
catppuccin-latte
catppuccin-mocha
dracula
tokyo-night
nord
solarized-light
gruvbox-dark
rose-pine
neo-brutalism
glassmorphism
bauhaus
swiss-grid
terminal-green
xiaohongshu-white
rainbow-gradient
aurora
blueprint
memphis-pop
cyberpunk-neon
y2k-chrome
retro-tv
japanese-minimal
vaporwave
midcentury
corporate-clean
academic-paper
news-broadcast
pitch-deck-vc
magazine-bold
engineering-whiteprint
```

Theme selection guidance:

- Business / executive report: `corporate-clean`, `swiss-grid`, `minimal-white`
- Technical sharing: `tokyo-night`, `aurora`, `blueprint`, `engineering-whiteprint`
- Engineering deep dive: `tokyo-night`, `terminal-green`, `dracula`, `catppuccin-mocha`
- Product launch: `pitch-deck-vc`, `aurora`, `magazine-bold`
- 小红书图文: `xiaohongshu-white`, `soft-pastel`, `rainbow-gradient`
- Academic / research: `academic-paper`, `editorial-serif`, `minimal-white`
- Bold / experimental: `cyberpunk-neon`, `vaporwave`, `neo-brutalism`, `y2k-chrome`

---

## Animation Rules

Use CSS animations with `data-anim` or animation classes.

Example:

```html
<h1 data-anim="fade-up">Title</h1>
<ul class="anim-stagger-list">
  <li>Point one</li>
  <li>Point two</li>
  <li>Point three</li>
</ul>
```

Use animations sparingly. They should support the message, not distract from it.

For canvas FX animations, add a container:

```html
<div data-fx="knowledge-graph"></div>
```

And include:

```html
<script src="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/animations/fx-runtime.js"></script>
```

Available FX examples:

```text
particle-burst
confetti-cannon
firework
starfield
matrix-rain
knowledge-graph
neural-net
constellation
orbit-ring
galaxy-swirl
word-cascade
letter-explode
chain-react
magnetic-field
data-stream
gradient-blob
sparkle-trail
shockwave
typewriter-multi
counter-explosion
```

---

## Design Rules

- Prefer existing full-deck templates or single-page layout patterns when they fit.
- Custom slide structure is allowed when existing layouts do not fit the user's content.
- Use design tokens from `base.css` and theme variables.
- Prefer CSS variables such as `var(--text-1)`, `var(--text-2)`, `var(--surface-1)`, `var(--accent-1)`.
- Avoid hard-coded colors unless necessary for a specific visual effect.
- Keep each slide focused on one main idea.
- Use strong hierarchy: title, subtitle, body, visual emphasis.
- Avoid dense paragraphs on slides.
- Use speaker notes for explanation-heavy content.
- Make the deck keyboard-first.

---

## Runtime Keyboard Shortcuts

The runtime supports:

```text
← / → / Space / PgUp / PgDn    Navigate
Home / End                     First / last slide
F                              Fullscreen
S                              Presenter mode
N                              Notes drawer
R                              Reset timer in presenter mode
O                              Slide overview
T                              Cycle themes
A                              Cycle demo animation
#/N                            Deep-link to slide N
?preview=N                     Preview-only mode for slide N
Esc                            Close overlays
```

---

## CDN Path Rules

Allowed CDN URLs MUST start with:

```text
https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main
```

Allowed examples:

```html
<link
  rel="stylesheet"
  href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/base.css"
/>
<link
  rel="stylesheet"
  href="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/themes/aurora.css"
/>
<script src="https://cdn.jsdelivr.net/gh/lewislulu/html-ppt-skill@main/assets/runtime.js"></script>
```

The generated deck must not load any package from `cdn.jsdelivr.net/npm`,
even though it is also jsDelivr.
Forbidden examples:

```html
<link rel="stylesheet" href="./assets/base.css" />
<link rel="stylesheet" href="../assets/base.css" />
<link rel="stylesheet" href="../../assets/base.css" />
<script src="https://unpkg.com/..."></script>
<script src="https://cdnjs.cloudflare.com/..."></script>
```

---

## Packaging

Use this command after creating the deck directory:

```bash
cd <output-dir>/.. && zip -r <name>.zip <name>/
```

Do not run:

```bash
fix-deck.sh
```

Do not copy:

```text
assets/
```

Do not start:

```text
HTTP server
preview server
dev server
```

---

## Final Response Format

After generating and zipping the deck, respond with:

```text
已生成。

下载链接：<path-to-zip>
HTML 文件：<output-dir>/index.html
```

Do not include long implementation details unless the user asks.

---

## Install

```bash
npx skills add https://github.com/lewislulu/html-ppt-skill
```

This skill generates pure static HTML decks. No build step is required.

---

## Reference Catalogs

Load these references only when needed:

- `references/themes.md` — theme catalog and usage guidance
- `references/layouts.md` — single-page layout catalog
- `references/animations.md` — CSS and canvas animation catalog
- `references/full-decks.md` — full-deck template catalog
- `references/presenter-mode.md` — presenter mode and speaker notes guide
- `references/authoring-guide.md` — complete authoring workflow

---

## File Structure

```text
html-ppt/
├── SKILL.md
├── references/
│   ├── themes.md
│   ├── layouts.md
│   ├── animations.md
│   ├── full-decks.md
│   ├── presenter-mode.md
│   └── authoring-guide.md
├── assets/
│   ├── base.css
│   ├── fonts.css
│   ├── runtime.js
│   ├── themes/*.css
│   └── animations/
│       ├── animations.css
│       ├── fx-runtime.js
│       └── fx/*.js
├── templates/
│   ├── deck.html
│   ├── theme-showcase.html
│   ├── layout-showcase.html
│   ├── animation-showcase.html
│   ├── full-decks-index.html
│   ├── full-decks/<name>/
│   └── single-page/*.html
├── scripts/
│   ├── new-deck.sh
│   └── render.sh
└── examples/
    └── demo-deck/
```

---

## Important Constraints

- Final output is a zipped static HTML deck directory.
- Shared assets are loaded from the official jsDelivr CDN.
- Do not use local asset paths in generated decks.
- Do not copy assets into the output directory.
- Do not run `fix-deck.sh`.
- Do not start a server.
- Do not verify unless explicitly requested.
- Do not put speaker-only content on visible slides.
- Do not finish without providing the zip path.
