// Product-only projections. All backend and provider content remains text.
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
  };
  const money = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
  const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

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

  function renderPaper(capabilities) {
    const paper = capabilities && capabilities.paper;
    clear(dom.paperBody);
    if (!paper || paper.state !== "ready" || !paper.portfolio) {
      dom.paperNote.textContent = paper ? paper.state : "unavailable";
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
      dom.paperPanel.setAttribute("data-rail", "idle");
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
    queueMicrotask(() => renderPaper(event.detail));
  });
  window.addEventListener("stonks:market-view", (event) => renderMarket(event.detail));
  dom.servicesRefresh.addEventListener("click", () => {
    window.dispatchEvent(new CustomEvent("stonks:refresh-capabilities"));
  });
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
