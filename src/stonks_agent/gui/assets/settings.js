"use strict";
(() => {
  const el = (id) => document.getElementById(id);
  const dom = {
    panel: el("model-settings"),
    status: el("model-settings-status"),
    summary: el("model-settings-summary"),
    disclosure: el("model-settings-disclosure"),
    toggle: el("model-settings-toggle"),
    form: el("model-settings-form"),
    error: el("model-settings-error"),
    clear: el("model-settings-clear"),
    key: el("model-api-key"),
    keyToggle: el("model-api-key-toggle"),
  };
  const state = {
    token: "",
    busy: false,
    current: null,
  };
  const numeric = new Set([
    "max_output_tokens",
    "max_total_tokens",
    "max_transient_retries",
    "max_repairs",
    "max_response_bytes",
  ]);
  const decimal = new Set([
    "input_cost_per_million",
    "cached_input_cost_per_million",
    "cache_write_input_cost_per_million",
    "output_cost_per_million",
    "max_cost_usd",
    "timeout_seconds",
  ]);
  function configure(capabilities) {
    const research = capabilities && capabilities.research;
    state.token =
      research && typeof research.intent_token === "string"
        ? research.intent_token
        : "";
    render(
      capabilities && capabilities.model_settings
        ? capabilities.model_settings
        : unavailableView()
    );
  }
  function render(view) {
    state.current = view;
    const configured = view && view.state === "configured" && view.config;
    const available = view && view.state !== "unavailable";
    dom.panel.dataset.state = view ? view.state : "unavailable";
    dom.status.textContent = configured
      ? view.verified
        ? "已驗證"
        : "尚未驗證"
      : available
        ? "需要設定"
        : "不可用";
    dom.summary.textContent = summary(view);
    dom.toggle.textContent = configured ? "變更模型設定" : "設定模型連線";
    dom.disclosure.open = Boolean(available && !configured);
    fill(view && view.config);
    setDisabled(!available || state.busy);
    dom.clear.disabled = !configured || state.busy;
    emit(view);
  }
  function summary(view) {
    if (!view || view.state === "unavailable") {
      return "此工作階段沒有組合模型設定服務。";
    }
    if (view.state !== "configured" || !view.config) {
      return "開始研究前，請先設定並驗證一個支援 JSON Schema 的 OpenAI-compatible 模型。";
    }
    const config = view.config;
    const host = safeHost(config.base_url);
    const test = view.connection_test;
    const verified = view.verified ? "structured completion 已驗證" : "尚未於本次 session 驗證";
    const usage = test
      ? ` · ${test.input_tokens} in / ${test.output_tokens} out · ${test.elapsed_ms} ms`
      : "";
    return `${config.model_id} · ${host} · ${verified}${usage}`;
  }
  function safeHost(value) {
    try {
      const url = new URL(value);
      return url.host || "custom endpoint";
    } catch {
      return "custom endpoint";
    }
  }
  function fill(config) {
    if (!config) return;
    for (const [name, value] of Object.entries(config)) {
      const field = dom.form.elements.namedItem(name);
      if (field instanceof HTMLInputElement) field.value = String(value);
    }
    dom.key.value = "";
    hideKey();
  }
  function setDisabled(disabled) {
    for (const field of dom.form.elements) {
      if (
        field instanceof HTMLInputElement ||
        field instanceof HTMLButtonElement
      ) {
        if (field.id !== "model-provider") field.disabled = disabled;
      }
    }
  }
  function emit(view) {
    window.dispatchEvent(
      new CustomEvent("stonks:model-settings", { detail: view })
    );
  }
  function payload() {
    const value = {};
    for (const field of dom.form.elements) {
      if (!(field instanceof HTMLInputElement) || !field.name) continue;
      if (numeric.has(field.name)) value[field.name] = Number.parseInt(field.value, 10);
      else if (decimal.has(field.name)) value[field.name] = field.value;
      else value[field.name] = field.value;
    }
    return value;
  }
  async function save(event) {
    event.preventDefault();
    clearErrors();
    if (state.busy || !state.token) return;
    if (!dom.form.checkValidity()) {
      const invalid = dom.form.querySelector(":invalid");
      if (invalid) {
        invalid.setAttribute("aria-invalid", "true");
        invalid.focus();
      }
      dom.form.reportValidity();
      showError("請修正標示的模型設定欄位。");
      return;
    }
    state.busy = true;
    dom.form.setAttribute("aria-busy", "true");
    dom.panel.dataset.state = "testing";
    dom.status.textContent = "驗證中";
    dom.summary.textContent = "正在執行 bounded structured completion…";
    setDisabled(true);
    const requestBody = JSON.stringify(payload());
    dom.key.value = "";
    hideKey();
    const result = await request("/api/v1/settings/llm", {
      method: "PUT",
      headers: mutationHeaders(),
      body: requestBody,
    });
    state.busy = false;
    dom.form.setAttribute("aria-busy", "false");
    if (!result.ok || !result.data) {
      render(state.current || unavailableView());
      dom.disclosure.open = true;
      showError(`${result.message || "模型連線驗證失敗"}（${result.code}）`);
      dom.key.focus();
      return;
    }
    render(result.data);
    dom.disclosure.open = false;
    dom.toggle.focus();
    window.dispatchEvent(new CustomEvent("stonks:refresh-capabilities"));
  }
  async function clearSettings() {
    if (
      state.busy ||
      !state.token ||
      !window.confirm("清除本次 session 的模型設定與 API key？")
    ) {
      return;
    }
    state.busy = true;
    setDisabled(true);
    dom.status.textContent = "清除中";
    const result = await request("/api/v1/settings/llm", {
      method: "DELETE",
      headers: mutationHeaders(),
      body: "{}",
    });
    state.busy = false;
    dom.key.value = "";
    hideKey();
    if (!result.ok || !result.data) {
      render(state.current || unavailableView());
      showError(`${result.message || "無法清除模型設定"}（${result.code}）`);
      return;
    }
    resetDefaults();
    render(result.data);
    window.dispatchEvent(new CustomEvent("stonks:refresh-capabilities"));
  }
  function mutationHeaders() {
    return {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-Stonks-Intent": state.token,
    };
  }
  async function request(path, options) {
    try {
      const response = await fetch(path, {
        ...options,
        cache: "no-store",
        credentials: "same-origin",
      });
      const envelope = await response.json();
      return {
        ok: response.ok && envelope.success === true,
        data: envelope.data,
        code: envelope.error && envelope.error.code,
        message: envelope.error && envelope.error.message,
      };
    } catch {
      return {
        ok: false,
        code: "data_unavailable",
        message: "模型設定服務沒有回應",
      };
    }
  }
  function clearErrors() {
    dom.error.hidden = true;
    dom.error.textContent = "";
    for (const field of dom.form.querySelectorAll("[aria-invalid]")) {
      field.removeAttribute("aria-invalid");
    }
  }
  function showError(message) {
    dom.error.textContent = message;
    dom.error.hidden = false;
  }
  function hideKey() {
    dom.key.type = "password";
    dom.keyToggle.textContent = "顯示";
    dom.keyToggle.setAttribute("aria-pressed", "false");
  }
  function toggleKey() {
    const show = dom.key.type === "password";
    dom.key.type = show ? "text" : "password";
    dom.keyToggle.textContent = show ? "隱藏" : "顯示";
    dom.keyToggle.setAttribute("aria-pressed", String(show));
    dom.key.focus();
  }
  function resetDefaults() {
    dom.form.reset();
    dom.key.value = "";
    hideKey();
  }
  function unavailableView() {
    return {
      state: "unavailable",
      detail: "Model settings runtime is not composed.",
      source: "none",
      verified: false,
    };
  }
  window.addEventListener("stonks:capabilities", (event) =>
    configure(event.detail)
  );
  dom.form.addEventListener("submit", save);
  dom.clear.addEventListener("click", clearSettings);
  dom.keyToggle.addEventListener("click", toggleKey);
  window.addEventListener("pagehide", () => {
    dom.key.value = "";
    hideKey();
  });
  dom.form.addEventListener(
    "invalid",
    (event) => event.target.setAttribute("aria-invalid", "true"),
    true
  );
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !dom.disclosure.open || state.busy) return;
    dom.disclosure.open = false;
    dom.toggle.focus();
  });
})();
