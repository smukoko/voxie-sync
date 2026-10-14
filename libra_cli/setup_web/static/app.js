// Voxie setup: one step on screen at a time, each calling the local server
// (libra_cli/setup_web/server.py). The session key arrives in the #fragment,
// is kept for this tab only, and goes with every request.
"use strict";

const $ = (id) => document.getElementById(id);
const STEPS = ["welcome", "account", "server", "narration", "kobo", "done"];
const RAIL = ["account", "server", "narration", "kobo"];

let key = new URLSearchParams(location.hash.slice(1)).get("k");
try {
  if (key) sessionStorage.setItem("voxie-key", key);
  else key = sessionStorage.getItem("voxie-key");
} catch (_) { /* private mode: the fragment still works for this load */ }
history.replaceState(null, "", location.pathname);   // the key leaves the address bar

let state = {};
let current = "welcome";
const done = new Set();

// ---------------------------------------------------------------- api --
async function api(path, options = {}) {
  const headers = { "X-Voxie-Key": key || "" };
  let body = options.body;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, { method: options.method || (body ? "POST" : "GET"), headers, body });
  } catch (_) {
    $("session-lost").hidden = false;
    throw new Error("Voxie is not running any more.");
  }
  if (res.status === 403) { $("session-lost").hidden = false; throw new Error("forbidden"); }
  if (res.status === 409) { go("account"); throw new Error("Choose your login first."); }
  return res.json();
}

// Polls a long job, feeding each new log line to onLine. Resolves with its result.
async function follow(jobId, onLine) {
  let from = 0;
  for (;;) {
    const v = await api(`/api/jobs/${jobId}?from=${from}`);
    v.lines.forEach(onLine);
    from = v.next;
    if (v.status === "done") return v.result;
    if (v.status === "failed") throw new Error(v.error);
    await new Promise((r) => setTimeout(r, 700));
  }
}

// ------------------------------------------------------------- helpers --
function setStatus(el, text, kind) {
  el.textContent = text || "";
  el.className = "status" + (kind ? " " + kind : "");
}
function fieldError(form, name, text) {
  const el = form.querySelector(`[data-error-for="${name}"]`);
  if (el) el.textContent = text || "";
}
function clearErrors(scope) {
  scope.querySelectorAll(".error").forEach((e) => (e.textContent = ""));
}
function busy(button, on, label) {
  if (on) { button.dataset.label = button.textContent; button.textContent = label; }
  else if (button.dataset.label) button.textContent = button.dataset.label;
  button.disabled = on;
}

// Milestones: known log lines tick a checklist, so progress reads as steps.
function milestones(listEl, items) {
  listEl.replaceChildren(...items.map((m) => {
    const li = document.createElement("li");
    li.textContent = m.label;
    return li;
  }));
  let reached = -1;
  return {
    see(line) {
      items.forEach((m, i) => { if (i > reached && m.match.some((s) => line.includes(s))) reached = i; });
      [...listEl.children].forEach((li, i) => {
        li.className = i < reached ? "done" : i === reached ? "active" : "";
      });
      return (reached + 1) / items.length;
    },
    finish() { [...listEl.children].forEach((li) => (li.className = "done")); },
  };
}
function logTo(pre) {
  return (line) => {
    pre.textContent += line.replace(/[\u{1F300}-\u{1FAFF}☀-➿️]/gu, "").trimEnd() + "\n";
    pre.scrollTop = pre.scrollHeight;
  };
}

// ---------------------------------------------------------- navigation --
function go(step) {
  current = step;
  STEPS.forEach((s) => (document.querySelector(`[data-step="${s}"]`).hidden = s !== step));
  const onRail = RAIL.includes(step);
  document.querySelector(".rail").hidden = !onRail;
  document.querySelector(".frame").classList.toggle("no-rail", !onRail);
  document.querySelectorAll("[data-rail]").forEach((li) => {
    const s = li.dataset.rail;
    li.classList.toggle("current", s === step);
    li.classList.toggle("done", done.has(s) && s !== step);
    if (s === step) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  });
  try { sessionStorage.setItem("voxie-step", step); } catch (_) {}
  window.scrollTo(0, 0);
  document.querySelector(`[data-step="${step}"] h1`).focus();
  if (step === "kobo") startKoboWatch(); else stopKoboWatch();
  if (step === "done") fillDone();
}
document.addEventListener("click", (e) => {
  const to = e.target.closest("[data-go]");
  if (to) go(to.dataset.go);
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    navigator.clipboard.writeText($(copy.dataset.copy).textContent).then(() => {
      copy.textContent = "Copied";
      setTimeout(() => (copy.textContent = "Copy"), 1600);
    });
  }
  if (e.target.closest("[data-open-docker]")) api("/api/docker/open", { body: {} });
});

