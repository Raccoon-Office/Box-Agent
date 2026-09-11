(() => {
  const dialog = document.querySelector("#evaluation-dialog");
  const openButton = document.querySelector("#open-evaluation-dialog");
  const closeButtons = [
    document.querySelector("#close-evaluation-dialog"),
    document.querySelector("#cancel-evaluation"),
  ];
  const form = document.querySelector("#evaluation-form");
  const taskTypeSelect = document.querySelector("#evaluation-task-type");
  const datasetSelect = document.querySelector("#evaluation-dataset");
  const modelSelect = document.querySelector("#evaluation-model");
  const datasetHint = document.querySelector("#dataset-hint");
  const countInput = document.querySelector("#evaluation-count");
  const countHint = document.querySelector("#count-hint");
  const disclosure = document.querySelector("#disclosure-confirmation");
  const disclosureCheckbox = document.querySelector("#approved-data-disclosure");
  const status = document.querySelector("#evaluation-status");
  const submitButton = document.querySelector("#submit-evaluation");
  let datasets = [];
  let pollTimer = null;

  if (!dialog || !openButton || !form) return;

  const setStatus = (message, tone = "") => {
    status.replaceChildren();
    status.textContent = message;
    status.dataset.tone = tone;
  };

  const responseDetail = async (response) => {
    try {
      const payload = await response.json();
      return payload.detail || `请求失败（HTTP ${response.status}）`;
    } catch (_error) {
      return `请求失败（HTTP ${response.status}）`;
    }
  };

  const selectedDatasetStats = (dataset) => {
    if (!dataset) return null;
    if (!taskTypeSelect.value) {
      return {
        item_count: dataset.item_count,
        attachment_count: dataset.attachment_count,
      };
    }
    return (dataset.task_type_stats || []).find(
      (item) => item.name === taskTypeSelect.value,
    ) || null;
  };

  const updateDatasetHint = () => {
    const selected = datasets.find((item) => item.id === datasetSelect.value);
    const stats = selectedDatasetStats(selected);
    const attachmentCount = stats ? stats.attachment_count : 0;
    const itemCount = stats ? stats.item_count : 0;
    datasetHint.textContent = selected
      ? `${itemCount} 个可执行任务${attachmentCount ? `，包含 ${attachmentCount} 个附件` : "，无附件"}`
      : "请选择本次需要执行的任务集合。";
    countInput.disabled = !selected || itemCount < 1;
    if (selected && itemCount > 0) {
      countInput.max = String(itemCount);
      countInput.value = String(itemCount);
      countHint.textContent = `本次可设置 1–${itemCount} 条，实际样本将记录在 selection.json。`;
    } else {
      countInput.removeAttribute("max");
      countInput.value = "";
      countHint.textContent = "条数上限随任务类型和数据集变化。";
    }
    disclosure.hidden = !attachmentCount;
    disclosureCheckbox.required = Boolean(attachmentCount);
    if (!attachmentCount) disclosureCheckbox.checked = false;
  };

  const updateDatasetOptions = () => {
    const taskType = taskTypeSelect.value;
    const compatible = taskType
      ? datasets.filter((item) => (item.task_types || []).includes(taskType))
      : datasets;
    fillSelect(
      datasetSelect,
      compatible.map((item) => {
        const stats = selectedDatasetStats(item);
        return {
          id: item.id,
          label: `${item.name}（${stats ? stats.item_count : item.item_count} 个任务）`,
        };
      }),
      "数据集",
    );
    updateDatasetHint();
  };

  const fillSelect = (select, values, label) => {
    select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = `请选择${label}`;
    select.append(placeholder);
    values.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.label;
      select.append(option);
    });
    select.disabled = values.length === 0;
  };

  const loadOptions = async () => {
    setStatus("正在从 RaccoonOps 读取可用配置…");
    submitButton.disabled = true;
    try {
      const response = await fetch("/api/evaluation-options", {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(await responseDetail(response));
      const payload = await response.json();
      datasets = Array.isArray(payload.datasets) ? payload.datasets : [];
      const models = Array.isArray(payload.models) ? payload.models : [];
      const taskTypes = [...new Set(datasets.flatMap((item) => item.task_types || []))].sort();
      taskTypeSelect.replaceChildren();
      const allTaskTypes = document.createElement("option");
      allTaskTypes.value = "";
      allTaskTypes.textContent = "全部任务类型";
      taskTypeSelect.append(allTaskTypes);
      taskTypes.forEach((taskType) => {
        const option = document.createElement("option");
        option.value = taskType;
        option.textContent = taskType;
        taskTypeSelect.append(option);
      });
      taskTypeSelect.disabled = datasets.length === 0;
      updateDatasetOptions();
      fillSelect(
        modelSelect,
        models.map((item) => ({
          id: item.id,
          label: item.multiplier ? `${item.name} · ${item.multiplier}` : item.name,
        })),
        "被测模型",
      );
      if (!datasets.length || !models.length) {
        setStatus("Ops 中暂无可用数据集或模型绑定配置。", "error");
        return;
      }
      setStatus("");
      submitButton.disabled = false;
    } catch (error) {
      setStatus(error.message || "读取评测配置失败。", "error");
    }
  };

  const pollLaunch = async (launchId) => {
    try {
      const response = await fetch(`/api/evaluation-runs/${encodeURIComponent(launchId)}`);
      if (!response.ok) throw new Error(await responseDetail(response));
      const payload = await response.json();
      if (["queued", "running"].includes(payload.status)) {
        setStatus(payload.status === "queued" ? "任务已进入队列…" : "评测正在执行，请勿关闭服务…");
        pollTimer = window.setTimeout(() => pollLaunch(launchId), 1500);
        return;
      }
      submitButton.disabled = false;
      if (["completed", "completed_with_failures"].includes(payload.status)) {
        const message = payload.status === "completed" ? "评测已完成。" : "评测已结束，部分任务未通过。";
        setStatus(message, payload.status === "completed" ? "success" : "warning");
        if (payload.run_name) {
          const link = document.createElement("a");
          link.href = `/runs/${encodeURIComponent(payload.run_name)}`;
          link.textContent = "查看评测结果";
          status.append(" ", link);
        }
      } else {
        setStatus(`任务启动失败：${payload.error || "未知错误"}`, "error");
      }
    } catch (error) {
      submitButton.disabled = false;
      setStatus(error.message || "读取任务状态失败。", "error");
    }
  };

  openButton.addEventListener("click", () => {
    dialog.showModal();
    loadOptions();
  });
  closeButtons.forEach((button) => button?.addEventListener("click", () => dialog.close()));
  dialog.addEventListener("close", () => {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = null;
  });
  taskTypeSelect.addEventListener("change", updateDatasetOptions);
  datasetSelect.addEventListener("change", updateDatasetHint);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    submitButton.disabled = true;
    setStatus("正在创建评测任务…");
    try {
      const response = await fetch("/api/evaluation-runs", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          dataset_id: datasetSelect.value,
          model_id: modelSelect.value,
          task_type: taskTypeSelect.value || null,
          execution_count: Number(countInput.value),
          approved_data_disclosure: disclosureCheckbox.checked,
        }),
      });
      if (!response.ok) throw new Error(await responseDetail(response));
      const payload = await response.json();
      setStatus("任务已创建，等待执行…", "success");
      pollLaunch(payload.launch_id);
    } catch (error) {
      submitButton.disabled = false;
      setStatus(error.message || "创建评测任务失败。", "error");
    }
  });
})();
