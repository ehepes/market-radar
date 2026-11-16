/* Market Radar dashboard.
 *
 * Dependency-free and static: all computation happens in CI, so this page only
 * renders a JSON file. Fast on a phone, cacheable offline, nothing to break.
 *
 * Information-design notes:
 *  - Breadth leads. "0 of 20 up, -1.7% average" is the headline a market page
 *    owes you; the per-instrument list is detail beneath it.
 *  - Numbers wear ink; only the CHANGE wears a status colour, and it always
 *    carries a +/- sign so colour is never the sole encoding.
 *  - RSI is drawn as a meter with its conventional 30/70 zones, because "22.6"
 *    means nothing to the eye and a marker near the floor means everything.
 *  - Sparklines get an area fill and an emphasised endpoint: the end is the
 *    part you are actually reading.
 */

(function () {
  "use strict";

  var LEVEL_ORDER = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
  var state = { data: null, assetFilter: "all", sort: "move", expanded: {} };

  function el(id) { return document.getElementById(id); }

  // ------------------------------------------------------------- formatting

  function fmtNum(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var d = digits === undefined ? 2 : digits;
    // Large prices (BTC) read better without decimals.
    if (d === 2 && Math.abs(v) >= 1000) d = 0;
    return Number(v).toLocaleString("en-IE", { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function fmtPct(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var d = digits === undefined ? 1 : digits;
    return (v >= 0 ? "+" : "") + (v * 100).toFixed(d) + "%";
  }

  function pctClass(v) {
    if (v === null || v === undefined || isNaN(v)) return "";
    return v > 0 ? "up" : v < 0 ? "down" : "";
  }

  function esc(t) {
    return String(t === null || t === undefined ? "" : t)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function timeAgo(iso) {
    var then = Date.parse(iso);
    if (isNaN(then)) return "";
    var mins = Math.round((Date.now() - then) / 60000);
    if (mins < 2) return "just now";
    if (mins < 60) return mins + " min ago";
    var hours = Math.round(mins / 60);
    if (hours < 24) return hours + (hours === 1 ? " hour ago" : " hours ago");
    var days = Math.round(hours / 24);
    return days + (days === 1 ? " day ago" : " days ago");
  }

  // ----------------------------------------------------------------- marks

  function sparkline(points) {
    if (!points || points.length < 2) return "";
    var W = 100, H = 30, step = W / (points.length - 1);
    var xy = points.map(function (p, i) {
      return [i * step, H - (p / 100) * (H - 4) - 2];
    });
    var line = xy.map(function (p, i) {
      return (i === 0 ? "M" : "L") + p[0].toFixed(2) + "," + p[1].toFixed(2);
    }).join(" ");
    var area = line + " L" + W + "," + H + " L0," + H + " Z";
    var rising = points[points.length - 1] >= points[0];
    var c = rising ? "var(--up)" : "var(--down)";
    var last = xy[xy.length - 1];
    return '<svg class="spark" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<path d="' + area + '" fill="' + c + '" fill-opacity="0.13" stroke="none"/>' +
      '<path d="' + line + '" fill="none" stroke="' + c + '" stroke-width="1.5" ' +
      'vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>' +
      '<circle cx="' + last[0].toFixed(2) + '" cy="' + last[1].toFixed(2) + '" r="2.5" ' +
      'fill="' + c + '" vector-effect="non-scaling-stroke"/></svg>';
  }

  function rsiMeter(v) {
    if (v === null || v === undefined || isNaN(v)) return "";
    var pos = Math.max(0, Math.min(100, v));
    var tone = v < 30 ? "low" : v > 70 ? "high" : "mid";
    return '<span class="rsi" title="RSI 14-day: ' + v.toFixed(1) + '">' +
      '<span class="rsi-label">RSI</span>' +
      '<span class="rsi-track">' +
        '<span class="rsi-zone rsi-zone-low"></span>' +
        '<span class="rsi-zone rsi-zone-high"></span>' +
        '<span class="rsi-mark rsi-' + tone + '" style="left:' + pos + '%"></span>' +
      '</span>' +
      '<span class="rsi-value rsi-' + tone + '">' + v.toFixed(0) + "</span></span>";
  }

  function daysUntil(isoDate) {
    if (!isoDate) return null;
    var t = Date.parse(isoDate + "T00:00:00Z");
    if (isNaN(t)) return null;
    // Computed client-side so the badge stays honest even if the published
    // data is a few hours old.
    return Math.round((t - Date.now()) / 86400000);
  }

  function earningsChip(s) {
    var days = daysUntil(s.earnings_date);
    if (days === null || days < 0 || days > 3) return "";
    var label = days === 0 ? "EARNINGS TODAY"
      : days === 1 ? "EARNINGS TMRW"
      : "EARNINGS " + days + "D";
    return '<span class="earn-chip' + (days <= 1 ? " earn-now" : "") + '">' +
      label + "</span>";
  }

  // ------------------------------------------------------------- rendering

  function renderSignals() {
    var d = state.data;
    var signals = (d.signals_now || []).slice().sort(function (a, b) {
      return (LEVEL_ORDER[a.level] || 9) - (LEVEL_ORDER[b.level] || 9);
    });
    var byId = {};
    (d.snapshots || []).forEach(function (s) { byId[s.instrument_id] = s; });

    var strip = el("alert-strip");
    if (!signals.length) {
      el("signals").innerHTML =
        '<div class="empty"><strong>Nothing fired on the last run.</strong><br><br>' +
        "That is the normal state. Rules are tripwires for when something " +
        "<em>changes</em> — the Watchlist tab shows what is currently true.</div>";
      strip.hidden = true;
      return;
    }

    var urgent = signals.filter(function (s) { return s.level === "critical" || s.level === "high"; });
    if (urgent.length) {
      strip.hidden = false;
      strip.textContent = urgent.length + (urgent.length === 1 ? " signal needs" : " signals need") + " your attention";
    } else { strip.hidden = true; }

    el("signals").innerHTML = signals.map(function (sig) {
      var snap = byId[sig.instrument_id];
      var trade = "";
      if (snap && snap.is_proxy) {
        trade = '<div class="trade-line">Measured on <strong>' + esc(snap.signal_symbol) +
          "</strong> · buy <strong>" + esc(snap.trade_label || snap.trade_ticker) + "</strong>" +
          (snap.trade_note ? '<span class="note">' + esc(snap.trade_note) + "</span>" : "") + "</div>";
      }
      return '<article class="card signal level-' + esc(sig.level) + '">' +
        '<div class="card-head"><span class="card-title">' + esc(snap ? snap.name : sig.instrument_id) +
        ' <span class="badge ' + esc(sig.level) + '">' + esc(sig.level) + "</span></span>" +
        (snap ? '<span class="price">' + fmtNum(snap.close) + "</span>" : "") + "</div>" +
        '<div class="card-body"><strong>' + esc(sig.headline) + "</strong><br>" + esc(sig.detail) + "</div>" +
        trade +
        '<div class="card-meta">' + esc(sig.rule_id) + " · as of " +
        esc((sig.evidence && sig.evidence.as_of) || "—") + "</div></article>";
    }).join("");
  }

  function renderBreadth(rows) {
    var withChange = rows.filter(function (s) { return s.change_1d !== null && s.change_1d !== undefined; });
    if (!withChange.length) { el("breadth").innerHTML = ""; return; }

    var up = withChange.filter(function (s) { return s.change_1d > 0; }).length;
    var avg = withChange.reduce(function (a, s) { return a + s.change_1d; }, 0) / withChange.length;
    var sorted = withChange.slice().sort(function (a, b) { return a.change_1d - b.change_1d; });
    var worst = sorted[0], best = sorted[sorted.length - 1];
    var oversold = rows.filter(function (s) { return s.rsi14 !== null && s.rsi14 < 30; }).length;
    var reporting = rows.filter(function (s) {
      var d = daysUntil(s.earnings_date);
      return d !== null && d >= 0 && d <= 5;
    }).length;

    el("breadth").innerHTML = '<div class="breadth">' +
      tile("Up today", up + " / " + withChange.length, up === 0 ? "down" : "") +
      tile("Average", fmtPct(avg), pctClass(avg)) +
      tile("Best", esc(best.name.split(" ")[0]) + " " + fmtPct(best.change_1d), pctClass(best.change_1d)) +
      tile("Weakest", esc(worst.name.split(" ")[0]) + " " + fmtPct(worst.change_1d), pctClass(worst.change_1d)) +
      tile("RSI under 30", String(oversold), oversold > 0 ? "down" : "") +
      tile("Reporting", String(reporting) + (reporting === 1 ? " name" : " names"), reporting > 0 ? "earn" : "") +
      "</div>";
  }

  function tile(label, value, cls) {
    return '<div class="tile"><div class="tile-label">' + esc(label) + '</div>' +
      '<div class="tile-value ' + (cls || "") + '">' + value + "</div></div>";
  }

  var SORTS = {
    move:  function (a, b) { return (a.change_1d || 0) - (b.change_1d || 0); },
    name:  function (a, b) { return String(a.name).localeCompare(String(b.name)); },
    rsi:   function (a, b) { return (a.rsi14 === null ? 999 : a.rsi14) - (b.rsi14 === null ? 999 : b.rsi14); },
    drawdown: function (a, b) { return (a.from_252d_high || 0) - (b.from_252d_high || 0); }
  };

  function renderWatchlist() {
    var d = state.data;
    var sparks = d.sparklines || {};
    var all = d.snapshots || [];
    var rows = all.filter(function (s) {
      return state.assetFilter === "all" || s.asset_class === state.assetFilter;
    }).slice().sort(SORTS[state.sort] || SORTS.move);

    renderBreadth(state.assetFilter === "all" ? all : rows);

    if (!rows.length) {
      el("watchlist").innerHTML = '<div class="empty">No instruments in this group.</div>';
      return;
    }

    el("watchlist").innerHTML = rows.map(function (s) {
      var open = !!state.expanded[s.instrument_id];
      var sub = s.is_proxy
        ? s.signal_symbol + " → buy " + (s.trade_ticker || "—")
        : s.signal_symbol;
      return '<div class="inst' + (open ? " is-open" : "") + '" data-id="' + esc(s.instrument_id) + '">' +
        '<button class="inst-head" type="button" aria-expanded="' + open + '">' +
          '<span class="inst-id"><span class="inst-name">' + esc(s.name) + "</span>" +
            '<span class="inst-sub">' + esc(sub) + "</span></span>" +
          '<span class="inst-spark">' + sparkline(sparks[s.instrument_id]) + "</span>" +
          '<span class="inst-num"><span class="inst-price">' + fmtNum(s.close) + "</span>" +
            '<span class="inst-chg ' + pctClass(s.change_1d) + '">' + fmtPct(s.change_1d) + "</span></span>" +
        "</button>" +
        '<div class="inst-meter">' + rsiMeter(s.rsi14) + earningsChip(s) +
          '<span class="inst-dd">52wk ' + fmtPct(s.from_252d_high) + "</span></div>" +
        (open ? detail(s) : "") +
        "</div>";
    }).join("");

    Array.prototype.forEach.call(document.querySelectorAll(".inst-head"), function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.parentNode.getAttribute("data-id");
        state.expanded[id] = !state.expanded[id];
        renderWatchlist();
      });
    });
  }

  function detail(s) {
    var trendUp = s.sma50 !== null && s.sma200 !== null && s.sma50 > s.sma200;
    var rows = [
      ["1 day", fmtPct(s.change_1d), pctClass(s.change_1d)],
      ["5 days", fmtPct(s.change_5d), pctClass(s.change_5d)],
      ["21 days", fmtPct(s.change_21d), pctClass(s.change_21d)],
      ["50-day avg", fmtNum(s.sma50), ""],
      ["200-day avg", fmtNum(s.sma200), ""],
      ["Trend", s.sma50 === null || s.sma200 === null ? "—" : (trendUp ? "50 above 200" : "50 below 200"), trendUp ? "up" : "down"],
      ["ATR (14)", fmtNum(s.atr14), ""],
      ["Volatility", s.realized_vol_20 === null ? "—" : (s.realized_vol_20 * 100).toFixed(0) + "% ann.", ""],
      ["From 52wk high", fmtPct(s.from_252d_high), pctClass(s.from_252d_high)]
    ];
    if (s.earnings_when) rows.push(["Earnings", esc(s.earnings_when), "earn"]);
    var grid = rows.map(function (r) {
      return '<div class="dt"><span>' + esc(r[0]) + '</span><b class="' + r[2] + '">' + r[1] + "</b></div>";
    }).join("");

    var buy = "";
    if (s.trade_ticker) {
      buy = '<div class="trade-line">Buy as <strong>' + esc(s.trade_ticker) + "</strong>" +
        (s.trade_venue ? " on " + esc(s.trade_venue) : "") +
        (s.trade_isin ? '<span class="note">Search Revolut by ISIN: <b>' + esc(s.trade_isin) + "</b></span>" : "") +
        (s.trade_note ? '<span class="note">' + esc(s.trade_note) + "</span>" : "") + "</div>";
    }
    return '<div class="inst-detail"><div class="dt-grid">' + grid + "</div>" + buy +
      '<div class="card-meta">' + esc(s.signal_symbol) + " · " + esc(s.source || "?") +
      " · as of " + esc(s.as_of) + " · " + esc(s.bars) + " bars</div></div>";
  }

  function renderPositions() {
    var d = state.data, totals = d.totals || {}, positions = d.positions || [];

    if (totals && totals.withheld) {
      var n = totals.positions || 0;
      el("totals").innerHTML = "";
      el("positions").innerHTML = '<div class="empty"><strong>' + n +
        (n === 1 ? " position is" : " positions are") + " being monitored.</strong><br><br>" +
        "Sizes and P/L are withheld because this page is publicly reachable. " +
        "Stop and target alerts still reach your phone.<br><br>" +
        '<span class="inst-sub">Set <code>include_positions = true</code> in config/radar.toml to show them.</span></div>';
      return;
    }
    if (!positions.length) {
      el("totals").innerHTML = "";
      el("positions").innerHTML = '<div class="empty"><strong>No open positions configured.</strong><br><br>' +
        "Add them to the <code>RADAR_PORTFOLIO_TOML</code> secret to get stop and target alerts.</div>";
      return;
    }

    var ccy = totals.currency || "EUR";
    el("totals").innerHTML = '<div class="breadth">' +
      tile("Value", fmtNum(totals.value) + " " + ccy, "") +
      tile("Cost", fmtNum(totals.cost) + " " + ccy, "") +
      tile("P/L", fmtNum(totals.pnl), pctClass(totals.pnl)) +
      tile("P/L %", fmtPct(totals.pnl_pct), pctClass(totals.pnl_pct)) + "</div>";

    el("positions").innerHTML = positions.map(function (p) {
      var toStop = (p.stop && p.price) ? (p.stop / p.price - 1) : null;
      var toTarget = (p.target && p.price) ? (p.target / p.price - 1) : null;
      return '<article class="card"><div class="card-head">' +
        '<span class="card-title">' + esc(p.name) + "</span>" +
        '<span class="price ' + pctClass(p.pnl_pct) + '">' + fmtPct(p.pnl_pct) + "</span></div>" +
        '<div class="card-meta">' + fmtNum(p.quantity, 4) + " units · entry " + fmtNum(p.entry_price) +
        " · now " + fmtNum(p.price) + " · P/L " + fmtNum(p.pnl) + "</div>" +
        '<div class="card-meta">' +
        (p.stop ? "Stop " + fmtNum(p.stop) + " (" + fmtPct(toStop) + " away)" : "No stop set") + " · " +
        (p.target ? "Target " + fmtNum(p.target) + " (" + fmtPct(toTarget) + " away)" : "No target set") +
        (p.trailing_stop_level ? " · Trailing " + fmtNum(p.trailing_stop_level) : "") + "</div>" +
        (p.weight ? '<div class="bar"><span style="width:' + Math.min(100, p.weight * 100).toFixed(1) +
          '%"></span></div><div class="card-meta">' + fmtPct(p.weight, 1) + " of account</div>" : "") +
        "</article>";
    }).join("");
  }

  function renderHistory() {
    var recent = state.data.signals_recent || [];
    if (!recent.length) {
      el("history").innerHTML = '<div class="empty"><strong>No signal history yet.</strong><br><br>' +
        "Signals are recorded here once a live (non-dry) run fires one.</div>";
      return;
    }
    el("history").innerHTML = '<div class="card">' + recent.map(function (s) {
      return '<div class="hist"><div><div class="hist-head">' +
        '<span class="badge ' + esc(s.level) + '">' + esc(s.level) + "</span> " + esc(s.headline) + "</div>" +
        '<div class="inst-sub">' + esc(s.instrument_id) + " · " + esc(s.rule_id) +
        (s.notified ? ' · <span class="pushed">pushed</span>' : " · dashboard only") + "</div></div>" +
        '<div class="hist-when">' + esc(timeAgo(s.fired_at)) + "</div></div>";
    }).join("") + "</div>";
  }

  function renderDiagnostics() {
    var problems = state.data.problems || [];
    var section = el("diagnostics");
    if (!problems.length) { section.hidden = true; return; }
    section.hidden = false;
    el("problem-count").textContent = problems.length;
    el("problems").innerHTML = problems.map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("");
  }

  function render() {
    var d = state.data;
    el("updated").textContent = "Updated " + timeAgo(d.generated_at) + " · " +
      (d.counts ? d.counts.instruments : 0) + " instruments";
    renderSignals(); renderWatchlist(); renderPositions(); renderHistory(); renderDiagnostics();
    el("main").hidden = false;
  }

  // ----------------------------------------------------------- interaction

  function showPanel(name) {
    ["signals", "watchlist", "positions", "history"].forEach(function (p) {
      el("panel-" + p).hidden = p !== name;
    });
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (t) {
      t.classList.toggle("is-active", t.dataset.panel === name);
    });
    try { localStorage.setItem("radar.panel", name); } catch (e) { /* private mode */ }
  }

  function bind() {
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
      tab.addEventListener("click", function () { showPanel(tab.dataset.panel); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".chip"), function (chip) {
      chip.addEventListener("click", function () {
        state.assetFilter = chip.dataset.class;
        Array.prototype.forEach.call(document.querySelectorAll(".chip"), function (c) {
          c.classList.toggle("is-active", c === chip);
        });
        renderWatchlist();
      });
    });
    var sortEl = el("sort");
    if (sortEl) {
      sortEl.addEventListener("change", function () { state.sort = sortEl.value; renderWatchlist(); });
    }
    el("refresh").addEventListener("click", function () { load(true); });
  }

  function load(bust) {
    var url = "data.json" + (bust ? "?t=" + Date.now() : "");
    fetch(url, { cache: bust ? "reload" : "default" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) { state.data = data; el("error").hidden = true; render(); })
      .catch(function (err) {
        var box = el("error");
        box.hidden = false;
        box.textContent = "Could not load data.json (" + err.message +
          "). If this is a fresh deployment, the first run has not published yet.";
      });
  }

  bind();
  try { var saved = localStorage.getItem("radar.panel"); if (saved) showPanel(saved); } catch (e) { /* ignore */ }
  load(false);

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("sw.js").catch(function () { /* optional */ });
    });
  }
})();