// ------------------------------------------------------------- account --
function initAccount() {
  $("username").value = state.username || "";
  if (state.hasPassword) {
    $("keep-password-row").hidden = false;
    $("keep-password").checked = true;
    $("password").disabled = true;
  }
  $("keep-password").addEventListener("change", (e) => {
    $("password").disabled = e.target.checked;
    if (!e.target.checked) $("password").focus();
  });
  document.querySelector('[data-toggle="password"]').addEventListener("click", (e) => {
    const shown = $("password").type === "text";
    $("password").type = shown ? "password" : "text";
    e.target.textContent = shown ? "Show" : "Hide";
    e.target.setAttribute("aria-pressed", String(!shown));
  });
  $("account-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    clearErrors(form);
    const r = await api("/api/account", { body: {
      username: $("username").value.trim(),
      password: $("keep-password").checked ? "" : $("password").value,
      keepPassword: $("keep-password").checked,
    } });
    if (!r.ok) { fieldError(form, r.field, r.error); $(r.field).focus(); return; }
    accountChanged = !$("keep-password").checked || $("username").value.trim() !== state.username;
    $("keep-server").hidden = !state.configured || accountChanged;
    done.add("account");
    go("server");
  });
}

// -------------------------------------------------------------- server --
const SERVER_MILESTONES = [
  { label: "Copying Voxie to the server", match: ["Copying Voxie"] },
  { label: "Installing Docker", match: ["Installing Docker"] },
  { label: "Starting Voxie and Audiobookshelf", match: ["Starting the gateway"] },
  { label: "Preparing Audiobookshelf", match: ["Audiobookshelf"] },
  { label: "Creating your account", match: ["Account '"] },
  { label: "Checking it all answers", match: ["Server ready", "certificate"] },
];

