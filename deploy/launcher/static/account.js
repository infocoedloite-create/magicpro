(() => {
  const $ = (s) => document.querySelector(s);
  async function call(path, body) {
    const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.status === 401) { location.href = "/login?next=/app"; throw new Error("Signed out."); }
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(b.detail || r.statusText);
    return b;
  }
  const show = (el, text, kind) => { el.hidden = !text; el.textContent = text || ""; if (kind) el.className = `notice ${kind}`; };
  const when = (t) => new Date(t * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const kinds = { purchase: "Hour package", grant: "From the team", invite: "Invite code", trial: "Free trial" };

  function row(cells) {
    const tr = document.createElement("tr");
    for (const c of cells) { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); }
    return tr;
  }
  async function refresh() {
    const me = await call("/api/me");
    $("#balance").textContent = me.balance;
    $("#start").disabled = me.balance_s <= 0 || me.blocked;
    const dl = $("#daily-left");
    dl.hidden = !me.daily_left; dl.textContent = me.daily_left ? `Left today: ${me.daily_left} (daily limit).` : "";
    const need = $("#need-hours"); if (need) need.hidden = me.balance_s > 0;
    if (me.sessions.length) $("#sessions").replaceChildren(...me.sessions.map((s) => row([when(s.started), s.used])));
    if (me.ledger.length) $("#ledger").replaceChildren(...me.ledger.map((l) => row([when(l.t), kinds[l.kind] || l.kind, (l.hours > 0 ? "+" : "") + l.hours])));
  }

  // ---- Start: wait for a GPU, then go to it ----
  let waiting = false, timer = null, exclude = "";
  async function tryStart() {
    if (!waiting) return;
    try {
      const r = await call("/api/start", { exclude });
      if (r.url) { $("#start-text").textContent = "Opening your studio…"; location.href = r.url; return; }
      $("#start-text").textContent = r.text || "Starting GPU…";
    } catch (e) { stopWaiting(); show($("#start-error"), e.message); return; }
    timer = setTimeout(tryStart, 3000);
  }
  function stopWaiting() { waiting = false; clearTimeout(timer); $("#start-status").hidden = true; $("#cancel").hidden = true; $("#start").disabled = false; }
  $("#start").addEventListener("click", () => {
    waiting = true; show($("#start-error"), "");
    $("#start").disabled = true; $("#cancel").hidden = false; $("#start-status").hidden = false;
    $("#start-text").textContent = "Starting GPU…";
    tryStart();
  });
  $("#cancel").addEventListener("click", stopWaiting);

  // ---- Buy a pack with Razorpay Checkout ----
  document.querySelectorAll(".buy").forEach((b) => b.addEventListener("click", async () => {
    const msg = $("#pay-msg");
    show(msg, "");
    if (!window.Razorpay) { show(msg, "The payment window didn't load. Check your connection and try again.", "bad"); return; }
    b.disabled = true;
    try {
      const o = await call("/api/pay/order", { pack: b.dataset.pack });
      const rzp = new window.Razorpay({
        key: o.key, order_id: o.order_id, amount: o.amount, currency: o.currency, name: o.name, description: o.description,
        prefill: { email: o.email, name: o.user_name }, theme: { color: "#f2a93b" },
        handler: async (resp) => {
          try {
            const v = await call("/api/pay/verify", resp);
            show(msg, `Payment received. Your balance is now ${v.balance}.`, "good");
            refresh();
          } catch (e) { show(msg, e.message, "bad"); }
        },
        modal: { ondismiss: () => { b.disabled = false; } },
      });
      rzp.on("payment.failed", (r) => show(msg, `The payment didn't go through: ${r.error && r.error.description || "try again"}.`, "bad"));
      rzp.open();
    } catch (e) { show(msg, e.message, "bad"); }
    finally { b.disabled = false; }
  }));

  // ---- Invite code, or coming back from a GPU that stopped answering ----
  const qs = new URLSearchParams(location.search);
  const code = qs.get("code");
  if (code) { $("#code").value = code; history.replaceState(null, "", "/app"); }
  if (qs.has("reconnect")) {
    exclude = qs.get("exclude") || "";
    history.replaceState(null, "", "/app");
    $("#start").click();
    $("#start-text").textContent = "Your GPU stopped answering. Moving you to another one…";
  }
  $("#redeem").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await call("/api/redeem", { code: $("#code").value });
      show($("#redeem-msg"), r.message, "good"); $("#code").value = ""; refresh();
    } catch (err) { show($("#redeem-msg"), err.message, "bad"); }
  });

  refresh().catch(() => {});
})();
