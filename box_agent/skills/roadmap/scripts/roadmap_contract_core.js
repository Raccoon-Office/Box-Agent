"use strict";

const crypto = require("crypto");

const ROADMAP_DRAFT_SCHEMA_VERSION = 1;
const ROADMAP_SPEC_SCHEMA_VERSION = 1;
const ROADMAP_KIND_DRAFT = "roadmap-draft";
const ROADMAP_KIND_SPEC = "roadmap-spec";
const LOW_CONFIDENCE_THRESHOLD = 0.8;
const MAX_LANES = 8;
const MAX_ITEMS = 80;
const MAX_RANGE_DAYS = 184;
const ID_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
const PLAIN_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const SOURCE_TYPES = new Set([
  "natural-language",
  "table",
  "image",
  "roadmap-spec",
]);
const ITEM_KINDS = new Set(["bar", "milestone"]);
const CERTAINTIES = new Set(["confirmed", "tentative"]);
const PROGRESS_STATES = new Set(["planned", "doing", "done", "blocked"]);
const GRANULARITIES = new Set(["half-month"]);

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function unknownFields(value, allowed, fieldPath, issues) {
  if (!isPlainObject(value)) return;
  const unknown = Object.keys(value).filter(key => !allowed.has(key));
  if (unknown.length) issues.push(`${fieldPath}: unknown field(s): ${unknown.join(", ")}`);
}

function normalizeNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function plainDateToDay(value) {
  if (!PLAIN_DATE_RE.test(String(value || ""))) return null;
  const [year, month, day] = String(value).split("-").map(Number);
  const timestamp = Date.UTC(year, month - 1, day);
  const date = new Date(timestamp);
  if (
    date.getUTCFullYear() !== year
    || date.getUTCMonth() !== month - 1
    || date.getUTCDate() !== day
  ) {
    return null;
  }
  return Math.floor(timestamp / 86400000);
}

function dayToPlainDate(day) {
  if (!Number.isInteger(day)) return null;
  return new Date(day * 86400000).toISOString().slice(0, 10);
}

function stableId(value, prefix, identityParts) {
  const supplied = text(value);
  if (supplied) return supplied;
  const seed = identityParts.map(part => String(part || "").normalize("NFKC")).join("\u001f");
  const slug = text(identityParts[0])
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32);
  const digest = crypto.createHash("sha256").update(seed).digest("hex").slice(0, 10);
  return `${prefix}-${slug ? `${slug}-` : ""}${digest}`;
}

function normalizeConfidence(value, fieldPath, issues) {
  const number = normalizeNumber(value);
  if (number == null || number < 0 || number > 1) {
    issues.push(`${fieldPath}: expected number between 0 and 1`);
    return null;
  }
  return number;
}

