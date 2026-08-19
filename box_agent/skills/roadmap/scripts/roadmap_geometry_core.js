"use strict";

const {
  dayToPlainDate,
  plainDateToDay,
  validateAndNormalizeRoadmapSpec,
} = require("./roadmap_contract_core.js");

const DEFAULT_VIEWPORT = Object.freeze({ width: 1440, height: 900 });
const MIN_VIEWPORT = Object.freeze({ width: 640, height: 360 });
const LAYOUT = Object.freeze({
  outerPadding: 32,
  titleHeight: 28,
  headerHeight: 76,
  laneLabelWidth: 260,
  lanePadding: 20,
  trackHeight: 50,
  trackGap: 14,
  barHeight: 46,
  milestoneSize: 18,
  minLaneHeight: 124,
  labelHeight: 46,
  labelGap: 10,
  collisionGap: 10,
  continuationSize: 10,
});

function round(value) {
  return Math.round(value * 1000) / 1000;
}

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function normalizeViewport(value) {
  const width = value && Number.isFinite(value.width) ? value.width : DEFAULT_VIEWPORT.width;
  const height = value && Number.isFinite(value.height) ? value.height : DEFAULT_VIEWPORT.height;
  if (width < MIN_VIEWPORT.width || height < MIN_VIEWPORT.height) {
    throw new Error(
      `viewport: expected at least ${MIN_VIEWPORT.width}x${MIN_VIEWPORT.height}, got ${width}x${height}`
    );
  }
  return { width: round(width), height: round(height) };
}

function nextMonthStart(day) {
  const date = new Date(day * 86400000);
  return Math.floor(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1) / 86400000);
}

function monthStart(day) {
  const date = new Date(day * 86400000);
  return Math.floor(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1) / 86400000);
}

function halfMonthBoundaries(startDay, endDay) {
  const boundaries = new Set([startDay, endDay]);
  let cursor = monthStart(startDay);
  while (cursor < endDay) {
    const date = new Date(cursor * 86400000);
    const firstHalfEnd = Math.floor(
      Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 16) / 86400000
    );
    const next = nextMonthStart(cursor);
    if (firstHalfEnd > startDay && firstHalfEnd < endDay) boundaries.add(firstHalfEnd);
    if (next > startDay && next < endDay) boundaries.add(next);
    cursor = next;
  }
  return [...boundaries].sort((left, right) => left - right);
}

function monthSegments(startDay, endDay) {
  const segments = [];
  let cursor = monthStart(startDay);
  while (cursor < endDay) {
    const next = nextMonthStart(cursor);
    const clippedStart = Math.max(startDay, cursor);
    const clippedEnd = Math.min(endDay, next);
    if (clippedStart < clippedEnd) {
      const date = new Date(cursor * 86400000);
      segments.push({
        start: clippedStart,
        end: clippedEnd,
        label: `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}`,
      });
    }
    cursor = next;
  }
  return segments;
}

function estimatedLabelWidth(title) {
  const width = Array.from(title).reduce(
    (total, character) => total + (/^[\u0000-\u00ff]$/.test(character) ? 8 : 15),
    0
  );
  return Math.min(260, Math.max(72, width));
}

function visualBounds(item, options) {
  const startDay = plainDateToDay(item.start);
  const endDay = item.kind === "milestone"
    ? startDay + 1
    : plainDateToDay(item.end);
  const startX = options.xForDay(startDay);
  const endX = options.xForDay(endDay);
  const labelWidth = estimatedLabelWidth(item.title);
  if (item.kind === "milestone") {
    const rightStart = startX + LAYOUT.milestoneSize / 2 + LAYOUT.labelGap;
    if (rightStart + labelWidth <= options.plotRight) {
      return {
        startDay,
        endDay,
        labelWidth,
        labelPlacement: "outside-right",
        visualStart: startX - LAYOUT.milestoneSize / 2,
        visualEnd: rightStart + labelWidth,
      };
    }
    const leftEnd = startX - LAYOUT.milestoneSize / 2 - LAYOUT.labelGap;
    return {
      startDay,
      endDay,
      labelWidth,
      labelPlacement: "outside-left",
      visualStart: Math.max(options.plotLeft, leftEnd - labelWidth),
      visualEnd: startX + LAYOUT.milestoneSize / 2,
    };
  }
  const width = endX - startX;
  if (width >= labelWidth + LAYOUT.labelGap * 2) {
    return {
      startDay,
      endDay,
      labelWidth,
      labelPlacement: "inside",
      visualStart: startX,
      visualEnd: endX,
    };
  }
  const rightStart = endX + LAYOUT.labelGap;
  if (rightStart + labelWidth <= options.plotRight) {
    return {
      startDay,
      endDay,
      labelWidth,
      labelPlacement: "outside-right",
      visualStart: startX,
      visualEnd: rightStart + labelWidth,
    };
  }
  const leftEnd = startX - LAYOUT.labelGap;
  return {
    startDay,
    endDay,
    labelWidth,
    labelPlacement: "outside-left",
    visualStart: Math.max(options.plotLeft, leftEnd - labelWidth),
    visualEnd: endX,
  };
}

