// Added to the studio page by desktop/gate.py: a licence/update chip in the top bar, and the
// app-window lifecycle (the app quits when its window closes). The studio itself is unchanged.
(() => {
  const chip = Object.assign(document.createElement("a"), { className: "dk-chip", href: "/desktop/?stay=1", title: "Licence and updates" });
  const dot = Object.assign(document.createElement("span"), { className: "dot" });
  const text = document.createElement("span");
  chip.append(dot, text);
  const place = () => { const bar = document.querySelector(".top-actions"); if (bar) bar.prepend(chip); };
  place();

  async function update() {
    try {
      const r = await fetch("/desktop/api/status", { cache: "no-store" });
      const s = await r.json();
      const L = s.license, U = s.update;
      if (!L.active || s.blocked) { location.href = "/desktop/"; return; }
      chip.classList.toggle("update", U.state === "available");
      chip.classList.toggle("warn", L.days_left !== null && L.days_left <= 3 || L.offline);
      text.textContent = U.state === "available" ? `Update ${U.latest} ready`
        : L.offline ? "Offline" : L.days_left !== null ? `${L.days_left} day${L.days_left === 1 ? "" : "s"} left` : "Licensed";
      chip.title = `${L.email} · ${L.plan || "subscription"}${L.expires ? " until " + new Date(L.expires * 1000).toLocaleDateString() : ""} · Magic Pro ${s.version}`;
    } catch { /* app restarting */ }
  }
  update();
  setInterval(update, 60000);
  addEventListener("pagehide", () => navigator.sendBeacon("/desktop/api/bye", ""));
})();
