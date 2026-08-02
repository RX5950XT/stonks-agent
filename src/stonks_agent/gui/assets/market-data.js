"use strict";
(() => {
  const intervals = Object.freeze([
    Object.freeze({ id: "1m", label: "1m", lookback: 7 }),
    Object.freeze({ id: "5m", label: "5m", lookback: 30 }),
    Object.freeze({ id: "15m", label: "15m", lookback: 59 }),
    Object.freeze({ id: "1h", label: "1h", lookback: 120 }),
    Object.freeze({ id: "1d", label: "1d", lookback: 180 }),
  ]);
  function intervalOf(id) {
    return intervals.find((item) => item.id === id) || intervals[4];
  }
  async function request(path, params) {
    const url = new URL(path, window.location.origin);
    for (const [name, value] of Object.entries(params || {})) {
      url.searchParams.set(name, value);
    }
    let response;
    try {
      response = await fetch(url, {
        headers: { Accept: "application/json" },
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
      });
    } catch {
      return { ok: false, code: "unreachable", message: "無法連線到本機服務" };
    }
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    if (!response.ok || !body || body.success !== true) {
      const failure = (body && body.error) || {};
      return {
        ok: false,
        code: failure.code || String(response.status),
        message: failure.message || "請求失敗",
      };
    }
    return { ok: true, data: body.data, metadata: body.metadata };
  }
  function certainty(view) {
    if (!view) return "idle";
    if (view.quality !== "available") return "stale";
    return view.freshness === "current" ||
      view.freshness === "market_closed" ||
      view.freshness === "delayed"
      ? "fresh"
      : "stale";
  }
  function freshnessLabel(value) {
    return {
      current: "目前可用",
      market_closed: "休市・最近交易時段",
      delayed: "延遲",
      stale: "過期",
      unknown: "無法判定",
    }[value] || "無法判定";
  }
  function qualityLabel(value) {
    return {
      available: "可用",
      degraded: "降級",
      unknown: "未知",
    }[value] || "未知";
  }
  function feedLabel(value) {
    return {
      intraday_historical: "盤中歷史 bar（非交易所 tick）",
      end_of_day_historical: "日線歷史 bar（非交易所 tick）",
    }[value] || value;
  }
  function humanAge(seconds) {
    if (seconds < 60) return `${seconds} 秒`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分`;
    if (seconds < 86400) {
      const hours = Math.floor(seconds / 3600);
      return `${hours} 小時 ${Math.floor((seconds % 3600) / 60)} 分`;
    }
    return `${Math.floor(seconds / 86400)} 天 ${Math.floor((seconds % 86400) / 3600)} 小時`;
  }
  function stamp(iso) {
    const moment = new Date(iso);
    if (Number.isNaN(moment.getTime())) return iso;
    return `${moment.toISOString().slice(0, 19).replace("T", " ")}Z`;
  }
  window.StonksMarketData = Object.freeze({
    AUTO_REFRESH_MS: 30_000,
    INTERVALS: intervals,
    certainty,
    feedLabel,
    freshnessLabel,
    humanAge,
    intervalOf,
    qualityLabel,
    request,
    stamp,
  });
})();