let sshOk = false;
let accountChanged = true;
function guideStep(n, ok) {
  document.querySelectorAll("#server-new .guide > li")[n].classList.toggle("ok", ok);
}
function showKey(text) {
  $("key-present").hidden = !text;
  $("key-missing").hidden = !!text;
  $("ssh-key").textContent = text || "";
  guideStep(0, !!text);
}
function initServer() {
  $("hetzner-link").href = state.links.hetzner;
  showKey(state.sshKey);
  $("server-ip").value = state.serverHost || "";
  $("domain").value = state.domain || "";
  $("existing-address").value = state.existingAddress || "";
  $("existing-ssh").value = state.serverSsh || "";
  if (state.configured && !state.serverSsh) {
    document.querySelector('input[name="server-mode"][value="existing"]').checked = true;
  }
  const syncMode = () => {
    const mode = document.querySelector('input[name="server-mode"]:checked').value;
    $("server-new").hidden = mode !== "new";
    $("server-existing").hidden = mode !== "existing";
  };
  document.querySelectorAll('input[name="server-mode"]').forEach((r) => r.addEventListener("change", syncMode));
  syncMode();

  $("make-key").addEventListener("click", async (e) => {
    busy(e.target, true, "Creating…");
    const r = await api("/api/ssh/key", { body: {} });
    busy(e.target, false);
    if (r.ok) showKey(r.sshKey); else $("make-key-error").textContent = r.error;
  });

  $("server-ip").addEventListener("input", () => { sshOk = false; guideStep(2, false); updateInstall(); });
  $("ssh-check").addEventListener("click", async (e) => {
    const status = $("ssh-status");
    setStatus(status, "Connecting…", "busy");
    busy(e.target, true, "Connecting…");
    const r = await api("/api/ssh/check", { body: { host: $("server-ip").value } });
    busy(e.target, false);
    sshOk = r.ok;
    guideStep(1, r.ok);
    guideStep(2, r.ok);
    setStatus(status, r.ok ? "Connected. Voxie can set this server up." : r.error, r.ok ? "ok" : "bad");
    updateInstall();
  });

  $("dns-check").addEventListener("click", async (e) => {
    const domain = $("domain").value.trim().toLowerCase();
    const status = $("dns-status");
    if (!domain) { setStatus(status, "", ""); $("dns-record").hidden = true; return; }
    setStatus(status, "Looking it up…", "busy");
    const r = await api("/api/dns/check", { body: { domain, host: $("server-ip").value } });
    $("dns-name").textContent = domain;
    $("dns-value").textContent = r.ip;
    $("dns-record").hidden = r.ok;
    guideStep(3, r.ok);
    setStatus(status, r.ok ? `${domain} points at your server.` : `${domain} does not point at ${r.ip} yet.`, r.ok ? "ok" : "bad");
    updateInstall();
  });
  $("domain").addEventListener("input", () => { guideStep(3, false); $("dns-record").hidden = true; setStatus($("dns-status"), "", ""); updateInstall(); });

  $("server-install").addEventListener("click", installServer);
  $("keep-server-btn").addEventListener("click", () => { state.absUrl = state.currentAbsUrl; done.add("server"); go("narration"); });
  initExisting();
}
function updateInstall() {
  const domainPending = $("domain").value.trim() && !document.querySelectorAll("#server-new .guide > li")[3].classList.contains("ok");
  $("server-install").disabled = !sshOk || !!domainPending;
  $("server-install").title = domainPending ? "Check the domain first, or clear it." : "";
}
async function installServer(e) {
  const button = e.currentTarget;
  busy(button, true, "Setting up…");
  $("server-error").textContent = "";
  $("server-progress").hidden = false;
  $("server-log").textContent = "";
  const domain = $("domain").value.trim();
  $("server-summary").textContent = `Setting up ${$("server-ip").value.trim()}` + (domain ? ` as ${domain}` : "") + ". This takes a few minutes the first time.";
  document.querySelector("#server-new .guide").hidden = true;
  $("server-choice").hidden = true;
  const steps = milestones($("server-milestones"), SERVER_MILESTONES.filter(
    (m) => $("domain").value.trim() || !m.match.includes("certificate")));
  const log = logTo($("server-log"));
  try {
    const { job } = await api("/api/server/install", { body: { host: $("server-ip").value, domain: $("domain").value.trim() } });
    const result = await follow(job, (line) => { log(line); steps.see(line); });
    steps.finish();
    state.absUrl = result.absUrl;
    done.add("server");
    button.textContent = "Continue";
    button.disabled = false;
    button.removeEventListener("click", installServer);
    button.addEventListener("click", () => go("narration"));
  } catch (err) {
    $("server-error").textContent = `The setup stopped: ${err.message}. The details above say where; fix it and press the button again.`;
    document.querySelector("#server-new .guide").hidden = false;
    $("server-choice").hidden = false;
    busy(button, false);
  }
}
function initExisting() {
  $("existing-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    clearErrors(form);
    const button = form.querySelector('button[type="submit"]');
    busy(button, true, $("existing-ssh").value.trim() ? "Linking the server…" : "Checking…");
    const r = await api("/api/server/existing", { body: { address: $("existing-address").value,
      absKey: $("abs-key").value, ssh: $("existing-ssh").value } });
    busy(button, false);
    if (!r.ok) { fieldError(form, r.field, r.error); return; }
    state.absUrl = r.absUrl;
    // With a login the server's part is already done: on to narration.
    if (r.linked) { done.add("server"); go("narration"); return; }
    $("env-line").textContent = r.envLine;
    $("add-user").textContent = r.addUser;
    form.hidden = true;
    $("existing-done").hidden = false;
  });
  $("existing-confirm").addEventListener("change", (e) => ($("existing-continue").disabled = !e.target.checked));
  $("existing-continue").addEventListener("click", () => { done.add("server"); go("narration"); });
}

