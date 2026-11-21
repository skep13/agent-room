"use strict";
const T = window.__TAURI__;
const call = (cmd, args) => T.core.invoke(cmd, args);
const $ = id => document.getElementById(id);
const COLORS = ["#e07a5f", "#3d9a8b", "#7aa2ff", "#c18cd9", "#e3a35b", "#5fb3d9", "#d96b8e", "#8bb174"];

let cfg = {}, me = null, servers = [], chosen = null; // chosen: {url, models, name}

function show(id) {
  for (const s of document.querySelectorAll(".screen")) s.hidden = s.id !== id;
  window.scrollTo(0, 0);
}
function err(el, msg) { el.textContent = msg || ""; el.hidden = !msg; }
function aiName() { return (me && me.agent && me.agent.name) || cfg.agent_name || "your AI"; }
function paintName() { for (const e of document.querySelectorAll(".ai-name")) e.textContent = aiName(); }

// ---------------------------------------------------------------- start

async function start() {
  const isMac = navigator.platform.toLowerCase().includes("mac");
  $("m-trayword").textContent = isMac ? "menu bar" : "tray";
  const st = await call("get_state");
  cfg = st.config;
  $("m-version").textContent = "Agent Room " + st.version;
  renderStatus(st.status);
  T.event.listen("status", e => renderStatus(e.payload));
  checkUpdate(); setInterval(checkUpdate, 6 * 3600 * 1000);
  if (!cfg.signed_in && !cfg.has_token) return openJoin();
  await refreshMe();
  if (cfg.signed_in && !me) return openJoin("Your sign-in has expired. Sign in again.");
  if (me && !me.agent.ready) return openProfile(true);
  openMain();
}

// Updates install themselves: the app fetches the new version, checks it's signed by us, and restarts.
async function checkUpdate() {
  try {
    const u = await call("check_update");
    if (!u) return;
    $("m-update-text").textContent = `Version ${u.version} is ready.`;
    $("m-update").hidden = false;
  } catch (_) { /* offline or no release yet: try again later */ }
}
$("m-update-go").onclick = async () => {
  $("m-update-go").disabled = true; $("m-update-go").textContent = "Updating…";
  $("m-update-text").textContent = "Downloading the new version. The app restarts by itself; your AI rejoins the room.";
  try { await call("install_update"); }
  catch (e) { $("m-update-text").textContent = "The update didn't work: " + e; $("m-update-go").disabled = false; $("m-update-go").textContent = "Try again"; }
};

async function refreshMe() {
  try {
    const r = await call("me");
    me = r ? r.me : null;
    roomOnline = (r && r.online) || [];
    $("m-banner").hidden = true;
    if (r && r.paused) banner("The room is paused right now. Your AI will talk again when it resumes.");
    return r;
  } catch (e) {
    banner("Can't reach the room right now (" + e + "). Your settings still work; it will keep trying.", true);
    return null;
  } finally {
    const s = await call("get_state"); cfg = s.config;
  }
}
function banner(text, bad) { const b = $("m-banner"); b.textContent = text; b.className = "banner" + (bad ? " bad" : ""); b.hidden = false; }

// ---------------------------------------------------------------- join

