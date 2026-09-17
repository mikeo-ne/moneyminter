const $ = (id) => document.getElementById(id);
const fmt = (n, d = 2) => Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const money = (n) => (n < 0 ? "-$" : "$") + fmt(Math.abs(n));
const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "muted");

let selectedPairs = new Set(["EUR/USD", "GBP/USD", "USD/JPY"]);
let curve = [];

async function loadMeta() {
  const m = await (await fetch("/api/meta")).json();
  $("strategy").innerHTML = Object.entries(m.strategies)
    .map(([k, v]) => `<option value="${k}" title="${v.doc}">${k}</option>`).join("");
  $("timeframe").innerHTML = m.timeframes.map(t => `<option ${t === "M1" ? "selected" : ""}>${t}</option>`).join("");
  $("pairs").innerHTML = m.instruments.map(s =>
    `<span class="chip ${selectedPairs.has(s) ? "on" : ""}" data-s="${s}">${s}</span>`).join("");
  document.querySelectorAll(".chip").forEach(c => c.onclick = () => {
    const s = c.dataset.s;
    selectedPairs.has(s) ? selectedPairs.delete(s) : selectedPairs.add(s);
    c.classList.toggle("on");
  });
}

function drawChart(points) {
  const cv = $("equityChart"), ctx = cv.getContext("2d");
  const w = cv.width = cv.clientWidth * devicePixelRatio;
  const h = cv.height = 230 * devicePixelRatio;
  ctx.clearRect(0, 0, w, h);
  if (points.length < 2) return;
  const ys = points.map(p => p.equity);
  let min = Math.min(...ys), max = Math.max(...ys);
  if (max - min < 1e-9) { max += 1; min -= 1; }
  const pad = (max - min) * 0.12; min -= pad; max += pad;
  const X = i => (i / (points.length - 1)) * (w - 60) + 50;
  const Y = v => h - 24 - ((v - min) / (max - min)) * (h - 50);

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

function render(s) {
  $("dot").className = "dot " + (s.running ? "live" : "off");
  $("statusText").textContent = s.running
    ? `live · ${s.config.strategy} · ${s.config.symbols.join(", ")} · ${s.config.timeframe} ×${s.config.speed}`
    : "stopped";
  $("equity").textContent = money(s.equity);
  $("pnl").innerHTML = `<span class="${cls(s.pnl)}">${money(s.pnl)} (${s.pnl_pct > 0 ? "+" : ""}${fmt(s.pnl_pct)}%)</span>`;
  $("openCount").textContent = s.open_positions.length;
  $("tradeCount").textContent = `${s.stats.trades} (${s.stats.wins}W/${s.stats.losses}L)`;
  $("winRate").textContent = fmt(s.stats.win_rate, 1) + "%";
  $("dd").innerHTML = `<span class="${s.stats.drawdown_pct > 0 ? "down" : "muted"}">${fmt(s.stats.drawdown_pct)}%</span>`;

  curve = s.equity_curve;
  drawChart(curve);

  $("pricesTable").querySelector("tbody").innerHTML = Object.entries(s.prices)
    .map(([k, v]) => `<tr><td>${k}</td><td style="text-align:right">${v}</td></tr>`).join("");

  $("positions").querySelector("tbody").innerHTML = s.open_positions.length
    ? s.open_positions.map(p => `<tr>
        <td>${p.symbol}</td><td><span class="badge ${p.side}">${p.side}</span></td>
        <td>${fmt(p.units, 0)}</td><td>${fmt(p.entry_price, 5)}</td><td>${fmt(p.current_price, 5)}</td>
        <td class="${cls(p.pips)}">${fmt(p.pips, 1)}</td>
        <td class="${cls(p.unrealized)}">${money(p.unrealized)}</td>
        <td class="muted">${p.stop_loss ? fmt(p.stop_loss, 5) : "–"} / ${p.take_profit ? fmt(p.take_profit, 5) : "–"}</td>
        <td><button class="xbtn" onclick="closePos('${p.id}')">close</button></td></tr>`).join("")
    : `<tr><td colspan="9" class="muted">No open positions.</td></tr>`;

  $("trades").querySelector("tbody").innerHTML = s.trades.length
    ? s.trades.slice(0, 40).map(t => `<tr>
        <td class="muted">${t.closed_at.slice(5, 16).replace("T", " ")}</td><td>${t.symbol}</td>
        <td><span class="badge ${t.side}">${t.side}</span></td><td>${fmt(t.units, 0)}</td>
        <td>${fmt(t.entry_price, 5)}</td><td>${fmt(t.exit_price, 5)}</td>
        <td class="${cls(t.pnl)}">${money(t.pnl)}</td><td class="muted">${t.reason}</td></tr>`).join("")
    : `<tr><td colspan="8" class="muted">No closed trades yet.</td></tr>`;

  $("events").innerHTML = s.events.map(e =>
    `<li><time>${e.ts.slice(11, 19)}</time><span class="k-${e.kind}">${e.message}</span></li>`).join("");
}

async function closePos(id) { await fetch("/api/close/" + id, { method: "POST" }); }
window.closePos = closePos;

$("startBtn").onclick = async () => {
  $("startBtn").disabled = true;
  await fetch("/api/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbols: [...selectedPairs], timeframe: $("timeframe").value, strategy: $("strategy").value,
      balance: +$("balance").value, speed: +$("speed").value, risk_per_trade: +$("risk").value / 100,
    }),
  });
  $("startBtn").disabled = false;
};
$("stopBtn").onclick = () => fetch("/api/stop", { method: "POST" });