function normalizeSource(value, fieldPath, issues) {
  if (!isPlainObject(value)) {
    issues.push(`${fieldPath}: expected object`);
    return null;
  }
  unknownFields(
    value,
    new Set(["type", "confidence", "field_confidence", "excerpt", "row", "column", "region", "page", "field_path"]),
    fieldPath,
    issues
  );
  const type = text(value.type);
  if (!SOURCE_TYPES.has(type)) {
    issues.push(`${fieldPath}.type: expected one of ${[...SOURCE_TYPES].join(", ")}`);
  }
  const confidence = normalizeConfidence(value.confidence, `${fieldPath}.confidence`, issues);
  const fieldConfidence = {};
  if (value.field_confidence !== undefined) {
    if (!isPlainObject(value.field_confidence)) {
      issues.push(`${fieldPath}.field_confidence: expected object`);
    } else {
      Object.entries(value.field_confidence).forEach(([key, entry]) => {
        const normalized = normalizeConfidence(
          entry,
          `${fieldPath}.field_confidence.${key}`,
          issues
        );
        if (normalized != null) fieldConfidence[key] = normalized;
      });
    }
  }
  const normalized = {
    type,
    ...(confidence == null ? {} : { confidence }),
    ...(Object.keys(fieldConfidence).length ? { field_confidence: fieldConfidence } : {}),
  };
  if (value.excerpt !== undefined) {
    const excerpt = text(value.excerpt);
    if (!excerpt || Array.from(excerpt).length > 500) {
      issues.push(`${fieldPath}.excerpt: expected 1-500 characters`);
    } else normalized.excerpt = excerpt;
  }
  if (value.row !== undefined) {
    if (!Number.isInteger(value.row) || value.row < 1) {
      issues.push(`${fieldPath}.row: expected positive integer`);
    } else normalized.row = value.row;
  }
  if (value.column !== undefined) {
    const column = text(value.column);
    if (!column) issues.push(`${fieldPath}.column: expected non-empty text`);
    else normalized.column = column;
  }
  if (value.page !== undefined) {
    if (!Number.isInteger(value.page) || value.page < 1) {
      issues.push(`${fieldPath}.page: expected positive integer`);
    } else normalized.page = value.page;
  }
  if (value.field_path !== undefined) {
    const fieldPathValue = text(value.field_path);
    if (!fieldPathValue) issues.push(`${fieldPath}.field_path: expected non-empty text`);
    else normalized.field_path = fieldPathValue;
  }
  if (value.region !== undefined) {
    const region = value.region;
    if (
      !Array.isArray(region)
      || region.length !== 4
      || region.some(entry => !Number.isFinite(entry))
      || region[0] < 0
      || region[1] < 0
      || region[2] <= 0
      || region[3] <= 0
    ) {
      issues.push(`${fieldPath}.region: expected [x, y, width, height] with positive size`);
    } else normalized.region = region.slice();
  }
  if (type === "table" && normalized.row === undefined) {
    issues.push(`${fieldPath}.row: required for table source`);
  }
  if (type === "image" && normalized.region === undefined) {
    issues.push(`${fieldPath}.region: required for image source`);
  }
  if (type === "roadmap-spec" && !normalized.field_path) {
    issues.push(`${fieldPath}.field_path: required for roadmap-spec source`);
  }
  return normalized;
}

function normalizeQuestion(value, fieldPath, issues) {
  if (!isPlainObject(value)) {
    issues.push(`${fieldPath}: expected object`);
    return null;
  }
  unknownFields(value, new Set(["field_path", "prompt", "reason"]), fieldPath, issues);
  const target = text(value.field_path);
  const prompt = text(value.prompt);
  const reason = text(value.reason);
  if (!target) issues.push(`${fieldPath}.field_path: expected non-empty text`);
  if (!prompt) issues.push(`${fieldPath}.prompt: expected non-empty text`);
  return target && prompt
    ? { field_path: target, prompt, ...(reason ? { reason } : {}) }
    : null;
}

function validateAndNormalizePendingQuestions(value, fieldPath = "pending_questions") {
  const issues = [];
  const pendingQuestions = [];
  if (!Array.isArray(value)) {
    issues.push(`${fieldPath}: expected array`);
  } else {
    value.forEach((question, index) => {
      const normalized = normalizeQuestion(question, `${fieldPath}.${index}`, issues);
      if (normalized) pendingQuestions.push(normalized);
    });
  }
  return { ok: issues.length === 0, pending_questions: pendingQuestions, issues };
}

function pendingQuestionsForRoadmapSpec(spec, existingQuestions = []) {
  const result = validateAndNormalizePendingQuestions(existingQuestions);
  const questions = result.pending_questions.filter(
    question => !/^items\.\d+\./.test(question.field_path)
  );
  if (!isPlainObject(spec) || !Array.isArray(spec.items)) return result;
  spec.items.forEach((item, index) => {
    if (!isPlainObject(item) || item.certainty !== "tentative") return;
    addQuestion(
      questions,
      `items.${index}.start`,
      `请确认“${text(item.title) || "该任务"}”的起止日期。`,
      [fieldConfidence(item.source, "start"), fieldConfidence(item.source, "end")]
        .some(confidence => confidence != null && confidence < LOW_CONFIDENCE_THRESHOLD)
        ? `date confidence is below ${LOW_CONFIDENCE_THRESHOLD}`
        : "item remains tentative"
    );
  });
  return { ...result, pending_questions: questions };
}

