"""Single-file admin + chat UI (no build step, Tailwind via CDN)."""

PAGE = r"""<!doctype html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gpt2api</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {
    theme: {
      extend: {
        colors: { accent: '#0070f3' },
        fontFamily: { sans: ['Geist', 'Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'] },
      },
    },
  };
</script>
<style>
  html, body { height: 100%; }
  body { font-feature-settings: "cv02","cv03","cv04","cv11"; -webkit-font-smoothing: antialiased; }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: #262626; border-radius: 10px; border: 3px solid #000; }
  ::-webkit-scrollbar-track { background: transparent; }
  .msg pre { white-space: pre-wrap; word-break: break-word; }
</style>
</head>
<body class="h-full bg-black text-neutral-100 font-sans">

<div class="flex h-full">

  <!-- ── Sidebar ─────────────────────────────────────────────────────── -->
  <aside class="flex w-72 shrink-0 flex-col border-r border-neutral-900 bg-neutral-950">
    <div class="flex items-center gap-2 border-b border-neutral-900 px-4 py-3">
      <div class="grid h-7 w-7 place-items-center rounded-md bg-white text-xs font-bold text-black">g2</div>
      <span class="text-sm font-semibold tracking-tight">gpt2api</span>
      <span id="modeBadge" class="ml-auto rounded-full border border-neutral-800 px-2 py-0.5 text-[10px] font-medium text-neutral-400">…</span>
    </div>

    <div class="px-3 py-3">
      <button id="newChat" class="w-full rounded-lg border border-transparent bg-white px-3 py-2 text-sm font-medium text-black transition-colors hover:bg-neutral-200">
        + New chat
      </button>
    </div>

    <div class="px-3 pb-1 text-[10px] font-medium uppercase tracking-wider text-neutral-600">History</div>
    <nav id="history" class="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-2"></nav>
    <p id="historyEmpty" class="px-4 pb-3 text-xs text-neutral-600">No chats yet.</p>

    <details class="border-t border-neutral-900" open>
      <summary class="cursor-pointer select-none px-4 py-3 text-[10px] font-medium uppercase tracking-wider text-neutral-500 hover:text-neutral-300">Settings</summary>
      <div class="space-y-3 px-4 pb-4">
        <div>
          <label class="text-xs text-neutral-400">Model</label>
          <select id="model" class="mt-1 w-full rounded-lg border border-neutral-800 bg-black px-2 py-1.5 text-sm focus:border-neutral-600 focus:outline-none">
            <option value="auto">auto</option>
          </select>
          <button id="refreshModels" class="mt-2 text-xs text-neutral-500 hover:text-neutral-300">refresh list</button>
        </div>

        <div>
          <label class="text-xs text-neutral-400">Access token</label>
          <input id="token" type="password" autocomplete="off" placeholder="eyJhbGciOi…"
                 class="mt-1 w-full rounded-lg border border-neutral-800 bg-black px-2 py-1.5 text-sm placeholder-neutral-600 focus:border-neutral-600 focus:outline-none">
          <div class="mt-2 grid grid-cols-2 gap-2">
            <button id="save" class="rounded-lg border border-transparent bg-white px-2 py-1.5 text-xs font-medium text-black hover:bg-neutral-200">Save</button>
            <button id="importBtn" class="rounded-lg border border-neutral-800 px-2 py-1.5 text-xs hover:bg-neutral-900">Import</button>
            <button id="reloadBtn" class="rounded-lg border border-neutral-800 px-2 py-1.5 text-xs hover:bg-neutral-900">Reload</button>
            <button id="clear" class="rounded-lg border border-neutral-800 px-2 py-1.5 text-xs text-red-400 hover:bg-red-950/40">Clear</button>
          </div>
          <p id="importNote" class="mt-2 hidden text-[11px] leading-snug text-neutral-600">
            Docker can’t read your browser — run <code class="text-neutral-400">python host_agent.py</code> on the host, then Import.
          </p>
        </div>

        <p id="account" class="text-[11px] leading-snug text-neutral-600">Checking…</p>
      </div>
    </details>
  </aside>

  <!-- ── Chat ────────────────────────────────────────────────────────── -->
  <main class="flex min-w-0 flex-1 flex-col">
    <header class="flex items-center gap-3 border-b border-neutral-900 px-6 py-3">
      <h1 id="chatTitle" class="truncate text-sm font-medium">New chat</h1>
      <span id="resolved" class="rounded-full border border-neutral-800 px-2 py-0.5 text-[10px] text-neutral-500"></span>
      <span id="modelCount" class="ml-auto text-[11px] text-neutral-600"></span>
    </header>

    <div id="messages" class="min-h-0 flex-1 overflow-y-auto">
      <div id="welcome" class="mx-auto flex h-full max-w-2xl flex-col items-center justify-center px-6 text-center">
        <div class="grid h-12 w-12 place-items-center rounded-xl bg-white text-lg font-bold text-black">g2</div>
        <h2 class="mt-4 text-lg font-medium">Ask anything</h2>
        <p class="mt-1 text-sm text-neutral-500">Uses the ChatGPT web session of the configured account.</p>
      </div>
      <div id="thread" class="mx-auto max-w-2xl space-y-6 px-6 py-6"></div>
    </div>

    <div class="border-t border-neutral-900 px-6 py-4">
      <div class="mx-auto max-w-2xl">
        <div class="flex items-end gap-2 rounded-xl border border-neutral-800 bg-neutral-950 p-2 focus-within:border-neutral-700">
          <textarea id="input" rows="1" placeholder="Send a message…"
                    class="max-h-48 min-h-[36px] w-full resize-none bg-transparent px-2 py-1.5 text-sm placeholder-neutral-600 focus:outline-none"></textarea>
          <button id="send" class="rounded-lg border border-transparent bg-white px-3 py-2 text-sm font-medium text-black transition-colors hover:bg-neutral-200 disabled:cursor-not-allowed disabled:opacity-40">Send</button>
        </div>
        <p class="mt-2 text-center text-[11px] text-neutral-600">Enter to send · Shift+Enter for newline · chat history is stored in this browser</p>
      </div>
    </div>
  </main>
</div>

<script>
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const ADMIN_KEY = new URLSearchParams(location.search).get("key");

  const app = {
    chats: [],
    current: null,
    models: [],
    sending: false,
    status: null,
  };

  // ── storage ───────────────────────────────────────────────────────────
  const STORE_KEY = "gpt2api.chats";
  function saveChats() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(app.chats)); } catch (e) {}
  }
  function loadChats() {
    try { app.chats = JSON.parse(localStorage.getItem(STORE_KEY) || "[]"); } catch (e) { app.chats = []; }
  }
  function newChat() {
    const chat = { id: crypto.randomUUID(), title: "New chat", model: $("model").value, messages: [], createdAt: Date.now() };
    app.chats.unshift(chat);
    app.current = chat;
    saveChats();
    renderHistory();
    renderThread();
  }

  // ── admin api ─────────────────────────────────────────────────────────
  function adminHeaders() {
    const h = { "Content-Type": "application/json" };
    if (ADMIN_KEY) h["X-Admin-Key"] = ADMIN_KEY;
    return h;
  }
  async function admin(method, path, body) {
    const res = await fetch(path, { method, headers: adminHeaders(), body: body === undefined ? undefined : JSON.stringify(body) });
    const raw = await res.text();
    let data; try { data = JSON.parse(raw); } catch { data = raw; }
    if (!res.ok) throw new Error((data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : raw);
    return data;
  }
  function status(text, kind) {
    const el = $("account");
    el.textContent = text || "";
    el.className = "text-[11px] leading-snug " + (kind === "error" ? "text-red-400" : kind === "ok" ? "text-emerald-500" : "text-neutral-600");
  }

  async function loadStatus() {
    try {
      const s = await admin("GET", "/admin/status");
      app.status = s;
      const badge = $("modeBadge");
      badge.textContent = s.authenticated ? "authenticated" : "anonymous";
      badge.className = "ml-auto rounded-full border px-2 py-0.5 text-[10px] font-medium " +
        (s.authenticated ? "border-emerald-900 bg-emerald-950/60 text-emerald-400" : "border-amber-900 bg-amber-950/50 text-amber-400");
      $("modelCount").textContent = s.model_count ? s.model_count + " models" : "";
      $("importNote").classList.toggle("hidden", !!s.can_import || !s.authenticated);
      $("account").textContent = s.authenticated
        ? [s.account && s.account.name, s.account && s.account.email].filter(Boolean).join(" · ") || "Authenticated"
        : "Anonymous mode — paste a token to unlock GPT models, images and files.";
      return s;
    } catch (e) {
      status("Cannot reach admin API: " + e.message, "error");
    }
  }

  async function loadModels() {
    try {
      const data = await fetch("/v1/models").then((r) => r.json());
      app.models = (data.data || []).map((m) => m.id);
      const select = $("model");
      const keep = select.value;
      select.innerHTML = "";
      app.models.forEach((id) => {
        const o = document.createElement("option");
        o.value = id; o.textContent = id;
        select.appendChild(o);
      });
      select.value = app.models.includes(keep) ? keep : (app.models[0] || "auto");
    } catch (e) { status("Could not load models: " + e.message, "error"); }
  }

  // ── rendering ─────────────────────────────────────────────────────────
  function renderHistory() {
    const nav = $("history");
    nav.innerHTML = "";
    $("historyEmpty").style.display = app.chats.length ? "none" : "block";
    app.chats.forEach((chat) => {
      const row = document.createElement("div");
      row.className = "group flex items-center gap-1 rounded-lg px-2 py-1.5 text-sm hover:bg-neutral-900 " +
        (app.current && app.current.id === chat.id ? "bg-neutral-900" : "");
      const btn = document.createElement("button");
      btn.className = "min-w-0 flex-1 truncate text-left " + (app.current && app.current.id === chat.id ? "text-neutral-100" : "text-neutral-400");
      btn.textContent = chat.title || "New chat";
      btn.onclick = () => { app.current = chat; renderHistory(); renderThread(); };
      const del = document.createElement("button");
      del.className = "hidden shrink-0 px-1 text-neutral-600 hover:text-red-400 group-hover:block";
      del.textContent = "×";
      del.title = "Delete chat";
      del.onclick = (ev) => {
        ev.stopPropagation();
        app.chats = app.chats.filter((c) => c.id !== chat.id);
        if (app.current && app.current.id === chat.id) app.current = app.chats[0] || null;
        saveChats(); renderHistory(); renderThread();
      };
      row.appendChild(btn); row.appendChild(del);
      nav.appendChild(row);
    });
  }

  function messageNode(role, content, model) {
    const wrap = document.createElement("div");
    wrap.className = "msg flex gap-3 " + (role === "user" ? "justify-end" : "");
    const bubble = document.createElement("div");
    bubble.className = role === "user"
      ? "max-w-[85%] rounded-2xl bg-neutral-900 px-4 py-2.5 text-sm text-neutral-100"
      : "max-w-full text-sm leading-relaxed text-neutral-200";
    bubble.textContent = content || "";
    wrap.appendChild(bubble);
    if (role === "assistant" && model) {
      const tag = document.createElement("div");
      tag.className = "mt-1 text-[10px] text-neutral-600";
      tag.textContent = model;
      const holder = document.createElement("div");
      holder.className = "max-w-full";
      holder.appendChild(bubble); holder.appendChild(tag);
      wrap.innerHTML = ""; wrap.appendChild(holder);
    }
    return wrap;
  }

  function renderThread() {
    const thread = $("thread");
    thread.innerHTML = "";
    const chat = app.current;
    $("welcome").classList.toggle("hidden", !!(chat && chat.messages.length));
    $("chatTitle").textContent = chat ? (chat.title || "New chat") : "New chat";
    $("resolved").textContent = "";
    if (!chat) return;
    chat.messages.forEach((m) => thread.appendChild(messageNode(m.role, m.content, m.model)));
    thread.parentElement.scrollTop = thread.parentElement.scrollHeight;
  }

  // ── chat ──────────────────────────────────────────────────────────────
  async function send() {
    if (app.sending) return;
    const input = $("input");
    const text = input.value.trim();
    if (!text) return;
    if (!app.current) newChat();

    app.sending = true;
    $("send").disabled = true;
    $("welcome").classList.add("hidden");

    const model = $("model").value || "auto";
    const chat = app.current;
    chat.messages.push({ role: "user", content: text });
    if (chat.messages.length === 1) {
      chat.title = text.slice(0, 40);
      $("chatTitle").textContent = chat.title;
    }
    input.value = "";
    autoGrow(input);
    saveChats(); renderHistory();

    const assistant = { role: "assistant", content: "", model: model };
    chat.messages.push(assistant);
    const node = messageNode("assistant", "", model);
    $("thread").appendChild(node);
    const bubble = node.querySelector("div");
    const scroller = $("thread").parentElement;
    scroller.scrollTop = scroller.scrollHeight;

    try {
      const res = await fetch("/v1/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, messages: chat.messages.filter((m) => m.content).map((m) => ({ role: m.role, content: m.content })), stream: true }),
      });
      if (!res.ok || !res.body) {
        const raw = await res.text();
        throw new Error(raw || ("HTTP " + res.status));
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();
        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (payload === "[DONE]") continue;
          let ev; try { ev = JSON.parse(payload); } catch { continue; }
          if (ev.error) throw new Error(ev.error.message || "upstream error");
          if (ev.model) { assistant.model = ev.model; $("resolved").textContent = ev.model; }
          const delta = ev.choices && ev.choices[0] && ev.choices[0].delta && ev.choices[0].delta.content;
          if (delta) {
            assistant.content += delta;
            bubble.textContent = assistant.content;
            scroller.scrollTop = scroller.scrollHeight;
          }
        }
      }
      if (!assistant.content) assistant.content = "(empty response)";
    } catch (e) {
      assistant.content = assistant.content || "";
      const err = document.createElement("div");
      err.className = "mt-2 rounded-lg border border-red-900 bg-red-950/30 px-3 py-2 text-xs text-red-400";
      err.textContent = e.message || String(e);
      node.appendChild(err);
    } finally {
      saveChats();
      app.sending = false;
      $("send").disabled = false;
      $("input").focus();
    }
  }

  function autoGrow(el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 192) + "px";
  }

  // ── wiring ────────────────────────────────────────────────────────────
  $("newChat").onclick = newChat;
  $("send").onclick = send;
  $("input").addEventListener("input", (e) => autoGrow(e.target));
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $("model").onchange = (e) => { if (app.current) { app.current.model = e.target.value; saveChats(); } };

  $("refreshModels").onclick = async (e) => {
    e.target.disabled = true;
    try { await admin("POST", "/admin/models/refresh"); await loadModels(); status("Model list refreshed.", "ok"); }
    catch (err) { status("Refresh failed: " + err.message, "error"); }
    finally { e.target.disabled = false; }
  };
  $("save").onclick = async (e) => {
    const token = $("token").value.trim();
    if (!token) return status("Paste a token first.", "error");
    e.target.disabled = true;
    try {
      await admin("POST", "/admin/token", { access_token: token });
      $("token").value = "";
      status("Token saved — authenticated.", "ok");
      await loadStatus(); await loadModels();
    } catch (err) { status(err.message, "error"); }
    finally { e.target.disabled = false; }
  };
  $("importBtn").onclick = async (e) => {
    e.target.disabled = true;
    try {
      try { await admin("POST", "/admin/import"); }
      catch (serverError) {
        let res;
        try { res = await fetch("http://127.0.0.1:8001/import-token", { method: "POST" }); }
        catch (netErr) { throw new Error("Browser import needs the proxy on the host, or the host helper (python host_agent.py)."); }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "host import failed");
        await admin("POST", "/admin/reload");
      }
      status("Token imported.", "ok");
      await loadStatus(); await loadModels();
    } catch (err) { status(err.message, "error"); }
    finally { e.target.disabled = false; }
  };
  $("reloadBtn").onclick = async (e) => {
    e.target.disabled = true;
    try { await admin("POST", "/admin/reload"); status("Reloaded from session_data.json.", "ok"); await loadStatus(); }
    catch (err) { status(err.message, "error"); }
    finally { e.target.disabled = false; }
  };
  $("clear").onclick = async (e) => {
    if (!confirm("Remove the saved access token and switch to anonymous mode?")) return;
    e.target.disabled = true;
    try { await admin("POST", "/admin/token", { access_token: "" }); status("Token cleared — anonymous mode.", "ok"); await loadStatus(); }
    catch (err) { status(err.message, "error"); }
    finally { e.target.disabled = false; }
  };

  // ── boot ──────────────────────────────────────────────────────────────
  loadChats();
  app.current = app.chats[0] || null;
  renderHistory();
  renderThread();
  loadStatus();
  loadModels();
  $("input").focus();
})();
</script>
</body>
</html>
"""
