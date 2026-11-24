// Home page: the time-left counter in the studio picture moves only while it shows "Live",
// which is how billing works. It rests under reduced motion.
(() => {
  const signin = new URLSearchParams(location.search).get("signin");
  const n = document.getElementById("signin-note");
  if (n && signin === "failed") n.hidden = false;
  if (n && signin === "closed") { n.hidden = false; n.textContent = "New accounts are by invitation right now. Open your invite link to join."; }
  const left = document.getElementById("m-left"), live = document.getElementById("m-live"), state = document.getElementById("m-state");
  if (!left || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let secs = 2 * 3600 + 41 * 60, on = true, t = 0;
  const fmt = (s) => `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  setInterval(() => {
    t++;
    if (t % 4 === 0) {                       // 4 s live, 4 s paused, repeating
      on = !on;
      live.classList.toggle("paused", !on);
      live.lastChild.textContent = on ? "Live" : "Paused";
      state.textContent = on ? "Counting while you stream" : "Paused: no time counts";
    }
    if (on) secs = Math.max(0, secs - 1);
    left.textContent = fmt(secs);
  }, 1000);
})();