function normalizeContinuation(value, fieldPath, issues) {
  if (value === undefined) return null;
  if (!isPlainObject(value)) {
    issues.push(`${fieldPath}: expected object`);
    return null;
  }
  unknownFields(value, new Set(["before", "after"]), fieldPath, issues);
  const normalized = {};
  for (const key of ["before", "after"]) {
    if (value[key] === undefined) continue;
    if (value[key] !== true) issues.push(`${fieldPath}.${key}: expected true when present`);
    else normalized[key] = true;
  }
  if (!Object.keys(normalized).length) {
    issues.push(`${fieldPath}: expected before=true or after=true`);
    return null;
  }
  return normalized;
}

function validateAndNormalizeRoadmapDraft(value) {
  const issues = [];
  const warnings = [];
  if (!isPlainObject(value)) {
    return { ok: false, normalized: null, issues: ["roadmap draft: expected object"], warnings };
  }
  unknownFields(
    value,
    new Set(["schema_version", "kind", "title", "range", "lanes", "items", "pending_questions"]),
    "roadmap draft",
    issues
  );
  if (value.schema_version !== ROADMAP_DRAFT_SCHEMA_VERSION) {
    issues.push(`schema_version: expected ${ROADMAP_DRAFT_SCHEMA_VERSION}`);
  }
  if (value.kind !== ROADMAP_KIND_DRAFT) {
    issues.push(`kind: expected ${ROADMAP_KIND_DRAFT}`);
  }
  const title = text(value.title);
  if (!title) warnings.push("title: missing; compilation requires a roadmap title");
  else if (Array.from(title).length > 120) issues.push("title: expected at most 120 characters");

  let range = null;
  if (value.range !== undefined) {
    if (!isPlainObject(value.range)) {
      issues.push("range: expected object");
    } else {
      unknownFields(value.range, new Set(["start", "end", "granularity"]), "range", issues);
      const start = text(value.range.start);
      const end = text(value.range.end);
      const granularity = text(value.range.granularity) || "half-month";
      if (start && plainDateToDay(start) == null) issues.push("range.start: expected valid YYYY-MM-DD");
      if (end && plainDateToDay(end) == null) issues.push("range.end: expected valid YYYY-MM-DD");
      if (!GRANULARITIES.has(granularity)) {
        issues.push(`range.granularity: expected one of ${[...GRANULARITIES].join(", ")}`);
      }
      range = {
        ...(start ? { start } : {}),
        ...(end ? { end } : {}),
        granularity,
      };
    }
  }

  const lanes = [];
  if (!Array.isArray(value.lanes)) {
    issues.push("lanes: expected array");
  } else {
    value.lanes.forEach((lane, index) => {
      const fieldPath = `lanes.${index}`;
      if (!isPlainObject(lane)) {
        issues.push(`${fieldPath}: expected object`);
        return;
      }
      unknownFields(lane, new Set(["id", "label", "order", "raw", "source"]), fieldPath, issues);
      const id = text(lane.id);
      const label = text(lane.label);
      if (id && !ID_RE.test(id)) issues.push(`${fieldPath}.id: invalid stable id`);
      if (!label) issues.push(`${fieldPath}.label: required non-empty text`);
      else if (Array.from(label).length > 60) issues.push(`${fieldPath}.label: expected at most 60 characters`);
      const order = lane.order;
      if (!Number.isInteger(order) || order < 1) issues.push(`${fieldPath}.order: expected positive integer`);
      if (!isPlainObject(lane.raw)) issues.push(`${fieldPath}.raw: expected object with original fields`);
      const source = normalizeSource(lane.source, `${fieldPath}.source`, issues);
      lanes.push({
        ...(id ? { id } : {}),
        ...(label ? { label } : {}),
        order,
        raw: isPlainObject(lane.raw) ? clone(lane.raw) : {},
        ...(source ? { source } : {}),
      });
    });
  }

  const items = [];
  if (!Array.isArray(value.items)) {
    issues.push("items: expected array");
  } else {
    value.items.forEach((item, index) => {
      const fieldPath = `items.${index}`;
      if (!isPlainObject(item)) {
        issues.push(`${fieldPath}: expected object`);
        return;
      }
      unknownFields(
        item,
        new Set(["id", "lane_ref", "title", "start", "end", "kind", "certainty", "progress", "continuation", "color", "detail", "raw", "source"]),
        fieldPath,
        issues
      );
      const id = text(item.id);
      const laneRef = text(item.lane_ref);
      const titleValue = text(item.title);
      const start = text(item.start);
      const end = text(item.end);
      const kind = text(item.kind);
      const certainty = text(item.certainty);
      const progress = text(item.progress);
      const continuation = normalizeContinuation(
        item.continuation,
        `${fieldPath}.continuation`,
        issues
      );
      const color = text(item.color);
      const detail = text(item.detail);
      if (id && !ID_RE.test(id)) issues.push(`${fieldPath}.id: invalid stable id`);
      if (!laneRef) issues.push(`${fieldPath}.lane_ref: required non-empty text`);
      if (!titleValue) issues.push(`${fieldPath}.title: required non-empty text`);
      else if (Array.from(titleValue).length > 100) issues.push(`${fieldPath}.title: expected at most 100 characters`);
      if (start && plainDateToDay(start) == null) issues.push(`${fieldPath}.start: expected valid YYYY-MM-DD`);
      if (end && plainDateToDay(end) == null) issues.push(`${fieldPath}.end: expected valid YYYY-MM-DD`);
      if (!ITEM_KINDS.has(kind)) issues.push(`${fieldPath}.kind: expected bar or milestone`);
      if (certainty && !CERTAINTIES.has(certainty)) {
        issues.push(`${fieldPath}.certainty: expected confirmed or tentative`);
      }
      if (!PROGRESS_STATES.has(progress)) {
        issues.push(`${fieldPath}.progress: expected one of ${[...PROGRESS_STATES].join(", ")}`);
      }
      if (color && Array.from(color).length > 40) issues.push(`${fieldPath}.color: expected at most 40 characters`);
      if (detail && Array.from(detail).length > 500) issues.push(`${fieldPath}.detail: expected at most 500 characters`);
      if (!isPlainObject(item.raw)) issues.push(`${fieldPath}.raw: expected object with original fields`);
      const source = normalizeSource(item.source, `${fieldPath}.source`, issues);
      items.push({
        ...(id ? { id } : {}),
        ...(laneRef ? { lane_ref: laneRef } : {}),
        ...(titleValue ? { title: titleValue } : {}),
        ...(start ? { start } : {}),
        ...(end ? { end } : {}),
        kind,
        ...(certainty ? { certainty } : {}),
        progress,
        ...(continuation ? { continuation } : {}),
        ...(color ? { color } : {}),
        ...(detail ? { detail } : {}),
        raw: isPlainObject(item.raw) ? clone(item.raw) : {},
        ...(source ? { source } : {}),
      });
    });
  }

  let pendingQuestions = [];
  if (value.pending_questions === undefined) {
    issues.push("pending_questions: required array");
  } else {
    const questionResult = validateAndNormalizePendingQuestions(value.pending_questions);
    issues.push(...questionResult.issues);
    pendingQuestions = questionResult.pending_questions;
  }
  return {
    ok: issues.length === 0,
    normalized: {
      schema_version: ROADMAP_DRAFT_SCHEMA_VERSION,
      kind: ROADMAP_KIND_DRAFT,
      ...(title ? { title } : {}),
      ...(range ? { range } : {}),
      lanes,
      items,
      pending_questions: pendingQuestions,
    },
    issues,
    warnings,
  };
}