// ----------------------------------------------------------- narration --
function narrationMode() { return document.querySelector('input[name="narration"]:checked').value; }
function initNarration() {
  $("runpod-link").href = state.links.runpod;
  $("disclosure").textContent = state.links.disclosure;
  if (state.provider === "kokoro" && state.configured) {
    document.querySelector('input[name="narration"][value="kokoro"]').checked = true;
  }
  if (state.hasRunpodKey) {
    $("keep-runpod-row").hidden = false;
    $("runpod-key-field").hidden = true;
  }
  $("keep-runpod").addEventListener("change", (e) => ($("runpod-key-field").hidden = e.target.checked));
  $("openrouter-link").href = state.links.openrouter;
  if (state.hasOpenRouterKey) {
    $("keep-openrouter-row").hidden = false;
    $("openrouter-key-field").hidden = true;
  }
  $("keep-openrouter").addEventListener("change", (e) => ($("openrouter-key-field").hidden = e.target.checked));
  if (!state.publicTemplate) {
    $("template-field").hidden = false;
    $("template-id").value = state.templateId || "";
  }
  const sync = () => {
    const runpod = narrationMode() === "runpod";
    $("runpod-fields").hidden = !runpod;
    $("voice-block").hidden = !runpod;
    $("docker-notice").hidden = true;
  };
  document.querySelectorAll('input[name="narration"]').forEach((r) => r.addEventListener("change", sync));
  sync();
  showVoices(state.voices);

  document.querySelectorAll('input[name="voice-source"]').forEach((r) => r.addEventListener("change", () => {
    const file = document.querySelector('input[name="voice-source"]:checked').value === "file";
    $("voice-url-field").hidden = file;
    $("voice-file-field").hidden = !file;
  }));
  $("voice-form").addEventListener("submit", addVoice);
  $("voice-keep").addEventListener("click", keepVoice);
  $("voice-again").addEventListener("click", tryAgain);
  $("voice-more").addEventListener("click", () => { $("voice-form-wrap").hidden = false; $("voice-more").hidden = true; $("voice-url").focus(); });
  $("narration-save").addEventListener("click", saveNarration);
}
function showVoices(voices) {
  const has = voices && voices.length;
  $("voice-list").hidden = !has;
  $("voice-names").textContent = has ? voices.join(", ") : "";
  $("voice-form-wrap").hidden = !!has;
  $("voice-form-wrap").querySelector("p").textContent = has
    ? "Add another voice if you like."
    : "No voice comes with Voxie. Use 8 to 15 seconds of someone speaking clearly: your own voice, or someone who agreed to it.";
}
async function addVoice(e) {
  e.preventDefault();
  const fromFile = document.querySelector('input[name="voice-source"]:checked').value === "file";
  const consent = document.querySelector('input[name="consent"]:checked');
  const err = $("voice-error");
  err.textContent = "";
  if (!consent) { err.textContent = "Say whose voice it is."; return; }
  if (!$("voice-text").value.trim()) { err.textContent = "Type exactly what is said in the clip."; return; }
  const data = new FormData();
  if (fromFile) {
    if (!$("voice-file").files[0]) { err.textContent = "Choose a file."; return; }
    data.append("file", $("voice-file").files[0]);
  } else {
    if (!$("voice-url").value.trim()) { err.textContent = "Paste a link."; return; }
    data.append("url", $("voice-url").value.trim());
  }
  data.append("start", $("voice-start").value.trim());
  data.append("end", $("voice-end").value.trim());
  data.append("text", $("voice-text").value.trim());
  data.append("name", $("voice-name").value.trim());
  data.append("consent", consent.value);
  const button = $("voice-add");
  busy(button, true, "Listening…");
  $("voice-progress").hidden = false;
  setStatus($("voice-status"), "Cutting the clip and listening to it…", "busy");
  try {
    const started = await api("/api/voice", { body: data });
    if (!started.job) throw new Error(started.error || "Voxie could not start on that clip.");
    const r = await follow(started.job, () => {});
    setStatus($("voice-status"), "", "");
    $("voice-progress").hidden = true;
    await showPreview(r);
  } catch (x) {
    setStatus($("voice-status"), "", "");
    err.textContent = x.message;
  }
  busy(button, false);
}
// The checked clip waits in a staging folder until "Keep": you hear what the
// narrator will copy, and what the check found, before it becomes a voice.
let stagedVoice = null;
async function showPreview(r) {
  stagedVoice = r;
  const res = await fetch(`/api/voice/preview/${r.staged}`, { headers: { "X-Voxie-Key": key || "" } });
  if (res.ok) {
    const old = $("voice-audio").src;
    $("voice-audio").src = URL.createObjectURL(await res.blob());
    if (old) URL.revokeObjectURL(old);
  }
  $("voice-warnings").replaceChildren(...(r.warnings || []).map((w) => {
    const li = document.createElement("li");
    li.textContent = w;
    return li;
  }));
  $("voice-keep-error").textContent = "";
  $("voice-form").hidden = true;
  $("voice-preview").hidden = false;
  $("voice-keep").focus();
}
function closePreview() {
  stagedVoice = null;
  $("voice-preview").hidden = true;
  $("voice-form").hidden = false;
  $("voice-audio").pause();
}
async function keepVoice() {
  if (!stagedVoice) return;
  const button = $("voice-keep");
  busy(button, true, "Keeping…");
  try {
    const r = await api("/api/voice/keep", { body: { staged: stagedVoice.staged } });
    if (!r.ok) { $("voice-keep-error").textContent = r.error; busy(button, false); return; }
    const seconds = stagedVoice.seconds;
    closePreview();
    state.voices = [...(state.voices || []), r.name];
    showVoices(state.voices);
    $("voice-more").hidden = false;
    setStatus($("voice-added"), `Added “${r.name}”, ${seconds} seconds.`, "ok");
    $("voice-form").reset();
  } catch (x) {
    $("voice-keep-error").textContent = x.message;
  }
  busy(button, false);
}
async function tryAgain() {
  if (stagedVoice) api("/api/voice/discard", { body: { staged: stagedVoice.staged } }).catch(() => {});
  closePreview();
  $("voice-text").focus();
}
async function saveNarration(e) {
  const button = e.currentTarget;
  const err = $("narration-error");
  err.textContent = "";
  const mode = narrationMode();
  const payload = { provider: mode };
  if (mode === "runpod") {
    const keep = state.hasRunpodKey && $("keep-runpod").checked;
    if (!keep) {
      const k = $("runpod-key").value.trim();
      if (!k) { fieldError(document, "runpod", "Paste your RunPod API key."); return; }
      busy(button, true, "Checking the key…");
      const c = await api("/api/runpod/check", { body: { key: k } });
      busy(button, false);
      if (!c.ok) { fieldError(document, "runpod", c.error); return; }
      payload.key = k;
    }
    payload.template = $("template-id").value.trim();
    const keepDirector = state.hasOpenRouterKey && $("keep-openrouter").checked;
    if (!keepDirector) {
      const o = $("openrouter-key").value.trim();
      if (o) {
        busy(button, true, "Checking the key…");
        const c = await api("/api/openrouter/check", { body: { key: o } });
        busy(button, false);
        if (!c.ok) { fieldError(document, "openrouter", c.error); return; }
        payload.openrouterKey = o;
      } else if (!confirm("Without an OpenRouter key the narrator reads without direction. Continue anyway?")) {
        $("openrouter-key").focus();
        return;
      }
    }
    if (!state.voices || !state.voices.length) {
      if (!confirm("No voice yet: RunPod cannot narrate until you add one. Continue anyway?")) return;
    }
  }
  busy(button, true, "Saving…");
  const r = await api("/api/narration", { body: payload });
  if (!r.ok) { err.textContent = r.error; busy(button, false); return; }
  if (mode === "kokoro" && !r.docker) $("docker-notice").hidden = false;
  button.textContent = "Starting the watcher…";
  const w = await api("/api/watcher", { body: {} });
  busy(button, false);
  if (!w.ok) { err.textContent = `Saved, but the watcher did not start: ${w.error}`; return; }
  done.add("narration");
  go("kobo");
}

