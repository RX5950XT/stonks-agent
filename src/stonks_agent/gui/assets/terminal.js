"use strict";
(() => {
  const {
    AUTO_REFRESH_MS,
    INTERVALS,
    certainty,
    feedLabel,
    freshnessLabel,
    freshnessRatio,
    humanAge,
    intervalOf,
    qualityLabel,
    request,
    stamp,
  } = window.StonksMarketData;
  const DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"];
  const MAX_WATCHLIST = 12;
  const SYMBOL_PATTERN = /^[A-Z0-9][A-Z0-9.-]{0,15}$/;
  const state = {
    symbol: "",
    interval: "1m",
    watchlist: [],
    view: null,
    quotes: new Map(),
    history: [],
    historyIndex: 0,
    hover: null,
    capabilities: null,
    helpReturn: null,
    loading: false,
    loadSerial: 0,
  };
  const el = (id) => document.getElementById(id);
  const dom = {
    marketSearch: el("market-search"),
    symbolSearch: el("symbol-search"),
    services: el("services"),
    quotePanel: el("panel-quote"),
    chartPanel: el("panel-chart"),
    watchPanel: el("panel-watch"),
    provPanel: el("panel-provenance"),
    symbol: el("quote-symbol"),
    price: el("quote-price"),
    delta: el("quote-delta"),
    session: el("quote-session"),
    quoteInterval: el("quote-interval"),
    intervals: el("intervals"),
    canvas: el("chart"),
    crosshair: el("crosshair"),
    watchlist: el("watchlist"),
    watchNote: el("watch-note"),
    provenance: el("provenance"),
    researchAction: el("research-action"),
    watchToggle: el("watch-toggle"),
    log: el("log"),
    prompt: el("prompt"),
    command: el("command"),
    hints: el("hints"),
    help: el("help"),
    helpBody: el("help-body"),
    helpClose: el("help-close"),
    helpTrigger: el("help-trigger"),
  };
  const price = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const compact = new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 2,
  });
  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }
  function add(parent, tag, text, attrs) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (attrs) {
      for (const [name, value] of Object.entries(attrs)) {
        if (value !== null && value !== undefined) node.setAttribute(name, String(value));
      }
    }
    parent.appendChild(node);
    return node;
  }
  function pair(parent, term, value, attrs) {
    const row = add(parent, "div");
    add(row, "dt", term);
    add(row, "dd", value, attrs);
    return row;
  }
  function say(message, tone) {
    dom.log.textContent = message;
    if (tone) dom.log.setAttribute("data-tone", tone);
    else dom.log.removeAttribute("data-tone");
  }
  function setRail(panel, level, ageRatio) {
    panel.setAttribute("data-rail", level);
    const decay = Math.max(0, Math.min(70, Math.round((ageRatio || 0) * 70)));
    panel.style.setProperty("--rail-decay", `${decay}%`);
  }
  function renderPrice(node, value) {
    clear(node);
    const rendered = price.format(Number(value));
    const cut = rendered.lastIndexOf(".");
    add(node, "span", rendered.slice(0, cut));
    add(node, "span", rendered.slice(cut), { class: "cents" });
  }
  function direction(change) {
    if (change === null || change === undefined) return "unknown";
    const value = Number(change);
    if (value > 0) return "up";
    if (value < 0) return "down";
    return "flat";
  }
  function deltaText(view) {
    if (view.change === null || view.change === undefined) {
      return "無前一根可比較";
    }
    const change = Number(view.change);
    const arrow = change > 0 ? "▲" : change < 0 ? "▼" : "■";
    const sign = change > 0 ? "+" : "";
    return `${arrow} ${sign}${price.format(change)}  ${sign}${view.change_percent}%`;
  }
  function renderQuote(view) {
    state.view = view;
    state.loading = false;
    dom.quotePanel.setAttribute("aria-busy", "false");
    dom.chartPanel.setAttribute("aria-busy", "false");
    dom.symbol.textContent = view.symbol;
    dom.quoteInterval.textContent =
      `${view.interval} · ${view.provider} · ${freshnessLabel(view.freshness)} · ` +
      `${view.is_real_time ? "即時" : "非 tick"}`;
    renderPrice(dom.price, view.latest.close);
    dom.delta.textContent = deltaText(view);
    dom.delta.setAttribute("data-direction", direction(view.change));

    clear(dom.session);
    pair(dom.session, "OPEN", price.format(Number(view.latest.open)));
    pair(dom.session, "HIGH", price.format(Number(view.latest.high)));
    pair(dom.session, "LOW", price.format(Number(view.latest.low)));
    pair(dom.session, "VOL", compact.format(Number(view.latest.volume)));
    pair(dom.session, "BARS", view.bars ? view.bars.length : "—");
    pair(dom.session, "AGE", humanAge(view.data_age_seconds));

    const level = certainty(view);
    const ratio = freshnessRatio(view.freshness);
    setRail(dom.quotePanel, level, ratio);
    setRail(dom.chartPanel, level, ratio);
    setRail(dom.provPanel, level, 0);
    renderProvenance(view, level);
    dom.canvas.setAttribute(
      "aria-label",
      `${view.symbol} ${view.interval} 歷史價格與成交量，共 ${view.bars.length} 根`
    );
    window.dispatchEvent(new CustomEvent("stonks:market-view", { detail: view }));
    syncPrimaryActions();
  }
  function renderProvenance(view, level) {
    clear(dom.provenance);
    pair(dom.provenance, "資料來源", view.provider);
    pair(dom.provenance, "資料型態", feedLabel(view.feed_type));
    pair(dom.provenance, "即時報價", view.is_real_time ? "是" : "否");
    pair(dom.provenance, "品質", qualityLabel(view.quality), {
      "data-certainty": level,
    });
    pair(dom.provenance, "新鮮度", freshnessLabel(view.freshness), {
      "data-certainty": level,
    });
    pair(dom.provenance, "Provider 觀測時間", stamp(view.observed_at));
    pair(dom.provenance, "送達時間", stamp(view.served_at));
    pair(dom.provenance, "最新資料事件", stamp(view.latest_event_time));
    pair(dom.provenance, "短期快取", view.cache_hit ? "命中" : "未命中");
    pair(
      dom.provenance,
      "資料年齡",
      level === "stale"
        ? `${humanAge(view.data_age_seconds)} · 超過此週期的預期更新間隔`
        : humanAge(view.data_age_seconds),
      { "data-certainty": level }
    );
    if (view.quality_reasons && view.quality_reasons.length) {
      pair(dom.provenance, "品質原因", view.quality_reasons.join(" · "), {
        "data-certainty": level,
      });
    }
    if (view.warnings && view.warnings.length) {
      pair(dom.provenance, "Provider warnings", view.warnings.join(" · "), {
        "data-certainty": "stale",
      });
    }
  }
  function renderQuoteFailure(symbol, failure) {
    state.view = null;
    state.loading = false;
    dom.quotePanel.setAttribute("aria-busy", "false");
    dom.chartPanel.setAttribute("aria-busy", "false");
    dom.symbol.textContent = symbol;
    clear(dom.price);
    add(dom.price, "span", "無法取得", { class: "price-void" });
    dom.delta.textContent = `${failure.message}（${failure.code}）`;
    dom.delta.setAttribute("data-direction", "unknown");
    clear(dom.session);
    clear(dom.provenance);
    pair(dom.provenance, "狀態", "外部 provider 未回傳資料。系統不會以快取或樣本資料頂替。");
    setRail(dom.quotePanel, "failed", 0);
    setRail(dom.chartPanel, "failed", 0);
    setRail(dom.provPanel, "failed", 0);
    syncPrimaryActions();
    drawChart();
    window.dispatchEvent(new CustomEvent("stonks:market-view", { detail: null }));
  }
  function renderIntervals() {
    clear(dom.intervals);
    for (const item of INTERVALS) {
      const button = add(dom.intervals, "button", item.label, {
        type: "button",
        "aria-pressed": String(item.id === state.interval),
      });
      button.addEventListener("click", () => loadSymbol(state.symbol, item.id));
    }
  }
  function renderWatchlist() {
    clear(dom.watchlist);
    dom.watchNote.textContent = `${state.watchlist.length}/${MAX_WATCHLIST} 檔`;
    let worst = "fresh";
    for (const symbol of state.watchlist) {
      const row = add(dom.watchlist, "li", null, {
        "data-active": String(symbol === state.symbol),
      });
      const button = add(row, "button", symbol, { type: "button" });
      button.addEventListener("click", () => loadSymbol(symbol, state.interval));
      const entry = state.quotes.get(symbol);
      if (!entry) {
        add(row, "span", "···", { class: "watch-price" });
        add(row, "span", "", { class: "watch-delta" });
        continue;
      }
      if (entry.error) {
        add(row, "span", "—", { class: "watch-price" });
        add(row, "span", entry.error.code, {
          class: "watch-delta",
          "data-direction": "failed",
        });
        worst = "failed";
        continue;
      }
      add(row, "span", price.format(Number(entry.quote.latest.close)), {
        class: "watch-price",
      });
      const change = entry.quote.change_percent;
      add(
        row,
        "span",
        change === null || change === undefined ? "—" : `${Number(change) > 0 ? "+" : ""}${change}%`,
        { class: "watch-delta", "data-direction": direction(entry.quote.change) }
      );
      if (worst !== "failed" && certainty(entry.quote) === "stale") worst = "stale";
    }
    setRail(dom.watchPanel, state.quotes.size ? worst : "idle", 0);
    syncPrimaryActions();
  }
  function renderServices() {
    clear(dom.services);
    const services = (state.capabilities && state.capabilities.services) || [];
    for (const service of services) {
      pair(dom.services, service.name, `${service.state} · ${service.detail}`, {
        "data-state": service.state,
      });
    }
  }
  function syncPrimaryActions() {
    dom.symbolSearch.value = state.symbol;
    const followed = state.watchlist.includes(state.symbol);
    dom.watchToggle.textContent = followed ? "移出觀察" : "加入觀察";
    dom.watchToggle.disabled = !SYMBOL_PATTERN.test(state.symbol);
    const research = state.capabilities && state.capabilities.research;
    const model = state.capabilities && state.capabilities.model_settings;
    dom.researchAction.disabled =
      !state.view ||
      !research ||
      research.state !== "ready" ||
      !model ||
      model.state !== "configured" ||
      model.api_key_configured !== true ||
      model.verified !== true;
  }
  function drawChart() {
    const canvas = dom.canvas;
    const frame = canvas.parentElement;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.floor(frame.clientWidth));
    const height = Math.max(1, Math.floor(frame.clientHeight));
    canvas.width = Math.floor(width * ratio);
    canvas.height = Math.floor(height * ratio);
    const context = canvas.getContext("2d");
    if (!context) return;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);

    const style = getComputedStyle(document.documentElement);
    const colors = {
      rule: style.getPropertyValue("--rule").trim() || "#1e2633",
      dim: style.getPropertyValue("--dim").trim() || "#6b7787",
      faint: style.getPropertyValue("--faint").trim() || "#414d5e",
      up: style.getPropertyValue("--up").trim() || "#46d3a0",
      down: style.getPropertyValue("--down").trim() || "#f2555a",
      signal: style.getPropertyValue("--signal").trim() || "#f2a93b",
    };

    const bars = state.view && state.view.bars ? state.view.bars : [];
    if (bars.length < 2) {
      context.fillStyle = colors.faint;
      context.font = "12px ui-monospace, monospace";
      context.textAlign = "center";
      context.fillText(
        state.view ? "資料點不足以繪圖" : "輸入代號後顯示走勢",
        width / 2,
        height / 2
      );
      return;
    }

    const padRight = 58;
    const padBottom = 18;
    const volumeHeight = Math.min(56, Math.max(24, height * 0.2));
    const plotWidth = Math.max(1, width - padRight);
    const plotHeight = Math.max(1, height - padBottom - volumeHeight - 6);

    let low = Infinity;
    let high = -Infinity;
    let peakVolume = 0;
    for (const bar of bars) {
      low = Math.min(low, Number(bar.low));
      high = Math.max(high, Number(bar.high));
      peakVolume = Math.max(peakVolume, Number(bar.volume));
    }
    const span = high - low || 1;
    const pad = span * 0.06;
    low -= pad;
    high += pad;

    const x = (index) => (index * plotWidth) / (bars.length - 1);
    const y = (value) => plotHeight - ((value - low) / (high - low)) * plotHeight;

    context.strokeStyle = colors.rule;
    context.lineWidth = 1;
    context.fillStyle = colors.faint;
    context.font = "10px ui-monospace, monospace";
    context.textAlign = "left";
    for (let step = 0; step <= 4; step += 1) {
      const value = low + ((high - low) * step) / 4;
      const line = Math.round(y(value)) + 0.5;
      context.beginPath();
      context.moveTo(0, line);
      context.lineTo(plotWidth, line);
      context.stroke();
      context.fillText(price.format(value), plotWidth + 6, Math.max(9, line + 3));
    }

    const slot = plotWidth / bars.length;
    const bodyWidth = Math.max(1, Math.min(9, slot * 0.62));
    const thin = slot < 2.2;

    if (thin) {
      context.beginPath();
      bars.forEach((bar, index) => {
        const point = y(Number(bar.close));
        if (index === 0) context.moveTo(x(index), point);
        else context.lineTo(x(index), point);
      });
      context.strokeStyle =
        Number(bars[bars.length - 1].close) >= Number(bars[0].close)
          ? colors.up
          : colors.down;
      context.lineWidth = 1.25;
      context.stroke();
    } else {
      bars.forEach((bar, index) => {
        const open = Number(bar.open);
        const close = Number(bar.close);
        const rising = close >= open;
        const color = rising ? colors.up : colors.down;
        const center = x(index);
        context.strokeStyle = color;
        context.fillStyle = color;
        context.lineWidth = 1;
        context.beginPath();
        context.moveTo(Math.round(center) + 0.5, y(Number(bar.high)));
        context.lineTo(Math.round(center) + 0.5, y(Number(bar.low)));
        context.stroke();
        const top = y(Math.max(open, close));
        const bottom = y(Math.min(open, close));
        context.fillRect(
          center - bodyWidth / 2,
          top,
          bodyWidth,
          Math.max(1, bottom - top)
        );
      });
    }

    const volumeTop = plotHeight + 6;
    bars.forEach((bar, index) => {
      const magnitude = peakVolume ? (Number(bar.volume) / peakVolume) * volumeHeight : 0;
      context.fillStyle = Number(bar.close) >= Number(bar.open) ? colors.up : colors.down;
      context.globalAlpha = 0.32;
      context.fillRect(
        x(index) - Math.max(0.5, bodyWidth / 2),
        volumeTop + volumeHeight - magnitude,
        Math.max(1, bodyWidth),
        magnitude
      );
      context.globalAlpha = 1;
    });

    context.fillStyle = colors.dim;
    context.font = "10px ui-monospace, monospace";
    context.textAlign = "left";
    context.fillText(axisLabel(bars[0]), 0, height - 5);
    context.textAlign = "right";
    context.fillText(axisLabel(bars[bars.length - 1]), plotWidth, height - 5);

    if (state.hover !== null && state.hover >= 0 && state.hover < bars.length) {
      const center = x(state.hover);
      context.strokeStyle = colors.signal;
      context.globalAlpha = 0.55;
      context.beginPath();
      context.moveTo(Math.round(center) + 0.5, 0);
      context.lineTo(Math.round(center) + 0.5, plotHeight + 6 + volumeHeight);
      context.stroke();
      context.globalAlpha = 1;
    }
  }
  function axisLabel(bar) {
    const moment = new Date(bar.event_time);
    if (Number.isNaN(moment.getTime())) return "";
    const iso = moment.toISOString();
    return state.interval === "1d" ? iso.slice(0, 10) : iso.slice(5, 16).replace("T", " ");
  }
  function onChartHover(event) {
    const bars = state.view && state.view.bars ? state.view.bars : [];
    if (bars.length < 2) return;
    const frame = dom.canvas.parentElement;
    const bounds = frame.getBoundingClientRect();
    const plotWidth = Math.max(1, bounds.width - 58);
    const offset = event.clientX - bounds.left;
    if (offset < 0 || offset > plotWidth) {
      state.hover = null;
      dom.crosshair.textContent = "";
      drawChart();
      return;
    }
    const index = Math.round((offset / plotWidth) * (bars.length - 1));
    showChartBar(Math.max(0, Math.min(bars.length - 1, index)));
  }
  function showChartBar(index) {
    const bars = state.view && state.view.bars ? state.view.bars : [];
    if (!bars.length || index < 0 || index >= bars.length) return;
    state.hover = index;
    const bar = bars[state.hover];
    dom.crosshair.textContent =
      `${axisLabel(bar)}  O ${price.format(Number(bar.open))}` +
      `  H ${price.format(Number(bar.high))}  L ${price.format(Number(bar.low))}` +
      `  C ${price.format(Number(bar.close))}  V ${compact.format(Number(bar.volume))}`;
    drawChart();
  }
  function moveChart(step) {
    const bars = state.view && state.view.bars ? state.view.bars : [];
    if (!bars.length) return;
    const current = state.hover === null ? bars.length - 1 : state.hover;
    showChartBar(Math.max(0, Math.min(bars.length - 1, current + step)));
  }
  async function loadSymbol(symbol, interval, quiet) {
    const target = (symbol || "").trim().toUpperCase();
    if (!SYMBOL_PATTERN.test(target)) {
      say("代號格式無效，只接受英數字、點與連字號", "error");
      return;
    }
    if (quiet && state.loading) return;
    const serial = state.loadSerial + 1;
    state.loadSerial = serial;
    state.loading = true;
    state.symbol = target;
    state.interval = intervalOf(interval).id;
    dom.symbolSearch.value = target;
    state.hover = null;
    dom.crosshair.textContent = "";
    renderIntervals();
    renderWatchlist();
    writeHash();
    setRail(dom.quotePanel, "loading", 0);
    setRail(dom.chartPanel, "loading", 0);
    setRail(dom.provPanel, "loading", 0);
    dom.quotePanel.setAttribute("aria-busy", "true");
    dom.chartPanel.setAttribute("aria-busy", "true");
    state.view = null;
    clear(dom.price);
    add(dom.price, "span", "更新中", { class: "price-void" });
    dom.delta.textContent = "等待 provider 回應";
    dom.delta.setAttribute("data-direction", "unknown");
    clear(dom.session);
    clear(dom.provenance);
    pair(dom.provenance, "狀態", "正在讀取外部資料；舊報價已隱藏。");
    drawChart();
    window.dispatchEvent(new CustomEvent("stonks:market-view", { detail: null }));
    syncPrimaryActions();
    dom.quoteInterval.textContent = `${state.interval} · 讀取中`;
    if (!quiet) say(`讀取 ${target} ${state.interval} …`);

    const result = await request("/api/v1/market/bars", {
      symbol: target,
      interval: state.interval,
      lookback_days: intervalOf(state.interval).lookback,
    });
    if (
      serial !== state.loadSerial ||
      state.symbol !== target ||
      state.interval !== intervalOf(interval).id
    ) return;
    if (!result.ok) {
      renderQuoteFailure(target, result);
      say(`${target}：${result.message}（${result.code}）`, "error");
      return;
    }
    renderQuote(result.data);
    drawChart();
    const level = certainty(result.data);
    if (!quiet) {
      say(
        `${target} ${state.interval} · ${result.data.bars.length} 根 · ` +
          `${result.data.provider} · ${freshnessLabel(result.data.freshness)} · ` +
          `資料年齡 ${humanAge(result.data.data_age_seconds)}`,
        level === "stale" ? "warn" : "ok"
      );
    }
    refreshWatchlist();
  }
  function autoRefresh() {
    if (document.visibilityState !== "visible" || state.loading) return;
    if (state.symbol) loadSymbol(state.symbol, state.interval, true);
    else refreshWatchlist();
  }
  async function refreshWatchlist() {
    if (!state.watchlist.length) {
      state.quotes.clear();
      renderWatchlist();
      return;
    }
    const result = await request("/api/v1/market/quotes", {
      symbols: state.watchlist.join(","),
    });
    state.quotes.clear();
    if (!result.ok) {
      for (const symbol of state.watchlist) {
        state.quotes.set(symbol, { error: result });
      }
      renderWatchlist();
      return;
    }
    for (const entry of result.data.quotes) {
      state.quotes.set(
        entry.symbol,
        entry.quote ? { quote: entry.quote } : { error: entry.error }
      );
    }
    renderWatchlist();
  }
  async function loadCapabilities() {
    const result = await request("/api/v1/capabilities", {});
    state.capabilities = result.ok
      ? result.data
      : {
          services: [{ name: "console", detail: result.code, state: "failed" }],
          paper: { state: "unavailable", detail: result.message },
          research: { state: "unavailable", detail: result.message },
          model_settings: { state: "unavailable", detail: result.message },
        };
    window.dispatchEvent(new CustomEvent("stonks:capabilities", { detail: state.capabilities }));
    renderServices();
    syncPrimaryActions();
  }
  const COMMANDS = [
    ["<代號>", "載入報價與走勢，例如 AAPL"],
    ["<代號> <週期>", "指定週期，1m / 5m / 15m / 1h / 1d"],
    ["ADD <代號>", "加入關注清單"],
    ["DROP <代號>", "從關注清單移除"],
    ["RESEARCH <代號>", "啟動已組合的 bounded paper research workflow"],
    ["REFRESH", "重新讀取目前代號與關注清單"],
    ["HELP", "開啟這個說明"],
    ["/", "游標跳到命令列"],
    ["↑ ↓", "瀏覽命令紀錄"],
    ["Esc", "清除命令列或關閉說明"],
  ];
  function runCommand(raw) {
    const input = raw.trim().replace(/\s+/g, " ");
    if (!input) return;
    state.history.push(input);
    state.historyIndex = state.history.length;
    const parts = input.toUpperCase().split(" ");
    const head = parts[0];

    if (head === "HELP" || head === "?") return toggleHelp(true);
    if (head === "RESEARCH") {
      const symbol = parts[1];
      if (!symbol || !SYMBOL_PATTERN.test(symbol)) return say("RESEARCH 需要一個有效代號", "error");
      window.dispatchEvent(new CustomEvent("stonks:research", { detail: { symbol, interval: state.interval } }));
      return;
    }
    if (head === "REFRESH") {
      say("重新讀取 …");
      if (state.symbol) loadSymbol(state.symbol, state.interval);
      else refreshWatchlist();
      return;
    }
    if (head === "ADD" || head === "DROP") {
      const symbol = parts[1];
      if (!symbol || !SYMBOL_PATTERN.test(symbol)) {
        say(`${head} 需要一個有效代號`, "error");
        return;
      }
      if (head === "ADD") {
        if (state.watchlist.includes(symbol)) return say(`${symbol} 已在關注清單`);
        if (state.watchlist.length >= MAX_WATCHLIST) {
          return say(`關注清單上限為 ${MAX_WATCHLIST} 檔`, "error");
        }
        state.watchlist.push(symbol);
        say(`已加入 ${symbol}`, "ok");
      } else {
        state.watchlist = state.watchlist.filter((item) => item !== symbol);
        state.quotes.delete(symbol);
        say(`已移除 ${symbol}`, "ok");
      }
      writeHash();
      renderWatchlist();
      refreshWatchlist();
      return;
    }
    const interval = parts[1] ? intervalOf(parts[1].toLowerCase()).id : state.interval;
    const requested = parts[1] ? parts[1].toLowerCase() : null;
    if (requested && !INTERVALS.some((item) => item.id === requested)) {
      say(`未知的週期 ${parts[1]}，可用：1m 5m 15m 1h 1d`, "error");
      return;
    }
    loadSymbol(head, interval);
  }
  function toggleHelp(open) {
    dom.help.hidden = !open;
    if (open) {
      state.helpReturn = document.activeElement;
      dom.helpClose.focus();
      return;
    }
    const target =
      state.helpReturn && typeof state.helpReturn.focus === "function"
        ? state.helpReturn
        : dom.command;
    state.helpReturn = null;
    target.focus();
  }
  function writeHash() {
    const params = new URLSearchParams();
    if (state.symbol) params.set("s", state.symbol);
    params.set("i", state.interval);
    if (state.watchlist.length) params.set("w", state.watchlist.join(","));
    const next = `#${params.toString()}`;
    if (window.location.hash !== next) {
      window.history.replaceState(null, "", next);
    }
  }
  function readHash() {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const watch = (params.get("w") || "")
      .split(",")
      .map((item) => item.trim().toUpperCase())
      .filter((item) => SYMBOL_PATTERN.test(item))
      .slice(0, MAX_WATCHLIST);
    state.watchlist = watch.length ? watch : DEFAULT_WATCHLIST.slice();
    const interval = params.get("i");
    state.interval = INTERVALS.some((item) => item.id === interval) ? interval : "1m";
    const symbol = (params.get("s") || "").toUpperCase();
    state.symbol = SYMBOL_PATTERN.test(symbol) ? symbol : state.watchlist[0] || "AAPL";
  }
  function bind() {
    dom.marketSearch.addEventListener("submit", (event) => {
      event.preventDefault();
      loadSymbol(dom.symbolSearch.value, state.interval);
    });
    dom.researchAction.addEventListener("click", () => {
      window.dispatchEvent(
        new CustomEvent("stonks:research", {
          detail: { symbol: state.symbol, interval: state.interval },
        })
      );
    });
    dom.watchToggle.addEventListener("click", () => {
      runCommand(`${state.watchlist.includes(state.symbol) ? "DROP" : "ADD"} ${state.symbol}`);
    });
    dom.prompt.addEventListener("submit", (event) => {
      event.preventDefault();
      const value = dom.command.value;
      dom.command.value = "";
      runCommand(value);
    });

    dom.command.addEventListener("keydown", (event) => {
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        if (!state.history.length) return;
        event.preventDefault();
        const step = event.key === "ArrowUp" ? -1 : 1;
        state.historyIndex = Math.max(
          0,
          Math.min(state.history.length, state.historyIndex + step)
        );
        dom.command.value = state.history[state.historyIndex] || "";
      }
      if (event.key === "Escape") dom.command.value = "";
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Tab" && !dom.help.hidden) {
        event.preventDefault();
        dom.helpClose.focus();
        return;
      }
      if (event.key === "Escape" && !dom.help.hidden) {
        event.preventDefault();
        toggleHelp(false);
        return;
      }
      if (event.target === dom.command || event.metaKey || event.ctrlKey) return;
      if (event.key === "/") {
        event.preventDefault();
        window.dispatchEvent(new CustomEvent("stonks:open-command"));
      }
      if (event.key === "?" || event.key === "F1") {
        event.preventDefault();
        toggleHelp(dom.help.hidden);
      }
    });

    dom.helpClose.addEventListener("click", () => toggleHelp(false));
    dom.helpTrigger.addEventListener("click", () => toggleHelp(true));
    dom.canvas.addEventListener("pointermove", onChartHover);
    dom.canvas.addEventListener("pointerleave", () => {
      state.hover = null;
      dom.crosshair.textContent = "";
      drawChart();
    });
    dom.canvas.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      moveChart(event.key === "ArrowLeft" ? -1 : 1);
    });
    window.addEventListener("resize", drawChart);
    window.addEventListener("hashchange", () => {
      readHash();
      loadSymbol(state.symbol, state.interval);
    });
    window.addEventListener("stonks:research-terminal", loadCapabilities);
    window.addEventListener("stonks:refresh-capabilities", loadCapabilities);
    window.addEventListener("stonks:model-settings", syncPrimaryActions);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") autoRefresh();
    });
  }
  function renderStatic() {
    clear(dom.helpBody);
    clear(dom.hints);
    for (const [key, description] of COMMANDS) {
      add(dom.helpBody, "dt", key);
      add(dom.helpBody, "dd", description);
    }
    for (const [key, description] of COMMANDS.slice(0, 4)) {
      const hint = add(dom.hints, "span");
      add(hint, "b", key);
      hint.appendChild(document.createTextNode(` ${description}`));
    }
  }
  function boot() {
    readHash();
    renderStatic();
    renderIntervals();
    renderWatchlist();
    bind();
    syncPrimaryActions();
    say("選擇標的後可直接開始 AI 研究；按 / 使用進階命令列。");
    loadCapabilities();
    loadSymbol(state.symbol, state.interval);
    window.setInterval(autoRefresh, AUTO_REFRESH_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
