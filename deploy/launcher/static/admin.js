(() => {
  const $ = (s) => document.querySelector(s);
  async function call(path, body) {
    const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(b.detail || r.statusText);
    return b;
  }
  const msg = (t, kind = "good") => { const m = $("#msg"); m.hidden = !t; m.textContent = t || ""; m.className = `notice ${kind}`; };
  const when = (t) => (t ? new Date(t * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
  const td = (content) => { const c = document.createElement("td"); if (content instanceof Node) c.appendChild(content); else c.textContent = content ?? ""; return c; };
  const btn = (label, cls, fn) => { const b = document.createElement("button"); b.type = "button"; b.className = `btn ${cls}`; b.textContent = label; b.addEventListener("click", fn); return b; };

  async function load() {
    const d = await call("/api/admin/overview");
    $("#mode").textContent = d.test_mode ? "Payments: test mode" : "Payments: LIVE";
    $("#invites").replaceChildren(...d.invites.map((i) => {
      const tr = document.createElement("tr");
      const link = document.createElement("a"); link.href = i.link; link.textContent = i.code;
      tr.append(td(link), td(i.hours), td(`${i.uses} / ${i.max_uses}`), td(when(i.expires)), td(i.revoked ? "withdrawn" : i.active ? "active" : "closed"),
        td(i.active ? btn("Withdraw", "danger", async () => { await call("/api/admin/invite/revoke", { code: i.code }); load(); }) : ""));
      return tr;
    }));
    $("#users").replaceChildren(...d.users.map((u) => {
      const tr = document.createElement("tr");
      const who = document.createElement("div"); who.innerHTML = "<b></b><br><span class='muted small'></span>";
      who.querySelector("b").textContent = u.name; who.querySelector("span").textContent = u.email;
      const form = document.createElement("form"); form.className = "form-row";
      form.innerHTML = '<input type="number" step="0.5" min="-1000" max="1000" value="1" aria-label="Hours" style="width:90px"><input placeholder="Note" aria-label="Note" style="width:140px"><button class="btn" type="submit">Apply</button>';
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const [h, n] = form.querySelectorAll("input");
        try { await call("/api/admin/grant", { sub: u.sub, hours: parseFloat(h.value), note: n.value }); msg(`Balance of ${u.email} changed by ${h.value} h.`); load(); }
        catch (err) { msg(err.message, "bad"); }
      });
      const actions = document.createElement("div"); actions.className = "form-row";
      actions.append(btn(u.disabled ? "Allow again" : "Pause", "ghost", async () => { await call("/api/admin/user", { sub: u.sub, disabled: !u.disabled }); load(); }),
        btn("Delete data", "danger", async () => {
          if (!confirm(`Delete ${u.email}'s account, balance and history? Payment records are kept without the e-mail.`)) return;
          await call("/api/admin/user", { sub: u.sub, delete: true }); msg(`Deleted ${u.email}.`); load();
        }));
      tr.append(td(who), td(u.balance + (u.disabled ? " (paused)" : "")), td(when(u.last_seen)), td(form), td(actions));
      return tr;
    }));
    $("#orders").replaceChildren(...d.orders.map((o) => {
      const tr = document.createElement("tr");
      tr.append(td(when(o.t)), td(o.email), td(o.pack_label), td(`₹${(o.amount / 100).toFixed(0)}`), td(o.status + (o.live ? "" : " (test)")));
      return tr;
    }));
  }
  async function loadPool() {
    try {
      const s = await call("/api/pool");
      $("#pool-note").textContent = `GPU cost right now: $${s.billing_per_hour}/h · at most ${s.limits.max_nodes} GPUs` + (s.note ? ` · ${s.note}` : "");
      $("#nodes").replaceChildren(...s.nodes.map((n) => {
        const tr = document.createElement("tr");
        tr.append(td(`${n.id}${n.gpu ? " · " + n.gpu : ""}`), td(n.state + (n.dry_run ? " (test)" : "")), td(n.users), td(`${n.load} / ${n.budget}`), td(n.price_per_hour));
        return tr;
      }));
    } catch (e) { $("#pool-note").textContent = e.message; }
  }
  $("#invite").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await call("/api/admin/invite", { hours: parseFloat($("#inv-hours").value), valid_hours: parseFloat($("#inv-valid").value), max_uses: parseInt($("#inv-uses").value, 10) });
      const n = $("#inv-new"); n.hidden = false; n.textContent = `New code ${r.code}: ${r.link}`; load();
    } catch (err) { msg(err.message, "bad"); }
  });
  $("#stop-idle").addEventListener("click", async () => {
    if (!confirm("Stop every GPU nobody is streaming on?")) return;
    try { const r = await call("/api/pool/stop-all", {}); msg(`Stopping: ${r.stopped.join(", ") || "none"}`); loadPool(); } catch (e) { msg(e.message, "bad"); }
  });
  load().catch((e) => msg(e.message, "bad"));
  loadPool(); setInterval(loadPool, 10000);
})();
