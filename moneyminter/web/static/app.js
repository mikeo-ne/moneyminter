const $ = (id) => document.getElementById(id);
const fmt = (n, d = 2) => Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const money = (n) => (n < 0 ? "-$" : "$") + fmt(Math.abs(n));
const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "muted");
const pct = (n, d = 2) => `${n > 0 ? "+" : ""}${fmt(n, d)}%`;

let selectedPairs = new Set(["EUR/USD", "GBP/USD", "USD/JPY"]);
let demoPairs = new Set(["EUR/USD", "GBP/USD"]);
let curve = [];
let demoCurve = [];
let meta = null;

/* ---------------------------------------------------------------- tabs */
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("panel-" + t.dataset.panel).classList.add("active");
  };
});

/* ----------------------------------------------------------------- meta */
async function loadMeta() {
  meta = await (await fetch("/api/meta")).json();
  const opts = (sel, items, def) => {
    sel.innerHTML = items.map(([k, v]) =>
      `<option value="${k}" ${k === def ? "selected" : ""}>${v}</option>`).join("");
  };
  const strategies = Object.entries(meta.strategies).map(([k, v]) => [k, `${k} — ${v.style}`]);
  opts($("strategy"), strategies, "trend_pullback");
  opts($("dStrategy"), strategies, "trend_pullback");
  opts($("rStrategy"), strategies, "trend_pullback");
  const tfs = meta.timeframes.map((t) => [t, t]);
  opts($("timeframe"), tfs, "M5");
  opts($("dTimeframe"), tfs, "M5");
  opts($("rTimeframe"), tfs, "H1");

  const chips = (el, set) => {
    el.innerHTML = meta.instruments.map((s) =>
      `<span class="chip ${set.has(s) ? "on" : ""}" data-s="${s}">${s}</span>`).join("");
    el.querySelectorAll(".chip").forEach((c) => {
      c.onclick = () => {
        const s = c.dataset.s;
        set.has(s) ? set.delete(s) : set.add(s);
        c.classList.toggle("on");
      };
    });
  };
  chips($("pairs"), selectedPairs);
  chips($("dPairs"), demoPairs);

  if (meta.validation && meta.validation.reports && meta.validation.reports.length) {
    $("vOut").innerHTML = `<div class="muted" style="margin-bottom:8px">Last full scan
      (${(meta.validation.generated_at || "").slice(0, 16).replace("T", " ")})</div>` +
      renderValidationTable(meta.validation.reports);
  }
}

/* --------------------------------------------------------------- charts */
function drawChart(cvId, points, height, floor) {
  const cv = $(cvId), ctx = cv.getContext("2d");
  const w = cv.width = cv.clientWidth * devicePixelRatio;
  const h = cv.height = height * devicePixelRatio;
  ctx.clearRect(0, 0, w, h);
  if (!points || points.length < 2) {
    ctx.fillStyle = "#8497b5";
    ctx.font = `${12 * devicePixelRatio}px system-ui`;
    ctx.fillText("waiting for data…", 16, h / 2);
    return;
  }
  const ys = points.map((p) => p.equity);
  let min = Math.min(...ys), max = Math.max(...ys);
  if (max - min < 1e-9) { max += 1; min -= 1; }
  const pad = (max - min) * 0.12; min -= pad; max += pad;
  const X = (i) => (i / (points.length - 1)) * (w - 60) + 50;
  const Y = (v) => h - 24 - ((v - min) / (max - min)) * (h - 50);

  ctx.strokeStyle = "rgba(255,255,255,.07)"; ctx.fillStyle = "#8497b5";
  ctx.font = `${11 * devicePixelRatio}px system-ui`; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = min + (max - min) * i / 4, y = Y(v);
    ctx.beginPath(); ctx.moveTo(50, y); ctx.lineTo(w - 8, y); ctx.stroke();
    ctx.fillText(Math.round(v).toLocaleString(), 4, y + 4);
  }
  const start = points[0].equity, end = points[points.length - 1].equity;
  const color = end >= start ? "#25d07d" : "#ff5f6d";
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + "55"); grad.addColorStop(1, color + "00");
  ctx.beginPath(); ctx.moveTo(X(0), Y(points[0].equity));
  points.forEach((p, i) => ctx.lineTo(X(i), Y(p.equity)));
  ctx.strokeStyle = color; ctx.lineWidth = 2 * devicePixelRatio; ctx.stroke();
  ctx.lineTo(X(points.length - 1), h - 24); ctx.lineTo(X(0), h - 24); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
}