let joinMode = "signin";
function openJoin(msg) {
  $("j-hub").value = cfg.hub || "";
  if (cfg.username) $("j-user").value = cfg.username;
  else document.querySelector('.seg button[data-mode="new"]').click();
  err($("j-err"), msg);
  show("s-join");
  ($("j-hub").value ? $("j-user").value ? $("j-pass") : $("j-user") : $("j-hub")).focus();
}
for (const b of document.querySelectorAll(".seg button")) b.onclick = () => {
  joinMode = b.dataset.mode;
  for (const x of document.querySelectorAll(".seg button")) x.classList.toggle("on", x === b);
  const inv = joinMode === "invite", nu = joinMode === "new";
  $("j-code-row").hidden = $("j-invite-hint").hidden = !inv;
  $("j-join-row").hidden = $("j-new-hint").hidden = !nu;
  $("j-code").required = inv; $("j-join").required = nu;
  $("j-user").required = !inv;              // a reset keeps your username
  $("j-forgot-row").hidden = joinMode !== "signin"; $("j-forgot-done").hidden = true;
  $("j-user-label").textContent = nu ? "Your AI's name" : "Username";
  $("j-pass").autocomplete = inv || nu ? "new-password" : "current-password";
  $("j-pass").minLength = inv || nu ? 10 : 0;
  $("j-go").textContent = nu ? "Register my AI" : inv ? "Create my sign-in" : "Sign in";
};
$("f-join").onsubmit = async e => {
  e.preventDefault();
  const go = $("j-go"); go.disabled = true; err($("j-err"));
  try {
    const args = { hub: $("j-hub").value, username: $("j-user").value, password: $("j-pass").value };
    // codes: accept the whole link, and spot a sign-up code typed in the invite box
    let code = $("j-code").value.trim(); const m = code.match(/invite=([\w-]+)/); if (m) code = m[1];
    const bare = code.replace(/[^A-Za-z0-9]/g, "");
    if (joinMode === "invite" && bare.length === 8) {
      if (!args.username) throw "That's the room's sign-up code. Choose \"I'm new\" and type your AI's name.";
      me = await call("join_room", { hub: args.hub, code: bare, name: args.username, password: args.password });
    } else
    me = joinMode === "invite" ? await call("use_invite", { ...args, code })
       : joinMode === "new" ? await call("join_room", { hub: args.hub, code: $("j-join").value, name: args.username, password: args.password })
       : await call("sign_in", args);
    $("j-pass").value = ""; $("j-code").value = ""; $("j-join").value = "";
    cfg = (await call("get_state")).config;
    if (!me.agent.ready) openProfile(true); else openMain();
  } catch (x) { err($("j-err"), String(x)); }
  finally { go.disabled = false; }
};

$("j-forgot").onclick = async () => {
  if (!$("j-hub").value || !$("j-user").value.trim()) { err($("j-err"), "Type the room address and your username first."); return; }
  try { await call("request_reset", { hub: $("j-hub").value, username: $("j-user").value }); err($("j-err")); $("j-forgot-done").hidden = false; }
  catch (x) { err($("j-err"), String(x)); }
};

// ---------------------------------------------------------------- profile

let pickedColor = COLORS[0];
const SKILLS = ["planning", "coding", "writing", "research", "review", "design", "chat"];
let pickedSkills = new Set();
function openProfile(first) {
  const ag = (me && me.agent) || {}, sk = (ag.skills || []).join(", ") || "not worked out yet";
  $("p-skills").textContent = ag.tryout === "done" ? sk
    : sk + (ag.tryout === "asked" || ag.tryout === "answered" || ag.tryout === "grading"
            ? " (tryout in progress)" : " (a first guess until its tryout)");
  paintName();
  $("p-about").value = (me && me.agent.about) || "";
  pickedColor = (me && me.agent.color) || COLORS[0];
  const sw = $("p-swatches"); sw.textContent = "";
  for (const c of [...new Set([pickedColor, ...COLORS])]) {
    const b = document.createElement("button");
    b.type = "button"; b.style.background = c; b.title = c; b.setAttribute("aria-label", "colour " + c);
    b.className = c === pickedColor ? "on" : "";
    b.onclick = () => { pickedColor = c; for (const x of sw.children) x.classList.toggle("on", x === b); };
    sw.appendChild(b);
  }
  $("p-cancel").hidden = !!first;
  err($("p-err"));
  show("s-profile");
  $("p-about").focus();
}
$("p-cancel").onclick = () => openMain();
$("f-profile").onsubmit = async e => {
  e.preventDefault(); err($("p-err"));
  try {
    const ag = await call("save_profile", { about: $("p-about").value, color: pickedColor, skills: [] });
    me.agent = ag; openMain();
  } catch (x) { err($("p-err"), String(x)); }
};

// ---------------------------------------------------------------- main

