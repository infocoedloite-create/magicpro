// Magic Pro Desktop: activation, first-run setup and updates (served by desktop/gate.py).
(() => {
  const $ = (s) => document.querySelector(s);
  const views = ["activate", "pending", "checking", "locked", "setup", "update", "error"];
  let st = null, autoStarted = false, lastEmail = "", lastKey = "";

  async function api(path, body) {
    const get = path.endsWith("/status");     // everything else is a POST, with the header other websites can't send
    const r = await fetch(path, get ? { cache: "no-store" } : {
      method: "POST", headers: { "Content-Type": "application/json", "X-Magic-Desktop": "1" }, body: JSON.stringify(body || {}),
    });
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof b.detail === "string" ? b.detail : r.statusText);
    return b;
  }
  const msg = (t, kind = "") => { const m = $("#msg"); m.hidden = !t; m.textContent = t || ""; m.className = `dk-note ${kind}`; };
  const show = (v) => views.forEach((x) => { $(`#v-${x}`).hidden = x !== v; });
  const date = (t) => new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  const dateTime = (t) => new Date(t * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const mb = (n) => `${Math.round(n / 1048576)} MB`;

  function facts(s) {
    const L = s.license, rows = [];
    const add = (k, v, warn) => rows.push([k, v, warn]);
    if (L.machine.name) add("This PC", L.machine.name);
    const gpu = s.setup?.gpu;
    if (gpu) add("Graphics card", gpu.name ? `${gpu.name} · driver ${gpu.driver} · ${Math.round(gpu.memory_mb / 1024)} GB` : "none found", !gpu.ok);
    if (gpu?.note) add("", gpu.note, true);
    if (L.email) add("Account", L.email);
    if (L.key) add("Key", `${L.key}${L.plan ? " · " + L.plan : ""}`);
    if (L.expires) add("Subscription", `until ${date(L.expires)} (${L.days_left} days left)`, L.days_left <= 3);
    if (L.state === "offline" || L.offline) add("Offline", L.offline_until ? `works offline until ${dateTime(L.offline_until)}` : "can't reach the licence server", true);
    const U = s.update;
    if (U.state === "available") add("Update", `version ${U.latest} is ready${U.notes ? ": " + U.notes : ""}`, true);
    else if (U.state === "downloading") add("Update", `downloading ${Math.round(U.progress * 100)}% of ${mb(U.size)}`);
    else if (U.state === "installing") add("Update", U.message);
    else if (U.state === "error" && U.message) add("Update", U.message, true);
    $("#facts").replaceChildren(...rows.flatMap(([k, v, warn]) => {
      const dt = Object.assign(document.createElement("dt"), { textContent: k });
      const dd = Object.assign(document.createElement("dd"), { textContent: v });
      if (warn) dd.className = "warn";
      return [dt, dd];
    }));
  }

  function render(s) {
    st = s;
    $("#ver").textContent = `v${s.version}`;
    const L = s.license, U = s.update;
    facts(s);
    $("#b-signout").hidden = !(L.active || L.state === "locked");
    $("#b-studio").hidden = !(L.active && !s.blocked);
    const upd = $("#b-update");
    upd.hidden = !["available", "downloading", "installing"].includes(U.state);
    upd.disabled = U.state !== "available";
    upd.textContent = U.state === "available" ? `Update to ${U.latest} (${mb(U.size)})` : U.state === "downloading" ? `Downloading ${Math.round(U.progress * 100)}%` : "Installing…";

    if (L.state === "unconfigured") { show("error"); $("#error-msg").textContent = "This copy of Magic Pro was built without its licence key. Please download the official installer."; return; }
    if (L.state === "pending") {
      const first = $("#v-pending").hidden;
      show("pending");
      $("#code-sent").textContent = `We sent a 6-digit code to ${L.pending.email || "your e-mail"}. Type it here.`;
      if (first) $("#code").focus();
      return;
    }
    if (L.state === "none") { show("activate"); if (L.message) msg(L.message, "bad"); return; }
    if (L.state === "checking") { show("checking"); setTimeout(refresh, 1500); return; }
    if (L.state === "locked" || L.state === "offline" || L.state === "clock") {
      show("locked");
      const titles = { expired: "Your subscription has ended", revoked: "This key was cancelled", blocked: "Your account is paused",
        machine: "This key belongs to another PC", signed_out: "This PC isn't activated any more" };
      $("#locked-title").textContent = L.state === "offline" ? "Magic Pro needs to go online"
        : L.state === "clock" ? "Your PC's clock looks wrong" : titles[L.code] || "Your subscription has ended";
      $("#locked-msg").textContent = L.state === "offline" ? "Magic Pro checks your subscription at least every 3 days. Connect to the internet and press Check again."
        : L.state === "clock" ? "The date or time on this PC is earlier than before. Set the correct time in Windows settings, then press Check again."
        : L.message || "Renew your subscription to keep using Magic Pro.";
      $("#b-renew").hidden = L.state !== "locked";
      return;
    }
    // active
    if (s.blocked === "update") {
      show("update");
      $("#update-msg").textContent = `This version (${s.version}) is no longer supported. Version ${U.latest} installs in a few minutes; your faces, settings and models are kept.`;
      return;
    }
    if (s.blocked === "setup") { renderSetup(s.setup); setTimeout(refresh, 1000); return; }
    if (s.blocked === "studio") { show("error"); $("#error-msg").textContent = s.studio_error; return; }
    // all good: into the studio (unless the user came here on purpose)
    if (!location.search.includes("stay")) { location.replace("/"); return; }
    show("");
    if (L.message) msg(L.message, L.offline ? "" : "bad");
  }

  function renderSetup(S) {
    show("setup");
    const running = S.state === "running";
    $("#b-setup").hidden = running;
    $("#b-setup").textContent = S.state === "error" ? "Try again" : "Start setup";
    $("#setup-title").textContent = S.state === "error" ? "Setup stopped" : "Setting up Magic Pro";
    // customers see only a stage name and a percentage, never file, model or package names
    const stage = { runtime: "Installing the graphics engine", models: "Downloading the application files", done: "Ready" }[S.phase] || "Preparing";
    const pct = Math.round((S.overall ?? S.progress ?? 0) * 100);
    $("#setup-step").textContent = S.error ? "Setup couldn't finish. Check your internet connection and try again." : stage;
    $("#setup-step").style.color = S.error ? "var(--bad)" : "";
    $("#setup-bar span").style.width = `${pct}%`;
    $("#setup-bar").setAttribute("aria-valuenow", pct);
    $("#setup-detail").textContent = S.error ? "" : `${pct}%`;
    if (S.state === "idle" && !autoStarted) { autoStarted = true; api("/desktop/api/setup/start", {}).catch((e) => msg(e.message, "bad")); }
  }

  async function refresh() {
    try { render(await api("/desktop/api/status")); } catch (e) { show("error"); $("#error-msg").textContent = e.message; }
  }

  const busy = async (btn, fn) => { btn.disabled = true; msg(""); try { await fn(); } catch (e) { msg(e.message, "bad"); } finally { btn.disabled = false; refresh(); } };
  const sendCode = () => api("/desktop/api/email/start", { key: lastKey, email: lastEmail });
  $("#f-activate").addEventListener("submit", (e) => {
    e.preventDefault();
    lastKey = $("#key").value.trim();
    lastEmail = $("#email").value.trim();
    busy($("#b-activate"), sendCode);
  });
  $("#f-code").addEventListener("submit", (e) => {
    e.preventDefault();
    busy($("#b-verify"), async () => { await api("/desktop/api/email/verify", { code: $("#code").value }); $("#code").value = ""; });
  });
  $("#b-resend").addEventListener("click", () => busy($("#b-resend"), async () => { await sendCode(); msg("A new code is on its way.", "good"); }));
  $("#b-cancel").addEventListener("click", () => busy($("#b-cancel"), () => api("/desktop/api/cancel", {})));
  $("#b-renew").addEventListener("click", () => api("/desktop/api/renew", {}));
  $("#b-recheck").addEventListener("click", () => busy($("#b-recheck"), async () => { const L = await api("/desktop/api/check", {}); if (L.message && !L.active) msg(L.message, "bad"); }));
  $("#b-other").addEventListener("click", () => busy($("#b-other"), () => api("/desktop/api/signout", {})));
  $("#b-signout").addEventListener("click", () => { if (confirm("Sign out on this PC? You can activate again with the same key and e-mail address.")) busy($("#b-signout"), () => api("/desktop/api/signout", {})); });
  $("#b-setup").addEventListener("click", () => busy($("#b-setup"), () => api("/desktop/api/setup/start", {})));
  const doUpdate = (btn) => busy(btn, async () => {
    if (!confirm("Download and install the update now? Magic Pro closes and opens again by itself.")) return;
    await api("/desktop/api/update/install", {});
  });
  $("#b-update").addEventListener("click", () => doUpdate($("#b-update")));
  $("#b-update-now").addEventListener("click", () => doUpdate($("#b-update-now")));

  // keep the app alive while this window is open; tell it when the window closes
  setInterval(() => { api("/desktop/api/heartbeat", {}).catch(() => {}); if (["downloading", "installing"].includes(st?.update?.state)) refresh(); }, 5000);
  addEventListener("pagehide", () => navigator.sendBeacon("/desktop/api/bye", ""));
  refresh();
})();
