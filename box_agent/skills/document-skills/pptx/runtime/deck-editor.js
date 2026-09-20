(function controlledDeckRuntime() {
  "use strict";

  const modelNode = document.getElementById("deck-document");
  const root = document.getElementById("deck-root");
  const toolbar = document.querySelector(".deck-toolbar");
  const toast = document.querySelector(".deck-toast");
  const layoutPicker = document.querySelector('[data-role="layout-picker"]');
  const layoutOptions = document.querySelector('[data-role="layout-options"]');
  const layoutControls = document.querySelector('[data-role="layout-controls"]');
  const layoutControlGroups = document.querySelector('[data-role="layout-control-groups"]');
  const presentControls = document.querySelector('[data-role="present-controls"]');
  const presentProgress = document.querySelector('[data-role="present-progress"]');
  const thumbnailRail = document.querySelector('[data-role="thumbnails"]');
  const thumbnailList = document.querySelector('[data-role="thumbnail-list"]');
  const layoutRegistry = window.__deckLayoutRegistry || null;
  if (!modelNode || !root || !toolbar) return;

  const runtimeMode = new URLSearchParams(window.location.search).get("mode");
  const galleryMode = runtimeMode === "gallery";
  const exportMode = navigator.webdriver || runtimeMode === "export" || galleryMode;
  document.documentElement.classList.toggle("deck-export-mode", exportMode);
  document.documentElement.classList.toggle("deck-gallery-mode", galleryMode);

  const RUNTIME_MESSAGE_SOURCE = "box-agent-controlled-deck";
  const HOST_MESSAGE_SOURCE = "officev3-controlled-deck-host";
  const HOST_PROTOCOL_VERSION = 1;
  const SAVE_TIMEOUT_MS = 10000;
  const PPTX_EXPORT_TIMEOUT_MS = 5 * 60 * 1000;
  const THUMBNAIL_MEDIA_QUERY = "(min-width: 1080px) and (min-height: 560px)";

  let documentModel;
  try {
    documentModel = JSON.parse(modelNode.textContent || "{}");
  } catch (error) {
    console.error("Controlled deck state is invalid", error);
    return;
  }

  let currentIndex = 0;
  let editing = false;
  let revision = 0;
  let savedRevision = 0;
  let hostSaveAvailable = false;
  let hostPptxExportAvailable = false;
  let saveInFlight = null;
  let pptxExportInFlight = null;
  let saveError = false;
  let saveTimer = null;
  let pptxExportTimer = null;
  let toastTimer = null;
  let locationPulseTimer = null;
  let observerLockedUntil = 0;
  let pickerMode = "add";
  let presenting = false;
  let presentationOwnsFullscreen = false;
  let presentationScrollY = 0;
  const subscribers = new Set();
  const imageInput = document.createElement("input");
  imageInput.type = "file";
  imageInput.accept = "image/png,image/jpeg,image/webp,image/gif";
  imageInput.hidden = true;
  imageInput.dataset.deckRuntimeInput = "image";
  document.body.appendChild(imageInput);
  let pendingImageTarget = null;
  const toolbarMenus = Array.from(toolbar.querySelectorAll("[data-toolbar-menu]"));
  const toolbarMenuCloseTimers = new WeakMap();

  function cancelToolbarMenuClose(menu) {
    const timer = toolbarMenuCloseTimers.get(menu);
    if (timer) clearTimeout(timer);
    toolbarMenuCloseTimers.delete(menu);
  }

  function setToolbarMenuOpen(menu, open) {
    if (!menu) return;
    cancelToolbarMenuClose(menu);
    menu.classList.toggle("is-open", Boolean(open));
    const trigger = menu.querySelector("[data-toolbar-menu-trigger]");
    if (trigger) trigger.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function scheduleToolbarMenuClose(menu) {
    cancelToolbarMenuClose(menu);
    const timer = setTimeout(() => {
      toolbarMenuCloseTimers.delete(menu);
      if (!menu.matches(":hover") && !menu.contains(document.activeElement)) {
        setToolbarMenuOpen(menu, false);
      }
    }, 320);
    toolbarMenuCloseTimers.set(menu, timer);
  }

  function closeToolbarMenus(except = null) {
    toolbarMenus.forEach(menu => {
      if (menu !== except) setToolbarMenuOpen(menu, false);
    });
  }

  toolbarMenus.forEach(menu => {
    menu.addEventListener("pointerenter", () => {
      cancelToolbarMenuClose(menu);
      closeToolbarMenus(menu);
      setToolbarMenuOpen(menu, true);
    });
    menu.addEventListener("pointerleave", () => {
      if (!menu.contains(document.activeElement)) scheduleToolbarMenuClose(menu);
    });
    menu.addEventListener("focusin", () => {
      cancelToolbarMenuClose(menu);
      closeToolbarMenus(menu);
      setToolbarMenuOpen(menu, true);
    });
    menu.addEventListener("focusout", event => {
      if (!menu.contains(event.relatedTarget)) setToolbarMenuOpen(menu, false);
    });
  });

  function slides() {
    return Array.from(root.querySelectorAll(".slide"));
  }

  function deepClone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function pathParts(pathValue) {
    return String(pathValue).split(".").filter(Boolean).map(part => /^\d+$/.test(part) ? Number(part) : part);
  }

  function setAtPath(target, pathValue, value) {
    const parts = pathParts(pathValue);
    if (!parts.length) return;
    let cursor = target;
    for (let index = 0; index < parts.length - 1; index += 1) {
      const part = parts[index];
      if (cursor[part] == null) {
        cursor[part] = typeof parts[index + 1] === "number" ? [] : {};
      } else if (typeof cursor[part] !== "object") {
        return;
      }
      cursor = cursor[part];
    }
    cursor[parts[parts.length - 1]] = value;
  }

  function getAtPath(target, pathValue) {
    return pathParts(pathValue).reduce((cursor, part) => {
      if (cursor == null || typeof cursor !== "object") return undefined;
      return cursor[part];
    }, target);
  }

  function getLayout(layoutId) {
    return layoutRegistry && typeof layoutRegistry.getLayout === "function"
      ? layoutRegistry.getLayout(layoutId)
      : null;
  }

  function getFieldContract(layout, pathValue) {
    if (!layout || !layout.fields) return null;
    const parts = pathParts(pathValue);
    let contract = { type: "object", shape: layout.fields };
    for (const part of parts) {
      if (typeof part === "number") {
        if (!contract || contract.type !== "array") return null;
        contract = contract.itemShape && contract.itemShape.type
          ? contract.itemShape
          : { type: "object", shape: contract.itemShape || {} };
      } else {
        if (!contract || contract.type !== "object" || !contract.shape) return null;
        contract = contract.shape[part];
      }
      if (!contract) return null;
    }
    return contract;
  }

  function renderSlideElement(modelSlide, index) {
    const layout = getLayout(modelSlide.layout_id);
    if (!layout || typeof layout.render !== "function") return null;
    const template = document.createElement("template");
    const palette = documentModel.design_contract?.palette;
    const renderContext = palette?.version === 2 ? { palette: palette.tokens, useDeckPalette: true } : null;
    template.innerHTML = layout.render(modelSlide, index, documentModel.design, renderContext).trim();
    const element = template.content.firstElementChild;
    return element && element.matches(".slide") ? element : null;
  }

  function createSlideId() {
    const ids = new Set(documentModel.slides.map(slide => slide.id));
    let id;
    do {
      id = `slide-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
    } while (ids.has(id));
    return id;
  }

  function selectedSlide() {
    return slides()[Math.max(0, Math.min(currentIndex, slides().length - 1))] || null;
  }

  function modelSlideForElement(element) {
    const slide = element.closest(".slide");
    if (!slide) return null;
    const id = slide.getAttribute("data-slide-id");
    return documentModel.slides.find(item => item.id === id) || null;
  }

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("is-visible");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 1800);
  }

  function emitChange(reason) {
    // Human edits supersede the AI proposal. They remain fully editable and
    // save normally; only subsequent automatic plan reuse is invalidated.
    if (documentModel.design_plan) documentModel.design_plan.user_edited = true;
    revision += 1;
    saveError = false;
    modelNode.textContent = safeJson(documentModel);
    window.__deckTextFit?.refresh(selectedSlide() || root);
    refreshCurrentThumbnail();
    const detail = {
      reason,
      revision,
      document: deepClone(documentModel),
    };
    window.dispatchEvent(new CustomEvent("box-agent:deck-change", { detail }));
    subscribers.forEach(callback => {
      try {
        callback(detail);
      } catch (error) {
        console.error(error);
      }
    });
    updateSaveButton();
  }

  function safeJson(value) {
    return JSON.stringify(value, null, 2)
      .replace(/</g, "\\u003c")
      .replace(/\u2028/g, "\\u2028")
      .replace(/\u2029/g, "\\u2029");
  }

  function pageTitleAt(index) {
    const modelSlide = documentModel.slides[index];
    if (!modelSlide) return "未命名页面";
    const props = modelSlide.props || {};
    return String(
      props.title || props.statement || props.number || modelSlide.layout_id || "未命名页面"
    ).trim();
  }

  function thumbnailSlideClone(sourceSlide) {
    const clone = sourceSlide.cloneNode(true);
    clone.classList.remove("is-current-slide");
    clone.removeAttribute("id");
    clone.querySelectorAll("[id]").forEach(element => element.removeAttribute("id"));
    clone.querySelectorAll("[contenteditable]").forEach(element => {
      element.removeAttribute("contenteditable");
      element.removeAttribute("spellcheck");
    });
    clone.querySelectorAll("[data-pptx-chart]").forEach(element => {
      element.removeAttribute("data-pptx-chart");
      element.removeAttribute("data-chart-renderer");
      element.classList.remove("chart-runtime-ready", "chart-runtime-missing");
    });
    clone.querySelectorAll("[data-chart-canvas]").forEach(element => element.replaceChildren());
    clone.querySelectorAll("button, input, select, textarea, a").forEach(element => {
      element.setAttribute("tabindex", "-1");
    });
    return clone;
  }

  function createThumbnailButton(sourceSlide, index) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "deck-thumbnail";
    button.dataset.thumbnailIndex = String(index);
    button.setAttribute("aria-label", `第 ${index + 1} 页：${pageTitleAt(index)}`);
    button.title = pageTitleAt(index);

    const number = document.createElement("span");
    number.className = "deck-thumbnail-number";
    number.textContent = String(index + 1).padStart(2, "0");

    const canvas = document.createElement("span");
    canvas.className = "deck-thumbnail-canvas";
    canvas.appendChild(thumbnailSlideClone(sourceSlide));
    button.append(number, canvas);
    return button;
  }

  function updateThumbnailSelection() {
    if (!thumbnailList) return;
    thumbnailList.querySelectorAll(".deck-thumbnail").forEach((button, index) => {
      const selected = index === currentIndex;
      button.classList.toggle("is-current", selected);
      button.setAttribute("aria-current", selected ? "page" : "false");
    });
  }

  function renderThumbnails() {
    if (!thumbnailList || exportMode) return;
    const fragment = document.createDocumentFragment();
    slides().forEach((slide, index) => {
      fragment.appendChild(createThumbnailButton(slide, index));
    });
    thumbnailList.replaceChildren(fragment);
    updateThumbnailSelection();
  }

  function refreshCurrentThumbnail() {
    if (!thumbnailList || exportMode) return;
    const sourceSlide = slides()[currentIndex];
    const current = thumbnailList.querySelector(
      `.deck-thumbnail[data-thumbnail-index="${currentIndex}"]`
    );
    if (!sourceSlide || !current) return;
    current.replaceWith(createThumbnailButton(sourceSlide, currentIndex));
    updateThumbnailSelection();
  }

  function pulseLocation() {
    const location = toolbar.querySelector(".toolbar-location");
    if (!location) return;
    location.classList.remove("is-updated");
    void location.offsetWidth;
    location.classList.add("is-updated");
    if (locationPulseTimer) clearTimeout(locationPulseTimer);
    locationPulseTimer = setTimeout(() => location.classList.remove("is-updated"), 560);
  }

  function updateToolbar() {
    const allSlides = slides();
    currentIndex = Math.max(0, Math.min(currentIndex, Math.max(0, allSlides.length - 1)));
    const pageNumber = allSlides.length ? currentIndex + 1 : 0;
    const currentPage = toolbar.querySelector('[data-role="current-page"]');
    const totalPages = toolbar.querySelector('[data-role="total-pages"]');
    const currentTitle = toolbar.querySelector('[data-role="current-title"]');
    if (currentPage) currentPage.textContent = String(pageNumber).padStart(2, "0");
    if (totalPages) totalPages.textContent = String(allSlides.length).padStart(2, "0");
    if (currentTitle) currentTitle.textContent = pageTitleAt(currentIndex);
    toolbar.setAttribute(
      "aria-label",
      `Deck editor，当前第 ${pageNumber} 页，共 ${allSlides.length} 页：${pageTitleAt(currentIndex)}`
    );
    allSlides.forEach((slide, index) => {
      slide.classList.toggle("is-current-slide", index === currentIndex);
    });
    updateThumbnailSelection();
    const editButton = toolbar.querySelector('[data-action="edit"]');
    if (editButton) editButton.setAttribute("aria-pressed", editing ? "true" : "false");
    const presentButton = toolbar.querySelector('[data-action="present"]');
    if (presentButton) presentButton.setAttribute("aria-pressed", presenting ? "true" : "false");
    const currentModel = documentModel.slides[currentIndex];
    const currentLayout = currentModel ? getLayout(currentModel.layout_id) : null;
    const hasLayoutControls = Boolean(
      currentLayout && currentLayout.editor && currentLayout.editor.controls
    );
    const boundaries = {
      previous: currentIndex <= 0,
      next: currentIndex >= allSlides.length - 1,
      "move-up": currentIndex <= 0,
      "move-down": currentIndex >= allSlides.length - 1,
      delete: allSlides.length <= 1,
      "add-slide": allSlides.length >= 40 || !layoutRegistry,
      layout: !layoutRegistry,
      adjust: !layoutRegistry || !hasLayoutControls,
    };
    Object.entries(boundaries).forEach(([action, disabled]) => {
      toolbar.querySelectorAll(`[data-action="${action}"]`).forEach(button => {
        button.disabled = disabled;
      });
    });
    updateSaveButton();
    updatePptxExportButton();
    if (layoutControls && !layoutControls.hidden) renderLayoutControls();
    if (presenting) updatePresentationControls();
  }

  function getSaveState() {
    if (saveInFlight) return "saving";
    if (saveError) return "error";
    if (!hostSaveAvailable) return "download";
    return revision === savedRevision ? "clean" : "dirty";
  }

  function updateSaveButton() {
    const button = toolbar.querySelector('[data-action="save"], [data-action="download"]');
    if (!button) return;
    const state = getSaveState();
    const labels = {
      clean: "已保存",
      dirty: "保存 HTML",
      saving: "保存中…",
      error: "重试保存",
      download: "另存 HTML",
    };
    const compactLabels = {
      clean: "已存",
      dirty: "HTML",
      saving: "…",
      error: "重试",
      download: "HTML",
    };
    button.textContent = labels[state];
    button.dataset.compactLabel = compactLabels[state];
    button.dataset.saveState = state;
    button.disabled = state === "clean" || state === "saving";
    button.setAttribute("aria-busy", state === "saving" ? "true" : "false");
    button.title = state === "download" ? "下载编辑后的 HTML 副本" : "保存到当前 HTML 文件";
  }

  function updatePptxExportButton() {
    const button = toolbar.querySelector('[data-action="export-pptx"]');
    if (!button) return;
    const state = pptxExportInFlight
      ? "exporting"
      : hostPptxExportAvailable
        ? "ready"
        : "unavailable";
    button.textContent = state === "exporting" ? "导出中…" : "导出 PPT";
    button.dataset.compactLabel = state === "exporting" ? "…" : "PPT";
    button.dataset.exportState = state;
    button.disabled = state !== "ready";
    button.setAttribute("aria-busy", state === "exporting" ? "true" : "false");
    button.title = state === "unavailable"
      ? "请在 officev3 中打开后导出可编辑 PPT"
      : "导出为可编辑 PowerPoint 文件";
  }

  function renumberSlides() {
    slides().forEach((slide, index) => {
      const number = String(index + 1).padStart(2, "0");
      slide.setAttribute("data-slide", number);
      const page = slide.querySelector(".deck-page");
      if (page) page.textContent = number;
    });
    renderThumbnails();
    updateToolbar();
  }

  function scrollToCurrent(behavior = "smooth") {
    if (presenting) {
      updateToolbar();
      return;
    }
    const slide = selectedSlide();
    observerLockedUntil = Date.now() + (behavior === "smooth" ? 720 : 220);
    if (slide) slide.scrollIntoView({ behavior, block: "start", inline: "center" });
    updateToolbar();
  }

  function refreshEditingAttributes() {
    root.querySelectorAll('[data-prop-kind="text"]').forEach(element => {
      if (editing) {
        element.setAttribute("contenteditable", "true");
        element.setAttribute("spellcheck", "true");
      } else {
        element.removeAttribute("contenteditable");
        element.removeAttribute("spellcheck");
      }
    });
  }

  function setEditing(next) {
    editing = Boolean(next);
    document.body.classList.toggle("deck-editing", editing);
    refreshEditingAttributes();
    updateToolbar();
    showToast(editing ? "编辑已开启：文字可直接修改，双击图片可替换" : "编辑已关闭");
  }

  function updateEditorScale() {
    if (presenting || exportMode) return;
    const thumbnailsVisible = Boolean(
      thumbnailRail && window.matchMedia(THUMBNAIL_MEDIA_QUERY).matches
    );
    document.body.classList.toggle("deck-thumbnails-visible", thumbnailsVisible);
    const thumbnailSpace = thumbnailsVisible
      ? Math.ceil(thumbnailRail.getBoundingClientRect().width) + 20
      : 0;
    const toolbarHeight = toolbar ? toolbar.getBoundingClientRect().height : 64;
    const availableWidth = Math.max(320, window.innerWidth - 32 - thumbnailSpace);
    const availableHeight = Math.max(240, window.innerHeight - 48 - toolbarHeight - 48);
    const scale = Math.max(
      0.25,
      Math.min(1, availableWidth / 1920, availableHeight / 1080)
    );
    const flowGap = 48 - (1080 * (1 - scale));
    document.documentElement.style.setProperty("--deck-editor-scale", String(scale));
    document.documentElement.style.setProperty("--deck-editor-slide-gap", `${flowGap}px`);
    document.documentElement.style.setProperty(
      "--deck-editor-chrome-shift",
      `${Math.round(thumbnailSpace / 2)}px`
    );
  }

  function updatePresentationScale() {
    if (!presenting) return;
    const scale = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    document.documentElement.style.setProperty("--deck-present-scale", String(scale));
  }

  function updatePresentationControls() {
    if (!presentControls) return;
    const allSlides = slides();
    const page = presentControls.querySelector('[data-role="present-page"]');
    const total = presentControls.querySelector('[data-role="present-total"]');
    const title = presentControls.querySelector('[data-role="present-title"]');
    if (page) page.textContent = String(currentIndex + 1).padStart(2, "0");
    if (total) total.textContent = String(allSlides.length).padStart(2, "0");
    if (title) title.textContent = pageTitleAt(currentIndex);
    const previous = presentControls.querySelector('[data-present-action="previous"]');
    const next = presentControls.querySelector('[data-present-action="next"]');
    if (previous) previous.disabled = currentIndex <= 0;
    if (next) next.disabled = currentIndex >= allSlides.length - 1;
    if (presentProgress) {
      presentProgress.style.width = allSlides.length
        ? `${((currentIndex + 1) / allSlides.length) * 100}%`
        : "0%";
    }
  }

  function movePresentation(offset) {
    if (!presenting) return;
    const nextIndex = Math.max(0, Math.min(slides().length - 1, currentIndex + offset));
    if (nextIndex === currentIndex) return;
    currentIndex = nextIndex;
    updateToolbar();
    window.dispatchEvent(new CustomEvent("box-agent:deck-present-slide", {
      detail: { presenting: true, index: currentIndex },
    }));
  }

  function enterPresentation() {
    flushActiveTextEdit();
    closeLayoutPicker();
    closeLayoutControls();
    presentationScrollY = window.scrollY;
    editing = false;
    presenting = true;
    presentationOwnsFullscreen = false;
    document.body.classList.remove("deck-editing");
    document.body.classList.add("deck-presenting");
    refreshEditingAttributes();
    updatePresentationScale();
    updateToolbar();
    window.dispatchEvent(new CustomEvent("box-agent:deck-present", {
      detail: { presenting: true, index: currentIndex },
    }));
    if (document.documentElement.requestFullscreen && !document.fullscreenElement) {
      try {
        const request = document.documentElement.requestFullscreen();
        if (request && typeof request.then === "function") {
          request.then(() => {
            presentationOwnsFullscreen = presenting && Boolean(document.fullscreenElement);
          }).catch(() => {
            presentationOwnsFullscreen = false;
          });
        }
      } catch (error) {
        presentationOwnsFullscreen = false;
      }
    }
  }

  function exitPresentation(exitFullscreen = true) {
    if (!presenting) return;
    presenting = false;
    document.body.classList.remove("deck-presenting");
    document.documentElement.style.removeProperty("--deck-present-scale");
    const shouldExitFullscreen = exitFullscreen && presentationOwnsFullscreen &&
      document.fullscreenElement && document.exitFullscreen;
    presentationOwnsFullscreen = false;
    if (shouldExitFullscreen) {
      try {
        const request = document.exitFullscreen();
        if (request && typeof request.catch === "function") request.catch(() => {});
      } catch (error) {
        // The document may already be leaving fullscreen; presentation mode is
        // still safely restored below.
      }
    }
    updateEditorScale();
    updateToolbar();
    window.scrollTo({ top: presentationScrollY, behavior: "auto" });
    scrollToCurrent("auto");
    window.dispatchEvent(new CustomEvent("box-agent:deck-present", {
      detail: { presenting: false, index: currentIndex },
    }));
  }

  function applyTextEdit(element) {
    const modelSlide = modelSlideForElement(element);
    const propPath = element.getAttribute("data-prop-path");
    if (!modelSlide || !propPath) return;
    const nextValue = element.innerText.replace(/\r/g, "").trim();
    if (getAtPath(modelSlide.props, propPath) === nextValue) return;
    setAtPath(modelSlide.props, propPath, nextValue);
    if (element.getAttribute("data-prop-rerender") === "true"
      || element.closest(".expressive-slide[data-presentation-density]")) {
      rerenderCurrentSlide("text");
    } else {
      emitChange("text");
    }
  }

  function flushActiveTextEdit() {
    const activeElement = document.activeElement;
    if (!editing || !activeElement || !activeElement.matches('[data-prop-kind="text"][data-prop-path]')) {
      return;
    }
    applyTextEdit(activeElement);
  }

  function closeLayoutPicker() {
    if (!layoutPicker) return;
    layoutPicker.hidden = true;
    toolbar.querySelectorAll('[data-action="add-slide"], [data-action="layout"]').forEach(button => {
      button.setAttribute("aria-expanded", "false");
    });
  }

  function closeLayoutControls() {
    if (!layoutControls) return;
    layoutControls.hidden = true;
    const button = toolbar.querySelector('[data-action="adjust"]');
    if (button) button.setAttribute("aria-expanded", "false");
  }

  function createLayoutOption(layout) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "deck-layout-option";
    button.dataset.layoutId = layout.id;
    const isCurrent = pickerMode === "replace" &&
      documentModel.slides[currentIndex] &&
      documentModel.slides[currentIndex].layout_id === layout.id;
    button.classList.toggle("is-current", isCurrent);
    button.setAttribute(
      "aria-label",
      `${isCurrent ? "当前版式，" : ""}${layout.editor.label}：${layout.editor.description}`
    );

    const preview = document.createElement("span");
    preview.className = "layout-option-preview";
    preview.setAttribute("aria-hidden", "true");
    const previewSlide = renderSlideElement({
      id: `preview-${layout.id}`,
      layout_id: layout.id,
      props: deepClone(layout.editor.defaultProps),
    }, 0);
    if (previewSlide) preview.appendChild(previewSlide);

    const copy = document.createElement("span");
    copy.className = "layout-option-copy";
    const nameRow = document.createElement("span");
    nameRow.className = "layout-option-name-row";
    const name = document.createElement("span");
    name.className = "layout-option-name";
    name.textContent = layout.editor.label;
    nameRow.appendChild(name);
    if (isCurrent) {
      const current = document.createElement("span");
      current.className = "layout-option-current";
      current.textContent = "当前";
      nameRow.appendChild(current);
    }
    const description = document.createElement("span");
    description.className = "layout-option-description";
    description.textContent = layout.editor.description;
    copy.append(nameRow, description);
    button.append(preview, copy);
    return button;
  }

  function renderLayoutOptions() {
    if (!layoutOptions || !layoutRegistry || !Array.isArray(layoutRegistry.layouts)) return;
    layoutOptions.replaceChildren();
    layoutRegistry.layouts
      .filter(layout => layout.editor && layout.editor.defaultProps)
      .forEach(layout => layoutOptions.appendChild(createLayoutOption(layout)));
  }

  function itemSummary(item, index) {
    if (typeof item === "string") return item.trim() || `条目 ${index + 1}`;
    if (Array.isArray(item)) {
      const value = item.find(candidate => typeof candidate === "string" && candidate.trim());
      return value ? Array.from(value.trim()).slice(0, 36).join("") : `条目 ${index + 1}`;
    }
    if (!item || typeof item !== "object") return `条目 ${index + 1}`;
    const value = [item.title, item.value, item.label, item.phase, item.kicker, item.body, item.detail]
      .find(candidate => typeof candidate === "string" && candidate.trim());
    const text = value ? value.trim() : `条目 ${index + 1}`;
    return Array.from(text).slice(0, 36).join("");
  }

  function createEnumControl(layout, pathValue, config, modelSlide) {
    const contract = getFieldContract(layout, pathValue);
    if (!contract || contract.type !== "enum" || !config || !config.options) return null;
    const group = document.createElement("section");
    group.className = "layout-control-group layout-enum-control";

    const label = document.createElement("h3");
    label.textContent = config.label || pathValue;
    const segments = document.createElement("div");
    segments.className = "layout-control-segments";
    segments.setAttribute("role", "group");
    segments.setAttribute("aria-label", label.textContent);
    const currentValue = getAtPath(modelSlide.props, pathValue);

    contract.values.forEach(value => {
      if (!Object.prototype.hasOwnProperty.call(config.options, value)) return;
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.controlAction = "set-enum";
      button.dataset.controlPath = pathValue;
      button.dataset.controlValue = value;
      button.textContent = config.options[value];
      const isCurrent = currentValue === value;
      button.classList.toggle("is-current", isCurrent);
      button.setAttribute("aria-pressed", isCurrent ? "true" : "false");
      segments.appendChild(button);
    });
    if (!segments.childElementCount) return null;
    group.append(label, segments);
    return group;
  }

  function createCollectionControl(layout, pathValue, config, modelSlide) {
    const contract = getFieldContract(layout, pathValue);
    const items = getAtPath(modelSlide.props, pathValue);
    if (!contract || contract.type !== "array" || !Array.isArray(items) || !config) return null;
    const labelText = config.label || pathValue;
    const group = document.createElement("section");
    group.className = "layout-control-group layout-collection-control";

    const heading = document.createElement("div");
    heading.className = "layout-control-heading";
    const label = document.createElement("h3");
    label.textContent = labelText;
    const count = document.createElement("span");
    count.className = "layout-control-count";
    count.textContent = `${items.length} / ${contract.maxItems}`;
    heading.append(label, count);

    const list = document.createElement("div");
    list.className = "layout-collection-list";
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "layout-collection-empty";
      empty.textContent = `暂无${labelText}`;
      list.appendChild(empty);
    } else {
      items.forEach((item, index) => {
        const row = document.createElement("div");
        row.className = "layout-collection-row";
        const ordinal = document.createElement("span");
        ordinal.className = "layout-collection-index";
        ordinal.textContent = String(index + 1).padStart(2, "0");
        const summary = document.createElement("span");
        summary.className = "layout-collection-summary";
        summary.textContent = itemSummary(item, index);
        const actions = document.createElement("span");
        actions.className = "layout-collection-actions";

        [
          { label: "↑", direction: -1, title: `前移第 ${index + 1} 个${labelText}` },
          { label: "↓", direction: 1, title: `后移第 ${index + 1} 个${labelText}` },
        ].forEach(action => {
          const button = document.createElement("button");
          button.type = "button";
          button.dataset.controlAction = "move-item";
          button.dataset.controlPath = pathValue;
          button.dataset.controlIndex = String(index);
          button.dataset.controlDirection = String(action.direction);
          button.textContent = action.label;
          button.title = action.title;
          button.setAttribute("aria-label", action.title);
          button.disabled = action.direction < 0 ? index === 0 : index === items.length - 1;
          actions.appendChild(button);
        });

        const remove = document.createElement("button");
        remove.type = "button";
        remove.dataset.controlAction = "delete-item";
        remove.dataset.controlPath = pathValue;
        remove.dataset.controlIndex = String(index);
        remove.textContent = "删除";
        remove.title = `删除第 ${index + 1} 个${labelText}`;
        remove.disabled = items.length <= contract.minItems;
        actions.appendChild(remove);
        row.append(ordinal, summary, actions);
        list.appendChild(row);
      });
    }

    const add = document.createElement("button");
    add.type = "button";
    add.className = "layout-collection-add";
    add.dataset.controlAction = "add-item";
    add.dataset.controlPath = pathValue;
    add.textContent = `＋ 添加${labelText}`;
    add.disabled = items.length >= contract.maxItems;
    add.title = items.length >= contract.maxItems
      ? `${labelText}最多 ${contract.maxItems} 个`
      : `添加一个${labelText}`;

    group.append(heading, list, add);
    return group;
  }

  function createChartDataInput(pathValue, value, label, numeric = false) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = String(value == null ? "" : value);
    input.dataset.controlAction = "set-data-value";
    input.dataset.controlPath = pathValue;
    input.setAttribute("aria-label", label);
    input.autocomplete = "off";
    input.spellcheck = false;
    if (numeric) input.inputMode = "decimal";
    return input;
  }

  function createItemDataControl(config, modelSlide) {
    if (!config || !Array.isArray(config.columns)) return null;
    const items = getAtPath(modelSlide.props, config.path);
    if (!Array.isArray(items)) return null;
    const group = document.createElement("section");
    group.className = "layout-control-group layout-chart-data-control";
    const heading = document.createElement("div");
    heading.className = "layout-control-heading";
    const title = document.createElement("h3");
    title.textContent = config.label || "图表数据";
    const count = document.createElement("span");
    count.className = "layout-control-count";
    count.textContent = `${items.length} 个数据项`;
    heading.append(title, count);

    const scroller = document.createElement("div");
    scroller.className = "chart-data-editor-scroll";
    const table = document.createElement("table");
    table.className = "chart-data-editor-table chart-item-editor-table";
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    config.columns.forEach(column => {
      const cell = document.createElement("th");
      cell.textContent = column.label || column.key;
      headerRow.appendChild(cell);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement("tbody");
    items.forEach((item, itemIndex) => {
      const row = document.createElement("tr");
      config.columns.forEach(column => {
        const cell = document.createElement("td");
        cell.appendChild(createChartDataInput(
          `${config.path}.${itemIndex}.${column.key}`,
          item && item[column.key],
          `${column.label || column.key} ${itemIndex + 1}`,
          column.numeric === true
        ));
        row.appendChild(cell);
      });
      tbody.appendChild(row);
    });
    table.append(thead, tbody);
    scroller.appendChild(table);
    group.append(heading, scroller);
    return group;
  }

  function createDiagramSelect(pathValue, value, label, options) {
    const select = document.createElement("select");
    select.dataset.controlAction = "set-data-value";
    select.dataset.controlPath = pathValue;
    select.setAttribute("aria-label", label);
    options.forEach(option => {
      const element = document.createElement("option");
      element.value = option.value;
      element.textContent = option.label;
      element.selected = option.value === value;
      select.appendChild(element);
    });
    return select;
  }

  function createDiagramDataControl(config, modelSlide) {
    if (!config) return null;
    const nodes = getAtPath(modelSlide.props, config.nodesPath);
    const edges = getAtPath(modelSlide.props, config.edgesPath);
    if (!Array.isArray(nodes) || !Array.isArray(edges)) return null;
    const group = document.createElement("section");
    group.className = "layout-control-group layout-diagram-data-control";

    const heading = document.createElement("div");
    heading.className = "layout-control-heading";
    const title = document.createElement("h3");
    title.textContent = config.label || "DiagramSpec";
    const count = document.createElement("span");
    count.className = "layout-control-count";
    count.textContent = `${nodes.length} 节点 · ${edges.length} 条边`;
    heading.append(title, count);

    const nodeTitle = document.createElement("h4");
    nodeTitle.className = "diagram-data-subheading";
    nodeTitle.textContent = "节点";
    const nodeScroller = document.createElement("div");
    nodeScroller.className = "chart-data-editor-scroll";
    const nodeTable = document.createElement("table");
    nodeTable.className = "chart-data-editor-table diagram-data-editor-table";
    const nodeHead = document.createElement("thead");
    const nodeHeadRow = document.createElement("tr");
    ["ID", "名称", "说明", "类型", ""].forEach(label => {
      const cell = document.createElement("th");
      cell.textContent = label;
      nodeHeadRow.appendChild(cell);
    });
    nodeHead.appendChild(nodeHeadRow);
    const nodeBody = document.createElement("tbody");
    const kindOptions = [
      { value: "service", label: "服务" },
      { value: "hub", label: "核心平台" },
      { value: "client", label: "入口 / 外部" },
      { value: "data", label: "数据" },
      { value: "gateway", label: "网关" },
      { value: "queue", label: "消息 / 队列" },
      { value: "external", label: "外部系统" },
    ];
    nodes.forEach((node, nodeIndex) => {
      const row = document.createElement("tr");
      const idCell = document.createElement("th");
      idCell.scope = "row";
      idCell.textContent = node.id;
      row.appendChild(idCell);
      [
        { key: "label", label: `节点 ${nodeIndex + 1} 名称` },
        { key: "detail", label: `节点 ${nodeIndex + 1} 说明` },
      ].forEach(column => {
        const cell = document.createElement("td");
        cell.appendChild(createChartDataInput(
          `${config.nodesPath}.${nodeIndex}.${column.key}`,
          node[column.key],
          column.label
        ));
        row.appendChild(cell);
      });
      const kindCell = document.createElement("td");
      kindCell.appendChild(createDiagramSelect(
        `${config.nodesPath}.${nodeIndex}.kind`,
        node.kind || "service",
        `节点 ${nodeIndex + 1} 类型`,
        kindOptions
      ));
      row.appendChild(kindCell);
      const actionCell = document.createElement("td");
      actionCell.className = "chart-data-row-action";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.controlAction = "delete-diagram-node";
      remove.dataset.controlIndex = String(nodeIndex);
      remove.textContent = "×";
      remove.title = `删除节点 ${node.label || node.id}`;
      remove.setAttribute("aria-label", remove.title);
      remove.disabled = nodes.length <= Number(config.minNodes || 2);
      actionCell.appendChild(remove);
      row.appendChild(actionCell);
      nodeBody.appendChild(row);
    });
    nodeTable.append(nodeHead, nodeBody);
    nodeScroller.appendChild(nodeTable);

    const edgeTitle = document.createElement("h4");
    edgeTitle.className = "diagram-data-subheading";
    edgeTitle.textContent = "边";
    const edgeScroller = document.createElement("div");
    edgeScroller.className = "chart-data-editor-scroll";
    const edgeTable = document.createElement("table");
    edgeTable.className = "chart-data-editor-table diagram-data-editor-table";
    const edgeHead = document.createElement("thead");
    const edgeHeadRow = document.createElement("tr");
    ["来源", "目标", "标签", ""].forEach(label => {
      const cell = document.createElement("th");
      cell.textContent = label;
      edgeHeadRow.appendChild(cell);
    });
    edgeHead.appendChild(edgeHeadRow);
    const edgeBody = document.createElement("tbody");
    const endpointOptions = nodes.map(node => ({ value: node.id, label: node.label || node.id }));
    edges.forEach((edge, edgeIndex) => {
      const row = document.createElement("tr");
      ["source", "target"].forEach(endpoint => {
        const cell = document.createElement("td");
        cell.appendChild(createDiagramSelect(
          `${config.edgesPath}.${edgeIndex}.${endpoint}`,
          edge[endpoint],
          `边 ${edgeIndex + 1} ${endpoint === "source" ? "来源" : "目标"}`,
          endpointOptions
        ));
        row.appendChild(cell);
      });
      const labelCell = document.createElement("td");
      labelCell.appendChild(createChartDataInput(
        `${config.edgesPath}.${edgeIndex}.label`,
        edge.label || "",
        `边 ${edgeIndex + 1} 标签`
      ));
      row.appendChild(labelCell);
      const actionCell = document.createElement("td");
      actionCell.className = "chart-data-row-action";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.controlAction = "delete-diagram-edge";
      remove.dataset.controlIndex = String(edgeIndex);
      remove.textContent = "×";
      remove.title = `删除边 ${edgeIndex + 1}`;
      remove.setAttribute("aria-label", remove.title);
      remove.disabled = edges.length <= Number(config.minEdges || 0);
      actionCell.appendChild(remove);
      row.appendChild(actionCell);
      edgeBody.appendChild(row);
    });
    edgeTable.append(edgeHead, edgeBody);
    edgeScroller.appendChild(edgeTable);

    const actions = document.createElement("div");
    actions.className = "chart-data-editor-actions diagram-data-editor-actions";
    [
      { action: "add-diagram-node", label: "＋ 节点", disabled: nodes.length >= Number(config.maxNodes || 16) },
      { action: "add-diagram-edge", label: "＋ 边", disabled: nodes.length < 2 || edges.length >= Number(config.maxEdges || 24) },
      { action: "relayout-diagram", label: "重新布局", disabled: false },
    ].forEach(item => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.controlAction = item.action;
      button.textContent = item.label;
      button.disabled = item.disabled;
      actions.appendChild(button);
    });

    group.append(
      heading,
      nodeTitle,
      nodeScroller,
      edgeTitle,
      edgeScroller,
      actions
    );
    return group;
  }

  function createChartDataControl(layout, config, modelSlide) {
    if (!config) return null;
    const categories = getAtPath(modelSlide.props, config.categoriesPath);
    const series = getAtPath(modelSlide.props, config.seriesPath);
    if (!Array.isArray(categories) || !Array.isArray(series)) return null;

    const group = document.createElement("section");
    group.className = "layout-control-group layout-chart-data-control";
    const heading = document.createElement("div");
    heading.className = "layout-control-heading";
    const title = document.createElement("h3");
    title.textContent = config.label || "图表数据";
    const count = document.createElement("span");
    count.className = "layout-control-count";
    count.textContent = `${categories.length} 个分类 · ${series.length} 个系列`;
    heading.append(title, count);

    const scroller = document.createElement("div");
    scroller.className = "chart-data-editor-scroll";
    const table = document.createElement("table");
    table.className = "chart-data-editor-table";
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    const categoryHeading = document.createElement("th");
    categoryHeading.textContent = "分类";
    headerRow.appendChild(categoryHeading);
    series.forEach((item, seriesIndex) => {
      const cell = document.createElement("th");
      const wrap = document.createElement("div");
      wrap.className = "chart-data-series-heading";
      wrap.appendChild(createChartDataInput(
        `${config.seriesPath}.${seriesIndex}.name`,
        item && item.name,
        `系列 ${seriesIndex + 1} 名称`
      ));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.controlAction = "delete-chart-series";
      remove.dataset.controlIndex = String(seriesIndex);
      remove.textContent = "×";
      remove.title = `删除系列 ${seriesIndex + 1}`;
      remove.setAttribute("aria-label", remove.title);
      remove.disabled = series.length <= Number(config.minSeries || 1);
      wrap.appendChild(remove);
      cell.appendChild(wrap);
      headerRow.appendChild(cell);
    });
    const rowActionHeading = document.createElement("th");
    rowActionHeading.className = "chart-data-row-action";
    headerRow.appendChild(rowActionHeading);
    thead.appendChild(headerRow);

    const tbody = document.createElement("tbody");
    categories.forEach((category, categoryIndex) => {
      const row = document.createElement("tr");
      const categoryCell = document.createElement("th");
      categoryCell.scope = "row";
      categoryCell.appendChild(createChartDataInput(
        `${config.categoriesPath}.${categoryIndex}`,
        category,
        `分类 ${categoryIndex + 1}`
      ));
      row.appendChild(categoryCell);
      series.forEach((item, seriesIndex) => {
        const cell = document.createElement("td");
        const values = item && Array.isArray(item.values) ? item.values : [];
        cell.appendChild(createChartDataInput(
          `${config.seriesPath}.${seriesIndex}.values.${categoryIndex}`,
          values[categoryIndex] == null ? "0" : values[categoryIndex],
          `${item && item.name || `系列 ${seriesIndex + 1}`} · ${category}`,
          true
        ));
        row.appendChild(cell);
      });
      const actionCell = document.createElement("td");
      actionCell.className = "chart-data-row-action";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.controlAction = "delete-chart-category";
      remove.dataset.controlIndex = String(categoryIndex);
      remove.textContent = "×";
      remove.title = `删除分类 ${categoryIndex + 1}`;
      remove.setAttribute("aria-label", remove.title);
      remove.disabled = categories.length <= Number(config.minCategories || 2);
      actionCell.appendChild(remove);
      row.appendChild(actionCell);
      tbody.appendChild(row);
    });
    table.append(thead, tbody);
    scroller.appendChild(table);

    const actions = document.createElement("div");
    actions.className = "chart-data-editor-actions";
    const addCategory = document.createElement("button");
    addCategory.type = "button";
    addCategory.dataset.controlAction = "add-chart-category";
    addCategory.textContent = "＋ 分类";
    addCategory.disabled = categories.length >= Number(config.maxCategories || 12);
    const addSeries = document.createElement("button");
    addSeries.type = "button";
    addSeries.dataset.controlAction = "add-chart-series";
    addSeries.textContent = "＋ 系列";
    addSeries.disabled = series.length >= Number(config.maxSeries || 4);
    actions.append(addCategory, addSeries);

    group.append(heading, scroller, actions);
    return group;
  }

  function renderLayoutControls() {
    if (!layoutControls || !layoutControlGroups) return;
    const modelSlide = documentModel.slides[currentIndex];
    const layout = modelSlide ? getLayout(modelSlide.layout_id) : null;
    const controls = layout && layout.editor ? layout.editor.controls : null;
    if (!modelSlide || !layout || !controls) {
      closeLayoutControls();
      return;
    }

    const kicker = layoutControls.querySelector('[data-role="layout-controls-kicker"]');
    const title = layoutControls.querySelector('[data-role="layout-controls-title"]');
    const description = layoutControls.querySelector('[data-role="layout-controls-description"]');
    if (kicker) kicker.textContent = `第 ${String(currentIndex + 1).padStart(2, "0")} 页 · 当前版式`;
    if (title) title.textContent = layout.editor.label;
    if (description) description.textContent = layout.editor.description;
    layoutControlGroups.replaceChildren();

    Object.entries(controls.enums || {}).forEach(([pathValue, config]) => {
      const control = createEnumControl(layout, pathValue, config, modelSlide);
      if (control) layoutControlGroups.appendChild(control);
    });
    Object.entries(controls.collections || {}).forEach(([pathValue, config]) => {
      const control = createCollectionControl(layout, pathValue, config, modelSlide);
      if (control) layoutControlGroups.appendChild(control);
    });
    const itemDataControl = createItemDataControl(controls.itemData, modelSlide);
    if (itemDataControl) layoutControlGroups.appendChild(itemDataControl);
    const diagramDataControl = createDiagramDataControl(controls.diagramData, modelSlide);
    if (diagramDataControl) layoutControlGroups.appendChild(diagramDataControl);
    const chartDataControl = createChartDataControl(layout, controls.chartData, modelSlide);
    if (chartDataControl) layoutControlGroups.appendChild(chartDataControl);
  }

  function openLayoutControls() {
    flushActiveTextEdit();
    if (!layoutControls || !layoutControlGroups || !layoutRegistry) {
      showToast("当前 HTML 未包含版式调整能力");
      return;
    }
    closeLayoutPicker();
    renderLayoutControls();
    if (!layoutControlGroups.childElementCount) {
      showToast("当前版式没有可调整参数");
      return;
    }
    layoutControls.hidden = false;
    const button = toolbar.querySelector('[data-action="adjust"]');
    if (button) button.setAttribute("aria-expanded", "true");
    requestAnimationFrame(() => {
      const target = layoutControlGroups.querySelector("button.is-current") ||
        layoutControlGroups.querySelector("button:not(:disabled)");
      if (target) target.focus({ preventScroll: true });
    });
  }

  function requestDiagramLayout(scope, message = "") {
    const runtime = window.__deckDiagramRuntime;
    if (!runtime || typeof runtime.requestLayout !== "function" ||
        !scope || !scope.querySelector("[data-pptx-diagram]")) {
      return null;
    }
    const pending = runtime.requestLayout(scope);
    if (pending && typeof pending.then === "function") {
      pending.then(() => {
        if (message) showToast(message);
      }).catch(() => showToast("技术图布局失败，请检查节点和边"));
    }
    return pending;
  }

  function rerenderCurrentSlide(reason, message) {
    const sourceElement = selectedSlide();
    const modelSlide = documentModel.slides[currentIndex];
    if (!sourceElement || !modelSlide) return false;
    const nextElement = renderSlideElement(modelSlide, currentIndex);
    if (!nextElement) {
      showToast("版式调整渲染失败，当前页未改变");
      return false;
    }
    sourceElement.replaceWith(nextElement);
    observer.unobserve(sourceElement);
    observer.observe(nextElement);
    requestDiagramLayout(nextElement);
    refreshEditingAttributes();
    renumberSlides();
    emitChange(reason);
    scrollToCurrent("auto");
    if (message) showToast(message);
    return true;
  }

  function setLayoutOption(pathValue, value) {
    flushActiveTextEdit();
    const modelSlide = documentModel.slides[currentIndex];
    const layout = modelSlide ? getLayout(modelSlide.layout_id) : null;
    const config = layout && layout.editor && layout.editor.controls &&
      layout.editor.controls.enums && layout.editor.controls.enums[pathValue];
    const contract = getFieldContract(layout, pathValue);
    if (!modelSlide || !config || !contract || contract.type !== "enum" ||
        !contract.values.includes(value) ||
        !Object.prototype.hasOwnProperty.call(config.options, value)) return false;
    if (getAtPath(modelSlide.props, pathValue) === value) return true;
    const previousProps = deepClone(modelSlide.props);
    setAtPath(modelSlide.props, pathValue, value);
    const changed = rerenderCurrentSlide(
      "change-layout-option",
      `${config.label}：${config.options[value]}`
    );
    if (!changed) modelSlide.props = previousProps;
    return changed;
  }

  function setChartDataValue(pathValue, value) {
    const modelSlide = documentModel.slides[currentIndex];
    const layout = modelSlide ? getLayout(modelSlide.layout_id) : null;
    const controls = layout && layout.editor && layout.editor.controls;
    const contract = getFieldContract(layout, pathValue);
    if (!modelSlide || !controls ||
        (!controls.chartData && !controls.itemData && !controls.diagramData) ||
        !contract || contract.type !== "text") {
      return false;
    }
    let nextValue = String(value == null ? "" : value).trim();
    if (!nextValue) nextValue = contract.role === "metric" ? "0" : "未命名";
    if (Number.isInteger(contract.maxChars)) {
      nextValue = Array.from(nextValue).slice(0, contract.maxChars).join("");
    }
    if (getAtPath(modelSlide.props, pathValue) === nextValue) return true;
    const previousProps = deepClone(modelSlide.props);
    setAtPath(modelSlide.props, pathValue, nextValue);
    const changed = rerenderCurrentSlide("chart-data", "图表数据已更新");
    if (!changed) modelSlide.props = previousProps;
    return changed;
  }

  function mutateChartData(action, index = -1) {
    const modelSlide = documentModel.slides[currentIndex];
    const layout = modelSlide ? getLayout(modelSlide.layout_id) : null;
    const config = layout && layout.editor && layout.editor.controls &&
      layout.editor.controls.chartData;
    if (!modelSlide || !config) return false;
    const categories = getAtPath(modelSlide.props, config.categoriesPath);
    const series = getAtPath(modelSlide.props, config.seriesPath);
    if (!Array.isArray(categories) || !Array.isArray(series)) return false;
    const previousProps = deepClone(modelSlide.props);

    if (action === "add-category") {
      if (categories.length >= Number(config.maxCategories || 12)) return false;
      categories.push(`分类 ${categories.length + 1}`);
      series.forEach(item => {
        if (!Array.isArray(item.values)) item.values = [];
        item.values.push("0");
      });
    } else if (action === "delete-category") {
      if (categories.length <= Number(config.minCategories || 2) ||
          index < 0 || index >= categories.length) return false;
      categories.splice(index, 1);
      series.forEach(item => {
        if (Array.isArray(item.values)) item.values.splice(index, 1);
      });
    } else if (action === "add-series") {
      if (series.length >= Number(config.maxSeries || 4)) return false;
      series.push({
        name: `系列 ${series.length + 1}`,
        values: categories.map(() => "0"),
      });
    } else if (action === "delete-series") {
      if (series.length <= Number(config.minSeries || 1) || index < 0 || index >= series.length) {
        return false;
      }
      series.splice(index, 1);
    } else {
      return false;
    }

    const messages = {
      "add-category": "已添加分类",
      "delete-category": "已删除分类",
      "add-series": "已添加系列",
      "delete-series": "已删除系列",
    };
    const changed = rerenderCurrentSlide("chart-data-structure", messages[action]);
    if (!changed) modelSlide.props = previousProps;
    return changed;
  }

  function nextDiagramId(items, prefix) {
    const ids = new Set(items.map(item => String(item && item.id || "")));
    let sequence = items.length + 1;
    while (ids.has(`${prefix}-${sequence}`)) sequence += 1;
    return `${prefix}-${sequence}`;
  }

  function mutateDiagramData(action, index = -1) {
    flushActiveTextEdit();
    const modelSlide = documentModel.slides[currentIndex];
    const layout = modelSlide ? getLayout(modelSlide.layout_id) : null;
    const config = layout && layout.editor && layout.editor.controls &&
      layout.editor.controls.diagramData;
    if (!modelSlide || !config) return false;
    const nodes = getAtPath(modelSlide.props, config.nodesPath);
    const edges = getAtPath(modelSlide.props, config.edgesPath);
    if (!Array.isArray(nodes) || !Array.isArray(edges)) return false;
    const previousProps = deepClone(modelSlide.props);

    if (action === "add-node") {
      if (nodes.length >= Number(config.maxNodes || 16)) return false;
      const id = nextDiagramId(nodes, "node");
      nodes.push({ id, label: `新节点 ${nodes.length + 1}`, detail: "", kind: "service" });
    } else if (action === "delete-node") {
      if (nodes.length <= Number(config.minNodes || 2) || index < 0 || index >= nodes.length) {
        return false;
      }
      const nodeId = nodes[index].id;
      nodes.splice(index, 1);
      for (let edgeIndex = edges.length - 1; edgeIndex >= 0; edgeIndex -= 1) {
        if (edges[edgeIndex].source === nodeId || edges[edgeIndex].target === nodeId) {
          edges.splice(edgeIndex, 1);
        }
      }
    } else if (action === "add-edge") {
      if (nodes.length < 2 || edges.length >= Number(config.maxEdges || 24)) return false;
      const existing = new Set(edges.map(edge => `${edge.source}->${edge.target}`));
      let source = nodes[0].id;
      let target = nodes[1].id;
      let found = false;
      for (let sourceIndex = 0; sourceIndex < nodes.length && !found; sourceIndex += 1) {
        for (let targetIndex = 0; targetIndex < nodes.length; targetIndex += 1) {
          if (sourceIndex === targetIndex) continue;
          const candidate = `${nodes[sourceIndex].id}->${nodes[targetIndex].id}`;
          if (!existing.has(candidate)) {
            source = nodes[sourceIndex].id;
            target = nodes[targetIndex].id;
            found = true;
            break;
          }
        }
      }
      if (!found) return false;
      edges.push({
        id: nextDiagramId(edges, "edge"),
        source,
        target,
        label: "新连接",
      });
    } else if (action === "delete-edge") {
      if (edges.length <= Number(config.minEdges || 0) || index < 0 || index >= edges.length) {
        return false;
      }
      edges.splice(index, 1);
    } else {
      return false;
    }

    const messages = {
      "add-node": `已添加节点 · ${nodes.length}/${config.maxNodes || 16}`,
      "delete-node": `已删除节点及关联边 · ${nodes.length}/${config.maxNodes || 16}`,
      "add-edge": `已添加边 · ${edges.length}/${config.maxEdges || 24}`,
      "delete-edge": `已删除边 · ${edges.length}/${config.maxEdges || 24}`,
    };
    const changed = rerenderCurrentSlide(`diagram-${action}`, messages[action]);
    if (!changed) modelSlide.props = previousProps;
    return changed;
  }

  function mutateLayoutCollection(pathValue, action, index = -1, direction = 0) {
    flushActiveTextEdit();
    const modelSlide = documentModel.slides[currentIndex];
    const layout = modelSlide ? getLayout(modelSlide.layout_id) : null;
    const config = layout && layout.editor && layout.editor.controls &&
      layout.editor.controls.collections && layout.editor.controls.collections[pathValue];
    const contract = getFieldContract(layout, pathValue);
    const items = modelSlide ? getAtPath(modelSlide.props, pathValue) : null;
    if (!modelSlide || !config || !contract || contract.type !== "array" || !Array.isArray(items)) {
      return false;
    }

    const previousProps = deepClone(modelSlide.props);
    if (action === "add") {
      if (items.length >= contract.maxItems) return false;
      items.push(deepClone(config.itemDefault));
    } else if (action === "delete") {
      if (items.length <= contract.minItems || index < 0 || index >= items.length) return false;
      items.splice(index, 1);
    } else if (action === "move") {
      const targetIndex = index + direction;
      if (index < 0 || index >= items.length || targetIndex < 0 || targetIndex >= items.length) return false;
      const [item] = items.splice(index, 1);
      items.splice(targetIndex, 0, item);
    } else {
      return false;
    }

    const reasons = {
      add: "add-layout-item",
      delete: "delete-layout-item",
      move: "move-layout-item",
    };
    const messages = {
      add: `已添加${config.label} · ${items.length}/${contract.maxItems}`,
      delete: `已删除${config.label} · ${items.length}/${contract.maxItems}`,
      move: `已调整${config.label}顺序`,
    };
    const changed = rerenderCurrentSlide(reasons[action], messages[action]);
    if (!changed) modelSlide.props = previousProps;
    return changed;
  }

  function addLayoutItem(pathValue) {
    return mutateLayoutCollection(pathValue, "add");
  }

  function deleteLayoutItem(pathValue, index) {
    return mutateLayoutCollection(pathValue, "delete", Number(index));
  }

  function moveLayoutItem(pathValue, index, direction) {
    return mutateLayoutCollection(pathValue, "move", Number(index), Number(direction));
  }

  function openLayoutPicker(mode) {
    flushActiveTextEdit();
    if (!layoutPicker || !layoutOptions || !layoutRegistry) {
      showToast("当前 HTML 未包含可编辑版式注册表");
      return;
    }
    closeLayoutControls();
    if (mode === "add" && documentModel.slides.length >= 40) {
      showToast("一套演示最多 40 页");
      return;
    }
    pickerMode = mode === "replace" ? "replace" : "add";
    const kicker = layoutPicker.querySelector('[data-role="layout-picker-kicker"]');
    const title = layoutPicker.querySelector('[data-role="layout-picker-title"]');
    const description = layoutPicker.querySelector('[data-role="layout-picker-description"]');
    if (kicker) kicker.textContent = pickerMode === "add" ? "新增页面" : "当前页面";
    if (title) title.textContent = pickerMode === "add" ? "选择受控版式" : "更换当前页版式";
    if (description) {
      description.textContent = pickerMode === "add"
        ? "新页面会插入到当前页之后。"
        : "内容会尽量映射；原版式内容会保留，切换回来即可恢复。";
    }
    renderLayoutOptions();
    layoutPicker.hidden = false;
    toolbar.querySelectorAll('[data-action="add-slide"], [data-action="layout"]').forEach(button => {
      const matches = (pickerMode === "add" && button.dataset.action === "add-slide") ||
        (pickerMode === "replace" && button.dataset.action === "layout");
      button.setAttribute("aria-expanded", matches ? "true" : "false");
    });
    requestAnimationFrame(() => {
      const target = layoutOptions.querySelector(".is-current") ||
        layoutOptions.querySelector(".deck-layout-option");
      if (target) target.focus({ preventScroll: true });
    });
  }

  function addSlideWithLayout(layoutId) {
    if (!layoutRegistry || typeof layoutRegistry.createEditorProps !== "function") return;
    if (documentModel.slides.length >= 40) {
      showToast("一套演示最多 40 页");
      closeLayoutPicker();
      return;
    }
    const layout = getLayout(layoutId);
    const props = layoutRegistry.createEditorProps(layoutId);
    if (!layout || !props) return;
    const composition = documentModel.slides[currentIndex]?.props.composition;
    if (["open", "poster", "editorial"].includes(composition)) {
      props.composition = window.__deckPresentation?.preferredComposition(layout) || props.composition;
    }
    const modelSlide = { id: createSlideId(), layout_id: layoutId, props };
    const element = renderSlideElement(modelSlide, currentIndex + 1);
    if (!element) {
      showToast("版式渲染失败，请换一个版式");
      return;
    }
    const allSlides = slides();
    const insertIndex = Math.min(currentIndex + 1, documentModel.slides.length);
    if (allSlides[currentIndex]) allSlides[currentIndex].after(element);
    else root.appendChild(element);
    documentModel.slides.splice(insertIndex, 0, modelSlide);
    currentIndex = insertIndex;
    observer.observe(element);
    requestDiagramLayout(element);
    refreshEditingAttributes();
    renumberSlides();
    emitChange("add-slide");
    closeLayoutPicker();
    scrollToCurrent();
    showToast(`已新增第 ${currentIndex + 1} 页 · ${layout.editor.label}`);
  }

  function changeCurrentLayout(layoutId) {
    flushActiveTextEdit();
    const sourceElement = selectedSlide();
    const sourceModel = documentModel.slides[currentIndex];
    const targetLayout = getLayout(layoutId);
    if (!sourceElement || !sourceModel || !targetLayout ||
        !layoutRegistry || typeof layoutRegistry.createEditorProps !== "function") return;
    if (sourceModel.layout_id === layoutId) {
      closeLayoutPicker();
      showToast("当前页已经使用这个版式");
      return;
    }

    const sourceSnapshot = deepClone(sourceModel);
    const drafts = sourceModel.layout_drafts && typeof sourceModel.layout_drafts === "object"
      ? deepClone(sourceModel.layout_drafts)
      : {};
    drafts[sourceModel.layout_id] = deepClone(sourceModel.props);
    const restoredProps = drafts[layoutId] ? deepClone(drafts[layoutId]) : null;
    delete drafts[layoutId];
    const nextProps = restoredProps || layoutRegistry.createEditorProps(layoutId, sourceSnapshot);
    if (!nextProps) return;
    const nextModel = {
      ...sourceSnapshot,
      layout_id: layoutId,
      props: nextProps,
    };
    if (Object.keys(drafts).length) nextModel.layout_drafts = drafts;
    else delete nextModel.layout_drafts;
    const nextElement = renderSlideElement(nextModel, currentIndex);
    if (!nextElement) {
      showToast("版式渲染失败，当前页未改变");
      return;
    }

    sourceElement.replaceWith(nextElement);
    documentModel.slides[currentIndex] = nextModel;
    observer.unobserve(sourceElement);
    observer.observe(nextElement);
    requestDiagramLayout(nextElement);
    refreshEditingAttributes();
    renumberSlides();
    emitChange("change-layout");
    closeLayoutPicker();
    scrollToCurrent("auto");
    showToast(
      restoredProps
        ? `已恢复 ${targetLayout.editor.label} 中保留的内容`
        : `已切换为 ${targetLayout.editor.label}；原版式内容已保留`
    );
  }

  function duplicateCurrent() {
    const allSlides = slides();
    const source = allSlides[currentIndex];
    const sourceModel = documentModel.slides[currentIndex];
    if (!source || !sourceModel) return;
    const clone = source.cloneNode(true);
    const cloneModel = deepClone(sourceModel);
    cloneModel.id = `${sourceModel.id}-copy-${Date.now().toString(36)}`;
    clone.setAttribute("data-slide-id", cloneModel.id);
    source.after(clone);
    documentModel.slides.splice(currentIndex + 1, 0, cloneModel);
    currentIndex += 1;
    observer.observe(clone);
    refreshEditingAttributes();
    renumberSlides();
    emitChange("duplicate-slide");
    scrollToCurrent();
  }

  function deleteCurrent() {
    const allSlides = slides();
    if (allSlides.length <= 1) return;
    observer.unobserve(allSlides[currentIndex]);
    allSlides[currentIndex].remove();
    documentModel.slides.splice(currentIndex, 1);
    currentIndex = Math.min(currentIndex, documentModel.slides.length - 1);
    renumberSlides();
    emitChange("delete-slide");
    scrollToCurrent();
  }

  function moveCurrent(offset) {
    const allSlides = slides();
    const fromIndex = currentIndex;
    const targetIndex = currentIndex + offset;
    if (targetIndex < 0 || targetIndex >= allSlides.length) return;
    const currentSlide = allSlides[currentIndex];
    const targetSlide = allSlides[targetIndex];
    if (offset < 0) targetSlide.before(currentSlide);
    else targetSlide.after(currentSlide);
    const [modelSlide] = documentModel.slides.splice(currentIndex, 1);
    documentModel.slides.splice(targetIndex, 0, modelSlide);
    currentIndex = targetIndex;
    renumberSlides();
    emitChange("move-slide");
    scrollToCurrent("auto");
    pulseLocation();
    showToast(
      `${offset < 0 ? "已前移" : "已后移"}：第 ${fromIndex + 1} 页 → 第 ${targetIndex + 1} 页 · ${pageTitleAt(currentIndex)}`
    );
  }

  function serializeHtml() {
    window.__deckTextFit?.refresh(root);
    modelNode.textContent = safeJson(documentModel);
    const clone = document.documentElement.cloneNode(true);
    clone.querySelector("body").classList.remove(
      "deck-editing",
      "deck-presenting",
      "deck-thumbnails-visible"
    );
    clone.querySelectorAll("[data-toolbar-menu]").forEach(menu => menu.classList.remove("is-open"));
    clone.querySelectorAll("[data-toolbar-menu-trigger]").forEach(trigger => {
      trigger.setAttribute("aria-expanded", "false");
    });
    clone.style.removeProperty("--deck-present-scale");
    clone.style.removeProperty("--deck-editor-scale");
    clone.style.removeProperty("--deck-editor-slide-gap");
    clone.style.removeProperty("--deck-editor-chrome-shift");
    clone.querySelectorAll(".is-current-slide").forEach(slide => slide.classList.remove("is-current-slide"));
    clone.querySelectorAll("[data-deck-runtime-input]").forEach(element => element.remove());
    clone.querySelectorAll("[contenteditable]").forEach(element => {
      element.removeAttribute("contenteditable");
      element.removeAttribute("spellcheck");
    });
    clone.querySelectorAll("[data-pptx-chart]").forEach(element => {
      element.classList.remove("chart-runtime-ready", "chart-runtime-missing");
      element.removeAttribute("data-chart-renderer");
    });
    clone.querySelectorAll("[data-chart-canvas]").forEach(element => {
      element.replaceChildren();
      element.removeAttribute("style");
      element.removeAttribute("_echarts_instance_");
    });
    const clonePicker = clone.querySelector('[data-role="layout-picker"]');
    if (clonePicker) {
      clonePicker.setAttribute("hidden", "");
      const cloneOptions = clonePicker.querySelector('[data-role="layout-options"]');
      if (cloneOptions) cloneOptions.replaceChildren();
    }
    const cloneControls = clone.querySelector('[data-role="layout-controls"]');
    if (cloneControls) {
      cloneControls.setAttribute("hidden", "");
      const cloneGroups = cloneControls.querySelector('[data-role="layout-control-groups"]');
      if (cloneGroups) cloneGroups.replaceChildren();
    }
    clone.querySelectorAll('[data-action="add-slide"], [data-action="layout"], [data-action="adjust"]').forEach(button => {
      button.setAttribute("aria-expanded", "false");
    });
    const clonePresentButton = clone.querySelector('[data-action="present"]');
    if (clonePresentButton) clonePresentButton.setAttribute("aria-pressed", "false");
    const clonePresentProgress = clone.querySelector('[data-role="present-progress"]');
    if (clonePresentProgress) clonePresentProgress.removeAttribute("style");
    const cloneThumbnailList = clone.querySelector('[data-role="thumbnail-list"]');
    if (cloneThumbnailList) cloneThumbnailList.replaceChildren();
    const cloneExportButton = clone.querySelector('[data-action="export-pptx"]');
    if (cloneExportButton) {
      cloneExportButton.textContent = "导出 PPT";
      cloneExportButton.dataset.exportState = "unavailable";
      cloneExportButton.dataset.compactLabel = "PPT";
      cloneExportButton.disabled = false;
      cloneExportButton.setAttribute("aria-busy", "false");
    }
    const cloneSaveButton = clone.querySelector('[data-action="save"], [data-action="download"]');
    if (cloneSaveButton) {
      cloneSaveButton.textContent = "另存 HTML";
      cloneSaveButton.dataset.saveState = "download";
      cloneSaveButton.dataset.compactLabel = "HTML";
      cloneSaveButton.disabled = false;
      cloneSaveButton.setAttribute("aria-busy", "false");
    }
    const cloneModel = clone.querySelector("#deck-document");
    if (cloneModel) cloneModel.textContent = safeJson(documentModel);
    return `<!doctype html>\n${clone.outerHTML}\n`;
  }

  function downloadHtml() {
    const blob = new Blob([serializeHtml()], { type: "text/html;charset=utf-8" });
    const link = document.createElement("a");
    const name = String(documentModel.title || "deck").replace(/[^a-zA-Z0-9\u4e00-\u9fff_-]+/g, "-");
    link.href = URL.createObjectURL(blob);
    link.download = `${name || "deck"}-edited.html`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    showToast("已下载 HTML 副本；原文件未覆盖");
  }

  function postToHost(type, payload) {
    if (window.parent === window) return false;
    window.parent.postMessage({
      source: RUNTIME_MESSAGE_SOURCE,
      version: HOST_PROTOCOL_VERSION,
      type,
      ...payload,
    }, "*");
    return true;
  }

  function requestSave() {
    flushActiveTextEdit();
    if (saveInFlight) return;
    if (!hostSaveAvailable) {
      downloadHtml();
      return;
    }
    if (revision === savedRevision) {
      showToast("当前内容已保存");
      return;
    }

    const requestId = `deck-save-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const requestedRevision = revision;
    saveError = false;
    saveInFlight = { requestId, revision: requestedRevision };
    updateSaveButton();
    postToHost("save-request", {
      requestId,
      revision: requestedRevision,
      title: String(documentModel.title || "deck"),
      html: serializeHtml(),
    });
    saveTimer = setTimeout(() => {
      if (!saveInFlight || saveInFlight.requestId !== requestId) return;
      saveInFlight = null;
      saveError = true;
      updateSaveButton();
      showToast("保存超时，请重试");
    }, SAVE_TIMEOUT_MS);
  }

  function requestPptxExport() {
    flushActiveTextEdit();
    if (pptxExportInFlight) return;
    if (!hostPptxExportAvailable) {
      showToast("请在 officev3 中打开后导出可编辑 PPT");
      return;
    }

    const requestId = `deck-pptx-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    pptxExportInFlight = { requestId, revision };
    updatePptxExportButton();
    postToHost("export-pptx-request", {
      requestId,
      revision,
      title: String(documentModel.title || "deck"),
      html: serializeHtml(),
    });
    pptxExportTimer = setTimeout(() => {
      if (!pptxExportInFlight || pptxExportInFlight.requestId !== requestId) return;
      pptxExportInFlight = null;
      updatePptxExportButton();
      showToast("PPT 导出超时，请重试");
    }, PPTX_EXPORT_TIMEOUT_MS);
  }

  toolbar.addEventListener("click", event => {
    const menuTrigger = event.target.closest("[data-toolbar-menu-trigger]");
    if (menuTrigger) {
      const menu = menuTrigger.closest("[data-toolbar-menu]");
      closeToolbarMenus(menu);
      setToolbarMenuOpen(menu, true);
      return;
    }
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    closeToolbarMenus();
    const action = button.getAttribute("data-action");
    if (action === "previous") {
      currentIndex = Math.max(0, currentIndex - 1);
      scrollToCurrent();
    } else if (action === "next") {
      currentIndex = Math.min(slides().length - 1, currentIndex + 1);
      scrollToCurrent();
    } else if (action === "edit") {
      setEditing(!editing);
    } else if (action === "add-slide") {
      openLayoutPicker("add");
    } else if (action === "layout") {
      openLayoutPicker("replace");
    } else if (action === "adjust") {
      if (layoutControls && !layoutControls.hidden) closeLayoutControls();
      else openLayoutControls();
    } else if (action === "present") {
      enterPresentation();
    } else if (action === "duplicate") {
      duplicateCurrent();
    } else if (action === "delete") {
      deleteCurrent();
    } else if (action === "move-up") {
      moveCurrent(-1);
    } else if (action === "move-down") {
      moveCurrent(1);
    } else if (action === "export-pptx") {
      requestPptxExport();
    } else if (action === "save" || action === "download") {
      requestSave();
    }
  });

  document.addEventListener("click", event => {
    if (!event.target.closest("[data-toolbar-menu]")) closeToolbarMenus();
  });

  if (thumbnailList) {
    thumbnailList.addEventListener("click", event => {
      const button = event.target.closest("button[data-thumbnail-index]");
      if (!button) return;
      const nextIndex = Number(button.dataset.thumbnailIndex);
      if (!Number.isInteger(nextIndex) || nextIndex < 0 || nextIndex >= slides().length) return;
      currentIndex = nextIndex;
      scrollToCurrent("auto");
    });
  }

  if (layoutPicker) {
    layoutPicker.addEventListener("click", event => {
      const closeButton = event.target.closest('[data-layout-action="close"]');
      if (closeButton) {
        closeLayoutPicker();
        return;
      }
      const option = event.target.closest("button[data-layout-id]");
      if (!option) return;
      const layoutId = option.getAttribute("data-layout-id");
      if (pickerMode === "replace") changeCurrentLayout(layoutId);
      else addSlideWithLayout(layoutId);
    });

    document.addEventListener("click", event => {
      if (layoutPicker.hidden || layoutPicker.contains(event.target)) return;
      const trigger = event.target.closest('[data-action="add-slide"], [data-action="layout"]');
      if (!trigger) closeLayoutPicker();
    });
  }

  if (layoutControls) {
    layoutControls.addEventListener("click", event => {
      // The control DOM is rebuilt after every live layout change. Without
      // stopping propagation, the document-level outside-click handler sees
      // the now-detached event target and incorrectly closes the panel.
      event.stopPropagation();
      const button = event.target.closest("button[data-control-action]");
      if (!button) return;
      const action = button.dataset.controlAction;
      const pathValue = button.dataset.controlPath;
      if (action === "close") {
        closeLayoutControls();
      } else if (action === "set-enum") {
        setLayoutOption(pathValue, button.dataset.controlValue);
      } else if (action === "add-item") {
        addLayoutItem(pathValue);
      } else if (action === "delete-item") {
        deleteLayoutItem(pathValue, button.dataset.controlIndex);
      } else if (action === "move-item") {
        moveLayoutItem(pathValue, button.dataset.controlIndex, button.dataset.controlDirection);
      } else if (action === "add-chart-category") {
        mutateChartData("add-category");
      } else if (action === "delete-chart-category") {
        mutateChartData("delete-category", Number(button.dataset.controlIndex));
      } else if (action === "add-chart-series") {
        mutateChartData("add-series");
      } else if (action === "delete-chart-series") {
        mutateChartData("delete-series", Number(button.dataset.controlIndex));
      } else if (action === "add-diagram-node") {
        mutateDiagramData("add-node");
      } else if (action === "delete-diagram-node") {
        mutateDiagramData("delete-node", Number(button.dataset.controlIndex));
      } else if (action === "add-diagram-edge") {
        mutateDiagramData("add-edge");
      } else if (action === "delete-diagram-edge") {
        mutateDiagramData("delete-edge", Number(button.dataset.controlIndex));
      } else if (action === "relayout-diagram") {
        requestDiagramLayout(selectedSlide(), "已重新执行技术图布局");
      }
    });

    layoutControls.addEventListener("change", event => {
      event.stopPropagation();
      const input = event.target.closest('[data-control-action="set-data-value"]');
      if (!input) return;
      setChartDataValue(input.dataset.controlPath, input.value);
    });

    document.addEventListener("click", event => {
      const clickPath = typeof event.composedPath === "function" ? event.composedPath() : [];
      if (layoutControls.hidden || layoutControls.contains(event.target) || clickPath.includes(layoutControls)) {
        return;
      }
      const trigger = event.target.closest('[data-action="adjust"]');
      if (!trigger) closeLayoutControls();
    });
  }

  if (presentControls) {
    presentControls.addEventListener("click", event => {
      const button = event.target.closest("button[data-present-action]");
      if (!button) return;
      const action = button.dataset.presentAction;
      if (action === "previous") movePresentation(-1);
      else if (action === "next") movePresentation(1);
      else if (action === "exit") exitPresentation();
    });
  }

  root.addEventListener("focusout", event => {
    const element = event.target.closest('[data-prop-kind="text"][data-prop-path]');
    if (editing && element) applyTextEdit(element);
  });

  root.addEventListener("click", event => {
    if (presenting) {
      event.preventDefault();
      movePresentation(event.clientX < window.innerWidth * 0.32 ? -1 : 1);
      return;
    }
    const slide = event.target.closest(".slide");
    if (!slide) return;
    const index = slides().indexOf(slide);
    if (index >= 0 && index !== currentIndex) {
      currentIndex = index;
      updateToolbar();
    }
  });

  root.addEventListener("paste", event => {
    const element = event.target.closest('[data-prop-kind="text"]');
    if (!editing || !element) return;
    event.preventDefault();
    const text = (event.clipboardData || window.clipboardData).getData("text/plain");
    document.execCommand("insertText", false, text);
  });

  root.addEventListener("dblclick", event => {
    const element = event.target.closest('[data-prop-kind="image"][data-prop-path]');
    if (!editing || !element) return;
    pendingImageTarget = element;
    imageInput.value = "";
    imageInput.click();
  });

  imageInput.addEventListener("change", () => {
    const file = imageInput.files && imageInput.files[0];
    if (!file || !pendingImageTarget) return;
    const target = pendingImageTarget;
    pendingImageTarget = null;
    const reader = new FileReader();
    reader.onload = () => {
      const modelSlide = modelSlideForElement(target);
      const propPath = target.getAttribute("data-prop-path");
      if (!modelSlide || !propPath) return;
      const dataUrl = String(reader.result || "");
      const modelRoot = target.getAttribute("data-model-root") === "slide"
        ? modelSlide
        : modelSlide.props;
      setAtPath(modelRoot, propPath, dataUrl);
      if (propPath.endsWith(".src")) {
        setAtPath(modelRoot, propPath.replace(/\.src$/, ".origin"), "uploaded");
      }
      if (target.tagName === "IMG") {
        target.setAttribute("src", dataUrl);
        target.classList.remove("editor-placeholder-image");
        emitChange("replace-image");
        showToast("图片已替换；保存 HTML 时会内嵌该图片");
      } else {
        rerenderCurrentSlide("replace-image", "主视觉已替换；保存 HTML 时会内嵌该图片");
      }
    };
    reader.readAsDataURL(file);
  });

  document.addEventListener("keydown", event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      requestSave();
      return;
    }
    if (event.key === "Escape" && toolbarMenus.some(menu => menu.classList.contains("is-open"))) {
      event.preventDefault();
      closeToolbarMenus();
      return;
    }
    if (presenting) {
      if (event.key === "Escape") {
        event.preventDefault();
        exitPresentation();
      } else if (event.key === "ArrowRight" || event.key === " " || event.key === "PageDown") {
        event.preventDefault();
        movePresentation(1);
      } else if (event.key === "ArrowLeft" || event.key === "PageUp") {
        event.preventDefault();
        movePresentation(-1);
      } else if (event.key === "Home") {
        event.preventDefault();
        currentIndex = 0;
        updateToolbar();
      } else if (event.key === "End") {
        event.preventDefault();
        currentIndex = slides().length - 1;
        updateToolbar();
      }
      return;
    }
    if (layoutPicker && !layoutPicker.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeLayoutPicker();
      }
      return;
    }
    if (layoutControls && !layoutControls.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeLayoutControls();
      }
      return;
    }
    if (editing && event.target.closest('[contenteditable="true"]')) return;
    if (event.key === "ArrowRight" || event.key === "PageDown") {
      event.preventDefault();
      currentIndex = Math.min(slides().length - 1, currentIndex + 1);
      scrollToCurrent();
    } else if (event.key === "ArrowLeft" || event.key === "PageUp") {
      event.preventDefault();
      currentIndex = Math.max(0, currentIndex - 1);
      scrollToCurrent();
    }
  });

  const observer = new IntersectionObserver(entries => {
    if (presenting) return;
    if (Date.now() < observerLockedUntil) return;
    const visible = entries
      .filter(entry => entry.isIntersecting)
      .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
    if (!visible) return;
    const index = slides().indexOf(visible.target);
    if (index >= 0) {
      currentIndex = index;
      updateToolbar();
    }
  }, { threshold: [0.25, 0.5, 0.75] });
  slides().forEach(slide => observer.observe(slide));

  window.addEventListener("resize", () => {
    updateEditorScale();
    updatePresentationScale();
  });
  document.addEventListener("fullscreenchange", () => {
    if (presenting && presentationOwnsFullscreen && !document.fullscreenElement) {
      exitPresentation(false);
    }
  });

  window.addEventListener("message", event => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.source !== HOST_MESSAGE_SOURCE || message.version !== HOST_PROTOCOL_VERSION) {
      return;
    }
    if (message.type === "host-ready") {
      hostSaveAvailable = message.canSave === true;
      hostPptxExportAvailable = message.canExportPptx === true;
      saveError = false;
      updateSaveButton();
      updatePptxExportButton();
      return;
    }

    if (message.type === "export-pptx-result") {
      if (!pptxExportInFlight || message.requestId !== pptxExportInFlight.requestId) return;
      if (pptxExportTimer) clearTimeout(pptxExportTimer);
      pptxExportTimer = null;
      pptxExportInFlight = null;
      updatePptxExportButton();
      if (message.ok === true) {
        showToast("PPT 已导出");
      } else if (message.code === "canceled") {
        showToast("已取消导出");
      } else {
        showToast(String(message.error || "PPT 导出失败，请重试"));
      }
      return;
    }

    if (
      message.type !== "save-result"
      || !saveInFlight
      || message.requestId !== saveInFlight.requestId
    ) {
      return;
    }

    if (saveTimer) clearTimeout(saveTimer);
    const completedSave = saveInFlight;
    saveTimer = null;
    saveInFlight = null;
    if (message.ok === true) {
      savedRevision = completedSave.revision;
      saveError = false;
      showToast(
        revision === savedRevision
          ? "已保存到当前 HTML 文件"
          : "上一版已保存，当前仍有未保存修改"
      );
    } else {
      saveError = true;
      showToast(
        message.code === "conflict"
          ? "文件已在外部改变，请重新打开后再编辑"
          : String(message.error || "保存失败，请重试")
      );
    }
    updateSaveButton();
  });

  window.__deckRuntime = {
    addSlide: addSlideWithLayout,
    addLayoutItem,
    changeLayout: changeCurrentLayout,
    deleteLayoutItem,
    enterPresentation,
    exitPresentation,
    getDocument: () => deepClone(documentModel),
    getRevision: () => revision,
    getSaveState,
    isPresenting: () => presenting,
    moveLayoutItem,
    openLayoutControls,
    openLayoutPicker,
    requestPptxExport,
    requestSave,
    setLayoutOption,
    serializeHtml,
    subscribe(callback) {
      subscribers.add(callback);
      return () => subscribers.delete(callback);
    },
  };

  updateEditorScale();
  renderThumbnails();
  updateToolbar();
  postToHost("ready", {
    revision,
    title: String(documentModel.title || "deck"),
  });
})();