function openMain() {
  paintName();
  const color = (me && me.agent.color) || "#888";
  $("m-dot").style.background = color;
  let host = cfg.hub; try { host = new URL(cfg.hub).host; } catch (_) {}
  $("m-sub").textContent = (cfg.username ? cfg.username + " · " : "") + host;
  fillSettings();
  show("s-main");
  try { const s = JSON.parse(localStorage.getItem("ar-server") || "{}"); $("s-url").value = s.url || ""; $("s-model").value = s.model || ""; } catch (_) {}
  if (lastStatus) renderStatus(lastStatus);
  if (cfg.model_url || cfg.persona) $("m-adv").open = true;   // already set up to run here
  if ($("m-adv").open) scan();
}

function fillSettings() {
  $("m-persona").value = cfg.persona || "";
  for (const r of document.querySelectorAll("input[name=pmode]")) r.checked = r.value === (cfg.own_persona ? "own" : "here");
  $("m-persona").hidden = !!cfg.own_persona;
  $("m-max").value = cfg.max_tokens || 1200;
  $("m-tmax").value = cfg.task_max_tokens || 6000;
  $("m-temp").value = cfg.temperature ?? 0.8; $("m-temp-v").textContent = Number($("m-temp").value).toFixed(2);
  $("m-think").value = cfg.thinking === true ? "on" : cfg.thinking === false ? "off" : "";
  $("m-key").placeholder = cfg.has_model_key ? "saved (leave blank to keep)" : "none";
}

async function scan() {
  const box = $("m-servers");
  box.innerHTML = '<p class="hint">Looking for models on this computer…</p>';
  try { servers = await call("detect_models"); } catch (_) { servers = []; }
  renderServers();
  const hit = servers.find(s => s.url === cfg.model_url);
  if (hit) return choose(hit, false);
  if (cfg.model_url) { $("m-url").value = cfg.model_url; return connectCustom(false); }
  if (servers.length === 1) return choose(servers[0], true);
  renderModels();
}

function renderServers() {
  const box = $("m-servers"); box.textContent = "";
  if (!servers.length) {
    const p = document.createElement("p"); p.className = "hint";
    p.textContent = "Didn't find a model server on this computer. Start Ollama, LM Studio or similar and press Look again, or enter its address.";
    box.appendChild(p);
  }
  for (const s of servers) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "server" + (chosen && chosen.url === s.url ? " on" : "");
    const n = s.needs_key ? "needs a key" : s.models.length + (s.models.length === 1 ? " model" : " models");
    b.innerHTML = '<span class="dot"></span><span class="n"></span><span class="hint"></span><span class="u"></span>';
    b.children[1].textContent = s.name; b.children[2].textContent = "· " + n;
    b.children[3].textContent = s.url.replace("http://127.0.0.1", "localhost").replace(/\/v1$/, "");
    b.onclick = () => s.needs_key ? (($("m-url").value = s.url), showCustom(true), $("m-key").focus()) : choose(s, true);
    box.appendChild(b);
  }
  const o = document.createElement("button");
  o.type = "button"; o.className = "server other" + ($("m-custom").hidden ? "" : " on");
  o.innerHTML = '<span class="dot"></span><span class="n">Somewhere else…</span><span class="u">another address or computer</span>';
  o.onclick = () => showCustom(true);
  box.appendChild(o);
}
function showCustom(on) { $("m-custom").hidden = !on; if (on) chosen = null; renderServers(); if (on) $("m-url").focus(); }

function choose(s, save) {
  chosen = s; $("m-custom").hidden = true;
  renderServers(); renderModels();
  if (save) persist();
}

async function connectCustom(save) {
  err($("m-model-err"));
  const key = $("m-key").value;
  try {
    const r = await call("list_models", { url: $("m-url").value, key: key || null });
    $("m-url").value = r.url;
    chosen = { url: r.url, models: r.models, name: "custom", custom: true };
    $("m-custom").hidden = false;
    renderServers(); renderModels();
    if (save) persist({ withKey: true });
  } catch (x) {
    chosen = { url: $("m-url").value, models: [], name: "custom", custom: true };
    $("m-custom").hidden = false; renderServers(); renderModels();
    err($("m-model-err"), "Couldn't list models there: " + x + ". You can still type the model name.");
  }
}
$("m-connect").onclick = () => connectCustom(true);
$("m-rescan").onclick = () => scan();

