(function interactionRuntime(root) {
  "use strict";

  const interactions = Array.from(root.document.querySelectorAll("[data-deck-interaction-target]"));
  interactions.forEach(container => {
    const toggle = container.querySelector("[data-deck-interaction-toggle]");
    if (toggle) {
      toggle.addEventListener("click", () => {
        const paused = container.classList.toggle("is-paused");
        toggle.textContent = paused ? "继续运动" : "暂停运动";
        toggle.setAttribute("aria-pressed", paused ? "true" : "false");
      });
    }

    const spinStage = container.querySelector("[data-deck-spin-stage]");
    if (spinStage) {
      let dragging = false;
      let previousX = 0;
      let angle = 0;
      spinStage.addEventListener("pointerdown", event => {
        dragging = true;
        previousX = event.clientX;
        spinStage.classList.add("is-user-controlled");
        spinStage.setPointerCapture(event.pointerId);
      });
      spinStage.addEventListener("pointermove", event => {
        if (!dragging) return;
        angle += (event.clientX - previousX) * 0.7;
        previousX = event.clientX;
        spinStage.style.setProperty("--deck-spin-angle", `${angle}deg`);
      });
      const stopDragging = event => {
        dragging = false;
        if (spinStage.hasPointerCapture(event.pointerId)) {
          spinStage.releasePointerCapture(event.pointerId);
        }
      };
      spinStage.addEventListener("pointerup", stopDragging);
      spinStage.addEventListener("pointercancel", stopDragging);
    }
    container.dataset.interactionReady = "true";
  });
  root.__deckInteractionReady = Promise.resolve({ count: interactions.length });
})(window);
