/* Market Radar dashboard.
 *
 * Deliberately dependency-free and static: all computation happens in CI, so
 * this page just renders a JSON file. That makes it fast on a phone, cacheable
 * offline, and impossible to break with a bad CDN.
 */

(function () {
  "use strict";

  var LEVEL_ORDER = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
  var state = { data: null, assetFilter: "all" };

  var el = function (id) { return document.getElementById(id); };

  // ------------------------------------------------------------- formatting

  function fmtNum(value, digits) {
    if (value === null || value === undefined || isNaN(value)) return "—";
    var d = digits === undefined ? 2 : digits;
    return Number(value).toLocaleString("en-IE", {
      minimumFractionDigits: d,
      maximumFractionDigits: d
    });
  }

  function fmtPct(value, digits) {
    if (value === null || value === undefined || isNaN(value)) return "—";
    var d = digits === undefined ? 1 : digits;
    return (value >= 0 ? "+" : "") + (value * 100).toFixed(d) + "%";
  }

  function pctClass(value) {
    if (value === null || value === undefined || isNaN(value)) return "";
    return value > 0 ? "up" : value < 0 ? "down" : "";
  }

  function escapeHtml(text) {
    return String(text === null || text === undefined ? "" : text)
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

  // ------------------------------------------------------------- rendering

  function sparkline(points) {
    if (!points || points.length < 2) return "";
    var width = 100, height = 26, step = width / (points.length - 1);
    var path = points.map(function (p, i) {
      // Values arrive normalised 0-100; invert because SVG y grows downward.
      return (i === 0 ? "M" : "L") + (i * step).toFixed(2) + "," +
        (height - (p / 100) * (height - 2) - 1).toFixed(2);
    }).join(" ");
    var rising = points[points.length - 1] >= points[0];
    var stroke = rising ? "var(--up)" : "var(--down)";
    return '<svg class="spark" viewBox="0 0 ' + width + " " + height +
      '" preserveAspectRatio="none" aria-hidden="true">' +
      '<path d="' + path + '" fill="none" stroke="' + stroke +
      '" stroke-width="1.5" vector-effect="non-scaling-stroke" ' +
      'stroke-linejoin="round" stroke-linecap="round"/></svg>';
  }

  function tradeLine(snap) {
    if (!snap) return "";
    if (snap.is_proxy) {
      return '<div class="trade-line">Measured on <strong>' +
        escapeHtml(snap.signal_symbol) + "</strong> · buy <strong>" +
        escapeHtml(snap.trade_label || snap.trade_ticker) + "</strong>" +
        (snap.trade_note ? '<span class="note">' + escapeHtml(snap.trade_note) + "</span>" : "") +
        "</div>";
    }
    return "";
  }

  function renderSignals() {
    var data = state.data;
    var signals = (data.signals_now || []).slice().sort(function (a, b) {
      return (LEVEL_ORDER[a.level] || 9) - (LEVEL_ORDER[b.level] || 9);
    });
    var byId = {};
    (data.snapshots || []).forEach(function (s) { byId[s.instrument_id] = s; });

    if (!signals.length) {
      el("signals").innerHTML =
        '<div class="empty">Nothing fired on the last run.<br>' +
        "That is the normal state — rules are tripwires, not a feed.</div>";
      el("alert-strip").hidden = true;
      return;
    }

    var urgent = signals.filter(function (s) {
      return s.level === "critical" || s.level === "high";
    });
    var strip = el("alert-strip");
    if (urgent.length) {
      strip.hidden = false;
      strip.textContent = urgent.length +
        (urgent.length === 1 ? " signal needs" : " signals need") + " your attention";
    } else {
      strip.hidden = true;
    }

    el("signals").innerHTML = signals.map(function (sig) {
      var snap = byId[sig.instrument_id];
      var name = snap ? snap.name : sig.instrument_id;
      return '<article class="card signal level-' + escapeHtml(sig.level) + '">' +
        '<div class="card-head"><span class="card-title">' + escapeHtml(name) +
        ' <span class="badge ' + escapeHtml(sig.level) + '">' + escapeHtml(sig.level) +
        "</span></span>" +
        (snap ? '<span class="price">' + fmtNum(snap.close) + "</span>" : "") +
        "</div>" +
        '<div class="card-body"><strong>' + escapeHtml(sig.headline) + "</strong><br>" +
        escapeHtml(sig.detail) + "</div>" +
        tradeLine(snap) +
        '<div class="card-meta">' + escapeHtml(sig.rule_id) + " · as of " +
        escapeHtml((sig.evidence && sig.evidence.as_of) || "—") + "</div>" +
        "</article>";
    }).join("");
  }

  function renderWatchlist() {
    var data = state.data;
    var sparks = data.sparklines || {};
    var rows = (data.snapshots || []).filter(function (s) {
      return state.assetFilter === "all" || s.asset_class === state.assetFilter;
    });

    if (!rows.length) {
      el("watchlist").innerHTML = '<div class="empty">No instruments in this group.</div>';
      return;
    }

    el("watchlist").innerHTML = '<div class="card">' + rows.map(function (s) {
      var sub = s.is_proxy
        ? s.signal_symbol + " → buy " + (s.trade_ticker || "—") +
          (s.trade_venue ? " (" + s.trade_venue + ")" : "")
        : s.signal_symbol;
      return '<div class="row">' +
        '<div><div class="row-name">' + escapeHtml(s.name) + "</div>" +
        '<div class="row-sub">' + escapeHtml(sub) + "</div></div>" +
        '<div class="row-right"><div class="row-price ' + pctClass(s.change_1d) + '">' +
        fmtNum(s.close) + "</div>" +
        '<div class="row-metrics">' + fmtPct(s.change_1d) + " · 21d " +
        fmtPct(s.change_21d) + "<br>RSI " + fmtNum(s.rsi14, 0) +
        " · 52wk " + fmtPct(s.from_252d_high) + "</div></div>" +
        '<div class="row-spark">' + sparkline(sparks[s.instrument_id]) + "</div>" +
        "</div>";
    }).join("") + "</div>";
  }

  function renderPositions() {
    var data = state.data;
    var totals = data.totals || {};
    var positions = data.positions || [];

    // A Pages site is public, so the engine withholds position detail unless
    // the config opts in. Say so plainly rather than looking empty.
    if (totals && totals.withheld) {
      var watched = totals.positions || 0;
      el("totals").innerHTML = "";
      el("positions").innerHTML =
        '<div class="empty">' + watched +
        (watched === 1 ? " position is" : " positions are") + " being monitored.<br><br>" +
        "Sizes and P/L are withheld because this page is publicly reachable. " +
        "Stop and target alerts still reach your phone.<br><br>" +
        '<span class="row-sub">Set <code>include_positions = true</code> in ' +
        "config/radar.toml to show them here.</span></div>";
      return;
    }

    if (!positions.length) {
      el("totals").innerHTML = "";
      el("positions").innerHTML =
        '<div class="empty">No open positions configured.<br>' +
        "Add them to <code>config/portfolio.toml</code> to get stop and target alerts.</div>";
      return;
    }

    var currency = totals.currency || "EUR";
    el("totals").innerHTML = '<div class="totals">' +
      stat("Value", fmtNum(totals.value) + " " + currency) +
      stat("Cost", fmtNum(totals.cost) + " " + currency) +
      stat("P/L", fmtNum(totals.pnl), pctClass(totals.pnl)) +
      stat("P/L %", fmtPct(totals.pnl_pct), pctClass(totals.pnl_pct)) +
      "</div>";

    el("positions").innerHTML = positions.map(function (p) {
      var toStop = (p.stop && p.price) ? (p.stop / p.price - 1) : null;
      var toTarget = (p.target && p.price) ? (p.target / p.price - 1) : null;
      return '<article class="card">' +
        '<div class="card-head"><span class="card-title">' + escapeHtml(p.name) +
        '</span><span class="price ' + pctClass(p.pnl_pct) + '">' +
        fmtPct(p.pnl_pct) + "</span></div>" +
        '<div class="card-meta">' + fmtNum(p.quantity, 4) + " units · entry " +
        fmtNum(p.entry_price) + " · now " + fmtNum(p.price) + " · P/L " +
        fmtNum(p.pnl) + "</div>" +
        '<div class="card-meta">' +
        (p.stop ? "Stop " + fmtNum(p.stop) + " (" + fmtPct(toStop) + " away)" : "No stop set") +
        " · " +
        (p.target ? "Target " + fmtNum(p.target) + " (" + fmtPct(toTarget) + " away)" : "No target set") +
        (p.trailing_stop_level ? " · Trailing " + fmtNum(p.trailing_stop_level) : "") +
        "</div>" +
        (p.weight ? '<div class="bar"><span style="width:' +
          Math.min(100, p.weight * 100).toFixed(1) + '%"></span></div>' +
          '<div class="card-meta">' + fmtPct(p.weight, 1) + " of account</div>" : "") +
        "</article>";
    }).join("");
  }

  function stat(label, value, cls) {
    return '<div class="stat"><div class="stat-label">' + escapeHtml(label) +
      '</div><div class="stat-value ' + (cls || "") + '">' + value + "</div></div>";
  }

  function renderHistory() {
    var recent = state.data.signals_recent || [];
    if (!recent.length) {
      el("history").innerHTML = '<div class="empty">No signal history yet.</div>';
      return;
    }
    el("history").innerHTML = '<div class="card">' + recent.map(function (s) {
      return '<div class="row"><div><div class="row-name">' +
        '<span class="badge ' + escapeHtml(s.level) + '">' + escapeHtml(s.level) +
        "</span> " + escapeHtml(s.headline) + "</div>" +
        '<div class="row-sub">' + escapeHtml(s.instrument_id) + " · " +
        escapeHtml(s.rule_id) + (s.notified ? " · alerted" : " · dashboard only") +
        "</div></div>" +
        '<div class="row-right"><div class="row-metrics">' +
        escapeHtml(timeAgo(s.fired_at)) + "</div></div></div>";
    }).join("") + "</div>";
  }

  function renderDiagnostics() {
    var problems = state.data.problems || [];
    var section = el("diagnostics");
    if (!problems.length) { section.hidden = true; return; }
    section.hidden = false;
    el("problem-count").textContent = problems.length;
    el("problems").innerHTML = problems.map(function (p) {
      return "<li>" + escapeHtml(p) + "</li>";
    }).join("");
  }

  function render() {
    var data = state.data;
    el("updated").textContent = "Updated " + timeAgo(data.generated_at) +
      " · " + (data.generated_at_human || "") +
      " · " + (data.counts ? data.counts.instruments : 0) + " instruments";
    renderSignals();
    renderWatchlist();
    renderPositions();
    renderHistory();
    renderDiagnostics();
    el("main").hidden = false;
  }

  // ----------------------------------------------------------- interaction

  function showPanel(name) {
    ["signals", "watchlist", "positions", "history"].forEach(function (panel) {
      el("panel-" + panel).hidden = panel !== name;
    });
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
      tab.classList.toggle("is-active", tab.dataset.panel === name);
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
    el("refresh").addEventListener("click", function () { load(true); });
  }

  function load(bust) {
    // Cache-bust on explicit reload so a stale service-worker copy cannot
    // make the page look current when it is not.
    var url = "data.json" + (bust ? "?t=" + Date.now() : "");
    fetch(url, { cache: bust ? "reload" : "default" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        state.data = data;
        el("error").hidden = true;
        render();
      })
      .catch(function (err) {
        var box = el("error");
        box.hidden = false;
        box.textContent = "Could not load data.json (" + err.message +
          "). If this is a fresh deployment, the first scheduled run has not " +
          "published data yet.";
      });
  }

  bind();
  try {
    var saved = localStorage.getItem("radar.panel");
    if (saved) showPanel(saved);
  } catch (e) { /* ignore */ }
  load(false);

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("sw.js").catch(function () { /* optional */ });
    });
  }
})();
