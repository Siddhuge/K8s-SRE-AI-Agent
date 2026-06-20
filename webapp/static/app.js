"use strict";

const $ = (sel) => document.querySelector(sel);
const history = [];   // {role, content} pairs sent back for context
let clustersById = {};

// --- tiny, safe markdown-ish renderer (escape first, then a few patterns) ---
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
function renderMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/```([\s\S]*?)```/g, (_, c) => `<pre>${c.trim()}</pre>`);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return html;
}

// --- clusters ---
async function loadClusters() {
  const box = $("#clusters");
  try {
    const res = await fetch("/api/clusters");
    const data = await res.json();
    updateChatBadge(data.chat_enabled);
    if (!data.clusters || data.clusters.length === 0) {
      box.innerHTML = `<div class="empty">No clusters configured.${data.error ? "<br>" + escapeHtml(data.error) : ""}</div>`;
      return;
    }
    clustersById = Object.fromEntries(data.clusters.map((c) => [c.name, c]));
    box.innerHTML = data.clusters.map(clusterCard).join("");
  } catch (e) {
    box.innerHTML = `<div class="empty">Failed to load clusters: ${escapeHtml(String(e))}</div>`;
  }
}
function clusterCard(c) {
  const ok = c.reachable;
  const ns = (c.namespaces || []).join(", ") || "—";
  return `<div class="card" data-name="${escapeHtml(c.name)}" title="Click to drill in">
    <div class="card-top">
      <span class="card-name">${escapeHtml(c.name)}</span>
      <span class="status"><span class="dot ${ok ? "ok" : "bad"}"></span>${ok ? "reachable" : escapeHtml(c.status_detail || "unreachable")}</span>
    </div>
    <div class="chips">
      ${c.provider ? `<span class="chip prov">${escapeHtml(c.provider)}</span>` : ""}
      ${c.tenant ? `<span class="chip">tenant: ${escapeHtml(c.tenant)}</span>` : ""}
      ${c.region ? `<span class="chip">${escapeHtml(c.region)}</span>` : ""}
    </div>
    <div class="ns">namespaces: ${escapeHtml(ns)}</div>
  </div>`;
}
function updateChatBadge(enabled) {
  const b = $("#chat-badge");
  b.textContent = enabled ? "AI chat: enabled" : "AI chat: offline (set ANTHROPIC_API_KEY)";
  b.className = "badge " + (enabled ? "badge-ok" : "badge-muted");
}

async function loadTools() {
  try {
    const { tools } = await (await fetch("/api/tools")).json();
    $("#tools").innerHTML = tools.map(
      (t) => `<li><code>${escapeHtml(t.name)}</code> — ${escapeHtml(t.summary)}</li>`
    ).join("");
  } catch { /* non-critical */ }
}

// --- cluster drill-down ---
function arrOrErr(x) { return Array.isArray(x) ? null : (x && x.error) || "unavailable"; }

function podsSection(pods) {
  const err = arrOrErr(pods);
  if (err) return `<div><h3>Pods</h3><div class="mono">${escapeHtml(err)}</div></div>`;
  if (!pods.length) return `<div><h3>Pods</h3><div class="mono">no pods</div></div>`;
  const rows = pods.map((p) => {
    const bad = ["CrashLoopBackOff", "Error", "ImagePullBackOff", "OOMKilled", "Evicted"].includes(p.reason || p.last_terminated);
    const [r, t] = (p.ready || "0/0").split("/");
    const readyCls = r === t ? "ok" : "warn";
    return `<tr><td>${escapeHtml(p.name)}</td><td>${escapeHtml(p.phase || "")}</td>
      <td>${p.reason ? `<span class="pill ${bad ? "bad" : ""}">${escapeHtml(p.reason)}</span>` : "—"}</td>
      <td>${p.restarts}</td><td><span class="pill ${readyCls}">${escapeHtml(p.ready || "")}</span></td>
      <td class="mono">${escapeHtml(p.node || "—")}</td></tr>`;
  }).join("");
  return `<div><h3>Pods (${pods.length})</h3><table class="grid"><tr><th>Name</th><th>Phase</th><th>Reason</th><th>Restarts</th><th>Ready</th><th>Node</th></tr>${rows}</table></div>`;
}

function depsSection(deps, cluster, ns) {
  const err = arrOrErr(deps);
  if (err) return `<div><h3>Deployments</h3><div class="mono">${escapeHtml(err)}</div></div>`;
  if (!deps.length) return `<div><h3>Deployments</h3><div class="mono">no deployments</div></div>`;
  const rows = deps.map((d) => {
    const [r, t] = (d.ready || "0/0").split("/");
    return `<tr><td>${escapeHtml(d.name)}</td><td><span class="pill ${r === t ? "ok" : "warn"}">${escapeHtml(d.ready || "")}</span></td>
      <td class="mono">${escapeHtml(d.image || "—")}</td>
      <td><button class="diag-btn" data-cluster="${escapeHtml(cluster)}" data-ns="${escapeHtml(ns)}" data-name="${escapeHtml(d.name)}">Diagnose</button></td></tr>`;
  }).join("");
  return `<div><h3>Deployments (${deps.length})</h3><table class="grid"><tr><th>Name</th><th>Ready</th><th>Image</th><th></th></tr>${rows}</table></div>`;
}