function fieldConfidence(source, field) {
  if (!source) return null;
  if (source.field_confidence && source.field_confidence[field] !== undefined) {
    return source.field_confidence[field];
  }
  return source.confidence;
}

function addQuestion(questions, fieldPath, prompt, reason) {
  if (questions.some(question => question.field_path === fieldPath)) return;
  questions.push({ field_path: fieldPath, prompt, reason });
}

function compileRoadmapDraft(value) {
  const draftResult = validateAndNormalizeRoadmapDraft(value);
  const issues = draftResult.issues.slice();
  const warnings = draftResult.warnings.slice();
  const draft = draftResult.normalized;
  const questions = draft ? draft.pending_questions.map(clone) : [];
  if (!draftResult.ok || !draft) {
    return { ok: false, spec: null, draft, issues, warnings, pending_questions: questions };
  }
  if (!draft.title) {
    issues.push("title: required to compile RoadmapSpec");
    addQuestion(questions, "title", "这份路线图的标题是什么？", "renderer requires a title");
  }
  if (!draft.lanes.length) issues.push("lanes: expected at least one lane");
  if (draft.lanes.length > MAX_LANES) issues.push(`lanes: expected at most ${MAX_LANES}`);
  if (!draft.items.length) issues.push("items: expected at least one item");
  if (draft.items.length > MAX_ITEMS) issues.push(`items: expected at most ${MAX_ITEMS}`);

  const laneIds = new Set();
  const laneAliases = new Map();
  const lanes = draft.lanes.map((lane, index) => {
    if (!lane.label) issues.push(`lanes.${index}.label: required to compile RoadmapSpec`);
    let id = stableId(lane.id, "lane", [lane.label, lane.order, JSON.stringify(lane.raw)]);
    if (!ID_RE.test(id)) issues.push(`lanes.${index}.id: invalid stable id`);
    if (laneIds.has(id)) {
      id = stableId(null, "lane", [lane.label, lane.order, JSON.stringify(lane.raw), index]);
    }
    if (laneIds.has(id)) issues.push(`lanes.${index}.id: duplicate id ${id}`);
    laneIds.add(id);
    [lane.id, lane.label, id].filter(Boolean).forEach(alias => laneAliases.set(alias, id));
    return { id, label: lane.label || "待确认泳道", order: lane.order };
  });

  const itemIds = new Set();
  const items = draft.items.map((item, index) => {
    const fieldPath = `items.${index}`;
    if (!item.title) issues.push(`${fieldPath}.title: required to compile RoadmapSpec`);
    const laneId = laneAliases.get(item.lane_ref);
    if (!laneId) issues.push(`${fieldPath}.lane_ref: unknown lane ${JSON.stringify(item.lane_ref || "")}`);
    if (!item.start) {
      issues.push(`${fieldPath}.start: required to compile RoadmapSpec`);
      addQuestion(questions, `${fieldPath}.start`, `“${item.title || "该任务"}”从哪一天开始？`, "missing date cannot be invented");
    }
    if (item.kind === "bar" && !item.end) {
      issues.push(`${fieldPath}.end: required for bar item`);
      addQuestion(questions, `${fieldPath}.end`, `“${item.title || "该任务"}”在哪一天结束（结束日不包含）？`, "bar interval uses [start, end)");
    }
    const startConfidence = fieldConfidence(item.source, "start");
    const endConfidence = item.kind === "milestone" ? 1 : fieldConfidence(item.source, "end");
    const lowDateConfidence = (
      (startConfidence != null && startConfidence < LOW_CONFIDENCE_THRESHOLD)
      || (endConfidence != null && endConfidence < LOW_CONFIDENCE_THRESHOLD)
    );
    let certainty = item.certainty || (lowDateConfidence ? "tentative" : "confirmed");
    if (lowDateConfidence && certainty !== "tentative") {
      certainty = "tentative";
      warnings.push(`${fieldPath}.certainty: normalized to tentative from low-confidence date source`);
    }
    if (lowDateConfidence) {
      addQuestion(
        questions,
        `${fieldPath}.start`,
        `请确认“${item.title || "该任务"}”的起止日期。`,
        `date confidence is below ${LOW_CONFIDENCE_THRESHOLD}`
      );
    }
    let id = stableId(item.id, "item", [
      item.title,
      item.lane_ref,
      item.start,
      item.end,
      item.kind,
      JSON.stringify(item.raw),
    ]);
    if (itemIds.has(id)) {
      id = stableId(null, "item", [item.title, item.lane_ref, item.start, item.end, JSON.stringify(item.raw), index]);
    }
    if (itemIds.has(id)) issues.push(`${fieldPath}.id: duplicate id ${id}`);
    itemIds.add(id);
    return {
      id,
      lane_id: laneId || "invalid-lane",
      title: item.title || "待确认任务",
      ...(item.start ? { start: item.start } : {}),
      ...(item.kind === "bar" && item.end ? { end: item.end } : {}),
      kind: item.kind,
      certainty,
      progress: item.progress,
      ...(item.continuation ? { continuation: clone(item.continuation) } : {}),
      ...(item.color ? { color: item.color } : {}),
      ...(item.detail ? { detail: item.detail } : {}),
      ...(item.source ? { source: clone(item.source) } : {}),
    };
  });

  const validStarts = items.map(item => plainDateToDay(item.start)).filter(Number.isInteger);
  const validEnds = items.map(item => {
    const startDay = plainDateToDay(item.start);
    if (item.kind === "milestone") return startDay == null ? null : startDay + 1;
    return plainDateToDay(item.end);
  }).filter(Number.isInteger);
  const rangeStart = draft.range && draft.range.start
    ? draft.range.start
    : (validStarts.length ? dayToPlainDate(Math.min(...validStarts)) : null);
  const rangeEnd = draft.range && draft.range.end
    ? draft.range.end
    : (validEnds.length ? dayToPlainDate(Math.max(...validEnds)) : null);
  if (!rangeStart) issues.push("range.start: missing and cannot be derived from items");
  if (!rangeEnd) issues.push("range.end: missing and cannot be derived from items");

  if (issues.length) {
    return { ok: false, spec: null, draft, issues, warnings, pending_questions: questions };
  }
  const specCandidate = {
    schema_version: ROADMAP_SPEC_SCHEMA_VERSION,
    kind: ROADMAP_KIND_SPEC,
    title: draft.title,
    range: {
      start: rangeStart,
      end: rangeEnd,
      granularity: draft.range && draft.range.granularity || "half-month",
    },
    lanes,
    items,
  };
  const specResult = validateAndNormalizeRoadmapSpec(specCandidate);
  return {
    ok: specResult.ok,
    spec: specResult.ok ? specResult.normalized : null,
    draft,
    issues: specResult.issues,
    warnings: [...warnings, ...specResult.warnings],
    pending_questions: questions,
  };
}

