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
      return "目前沒有可用的模型設定服務。";
    }
    if (view.state !== "configured" || !view.config) {
      return "輸入模型網址、模型名稱和存取金鑰，系統會先測試連線。";
    }
    const config = view.config;
    const host = safeHost(config.base_url);
    const test = view.connection_test;
    const verified = view.verified ? "連線已驗證" : "尚未驗證";
    const usage = test
      ? ` · 輸入 ${test.input_tokens} · 輸出 ${test.output_tokens} · ${test.elapsed_ms} ms`
      : "";
    return `${config.model_id} · ${host} · ${verified}${usage}`;
  }
  function safeHost(value) {
    try {
      const url = new URL(value);
      return url.host || "自訂網址";
    } catch {
      return "自訂網址";
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
        field.disabled = disabled;
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
      value[field.name] = field.value;
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
      showError("請補齊模型網址、模型名稱和存取金鑰。");
      return;
    }
    state.busy = true;
    dom.form.setAttribute("aria-busy", "true");
    dom.panel.dataset.state = "testing";
    dom.status.textContent = "驗證中";
    dom.summary.textContent = "正在測試模型連線…";
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
      !window.confirm("清除本次設定與存取金鑰？")
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
      detail: "模型設定服務尚未組合。",
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
