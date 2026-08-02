"use strict";
(() => {
  const el = (id) => document.getElementById(id);
  const dom = {
    paperPanel: el("panel-paper"),
    paperBody: el("paper-body"),
    paperNote: el("paper-note"),
    marketBody: el("market-table-body"),
    marketNote: el("market-table-note"),
    servicesRefresh: el("services-refresh"),
    expert: el("expert-disclosure"),
    command: el("command"),
    capabilityMarket: el("capability-market-state"),
    capabilityResearch: el("capability-research-state"),
    capabilityModel: el("capability-model-state"),
    capabilityKronos: el("capability-kronos-state"),
    capabilityPaper: el("capability-paper-state"),
    capabilityData: el("capability-data-state"),
    runtimeReadiness: el("runtime-readiness"),
  };
  const money = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
  const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const workspaceLinks = document.querySelectorAll("[data-workspace-target]");
  let initialAnchorState = window.location.hash ? 0 : -1;
  function syncWorkspaceCurrent() {
    const target = window.location.hash.slice(1);
    let current = null;
    for (const link of workspaceLinks) {
      link.removeAttribute("aria-current");
      if (!current && link.dataset.workspaceTarget === target) current = link;
    }
    if (current) current.setAttribute("aria-current", "page");
  }
  function settleInitialAnchor(signal) {
    if (initialAnchorState < 0) return;
    initialAnchorState |= signal;
    if (initialAnchorState !== 3) return;
    const target = document.getElementById(window.location.hash.slice(1));
    initialAnchorState = -1;
    if (target) requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  }
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
  function pair(parent, term, value) {
    const row = add(parent, "div");
    add(row, "dt", term);
    add(row, "dd", value);
    return row;
  }
  function metric(parent, label, value) {
    const card = add(parent, "div", null, { class: "metric-card" });
    add(card, "span", label);
    add(card, "strong", value);
  }
  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? money.format(parsed) : "—";
  }
  function stamp(value) {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
      ? "—"
      : `${parsed.toISOString().slice(0, 19).replace("T", " ")}Z`;
  }
  function capability(node, label, state) {
    node.textContent = label;
    node.setAttribute("data-state", state || "unavailable");
  }
  function stateLabel(value) {
    return {
      ready: "可用",
      configured: "已設定",
      unconfigured: "需要設定",
      absent: "未組合",
      degraded: "降級",
      unknown: "未知",
      failed: "失敗",
      unavailable: "不可用",
    }[value] || value || "不可用";
  }
  function renderCapabilityMap(capabilities) {
    const services = new Map(
      ((capabilities && capabilities.services) || []).map((item) => [item.name, item])
    );
    const research = capabilities && capabilities.research;
    const model = capabilities && capabilities.model_settings;
    const paper = capabilities && capabilities.paper;
    const kronos = services.get("kronos");
    const researchService = services.get("research");
    const researchReady = Boolean(
      research &&
        research.state === "ready" &&
        researchService &&
        researchService.state === "ready"
    );
    const researchState = researchReady
      ? "ready"
      : research && research.state !== "ready"
        ? research.state
        : (researchService && researchService.state) || "unavailable";
    const modelReady = Boolean(
      model &&
        model.state === "configured" &&
        model.api_key_configured === true &&
        model.verified === true
    );
    const modelState = modelReady
      ? "ready"
      : model && model.state === "configured"
        ? "failed"
        : (model && model.state) || "unavailable";
    const failed =
      modelState === "failed" || [...services.values()].some((item) => item.state === "failed");
    const runtimeState = failed
      ? "failed"
      : modelReady && researchReady
        ? "ready"
        : modelState === "unconfigured"
          ? "unconfigured"
          : "unavailable";
    dom.runtimeReadiness.textContent = failed
      ? "後端能力降級"
      : modelReady && researchReady
        ? "研究功能就緒"
        : modelState === "unconfigured"
          ? "需要設定模型"
          : "研究服務不可用";
    dom.runtimeReadiness.setAttribute("data-state", runtimeState);
    dom.runtimeReadiness.closest(".environment").dataset.state = runtimeState;
    capability(dom.capabilityResearch, stateLabel(researchState), researchState);
    capability(
      dom.capabilityModel,
      modelReady ? "已驗證" : modelState === "failed" ? "尚未驗證" : stateLabel(modelState),
      modelState
    );
    capability(dom.capabilityKronos, stateLabel(kronos && kronos.state), kronos && kronos.state);
    const stopped = paper && paper.safety && paper.safety.active === true;
    const integrity = paper && paper.integrity;
    capability(
      dom.capabilityPaper,
      stopped
        ? "Kill switch 已啟用"
        : integrity && integrity.state === "verified"
          ? "可用 · 投影已驗證"
          : stateLabel(paper && paper.state),
      stopped ? "failed" : paper && paper.state
    );
  }
  function renderMarketCapabilities(view) {
    if (!view) {
      capability(dom.capabilityMarket, "等待行情", "loading");
      capability(dom.capabilityData, "等待行情", "loading");
      return;
    }
    capability(dom.capabilityMarket, `${view.symbol} · ${view.interval}`, "ready");
    capability(
      dom.capabilityData,
      `${view.provider} · ${view.quality === "available" ? "" : `${stateLabel(view.quality)} · `}${view.is_real_time ? "即時" : "非 tick"}`,
      view.quality === "available" ? "ready" : view.quality
    );
  }
  function renderMarketFailure(failure) {
    capability(dom.capabilityMarket, failure.code || "data_unavailable", "failed");
    capability(dom.capabilityData, "Provider 失敗", "failed");
  }
  function renderPaper(capabilities) {
    const paper = capabilities && capabilities.paper;
    clear(dom.paperBody);
    if (!paper || paper.state !== "ready" || !paper.portfolio) {
      dom.paperNote.textContent = stateLabel(paper && paper.state);
      add(
        dom.paperBody,
        "p",
        (paper && paper.detail) ||
          "此工作階段沒有連上 PostgreSQL，Paper 投資組合不可用。",
        { class: "notice" }
      );
      add(
        dom.paperBody,
        "p",
        "沒有真實 canonical projection 就不顯示示範數字。",
        { class: "paper-empty" }
      );
      dom.paperPanel.setAttribute("data-rail", paper && paper.state === "failed" ? "failed" : "stale");
      return;
    }
    const safety = paper.safety || { state: "unavailable" };
    const stopped = safety.state === "available" && safety.active === true;
    dom.paperNote.textContent = stopped
      ? "KILL SWITCH ACTIVE"
      : paper.account_id || "paper";
    dom.paperPanel.setAttribute("data-rail", stopped ? "failed" : "fresh");
    const root = add(dom.paperBody, "div", null, { class: "paper-product" });
    const overview = add(root, "div", null, { class: "paper-overview" });
    const nav = paper.nav || { state: "empty" };
    const cash = paper.portfolio.cash || [];
    const base = paper.portfolio.base_currency || "";
    metric(
      overview,
      "NAV",
      nav.state === "available" ? `${number(nav.nav)} ${base}` : "尚未估值"
    );
    metric(
      overview,
      "可用現金",
      cash.length ? `${number(cash[0].available)} ${cash[0].currency}` : "—"
    );
    metric(
      overview,
      "持倉 / 未結訂單",
      `${integer.format(paper.portfolio.position_count || 0)} / ${integer.format(
        paper.portfolio.pending_order_count || 0
      )}`
    );
    metric(
      overview,
      "交易安全",
      safety.state !== "available"
        ? "狀態不可用"
        : stopped
          ? `已停止 · ${safety.reason_code}`
          : "Kill switch 未啟用"
    );
    metric(
      overview,
      "Latest target",
      paper.portfolio.latest_target ? "已建立" : "尚無 target"
    );
    renderNav(root, nav, base);
    renderCash(root, cash);
    renderPositions(root, paper.portfolio);
    renderRisk(root, paper.risk);
    renderSafety(root, safety);
    renderIntegrity(root, paper.integrity, paper.portfolio.as_of);
  }
  function renderNav(root, nav, base) {
    const node = section(root, "ACCOUNT VALUE", "NAV 拆解");
    if (!nav || nav.state !== "available") {
      add(
        node,
        "p",
        nav && nav.state === "unavailable"
          ? `NAV 無法讀取（${nav.error_code || "unknown"}）。`
          : "尚無可驗證估值。",
        { class: "paper-empty" }
      );
      return;
    }
    const list = add(node, "dl");
    pair(list, "As of", stamp(nav.as_of));
    pair(list, "Cash value", `${number(nav.cash_value)} ${base}`);
    pair(list, "Position value", `${number(nav.position_value)} ${base}`);
    pair(list, "Cumulative fees", `${number(nav.cumulative_fees)} ${base}`);
    pair(list, "Realized P&L", `${number(nav.realized_pnl)} ${base}`);
  }
  function section(parent, label, title) {
    const node = add(parent, "section", null, { class: "paper-section" });
    add(node, "span", label);
    add(node, "h3", title);
    return node;
  }
  function renderCash(root, balances) {
    const node = section(root, "ACCOUNT BALANCES", "現金與保留額");
    if (!balances.length) {
      add(node, "p", "尚無現金投影。", { class: "paper-empty" });
      return;
    }
    const list = add(node, "dl");
    for (const balance of balances) {
      pair(list, `${balance.currency} settled`, number(balance.settled));
      pair(list, `${balance.currency} reserved`, number(balance.reserved));
      pair(list, `${balance.currency} available`, number(balance.available));
    }
  }
  function renderPositions(root, portfolio) {
    const node = section(root, "CANONICAL HOLDINGS", "持倉");
    const positions = portfolio.positions || [];
    if (!positions.length) {
      add(
        node,
        "p",
        "目前沒有持倉。Kronos 仍為 shadow，系統不會為展示而建立訂單。",
        { class: "paper-empty" }
      );
      return;
    }
    const list = add(node, "dl");
    for (const position of positions) {
      pair(list, String(position.instrument_id).slice(0, 12), number(position.quantity));
      pair(list, "sellable", number(position.sellable));
      pair(list, "reserved", number(position.reserved));
      pair(list, "available", number(position.available));
    }
  }
  function renderRisk(root, risk) {
    const node = section(root, "RISK AUTHORITY", "Risk decision");
    if (!risk || risk.state === "empty") {
      add(node, "p", "尚無 risk decision；沒有任何交易授權。", {
        class: "paper-empty",
      });
      return;
    }
    if (risk.state !== "available") {
      add(node, "p", `Risk projection 無法讀取（${risk.error_code || "unknown"}）。`, {
        class: "paper-empty",
      });
      return;
    }
    const list = add(node, "dl");
    pair(list, "Approved", risk.approved ? "是" : "否");
    pair(list, "Current authority", risk.currently_authorized ? "有效" : "無效");
    pair(list, "Policy", risk.policy_version || "—");
    pair(list, "Decided", stamp(risk.decided_at));
    pair(list, "Expires", stamp(risk.expires_at));
    if (risk.failed_checks && risk.failed_checks.length) {
      pair(list, "Failed checks", risk.failed_checks.join(" · "));
    }
  }
  function renderSafety(root, safety) {
    const node = section(root, "SAFETY CENTER", "Global kill switch");
    if (!safety || safety.state !== "available") {
      add(node, "p", `安全狀態無法讀取（${safety && safety.error_code || "unknown"}）。`, {
        class: "paper-empty",
      });
      return;
    }
    const list = add(node, "dl");
    pair(list, "Active", safety.active ? "是：交易已停止" : "否");
    pair(list, "Reason", safety.reason_code || "—");
    pair(list, "Version", safety.version);
    pair(list, "Updated", stamp(safety.updated_at));
  }
  function renderIntegrity(root, integrity, asOf) {
    const node = section(root, "INTEGRITY", "Canonical projection");
    if (!integrity) {
      add(node, "p", "完整性投影不可用。", { class: "paper-empty" });
      return;
    }
    const list = add(node, "dl");
    pair(list, "State", "已驗證 content hash");
    pair(list, "As of", stamp(asOf));
    pair(list, "Account sequence", integrity.account_sequence);
    pair(list, "Portfolio sequence", integrity.portfolio_sequence);
    pair(list, "Ledger sequence", integrity.ledger_sequence);
    pair(
      list,
      "Ledger hash",
      integrity.ledger_hash ? String(integrity.ledger_hash).slice(0, 16) : "尚無 journal"
    );
    pair(list, "Projection hash", String(integrity.projection_hash).slice(0, 16));
  }
  function renderMarket(view) {
    clear(dom.marketBody);
    const bars = view && Array.isArray(view.bars) ? view.bars : [];
    if (!bars.length) {
      dom.marketNote.textContent = "本次沒有可顯示的市場資料。";
      return;
    }
    dom.marketNote.textContent = `${view.symbol} ${view.interval} · ${bars.length} 根 · ${view.provider}`;
    for (const bar of bars.slice(-180).reverse()) {
      const row = add(dom.marketBody, "tr");
      add(row, "td", stamp(bar.event_time));
      add(row, "td", number(bar.open));
      add(row, "td", number(bar.high));
      add(row, "td", number(bar.low));
      add(row, "td", number(bar.close));
      add(row, "td", integer.format(Number(bar.volume)));
    }
  }
  window.addEventListener("stonks:capabilities", (event) => {
    queueMicrotask(() => {
      renderCapabilityMap(event.detail);
      renderPaper(event.detail);
      settleInitialAnchor(1);
    });
  });
  window.addEventListener("stonks:market-view", (event) => {
    renderMarketCapabilities(event.detail);
    renderMarket(event.detail);
    if (event.detail) settleInitialAnchor(2);
  });
  window.addEventListener("stonks:market-failure", (event) => {
    renderMarketFailure(event.detail);
    settleInitialAnchor(2);
  });
  dom.servicesRefresh.addEventListener("click", () => {
    window.dispatchEvent(new CustomEvent("stonks:refresh-capabilities"));
  });
  for (const link of workspaceLinks) {
    link.addEventListener("click", () => {
      for (const item of workspaceLinks) item.removeAttribute("aria-current");
      link.setAttribute("aria-current", "page");
    });
  }
  window.addEventListener("hashchange", syncWorkspaceCurrent);
  syncWorkspaceCurrent();
  window.addEventListener("stonks:open-command", () => {
    dom.expert.open = true;
    dom.command.focus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !dom.expert.open) return;
    dom.expert.open = false;
    dom.expert.querySelector("summary").focus();
  });
})();
