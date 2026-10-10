const $ = (id) => document.getElementById(id);

let lastSnap = null;
let selected = null;
let lastEmbed = "";
let lastTs = 0;
let side = "buy";
let buyUsdc = 10;
let sellPct = 50;
let quote = null;

const fmt = (n, d = 2) => {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? d : abs >= 0.01 ? 4 : 6;
  return Number(n).toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: 2 });
};
const u = (n, d = 2) => (n == null || Number.isNaN(n) ? "—" : `${fmt(n, d)}U`);
const cls = (n) => (n == null ? "" : n > 0 ? "up" : n < 0 ? "down" : "");
const signed = (n, d = 2) => {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n > 0 ? "+" : ""}${fmt(n, d)}`;
};
const signedU = (n, d = 2) => (n == null || Number.isNaN(n) ? "—" : `${signed(n, d)}U`);
const pct = (n) => (n == null ? "—" : `${signed(n, 2)}%`);
const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "—");
const when = (ts) => {
  if (!ts) return "—";
  return new Date(ts * 1000).toISOString().replace("T", " ").replace(".000Z", " UTC");
};
const mcap = (n) => {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const trim = (x) => String(x).replace(/\.0$/, "");
  if (abs >= 1e9) return `${trim((n / 1e9).toFixed(2))}B`;
  if (abs >= 1e6) return `${trim((n / 1e6).toFixed(1))}M`;
  if (abs >= 1e3) return `${trim((n / 1e3).toFixed(1))}K`;
  return String(Math.round(n));
};

function renderKpis(s) {
  const tokenAum = (s.aum_mark ?? 0) - (s.usdc ?? 0);
  const items = [
    ["总资产", u(s.aum_mark), `可兑现 ${u(s.aum_exit)}`, ""],
    ["现金", u(s.usdc), "USDC", ""],
    ["代币总资产", u(tokenAum), `开仓成本 ${u(s.cost_open)}`, ""],
    ["未实现盈亏", signedU(s.upnl_mark), `相对成本 ${pct(s.upnl_pct)}`, cls(s.upnl_mark)],
    ["持仓", String((s.holdings || []).length), `可卖回 ${signedU(s.upnl_exit)}`, cls(s.upnl_exit)],
  ];
  $("kpis").innerHTML = items.map(([lab, val, tiny, c]) => `
    <div class="kpi">
      <div class="lab">${lab}</div>
      <div class="val ${c}">${val}</div>
      <div class="tiny">${tiny}</div>
    </div>
  `).join("");
}

function renderHoldings(s) {
  const rows = s.holdings || [];
  $("hold-count").textContent = `${rows.length} 个 · 每 ${(s.refresh_ms || 4000) / 1000}s 刷新`;
  $("holdings").innerHTML = rows.map((h) => {
    const active = h.token.toLowerCase() === (selected || "").toLowerCase() ? "active" : "";
    const img = h.image
      ? `<img src="${h.image}" alt="">`
      : `<div class="ph">${(h.symbol || "?").slice(0, 2)}</div>`;
    return `
      <div class="row ${active}" data-token="${h.token}">
        ${img}
        <div>
          <div class="sym">${h.symbol}</div>
          <div class="name">${h.name}</div>
          <div class="meta-line">成本 ${u(h.cost_usdc)} · 仓位 ${u(h.mark_value)} · 1h ${pct(h.chg_h1)}</div>
        </div>
        <div class="right">
          <div class="mcap">${mcap(h.mcap)}</div>
          <div class="pnl ${cls(h.upnl_mark)}">${signedU(h.upnl_mark)} · ${pct(h.upnl_pct)}</div>
        </div>
      </div>
    `;
  }).join("");
  for (const el of document.querySelectorAll(".row")) {
    el.addEventListener("click", () => selectToken(el.dataset.token, true));
  }
}

function selectToken(token, user) {
  selected = token;
  const h = (lastSnap?.holdings || []).find((x) => x.token.toLowerCase() === token.toLowerCase());
  if (!h) return;
  $("chart-title").textContent = `${h.symbol} / USDC`;
  $("chart-sub").textContent = h.pair ? `池 ${short(h.pair)} · fee ${((h.fee || 0) / 10000).toFixed(2)}%` : "尚未索引到池";
  $("open-dex").href = h.dex_maker || h.dex_url || "#";
  const firstBuy = (h.fills || []).find((f) => f.side === "buy");
  $("open-tx").href = firstBuy?.explorer || lastSnap.explorer_wallet;
  $("buy-strip").innerHTML = `
    <div>池市值 <b>${mcap(h.mcap)}</b></div>
    <div>成本 <b>${u(h.cost_usdc)}</b></div>
    <div>仓位 <b>${u(h.mark_value)}</b></div>
    <div>可卖回 ${u(h.exit_value)}</div>
    <div>未实现 <b class="${cls(h.upnl_mark)}">${signedU(h.upnl_mark)} (${pct(h.upnl_pct)})</b></div>
    <div>24h ${pct(h.chg_h24)}</div>
  `;
  if (h.dex_embed && h.dex_embed !== lastEmbed) {
    lastEmbed = h.dex_embed;
    $("dex-frame").src = h.dex_embed;
    $("frame-fallback").classList.add("hidden");
  } else if (!h.dex_embed) {
    $("dex-frame").src = "about:blank";
    $("frame-fallback").classList.remove("hidden");
  }
  $("fills").innerHTML = `
    <table>
      <thead><tr><th>时间</th><th>方向</th><th>数量</th><th>金额</th><th>交易</th></tr></thead>
      <tbody>
        ${(h.fills || []).map((f) => `
          <tr>
            <td>${when(f.ts)}</td>
            <td class="side-${f.side}">${f.side === "buy" ? "买" : "卖"}</td>
            <td>${fmt(f.qty, 4)}</td>
            <td>${u(f.usdc)}</td>
            <td><a href="${f.explorer}" target="_blank" rel="noreferrer">${short(f.tx)}</a></td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
  if (user) {
    quote = null;
    $("quote-box").classList.add("hidden");
    renderHoldings(lastSnap);
  }
}

function applySnap(s) {
  lastSnap = s;
  lastTs = s.ts || Math.floor(Date.now() / 1000);
  $("wallet").textContent = s.wallet;
  $("wallet").href = s.explorer_wallet;
  $("pulse").className = "pulse ok";
  renderKpis(s);
  if (!selected && s.holdings?.length) selected = s.holdings[0].token;
  renderHoldings(s);
  if (selected) selectToken(selected, false);
}

function setSide(next) {
  side = next;
  $("side-buy").classList.toggle("on", side === "buy");
  $("side-sell").classList.toggle("on", side === "sell");
  $("buy-presets").classList.toggle("hidden", side !== "buy");
  $("sell-presets").classList.toggle("hidden", side !== "sell");
  $("amount-unit").textContent = side === "buy" ? "U" : "%";
  $("trade-amount").value = side === "buy" ? String(buyUsdc) : String(sellPct);
  quote = null;
  $("quote-box").classList.add("hidden");
}

function bindTrade() {
  const saved = sessionStorage.getItem("arc-desk-token");
  if (saved) $("trade-token").value = saved;
  $("trade-token").addEventListener("change", () => {
    sessionStorage.setItem("arc-desk-token", $("trade-token").value.trim());
  });
  $("side-buy").addEventListener("click", () => setSide("buy"));
  $("side-sell").addEventListener("click", () => setSide("sell"));
  $("buy-presets").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-usdc]");
    if (!btn) return;
    buyUsdc = Number(btn.dataset.usdc);
    $("trade-amount").value = String(buyUsdc);
    for (const b of $("buy-presets").querySelectorAll("button")) b.classList.toggle("on", b === btn);
  });
  $("sell-presets").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-pct]");
    if (!btn) return;
    sellPct = Number(btn.dataset.pct);
    $("trade-amount").value = String(sellPct);
    for (const b of $("sell-presets").querySelectorAll("button")) b.classList.toggle("on", b === btn);
  });
  $("btn-quote").addEventListener("click", askQuote);
}

function holding() {
  return (lastSnap?.holdings || []).find((x) => x.token.toLowerCase() === (selected || "").toLowerCase());
}

function renderQuote(q, err) {
  const box = $("quote-box");
  box.classList.remove("hidden");
  if (err) {
    box.innerHTML = `<div class="pnl down">${err}</div>`;
    return;
  }
  const h = holding();
  const label = q.side === "buy"
    ? `花 ${u(q.notional_usdc)} 大约得到 ${fmt(q.out_human, 4)} ${q.symbol}，最少 ${fmt(q.min_out_human, 4)}`
    : `卖 ${fmt(q.in_human, 4)} ${q.symbol} 大约收回 ${u(q.out_human)}，最少 ${u(q.min_out_human)}`;
  box.innerHTML = `
    <div>${label}</div>
    <div class="warn">报价 ${q.ttl_sec}s 内有效。确认时会再核一次，差超过 3% 就取消。单笔买入最多 ${lastSnap?.trade?.buy_cap_usdc ?? 25}U。</div>
    <div class="quote-confirm">
      <span>输入 ${q.confirm_symbol} 确认</span>
      <input id="confirm-sym" autocomplete="off" placeholder="${q.confirm_symbol}">
      <button type="button" id="btn-exec" class="btn go">确认成交</button>
    </div>
  `;
  $("btn-exec").addEventListener("click", () => execQuote(q));
}

async function askQuote() {
  const h = holding();
  if (!h) return renderQuote(null, "先点左侧代币");
  const token = $("trade-token").value.trim();
  sessionStorage.setItem("arc-desk-token", token);
  const amount = Number($("trade-amount").value);
  const body = side === "buy"
    ? { side: "buy", token: h.token, usdc: amount, action_token: token }
    : { side: "sell", token: h.token, sell_pct: amount, action_token: token };
  $("btn-quote").disabled = true;
  try {
    const res = await fetch("/api/quote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    quote = data;
    renderQuote(data);
  } catch (err) {
    quote = null;
    renderQuote(null, err.message || String(err));
  } finally {
    $("btn-quote").disabled = false;
  }
}

async function execQuote(q) {
  const typed = $("confirm-sym")?.value.trim();
  const btn = $("btn-exec");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticket: q.ticket,
        confirm_symbol: typed,
        action_token: $("trade-token").value.trim(),
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    quote = null;
    $("quote-box").classList.remove("hidden");
    $("quote-box").innerHTML = `<div class="pnl up">已成交 ${data.symbol} · <a href="${data.explorer}" target="_blank" rel="noreferrer">${short(data.tx)}</a></div>`;
    await tick();
  } catch (err) {
    renderQuote(null, err.message || String(err));
  }
}

async function tick() {
  try {
    const res = await fetch("/api/snapshot", { cache: "no-store" });
    if (!res.ok) throw new Error(await res.text());
    applySnap(await res.json());
  } catch (err) {
    $("pulse").className = "pulse err";
    $("age").textContent = "刷新失败";
    console.error(err);
  }
}

setInterval(() => {
  if (!lastTs) return;
  const sec = Math.max(0, Math.floor(Date.now() / 1000) - lastTs);
  $("age").textContent = `LIVE · ${sec}s 前更新 · chain 5042`;
}, 500);

bindTrade();
tick();
setInterval(tick, 3000);
