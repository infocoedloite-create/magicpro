(() => {
  const $ = (s, el = document) => el.querySelector(s);
  async function call(path, body) {
    const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(b.detail || r.statusText);
    return b;
  }
  const msg = (t, kind = "good") => { const m = $("#msg"); m.hidden = !t; m.textContent = t || ""; m.className = `notice ${kind}`; if (t) m.scrollIntoView({ block: "nearest" }); };
  const when = (t) => (t ? new Date(t * 1000).toLocaleString(undefined, { day: "numeric", month: "short", year: "2-digit", hour: "2-digit", minute: "2-digit" }) : "");
  const el = (tag, props = {}, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids.filter((k) => k !== null && k !== undefined)); return e; };
  const td = (...kids) => el("td", {}, ...kids.map((k) => (k instanceof Node ? k : String(k ?? ""))));
  const btn = (label, cls, fn) => { const b = el("button", { type: "button", className: `btn ${cls}`, textContent: label }); b.addEventListener("click", () => fn().catch((e) => msg(e.message, "bad"))); return b; };
  const inr = (n) => `₹${Number(n || 0).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

  // ---- overview ----
  async function loadOverview() {
    const o = await call("/api/admin/overview");
    $("#mode").hidden = !o.test_mode;
    $("#mode").textContent = "Payments are in TEST mode: revenue below is test money.";
    const kpi = (label, value, sub) => el("div", { className: "kpi" }, el("span", { className: "muted small", textContent: label }), el("b", { textContent: value }), sub ? el("span", { className: "muted small", textContent: sub }) : null);
    $("#kpis").replaceChildren(
      kpi("Streaming now", o.online_users, "people"),
      kpi("GPUs running", o.gpus_running, `$${o.gpu_usd_per_hour}/h now`),
      kpi("GPU cost today", `$${o.gpu_usd_today}`, `this month $${o.gpu_usd_month}`),
      kpi("Hours sold today", o.hours_sold_today, `this month ${o.hours_sold_month}`),
      kpi("Hours used today", o.hours_used_today, `this month ${o.hours_used_month}`),
      kpi(o.test_mode ? "Revenue today (test)" : "Revenue today", inr(o.test_mode ? o.revenue_test_today : o.revenue_live_today),
        `this month ${inr(o.test_mode ? o.revenue_test_month : o.revenue_live_month)}`),
    );
    $("#ov-note").textContent = [o.pay_problem && `Payments: ${o.pay_problem}`, o.pool_note && `GPUs: ${o.pool_note}`,
      "Days are India time. GPU cost counts while this website is in contact with the GPUs."].filter(Boolean).join(" · ");
  }

  // ---- users ----
  async function loadUsers() {
    const { users } = await call(`/api/admin/users?search=${encodeURIComponent($("#q").value)}`);
    $("#user-rows").replaceChildren(...users.map((u) => {
      const who = el("div", {}, el("b", { textContent: u.name }), el("br"), el("span", { className: "muted small", textContent: u.email }));
      const state = [u.blocked && "blocked", u.owner ? "owner" : u.admin && "admin"].filter(Boolean).join(", ") || "active";
      return el("tr", {}, td(who), td(u.balance), td(u.used), td(when(u.created)), td(when(u.last_seen)), td(state),
        td(btn("Open the profile", "ghost", () => openProfile(u.sub))));
    }));
    if (!users.length) $("#user-rows").replaceChildren(el("tr", {}, el("td", { colSpan: 7, className: "muted", textContent: "No users match." })));
  }
  $("#user-search").addEventListener("submit", (e) => { e.preventDefault(); loadUsers().catch((err) => msg(err.message, "bad")); });

  async function openProfile(sub) {
    const p = await call(`/api/admin/user/${encodeURIComponent(sub)}`);
    const box = $("#profile");
    box.hidden = false;
    const kinds = { purchase: "Package", grant: "Added by admin", invite: "Invite code", trial: "Free trial" };
    const table = (heads, rows) => el("div", { className: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, ...heads.map((h) => el("th", { textContent: h })))),
      el("tbody", {}, ...(rows.length ? rows : [el("tr", {}, el("td", { colSpan: heads.length, className: "muted", textContent: "Nothing yet." }))]))));

    const grant = el("form", { className: "form-row" });
    grant.innerHTML = '<label>Hours (minus takes away)<input name="hours" type="number" step="0.25" min="-1000" max="1000" value="1" required></label><label>Note for the audit log<input name="note" required maxlength="200"></label><button class="btn primary" type="submit">Change the balance</button>';
    grant.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { await call("/api/admin/grant", { sub, hours: parseFloat(grant.hours.value), note: grant.note.value }); msg(`Balance of ${p.email} changed.`); openProfile(sub); loadUsers(); }
      catch (err) { msg(err.message, "bad"); }
    });

    const ov = p.overrides || {};
    const lim = el("form", { className: "form-row" });
    lim.innerHTML = `<label>Longest session (h)<input name="max_session_hours" type="number" step="0.25" min="0.25" max="24" placeholder="global"></label>
      <label>Hours per day (0 = none)<input name="daily_hours" type="number" step="0.5" min="0" placeholder="global"></label>
      <label>One session at a time<select name="single_session"><option value="">global</option><option value="true">yes</option><option value="false">no</option></select></label>
      <button class="btn" type="submit">Save this user's limits</button>`;
    lim.max_session_hours.value = ov.max_session_hours ?? ""; lim.daily_hours.value = ov.daily_hours ?? "";
    lim.single_session.value = ov.single_session === undefined ? "" : String(ov.single_session);
    lim.addEventListener("submit", async (e) => {
      e.preventDefault();
      const limits = { max_session_hours: lim.max_session_hours.value || null, daily_hours: lim.daily_hours.value === "" ? null : lim.daily_hours.value,
        single_session: lim.single_session.value === "" ? null : lim.single_session.value === "true" };
      try { await call("/api/admin/user", { sub, limits }); msg(`Limits of ${p.email} saved.`); openProfile(sub); } catch (err) { msg(err.message, "bad"); }
    });

    const actions = el("div", { className: "actions" },
      p.owner ? null : btn(p.blocked ? "Unblock the user" : "Block the user", p.blocked ? "" : "danger", async () => {
        if (!p.blocked && !confirm(`Block ${p.email}? A live session ends within about 30 seconds.`)) return;
        await call("/api/admin/user", { sub, disabled: !p.blocked }); msg(`${p.email} ${p.blocked ? "unblocked" : "blocked"}.`); openProfile(sub); loadUsers();
      }),
      p.owner ? null : btn(p.admin ? "Remove admin role" : "Make admin", "ghost", async () => {
        if (!confirm(p.admin ? `Remove ${p.email}'s admin role?` : `Give ${p.email} full admin access?`)) return;
        await call("/api/admin/user", { sub, admin: !p.admin }); msg(`${p.email} is ${p.admin ? "no longer" : "now"} an admin.`); openProfile(sub); loadUsers();
      }),
      p.owner ? null : btn("Delete all data", "danger", async () => {
        if (!confirm(`Delete ${p.email}'s account, balance and history? Payment records stay, without the e-mail. This can't be undone.`)) return;
        await call("/api/admin/user", { sub, delete: true }); msg(`Deleted ${p.email}.`); box.hidden = true; loadUsers();
      }),
    );

    box.replaceChildren(...[
      el("h3", { textContent: `${p.name} · ${p.email}` }),
      el("p", { className: "muted small", textContent: `Joined ${when(p.created)} · last seen ${when(p.last_seen)} · ${p.blocked ? "BLOCKED · " : ""}${p.owner ? "owner" : p.admin ? "admin" : "user"}` }),
      el("div", { className: "kpis" },
        ...[["Balance", p.balance], ["Used in total", p.used], ["Used today", p.day_used],
          ["Session limit", `${p.limits.max_session_hours} h`], ["Daily limit", p.limits.daily_hours ? `${p.limits.daily_hours} h` : "none"]]
          .map(([k, v]) => el("div", { className: "kpi" }, el("span", { className: "muted small", textContent: k }), el("b", { textContent: v })))),
      p.expiring.length ? el("p", { className: "small muted", textContent: "Expiring: " + p.expiring.map((x) => `${x.hours} h on ${when(x.expires)}`).join(", ") }) : null,
      el("h4", { textContent: "Give or take hours" }), grant,
      el("h4", { textContent: "This user's limits" }), lim,
      actions,
      el("h4", { textContent: "Hours added and removed" }),
      table(["When", "What", "Hours", "Note"], p.ledger.map((l) => el("tr", {}, td(when(l.t)), td(kinds[l.kind] || l.kind), td((l.seconds > 0 ? "+" : "") + (l.seconds / 3600).toFixed(2)), td(l.note || "")))),
      el("h4", { textContent: "Payments" }),
      table(["When", "Package", "Amount", "State"], p.orders.map((o) => el("tr", {}, td(when(o.t)), td(o.pack_label), td(inr(o.amount / 100)), td(o.status + (o.live ? "" : " (test)"))))),
      el("h4", { textContent: "Sessions" }),
      table(["Started", "Streamed", "GPU", ""], p.sessions.map((s) => el("tr", {}, td(when(s.started)), td(s.used), td(s.node || ""), td(s.live ? "live now" : "")))),
      el("h4", { textContent: "Admin actions on this user" }),
      table(["When", "Admin", "Action", "Details"], p.audit.map((a) => el("tr", {}, td(when(a.t)), td(a.admin), td(a.action), td(a.detail)))),
    ].filter(Boolean));
    box.scrollIntoView({ block: "start" });
  }

  // ---- packages ----
  let packages = [];
  async function loadPackages() {
    packages = (await call("/api/admin/packages")).packages;
    $("#pkg-rows").replaceChildren(...packages.map((p, i) => pkgRow(p, i)));
  }
  function pkgRow(p, i) {
    const inp = (name, attrs) => Object.assign(el("input", { name, ...attrs }), { value: p[name] ?? "" });
    const name = inp("name", { maxLength: 40, "aria-label": "Name" });
    const hours = inp("hours", { type: "number", step: "0.25", min: "0.25", "aria-label": "Hours" });
    const price = inp("price_inr", { type: "number", step: "1", min: "1", "aria-label": "Price in rupees" });
    const valid = inp("validity_days", { type: "number", step: "1", min: "1", placeholder: "no expiry", "aria-label": "Use within days" });
    const popular = el("input", { type: "checkbox", checked: !!p.popular, "aria-label": "Popular badge" });
    const enabled = el("input", { type: "checkbox", checked: p.enabled !== false, "aria-label": "On sale" });
    const move = (d) => btn(d < 0 ? "↑" : "↓", "ghost", async () => {
      const ids = packages.map((x) => x.id); const j = i + d;
      if (!p.id || j < 0 || j >= ids.length) return;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      await call("/api/admin/packages/order", { ids }); loadPackages();
    });
    const up = move(-1); up.setAttribute("aria-label", "Move up"); const down = move(1); down.setAttribute("aria-label", "Move down");
    const save = btn(p.id ? "Save" : "Create", "primary", async () => {
      await call("/api/admin/packages", { ...(p.id ? { id: p.id } : {}), name: name.value, hours: hours.value, price_inr: price.value,
        validity_days: valid.value || null, popular: popular.checked, enabled: enabled.checked });
      msg(`Package "${name.value}" saved.`); loadPackages();
    });
    return el("tr", {}, td(el("div", { className: "move" }, up, down)), td(name), td(hours), td(price), td(valid), td(popular), td(enabled), td(save));
  }
  $("#pkg-add").addEventListener("click", () => {
    const p = { name: "", hours: 1, price_inr: "", enabled: false };
    packages.push(p);
    $("#pkg-rows").append(pkgRow(p, packages.length - 1));
  });

  // ---- limits ----
  async function loadLimits() {
    const { limits } = await call("/api/admin/limits");
    $("#grace-min").textContent = limits.idle_grace_minutes;
    const f = $("#limits-form");
    for (const [k, v] of Object.entries(limits)) {
      const input = f.elements[k];
      if (!input) continue;
      if (input.type === "checkbox") input.checked = !!v; else input.value = v;
    }
  }
  $("#limits-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, body = {};
    for (const input of f.querySelectorAll("input")) body[input.name] = input.type === "checkbox" ? input.checked : input.value;
    try { await call("/api/admin/limits", body); msg("Limits saved. GPUs pick up the new idle time at their next check-in."); loadLimits(); }
    catch (err) { msg(err.message, "bad"); }
  });

  // ---- invites and payments ----
  async function loadInvites() {
    const d = await call("/api/admin/invites");
    $("#invite-rows").replaceChildren(...d.invites.map((i) => el("tr", {},
      td(el("a", { href: i.link, textContent: i.code })), td(i.hours), td(`${i.uses} / ${i.max_uses}`), td(when(i.expires)),
      td(i.revoked ? "withdrawn" : i.active ? "active" : "closed"),
      td(i.active ? btn("Withdraw", "danger", async () => { await call("/api/admin/invite/revoke", { code: i.code }); loadInvites(); }) : ""))));
    $("#order-rows").replaceChildren(...d.orders.map((o) => el("tr", {},
      td(when(o.t)), td(o.email), td(o.pack_label), td(inr(o.amount / 100)), td(o.status + (o.live ? "" : " (test)")))));
  }
  $("#invite").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await call("/api/admin/invite", { hours: parseFloat($("#inv-hours").value), valid_hours: parseFloat($("#inv-valid").value), max_uses: parseInt($("#inv-uses").value, 10) });
      const n = $("#inv-new"); n.hidden = false; n.textContent = `New code ${r.code}: ${r.link}`; loadInvites();
    } catch (err) { msg(err.message, "bad"); }
  });

  // ---- GPUs and audit ----
  const dur = (sec) => (sec ? `${Math.floor(sec / 3600)} h ${String(Math.floor((sec % 3600) / 60)).padStart(2, "0")} min` : "");
  async function gpuOff(body, label) {
    if (!confirm(`${label}?`)) return;
    try {
      const r = await call("/api/admin/gpu/off", body);
      msg(`Turned off: ${r.stopped.join(", ") || "none"}.`);
    } catch (e) {
      if (!/streaming/.test(e.message)) throw e;
      if (!confirm(`${e.message} Turn off anyway?`)) return;
      const r = await call("/api/admin/gpu/off", { ...body, confirm: true });
      msg(`Turned off: ${r.stopped.join(", ") || "none"}. ${r.users_cut_off} streaming user(s) were cut off and are no longer charged.`);
    }
    loadPool(); loadAudit();
  }
  async function loadPool() {
    try {
      const s = await call("/api/pool");
      $("#pool-note").textContent = `GPU cost right now: $${s.billing_per_hour}/h · at most ${s.limits.max_nodes} GPUs` + (s.note ? ` · ${s.note}` : "");
      $("#maint").checked = !!s.paused;
      $("#nodes").replaceChildren(...s.nodes.map((n) => {
        const on = ["running", "starting"].includes(n.status);
        const action = n.status === "stopping" ? "" : on
          ? btn("Turn off now", "danger", () => gpuOff({ id: n.id }, `Turn GPU ${n.id} off now`))
          : btn("Turn on now", "", async () => {
              await call("/api/admin/gpu/on", { id: n.id });
              msg(`Turning GPU ${n.id} on. It is ready in 1-2 minutes.`); loadPool(); loadAudit();
            });
        return el("tr", {}, td(`${n.id}${n.gpu ? " · " + n.gpu : ""}`), td(n.status + (n.dry_run ? " (test)" : "")), td(n.users),
          td(dur(n.uptime_s)), td(n.uptime_s ? `$${n.cost_usd}` : ""), td(n.price_per_hour), td(action));
      }));
      if (!s.nodes.length) $("#nodes").replaceChildren(el("tr", {}, el("td", { colSpan: 7, className: "muted", textContent: "No GPUs in the pool yet." })));
    } catch (e) { $("#pool-note").textContent = e.message; }
  }
  $("#off-all").addEventListener("click", () => gpuOff({ all: true }, "Turn every GPU off now").catch((e) => msg(e.message, "bad")));
  $("#maint").addEventListener("change", async (e) => {
    const on = e.target.checked;
    const ask = 'Pause the service? Start will show "Service is paused, try again later" and no GPU starts by itself. Running GPUs keep running until you turn them off.';
    if (on && !confirm(ask)) { e.target.checked = false; return; }
    try {
      await call("/api/admin/limits", { maintenance: on });
      msg(on ? "Service paused." : "Service open again."); loadPool(); loadAudit();
    } catch (err) { msg(err.message, "bad"); e.target.checked = !on; }
  });
  async function loadAudit() {
    const { audit } = await call("/api/admin/audit");
    $("#audit-rows").replaceChildren(...audit.map((a) => el("tr", {}, td(when(a.t)), td(a.admin), td(a.action), td(a.target), td(a.detail))));
  }

  const all = [loadOverview, loadUsers, loadPackages, loadLimits, loadInvites, loadPool, loadAudit];
  Promise.all(all.map((f) => f().catch((e) => msg(e.message, "bad"))));
  setInterval(() => { loadOverview().catch(() => {}); loadPool(); }, 15000);
  document.addEventListener("click", (e) => { if (e.target.closest("button")) setTimeout(loadAudit, 400); });
})();
