(function attachDeckDiagramRuntime() {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const DIAGRAM_SELECTOR = "[data-pptx-diagram]";
  // A single CJK-capable face first is intentional. Browser font fallback can
  // switch per glyph, but Office/LibreOffice SVG renderers may keep the first
  // Latin face for the whole text run and drop Chinese glyphs.
  const SVG_FONT_FAMILY = "'Arial Unicode MS', 'PingFang SC', 'Microsoft YaHei', sans-serif";
  const SVG_PPTX_FONT = "Arial Unicode MS";
  let instanceSequence = 0;

  function escapeXml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function cssColor(style, name, fallback) {
    const value = style.getPropertyValue(name).trim();
    return value || fallback;
  }

  function paletteFor(root) {
    const style = getComputedStyle(root);
    return {
      background: cssColor(style, "--deck-bg", "#f7faff"),
      surface: cssColor(style, "--deck-surface", "#ffffff"),
      surfaceStrong: cssColor(style, "--deck-surface-strong", "#eaf1fb"),
      primary: cssColor(style, "--deck-primary-text", "#1d5dcc"),
      primaryFill: cssColor(style, "--deck-primary", "#1d5dcc"),
      primarySoft: cssColor(style, "--deck-primary-soft", "#dce9ff"),
      text: cssColor(style, "--deck-text", "#14233a"),
      muted: cssColor(style, "--deck-muted", "#53657b"),
      border: cssColor(style, "--deck-border", "#b6c7e3"),
      inverse: cssColor(style, "--deck-inverse", "#ffffff"),
    };
  }

  function parseSpec(root) {
    const raw = root.getAttribute("data-diagram-spec");
    if (!raw) {
      throw new Error("Controlled diagram runtime requires inline data-diagram-spec JSON.");
    }
    const spec = JSON.parse(raw);
    const nodeIds = new Set();
    if (!spec || spec.version !== 1 || !Array.isArray(spec.nodes) || !Array.isArray(spec.edges)) {
      throw new Error("DiagramSpec must use version 1 with nodes and edges arrays.");
    }
    spec.nodes.forEach((node, index) => {
      if (!node || !node.id || nodeIds.has(node.id)) {
        throw new Error(`DiagramSpec node ${index + 1} has a missing or duplicate id.`);
      }
      nodeIds.add(node.id);
    });
    spec.edges.forEach((edge, index) => {
      if (!edge || !nodeIds.has(edge.source) || !nodeIds.has(edge.target)) {
        throw new Error(`DiagramSpec edge ${index + 1} references an unknown node.`);
      }
    });
    return spec;
  }

  function directionFor(spec) {
    if (spec.direction === "DOWN" || spec.direction === "UP" || spec.direction === "LEFT") {
      return spec.direction;
    }
    return "RIGHT";
  }

  function diagramKind(spec) {
    return spec.kind === "integration" || spec.kind === "pipeline"
      ? spec.kind
      : "architecture";
  }

  function renderProfileFor(spec) {
    const kind = diagramKind(spec);
    if (kind === "integration") {
      return {
        strategy: "center-hub",
        padding: 34,
        maxScale: 1.08,
        labelFontSize: 22,
        labelLineHeight: 26,
        labelMaxChars: 13,
        detailFontSize: 13,
        detailMaxChars: 28,
        kindFontSize: 11,
        edgeFontSize: 11,
        edgeLabelHeight: 26,
        edgeLabelCharWidth: 11,
        edgeLabelMaxChars: 16,
        cornerRadius: 16,
      };
    }
    if (kind === "pipeline") {
      return {
        strategy: "wrapped-pipeline",
        padding: 26,
        maxScale: 1.14,
        labelFontSize: spec.nodes.length <= 3 ? 38 : 30,
        labelLineHeight: spec.nodes.length <= 3 ? 44 : 34,
        labelMaxChars: 10,
        detailFontSize: spec.nodes.length <= 3 ? 24 : 20,
        detailMaxChars: 20,
        kindFontSize: 10,
        edgeFontSize: 11,
        edgeLabelHeight: 24,
        edgeLabelCharWidth: 11,
        edgeLabelMaxChars: 13,
        cornerRadius: 14,
      };
    }
    return {
      strategy: "layered-architecture",
      padding: 30,
      maxScale: 1.42,
      labelFontSize: 34,
      labelLineHeight: 38,
      labelMaxChars: 12,
      detailFontSize: 22,
      detailMaxChars: 28,
      kindFontSize: 12,
      edgeFontSize: 13,
      edgeLabelHeight: 28,
      edgeLabelCharWidth: 12,
      edgeLabelMaxChars: 16,
      cornerRadius: 16,
    };
  }

  function nodeSize(node, spec) {
    const labelLength = Array.from(String(node.label || "")).length;
    const hub = node.kind === "hub" || node.kind === "platform";
    const kind = diagramKind(spec);
    if (kind === "integration") {
      return {
        width: hub ? 324 : Math.max(226, Math.min(252, 214 + labelLength * 4)),
        height: hub ? 136 : 82,
      };
    }
    if (kind === "pipeline") {
      const columns = Math.min(6, spec.nodes.length);
      return {
        width: Math.min(380, (1394 - (columns - 1) * 32) / columns),
        height: spec.nodes.length <= 3 ? 190 : 164,
      };
    }
    return {
      width: hub ? 380 : Math.max(320, Math.min(360, 300 + labelLength * 5)),
      height: 146,
    };
  }

  function edgeLabelSize(edge, spec) {
    const profile = renderProfileFor(spec);
    const label = Array.from(String(edge && edge.label || ""))
      .slice(0, profile.edgeLabelMaxChars)
      .join("");
    return {
      width: Math.max(48, Math.min(172, 22 + Array.from(label).length * profile.edgeLabelCharWidth)),
      height: profile.edgeLabelHeight,
    };
  }

  function toElkGraph(spec) {
    const direction = directionFor(spec);
    return {
      id: "diagram-root",
      layoutOptions: {
        "elk.algorithm": "layered",
        "elk.direction": direction,
        "elk.edgeRouting": "ORTHOGONAL",
        "elk.padding": "[top=20,left=20,bottom=20,right=20]",
        "elk.spacing.nodeNode": "28",
        "elk.spacing.edgeNode": "24",
        "elk.spacing.edgeEdge": "16",
        "elk.layered.spacing.nodeNodeBetweenLayers": "28",
        "elk.layered.spacing.edgeNodeBetweenLayers": "28",
        "elk.layered.spacing.edgeEdgeBetweenLayers": "18",
        "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
        "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
        "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      },
      children: spec.nodes.map(node => ({
        id: node.id,
        ...nodeSize(node, spec),
      })),
      edges: spec.edges.map((edge, index) => ({
        id: edge.id || `edge-${index + 1}`,
        sources: [edge.source],
        targets: [edge.target],
        // Small diagrams place labels after routing below. ELK's center-label
        // dummy layers otherwise consume most of a four-level slide's height.
        labels: edge.label && spec.nodes.length > 8 ? [{
          id: `${edge.id || `edge-${index + 1}`}-label`,
          text: String(edge.label),
          ...edgeLabelSize(edge, spec),
          layoutOptions: { "elk.edgeLabels.placement": "CENTER" },
        }] : [],
      })),
    };
  }

  function edgeId(edge, index) {
    return edge.id || `edge-${index + 1}`;
  }

  function nodeCenter(node) {
    return {
      x: node.x + node.width / 2,
      y: node.y + node.height / 2,
    };
  }

  function evenlyStackedY(count, nodeHeight, height) {
    if (count <= 0) return [];
    const gap = Math.max(14, (height - count * nodeHeight) / (count + 1));
    return Array.from({ length: count }, (_, index) => gap + index * (nodeHeight + gap));
  }

  function orthogonalBetween(source, target) {
    const sourceCenter = nodeCenter(source);
    const targetCenter = nodeCenter(target);
    const horizontal = Math.abs(targetCenter.x - sourceCenter.x) >= Math.abs(targetCenter.y - sourceCenter.y);
    if (horizontal) {
      const leftToRight = targetCenter.x >= sourceCenter.x;
      const startPoint = {
        x: leftToRight ? source.x + source.width : source.x,
        y: sourceCenter.y,
      };
      const endPoint = {
        x: leftToRight ? target.x : target.x + target.width,
        y: targetCenter.y,
      };
      const middleX = (startPoint.x + endPoint.x) / 2;
      return {
        startPoint,
        bendPoints: [
          { x: middleX, y: startPoint.y },
          { x: middleX, y: endPoint.y },
        ],
        endPoint,
      };
    }
    const topToBottom = targetCenter.y >= sourceCenter.y;
    const startPoint = {
      x: sourceCenter.x,
      y: topToBottom ? source.y + source.height : source.y,
    };
    const endPoint = {
      x: targetCenter.x,
      y: topToBottom ? target.y : target.y + target.height,
    };
    const middleY = (startPoint.y + endPoint.y) / 2;
    return {
      startPoint,
      bendPoints: [
        { x: startPoint.x, y: middleY },
        { x: endPoint.x, y: middleY },
      ],
      endPoint,
    };
  }

  function integrationLayout(spec) {
    const width = 1500;
    const height = 520;
    const degrees = new Map(spec.nodes.map(node => [node.id, 0]));
    spec.edges.forEach(edge => {
      degrees.set(edge.source, (degrees.get(edge.source) || 0) + 1);
      degrees.set(edge.target, (degrees.get(edge.target) || 0) + 1);
    });
    const hubSource = spec.nodes.find(node => node.kind === "hub" || node.kind === "platform") ||
      spec.nodes.slice().sort((left, right) => (degrees.get(right.id) || 0) - (degrees.get(left.id) || 0))[0];
    const hubId = hubSource.id;
    const left = [];
    const right = [];
    spec.nodes.filter(node => node.id !== hubId).forEach(node => {
      const inbound = spec.edges.some(edge => edge.source === node.id && edge.target === hubId);
      const outbound = spec.edges.some(edge => edge.source === hubId && edge.target === node.id);
      if (inbound && !outbound) left.push(node);
      else if (outbound && !inbound) right.push(node);
      else (left.length <= right.length ? left : right).push(node);
    });
    const leftIds = new Set(left.map(node => node.id));
    const rightIds = new Set(right.map(node => node.id));
    const incidentCount = ids => spec.edges.filter(edge =>
      (edge.source === hubId && ids.has(edge.target))
      || (edge.target === hubId && ids.has(edge.source))
    ).length;
    const hubSize = nodeSize(hubSource, spec);
    hubSize.height = Math.min(
      300,
      Math.max(hubSize.height, 58 + Math.max(incidentCount(leftIds), incidentCount(rightIds)) * 34)
    );
    const hub = {
      id: hubId,
      ...hubSize,
      x: (width - hubSize.width) / 2,
      y: (height - hubSize.height) / 2,
    };
    const placeColumn = (nodes, side) => {
      if (!nodes.length) return [];
      const sizes = nodes.map(node => nodeSize(node, spec));
      const nodeHeight = Math.max(...sizes.map(size => size.height));
      const positions = evenlyStackedY(nodes.length, nodeHeight, height);
      return nodes.map((node, index) => ({
        id: node.id,
        ...sizes[index],
        x: side === "left" ? 24 : width - 24 - sizes[index].width,
        y: positions[index] + (nodeHeight - sizes[index].height) / 2,
      }));
    };
    const leftNodes = placeColumn(left, "left");
    const rightNodes = placeColumn(right, "right");
    const children = leftNodes.concat([hub], rightNodes);
    const byId = new Map(children.map(node => [node.id, node]));
    const incidentBySide = { left: [], right: [] };
    spec.edges.forEach((edge, index) => {
      if (edge.source !== hub.id && edge.target !== hub.id) return;
      const other = edge.source === hub.id ? edge.target : edge.source;
      const otherNode = byId.get(other);
      const side = otherNode && nodeCenter(otherNode).x < nodeCenter(hub).x ? "left" : "right";
      incidentBySide[side].push({ edge, index, otherNode });
    });
    const portY = {};
    Object.entries(incidentBySide).forEach(([side, items]) => {
      items.sort((a, b) => nodeCenter(a.otherNode).y - nodeCenter(b.otherNode).y);
      items.forEach((item, index) => {
        const span = hub.height - 40;
        portY[edgeId(item.edge, item.index)] = hub.y + 20 + (items.length === 1 ? span / 2 : span * index / (items.length - 1));
      });
    });
    const pairCounts = new Map();
    spec.edges.forEach(edge => {
      const key = [edge.source, edge.target].sort().join("::");
      pairCounts.set(key, (pairCounts.get(key) || 0) + 1);
    });
    const pairSeen = new Map();
    const edges = spec.edges.map((edge, index) => {
      const id = edgeId(edge, index);
      const source = byId.get(edge.source);
      const target = byId.get(edge.target);
      let section = orthogonalBetween(source, target);
      let labelPoint;
      if (source && target && (source.id === hub.id || target.id === hub.id)) {
        const other = source.id === hub.id ? target : source;
        const otherOnLeft = nodeCenter(other).x < nodeCenter(hub).x;
        const hubPoint = { x: otherOnLeft ? hub.x : hub.x + hub.width, y: portY[id] };
        const otherPoint = { x: otherOnLeft ? other.x + other.width : other.x, y: nodeCenter(other).y };
        const startPoint = source.id === hub.id ? hubPoint : otherPoint;
        const endPoint = target.id === hub.id ? hubPoint : otherPoint;
        const middleX = (hubPoint.x + otherPoint.x) / 2;
        section = {
          startPoint,
          bendPoints: [
            { x: middleX, y: startPoint.y },
            { x: middleX, y: endPoint.y },
          ],
          endPoint,
        };
        const pairKey = [edge.source, edge.target].sort().join("::");
        const seen = pairSeen.get(pairKey) || 0;
        pairSeen.set(pairKey, seen + 1);
        const lane = seen - ((pairCounts.get(pairKey) || 1) - 1) / 2;
        labelPoint = {
          x: (otherPoint.x + middleX) / 2,
          y: otherPoint.y + lane * 34,
        };
      }
      return { id, sources: [edge.source], targets: [edge.target], sections: [section], labelPoint };
    });
    return { id: "diagram-root", width, height, children, edges, strategy: "center-hub" };
  }

  function longestDirectedPath(spec) {
    const adjacency = new Map(spec.nodes.map(node => [node.id, []]));
    spec.edges.forEach(edge => adjacency.get(edge.source)?.push(edge.target));
    const visit = (nodeId, active = new Set()) => {
      const nextActive = new Set(active);
      nextActive.add(nodeId);
      let best = [nodeId];
      (adjacency.get(nodeId) || []).forEach(targetId => {
        if (nextActive.has(targetId)) return;
        const candidate = [nodeId].concat(visit(targetId, nextActive));
        if (candidate.length > best.length) best = candidate;
      });
      return best;
    };
    return spec.nodes.reduce((best, node) => {
      const candidate = visit(node.id);
      return candidate.length > best.length ? candidate : best;
    }, []);
  }

  function bottomRailBetween(source, target, railY) {
    const startPoint = {
      x: nodeCenter(source).x,
      y: source.y + source.height,
    };
    const endPoint = {
      x: nodeCenter(target).x,
      y: target.y + target.height,
    };
    return {
      startPoint,
      bendPoints: [
        { x: startPoint.x, y: railY },
        { x: endPoint.x, y: railY },
      ],
      endPoint,
    };
  }

  function architectureChainLayout(spec) {
    const mainIds = longestDirectedPath(spec);
    if (mainIds.length < 5 || mainIds.length !== spec.nodes.length) return null;
    const mainNodes = mainIds.map(id => spec.nodes.find(node => node.id === id)).filter(Boolean);
    const columns = Math.min(4, Math.ceil(mainNodes.length / 2));
    const rows = Math.ceil(mainNodes.length / columns);
    const marginX = 70;
    const sampleWidth = Math.max(...mainNodes.map(node => nodeSize(node, spec).width));
    const maxHeight = Math.max(...mainNodes.map(node => nodeSize(node, spec).height));
    const width = Math.max(1450, columns * sampleWidth + marginX * 2 + (columns - 1) * 32);
    const height = Math.max(500, rows * maxHeight + (rows - 1) * 48 + 112);
    const columnStep = columns > 1
      ? (width - marginX * 2 - sampleWidth) / (columns - 1)
      : 0;
    const topY = rows > 1 ? 76 : (height - maxHeight) / 2;
    const bottomY = height - 66 - maxHeight;
    const rowStep = rows > 1 ? (bottomY - topY) / (rows - 1) : 0;
    const children = [];
    const mainMeta = new Map();
    mainNodes.forEach((node, index) => {
      const row = Math.floor(index / columns);
      const offset = index % columns;
      const snakeOffset = row % 2 === 0 ? offset : columns - 1 - offset;
      const size = nodeSize(node, spec);
      children.push({
        id: node.id,
        ...size,
        x: marginX + snakeOffset * columnStep + (sampleWidth - size.width) / 2,
        y: topY + row * rowStep + (maxHeight - size.height) / 2,
      });
      mainMeta.set(node.id, { row, order: index + 1 });
    });
    const byId = new Map(children.map(node => [node.id, node]));
    const consecutive = new Set();
    mainIds.slice(0, -1).forEach((source, index) => {
      consecutive.add(`${source}->${mainIds[index + 1]}`);
    });
    const edges = spec.edges.map((edge, index) => {
      const id = edgeId(edge, index);
      const source = byId.get(edge.source);
      const target = byId.get(edge.target);
      const sourceOrder = mainMeta.get(edge.source)?.order || 0;
      const targetOrder = mainMeta.get(edge.target)?.order || 0;
      const feedback = sourceOrder > targetOrder;
      let section;
      let labelPoint;
      if (consecutive.has(`${edge.source}->${edge.target}`)) {
        section = orthogonalBetween(source, target);
      } else if (feedback) {
        const useLeftRail = nodeCenter(source).x < width / 2;
        const railX = useLeftRail ? 20 : width - 20;
        const railY = 24;
        const sourcePoint = {
          x: useLeftRail ? source.x : source.x + source.width,
          y: nodeCenter(source).y,
        };
        const targetPoint = {
          x: nodeCenter(target).x,
          y: target.y,
        };
        section = {
          startPoint: sourcePoint,
          bendPoints: [
            { x: railX, y: sourcePoint.y },
            { x: railX, y: railY },
            { x: targetPoint.x, y: railY },
          ],
          endPoint: targetPoint,
        };
        labelPoint = {
          x: (railX + targetPoint.x) / 2,
          y: railY,
        };
      } else {
        section = orthogonalBetween(source, target);
      }
      return {
        id,
        sources: [edge.source],
        targets: [edge.target],
        sections: [section],
        governance: feedback,
        labelPoint,
      };
    });
    return {
      id: "diagram-root",
      width,
      height,
      children,
      edges,
      strategy: "layered-architecture",
    };
  }

  function pipelineLayout(spec) {
    const width = 1450;
    const mainIds = longestDirectedPath(spec);
    const mainSet = new Set(mainIds);
    const mainNodes = mainIds.map(id => spec.nodes.find(node => node.id === id)).filter(Boolean);
    const sideNodes = spec.nodes.filter(node => !mainSet.has(node.id));
    const maxColumns = 6;
    const rowCount = Math.max(1, Math.ceil(mainNodes.length / maxColumns));
    const columns = Math.min(maxColumns, Math.max(1, mainNodes.length));
    const maxHeight = Math.max(...spec.nodes.map(node => nodeSize(node, spec).height));
    const height = Math.max(520, (rowCount + (sideNodes.length ? 1 : 0)) * (maxHeight + 48) + 48);
    const marginX = 28;
    const sampleWidth = Math.max(...spec.nodes.map(node => nodeSize(node, spec).width));
    const columnStep = columns > 1 ? (width - marginX * 2 - sampleWidth) / (columns - 1) : 0;
    const sideY = height - maxHeight - 24;
    const mainBottom = (sideNodes.length ? sideY - 48 : height - 32) - maxHeight;
    const mainTop = rowCount === 1 ? (32 + mainBottom) / 2 : 32;
    const rowStep = rowCount > 1 ? (mainBottom - mainTop) / (rowCount - 1) : 0;
    const children = [];
    const mainMeta = new Map();
    mainNodes.forEach((node, index) => {
      const row = Math.floor(index / maxColumns);
      const offset = index % maxColumns;
      const snakeOffset = row % 2 === 0 ? offset : columns - 1 - offset;
      const size = nodeSize(node, spec);
      const x = marginX + snakeOffset * columnStep + (sampleWidth - size.width) / 2;
      const y = mainTop + row * rowStep;
      children.push({ id: node.id, ...size, x, y });
      mainMeta.set(node.id, { row, order: index + 1 });
    });
    sideNodes.forEach((node, index) => {
      const size = nodeSize(node, spec);
      const x = sideNodes.length === 1
        ? (width - size.width) / 2
        : 32 + index * ((width - 64 - size.width) / (sideNodes.length - 1));
      children.push({ id: node.id, ...size, x, y: sideY });
    });
    const byId = new Map(children.map(node => [node.id, node]));
    const consecutive = new Map();
    mainIds.slice(0, -1).forEach((source, index) => consecutive.set(`${source}->${mainIds[index + 1]}`, true));
    const edges = spec.edges.map((edge, index) => {
      const id = edgeId(edge, index);
      const source = byId.get(edge.source);
      const target = byId.get(edge.target);
      const sideEdge = !mainSet.has(edge.source) || !mainSet.has(edge.target);
      const sourceOrder = mainMeta.get(edge.source)?.order || 0;
      const targetOrder = mainMeta.get(edge.target)?.order || 0;
      const feedbackEdge = !sideEdge && sourceOrder > targetOrder;
      let section;
      let labelPoint;
      if (consecutive.has(`${edge.source}->${edge.target}`)) {
        section = orthogonalBetween(source, target);
      } else if (sideEdge) {
        const side = !mainSet.has(edge.source) ? source : target;
        const main = side === source ? target : source;
        const sideCenter = nodeCenter(side);
        const mainCenter = nodeCenter(main);
        const mainRow = mainMeta.get(main.id)?.row || 0;
        const useLeftRail = sideCenter.x < width / 2;
        const railX = useLeftRail ? 8 : width - 8;
        const connectFromBottom = mainRow === 0;
        const gapY = connectFromBottom
          ? Math.min(side.y - 28, main.y + main.height + 36)
          : Math.max(8, main.y - 30);
        const sidePoint = { x: useLeftRail ? side.x : side.x + side.width, y: sideCenter.y };
        const mainPoint = {
          x: mainCenter.x,
          y: connectFromBottom ? main.y + main.height : main.y,
        };
        const forward = side === source;
        const route = [
          sidePoint,
          { x: railX, y: sidePoint.y },
          { x: railX, y: gapY },
          { x: mainPoint.x, y: gapY },
          mainPoint,
        ];
        const points = forward ? route : route.slice().reverse();
        section = {
          startPoint: points[0],
          bendPoints: points.slice(1, -1),
          endPoint: points[points.length - 1],
        };
        labelPoint = { x: mainPoint.x, y: gapY };
      } else if (feedbackEdge) {
        const railY = Math.min(
          height - 24,
          Math.max(source.y + source.height, target.y + target.height) + 150
        );
        section = bottomRailBetween(source, target, railY);
        labelPoint = {
          x: (nodeCenter(source).x + nodeCenter(target).x) / 2,
          y: railY,
        };
      } else {
        section = orthogonalBetween(source, target);
      }
      return {
        id,
        sources: [edge.source],
        targets: [edge.target],
        sections: [section],
        governance: sideEdge || feedbackEdge,
        labelPoint,
      };
    });
    return {
      id: "diagram-root",
      width,
      height,
      children,
      edges,
      strategy: "wrapped-pipeline",
      mainOrder: Object.fromEntries(Array.from(mainMeta.entries()).map(([id, meta]) => [id, meta.order])),
    };
  }

  async function layoutFor(spec) {
    const kind = diagramKind(spec);
    if (kind === "integration") return integrationLayout(spec);
    if (kind === "pipeline") return pipelineLayout(spec);
    const wrappedArchitecture = architectureChainLayout(spec);
    if (wrappedArchitecture) return wrappedArchitecture;
    if (typeof window.ELK !== "function") {
      throw new Error("ELK diagram layout runtime is unavailable.");
    }
    const elk = new window.ELK();
    const layout = await elk.layout(toElkGraph(spec));
    layout.strategy = "layered-architecture";
    return layout;
  }

  function svgPathFor(edge, nodesById) {
    const sections = Array.isArray(edge.sections) ? edge.sections : [];
    if (sections.length) {
      return sections.map(section => {
        const points = [section.startPoint]
          .concat(section.bendPoints || [])
          .concat([section.endPoint])
          .filter(Boolean);
        return points.map((point, index) =>
          `${index ? "L" : "M"}${Number(point.x).toFixed(1)} ${Number(point.y).toFixed(1)}`
        ).join(" ");
      }).join(" ");
    }
    const source = nodesById.get(edge.sources && edge.sources[0]);
    const target = nodesById.get(edge.targets && edge.targets[0]);
    if (!source || !target) return "";
    return `M${source.x + source.width / 2} ${source.y + source.height / 2} L${target.x + target.width / 2} ${target.y + target.height / 2}`;
  }

  function pointsFor(edge, nodesById) {
    const section = Array.isArray(edge.sections) && edge.sections[0];
    if (section) {
      return [section.startPoint].concat(section.bendPoints || []).concat([section.endPoint]).filter(Boolean);
    }
    const source = nodesById.get(edge.sources && edge.sources[0]);
    const target = nodesById.get(edge.targets && edge.targets[0]);
    if (!source || !target) return [];
    return [
      { x: source.x + source.width / 2, y: source.y + source.height / 2 },
      { x: target.x + target.width / 2, y: target.y + target.height / 2 },
    ];
  }

  function midpoint(points) {
    if (!points.length) return { x: 0, y: 0 };
    if (points.length === 1) return points[0];
    let total = 0;
    const lengths = [];
    for (let index = 1; index < points.length; index += 1) {
      const length = Math.hypot(
        points[index].x - points[index - 1].x,
        points[index].y - points[index - 1].y
      );
      lengths.push(length);
      total += length;
    }
    let remaining = total / 2;
    for (let index = 0; index < lengths.length; index += 1) {
      if (remaining <= lengths[index]) {
        const ratio = lengths[index] ? remaining / lengths[index] : 0;
        return {
          x: points[index].x + (points[index + 1].x - points[index].x) * ratio,
          y: points[index].y + (points[index + 1].y - points[index].y) * ratio,
        };
      }
      remaining -= lengths[index];
    }
    return points[points.length - 1];
  }

  function labelCenterForEdge(edge, nodesById) {
    if (edge.labelPoint) return edge.labelPoint;
    const label = Array.isArray(edge.labels) ? edge.labels[0] : null;
    if (label && Number.isFinite(label.x) && Number.isFinite(label.y)) {
      return {
        x: label.x + (Number(label.width) || 0) / 2,
        y: label.y + (Number(label.height) || 0) / 2,
      };
    }
    return midpoint(pointsFor(edge, nodesById));
  }

  function rectanglesOverlap(left, right, padding = 4) {
    return left.x < right.x + right.width + padding
      && left.x + left.width + padding > right.x
      && left.y < right.y + right.height + padding
      && left.y + left.height + padding > right.y;
  }

  function resolveEdgeLabelPoint(point, width, height, nodes, occupied, graphWidth, graphHeight) {
    const stepY = height + 8;
    const offsets = [
      [0, 0], [0, -stepY], [0, stepY], [0, -stepY * 2], [0, stepY * 2],
      [-width * 0.6, 0], [width * 0.6, 0],
    ];
    for (const [offsetX, offsetY] of offsets) {
      const center = {
        x: Math.max(width / 2 + 6, Math.min(graphWidth - width / 2 - 6, point.x + offsetX)),
        y: Math.max(height / 2 + 6, Math.min(graphHeight - height / 2 - 6, point.y + offsetY)),
      };
      const rect = { x: center.x - width / 2, y: center.y - height / 2, width, height };
      if (nodes.some(node => rectanglesOverlap(rect, node, 6))) continue;
      if (occupied.some(other => rectanglesOverlap(rect, other, 6))) continue;
      occupied.push(rect);
      return center;
    }
    const fallback = { x: point.x - width / 2, y: point.y - height / 2, width, height };
    occupied.push(fallback);
    return point;
  }

  function wrapLabel(value, maxPerLine = 12, maxLines = 2) {
    const text = String(value || "").trim();
    if (!text) return ["未命名节点"];
    const capacity = maxPerLine * maxLines;
    const shortened = Array.from(text).length > capacity
      ? `${Array.from(text).slice(0, Math.max(1, capacity - 1)).join("")}…`
      : text;
    if (shortened.includes(" ")) {
      const lines = [];
      let current = "";
      shortened.split(/\s+/).forEach(word => {
        const candidate = current ? `${current} ${word}` : word;
        if (Array.from(candidate).length > maxPerLine && current) {
          lines.push(current);
          current = word;
        } else {
          current = candidate;
        }
      });
      if (current) lines.push(current);
      return lines.slice(0, maxLines);
    }
    const chars = Array.from(shortened);
    const lines = [];
    for (let index = 0; index < chars.length && lines.length < maxLines; index += maxPerLine) {
      lines.push(chars.slice(index, index + maxPerLine).join(""));
    }
    return lines;
  }

  function nodeColors(node, colors) {
    if (node.kind === "hub" || node.kind === "platform") {
      return { fill: colors.primaryFill, stroke: colors.primary, label: colors.inverse, detail: colors.inverse };
    }
    if (node.kind === "data" || node.kind === "database") {
      return { fill: colors.primarySoft, stroke: colors.primary, label: colors.text, detail: colors.muted };
    }
    if (node.kind === "external" || node.kind === "client") {
      return { fill: colors.background, stroke: colors.border, label: colors.text, detail: colors.muted };
    }
    return { fill: colors.surface, stroke: colors.border, label: colors.text, detail: colors.muted };
  }

  function renderNode(layoutNode, sourceNode, colors, profile, order = null) {
    const scheme = nodeColors(sourceNode, colors);
    const compact = profile.strategy === "wrapped-pipeline";
    const lines = wrapLabel(
      sourceNode.label,
      Math.min(profile.labelMaxChars, Math.floor((layoutNode.width - 32) / profile.labelFontSize)),
      2
    );
    const labelY = compact
      ? (sourceNode.detail ? layoutNode.height * 0.4 : layoutNode.height / 2) - (lines.length - 1) * profile.labelLineHeight / 2
      : sourceNode.detail
        ? layoutNode.height / 2 - (lines.length - 1) * profile.labelLineHeight / 2 - 5
        : layoutNode.height / 2 - (lines.length - 1) * profile.labelLineHeight / 2 + 4;
    const labelMarkup = lines.map((line, index) =>
      `<tspan x="${(layoutNode.width / 2).toFixed(1)}" dy="${index ? profile.labelLineHeight : 0}">${escapeXml(line)}</tspan>`
    ).join("");
    const kind = order ? String(order).padStart(2, "0") : "";
    const detail = String(sourceNode.detail || "").trim();
    const detailLines = wrapLabel(detail, Math.floor((layoutNode.width - 32) / profile.detailFontSize), 2);
    const dash = sourceNode.kind === "external" || sourceNode.kind === "client"
      ? ' stroke-dasharray="8 6"'
      : "";
    return [
      `<g data-diagram-node-id="${escapeXml(sourceNode.id)}" transform="translate(${layoutNode.x.toFixed(1)} ${layoutNode.y.toFixed(1)})">`,
      `<rect width="${layoutNode.width}" height="${layoutNode.height}" rx="${profile.cornerRadius}" fill="${scheme.fill}" stroke="${scheme.stroke}" stroke-width="2"${dash}/>` ,
      `<rect x="16" y="${compact ? 9 : 14}" width="5" height="${compact ? 13 : 19}" rx="2.5" fill="${sourceNode.kind === "hub" ? colors.inverse : colors.primary}"/>`,
      `<text x="29" y="${compact ? 18 : 28}" fill="${sourceNode.kind === "hub" ? colors.inverse : colors.primary}" font-family="${SVG_PPTX_FONT}" font-size="${profile.kindFontSize}" font-weight="700" letter-spacing="1.15">${escapeXml(kind)}</text>`,
      `<text data-diagram-text="label" x="${(layoutNode.width / 2).toFixed(1)}" y="${labelY.toFixed(1)}" text-anchor="middle" dominant-baseline="middle" fill="${scheme.label}" font-family="${SVG_PPTX_FONT}" font-size="${profile.labelFontSize}" font-weight="700">${labelMarkup}</text>`,
      detail
        ? `<text x="${(layoutNode.width / 2).toFixed(1)}" y="${(layoutNode.height - 14 - (detailLines.length - 1) * (profile.detailFontSize + 4)).toFixed(1)}" text-anchor="middle" fill="${scheme.detail}" font-family="${SVG_PPTX_FONT}" font-size="${profile.detailFontSize}">${detailLines.map((line, index) => `<tspan x="${(layoutNode.width / 2).toFixed(1)}" dy="${index ? profile.detailFontSize + 4 : 0}">${escapeXml(line)}</tspan>`).join("")}</text>`
        : "",
      "</g>",
    ].join("");
  }

  function renderSvg(root, spec, layout) {
    const colors = paletteFor(root);
    const profile = renderProfileFor(spec);
    const width = 1600;
    const height = 620;
    const padding = profile.padding;
    const graphWidth = Math.max(1, Number(layout.width) || 1);
    const graphHeight = Math.max(1, Number(layout.height) || 1);
    const scale = Math.min(
      (width - padding * 2) / graphWidth,
      (height - padding * 2) / graphHeight,
      profile.maxScale
    );
    const offsetX = (width - graphWidth * scale) / 2;
    const offsetY = (height - graphHeight * scale) / 2;
    const nodesById = new Map((layout.children || []).map(node => [node.id, node]));
    const sourceNodes = new Map(spec.nodes.map(node => [node.id, node]));
    const sourceEdges = new Map(spec.edges.map((edge, index) => [edge.id || `edge-${index + 1}`, edge]));
    const markerId = `diagram-arrow-${++instanceSequence}`;
    const gridId = `diagram-grid-${instanceSequence}`;
    const occupiedLabels = [];
    const nodeRects = Array.from(nodesById.values()).map(node => ({
      x: node.x,
      y: node.y,
      width: node.width,
      height: node.height,
    }));
    const edges = (layout.edges || []).map(layoutEdge => {
      const sourceEdge = sourceEdges.get(layoutEdge.id) || {};
      const path = svgPathFor(layoutEdge, nodesById);
      const label = String(sourceEdge.label || "").trim();
      const labelText = Array.from(label).slice(0, profile.edgeLabelMaxChars).join("");
      const labelWidth = Math.max(48, Math.min(172, 22 + Array.from(labelText).length * profile.edgeLabelCharWidth));
      const rawLabelPoint = labelCenterForEdge(layoutEdge, nodesById);
      const labelPoint = label
        ? resolveEdgeLabelPoint(
          rawLabelPoint,
          labelWidth,
          profile.edgeLabelHeight,
          nodeRects,
          occupiedLabels,
          graphWidth,
          graphHeight
        )
        : rawLabelPoint;
      const governance = Boolean(layoutEdge.governance);
      const bidirectional = Boolean(sourceEdge.bidirectional) || /双向|bidirectional/i.test(label);
      return [
        `<path data-diagram-edge-id="${escapeXml(layoutEdge.id)}" d="${path}" fill="none" stroke="${governance ? colors.muted : colors.primary}" stroke-width="${governance ? "1.8" : "2.4"}"${governance ? ' stroke-dasharray="7 6"' : ""} stroke-linecap="round" stroke-linejoin="round"${bidirectional ? ` marker-start="url(#${markerId})"` : ""} marker-end="url(#${markerId})"/>`,
        label
          ? `<g data-diagram-edge-label-id="${escapeXml(layoutEdge.id)}" transform="translate(${(labelPoint.x - labelWidth / 2).toFixed(1)} ${(labelPoint.y - profile.edgeLabelHeight / 2).toFixed(1)})"><rect width="${labelWidth}" height="${profile.edgeLabelHeight}" rx="${profile.edgeLabelHeight / 2}" fill="${colors.background}" stroke="${colors.border}"/><text x="${(labelWidth / 2).toFixed(1)}" y="${(profile.edgeLabelHeight / 2 + profile.edgeFontSize * 0.38).toFixed(1)}" text-anchor="middle" fill="${colors.muted}" font-family="${SVG_PPTX_FONT}" font-size="${profile.edgeFontSize}" font-weight="600">${escapeXml(labelText)}</text></g>`
          : "",
      ].join("");
    }).join("");
    const nodes = (layout.children || []).map(layoutNode =>
      renderNode(
        layoutNode,
        sourceNodes.get(layoutNode.id) || { id: layoutNode.id },
        colors,
        profile,
        layout.mainOrder && layout.mainOrder[layoutNode.id]
      )
    ).join("");
    const kindLabel = spec.title || "";
    return [
      `<svg xmlns="${SVG_NS}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeXml(spec.title || kindLabel)}" data-diagram-layout-strategy="${profile.strategy}" preserveAspectRatio="xMidYMid meet" font-family="${SVG_PPTX_FONT}" style="font-family:${SVG_FONT_FAMILY}">`,
      "<defs>",
      `<pattern id="${gridId}" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M32 0H0V32" fill="none" stroke="${colors.border}" stroke-width="0.7" opacity="0.24"/></pattern>`,
      `<marker id="${markerId}" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto-start-reverse" markerUnits="strokeWidth"><path d="M1 1L9 5L1 9Z" fill="${colors.primary}"/></marker>`,
      "</defs>",
      `<rect width="${width}" height="${height}" rx="24" fill="${colors.background}"/>`,
      `<rect x="1" y="1" width="${width - 2}" height="${height - 2}" rx="23" fill="url(#${gridId})" stroke="${colors.border}" stroke-width="2"/>`,
      `<g transform="translate(30 26)"><rect width="8" height="24" rx="4" fill="${colors.primary}"/><text x="22" y="18" fill="${colors.primary}" font-family="${SVG_PPTX_FONT}" font-size="13" font-weight="700" letter-spacing="2">${escapeXml(kindLabel)}</text></g>`,
      `<g transform="translate(${offsetX.toFixed(2)} ${offsetY.toFixed(2)}) scale(${scale.toFixed(5)})">`,
      edges,
      nodes,
      "</g>",
      "</svg>",
    ].join("");
  }

  async function renderRoot(root) {
    if (!root || !root.matches || !root.matches(DIAGRAM_SELECTOR)) return null;
    root.setAttribute("data-diagram-render-state", "rendering");
    try {
      const spec = parseSpec(root);
      const layout = await layoutFor(spec);
      root.innerHTML = renderSvg(root, spec, layout);
      root.setAttribute("data-diagram-render-state", "ready");
      root.setAttribute("data-diagram-layout-strategy", layout.strategy || renderProfileFor(spec).strategy);
      root.dispatchEvent(new CustomEvent("box-agent:diagram-rendered", {
        bubbles: true,
        detail: {
          kind: spec.kind,
          nodes: spec.nodes.length,
          edges: spec.edges.length,
          strategy: layout.strategy || renderProfileFor(spec).strategy,
        },
      }));
      return { root, spec, layout };
    } catch (error) {
      root.setAttribute("data-diagram-render-state", "error");
      root.setAttribute("data-diagram-render-error", String(error && error.message || error));
      console.error("Technical diagram render failed:", error);
      throw error;
    }
  }

  function rootsIn(scope) {
    if (!scope) return [];
    const roots = [];
    if (scope.matches && scope.matches(DIAGRAM_SELECTOR)) roots.push(scope);
    if (scope.querySelectorAll) roots.push(...scope.querySelectorAll(DIAGRAM_SELECTOR));
    return Array.from(new Set(roots));
  }

  function renderAll(scope = document) {
    const promise = Promise.all(rootsIn(scope).map(renderRoot));
    window.__deckDiagramReady = promise;
    return promise;
  }

  window.__deckDiagramRuntime = {
    renderAll,
    renderRoot,
    requestLayout(scope) {
      return renderAll(scope || document);
    },
  };

  const start = () => renderAll(document).catch(() => {});
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
