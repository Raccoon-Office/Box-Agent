"use strict";

const fs = require("fs");
const path = require("path");
const clone = value => JSON.parse(JSON.stringify(value));

function correction(base, update, error) {
  const fields = [...new Set([...String(error).matchAll(/design_plan\.([a-z_]+)/g)].map(m => m[1]))]
    .filter(key => ["theme_id", "palette", "visual_requirements", "slides", "reason"].includes(key));
  const merged = clone(base);
  for (const field of fields) if (update && Object.hasOwn(update, field)) merged[field] = clone(update[field]);
  return { decision: merged, fields };
}
function normalizeCorrectionUpdate(update, base) {
  if (update && !Array.isArray(update) && Array.isArray(update.patch)) update = update.patch;
  if (!Array.isArray(update)) return update;
  const paths = new Map([
    ["/theme_id", "theme_id"],
    ["/profile_id", "profile_id"],
    ["/visual_profile", "visual_profile"],
    ["/original_decision/theme_id", "theme_id"],
    ["/palette", "palette"],
    ["/visual_requirements", "visual_requirements"],
    ["/slides", "slides"],
    ["/pages", "slides"],
    ["/allow_plain_fallback", "visual_requirements"],
    ["/reason", "reason"],
  ]);
  const result = clone(base || {});
  for (const operation of update) {
    if (!operation || operation.op !== "replace" || !paths.has(operation.path)) {
      throw new Error("designer correction: return an object with named fields; unsupported JSON Patch operation");
    }
    const key = paths.get(operation.path);
    if (operation.path === "/allow_plain_fallback") {
      result.visual_requirements = { ...(result.visual_requirements || {}), allow_plain_fallback: Boolean(operation.value) };
    } else {
      result[key] = clone(operation.value);
    }
  }
  return result;
}
function decisionFromResponses(responses, input, plans) {
  const parse = require("./design_response_source.js").parseDecision;
  const first = responses[0];
  if (!first) throw new Error("No completed designer response");
  const second = responses[1];
  if (second?.correction_base && (first.error
    || second.correction_base.session_id !== first.session_id
    || second.correction_base.response_hash !== first.response_hash)) {
    throw new Error("Correction must bind the fully read original design response");
  }
  // An incomplete first attempt has no trustworthy decision to patch. A fully
  // read, completed second response may still succeed through normal validation.
  if (first.error) {
    if (!second || second.error) throw new Error(second?.error || first.error);
    return { decision: normalizeCorrectionUpdate(parse(second.text), parse(first.text)), correction_fields: [] };
  }
  let base;
  try { base = parse(first.text); }
  catch (error) {
    if (!second || second.error) throw error;
    return { decision: normalizeCorrectionUpdate(parse(second.text), base), correction_fields: [] };
  }
  if (responses.length === 1) return { decision: base, correction_fields: [] };
  let error;
  try { plans.canonicalPlan(base, input); }
  catch (failure) { error = failure.message; }
  if (second.error) throw new Error(second.error);
  const update = normalizeCorrectionUpdate(parse(second.text), base);
  if (second.correction_base) {
    const merged = correction(base,update,(second.correction_issues || []).join("\n"));
    return { decision:merged.decision, correction_fields:merged.fields };
  }
  if (!error) return { decision: update, correction_fields: [] };
  const merged = correction(base, update, error);
  return { decision: merged.decision, correction_fields: merged.fields };
}
// User-authorized fail-safe after one design correction: keep the normal deck
// schema, renderer and editor. Only the design selection is degraded; invalid
// model plans are never accepted. Tests exercise content retention, page growth,
// renderer validation, editor operations and preservation of human-edited HTML.
function chunks(value, size) {
  const chars = Array.from(String(value || ""));
  const result = [];
  while (chars.length) {
    let take = Math.min(size,chars.length);
    if (chars.length > size) {
      for (let i=size-1;i>=Math.floor(size/2);i--) {
        if (/[，。；、,;.!?\s]/.test(chars[i])) { take=i+1; break; }
      }
    }
    result.push(chars.splice(0,take).join(""));
  }
  return result;
}
function recoveryDeck(input, proposedCount = 0) {
  const core = require("./deck_spec_core.js");
  const colors = require("./design_contract_core.js");
  const theme = core.getTheme("plain-neutral");
  const issues = [];
  const palette = colors.mergePaletteDecision({ background: "#FFFFFF", text: "#1D1D1F",
    primary: "#1D1D1F", accent: "#0071E3", secondary: "#6E6E73", accent_usage: "sparse" }, input.user_constraints?.palette);
  // Page geometry belongs to the rejected design. Preserve palette locks, but
  // report unmet geometry instead of attaching it to newly paginated slide IDs.
  const paletteContract = input.user_constraints?.palette ? { version:1, palette:input.user_constraints.palette } : null;
  const contract = colors.frozenPaletteContract(paletteContract, palette, "Registered neutral recovery design", issues);
  const recoveryWarnings = input.user_constraints?.slides
    ? ["Requested page geometry is not preserved by neutral recovery layouts; original constraints remain in design_input.json."] : [];
  const pages = [];
  const add = (layout_id, props, source) => pages.push({ id: `slide-${String(pages.length + 1).padStart(2,"0")}`,
    layout_id, props, source_outline_page: source });
  const outline = input.outline.slides;
  const first = outline[0] || {};
  add("cover-editorial-v1", { eyebrow: "产品与主题介绍", title: chunks(input.title || first.title || "演示",84)[0],
    subtitle: chunks(first.message || first.title,160)[0] || "", marker: "", meta: "基础主题 · 内容待核验", tags: [],
    composition: "poster", alignment: "left" }, 1);
  const groups = outline.flatMap((slide, index) => {
    const title = chunks(slide.title || "内容",64)[0];
    // Keep the full title/message as content if they exceed their header capacity.
    const content = [...(Array.from(slide.title || "").length > 64 ? [slide.title] : []),
      ...(Array.from(slide.message || "").length > 120 ? [slide.message] : []), ...(slide.bullets || [])]
      .flatMap(value => chunks(value,100));
    const result = [];
    const capacity = Math.ceil(Math.max(1,content.length) / Math.ceil(Math.max(1,content.length)/6));
    for (let i = 0; i < Math.max(1,content.length); i += capacity) result.push({ title,
      subtitle: Array.from(slide.message || "").length <= 120 ? slide.message || "" : "",
      content: content.slice(i,i+capacity), source: index+1 });
    return result;
  });
  const desired = Math.min(39, Math.max(groups.length, Number.isInteger(proposedCount) ? proposedCount-1 : 0));
  while (groups.length < desired) {
    const candidates = groups.map((group,index)=>({index,size:group.content.length})).filter(item=>item.size>1)
      .sort((a,b)=>b.size-a.size);
    if (!candidates.length) break;
    const index = candidates[0].index, group = groups[index];
    const rest = group.content.splice(Math.ceil(group.content.length/2));
    groups.splice(index+1,0,{...group,content:rest});
  }
  // A full 40-page outline must not exceed the normal schema just for a cover.
  if (groups.length >= 40) pages.pop();
  for (const group of groups) {
    const items = group.content.map((body,index)=>({title:body.match(/^([^：:]{1,24})[：:]/)?.[1] || `要点 ${index+1}`,body}));
    if (items.length >= 3) add("cards-grid-v1", { eyebrow: "内容要点", title: group.title, subtitle: group.subtitle,
      items, variant: "balanced", composition: "open" }, group.source);
    else add("closing-next-steps-v1", { eyebrow: "内容要点", title: group.title, subtitle: group.subtitle,
      actions: items.map(item=>({label:item.title,detail:item.body})), contact: "", variant:"next-steps",composition:"open" }, group.source);
  }
  const deck = { schema_version:1, title:chunks(input.title || "演示",120)[0], theme_id:theme.id,
    design:require("./composition_core.js").createDeckDesign(theme), ...(contract ? {design_contract:contract} : {}), slides:pages };
  const checked = core.validateAndNormalizeDeck(deck);
  if (!checked.ok) throw new Error(`Recovery deck validation failed: ${checked.issues.join("; ")}`);
  return { deck:checked.normalized, theme, warnings:[...recoveryWarnings,...issues,...checked.warnings] };
}
function fallback(input, root, reason, proposedCount = 0, publish = true) {
  const { deck, theme, warnings } = recoveryDeck(input, proposedCount);
  const rendered = require("./render_deck_html.js").renderDocument(deck,theme);
  // Identify an untouched generated recovery document without confusing it with
  // a normal accepted deck. A hash below prevents overwriting manual edits.
  const html = rendered.replace("<head>", '<head>\n<meta name="box-agent-recovery" content="1">');
  const file = path.join(root,"fallback.html"), index = path.join(root,"index.html");
  const reportFile = path.join(root,"qa","design_delivery.json");
  const digest = value => require("crypto").createHash("sha256").update(value).digest("hex");
  let previous;
  try { previous = JSON.parse(fs.readFileSync(reportFile,"utf8")); } catch (_) {}
  const current = fs.existsSync(index) ? fs.readFileSync(index,"utf8") : null;
  const publishIndex = publish && (current === null || (previous?.html_hash && digest(current) === previous.html_hash));
  // A previously edited fallback.html is also retained, rather than overwritten.
  let artifact = file;
  if (fs.existsSync(file) && (!previous?.html_hash || digest(fs.readFileSync(file,"utf8")) !== previous.html_hash)) artifact = path.join(root,`fallback-${digest(html).slice(0,12)}.html`);
  if (fs.existsSync(artifact) && fs.readFileSync(artifact,"utf8") !== html
    && artifact !== file) {
    const base = artifact.slice(0,-5);
    let suffix = 1;
    while (fs.existsSync(`${base}-${suffix}.html`)) suffix += 1;
    artifact = `${base}-${suffix}.html`;
  }
  fs.writeFileSync(artifact,html);
  if (publishIndex) fs.writeFileSync(index,html);
  const deckPath = path.join(root,`recovery-${digest(html).slice(0,12)}.deck.json`);
  fs.writeFileSync(deckPath,JSON.stringify(deck,null,2)+"\n");
  const report = {ok:true,status:"degraded",terminal:true,artifact,primary_artifact:publishIndex?index:artifact,
    deck:deckPath,html_hash:digest(html),reason,outline_pages:input.outline.slides.length,
    proposed_pages:proposedCount || null,actual_pages:deck.slides.length,
    warnings:["Registered neutral design; normal editor retained. Content and visual quality remain unverified.",...warnings],
    next:"Design retries are finished. Keep primary_artifact as the HTML source. If PPTX delivery is required, run check_html_export_env.js, export primary_artifact with html_to_editable_pptx.js, then validate_pptx_package.py and check actual pages, text and picture objects. Deliver existing files with the degradation report; report any missing format honestly. Do not restart authoring or change production route."};
  fs.mkdirSync(path.dirname(reportFile),{recursive:true});
  fs.writeFileSync(reportFile,JSON.stringify(report,null,2)+"\n");
  return report;
}
module.exports = { correction, decisionFromResponses, normalizeCorrectionUpdate, recoveryDeck, fallback };
