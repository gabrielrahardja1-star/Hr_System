"use strict";

// --- dashboard: live roster refresh --------------------------------- //
function startRosterRefresh(day) {
  const tbody = document.querySelector("#roster tbody");
  if (!tbody) return;
  async function tick() {
    try {
      const r = await fetch("/api/roster?date=" + encodeURIComponent(day));
      const { roster } = await r.json();
      if (!roster.length) return;
      tbody.innerHTML = roster.map(row => `
        <tr>
          <td>${esc(row.name)}</td>
          <td class="mut">${esc(row.emp_id || row.uid)}</td>
          <td>${row.first_seen.slice(11, 19)}</td>
          <td>${row.last_seen.slice(11, 19)}</td>
          <td class="mut">${row.sightings}</td>
        </tr>`).join("");
    } catch (e) { /* transient */ }
  }
  setInterval(tick, 5000);
}

function wireForgetButtons() {
  document.querySelectorAll("[data-forget]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm(`Forget ${btn.dataset.name}? This deletes their face data and sightings.`)) return;
      const r = await fetch(`/api/people/${encodeURIComponent(btn.dataset.forget)}/forget`, { method: "POST" });
      if (r.ok) btn.closest("tr").remove();
      else alert("Could not remove: " + (await r.text()));
    });
  });
}

// --- kiosk page: recognised readout + Check In / Check Out --------- //
function kioskPage() {
  const who = document.getElementById("who");
  const whoName = document.getElementById("whoName");
  const whoSub = document.getElementById("whoSub");
  const btnIn = document.getElementById("btnIn");
  const btnOut = document.getElementById("btnOut");
  const toast = document.getElementById("toast");
  let current = null;      // {uid, name, ...} or null
  let busy = false;

  function setReadout(cand, hint) {
    current = cand;
    const ready = !!cand && !busy;
    btnIn.disabled = !ready;
    btnOut.disabled = !ready;
    if (busy) return;
    if (cand) {
      who.dataset.state = "ready";
      whoName.textContent = cand.name;
      whoSub.textContent = "Tap Check In or Check Out";
    } else if (hint && hint.stage === "liveness") {
      who.dataset.state = "wait";
      whoName.textContent = (hint.name ? hint.name + " — " : "") + "Turn your head";
      whoSub.textContent = "…then look back at the camera";
    } else if (hint && hint.stage === "recognizing") {
      who.dataset.state = "wait";
      whoName.textContent = "Recognising…";
      whoSub.textContent = "hold still";
    } else if (hint && hint.stage === "done") {
      who.dataset.state = "done";
      whoName.textContent = hint.name || "";
      whoSub.textContent = hint.prompt || "";
    } else {
      who.dataset.state = "idle";
      whoName.textContent = "Step up to the camera";
      whoSub.textContent = "";
    }
  }

  async function poll() {
    if (busy) return;
    try {
      const { candidate, hint } = await (await fetch("/api/candidate")).json();
      setReadout(candidate, hint);
    } catch (e) { /* transient */ }
  }

  async function stamp(direction) {
    if (!current || busy) return;
    busy = true;
    btnIn.disabled = btnOut.disabled = true;
    try {
      const r = await fetch("/api/stamp", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ direction }),
      });
      const body = await r.json();
      if (!r.ok) { showToast(body.detail || "could not record", "bad"); return; }
      const rec = body.record;
      showToast(`${direction === "in" ? "Checked in" : "Checked out"} · ${rec.name} · ${rec.ts.slice(11, 16)}`, "ok");
      who.dataset.state = "done";
      whoName.textContent = rec.name;
      whoSub.textContent = direction === "in" ? "checked in" : "checked out";
    } catch (e) {
      showToast("could not record", "bad");
    } finally {
      setTimeout(() => { busy = false; poll(); }, 2500);
    }
  }

  function showToast(text, kind) {
    toast.textContent = text;
    toast.className = "toast " + (kind || "");
    toast.hidden = false;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => { toast.hidden = true; }, 3500);
  }

  btnIn.addEventListener("click", () => stamp("in"));
  btnOut.addEventListener("click", () => stamp("out"));
  poll();
  setInterval(poll, 500);
}

// --- register page ------------------------------------------------- //
function registerPage(minShots) {
  const form = document.getElementById("reg");
  const capBtn = document.getElementById("capture");
  const saveBtn = document.getElementById("save");
  const cancelBtn = document.getElementById("cancel");
  const countEl = document.getElementById("shotcount");
  const dotsEl = document.getElementById("dots");
  const msg = document.getElementById("msg");
  let token = null, shots = 0;

  function render() {
    countEl.textContent = shots;
    dotsEl.innerHTML = Array.from({ length: Math.max(shots, minShots) },
      (_, i) => `<span class="d ${i < shots ? "on" : ""}"></span>`).join("");
    saveBtn.disabled = shots < minShots;
  }
  function say(text, kind) { msg.textContent = text; msg.className = "msg " + (kind || ""); }

  capBtn.addEventListener("click", async () => {
    capBtn.disabled = true; say("capturing…");
    try {
      const r = await fetch("/api/capture", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const body = await r.json();
      if (!r.ok) { say(body.detail || "capture failed", "bad"); return; }
      token = body.token; shots = body.shots; render();
      say(`shot ${shots} captured`, "ok");
    } catch (e) { say("capture failed", "bad"); }
    finally { capBtn.disabled = false; }
  });

  cancelBtn.addEventListener("click", async () => {
    if (token) await fetch("/api/enroll/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    token = null; shots = 0; form.reset(); render(); say("");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    saveBtn.disabled = true; say("saving…");
    try {
      const r = await fetch("/api/enroll", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, name: fd.get("name"), emp_id: fd.get("emp_id") }),
      });
      const body = await r.json();
      if (!r.ok) { say(body.detail || "save failed", "bad"); saveBtn.disabled = false; return; }
      say(`Saved ${body.person.name} (${body.person.emp_id || body.person.uid}). Redirecting…`, "ok");
      setTimeout(() => location.href = "/", 1200);
    } catch (e) { say("save failed", "bad"); saveBtn.disabled = false; }
  });

  render();
}

// --- shared ------------------------------------------------------- //
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function pollStatus() {
  const el = document.getElementById("fps");
  if (!el) return;
  try {
    const s = await (await fetch("/api/status")).json();
    el.textContent = s.fps;
  } catch (e) { /* ignore */ }
}
setInterval(pollStatus, 4000);