// ---------------------------------------------------------------- kobo --
const KOBO_MILESTONES = [
  { label: "KOReader, the reading app", match: ["Downloading KOReader", "KOReader already installed"] },
  { label: "The Voxie button", match: ["NickelMenu", "bottom bar"] },
  { label: "Sync with your server", match: ["Sidecar Plugin injected", "Injecting"] },
  { label: "The look you chose", match: ["Kobo look", "KOReader's own look", "Progress sync waits"] },
  { label: "Saving everything to the Kobo", match: ["Flushing all writes"] },
];
let koboTimer = null;
let installing = false;
function stage(stateName) { document.querySelector('[data-step="kobo"] .kobo-stage').dataset.state = stateName; }
function startKoboWatch() {
  if (!state.isMac) {
    document.querySelector('[data-step="kobo"] .kobo-stage').hidden = true;
    $("kobo-not-mac").hidden = false;
    return;
  }
  if (koboTimer || installing) return;
  const tick = async () => {
    let r;
    try { r = await api("/api/kobo"); } catch (_) { return; }
    if (installing) return;
    $("kobo-docker").hidden = r.docker;
    if (r.connected) {
      stage("connected");
      setStatus($("kobo-status"), `${r.name} is connected.`, "ok");
      $("kobo-install").hidden = false;
      $("kobo-install").disabled = !r.docker;
      $("kobo-look").hidden = false;
    } else {
      stage("waiting");
      setStatus($("kobo-status"), "Waiting for your Kobo…", "busy");
      $("kobo-install").hidden = true;
      $("kobo-look").hidden = true;
    }
  };
  tick();
  koboTimer = setInterval(tick, 2000);
}
function stopKoboWatch() { clearInterval(koboTimer); koboTimer = null; }
async function installKobo(e) {
  const button = e.currentTarget;
  installing = true;
  stopKoboWatch();
  busy(button, true, "Installing…");
  $("kobo-error").textContent = "";
  $("kobo-progress").hidden = false;
  $("kobo-log").textContent = "";
  setStatus($("kobo-status"), "Keep the cable in until this is done.", "busy");
  stage("installing");
  $("kobo-back").disabled = $("kobo-skip").disabled = true;
  const art = document.querySelector('[data-step="kobo"] .kobo-stage');
  const steps = milestones($("kobo-milestones"), KOBO_MILESTONES);
  const log = logTo($("kobo-log"));
  try {
    const look = document.querySelector('input[name="look"]:checked');
    const { job } = await api("/api/kobo/install", { body: { look: look ? look.value : null } });
    const result = await follow(job, (line) => {
      log(line);
      art.style.setProperty("--warmth", String(0.15 + 0.7 * steps.see(line)));
    });
    steps.finish();
    stage("done");
    setStatus($("kobo-status"), "", "");
    button.hidden = true;
    $("kobo-look").hidden = true;
    done.add("kobo");
    $("done-lead").innerHTML = "";
    $("done-lead").append(result.ejected
      ? "Your Kobo is ejected. Unplug it: it restarts with a Voxie button in its bottom bar."
      : "Eject your Kobo in Finder, then unplug it: it restarts with a Voxie button in its bottom bar.");
    setTimeout(() => go("done"), 1800);
  } catch (x) {
    stage("connected");
    $("kobo-back").disabled = $("kobo-skip").disabled = false;
    $("kobo-error").textContent = `${x.message} Nothing was left half-written: plug it in again and retry.`;
    busy(button, false);
    installing = false;
    startKoboWatch();
  }
}

