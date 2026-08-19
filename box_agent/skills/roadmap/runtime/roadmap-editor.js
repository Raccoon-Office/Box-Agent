(function () {
  "use strict";

  const RUNTIME_SOURCE = "box-agent-controlled-deck";
  const HOST_SOURCE = "officev3-controlled-deck-host";
  const PROTOCOL_VERSION = 1;
  const LAYOUT_ID = "roadmap-swimlane-v1";
  const SAVE_TIMEOUT_MS = 15000;
  const documentNode = document.querySelector("#deck-document");
  const geometryNode = document.querySelector("#roadmap-geometry");
  const diagnosticsNode = document.querySelector("#roadmap-diagnostics");
  const pendingQuestionsNode = document.querySelector("#roadmap-pending-questions");
  const paletteNode = document.querySelector("#roadmap-palette");
  const stage = document.querySelector('[data-role="stage"]');
  const stageShell = document.querySelector('[data-role="stage-shell"]');
  const editorBackdrop = document.querySelector('[data-role="editor-backdrop"]');
  const editor = document.querySelector('[data-role="editor"]');
  const titleNode = document.querySelector('[data-role="roadmap-title"]');
  const diagnosticBanner = document.querySelector('[data-role="diagnostics"]');
  const toast = document.querySelector('[data-role="toast"]');
  const actions = document.querySelector('[data-role="roadmap-actions"]');
  const adjustButton = document.querySelector('[data-action="adjust"]');
  const saveButton = document.querySelector('[data-action="save"]');
  const contractCore = window.__roadmapContractCore;
  const geometryCore = window.__roadmapGeometryCore;
  if (!documentNode || !geometryNode || !paletteNode || !stage || !stageShell || !editorBackdrop || !editor || !actions || !adjustButton || !saveButton || !contractCore || !geometryCore) return;

  let model = JSON.parse(documentNode.textContent || "{}");
  let geometry = JSON.parse(geometryNode.textContent || "{}");
  let diagnostics = JSON.parse(diagnosticsNode?.textContent || "[]");
  let pendingQuestions = JSON.parse(pendingQuestionsNode?.textContent || "[]");
  const palette = JSON.parse(paletteNode.textContent || "{}");
  const laneAccents = Array.isArray(palette.colors) && palette.colors.length
    ? palette.colors
    : ["var(--roadmap-primary)"];
  let revision = 0;
  let savedRevision = 0;
  let modelValid = true;
  let hostEditAvailable = false;
  let hostSaveAvailable = false;
  let saveInFlight = null;
  let saveTimer = null;
  let toastTimer = null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function safeColor(value) {
    const normalized = String(value || "").trim();
    if (/^#[0-9a-f]{3,8}$/i.test(normalized)) return normalized;
    if (/^(?:rgb|hsl)a?\([0-9.,%\s]+\)$/i.test(normalized)) return normalized;
    return "var(--roadmap-primary)";
  }

  function box(node, entry) {
    node.style.left = `${Number(entry.x) || 0}px`;
    node.style.top = `${Number(entry.y) || 0}px`;
    node.style.width = `${Number(entry.width) || 0}px`;
    node.style.height = `${Number(entry.height) || 0}px`;
  }

  function progressRatio(value) {
    return { planned: 0, doing: 0.55, done: 1, blocked: 1 }[value] || 0;
  }

  function progressLabel(value) {
    return { planned: "计划中", doing: "进行中", done: "已完成", blocked: "受阻" }[value] || value;
  }

  function laneAccent(index) {
    return laneAccents[index % laneAccents.length];
  }

  function compactHeaderLabel(header) {
    if (header.kind !== "half-month" || header.width >= 56) return header.label;
    return header.label.startsWith("上") ? "上" : "下";
  }

  function renderStage() {
    stage.replaceChildren();
    stage.style.width = `${geometry.canvas.width}px`;
    stage.style.height = `${geometry.canvas.height}px`;

    const firstLane = geometry.lanes[0];
    const axisLabel = element("div", "roadmap-axis-label");
    axisLabel.style.left = `${firstLane?.x || 0}px`;
    axisLabel.style.top = `${geometry.canvas.header_top}px`;
    axisLabel.style.width = `${geometry.canvas.plot_left - (firstLane?.x || 0)}px`;
    axisLabel.style.height = `${geometry.canvas.header_height}px`;
    axisLabel.appendChild(element("span", "", "阶段"));
    axisLabel.appendChild(element("small", "", "团队泳道"));
    stage.appendChild(axisLabel);

    geometry.headers.forEach(header => {
      const node = element("div", "roadmap-header", compactHeaderLabel(header));
      node.dataset.kind = header.kind;
      node.title = header.label;
      box(node, header);
      stage.appendChild(node);
    });

    const lanesById = new Map(model.lanes.map(lane => [lane.id, lane]));
    geometry.lanes.forEach((lane, index) => {
      const node = element("div", "roadmap-lane");
      node.dataset.laneId = lane.id;
      node.style.setProperty("--roadmap-lane-accent", laneAccent(index));
      box(node, lane);
      const label = element("div", "roadmap-lane-label");
      const labelText = lanesById.get(lane.id)?.label || lane.label;
      label.title = labelText;
      label.appendChild(element("span", "roadmap-lane-index", String(index + 1).padStart(2, "0")));
      label.appendChild(element("span", "roadmap-lane-title", labelText));
      node.appendChild(label);
      stage.appendChild(node);
    });

    const halfMonthHeaders = geometry.headers.filter(header => header.kind === "half-month");
    const gridLineXs = [...new Set([
      ...halfMonthHeaders.map(header => header.x),
      ...halfMonthHeaders.map(header => header.x + header.width),
    ].map(value => Number(value.toFixed(3))))];
    const laneTop = geometry.canvas.header_top + geometry.canvas.header_height;
    const laneBottom = geometry.lanes.reduce((bottom, lane) => Math.max(bottom, lane.y + lane.height), laneTop);
    gridLineXs.forEach(x => {
      const line = element("div", "roadmap-grid-line");
      line.style.left = `${x}px`;
      line.style.top = `${laneTop}px`;
      line.style.height = `${laneBottom - laneTop}px`;
      stage.appendChild(line);
    });

    const itemsById = new Map(model.items.map(item => [item.id, item]));
    const laneIndexById = new Map(model.lanes.map((lane, index) => [lane.id, index]));
    geometry.bars.forEach(bar => {
      const item = itemsById.get(bar.id) || bar;
      const node = element("div", "roadmap-bar");
      node.dataset.itemId = bar.id;
      node.dataset.lineStyle = bar.line_style;
      node.dataset.progress = item.progress;
      node.title = `${bar.title} · ${bar.start} → ${bar.end} · ${progressLabel(item.progress)}`;
      box(node, bar);
      const color = item.color ? safeColor(item.color) : laneAccent(laneIndexById.get(bar.lane_id) || 0);
      node.style.setProperty("--roadmap-item-accent", color);
      const progress = element("div", "roadmap-bar-progress");
      progress.style.width = `${progressRatio(item.progress) * 100}%`;
      node.appendChild(progress);
      stage.appendChild(node);
    });

    geometry.milestones.forEach(marker => {
      const node = element("div", "roadmap-milestone");
      node.dataset.itemId = marker.id;
      node.dataset.lineStyle = marker.line_style;
      node.title = `${marker.title} · ${marker.date}`;
      node.style.left = `${marker.x - marker.size / 2}px`;
      node.style.top = `${marker.y - marker.size / 2}px`;
      node.style.width = `${marker.size}px`;
      node.style.height = `${marker.size}px`;
      const item = itemsById.get(marker.id) || marker;
      const color = item.color ? safeColor(item.color) : laneAccent(laneIndexById.get(marker.lane_id) || 0);
      node.style.setProperty("--roadmap-item-accent", color);
      stage.appendChild(node);
    });

    geometry.labels.forEach(label => {
      const node = element("div", "roadmap-label", label.text);
      node.dataset.itemId = label.item_id;
      node.dataset.placement = label.placement;
      node.title = label.text;
      box(node, label);
      stage.appendChild(node);
    });

    geometry.continuations.forEach(marker => {
      const before = marker.direction === "before";
      const node = element("div", "roadmap-continuation", before ? "‹" : "›");
      node.setAttribute("aria-label", before ? "开始前仍在继续" : "结束后仍将继续");
      node.style.left = `${marker.x - marker.size}px`;
      node.style.top = `${marker.y - marker.size}px`;
      stage.appendChild(node);
    });

    if (Array.isArray(model.legend) && model.legend.length) {
      const legend = element("div", "roadmap-legend");
      model.legend.forEach(entry => legend.appendChild(element("span", "", entry.label)));
      stage.appendChild(legend);
    }
    titleNode.textContent = model.title;
    resizeStage();
  }

  function resizeStage() {
    const width = Math.max(1, stageShell.clientWidth || geometry.canvas.width);
    const scale = Math.min(1, width / geometry.canvas.width);
    stage.style.transform = `scale(${scale})`;
    stageShell.style.height = `${Math.max(480, geometry.canvas.height * scale)}px`;
  }

  function showToast(message) {
    toast.textContent = String(message);
    toast.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 3200);
  }

  function updateDiagnostics(nextDiagnostics) {
    diagnostics = nextDiagnostics;
    if (diagnosticsNode) diagnosticsNode.textContent = JSON.stringify(diagnostics, null, 2);
    diagnosticBanner.hidden = diagnostics.length === 0;
    diagnosticBanner.textContent = diagnostics.map(entry => entry.message).join("；");
  }

  function safeJsonText(value) {
    return JSON.stringify(value, null, 2)
      .replace(/</g, "\\u003c")
      .replace(/\u2028/g, "\\u2028")
      .replace(/\u2029/g, "\\u2029");
  }

  function capacityDiagnostics() {
    const next = [];
    if (model.items.length >= 80) {
      next.push({ code: "capacity.items-at-limit", severity: "warning", message: "任务数量已达到 80 条上限；建议拆分为多个路线图。" });
    } else if (model.items.length >= 30) {
      next.push({ code: "capacity.dense", severity: "warning", message: `当前包含 ${model.items.length} 条任务，已启用可滚动的密集视图。` });
    }
    if (geometry.canvas.height > 900) {
      next.push({ code: "layout.vertical-scroll", severity: "warning", message: `内容高度为 ${Math.round(geometry.canvas.height)}px，预览将使用纵向滚动。` });
    }
    return next;
  }

  function relayout() {
    const started = performance.now();
    const validation = contractCore.validateAndNormalizeRoadmapSpec(model);
    if (!validation.ok) {
      modelValid = false;
      updateDiagnostics(validation.issues.slice(0, 6).map(message => ({ code: "contract.invalid", severity: "error", message })));
      updateButtons();
      return false;
    }
    modelValid = true;
    model = validation.normalized;
    geometry = geometryCore.layoutRoadmap(model, { width: 1440, height: 900 });
    const elapsed = performance.now() - started;
    const next = capacityDiagnostics();
    if (elapsed >= 100) next.push({ code: "performance.relayout", severity: "warning", message: `本次重排耗时 ${Math.round(elapsed)}ms。` });
    updateDiagnostics(next);
    documentNode.textContent = safeJsonText(model);
    geometryNode.textContent = safeJsonText(geometry);
    renderStage();
    updateButtons();
    return true;
  }

  function inputField(labelText, value, path, type) {
    const label = element("label", "roadmap-field");
    label.appendChild(element("span", "", labelText));
    const input = document.createElement("input");
    input.type = type || "text";
    input.value = value || "";
    input.dataset.modelPath = path;
    label.appendChild(input);
    return label;
  }

  function selectField(value, path, choices) {
    const field = element("div", "roadmap-select");
    field.dataset.modelPath = path;
    const menuId = `roadmap-select-${path.replace(/[^a-z0-9]+/gi, "-")}`;
    const selected = choices.find(choice => choice.value === value) || choices[0];
    const trigger = element("button", "roadmap-select-trigger", selected?.label || "请选择");
    trigger.type = "button";
    trigger.dataset.selectTrigger = "true";
    trigger.setAttribute("role", "combobox");
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", menuId);
    const menu = element("div", "roadmap-select-menu");
    menu.id = menuId;
    menu.setAttribute("role", "listbox");
    menu.setAttribute("popover", "manual");
    choices.forEach(choice => {
      const option = element("button", "roadmap-select-option", choice.label);
      option.type = "button";
      option.dataset.selectValue = choice.value;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(choice.value === value));
      menu.appendChild(option);
    });
    field.append(trigger, menu);
    return field;
  }

  function closeSelectMenu(field, restoreFocus) {
    const menu = field?.querySelector(".roadmap-select-menu");
    const trigger = field?.querySelector("[data-select-trigger]");
    if (!menu || !trigger) return;
    if (typeof menu.hidePopover === "function" && menu.matches(":popover-open")) menu.hidePopover();
    else menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus();
  }

  function closeSelectMenus(except, restoreFocus) {
    editor.querySelectorAll(".roadmap-select").forEach(field => {
      if (field !== except) closeSelectMenu(field, restoreFocus);
    });
  }

  function openSelectMenu(field) {
    closeSelectMenus(field, false);
    const trigger = field.querySelector("[data-select-trigger]");
    const menu = field.querySelector(".roadmap-select-menu");
    const rect = trigger.getBoundingClientRect();
    menu.hidden = false;
    menu.style.width = `${rect.width}px`;
    menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - rect.width - 8))}px`;
    menu.style.top = `${rect.bottom + 4}px`;
    if (typeof menu.showPopover === "function") menu.showPopover();
    const menuRect = menu.getBoundingClientRect();
    if (menuRect.bottom > window.innerHeight - 8 && rect.top > menuRect.height + 12) {
      menu.style.top = `${rect.top - menuRect.height - 4}px`;
    }
    trigger.setAttribute("aria-expanded", "true");
  }

  function chooseSelectOption(field, option) {
    const path = field.dataset.modelPath;
    const value = option.dataset.selectValue;
    const trigger = field.querySelector("[data-select-trigger]");
    const rerenderEditor = /\.kind$/.test(path);
    applyMutation(() => setPath(path, value), rerenderEditor);
    if (!rerenderEditor) {
      trigger.textContent = option.textContent;
      field.querySelectorAll("[data-select-value]").forEach(entry => {
        entry.setAttribute("aria-selected", String(entry === option));
      });
      closeSelectMenu(field, true);
    }
  }

  function svgIcon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const paths = {
      close: ["M6 6l12 12", "M18 6L6 18"],
      plus: ["M12 5v14", "M5 12h14"],
      trash: ["M3 6h18", "M8 6V4h8v2", "M19 6l-1 14H6L5 6", "M10 11v5", "M14 11v5"],
    }[name] || [];
    paths.forEach(value => {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", value);
      svg.appendChild(path);
    });
    return svg;
  }

  function actionButton(label, action, index, options = {}) {
    const button = element("button", "roadmap-button", label);
    button.type = "button";
    button.dataset.editorAction = action;
    if (index !== undefined) button.dataset.index = String(index);
    if (options.variant) button.dataset.variant = options.variant;
    if (options.icon) {
      button.replaceChildren(svgIcon(options.icon));
      if (!options.iconOnly) button.appendChild(element("span", "", label));
    }
    if (options.iconOnly) button.classList.add("roadmap-icon-button");
    button.setAttribute("aria-label", label);
    button.title = label;
    return button;
  }

  function setEditorOpen(open) {
    if (!open) closeSelectMenus(null, false);
    editorBackdrop.hidden = !open;
    editor.hidden = !open;
    if (open) document.body.dataset.editorOpen = "true";
    else delete document.body.dataset.editorOpen;
  }

  function renderEditor() {
    editor.replaceChildren();
    const header = document.createElement("header");
    header.appendChild(element("h2", "", "调整路线图"));
    header.appendChild(actionButton("关闭", "close", undefined, { icon: "close", iconOnly: true }));
    editor.appendChild(header);

    const basic = element("section", "roadmap-editor-section");
    basic.appendChild(element("h3", "", "基本信息"));
    const grid = element("div", "roadmap-editor-grid");
    grid.appendChild(inputField("标题", model.title, "title"));
    grid.appendChild(inputField("开始日期", model.range.start, "range.start", "date"));
    grid.appendChild(inputField("结束日期（不包含）", model.range.end, "range.end", "date"));
    basic.appendChild(grid);
    editor.appendChild(basic);

    const lanes = element("section", "roadmap-editor-section");
    const laneHeader = document.createElement("header");
    laneHeader.appendChild(element("h3", "", `泳道（${model.lanes.length}/8）`));
    laneHeader.appendChild(actionButton("新增泳道", "add-lane", undefined, { icon: "plus", variant: "primary" }));
    lanes.appendChild(laneHeader);
    const laneTable = element("table", "roadmap-table");
    const laneBody = document.createElement("tbody");
    model.lanes.forEach((lane, index) => {
      const row = document.createElement("tr");
      const labelCell = document.createElement("td");
      const input = document.createElement("input");
      input.value = lane.label;
      input.dataset.modelPath = `lanes.${index}.label`;
      labelCell.appendChild(input);
      const actionCell = document.createElement("td");
      actionCell.appendChild(actionButton("删除泳道", "remove-lane", index, { icon: "trash", iconOnly: true, variant: "danger" }));
      row.append(labelCell, actionCell);
      laneBody.appendChild(row);
    });
    laneTable.appendChild(laneBody);
    lanes.appendChild(laneTable);
    editor.appendChild(lanes);

    const items = element("section", "roadmap-editor-section");
    const itemHeader = document.createElement("header");
    itemHeader.appendChild(element("h3", "", `任务与里程碑（${model.items.length}/80）`));
    itemHeader.appendChild(actionButton("新增任务", "add-item", undefined, { icon: "plus", variant: "primary" }));
    items.appendChild(itemHeader);
    const wrap = element("div", "roadmap-table-wrap");
    const table = element("table", "roadmap-table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    ["标题", "泳道", "类型", "开始", "结束", "状态", "确定性", ""].forEach(text => headRow.appendChild(element("th", "", text)));
    head.appendChild(headRow);
    const body = document.createElement("tbody");
    model.items.forEach((item, index) => {
      const row = document.createElement("tr");
      const values = [
        (() => { const input = document.createElement("input"); input.value = item.title; input.dataset.modelPath = `items.${index}.title`; return input; })(),
        selectField(item.lane_id, `items.${index}.lane_id`, model.lanes.map(lane => ({ value: lane.id, label: lane.label }))),
        selectField(item.kind, `items.${index}.kind`, [{ value: "bar", label: "任务" }, { value: "milestone", label: "里程碑" }]),
        (() => { const input = document.createElement("input"); input.type = "date"; input.value = item.start; input.dataset.modelPath = `items.${index}.start`; return input; })(),
        (() => {
          const input = document.createElement("input");
          const isMilestone = item.kind === "milestone";
          input.type = isMilestone ? "text" : "date";
          input.value = isMilestone ? "无需结束日期" : (item.end || "");
          input.disabled = isMilestone;
          input.dataset.modelPath = `items.${index}.end`;
          return input;
        })(),
        selectField(item.progress, `items.${index}.progress`, [
          { value: "planned", label: "计划中" },
          { value: "doing", label: "进行中" },
          { value: "done", label: "已完成" },
          { value: "blocked", label: "受阻" },
        ]),
        selectField(item.certainty, `items.${index}.certainty`, [{ value: "confirmed", label: "已确认" }, { value: "tentative", label: "待确认" }]),
        actionButton("删除任务", "remove-item", index, { icon: "trash", iconOnly: true, variant: "danger" }),
      ];
      values.forEach(value => { const cell = document.createElement("td"); cell.appendChild(value); row.appendChild(cell); });
      body.appendChild(row);
    });
    table.append(head, body);
    wrap.appendChild(table);
    items.appendChild(wrap);
    editor.appendChild(items);
  }

  function setPath(path, value) {
    const segments = path.split(".");
    let target = model;
    for (let index = 0; index < segments.length - 1; index += 1) target = target[segments[index]];
    const key = segments[segments.length - 1];
    if (/\.end$/.test(path) && !value) delete target[key];
    else target[key] = value;
    if (/\.kind$/.test(path) && value === "milestone") delete target.end;
  }

  function uniqueId(prefix, collection) {
    let index = collection.length + 1;
    while (collection.some(entry => entry.id === `${prefix}-${index}`)) index += 1;
    return `${prefix}-${index}`;
  }

  function applyMutation(callback, rerenderEditor) {
    callback();
    revision += 1;
    relayout();
    if (rerenderEditor) renderEditor();
  }

  editor.addEventListener("input", event => {
    const control = event.target.closest("[data-model-path]");
    if (!control || control.classList.contains("roadmap-select")) return;
    applyMutation(() => setPath(control.dataset.modelPath, control.value), false);
  });
  editor.addEventListener("click", event => {
    const option = event.target.closest("[data-select-value]");
    if (option) {
      chooseSelectOption(option.closest(".roadmap-select"), option);
      return;
    }
    const trigger = event.target.closest("[data-select-trigger]");
    if (trigger) {
      const field = trigger.closest(".roadmap-select");
      if (trigger.getAttribute("aria-expanded") === "true") closeSelectMenu(field, false);
      else openSelectMenu(field);
      return;
    }
    const button = event.target.closest("[data-editor-action]");
    if (!button) return;
    const action = button.dataset.editorAction;
    const index = Number(button.dataset.index);
    if (action === "close") setEditorOpen(false);
    if (action === "add-lane") {
      if (model.lanes.length >= 8) return showToast("最多支持 8 条泳道");
      applyMutation(() => model.lanes.push({ id: uniqueId("lane", model.lanes), label: "新泳道", order: model.lanes.length + 1 }), true);
    }
    if (action === "remove-lane") {
      const lane = model.lanes[index];
      if (model.lanes.length <= 1) return showToast("至少保留一条泳道");
      if (model.items.some(item => item.lane_id === lane.id)) return showToast("请先移动或删除该泳道中的任务");
      applyMutation(() => { model.lanes.splice(index, 1); model.lanes.forEach((entry, laneIndex) => { entry.order = laneIndex + 1; }); }, true);
    }
    if (action === "add-item") {
      if (model.items.length >= 80) return showToast("最多支持 80 条任务");
      applyMutation(() => model.items.push({
        id: uniqueId("item", model.items),
        lane_id: model.lanes[0].id,
        title: "新任务",
        start: model.range.start,
        end: model.range.end,
        kind: "bar",
        certainty: "tentative",
        progress: "planned",
      }), true);
    }
    if (action === "remove-item") {
      if (model.items.length <= 1) return showToast("至少保留一条任务或里程碑");
      applyMutation(() => model.items.splice(index, 1), true);
    }
  });
  editor.addEventListener("keydown", event => {
    const trigger = event.target.closest("[data-select-trigger]");
    if (trigger && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      const field = trigger.closest(".roadmap-select");
      openSelectMenu(field);
      const options = [...field.querySelectorAll("[data-select-value]")];
      const selectedIndex = Math.max(0, options.findIndex(option => option.getAttribute("aria-selected") === "true"));
      options[event.key === "ArrowUp" ? Math.max(0, selectedIndex - 1) : selectedIndex].focus();
      return;
    }
    const option = event.target.closest("[data-select-value]");
    if (!option || !["ArrowDown", "ArrowUp", "Home", "End", "Escape"].includes(event.key)) return;
    event.preventDefault();
    const field = option.closest(".roadmap-select");
    if (event.key === "Escape") return closeSelectMenu(field, true);
    const options = [...field.querySelectorAll("[data-select-value]")];
    const currentIndex = options.indexOf(option);
    const nextIndex = event.key === "Home" ? 0
      : event.key === "End" ? options.length - 1
      : event.key === "ArrowDown" ? Math.min(options.length - 1, currentIndex + 1)
      : Math.max(0, currentIndex - 1);
    options[nextIndex].focus();
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".roadmap-select")) closeSelectMenus(null, false);
  });
  editor.addEventListener("scroll", () => closeSelectMenus(null, false), { passive: true });
  editorBackdrop.addEventListener("click", () => setEditorOpen(false));
  window.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.key !== "Escape" || editor.hidden) return;
    const openField = editor.querySelector('[data-select-trigger][aria-expanded="true"]')?.closest(".roadmap-select");
    if (openField) closeSelectMenu(openField, true);
    else setEditorOpen(false);
  });

  function updateButtons() {
    actions.hidden = !hostEditAvailable;
    adjustButton.disabled = !hostEditAvailable;
    adjustButton.title = hostEditAvailable ? "调整泳道、任务、日期与状态" : "当前运行时未声明 Roadmap 编辑能力";
    saveButton.disabled = !hostEditAvailable || !hostSaveAvailable || !modelValid || revision === savedRevision || Boolean(saveInFlight);
    saveButton.textContent = saveInFlight ? "保存中…" : revision === savedRevision ? "已保存" : "保存";
  }

  function serializeHtml() {
    const clone = document.documentElement.cloneNode(true);
    clone.querySelector('[data-role="editor"]').hidden = true;
    clone.querySelector('[data-role="editor-backdrop"]').hidden = true;
    clone.querySelector("body").removeAttribute("data-editor-open");
    clone.querySelector('[data-role="toast"]').hidden = true;
    clone.querySelector('[data-role="roadmap-actions"]').hidden = true;
    clone.querySelector("#deck-document").textContent = safeJsonText(model);
    clone.querySelector("#roadmap-geometry").textContent = safeJsonText(geometry);
    clone.querySelector("#roadmap-diagnostics").textContent = safeJsonText(diagnostics);
    const pendingResult = contractCore.pendingQuestionsForRoadmapSpec(model, pendingQuestions);
    pendingQuestions = pendingResult.pending_questions;
    clone.querySelector("#roadmap-pending-questions").textContent = safeJsonText(
      pendingResult.pending_questions
    );
    const cloneAdjust = clone.querySelector('[data-action="adjust"]');
    cloneAdjust.disabled = true;
    cloneAdjust.title = "当前运行时未声明 Roadmap 编辑能力";
    const cloneSave = clone.querySelector('[data-action="save"]');
    cloneSave.disabled = true;
    cloneSave.textContent = "保存";
    return `<!doctype html>\n${clone.outerHTML}\n`;
  }

  function postToHost(type, payload) {
    if (window.parent === window) return false;
    window.parent.postMessage({ source: RUNTIME_SOURCE, version: PROTOCOL_VERSION, type, ...payload }, "*");
    return true;
  }

  function requestSave() {
    if (saveButton.disabled || saveInFlight || !modelValid) return;
    const requestId = `roadmap-save-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    saveInFlight = { requestId, revision };
    updateButtons();
    postToHost("save-request", { requestId, revision, title: model.title, html: serializeHtml() });
    saveTimer = setTimeout(() => {
      if (!saveInFlight || saveInFlight.requestId !== requestId) return;
      saveInFlight = null;
      updateButtons();
      showToast("保存超时，请重试");
    }, SAVE_TIMEOUT_MS);
  }

  adjustButton.addEventListener("click", () => {
    if (!hostEditAvailable) return;
    renderEditor();
    setEditorOpen(true);
  });
  saveButton.addEventListener("click", requestSave);

  window.addEventListener("message", event => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.source !== HOST_SOURCE || message.version !== PROTOCOL_VERSION) return;
    if (message.type === "host-ready") {
      hostEditAvailable = message.canEdit === true;
      hostSaveAvailable = message.canSave === true;
      updateButtons();
      return;
    }
    if (message.type !== "save-result" || !saveInFlight || message.requestId !== saveInFlight.requestId) return;
    if (saveTimer) clearTimeout(saveTimer);
    const completed = saveInFlight;
    saveInFlight = null;
    saveTimer = null;
    if (message.ok === true) {
      savedRevision = completed.revision;
      showToast(revision === savedRevision ? "已保存到当前 HTML 文件" : "上一版已保存，当前仍有未保存修改");
    } else {
      showToast(message.code === "conflict" ? "文件已在外部改变，请重新打开后再编辑" : String(message.error || "保存失败，请重试"));
    }
    updateButtons();
  });

  if (typeof ResizeObserver !== "undefined") new ResizeObserver(resizeStage).observe(stageShell);
  window.addEventListener("resize", resizeStage);
  renderStage();
  updateDiagnostics(diagnostics);
  updateButtons();
  postToHost("ready", { revision, title: model.title, layoutId: LAYOUT_ID, paletteId: palette.id });
})();