function eventsSection(events) {
  const err = arrOrErr(events);
  if (err) return `<div><h3>Recent events</h3><div class="mono">${escapeHtml(err)}</div></div>`;
  if (!events.length) return `<div><h3>Recent events</h3><div class="mono">no recent events</div></div>`;
  const rows = events.slice(0, 15).map((e) => `<tr>
    <td><span class="pill ${e.type === "Warning" ? "warn" : ""}">${escapeHtml(e.type || "")}</span></td>
    <td>${escapeHtml(e.reason || "")}</td><td class="mono">${escapeHtml(e.object || "")}</td>
    <td>${escapeHtml((e.message || "").slice(0, 100))}</td><td class="mono">${escapeHtml(e.age || "")}</td></tr>`).join("");
  return `<div><h3>Recent events</h3><table class="grid"><tr><th>Type</th><th>Reason</th><th>Object</th><th>Message</th><th>Age</th></tr>${rows}</table></div>`;
}

function openDetail(name) {
  const c = clustersById[name];
  if (!c) return;
  $("#detail-title").textContent = name;
  const sel = $("#detail-ns");
  const nss = c.namespaces && c.namespaces.length ? c.namespaces : ["default"];
  sel.innerHTML = nss.map((n) => `<option>${escapeHtml(n)}</option>`).join("");
  sel.onchange = () => loadNamespace(name, sel.value);
  $("#detail").classList.remove("hidden");
  loadNamespace(name, sel.value);
}

async function loadNamespace(cluster, ns) {
  const body = $("#detail-body");
  body.innerHTML = `<div class="empty">loading ${escapeHtml(ns)}…</div>`;
  try {
    const url = `/api/clusters/${encodeURIComponent(cluster)}/namespaces/${encodeURIComponent(ns)}`;
    const d = await (await fetch(url)).json();
    body.innerHTML = podsSection(d.pods) + depsSection(d.deployments, cluster, ns) + eventsSection(d.events);
  } catch (e) {
    body.innerHTML = `<div class="empty">${escapeHtml(String(e))}</div>`;
  }
}

// --- chat ---
function addMessage(role, html) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + (role === "user" ? "user" : "agent");
  wrap.innerHTML = `<div class="bubble">${html}</div>`;
  $("#messages").appendChild(wrap);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return wrap;
}
function traceHtml(trace) {
  if (!trace || !trace.length) return "";
  return `<div class="trace">${trace.map((t) => `<span class="chip">🔧 ${escapeHtml(t.tool)}</span>`).join("")}</div>`;
}

// Streamed chat over SSE: live token-by-token text + tool-call chips.
async function sendMessage(text) {
  addMessage("user", escapeHtml(text));
  history.push({ role: "user", content: text });
  const bubble = addMessage("agent", `<span class="typing">investigating…</span>`).querySelector(".bubble");
  $("#send").disabled = true;

  let reply = "";
  const trace = [];
  const render = () => { bubble.innerHTML = renderMarkdown(reply) + traceHtml(trace); $("#messages").scrollTop = 1e9; };

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, history: history.slice(0, -1) }),
    });
    if (res.status === 401) { window.location = "/auth/login"; return; }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const events = buf.split("\n\n");
      buf = events.pop();                         // keep the trailing partial
      for (const ev of events) {
        const line = ev.replace(/^data: /, "").trim();
        if (!line) continue;
        const msg = JSON.parse(line);
        if (msg.type === "delta") { reply += msg.text; render(); }
        else if (msg.type === "tool") { trace.push({ tool: msg.tool }); render(); }
      }
    }
    if (!reply) reply = "(no response)";
    render();
    history.push({ role: "assistant", content: reply });
  } catch (e) {
    bubble.innerHTML = `Error: ${escapeHtml(String(e))}`;
  } finally {
    $("#send").disabled = false;
    $("#chat-input").focus();
  }
}

async function loadMe() {
  try {
    const me = await (await fetch("/api/me")).json();
    const el = $("#user");
    if (me.auth_enabled && me.user) {
      el.innerHTML = `${escapeHtml(me.user.name || me.user.sub)} <a href="/auth/logout">log out</a>`;
    }
  } catch { /* non-critical */ }
}

// --- wiring ---
$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  sendMessage(text);
});
$("#refresh").addEventListener("click", (e) => { e.stopPropagation(); loadClusters(); });

// open drill-down when a cluster card is clicked
$("#clusters").addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (card) openDetail(card.dataset.name);
});
$("#detail-back").addEventListener("click", () => $("#detail").classList.add("hidden"));

// "Diagnose" inside the drill-down → route the question into the chat
$("#detail-body").addEventListener("click", (e) => {
  const btn = e.target.closest(".diag-btn");
  if (!btn) return;
  $("#detail").classList.add("hidden");
  const q = `diagnose ${btn.dataset.ns}/${btn.dataset.name} in ${btn.dataset.cluster}`;
  $("#chat-input").value = "";
  sendMessage(q);
});

loadMe();
loadClusters();
loadTools();
setInterval(loadClusters, 15000);   // live-ish reachability
