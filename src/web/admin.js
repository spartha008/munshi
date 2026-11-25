// Munshi operator console: read-only view of each shop's conversation, agent trace and ledger.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const rupee = (x) => (x == null ? "–" : "₹" + Math.round(x).toLocaleString("en-IN"));
const num = (x, d = 1) => (x == null ? "∞" : Number(x).toFixed(d));
const when = (iso) => (iso || "").replace("T", " ");
const hhmm = (iso) => (iso || "").slice(11, 16);

let current = null;
let S = null;

async function api(path) {
  // location.origin drops any user:pass@ in the address bar, which fetch() would otherwise reject.
  const r = await fetch(new URL(path, location.origin), { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

async function loadShops() {
  const d = await api("/admin/api/shops");
  const st = d.status;
  $("#chips").innerHTML = [
    ["WhatsApp", st.whatsapp], ["Claude", st.claude.enabled], ["Voice notes", st.voice],
  ].map(([k, ok]) => `<span class="chip ${ok ? "" : "off"}">${k}: ${ok ? "on" : "off"}</span>`).join("");
  $("#shopList").innerHTML = d.shops.length ? d.shops.map((s) => `
    <li><button data-id="${esc(s.shop_id)}" class="${s.shop_id === current ? "active" : ""}">
      <strong>${esc(s.name || "New shop")}</strong>
      <span>+${esc(s.shop_id)} · ${esc(s.locality || "")}</span>
      <span>${s.onboarding_step ? `<span class="pill open">onboarding: ${esc(s.onboarding_step)}</span>` :
        `${s.bills} bills · ${s.orders_sent} orders sent`} · last ${esc(when(s.last_inbound_at) || "–")}</span>
    </button></li>`).join("") : `<li class="empty">No shops yet. When someone messages the WhatsApp number, they appear here.</li>`;
  $$("#shopList button").forEach((b) => b.addEventListener("click", () => selectShop(b.dataset.id)));
}

async function selectShop(id) {
  current = id;
  $$("#shopList button").forEach((b) => b.classList.toggle("active", b.dataset.id === id));
  S = await api(`/admin/api/shops/${encodeURIComponent(id)}`);
  render();
}

function render() {
  const s = S.shop, d = S.distributor, b = S.budget;
  $("#detailHead").innerHTML = `<h2>${esc(s.name || "New shop")}</h2>
    <p class="empty">+${esc(s.shop_id)} · ${esc(s.locality || "area not set")} · ${esc({ en: "English", kn: "Kannada", hi: "Hindi" }[s.language] || s.language)}
    · salesman ${esc(s.salesman_day || "–")} · distributor ${esc(d ? d.name + (d.phone ? " (+" + d.phone + ")" : " (no number)") : "–")}
    ${b ? `· cap ${rupee(b.owner_cap)}` : ""}</p>
    ${S.kpis ? `<div class="cards kpis">
      <div class="card"><div class="v">${rupee(S.kpis.expiring_stock_caught_rupees)}</div><div class="l">expiring stock caught (${S.kpis.expiry_actions} actions)</div></div>
      <div class="card"><div class="v">${S.kpis.top20_stockouts_last_7d} <span class="muted small">vs ${S.kpis.top20_stockouts_prev_7d}</span></div><div class="l">stockouts on top-20 items, last 7 days vs week before</div></div>
      <div class="card"><div class="v">${S.kpis.median_minutes_to_ok == null ? "–" : S.kpis.median_minutes_to_ok + " min"}</div><div class="l">median time from proposal to OK (${S.kpis.orders_approved} orders)</div></div>
    </div>` : ""}`;
  renderChat(); renderTrace(); renderLedger(); renderOrders(); renderExpiry(); renderMemory();
}

function bubble(m) {
  if (m.kind === "system") return `<div class="sys">${esc(m.body)}</div>`;
  const side = m.direction === "in" ? "in" : "out";
  let inner = esc(m.body);
  if (m.kind === "image" && m.media_path) inner = `<img src="/admin/media/${esc(m.media_path)}" alt="Bill photo">` + (m.body !== "[bill photo]" ? esc(m.body) : "");
  if (m.kind === "voice") inner = `<span class="tag">🎤 Voice note (transcribed)</span><span class="transcript">“${esc(m.body)}”</span>`;
  return `<div class="msg ${side}">${inner}<time>${hhmm(m.created_at)}</time></div>`;
}

function renderChat() {
  const el = $("#pane-chat");
  el.innerHTML = `<div class="chat">${S.messages.map(bubble).join("") || '<p class="empty">No messages.</p>'}</div>`;
  const c = $(".chat", el); c.scrollTop = c.scrollHeight;
}

const GATE = new Set(["approve_order", "send_order", "ask_owner"]);
function renderTrace() {
  const byMsg = new Map();
  for (const t of [...S.trace].reverse()) {
    if (!byMsg.has(t.message_id)) byMsg.set(t.message_id, []);
    byMsg.get(t.message_id).push(t);
  }
  const msgs = new Map(S.messages.map((m) => [m.id, m]));
  const turns = [...byMsg.entries()].reverse().map(([mid, calls]) => {
    const m = msgs.get(mid) || {};
    const label = { image: "Bill photo", voice: "Voice note", upi: "UPI SMS", system: "Scheduled" }[m.kind] || "Text";
    return `<div class="turn"><div class="turn-head"><span class="k">${label}</span><span class="t">${esc(m.body || "")}</span><span class="muted">${hhmm(m.created_at)}</span></div>
      <ul class="calls">${calls.map((c) => {
        const err = (c.output || "").startsWith('{"error"');
        return `<li><details><summary><span class="tool ${err ? "err" : GATE.has(c.tool) ? "gate" : ""}">${esc(c.tool)}</span>
          <span class="io">→ ${esc((c.output || "").slice(0, 160))}</span></summary>
          <span class="io"><b>in:</b> ${esc(c.input)}</span><span class="io"><b>out:</b> ${esc(c.output)}</span></details></li>`;
      }).join("")}</ul></div>`;
  });
  $("#pane-trace").innerHTML = turns.join("") || `<p class="empty">No tool calls yet.</p>`;
}

function renderLedger() {
  const rows = [...S.ledger].sort((a, b) => (a.cover_days ?? 1e9) - (b.cover_days ?? 1e9));
  $("#pane-ledger").innerHTML = rows.length ? `
    <p class="empty">Built only from bills and shelf checks. Velocity = (units in − change in shelf count) ÷ days.</p>
    <table><thead><tr><th>Item</th><th class="num">On hand</th><th class="num">Sells / day</th><th>Confidence</th><th class="num">Cover (days)</th><th class="num">₹ at risk</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td>${esc(r.name)}${r.censored ? ' <span class="ranout">ran out</span>' : ""}</td>
      <td class="num">${num(r.on_hand, 0)}</td><td class="num">${num(r.velocity, 2)} <span class="io inline">(${num(r.band_low, 1)}–${num(r.band_high, 1)})</span></td>
      <td><span class="pill ${esc(r.confidence)}">${esc(r.confidence)}</span></td><td class="num">${num(r.cover_days, 1)}</td>
      <td class="num ${r.rupees_at_risk ? "risk" : ""}">${r.rupees_at_risk ? rupee(r.rupees_at_risk) : "–"}</td></tr>`).join("")}
    </tbody></table>` : `<p class="empty">No stock yet. It fills in from bill photos and shelf voice notes.</p>`;
}

function renderOrders() {
  const b = S.budget, o = S.orders[0];
  const stages = ["proposed", "edited", "approved", "sent"];
  let html = b ? `<div class="cards">
    <div class="card"><div class="v">${rupee(b.owner_cap)}</div><div class="l">owner's cap</div></div>
    <div class="card"><div class="v">${b.cash_on_hand == null ? "–" : rupee(b.cash_on_hand)}</div><div class="l">cash told</div></div>
    <div class="card"><div class="v">+${rupee(b.expected_udhaar)}</div><div class="l">udhaar likely back</div></div>
    <div class="card"><div class="v">−${rupee(b.commitments)}</div><div class="l">dues</div></div>
    <div class="card"><div class="v">${rupee(b.cap)}</div><div class="l">this week's limit</div></div></div>` : "";
  if (!o) html += `<p class="empty">No orders yet.</p>`;
  else {
    const at = stages.indexOf(o.status);
    html += `<h3>Order #${o.order_id} · ${rupee(o.budget_used)} of ${rupee(o.budget_cap)} · ${esc(when(o.proposed_on))}</h3>
      <div class="pipeline">${stages.map((s, i) => `<span class="${i <= at ? "on" : ""} ${s === "approved" ? "gate" : ""}">${s === "approved" ? "owner OK" : s}</span>`).join("")}${o.status === "cancelled" ? '<span class="on">superseded</span>' : ""}</div>
      ${o.approval_text ? `<p class="empty">Approved by the owner's message “${esc(o.approval_text)}” at ${esc(when(o.approved_on))}</p>` : ""}
      <table><thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Free</th><th class="num">Cost</th><th>Why</th></tr></thead><tbody>
      ${o.lines.map((l) => `<tr><td>${esc(l.name)}</td><td class="num">${l.qty}</td><td class="num">${l.free_qty || ""}</td><td class="num">${rupee(l.qty * l.unit_cost)}</td>
        <td>${esc(l.reason)}${l.scheme_verdict ? ` <span class="pill ${esc(l.scheme_verdict)}">${esc(l.scheme_verdict)}</span>` : ""}</td></tr>`).join("")}</tbody></table>`;
  }
  html += `<h3>Orders sent</h3>` + (S.outbox.length ? S.outbox.map((m) => `<div class="empty">To ${esc(m.to_name)} · ${esc(m.channel)} · ${esc(m.status)}${m.error ? " · " + esc(m.error) : ""} · ${esc(when(m.created_at))}</div><div class="outbox">${esc(m.body)}</div>`).join("") : `<p class="empty">None.</p>`);
  html += `<h3>Schemes evaluated</h3>` + (S.schemes.length ? `<table><tbody>${S.schemes.map((s) => `<tr><td>${esc(s.name)}</td><td>${s.buy_n}+${s.free_m}</td><td>${s.verdict ? `<span class="pill ${esc(s.verdict)}">${esc(s.verdict)}</span>` : ""}</td><td>${esc(s.reason || "")}</td></tr>`).join("")}</tbody></table>` : `<p class="empty">None.</p>`);
  $("#pane-orders").innerHTML = html;
}

function renderExpiry() {
  const e = S.expiry;
  $("#pane-expiry").innerHTML = (e.length ? `<table><thead><tr><th>Item</th><th class="num">At risk</th><th>Expires</th><th class="num">₹</th><th>Action</th></tr></thead><tbody>
    ${e.map((r) => `<tr><td>${esc(r.name)}</td><td class="num">${num(r.at_risk_units, 0)} of ${num(r.units, 0)}</td><td>${esc(r.expiry)} (${r.days_to_expiry} d)</td><td class="num risk">${rupee(r.rupees_at_risk)}</td><td>${esc(r.text)}</td></tr>`).join("")}</tbody></table>`
    : `<p class="empty">Nothing at risk.</p>`) +
    `<h3>Reminders</h3>` + (S.reminders.length ? `<table><tbody>${S.reminders.map((r) => `<tr><td>${esc(r.due_on)}</td><td>${esc(r.reason)}</td><td><span class="pill">${esc(r.status)}</span></td></tr>`).join("")}</tbody></table>` : `<p class="empty">None.</p>`);
}

function renderMemory() {
  $("#pane-memory").innerHTML = `
    <h3>Questions to the owner</h3>
    ${S.questions.length ? `<table><tbody>${S.questions.map((q) => `<tr><td>${esc(q.text)}</td><td><span class="pill ${esc(q.status)}">${esc(q.status)}</span></td><td>${esc(q.answer || "")}</td><td class="muted">${esc(when(q.asked_on || q.created_on))}</td></tr>`).join("")}</tbody></table>` : `<p class="empty">None.</p>`}
    <h3>Udhaar outstanding</h3>
    ${S.udhaar.length ? `<table><tbody>${S.udhaar.map((u) => `<tr><td>${esc(u.customer)}</td><td class="num">${rupee(u.amount)}</td><td>${esc(u.given_on)}</td></tr>`).join("")}</tbody></table>` : `<p class="empty">None.</p>`}
    <h3>Shop vocabulary (alias memory)</h3>
    ${S.aliases.length ? `<table><thead><tr><th>Phrase</th><th>Item</th><th>From</th><th>When</th></tr></thead><tbody>${S.aliases.map((a) => `<tr><td><code>${esc(a.phrase)}</code></td><td>${esc(a.name)}</td><td>${esc(a.source)}</td><td>${esc(when(a.last_confirmed))}</td></tr>`).join("")}</tbody></table>` : `<p class="empty">Empty — it grows with every bill and voice note.</p>`}`;
}

$$(".tabs button").forEach((b) => b.addEventListener("click", () => {
  $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".pane").forEach((p) => p.classList.toggle("active", p.id === "pane-" + b.dataset.tab));
}));
$("#reload").addEventListener("click", async () => { await loadShops(); if (current) await selectShop(current); });

loadShops().catch((e) => { $("#shopList").innerHTML = `<li class="empty">Couldn't load: ${esc(e.message)}</li>`; });
setInterval(() => { loadShops(); if (current) selectShop(current); }, 15000);