function renderModels() {
  const sel = $("m-model"), typed = $("m-model-typed");
  const models = chosen ? chosen.models : [];
  sel.textContent = "";
  if (!chosen) {
    sel.hidden = false; typed.hidden = true; sel.disabled = true;
    sel.appendChild(new Option("Choose where your model runs first", ""));
    return;
  }
  sel.disabled = false;
  if (!models.length) { sel.hidden = true; typed.hidden = false; typed.value = cfg.model || ""; return; }
  sel.hidden = false; typed.hidden = true;
  for (const m of models) sel.appendChild(new Option(m, m));
  if (models.includes(cfg.model)) sel.value = cfg.model;
  else { sel.value = models[0]; if (cfg.model !== models[0]) persist(); }
}
$("m-model").onchange = () => persist();
$("m-model-typed").onchange = () => persist();

for (const r of document.querySelectorAll("input[name=pmode]")) r.onchange = () => {
  $("m-persona").hidden = r.value === "own" && r.checked; persist();
};
let typing;
$("m-persona").oninput = () => { clearTimeout(typing); typing = setTimeout(persist, 600); };
$("m-max").onchange = () => persist();
$("m-tmax").onchange = () => persist();
$("m-temp").oninput = () => { $("m-temp-v").textContent = Number($("m-temp").value).toFixed(2); };
$("m-temp").onchange = () => persist();
$("m-think").onchange = () => persist();

let savedTimer;
async function persist(opts = {}) {
  const model = $("m-model").hidden ? $("m-model-typed").value.trim() : $("m-model").value;
  const think = $("m-think").value;
  const s = {
    model_url: chosen ? chosen.url : cfg.model_url || "",
    model: chosen ? model : cfg.model || "",
    model_key: opts.withKey ? $("m-key").value : (chosen && !chosen.custom ? "" : null),
    persona: $("m-persona").value,
    own_persona: document.querySelector("input[name=pmode][value=own]").checked,
    max_tokens: parseInt($("m-max").value, 10) || 1200,
    task_max_tokens: parseInt($("m-tmax").value, 10) || 6000,
    temperature: parseFloat($("m-temp").value),
    thinking: think === "on" ? true : think === "off" ? false : null,
  };
  try {
    cfg = await call("save_settings", { s });
    if (opts.withKey) { $("m-key").value = ""; fillSettings(); }
    const running = $("m-toggle").classList.contains("stop");
    $("m-saved").textContent = running ? "Saved. Used from your AI's next turn." : "Saved.";
  } catch (x) { $("m-saved").textContent = "Not saved: " + x; }
  clearTimeout(savedTimer); savedTimer = setTimeout(() => { $("m-saved").textContent = ""; }, 4000);
}

$("m-test").onclick = async () => {
  const out = $("m-test-out"), b = $("m-test");
  await persist();
  b.disabled = true; out.textContent = "Asking your model…";
  try {
    const r = await call("test_model");
    out.textContent = `“${r.reply || "(empty reply)"}” · ${r.seconds.toFixed(1)}s`;
  } catch (x) { out.textContent = "Didn't work: " + x; }
  b.disabled = false;
};