/* ----------------------------------------------------------- paper panel */
function render(s) {
  $("dot").className = "dot " + (s.running ? "live" : "off");
  $("statusText").textContent = s.running
    ? `live · ${s.config.strategy} · ${s.config.symbols.join(", ")} · ${s.config.timeframe} ×${s.config.speed}`
    : "stopped";
  $("equity").textContent = money(s.equity);
  $("pnl").innerHTML = `<span class="${cls(s.pnl)}">${money(s.pnl)} (${pct(s.pnl_pct)})</span>`;
  $("openCount").textContent = s.open_positions.length;
  $("tradeCount").textContent = `${s.stats.trades} (${s.stats.wins}W/${s.stats.losses}L)`;
  $("winRate").textContent = fmt(s.stats.win_rate, 1) + "%";
  $("dd").innerHTML = `<span class="${s.stats.drawdown_pct > 0 ? "down" : "muted"}">${fmt(s.stats.drawdown_pct)}%</span>`;

  curve = s.equity_curve;
  drawChart("equityChart", curve, 230);

  $("pricesTable").querySelector("tbody").innerHTML = Object.entries(s.prices)
    .map(([k, v]) => `<tr><td>${k}</td><td style="text-align:right">${v}</td></tr>`).join("");

  $("positions").querySelector("tbody").innerHTML = s.open_positions.length
    ? s.open_positions.map((p) => `<tr>
        <td>${p.symbol}</td><td><span class="badge ${p.side}">${p.side}</span></td>
        <td>${fmt(p.units, 0)}</td><td>${fmt(p.entry_price, 5)}</td><td>${fmt(p.current_price, 5)}</td>
        <td class="${cls(p.pips)}">${fmt(p.pips, 1)}</td>
        <td class="${cls(p.unrealized)}">${money(p.unrealized)}</td>
        <td class="muted">${p.stop_loss ? fmt(p.stop_loss, 5) : "–"} / ${p.take_profit ? fmt(p.take_profit, 5) : "–"}</td>
        <td><button class="xbtn" onclick="closePos('${p.id}')">close</button></td></tr>`).join("")
    : `<tr><td colspan="9" class="muted">No open positions.</td></tr>`;

  $("trades").querySelector("tbody").innerHTML = s.trades.length
    ? s.trades.slice(0, 40).map((t) => `<tr>
        <td class="muted">${t.closed_at.slice(5, 16).replace("T", " ")}</td><td>${t.symbol}</td>
        <td><span class="badge ${t.side}">${t.side}</span></td><td>${fmt(t.units, 0)}</td>
        <td>${fmt(t.entry_price, 5)}</td><td>${fmt(t.exit_price, 5)}</td>
        <td class="${cls(t.pnl)}">${money(t.pnl)}</td><td class="muted">${t.reason}</td></tr>`).join("")
    : `<tr><td colspan="8" class="muted">No closed trades yet.</td></tr>`;

  $("events").innerHTML = s.events.map((e) =>
    `<li><time>${e.ts.slice(11, 19)}</time><span class="k-${e.kind}">${e.message}</span></li>`).join("");
}

async function closePos(id) { await fetch("/api/close/" + id, { method: "POST" }); }
window.closePos = closePos;

$("startBtn").onclick = async () => {
  $("startBtn").disabled = true;
  await fetch("/api/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbols: [...selectedPairs], timeframe: $("timeframe").value,
      strategy: $("strategy").value, balance: +$("balance").value,
      speed: +$("speed").value, risk_per_trade: +$("risk").value / 100,
    }),
  });
  $("startBtn").disabled = false;
};
$("stopBtn").onclick = () => fetch("/api/stop", { method: "POST" });