function assignTracks(items, options) {
  const entries = items.map((item, index) => ({
    item,
    index,
    ...visualBounds(item, options),
  }));
  entries.sort((left, right) => (
    left.visualStart - right.visualStart
    || left.visualEnd - right.visualEnd
    || left.item.id.localeCompare(right.item.id)
  ));
  const trackEnds = [];
  entries.forEach(entry => {
    let track = trackEnds.findIndex(
      trackEnd => trackEnd + LAYOUT.collisionGap <= entry.visualStart
    );
    if (track === -1) {
      track = trackEnds.length;
      trackEnds.push(entry.visualEnd);
    } else {
      trackEnds[track] = entry.visualEnd;
    }
    entry.track = track;
  });
  return entries.sort((left, right) => left.index - right.index);
}

function layoutRoadmap(value, viewportValue = DEFAULT_VIEWPORT) {
  const validation = validateAndNormalizeRoadmapSpec(value);
  if (!validation.ok) {
    throw new Error(`RoadmapSpec invalid:\n${validation.issues.join("\n")}`);
  }
  const spec = validation.normalized;
  const viewport = normalizeViewport(viewportValue);
  const rangeStart = plainDateToDay(spec.range.start);
  const rangeEnd = plainDateToDay(spec.range.end);
  const rangeDays = rangeEnd - rangeStart;
  const plotLeft = LAYOUT.outerPadding + LAYOUT.laneLabelWidth;
  const plotRight = viewport.width - LAYOUT.outerPadding;
  const plotWidth = plotRight - plotLeft;
  const headerTop = LAYOUT.outerPadding + LAYOUT.titleHeight;
  const laneTop = headerTop + LAYOUT.headerHeight;
  const xForDay = day => round(plotLeft + ((day - rangeStart) / rangeDays) * plotWidth);

  const itemsByLane = new Map(spec.lanes.map(lane => [lane.id, []]));
  spec.items.forEach(item => itemsByLane.get(item.lane_id).push(item));
  const laneTrackRecords = spec.lanes.map(lane => {
    const assigned = assignTracks(itemsByLane.get(lane.id), {
      plotLeft,
      plotRight,
      xForDay,
    });
    const trackCount = Math.max(1, ...assigned.map(entry => entry.track + 1));
    const contentHeight = (trackCount * LAYOUT.trackHeight)
      + ((trackCount - 1) * LAYOUT.trackGap)
      + (LAYOUT.lanePadding * 2);
    return {
      lane,
      assigned,
      trackCount,
      height: Math.max(LAYOUT.minLaneHeight, contentHeight),
    };
  });
  const requiredHeight = laneTop
    + laneTrackRecords.reduce((sum, record) => sum + record.height, 0)
    + LAYOUT.outerPadding;
  const canvasHeight = Math.max(MIN_VIEWPORT.height, requiredHeight);

  const headers = [];
  monthSegments(rangeStart, rangeEnd).forEach((segment, index) => {
    headers.push({
      id: `month-${index + 1}`,
      kind: "month",
      label: segment.label,
      start: dayToPlainDate(segment.start),
      end: dayToPlainDate(segment.end),
      x: xForDay(segment.start),
      y: round(headerTop),
      width: round(xForDay(segment.end) - xForDay(segment.start)),
      height: round(LAYOUT.headerHeight / 2),
    });
  });
  const halfBoundaries = halfMonthBoundaries(rangeStart, rangeEnd);
  halfBoundaries.slice(0, -1).forEach((startDay, index) => {
    const endDay = halfBoundaries[index + 1];
    const date = new Date(startDay * 86400000);
    headers.push({
      id: `half-month-${index + 1}`,
      kind: "half-month",
      label: date.getUTCDate() <= 15 ? "上半月" : "下半月",
      start: dayToPlainDate(startDay),
      end: dayToPlainDate(endDay),
      x: xForDay(startDay),
      y: round(headerTop + LAYOUT.headerHeight / 2),
      width: round(xForDay(endDay) - xForDay(startDay)),
      height: round(LAYOUT.headerHeight / 2),
    });
  });

  const lanes = [];
  const bars = [];
  const milestones = [];
  const labels = [];
  const continuations = [];
  let currentY = laneTop;
  laneTrackRecords.forEach(record => {
    const laneY = currentY;
    lanes.push({
      id: record.lane.id,
      label: record.lane.label,
      order: record.lane.order,
      x: round(LAYOUT.outerPadding),
      y: round(laneY),
      width: round(viewport.width - LAYOUT.outerPadding * 2),
      height: round(record.height),
      track_count: record.trackCount,
    });
    record.assigned.forEach(entry => {
      const trackY = laneY
        + LAYOUT.lanePadding
        + entry.track * (LAYOUT.trackHeight + LAYOUT.trackGap);
      const startX = xForDay(entry.startDay);
      const endX = xForDay(entry.endDay);
      if (entry.item.kind === "milestone") {
        const x = startX;
        milestones.push({
          id: entry.item.id,
          lane_id: record.lane.id,
          title: entry.item.title,
          date: entry.item.start,
          track: entry.track,
          x,
          y: round(trackY + LAYOUT.barHeight / 2),
          size: LAYOUT.milestoneSize,
          certainty: entry.item.certainty,
          progress: entry.item.progress,
          line_style: entry.item.certainty === "tentative" ? "dashed" : "solid",
          ...(entry.item.color ? { color: entry.item.color } : {}),
        });
        const labelOnLeft = entry.labelPlacement === "outside-left";
        const preferredX = labelOnLeft
          ? x - LAYOUT.milestoneSize / 2 - LAYOUT.labelGap - entry.labelWidth
          : x + LAYOUT.milestoneSize / 2 + LAYOUT.labelGap;
        labels.push({
          id: `label-${entry.item.id}`,
          item_id: entry.item.id,
          lane_id: record.lane.id,
          text: entry.item.title,
          placement: entry.labelPlacement,
          x: round(clamp(preferredX, plotLeft + 4, plotRight - entry.labelWidth - 4)),
          y: round(trackY),
          width: round(entry.labelWidth),
          height: LAYOUT.labelHeight,
        });
        return;
      }
      const width = round(endX - startX);
      const placement = entry.labelPlacement;
      bars.push({
        id: entry.item.id,
        lane_id: record.lane.id,
        title: entry.item.title,
        start: entry.item.start,
        end: entry.item.end,
        track: entry.track,
        x: startX,
        y: round(trackY),
        width,
        height: LAYOUT.barHeight,
        certainty: entry.item.certainty,
        progress: entry.item.progress,
        line_style: entry.item.certainty === "tentative" ? "dashed" : "solid",
        ...(entry.item.color ? { color: entry.item.color } : {}),
      });
      const labelX = placement === "inside"
        ? startX + LAYOUT.labelGap
        : placement === "outside-left"
          ? startX - LAYOUT.labelGap - entry.labelWidth
          : endX + LAYOUT.labelGap;
      const renderedLabelWidth = placement === "inside"
        ? Math.max(0, width - LAYOUT.labelGap * 2)
        : entry.labelWidth;
      labels.push({
        id: `label-${entry.item.id}`,
        item_id: entry.item.id,
        lane_id: record.lane.id,
        text: entry.item.title,
        placement,
        x: round(clamp(labelX, plotLeft + 4, plotRight - renderedLabelWidth - 4)),
        y: round(trackY),
        width: round(renderedLabelWidth),
        height: LAYOUT.labelHeight,
      });
      for (const direction of ["before", "after"]) {
        if (!entry.item.continuation || entry.item.continuation[direction] !== true) continue;
        continuations.push({
          id: `continuation-${entry.item.id}-${direction}`,
          item_id: entry.item.id,
          lane_id: record.lane.id,
          direction,
          x: direction === "before" ? round(plotLeft) : round(plotRight),
          y: round(trackY + LAYOUT.barHeight / 2),
          size: LAYOUT.continuationSize,
        });
      }
    });
    currentY += record.height;
  });

  return {
    schema_version: 1,
    kind: "roadmap-geometry",
    source_schema_version: spec.schema_version,
    canvas: {
      width: viewport.width,
      height: round(canvasHeight),
      plot_left: round(plotLeft),
      plot_right: round(plotRight),
      header_top: round(headerTop),
      header_height: LAYOUT.headerHeight,
    },
    headers,
    lanes,
    bars,
    milestones,
    labels,
    continuations,
  };
}

module.exports = {
  DEFAULT_VIEWPORT,
  LAYOUT,
  assignTracks,
  estimatedLabelWidth,
  halfMonthBoundaries,
  layoutRoadmap,
  monthSegments,
  normalizeViewport,
};