function validateAndNormalizeRoadmapSpec(value) {
  const issues = [];
  const warnings = [];
  if (!isPlainObject(value)) {
    return { ok: false, normalized: null, issues: ["RoadmapSpec: expected object"], warnings };
  }
  unknownFields(value, new Set(["schema_version", "kind", "title", "range", "lanes", "items", "legend"]), "RoadmapSpec", issues);
  if (value.schema_version !== ROADMAP_SPEC_SCHEMA_VERSION) {
    issues.push(`schema_version: expected ${ROADMAP_SPEC_SCHEMA_VERSION}`);
  }
  if (value.kind !== ROADMAP_KIND_SPEC) issues.push(`kind: expected ${ROADMAP_KIND_SPEC}`);
  const title = text(value.title);
  if (!title || Array.from(title).length > 120) issues.push("title: expected 1-120 characters");

  let range = { start: "", end: "", granularity: "half-month" };
  if (!isPlainObject(value.range)) {
    issues.push("range: expected object");
  } else {
    unknownFields(value.range, new Set(["start", "end", "granularity"]), "range", issues);
    const start = text(value.range.start);
    const end = text(value.range.end);
    const granularity = text(value.range.granularity);
    const startDay = plainDateToDay(start);
    const endDay = plainDateToDay(end);
    if (startDay == null) issues.push("range.start: expected valid YYYY-MM-DD");
    if (endDay == null) issues.push("range.end: expected valid YYYY-MM-DD");
    if (startDay != null && endDay != null) {
      if (startDay >= endDay) issues.push("range: expected [start, end) with start before end");
      if (endDay - startDay > MAX_RANGE_DAYS) {
        issues.push(`range: expected at most ${MAX_RANGE_DAYS} days for one page`);
      }
    }
    if (!GRANULARITIES.has(granularity)) {
      issues.push(`range.granularity: expected one of ${[...GRANULARITIES].join(", ")}`);
    }
    range = { start, end, granularity };
  }

  const lanes = [];
  const laneIds = new Set();
  const laneOrders = new Set();
  if (!Array.isArray(value.lanes) || !value.lanes.length) {
    issues.push("lanes: expected 1-8 lanes");
  } else {
    if (value.lanes.length > MAX_LANES) issues.push(`lanes: expected at most ${MAX_LANES}`);
    value.lanes.forEach((lane, index) => {
      const fieldPath = `lanes.${index}`;
      if (!isPlainObject(lane)) {
        issues.push(`${fieldPath}: expected object`);
        return;
      }
      unknownFields(lane, new Set(["id", "label", "order"]), fieldPath, issues);
      const id = text(lane.id);
      const label = text(lane.label);
      const order = lane.order;
      if (!ID_RE.test(id)) issues.push(`${fieldPath}.id: invalid stable id`);
      if (laneIds.has(id)) issues.push(`${fieldPath}.id: duplicate id ${id}`);
      laneIds.add(id);
      if (!label || Array.from(label).length > 60) issues.push(`${fieldPath}.label: expected 1-60 characters`);
      if (!Number.isInteger(order) || order < 1) issues.push(`${fieldPath}.order: expected positive integer`);
      if (laneOrders.has(order)) issues.push(`${fieldPath}.order: duplicate order ${order}`);
      laneOrders.add(order);
      lanes.push({ id, label, order });
    });
  }
  lanes.sort((left, right) => left.order - right.order || left.id.localeCompare(right.id));
  const laneOrder = new Map(lanes.map((lane, index) => [lane.id, index]));

  const items = [];
  const itemIds = new Set();
  if (!Array.isArray(value.items) || !value.items.length) {
    issues.push("items: expected 1-80 items");
  } else {
    if (value.items.length > MAX_ITEMS) issues.push(`items: expected at most ${MAX_ITEMS}`);
    value.items.forEach((item, index) => {
      const fieldPath = `items.${index}`;
      if (!isPlainObject(item)) {
        issues.push(`${fieldPath}: expected object`);
        return;
      }
      unknownFields(
        item,
        new Set(["id", "lane_id", "title", "start", "end", "kind", "certainty", "progress", "continuation", "color", "detail", "source"]),
        fieldPath,
        issues
      );
      const id = text(item.id);
      const laneId = text(item.lane_id);
      const titleValue = text(item.title);
      const start = text(item.start);
      const end = text(item.end);
      const kind = text(item.kind);
      const certainty = text(item.certainty);
      const progress = text(item.progress);
      const continuation = normalizeContinuation(
        item.continuation,
        `${fieldPath}.continuation`,
        issues
      );
      const color = text(item.color);
      const detail = text(item.detail);
      if (!ID_RE.test(id)) issues.push(`${fieldPath}.id: invalid stable id`);
      if (itemIds.has(id)) issues.push(`${fieldPath}.id: duplicate id ${id}`);
      itemIds.add(id);
      if (!laneIds.has(laneId)) issues.push(`${fieldPath}.lane_id: unknown lane ${JSON.stringify(laneId)}`);
      if (!titleValue || Array.from(titleValue).length > 100) issues.push(`${fieldPath}.title: expected 1-100 characters`);
      const startDay = plainDateToDay(start);
      const endDay = plainDateToDay(end);
      if (startDay == null) issues.push(`${fieldPath}.start: expected valid YYYY-MM-DD`);
      if (!ITEM_KINDS.has(kind)) issues.push(`${fieldPath}.kind: expected bar or milestone`);
      if (kind === "bar") {
        if (endDay == null) issues.push(`${fieldPath}.end: required valid YYYY-MM-DD for bar`);
        else if (startDay != null && startDay >= endDay) {
          issues.push(`${fieldPath}: expected [start, end) with start before end`);
        }
      } else if (kind === "milestone" && item.end !== undefined) {
        issues.push(`${fieldPath}.end: milestone uses start only`);
      }
      if (continuation && kind !== "bar") {
        issues.push(`${fieldPath}.continuation: only bar items may continue`);
      }
      if (!CERTAINTIES.has(certainty)) issues.push(`${fieldPath}.certainty: expected confirmed or tentative`);
      if (!PROGRESS_STATES.has(progress)) {
        issues.push(`${fieldPath}.progress: expected one of ${[...PROGRESS_STATES].join(", ")}`);
      }
      if (color && Array.from(color).length > 40) issues.push(`${fieldPath}.color: expected at most 40 characters`);
      if (detail && Array.from(detail).length > 500) issues.push(`${fieldPath}.detail: expected at most 500 characters`);
      const source = item.source === undefined
        ? null
        : normalizeSource(item.source, `${fieldPath}.source`, issues);
      const rangeStart = plainDateToDay(range.start);
      const rangeEnd = plainDateToDay(range.end);
      const effectiveEnd = kind === "milestone" && startDay != null ? startDay + 1 : endDay;
      if (
        startDay != null
        && effectiveEnd != null
        && rangeStart != null
        && rangeEnd != null
        && (startDay < rangeStart || effectiveEnd > rangeEnd)
      ) {
        issues.push(`${fieldPath}: item interval must stay within roadmap range`);
      }
      if (continuation && continuation.before && start !== range.start) {
        issues.push(`${fieldPath}.continuation.before: item must start at range.start`);
      }
      if (continuation && continuation.after && end !== range.end) {
        issues.push(`${fieldPath}.continuation.after: item must end at range.end`);
      }
      items.push({
        id,
        lane_id: laneId,
        title: titleValue,
        start,
        ...(kind === "bar" ? { end } : {}),
        kind,
        certainty,
        progress,
        ...(continuation ? { continuation } : {}),
        ...(color ? { color } : {}),
        ...(detail ? { detail } : {}),
        ...(source ? { source } : {}),
      });
    });
  }
  items.sort((left, right) => {
    const laneDelta = (laneOrder.get(left.lane_id) ?? Number.MAX_SAFE_INTEGER)
      - (laneOrder.get(right.lane_id) ?? Number.MAX_SAFE_INTEGER);
    if (laneDelta) return laneDelta;
    const startDelta = (plainDateToDay(left.start) ?? 0) - (plainDateToDay(right.start) ?? 0);
    if (startDelta) return startDelta;
    const leftEnd = left.kind === "milestone" ? plainDateToDay(left.start) + 1 : plainDateToDay(left.end);
    const rightEnd = right.kind === "milestone" ? plainDateToDay(right.start) + 1 : plainDateToDay(right.end);
    return leftEnd - rightEnd || left.id.localeCompare(right.id);
  });

  let legend;
  if (value.legend !== undefined) {
    if (!Array.isArray(value.legend)) issues.push("legend: expected array");
    else {
      legend = value.legend.map((entry, index) => {
        if (!isPlainObject(entry)) {
          issues.push(`legend.${index}: expected object`);
          return null;
        }
        unknownFields(entry, new Set(["key", "label"]), `legend.${index}`, issues);
        const label = text(entry.label);
        const key = text(entry.key);
        if (!key || !label) issues.push(`legend.${index}: key and label are required`);
        return key && label ? { key, label } : null;
      }).filter(Boolean);
    }
  }

  return {
    ok: issues.length === 0,
    normalized: {
      schema_version: ROADMAP_SPEC_SCHEMA_VERSION,
      kind: ROADMAP_KIND_SPEC,
      title,
      range,
      lanes,
      items,
      ...(legend ? { legend } : {}),
    },
    issues,
    warnings,
  };
}