/* ------------------------------------------------------------ demo panel */
function renderDemo(s) {
  $("dMode").textContent = s.running ? (s.mode || "demo") : "idle";
  if (s.equity == null) return;
  $("dEquity").textContent = money(s.equity);
  const st = s.stats || {};
  $("dPnl").innerHTML = `<span class="${cls(st.total_pnl || 0)}">${money(st.total_pnl || 0)}</span>`;
  $("dTrades").textContent = `${st.trades || 0} (${st.wins || 0}W/${st.losses || 0}L)`;
  $("dWinRate").textContent = fmt(st.win_rate || 0, 1) + "%";
  $("dAvgR").innerHTML = `<span class="${cls(st.avg_r || 0)}">${(st.avg_r || 0).toFixed(2)}R</span>`;

  demoCurve = s.equity_curve || [];
  drawChart("demoChart", demoCurve, 200);

  $("dPositions").querySelector("tbody").innerHTML = (s.positions || []).length
    ? s.positions.map((p) => `<tr>
        <td>${p.symbol}</td><td><span class="badge ${p.side}">${p.side}</span></td>
        <td>${fmt(p.units, 0)}</td><td>${fmt(p.entry_price, 5)}</td>
        <td>${fmt(p.current_price, 5)}</td>
        <td class="${cls(p.unrealized)}">${money(p.unrealized)}</td>
        <td>${p.r_multiple == null ? "–" : p.r_multiple.toFixed(2) + "R"}</td>
        <td class="muted">${p.stop_loss ? fmt(p.stop_loss, 5) : "–"} / ${p.take_profit ? fmt(p.take_profit, 5) : "–"}</td>
      </tr>`).join("")
    : `<tr><td colspan="8" class="muted">No open positions.</td></tr>`;

  $("dTradesTable").querySelector("tbody").innerHTML = (s.trades || []).length
    ? s.trades.slice(0, 40).map((t) => `<tr>
        <td class="muted">${t.closed_at.slice(5, 16).replace("T", " ")}</td><td>${t.symbol}</td>
        <td><span class="badge ${t.side}">${t.side}</span></td>
        <td>${fmt(t.entry_price, 5)}</td><td>${fmt(t.exit_price, 5)}</td>
        <td class="${cls(t.pnl)}">${money(t.pnl)}</td>
        <td>${t.r_multiple == null ? "–" : t.r_multiple.toFixed(2) + "R"}</td>
        <td class="muted">${t.reason}</td></tr>`).join("")
    : `<tr><td colspan="8" class="muted">No trades yet.</td></tr>`;

  $("dEvents").innerHTML = (s.events || []).map((e) =>
    `<li><time>${e.ts.slice(11, 19)}</time><span class="k-${e.kind}">${e.message}</span></li>`).join("");
}

$("dStart").onclick = async () => {
  $("dStart").disabled = true;
  $("dMode").textContent = "starting…";
  await fetch("/api/demo/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbols: [...demoPairs], timeframe: $("dTimeframe").value,
      strategy: $("dStrategy").value, balance: +$("dBalance").value,
      risk_per_trade: +$("dRisk").value / 100,
      bars_per_poll: +$("dBarsPerPoll").value,
    }),
  });
  $("dStart").disabled = false;
};
$("dStop").onclick = () => fetch("/api/demo/stop", { method: "POST" });

/* -------------------------------------------------------- research panel */
const metricRows = (m) => [
  ["Net profit", `<span class="${cls(m.net_profit)}">${money(m.net_profit)} (${pct(m.return_pct)})</span>`],
  ["Trades", `${m.trades} (${m.wins}W/${m.losses}L)`],
  ["Win rate", fmt(m.win_rate) + "%"],
  ["Profit factor", fmt(m.profit_factor)],
  ["Expectancy", money(m.expectancy)],
  ["Max drawdown", fmt(m.max_drawdown_pct) + "%"],
  ["Sharpe", fmt(m.sharpe)],
  ["Sortino", fmt(m.sortino)],
  ["Exposure", fmt(m.exposure_pct, 1) + "%"],
];

$("btBtn").onclick = async () => {
  $("btOut").innerHTML = "Running backtest…";
  const r = await (await fetch("/api/backtest", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol: [...selectedPairs][0] || "EUR/USD", timeframe: $("rTimeframe").value,
      strategy: $("rStrategy").value, balance: +$("balance").value,
      risk_per_trade: +$("risk").value / 100, bars: +$("rBars").value,
    }),
  })).json();
  $("btOut").innerHTML = `<div class="muted" style="margin-bottom:8px">${r.strategy} ·
    ${r.symbol} ${r.timeframe}</div>
    <div class="metrics">${metricRows(r.metrics).map(([a, b]) => `<div>${a}</div><div>${b}</div>`).join("")}</div>`;
};

