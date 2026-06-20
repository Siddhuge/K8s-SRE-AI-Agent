"use strict";

const $ = (sel) => document.querySelector(sel);
const history = [];   // {role, content} pairs sent back for context

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
    box.innerHTML = data.clusters.map(clusterCard).join("");
  } catch (e) {
    box.innerHTML = `<div class="empty">Failed to load clusters: ${escapeHtml(String(e))}</div>`;
  }
}
function clusterCard(c) {
  const ok = c.reachable;
  const ns = (c.namespaces || []).join(", ") || "—";
  return `<div class="card">
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
$("#refresh").addEventListener("click", loadClusters);

loadMe();
loadClusters();
loadTools();
setInterval(loadClusters, 15000);   // live-ish reachability
