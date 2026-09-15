#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");
const core = require("./deck_spec_core.js");
const plans = require("./design_plan_core.js");

function write(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
}
function writeInput(file, input, root) {
  const requestDir = path.join(root, "qa", "design", input.input_hash);
  const detailDir = path.join(requestDir, "catalog");
  const directory = input.catalog;
  for (const theme of directory.themes) write(path.join(detailDir, "themes", `${theme.id}.json`), theme);
  for (const layout of directory.layouts) write(path.join(detailDir, "layouts", `${layout.id}.json`), layout);
  for (const palette of directory.palettes || []) write(path.join(detailDir, "palettes", `${palette.id}.json`), palette);
  write(path.join(detailDir, "palettes", "color-harmony-rules.json"), directory.harmony_rules);
  const briefPath = path.join(requestDir, "brief.json");
  // Independent reads must fit the tool-result context, not just its file limit.
  // One record per line keeps indices searchable and permits line pagination.
  const packets = (kind, records, maxBytes = 7000) => {
    const groups = [[]];
    for (const record of records) {
      const group = groups.at(-1);
      if (group.length && JSON.stringify([...group, record]).length > maxBytes) groups.push([]);
      groups.at(-1).push(record);
    }
    return groups.map((group, index) => {
      const packet = path.join(requestDir, `${kind}-${index + 1}.json`);
      const body = `[\n${group.map(item => JSON.stringify(item)).join(",\n")}\n]\n`;
      fs.writeFileSync(packet, body);
      return { path: packet, sha256: require("crypto").createHash("sha256").update(body).digest("hex") };
    });
  };
  const contentFiles = packets("pages", input.outline.slides.map((slide, index) => ({
    page: index + 1, title: slide.title, message: slide.message, bullets: slide.bullets,
    hard_requirements: slide.hard_requirements,
  })));
  const themeFiles = packets("themes", directory.themes.map(theme => ({
    id: theme.id,
    traits: Object.fromEntries(["canvas", "heading", "shadow", "display_font"].map(key => [key, theme.visual_traits?.[key]])),
    fit: (theme.selection?.industry_fit || [])[0] || "",
  })));
  const layoutFiles = packets("layouts", directory.layouts.map(layout => ({
    id: layout.id, label: layout.label, roles: layout.roles,
    visual_options: layout.visual_options,
  })));
  const paletteFiles = packets("palettes", directory.palettes || []);
  const harmonyFiles = packets("harmony", [directory.harmony_rules]);
  const profileFiles = packets("profiles", directory.brand_profiles || []);
  const brief = {
    title: input.title, goal: input.outline.deck_goal, audience: input.outline.audience,
    tone: input.outline.tone, source_text: input.source_text, user_constraints: input.user_constraints,
    page_count: input.outline.slides.length,
    content_files: contentFiles.map(item => item.path),
    theme_index_files: themeFiles.map(item => item.path),
    layout_index_files: layoutFiles.map(item => item.path),
    palette_index_files: paletteFiles.map(item => item.path),
    harmony_rule_files: harmonyFiles.map(item => item.path),
    profile_index_files: profileFiles.map(item => item.path),
    required_read_files: [...contentFiles, ...themeFiles, ...layoutFiles, ...paletteFiles, ...harmonyFiles, ...profileFiles],
    details_directory: detailDir,
    reading_policy: "Read every listed content/index file completely. Then shortlist at most 3 themes from traits and audience; read only shortlisted themes and chosen layout details using details_directory/themes/<id>.json and details_directory/layouts/<id>.json. Do not search the entire catalog. Final output must have exactly page_count slides.",
    visual_requirements: { fields: require("./theme_match.js").OPTIONS, allow_plain_fallback: "boolean; false when user-required visual features must not be relaxed", policy: "Choose visual requirements before theme. Match visual_traits, not original colors. If no catalog theme matches and fallback is allowed, program selects plain-neutral, preserving exact palette and layouts." },
    palette_contract: { version: 2, required_roles: ["background", "text", "primary", "accent", "secondary"],
      required_usage: "accent_usage: sparse | balanced | dominant",
      policy: "Return exact #RRGGBB values for every role, including theme defaults. User colors remain locked; fill missing roles only. User palettes default to sparse accent usage unless explicitly specified." },
    palette_sources: { harmony_rules: "Read harmony_rule_files for deterministic primary-color completion; palette_harmony.js is the programmatic equivalent.",
      premium_presets: paletteFiles.map(item => item.path),
      policy: "Use a premium preset for an explicitly requested高级感 mood; use harmony rules to complete missing roles and run contrast checks." },
  };
  // The entry point contains the read plan, never the expanded catalog.
  fs.mkdirSync(requestDir, { recursive: true });
  if (!fs.existsSync(briefPath)) write(briefPath, brief);
  input.request_file = briefPath;
  const requestMeta = path.join(requestDir, "request.json");
  if (!fs.existsSync(requestMeta)) write(requestMeta, { created_at: Date.now() });
  input.request_created_at = JSON.parse(fs.readFileSync(requestMeta, "utf8")).created_at;
  const { catalog, ...metadata } = input;
  write(file, metadata);
  require("./design_recovery.js").fallback({ ...input, input_file: path.resolve(file),
    outline_file: path.resolve(path.dirname(file), input.outline_file) }, root,
    "Design pending; baseline generated from validated outline", 0);
}
function accept(inputPath, planPath) {
  const { input, root } = plans.readInput(inputPath);
  const source = require("./design_response_source.js");
  const responses = source.findResponses(input, root);
  const recovery = require("./design_recovery.js");
  if (!responses.length) return recovery.fallback(input, root, "No completed designer response; using validated outline");
  const selected = responses.slice(0, 2);
  const response = selected.at(-1);
  const receiptPath = path.join(path.dirname(input.request_file), "accepted.json");
  const previousPlan = fs.existsSync(planPath) ? fs.readFileSync(planPath) : null;
  const previousReceipt = fs.existsSync(receiptPath) ? fs.readFileSync(receiptPath) : null;
  try {
    if (response.error) throw new Error(response.error);
    const merged = recovery.decisionFromResponses(selected, input, plans);
    const plan = plans.canonicalPlan(merged.decision, input);
    write(planPath, plan);
    write(receiptPath, { session_id: response.session_id, response_hash: response.response_hash,
      plan_hash: plans.hash(plan), input_hash: input.input_hash,
      sources: selected.map(item => ({ session_id: item.session_id, response_hash: item.response_hash })) });
    checkScaffold(planPath, inputPath);
    write(path.join(root,"qa","design_delivery.json"), { ok:true, status:"design_accepted", correction_fields:merged.correction_fields });
    return { ok: true, plan: planPath, source_session: response.session_id,
      plan_hash: plans.hash(plan), theme_match: plan.theme_match, attempts: responses.length };
  } catch (error) {
    if (previousPlan) fs.writeFileSync(planPath, previousPlan);
    else if (fs.existsSync(planPath)) fs.unlinkSync(planPath);
    if (previousReceipt) fs.writeFileSync(receiptPath, previousReceipt);
    else if (fs.existsSync(receiptPath)) fs.unlinkSync(receiptPath);
    if (responses.length >= 2) {
      let count = 0;
      try { count = source.parseDecision(response.text).slides?.length || 0; } catch (_) {}
      return recovery.fallback(input, root, error.message, count);
    }
    let fields = [];
    let originalDecision = null;
    try {
      originalDecision = source.parseDecision(response.text);
      fields = recovery.correction(originalDecision, {}, error.message).fields;
    } catch (_) {}
    const correctionFile = path.join(path.dirname(input.request_file), "correction.json");
    const layoutIds = [...new Set((originalDecision?.slides || []).map(slide => slide.layout_id))];
    write(correctionFile, { brief_file: input.request_file,
      base_session_id: response.session_id, base_response_hash: response.response_hash,
      // A patch requires a completed, fully read original, not just parseable JSON.
      requires_full_read: Boolean(response.error) || !originalDecision,
      page_count: input.outline.slides.length, original_decision: originalDecision,
      editable_fields: fields, issues: [error.message],
      layout_options: plans.catalog().layouts.filter(layout => layoutIds.includes(layout.id))
        .map(layout => ({ id: layout.id, visual_options: layout.visual_options })),
      instruction: "Read this correction file. For enum errors, use layout_options here and return only editable_fields as JSON. Preserve all unaffected pages and palette. If requires_full_read is true, read brief_file and all its packets before returning a complete decision. Do not search the catalog again." });
    const report = { ok: false, can_retry: true, correction_file: correctionFile,
      fallback_artifact: path.join(root,"fallback.html"),
      source_session: response.session_id, attempts: responses.length, issues: [error.message] };
    write(path.join(path.dirname(input.request_file), "check.json"), report);
    console.error(JSON.stringify(report));
    process.exitCode = 1;
    return null;
  }
}
function checkScaffold(planPath, inputPath) {
  const result = spawnSync(process.execPath, [path.join(__dirname, "inspect_deck_contract.js"),
    "--design-plan", planPath, "--design-input", inputPath], {
    encoding: "utf8", maxBuffer: 16 * 1024 * 1024,
  });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout);
}
function main() {
  const [action, target, ...args] = process.argv.slice(2);
  if (!["prepare", "accept", "validate", "apply"].includes(action) || !target) {
    throw new Error("Usage: design_plan.js prepare outline.json [--out design_input.json] [--research-handoff PATH] | accept design_input.json | validate design_plan.json [--input design_input.json] | apply design_plan.json --deck deck.json [--input design_input.json]");
  }
  const opts = {};
  for (let index = 0; index < args.length; index += 2) {
    if (!["--out", "--input", "--plan", "--report", "--research-handoff", "--deck", "--title"].includes(args[index]) || !args[index + 1]) {
      throw new Error(`Unknown or missing option: ${args[index]}`);
    }
    opts[args[index].slice(2)] = args[index + 1];
  }
  const targetPath = core.resolveArtifactPath(target);
  if (action === "prepare") {
    const root = path.dirname(targetPath);
    const outline = JSON.parse(fs.readFileSync(targetPath, "utf8"));
    const baseline = { title: opts.title || outline.deck_goal || "Presentation", outline, outline_file:targetPath,
      user_constraints: require("./design_contract_core.js").inferDesignContract({ source_text: core.runtimeSourceBinding().source_text }, []) };
    require("./design_recovery.js").fallback(baseline, root, "Outline awaiting validation; content is not verified");
    const report = path.join(root, "qa", "outline_check.json");
    const checkArgs = [path.join(__dirname, "validate_outline.js"), targetPath, "--report", report];
    if (opts["research-handoff"]) checkArgs.push("--research-handoff", core.resolveArtifactPath(opts["research-handoff"]));
    const check = spawnSync(process.execPath, checkArgs, { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 });
    if (check.status !== 0) {
      console.log(JSON.stringify(require("./design_recovery.js").fallback(baseline, root,
        `Outline validation incomplete; inspect ${report}. Content is not verified.`)));
      return;
    }
    const input = plans.makeInput(outline, opts.title || outline.deck_goal, core.runtimeSourceBinding().source_text);
    const out = core.resolveArtifactPath(opts.out || path.join(root, "design_input.json"));
    input.outline_file = path.relative(path.dirname(out), targetPath);
    writeInput(out, input, root);
    const planFile = core.resolveArtifactPath(opts.plan || path.join(root, "design_plan.json"));
    let reusable = false;
    if (fs.existsSync(planFile)) {
      try {
        const candidate = JSON.parse(fs.readFileSync(planFile, "utf8"));
        reusable = plans.validatePlan(candidate, input).ok;
        if (reusable) { plans.readValidatedPlan(planFile, out); checkScaffold(planFile, out); }
      } catch (_error) { reusable = false; }
    }
    // A user-edited HTML is authoritative; never silently reuse the old AI plan.
    const html = path.join(root, "index.html");
    if (reusable && fs.existsSync(html)) {
      const match = fs.readFileSync(html, "utf8").match(/<script[^>]*id="deck-document"[^>]*>([\s\S]*?)<\/script>/);
      if (match) reusable = JSON.parse(match[1]).design_plan?.user_edited !== true;
    }
    const researchStatus = outline.source_mode === "public_authoritative_research"
      ? (opts["research-handoff"] ? "handoff_validated" : "handoff_unverified") : "user_material";
    write(path.join(root, "qa", "research_handoff_check.json"), { ok: true,
      advisory: researchStatus === "handoff_unverified", status: researchStatus, issues: [],
      warnings: researchStatus === "handoff_unverified" ? ["Research handoff was not provided; research verification remains incomplete."] : [] });
    console.log(JSON.stringify({ ok: true, input: out, designer_brief: input.request_file,
      plan: planFile, reusable, research_status: researchStatus,
      next: reusable ? "reuse the accepted design" : "delegate the role with designer_brief; then run design_plan.js accept design_input.json. Do not write the plan yourself." }));
    return;
  }
  if (action === "accept") {
    const result = accept(targetPath, core.resolveArtifactPath(opts.plan || path.join(path.dirname(targetPath), "design_plan.json")));
    if (result) console.log(JSON.stringify(result));
    return;
  }
  const inputPath = core.resolveArtifactPath(opts.input || path.join(path.dirname(targetPath), "design_input.json"));
  const validated = plans.readValidatedPlan(targetPath, inputPath);
  checkScaffold(targetPath, inputPath);
  if (action === "validate") {
    const report = { ok: true, plan_hash: plans.hash(validated.plan), theme_id: validated.preset.theme.id,
      theme_match: validated.plan.theme_match,
      layout_plan: validated.plan.slides.map(slide => slide.layout_id) };
    if (opts.report) write(core.resolveArtifactPath(opts.report), report);
    console.log(JSON.stringify(report));
    return;
  }
  if (!opts.deck) throw new Error("apply requires --deck deck.json");
  const deckPath = core.resolveArtifactPath(opts.deck);
  const deck = JSON.parse(fs.readFileSync(deckPath, "utf8"));
  if (deck.slides.length !== validated.plan.slides.length) throw new Error("design_plan.slides: a redesign must preserve the existing page count; revise the content plan separately");
  const redesign = {
    theme_id: validated.preset.theme.id,
    design: { family: validated.preset.design.family, variant: validated.preset.design.variant },

    slides: Object.fromEntries(deck.slides.map((slide, index) => [slide.id, {
      layout_id: validated.plan.slides[index].layout_id, props: validated.plan.slides[index].visual_options,
    }])),
  };
  // Compute and validate the complete new document before writing anything.
  // Explicit user constraints come from the fresh input, never an old AI palette.
  if (validated.design_contract) deck.design_contract = validated.design_contract;
  else delete deck.design_contract;
  const { redesignDeck, updateContractReport, updateImageManifestDesign } = require("./apply_deck_redesign.js");
  const result = redesignDeck(deck, redesign, deckPath);
  const updated = result.deck;
  updated.design_plan = plans.bindingFor(validated.plan);
  write(deckPath, updated);
  updateContractReport(deckPath, updated, targetPath, result.redesignedSlides);
  updateImageManifestDesign(deckPath, updated);
  console.log(JSON.stringify({ ok: true, deck: deckPath, plan_hash: updated.design_plan.plan_hash }));
}
try { main(); } catch (error) {
  console.error(JSON.stringify({ ok: false, issues: [error.message] }));
  process.exitCode = 1;
}
