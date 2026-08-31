"use strict";
(() => {
  const interval = (id, label, maxLookback, defaultRange, intraday, rangeIds) => Object.freeze({ id, label, maxLookback, defaultRange, intraday, rangeIds });
  const range = (id, label, days) => Object.freeze({ id, label, days });
  const intervals = Object.freeze([
    interval("1m","1m",7,"1w",true),
    interval("2m","2m",21,"1w",true),
    interval("5m","5m",59,"1mo",true),
    interval("15m","15m",59,"1mo",true),
    interval("30m","30m",59,"1mo",true),
    interval("90m","90m",59,"1mo",true),
    interval("1h","1h",366,"3mo",true),
    interval("1d", "日線", 36525, "6mo", false),
    interval("1W", "週線", 36525, "1y", false),
    interval("1M", "月線", 36525, "1y", false),
    interval("1Y", "年線", 36525, "10y", false, "3mo,6mo,ytd,1y,5y,10y,max"),
  ]);
  const now = new Date();
  const daysFrom = (start) => Math.round((now - start) / 864e5) + 1;
  const ranges = Object.freeze([
    range("1d","1日",1), range("5d","5日",5), range("1w","1週",7),
    range("1mo","1月",30), range("3mo","3月",90), range("6mo","6月",180),
    range("ytd", "YTD", daysFrom(new Date(now.getFullYear(), 0, 1))),
    range("1y", "1年", 366),
    range("5y","5年",daysFrom(new Date(now.getFullYear() - 5, now.getMonth(), now.getDate()))),
    range("10y","10年",daysFrom(new Date(now.getFullYear() - 10, now.getMonth(), now.getDate()))),
    range("max","全部",36525),
  ]);
  function intervalOf(id) {
    return intervals.find((item) => item.id === id) || intervals.find((item) => item.id === "1d");
  }
  function rangesFor(interval) {
    const selected = intervalOf(interval);
    return ranges.filter((item) => item.days <= selected.maxLookback && (!selected.rangeIds || selected.rangeIds.includes(item.id)));
  }
  function rangeOf(id, interval) {
    const selected = intervalOf(interval);
    const available = rangesFor(selected.id);
    return (
      available.find((item) => item.id === id) ||
      available.find((item) => item.id === selected.defaultRange) ||
      available[available.length - 1]
    );
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
    RANGES: ranges,
    certainty,
    feedLabel,
    freshnessLabel,
    humanAge,
    intervalOf,
    qualityLabel,
    rangeOf,
    rangesFor,
    request,
    stamp,
  });
})();