// ---------------------------------------------------------------- done --
function initDone() {
  $("open-dropzone").addEventListener("click", () => api("/api/open-dropzone", { body: {} }));
  $("finish").addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    await api("/api/finish", { body: {} });
    setStatus($("finish-status"), "Setup closed. You can close this tab.", "ok");
  });
}
function fillDone() {
  $("donate-link").href = state.links.donate;
  $("done-abs").textContent = state.absUrl || "";
  $("done-user").textContent = $("username").value.trim() || state.username;
}

// ---------------------------------------------------------------- boot --
(async function boot() {
  const tpl = $("kobo-template");
  // Each copy gets its own gradient id: a url(#id) pointing into a hidden
  // step would render nothing.
  document.querySelectorAll(".kobo-slot").forEach((slot, i) => {
    const art = tpl.content.cloneNode(true);
    art.querySelector("radialGradient").id = `kobo-light-${i}`;
    art.querySelector(".glow").setAttribute("fill", `url(#kobo-light-${i})`);
    slot.replaceWith(art);
  });
  if (!key) { $("session-lost").hidden = false; return; }
  try { state = await api("/api/state"); } catch (_) { return; }
  initAccount();
  initServer();
  initNarration();
  $("kobo-install").addEventListener("click", installKobo);
  $("kobo-skip").addEventListener("click", () => { fillDone(); $("done-lead").textContent = "Pair your Kobo later: plug it in and run voxie setup-kobo."; go("done"); });
  initDone();
  go("welcome");
})();
