"use strict";
(() => {
  const RUN_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const SYMBOL = /^[A-Z0-9][A-Z0-9.-]{0,15}$/;
  const INTERVALS = new Set(["1m", "5m", "15m", "1h", "1d"]);
  const EVENT_TYPES = [
    "research.queued",
    "research.running",
    "research.degraded",
    "research.failed",
    "research.succeeded",
    "research.cancelled",
  ];
  const TERMINAL = new Set([
    "research.degraded",
    "research.failed",
    "research.succeeded",
    "research.cancelled",
  ]);
  const PHASES = [
    ["snapshot", "建立市場快照", "從即時資料來源保存本次價格資料"],
    ["evidence", "鎖定研究證據", "只使用本次資料中的唯讀工具"],
    ["analysis", "整理研究結論", "產生有來源的結論、反方觀點與風險"],
    ["report", "形成研究報告", "保存報告並讀取模擬帳戶結論"],
  ];
  const state = {
    token: "",
    profile: "",
    runtimeReady: false,
    modelReady: false,
    ready: false,
    source: null,
    runId: "",
    symbol: "",
    events: [],
    terminal: false,
    busy: false,
    serial: 0,
    hasResult: false,
  };
  const el = (id) => document.getElementById(id);
  const dom = {
    panel: el("panel-research"),
    note: el("research-note"),
    empty: el("research-empty"),
    progress: el("research-progress"),
    results: el("research-results"),
    summary: el("research-summary"),
    claims: el("research-claims"),
    counters: el("research-counters"),
    risks: el("research-risks"),
    signals: el("research-signals"),
    reportWrap: el("research-report-wrap"),
    report: el("research-report"),
    action: el("research-action"),
    body: el("research-body"),
    profile: el("research-profile"),
    evidence: el("research-evidence"),
    evidenceNote: el("research-evidence-note"),
    transparency: el("research-transparency"),
    transparencyWrap: el("research-transparency-wrap"),
    history: el("research-history"),
    historyNote: el("research-history-note"),
    historyRefresh: el("research-history-refresh"),
  };
  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }
  function add(parent, tag, text, attrs) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    for (const [name, value] of Object.entries(attrs || {})) {
      node.setAttribute(name, String(value));
    }
    parent.appendChild(node);
    return node;
  }
  function rail(value) {
    dom.panel.setAttribute("data-rail", value);
  }
  function show(view) {
    dom.empty.hidden = view !== "empty";
    dom.progress.hidden = view !== "progress";
    dom.results.hidden = view !== "results";
  }
  function configure(capabilities) {
    const research = capabilities && capabilities.research;
    if (!research || research.state !== "ready") {
      unavailable(
        (research && research.detail) ||
          "此工作階段尚未連上 AI 研究服務。"
      );
      return;
    }
    const profiles = Array.isArray(research.allowed_profiles)
      ? research.allowed_profiles
      : [];
    if (
      typeof research.intent_token !== "string" ||
      research.intent_token.length < 32 ||
      !profiles.includes(research.default_profile)
    ) {
      unavailable("研究能力回應無效，系統已停止送出工作。");
      return;
    }
    state.runtimeReady = true;
    state.ready = state.modelReady;
    state.token = research.intent_token;
    state.profile = research.default_profile;
    clear(dom.profile);
    for (const profile of profiles) {
      const option = add(dom.profile, "option", profile === "balanced/1" ? "一般模式" : profile, { value: profile });
      option.selected = profile === state.profile;
    }
    dom.profile.disabled = false;
    loadHistory();
    if (!state.hasResult && !state.busy) {
      dom.note.textContent = state.ready ? "可以開始" : "請先設定模型";
      show("empty");
      rail("idle");
    }
    dom.action.disabled = !state.ready || state.busy;
  }
  function unavailable(message) {
    state.runtimeReady = false;
    state.ready = false;
    state.token = "";
    if (!state.busy) dom.action.disabled = true;
    dom.profile.disabled = true;
    dom.note.textContent = "尚未連線";
    show("empty");
    clear(dom.empty);
    add(dom.empty, "span", "AI", { class: "empty-mark", "aria-hidden": "true" });
    add(dom.empty, "h3", "研究服務目前不可用");
    add(dom.empty, "p", message);
    rail("idle");
  }
  function configureModel(view) {
    state.modelReady = Boolean(
      view &&
        view.state === "configured" &&
        view.api_key_configured === true &&
        view.verified === true
    );
    state.ready = state.runtimeReady && state.modelReady;
    dom.action.disabled = !state.ready || state.busy;
    if (state.runtimeReady && !state.busy && !state.hasResult) {
      dom.note.textContent = state.modelReady ? "可以開始" : "請先設定模型";
      clear(dom.empty);
      add(dom.empty, "span", "模型", {
        class: "empty-mark",
        "aria-hidden": "true",
      });
      add(
        dom.empty,
        "h3",
        state.modelReady ? "從可追溯證據開始研究" : "先完成模型連線"
      );
      add(
        dom.empty,
        "p",
        state.modelReady
          ? "選好標的後開始研究；所有結論都會連回本輪實際讀取的資料。"
          : "在上方輸入模型網址、模型名稱和存取金鑰，驗證成功後即可開始。"
      );
      show("empty");
    }
  }
  async function jsonRequest(path, options) {
    let response;
    try {
      response = await fetch(new URL(path, window.location.origin), {
        ...options,
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
      });
    } catch {
      return {
        ok: false,
        code: "unreachable",
        message: "無法連線到本機研究服務",
      };
    }
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok || !payload || payload.success !== true) {
      const failure = (payload && payload.error) || {};
      return {
        ok: false,
        code: failure.code || String(response.status),
        message: failure.message || "研究請求失敗",
      };
    }
    return { ok: true, data: payload.data };
  }
  async function start(detail) {
    const symbol = String((detail && detail.symbol) || "").toUpperCase();
    const interval = String((detail && detail.interval) || "");
    if (!state.ready) {
      if (state.runtimeReady) {
        configureModel(null);
      } else {
        unavailable("研究 runtime 尚未組合，沒有送出任何工作。");
      }
      return;
    }
    if (state.busy) {
      dom.note.textContent = `${state.symbol} · 執行中`;
      return;
    }
    if (!SYMBOL.test(symbol) || !INTERVALS.has(interval)) {
      dom.note.textContent = "輸入無效";
      rail("failed");
      return;
    }
    state.busy = true;
    state.terminal = false;
    state.events = [];
    state.symbol = symbol;
    state.runId = "";
    state.hasResult = false;
    state.serial += 1;
    const serial = state.serial;
    dom.action.disabled = true;
    dom.body.setAttribute("aria-busy", "true");
    dom.note.textContent = `${symbol} · 建立快照`;
    renderProgress(0, "current");
    rail("loading");
    const result = await jsonRequest("/api/v1/research/runs", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Stonks-Intent": state.token,
      },
      body: JSON.stringify({ symbol, interval, profile: state.profile }),
    });
    if (serial !== state.serial) return;
    if (!result.ok || !result.data || !RUN_ID.test(result.data.run_id || "")) {
      finishFailure(
        symbol,
        result.code || "invalid_response",
        result.message || "研究工作回應無效。"
      );
      return;
    }
    state.runId = result.data.run_id;
    renderProgress(1, "current");
    stream(symbol, serial);
  }
  function stream(symbol, serial) {
    const path = `/api/v1/research/runs/${encodeURIComponent(state.runId)}/events`;
    const source = new EventSource(path);
    state.source = source;
    for (const eventType of EVENT_TYPES) {
      source.addEventListener(eventType, (event) =>
        consume(eventType, event, symbol, serial)
      );
    }
    source.onerror = () => {
      if (!state.terminal && serial === state.serial) {
        dom.note.textContent = `${symbol} · 等待重新連線`;
      }
    };
  }
  function consume(eventType, event, symbol, serial) {
    if (serial !== state.serial) return;
    let envelope = null;
    try {
      envelope = JSON.parse(event.data);
    } catch {
      envelope = null;
    }
    if (!envelope || envelope.success !== true || !envelope.data) return;
    const stage = String(
      (envelope.data.payload && envelope.data.payload.stage) || eventType
    );
    state.events.push({ eventType, stage });
    const phase = phaseFor(stage, eventType);
    renderProgress(phase, TERMINAL.has(eventType) ? "terminal" : "current");
    dom.note.textContent = `${symbol} · ${phaseLabel(phase)}`;
    if (!TERMINAL.has(eventType)) return;
    state.terminal = true;
    state.source.close();
    state.source = null;
    loadDetail(symbol, serial);
  }
  function phaseFor(stage, eventType) {
    const value = `${stage} ${eventType}`.toLowerCase();
    if (value.includes("report") || TERMINAL.has(eventType)) return 3;
    if (
      value.includes("research") ||
      value.includes("model") ||
      value.includes("tool")
    ) {
      return 2;
    }
    if (value.includes("snapshot")) return 0;
    return 1;
  }
  function phaseLabel(index) {
    return PHASES[Math.max(0, Math.min(PHASES.length - 1, index))][1];
  }
  function renderProgress(active, mode) {
    show("progress");
    clear(dom.progress);
    PHASES.forEach((phase, index) => {
      const item = add(dom.progress, "li", null, {
        "data-state":
          index < active || (mode === "terminal" && index === active)
            ? "complete"
            : index === active
              ? "current"
              : "pending",
      });
      add(item, "span", index < active || mode === "terminal" ? "✓" : index + 1, {
        class: "progress-index",
        "aria-hidden": "true",
      });
      const copy = add(item, "span");
      add(copy, "strong", phase[1]);
      add(copy, "small", phase[2]);
      add(item, "small", index < active ? "完成" : index === active ? "處理中" : "等待");
    });
  }
  async function loadDetail(symbol, serial) {
    const result = await jsonRequest(
      `/api/v1/research/runs/${encodeURIComponent(state.runId)}`,
      { headers: { Accept: "application/json" } }
    );
    if (serial !== state.serial) return;
    if (!result.ok) {
      finishFailure(symbol, result.code, result.message);
      return;
    }
    renderDetail(result.data);
    loadEvidence(result.data.run_id, serial);
    finish();
  }
  function renderDetail(view) {
    state.hasResult = true;
    show("results");
    const status = String(view.status || "unknown");
    dom.note.textContent = `${view.symbol || state.symbol} · ${statusText(status)}`;
    renderSummary(view, status);
    renderClaims(view.claims || []);
    renderList(dom.counters, view.counterarguments || [], "未產生反方觀點。");
    renderList(dom.risks, view.risks || [], "未產生風險項目。");
    renderSignals(view);
    renderTransparency(view);
    dom.report.textContent = view.report_content || "";
    dom.reportWrap.hidden = !view.report_content;
    dom.reportWrap.open = false;
    rail(
      status === "succeeded"
        ? "fresh"
        : status === "degraded"
          ? "stale"
          : "failed"
    );
  }
  function renderSummary(view, status) {
    clear(dom.summary);
    const copy = add(dom.summary, "div");
    add(copy, "h3", summaryTitle(status));
    add(
      copy,
      "p",
      view.error_code
        ? `研究未 commit：${view.error_code}`
        : "結論來自本次資料與允許的資料工具。"
    );
    if (view.confidence === null || view.confidence === undefined) return;
    const confidence = Math.max(0, Math.min(100, Number(view.confidence) * 100));
    const meter = add(dom.summary, "div", null, { class: "confidence" });
    add(meter, "span", `${Math.round(confidence)}%`);
    add(meter, "small", "信心");
  }
  function renderClaims(claims) {
    clear(dom.claims);
    if (!claims.length) {
      add(dom.claims, "li", "沒有可安全顯示的 evidence-backed claim。");
      return;
    }
    for (const claim of claims) {
      const item = add(dom.claims, "li");
      add(item, "span", claim.text || "—");
      const refs = add(item, "div", null, { class: "evidence-refs" });
      for (const reference of claim.evidence_ids || []) {
        const button = add(refs, "button", `證據 ${String(reference).slice(0, 8)}`, {
          class: "evidence-ref",
          title: String(reference),
          type: "button",
          "aria-label": `查看證據 ${reference}`,
        });
        button.addEventListener("click", () => focusEvidence(String(reference)));
      }
    }
  }
  function renderTransparency(view) {
    clear(dom.transparency);
    const grid = add(dom.transparency, "dl", null, { class: "transparency-grid" });
    pair(grid, "資料時間", stamp(view.as_of));
    pair(grid, "資料快照", view.snapshot_id || "尚未建立");
    pair(grid, "資料筆數", view.evidence_count ?? 0);
    if (view.usage) {
      pair(grid, "分析次數／工具次數", `${view.usage.iterations} / ${view.usage.tool_calls}`);
      pair(
        grid,
        "Token 數",
        `輸入 ${view.usage.input_tokens}／輸出 ${view.usage.output_tokens}`
      );
      pair(grid, "費用", `$${view.usage.cost_usd} USD`);
      pair(grid, "耗時", `${view.usage.elapsed_ms} ms`);
    }
    for (const version of view.versions || []) {
      pair(grid, version.component, version.version);
    }
    for (const issue of view.issues || []) {
      pair(grid, `問題 · ${issue.stage}`, issue.code);
    }
    for (const warning of view.warnings || []) {
      pair(grid, "提醒", warning);
    }
    dom.transparencyWrap.open =
      Boolean((view.issues || []).length || (view.warnings || []).length);
  }
  function pair(parent, term, value) {
    const row = add(parent, "div");
    add(row, "dt", term);
    add(row, "dd", value === null || value === undefined ? "—" : value);
  }
  function stamp(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
      ? "—"
      : `${parsed.toISOString().slice(0, 19).replace("T", " ")}Z`;
  }
  async function loadEvidence(runId, serial) {
    clear(dom.evidence);
    dom.evidenceNote.textContent = "讀取引用資料 …";
    const result = await jsonRequest(
      `/api/v1/research/runs/${encodeURIComponent(runId)}/evidence`,
      { headers: { Accept: "application/json" } }
    );
    if (serial !== state.serial) return;
    if (!result.ok || !result.data) {
      dom.evidenceNote.textContent = `證據投影不可用（${result.code}）`;
      add(dom.evidence, "p", "沒有顯示未驗證或前次 run 的內容。");
      return;
    }
    const items = Array.isArray(result.data.items) ? result.data.items : [];
    dom.evidenceNote.textContent = `${items.length} 筆引用資料`;
    if (!items.length) {
      add(dom.evidence, "p", "本次研究沒有可安全顯示的引用資料。");
      return;
    }
    for (const item of items) renderEvidenceItem(item);
  }
  function renderEvidenceItem(item) {
    const card = add(dom.evidence, "article", null, {
      class: "evidence-card",
      id: `evidence-${item.evidence_id}`,
      "data-evidence-id": item.evidence_id,
    });
    const header = add(card, "header");
    add(header, "h4", `${item.kind} · ${item.source}`);
    add(header, "span", String(item.evidence_id).slice(0, 8));
    const details = add(card, "dl");
    pair(details, "資料來源", item.provider);
    pair(details, "事件時間", stamp(item.event_time));
    pair(details, "可用時間", stamp(item.available_at));
    pair(details, "品質", `${item.quality_status} · ${formatPercent(item.completeness)}`);
    pair(details, "內容雜湊", String(item.content_hash).slice(0, 16));
    for (const field of item.fields || []) pair(details, field.name, field.value);
    for (const warning of item.warnings || []) pair(details, "提醒", warning);
  }
  function focusEvidence(evidenceId) {
    for (const card of dom.evidence.querySelectorAll("[data-evidence-id]")) {
      card.removeAttribute("data-highlighted");
    }
    const card = document.getElementById(`evidence-${evidenceId}`);
    if (!card) return;
    card.setAttribute("data-highlighted", "true");
    card.setAttribute("tabindex", "-1");
    card.scrollIntoView({ block: "center", behavior: "auto" });
    card.focus({ preventScroll: true });
  }
  async function loadHistory() {
    if (!state.runtimeReady) return;
    dom.historyNote.textContent = "讀取中";
    const result = await jsonRequest("/api/v1/research/runs?limit=10", {
      headers: { Accept: "application/json" },
    });
    clear(dom.history);
    if (!result.ok || !result.data) {
      dom.historyNote.textContent = `研究歷史不可用（${result.code}）`;
      return;
    }
    const items = Array.isArray(result.data.items) ? result.data.items : [];
    dom.historyNote.textContent = items.length ? `${items.length} 筆研究紀錄` : "尚無研究紀錄";
    for (const item of items) {
      const row = add(dom.history, "li");
      const button = add(row, "button", null, {
        class: "history-run",
        type: "button",
      });
      const copy = add(button, "span");
      add(copy, "strong", `${item.symbol} · ${statusText(item.status)}`);
      const confidence =
        item.confidence === null || item.confidence === undefined
          ? "信心 —"
          : `信心 ${formatPercent(item.confidence)}`;
      add(
        copy,
        "small",
        `${item.profile} · 資料時間 ${stamp(item.as_of)} · ${confidence}`
      );
      add(
        button,
        "span",
        item.error_code ||
          (item.issue_count ? `${item.issue_count} issues` : item.stage)
      );
      button.addEventListener("click", () => openHistory(item));
    }
  }
  async function openHistory(item) {
    if (!item || !RUN_ID.test(item.run_id || "")) return;
    if (state.source) state.source.close();
    state.serial += 1;
    const serial = state.serial;
    state.runId = item.run_id;
    state.symbol = item.symbol;
    state.terminal = false;
    state.busy = item.status === "queued" || item.status === "running";
    dom.action.disabled = state.busy || !state.ready;
    dom.body.setAttribute("aria-busy", String(state.busy));
    const result = await jsonRequest(
      `/api/v1/research/runs/${encodeURIComponent(item.run_id)}`,
      { headers: { Accept: "application/json" } }
    );
    if (serial !== state.serial) return;
    if (!result.ok || !result.data) {
      finishFailure(item.symbol, result.code, result.message);
      return;
    }
    if (result.data.status === "queued" || result.data.status === "running") {
      renderProgress(result.data.status === "queued" ? 0 : 2, "current");
      dom.note.textContent = `${item.symbol} · ${statusText(result.data.status)}`;
      rail("loading");
      stream(item.symbol, serial);
      return;
    }
    renderDetail(result.data);
    loadEvidence(result.data.run_id, serial);
    finish();
  }
  function renderList(node, items, emptyText) {
    clear(node);
    if (!items.length) {
      add(node, "li", emptyText);
      return;
    }
    for (const item of items) add(node, "li", item);
  }
  function renderSignals(view) {
    clear(dom.signals);
    if (view.kronos_forecast) {
      forecastCard(view.kronos_forecast);
    }
    if (!view.kronos_forecast && !view.kronos_alpha && !view.paper_decision) {
      signalCard("決策界線", "尚未建立模型訊號或模擬決定。");
      return;
    }
    if (view.kronos_alpha || view.paper_decision) {
      renderAuthority(view);
    }
  }
  function renderAuthority(view) {
    const alpha = view.kronos_alpha;
    const shadow =
      alpha &&
      alpha.state === "blocked" &&
      alpha.deployment_state === "shadow" &&
      Number(alpha.weight) === 0;
    const strip = add(dom.signals, "aside", null, {
      class: "authority-strip",
      "aria-label": "交易授權狀態",
    });
    add(strip, "h4", "交易授權狀態");
    add(
      strip,
      "p",
      shadow
        ? "Kronos 目前是觀察策略、模擬交易權重為 0，依設計不會產生下單建議。" +
          "上方預測與研究論點才是本次輸出；以下為授權狀態，不是執行失敗。"
        : "以下為本次研究的訊號授權與模擬決定狀態。"
    );
    if (alpha) alphaCard(strip, alpha);
    if (view.paper_decision) {
      statusRow(strip, "模擬決定", view.paper_decision);
    }
  }
  function forecastCard(forecast) {
    if (forecast.state !== "succeeded") {
      signalCard(
        "Kronos 預測 · 失敗",
        `未產生預測（${forecast.error_code || "unknown"}）`
      );
      return;
    }
    const card = add(dom.signals, "article", null, { class: "signal-card" });
    add(card, "span", "Kronos 預測 · 實際推論結果");
    add(card, "strong", `預期報酬 ${formatPercent(forecast.expected_return)}`);
    add(
      card,
      "p",
      `${forecast.horizon_bars} 根日線 · ${forecast.path_count} 條路徑 · 上漲機率 ${formatPercent(
        forecast.direction_probability
      )} · 中位報酬 ${formatPercent(forecast.median_return)} · 波動 ${formatPercent(
        forecast.expected_volatility
      )}`
    );
    add(
      card,
      "p",
      `下行風險 ${formatPercent(forecast.downside_quantile)} · 最大回撤 ${formatPercent(
        forecast.max_drawdown_quantile
      )}`
    );
    add(
      card,
      "small",
      `${forecast.model_id} @ ${String(forecast.model_revision).slice(0, 12)} · ${
        forecast.quality_status
      } · ${String(forecast.forecast_id).slice(0, 8)} · ${stamp(
        forecast.generated_at
      )}`
    );
    for (const warning of forecast.warnings || []) {
      add(card, "small", `提醒 · ${warning}`);
    }
  }
  function alphaCard(parent, alpha) {
    if (alpha.state === "blocked") {
      statusRow(
        parent,
        `Kronos 訊號 · ${String(alpha.deployment_state).toUpperCase()}`,
        `已封鎖 · 權重 ${alpha.weight} · ${(alpha.reason_codes || []).join(", ")}`
      );
      return;
    }
    statusRow(
      parent,
      `Kronos 訊號 · ${String(alpha.direction).toUpperCase()}`,
      `${formatPercent(alpha.value)} · 信心 ${formatPercent(alpha.confidence)}`
    );
  }
  function statusRow(parent, label, value) {
    const row = add(parent, "div", null, { class: "authority-row" });
    add(row, "span", label);
    add(row, "p", value);
  }
  function formatPercent(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${(number * 100).toFixed(2)}%` : "—";
  }
  function signalCard(label, value) {
    const card = add(dom.signals, "article", null, { class: "signal-card" });
    add(card, "span", label);
    add(card, "p", value);
  }
  function finishFailure(symbol, code, message) {
    state.hasResult = true;
    show("results");
    clear(dom.summary);
    const copy = add(dom.summary, "div");
    add(copy, "h3", "研究未完成");
    add(copy, "p", `${message}（${code}）`);
    clear(dom.claims);
    add(dom.claims, "li", "沒有產生或沿用舊的研究論點。");
    renderList(dom.counters, [], "本次 run 沒有反方觀點。");
    renderList(dom.risks, [], "本次 run 沒有風險結論。");
    clear(dom.signals);
    signalCard("結構化錯誤", code);
    dom.report.textContent = "";
    dom.reportWrap.hidden = true;
    dom.note.textContent = `${symbol} · ${code}`;
    rail("failed");
    finish();
  }
  function finish() {
    state.busy = false;
    dom.action.disabled = !state.ready;
    dom.body.setAttribute("aria-busy", "false");
    dom.results.setAttribute("tabindex", "-1");
    dom.results.focus({ preventScroll: true });
    loadHistory();
    window.dispatchEvent(new CustomEvent("stonks:research-terminal"));
  }
  function summaryTitle(status) {
    if (status === "succeeded") return "研究完成";
    if (status === "degraded") return "研究完成，但部分能力降級";
    if (status === "cancelled") return "研究已取消";
    return "研究未完成";
  }
  function statusText(status) {
    if (status === "succeeded") return "完成";
    if (status === "degraded") return "降級完成";
    if (status === "failed") return "失敗";
    if (status === "cancelled") return "已取消";
    return status;
  }
  window.addEventListener("stonks:capabilities", (event) => configure(event.detail));
  window.addEventListener("stonks:model-settings", (event) =>
    configureModel(event.detail)
  );
  window.addEventListener("stonks:research", (event) => start(event.detail));
  dom.profile.addEventListener("change", () => {
    const options = Array.from(dom.profile.options).map((option) => option.value);
    if (options.includes(dom.profile.value)) state.profile = dom.profile.value;
  });
  dom.historyRefresh.addEventListener("click", loadHistory);
})();
