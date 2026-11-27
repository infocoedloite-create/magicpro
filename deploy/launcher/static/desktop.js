(() => {
  const box = document.getElementById("activate");
  if (!box) return;
  const link = box.dataset.link;
  const msg = (t, kind) => { const m = document.getElementById("msg"); m.hidden = false; m.textContent = t; m.className = `notice ${kind}`; };
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(b.detail || r.statusText);
    return b;
  }
  const go = document.getElementById("go"), deny = document.getElementById("deny");
  go.addEventListener("click", async () => {
    const own = document.querySelector('input[name="own"]:checked');
    const key = (own && own.value) || document.getElementById("key")?.value || "";
    go.disabled = true;
    try {
      const r = await post("/api/desktop/approve", { link_id: link, key });
      msg(r.message, "good");
      go.hidden = deny.hidden = true;
    } catch (e) {
      msg(e.message, "bad");
      go.disabled = false;
    }
  });
  deny.addEventListener("click", async () => {
    await post("/api/desktop/deny", { link_id: link }).catch(() => {});
    msg("Cancelled. Nothing was activated.", "good");
    go.hidden = deny.hidden = true;
  });
})();