function compileRoadmapInput(value) {
  if (isPlainObject(value) && value.kind === ROADMAP_KIND_SPEC) {
    const result = validateAndNormalizeRoadmapSpec(value);
    const questionResult = result.ok
      ? pendingQuestionsForRoadmapSpec(result.normalized)
      : { pending_questions: [] };
    return {
      ok: result.ok,
      input_kind: ROADMAP_KIND_SPEC,
      spec: result.ok ? result.normalized : null,
      issues: result.issues,
      warnings: result.warnings,
      pending_questions: questionResult.pending_questions,
    };
  }
  const result = compileRoadmapDraft(value);
  return { ...result, input_kind: ROADMAP_KIND_DRAFT };
}

module.exports = {
  CERTAINTIES,
  GRANULARITIES,
  ITEM_KINDS,
  LOW_CONFIDENCE_THRESHOLD,
  MAX_ITEMS,
  MAX_LANES,
  MAX_RANGE_DAYS,
  PROGRESS_STATES,
  ROADMAP_DRAFT_SCHEMA_VERSION,
  ROADMAP_KIND_DRAFT,
  ROADMAP_KIND_SPEC,
  ROADMAP_SPEC_SCHEMA_VERSION,
  SOURCE_TYPES,
  compileRoadmapDraft,
  compileRoadmapInput,
  dayToPlainDate,
  plainDateToDay,
  pendingQuestionsForRoadmapSpec,
  validateAndNormalizeRoadmapDraft,
  validateAndNormalizePendingQuestions,
  validateAndNormalizeRoadmapSpec,
};