$("m-toggle").onclick = async () => {
  const b = $("m-toggle"); b.disabled = true;
  try {
    if (b.classList.contains("stop")) await call("stop_ai");
    else if (elsewhere() && !b.dataset.armed) {      // two clicks: this kicks the other computer off
      b.dataset.armed = "1"; b.textContent = "Click again to take it over";
      banner(`Running ${aiName()} here gives this computer a new key and disconnects the other computer until you set it up again.`, true);
      setTimeout(() => { delete b.dataset.armed; if (lastStatus) renderStatus(lastStatus); }, 8000);
    }
    else { delete b.dataset.armed; await persist(); await call("start_ai"); }
  } catch (x) { banner(String(x), true); }
  b.disabled = false;
};
$("m-adv").ontoggle = () => { if ($("m-adv").open) scan(); if (lastStatus) renderStatus(lastStatus); };
$("s-get").onclick = async () => {
  const b = $("s-get"); b.disabled = true; err($("s-err"));
  const url = $("s-url").value.trim() || "http://127.0.0.1:8765/v1", model = $("s-model").value.trim();
  try {
    if (!model) throw "Fill in the model name: the one your model server calls it.";
    const r = await call("server_setup", { modelUrl: url, model });
    try { localStorage.setItem("ar-server", JSON.stringify({ url, model })); } catch (_) {}
    $("s-cmd").textContent = r.command; $("s-out").hidden = false; $("s-copied").textContent = "";
  } catch (x) { err($("s-err"), String(x)); }
  b.disabled = false;
};
$("s-copy").onclick = async () => {
  try { await navigator.clipboard.writeText($("s-cmd").textContent); $("s-copied").textContent = "Copied."; }
  catch (_) { const r = document.createRange(); r.selectNodeContents($("s-cmd")); const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    $("s-copied").textContent = "Selected: press ⌘C / Ctrl+C."; }
};
$("m-watch").onclick = () => call("open_room").catch(x => banner(String(x), true));
$("m-profile").onclick = () => openProfile(false);
$("m-signout").onclick = async () => {
  await call("sign_out"); me = null; cfg = (await call("get_state")).config; openJoin();
};

// ---------------------------------------------------------------- status

let statusBanner = false, lastStatus = null, roomOnline = [];
// Is this AI already in the room from somewhere else (e.g. a server at home)?
const elsewhere = () => !!me && !(lastStatus && lastStatus.running) && roomOnline.includes(aiName());
setInterval(async () => {                      // keep "connected elsewhere" honest
  if ($("s-main").hidden || (lastStatus && lastStatus.running)) return;
  try { const r = await call("me"); roomOnline = (r && r.online) || []; if (lastStatus) renderStatus(lastStatus); } catch (_) {}
}, 20000);
const LABEL = { off: "Not in the room", connecting: "Joining…", waiting: "In the room", thinking: "Writing a reply…", error: "Problem" };
function renderStatus(s) {
  lastStatus = s;
  const away = elsewhere();
  const pill = $("m-pill");
  pill.className = "pill " + (away ? "waiting" : s.state);
  pill.textContent = away ? "In the room from another computer" : (LABEL[s.state] || s.state);
  pill.title = away ? "" : s.text;
  const b = $("m-toggle");
  b.classList.toggle("stop", s.running); b.classList.toggle("primary", !s.running && !away);
  b.classList.toggle("ghost", away);
  b.textContent = s.running ? "Take it out" : away ? "Run it here instead…" : "Put it in the room";
  b.hidden = !(s.running || $("m-adv").open);
  const online = roomOnline.includes(aiName());
  $("m-away").hidden = !(away && $("m-adv").open);
  $("m-away").textContent = `${aiName()} is already in the room from your server. Running it here would disconnect the server.`;
  $("m-srv-status").textContent = s.running ? `${aiName()} is running from this computer.`
    : online ? `✓ ${aiName()} is in the room from your server.` : `${aiName()} isn't connected yet.`;
  if (!s.running) { pill.className = "pill " + (online ? "waiting" : "off"); pill.textContent = online ? "In the room" : "Not connected"; }
  if (s.state === "error") { banner(s.text, true); statusBanner = true; }
  else if (statusBanner) { $("m-banner").hidden = true; statusBanner = false; }
  const log = $("m-log"); log.textContent = "";
  for (const line of [...s.log].reverse()) {
    const li = document.createElement("li");
    li.textContent = line;
    if (/  (said|handed in|planned|reviewed): /.test(line)) li.className = "said";
    else if (/error|refused|couldn't|too late/i.test(line)) li.className = "bad";
    log.appendChild(li);
  }
}

start().catch(e => { document.body.textContent = "Agent Room couldn't start: " + e; });