function verdictClass(v) {
  return v === "validated" ? "up" : v === "promising" ? "muted" : "down";
}

function renderValidation(r) {
  const s = r.summary || {};
  const rows = [
    ["Score", `<b>${fmt(s.score, 1)}/100</b>`],
    ["Verdict", `<span class="${verdictClass(s.verdict)}"><b>${(s.verdict || "").toUpperCase()}</b></span>`],
    ["Samples profitable", `${fmt(s.profitable_pct, 0)}%`],
    ["Median profit factor", fmt(s.median_pf)],
    ["Median return", pct(s.median_return_pct)],
    ["Worst sample", `<span class="${cls(s.worst_return_pct)}">${pct(s.worst_return_pct)}</span>`],
    ["Total trades", s.total_trades],
  ];
  if (r.monte_carlo) {
    rows.push(["Monte Carlo (median)", pct(r.monte_carlo.p50_return_pct, 1)]);
    rows.push(["MC 95th pct drawdown", fmt(r.monte_carlo.p95_max_dd_pct, 1) + "%"]);
  }
  if (r.walk_forward && r.walk_forward.length) {
    const wins = r.walk_forward.filter((w) => w.return_pct > 0).length;
    rows.push(["Walk-forward folds", `${wins}/${r.walk_forward.length} profitable`]);
  }
  if (r.cost_sensitivity) {
    rows.push(["2× spread survival", fmt(r.cost_sensitivity.survival * 100, 0) + "%"]);
  }
  const findings = (r.reasons || []).length
    ? `<ul class="findings">${r.reasons.map((x) => `<li>${x}</li>`).join("")}</ul>` : "";
  return `<div class="metrics">${rows.map(([a, b]) => `<div>${a}</div><div>${b}</div>`).join("")}</div>${findings}`;
}

function renderValidationTable(reports) {
  const head = `<tr><th>Strategy</th><th>Score</th><th>Verdict</th><th>Profitable</th>
    <th>PF</th><th>Median</th><th>Trades</th></tr>`;
  const body = reports.map((r) => {
    const s = r.summary || {};
    return `<tr><td>${r.strategy}</td><td>${fmt(s.score, 1)}</td>
      <td class="${verdictClass(s.verdict)}">${(s.verdict || "").toUpperCase()}</td>
      <td>${fmt(s.profitable_pct, 0)}%</td><td>${fmt(s.median_pf)}</td>
      <td>${pct(s.median_return_pct)}</td><td>${s.total_trades}</td></tr>`;
  }).join("");
  return `<table class="plain"><thead>${head}</thead><tbody>${body}</tbody></table>`;
}

$("vBtn").onclick = async () => {
  $("vBtn").disabled = true;
  $("vOut").innerHTML = "Running the battery: independent samples, walk-forward, " +
    "Monte Carlo, cost sensitivity… this takes a moment.";
  const r = await (await fetch("/api/validate", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol: [...selectedPairs][0] || "EUR/USD", timeframe: $("rTimeframe").value,
      strategy: $("rStrategy").value, samples: +$("rSamples").value,
      bars: +$("rBars").value,
    }),
  })).json();
  $("vOut").innerHTML = renderValidation(r);
  $("vBtn").disabled = false;
};

/* --------------------------------------------------------------- streams */
let poller = null;
function startPolling() {
  if (poller) return;
  poller = setInterval(async () => {
    try { render(await (await fetch("/api/state")).json()); } catch (e) { /* retry */ }
    try { renderDemo(await (await fetch("/api/demo/state")).json()); } catch (e) { /* retry */ }
  }, 1500);
}
function socket(path, onMessage) {
  let opened = false;
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + path);
  ws.onopen = () => { opened = true; };
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onerror = () => startPolling();
  ws.onclose = () => { startPolling(); setTimeout(() => socket(path, onMessage), opened ? 2000 : 15000); };
  return ws;
}

loadMeta().then(() => {
  socket("/ws", render);
  socket("/ws/demo", renderDemo);
});
window.addEventListener("resize", () => {
  drawChart("equityChart", curve, 230);
  drawChart("demoChart", demoCurve, 200);
});