$("btBtn").onclick = async () => {
  $("btOut").innerHTML = "Running backtest…";
  const r = await (await fetch("/api/backtest", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol: [...selectedPairs][0] || "EUR/USD", timeframe: "H1",
      strategy: $("strategy").value, balance: +$("balance").value,
      risk_per_trade: +$("risk").value / 100, bars: 3000,
    }),
  })).json();
  const m = r.metrics;
  const rows = [
    ["Net profit", `<span class="${cls(m.net_profit)}">${money(m.net_profit)} (${fmt(m.return_pct)}%)</span>`],
    ["Trades", `${m.trades} (${m.wins}W/${m.losses}L)`], ["Win rate", fmt(m.win_rate) + "%"],
    ["Profit factor", fmt(m.profit_factor)], ["Expectancy", money(m.expectancy)],
    ["Max drawdown", fmt(m.max_drawdown_pct) + "%"], ["Sharpe", fmt(m.sharpe)],
    ["Sortino", fmt(m.sortino)], ["Exposure", fmt(m.exposure_pct, 1) + "%"],
  ];
  $("btOut").innerHTML = `<div class="muted" style="margin-bottom:8px">${r.strategy} · ${r.symbol} ${r.timeframe}</div>
    <div class="metrics">${rows.map(([a, b]) => `<div>${a}</div><div>${b}</div>`).join("")}</div>`;
};

// Live updates over WebSocket, with automatic fallback to HTTP polling when
// websockets are unavailable (e.g. a proxy that does not upgrade connections).
let poller = null;
function startPolling() {
  if (poller) return;
  poller = setInterval(async () => {
    try { render(await (await fetch("/api/state")).json()); } catch (e) { /* retry next tick */ }
  }, 1500);
}
function connect() {
  let opened = false;
  try {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.onopen = () => { opened = true; clearInterval(poller); poller = null; };
    ws.onmessage = (e) => render(JSON.parse(e.data));
    ws.onerror = () => startPolling();
    ws.onclose = () => { startPolling(); setTimeout(connect, opened ? 2000 : 15000); };
  } catch (e) {
    startPolling();
  }
}
loadMeta().then(connect);
window.addEventListener("resize", () => drawChart(curve));
