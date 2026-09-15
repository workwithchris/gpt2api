"""Minimal admin UI: status, access-token entry, browser import, model refresh, test call."""

PAGE = """<!doctype html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gpt2api — admin</title>
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
  body { font-feature-settings: "cv02","cv03","cv04","cv11"; -webkit-font-smoothing: antialiased; }
  .grid-bg {
    background-image: radial-gradient(circle at 1px 1px, #1f1f1f 1px, transparent 0);
    background-size: 32px 32px;
  }
</style>
</head>
<body class="min-h-screen bg-black text-neutral-100 font-sans antialiased">

<div class="grid-bg border-b border-neutral-900">
  <main class="mx-auto max-w-3xl px-6 py-12">
    <div class="flex items-center gap-3">
      <div class="h-8 w-8 rounded-md bg-white text-black grid place-items-center font-bold text-sm">g2</div>
      <div>
        <h1 class="text-xl font-semibold tracking-tight">gpt2api</h1>
        <p class="text-sm text-neutral-400">OpenAI-compatible proxy for ChatGPT Web</p>
      </div>
      <span id="mode" class="ml-auto rounded-full border border-neutral-800 px-3 py-1 text-xs font-medium text-neutral-400">…</span>
    </div>
  </main>
</div>

<main class="mx-auto max-w-3xl px-6 py-8 space-y-6">

  <!-- Status -->
  <section class="rounded-xl border border-neutral-800 bg-neutral-950 p-5">
    <div class="flex items-baseline justify-between">
      <h2 class="text-xs font-medium uppercase tracking-wider text-neutral-500">Status</h2>
      <span id="modelCount" class="text-xs text-neutral-500"></span>
    </div>
    <p id="account" class="mt-3 text-sm text-neutral-300">Checking…</p>
    <div class="mt-4 flex flex-wrap gap-2">
      <button id="refreshModels" class="btn">Refresh models</button>
    </div>
  </section>

  <!-- Token -->
  <section class="rounded-xl border border-neutral-800 bg-neutral-950 p-5">
    <h2 class="text-xs font-medium uppercase tracking-wider text-neutral-500">Access token</h2>
    <p class="mt-2 text-sm text-neutral-400">
      Paste the value from <code class="rounded bg-neutral-900 px-1.5 py-0.5 text-neutral-300 text-xs">chatgpt.com/api/auth/session</code>,
      or import it from a logged-in browser.
    </p>
    <div class="mt-4 flex flex-col gap-3 sm:flex-row">
      <input id="token" type="password" autocomplete="off" placeholder="eyJhbGciOi…"
             class="w-full rounded-lg border border-neutral-800 bg-black px-3 py-2 text-sm text-neutral-100 placeholder-neutral-600
                    focus:border-neutral-600 focus:outline-none focus:ring-2 focus:ring-accent/40">
      <button id="save" class="btn btn-primary shrink-0">Save &amp; enable</button>
    </div>
    <div class="mt-4 flex flex-wrap gap-2">
      <button id="importBtn" class="btn btn-primary">Import token from browser</button>
      <button id="reloadBtn" class="btn">Reload from file</button>
      <button id="clear" class="btn btn-danger">Clear token</button>
    </div>
    <p id="importNote" class="mt-3 hidden text-xs leading-relaxed text-neutral-500">
      Running in Docker, so the import is relayed through the host helper. Start it once with
      <code class="rounded bg-neutral-900 px-1.5 py-0.5 text-neutral-300">python host_agent.py</code>,
      then click the button again if it failed.
    </p>
  </section>

  <!-- Test -->
  <section class="rounded-xl border border-neutral-800 bg-neutral-950 p-5">
    <h2 class="text-xs font-medium uppercase tracking-wider text-neutral-500">Test</h2>
    <div class="mt-4 flex flex-col gap-3 sm:flex-row">
      <input id="prompt" type="text" value="Reply with exactly: ok"
             class="w-full rounded-lg border border-neutral-800 bg-black px-3 py-2 text-sm text-neutral-100
                    focus:border-neutral-600 focus:outline-none focus:ring-2 focus:ring-accent/40">
      <button id="test" class="btn shrink-0">Send</button>
    </div>
  </section>

  <!-- Output -->
  <section class="rounded-xl border border-neutral-800 bg-neutral-950 p-5">
    <div class="flex items-center justify-between">
      <h2 class="text-xs font-medium uppercase tracking-wider text-neutral-500">Output</h2>
      <button id="clearLog" class="text-xs text-neutral-500 hover:text-neutral-300">clear</button>
    </div>
    <pre id="out" class="mt-3 max-h-80 overflow-auto whitespace-pre-wrap font-mono text-xs leading-relaxed text-neutral-400">Ready.</pre>
  </section>

  <p class="pb-8 text-center text-xs text-neutral-600">
    base URL <code class="text-neutral-400">/v1</code> · admin routes under <code class="text-neutral-400">/admin/*</code>
  </p>
</main>

<script>
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);

  const btnBase = "rounded-lg border px-3 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40";
  const styles = {
    default: "border-neutral-800 bg-neutral-950 text-neutral-200 hover:bg-neutral-900 hover:border-neutral-700",
    primary: "border-transparent bg-white text-black hover:bg-neutral-200",
    danger: "border-neutral-800 bg-neutral-950 text-red-400 hover:bg-red-950/40 hover:border-red-900",
  };
  document.querySelectorAll("button.btn").forEach((b) => {
    const kind = b.classList.contains("btn-primary") ? "primary" : b.classList.contains("btn-danger") ? "danger" : "default";
    b.classList.remove("btn", "btn-primary", "btn-danger");
    b.className = btnBase + " " + styles[kind] + " " + (b.className || "");
  });

  const ADMIN_KEY = new URLSearchParams(location.search).get("key");
  function headers(extra) {
    const h = Object.assign({ "Content-Type": "application/json" }, extra || {});
    if (ADMIN_KEY) h["X-Admin-Key"] = ADMIN_KEY;
    return h;
  }

  let logLines = [];
  function log(value) {
    const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    logLines.push(text);
    if (logLines.length > 40) logLines = logLines.slice(-40);
    const el = $("out");
    el.textContent = logLines.join("\\n---\\n");
    el.scrollTop = el.scrollHeight;
  }

  async function request(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: headers(),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const raw = await res.text();
    let data;
    try { data = JSON.parse(raw); } catch { data = raw; }
    if (!res.ok) throw new Error(data && data.detail ? data.detail : (raw || res.statusText));
    return data;
  }

  async function withButton(button, fn) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Working…";
    try {
      await fn();
    } catch (e) {
      log("✗ " + (e && e.message ? e.message : e));
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function applyStatus(s) {
    const mode = $("mode");
    mode.textContent = s.authenticated ? "authenticated" : "anonymous";
    mode.className = "ml-auto rounded-full border px-3 py-1 text-xs font-medium " +
      (s.authenticated ? "border-emerald-900 bg-emerald-950/60 text-emerald-400"
                       : "border-amber-900 bg-amber-950/50 text-amber-400");
    const who = s.account ? [s.account.name, s.account.email].filter(Boolean).join(" · ") : "";
    $("account").textContent = s.authenticated
      ? (who || "Authenticated")
      : "Anonymous mode — no account. Paste a token to unlock GPT models, images and files.";
    $("modelCount").textContent = s.model_count ? s.model_count + " models" : "";
    $("importNote").classList.toggle("hidden", !!s.can_import);
  }

  async function loadStatus() {
    try {
      applyStatus(await request("GET", "/admin/status"));
    } catch (e) {
      log("✗ Cannot reach the admin API: " + (e && e.message ? e.message : e));
    }
  }

  $("save").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
    const token = $("token").value.trim();
    if (!token) return log("Paste a token first.");
    log(await request("POST", "/admin/token", { access_token: token }));
    $("token").value = "";
    await loadStatus();
  }));

  // Import: try the server first (works when the proxy runs on the host); if it is refused
  // because we are inside Docker, relay through the host helper on loopback.
  $("importBtn").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
    try {
      log(await request("POST", "/admin/import"));
    } catch (serverError) {
      let res;
      try {
        res = await fetch("http://127.0.0.1:8001/import-token", { method: "POST" });
      } catch (e) {
        throw new Error(
          "Browser import needs the proxy running on the host, or the host helper.\\n" +
          "Start it with:  python host_agent.py\\n\\nServer said: " + serverError.message
        );
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "host import failed");
      log(data);
      log(await request("POST", "/admin/reload"));
    }
    await loadStatus();
  }));

  $("reloadBtn").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
    log(await request("POST", "/admin/reload"));
    await loadStatus();
  }));

  $("clear").addEventListener("click", (ev) => {
    if (!confirm("Remove the saved access token and switch to anonymous mode?")) return;
    withButton(ev.currentTarget, async () => {
      log(await request("POST", "/admin/token", { access_token: "" }));
      await loadStatus();
    });
  });

  $("refreshModels").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
    const data = await request("POST", "/admin/models/refresh");
    log("Models (" + data.count + "): " + data.models.join(", "));
    await loadStatus();
  }));

  $("test").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
    const data = await request("POST", "/admin/test", { prompt: $("prompt").value });
    log(data.model + " → " + data.content);
  }));

  $("clearLog").addEventListener("click", () => { logLines = []; $("out").textContent = "Ready."; });

  loadStatus();
})();
</script>
</body>
</html>
"""
