"use strict";

// The kiosk — one job: tell the person in front of it whether it saw them,
// and let them stamp Check In/Out with one big, contextual tap. All language
// lives here in HTML (crisp at any size, screen-reader reachable); the video
// carries only the colored box drawn server-side.

function kioskPage() {
  const kiosk = document.getElementById("kiosk");
  const whoName = document.getElementById("whoName");
  const whoSub = document.getElementById("whoSub");
  const btnPrimary = document.getElementById("btnPrimary");
  const btnSecondary = document.getElementById("btnSecondary");
  const btnRetry = document.getElementById("btnRetry");
  const confirm = document.getElementById("confirm");
  const confirmName = document.getElementById("confirmName");
  const confirmDir = document.getElementById("confirmDir");
  const confirmTime = document.getElementById("confirmTime");
  const camDown = document.getElementById("camDown");

  let current = null;    // the candidate dict, or null
  let busy = false;       // a stamp is in flight, or we're showing its confirmation
  let retryable = false;

  function setState(state) { kiosk.dataset.state = state; }

  function labelFor(direction) { return direction === "out" ? "Check Out" : "Check In"; }

  function render(candidate, hint) {
    current = candidate;
    retryable = !!(hint && hint.retryable);
    btnRetry.hidden = !retryable;
    if (busy) return;   // a confirm overlay or in-flight stamp owns the screen

    if (candidate) {
      setState("ready");
      whoName.textContent = candidate.name;
      const primaryDir = candidate.expected === "out" ? "out" : "in";
      const secondaryDir = primaryDir === "out" ? "in" : "out";
      btnPrimary.textContent = labelFor(primaryDir);
      btnPrimary.dataset.dir = primaryDir;
      btnSecondary.textContent = labelFor(secondaryDir) + " instead";
      btnSecondary.dataset.dir = secondaryDir;
      btnPrimary.disabled = btnSecondary.disabled = false;
      if (primaryDir === "out" && candidate.last_action_ts) {
        whoSub.textContent = `You checked in at ${candidate.last_action_ts.slice(11, 16)}`;
      } else {
        whoSub.textContent = "Tap to record it";
      }
      return;
    }

    btnPrimary.disabled = btnSecondary.disabled = true;
    if (hint && hint.stage === "liveness") {
      setState("wait");
      whoName.textContent = (hint.name ? hint.name + " — " : "") + "Turn your head";
      whoSub.textContent = "…then look back at the camera";
    } else if (hint && hint.stage === "recognizing") {
      setState("wait");
      whoName.textContent = "Recognising…";
      whoSub.textContent = "Hold still";
    } else if (hint && hint.stage === "liveness_failed") {
      setState("failed");
      whoName.textContent = "Didn't catch that";
      whoSub.textContent = "Try again below, or wait — it'll retry on its own";
    } else if (hint && hint.stage === "unrecognized") {
      setState("failed");
      whoName.textContent = "Not recognised";
      whoSub.textContent = "See HR to enrol";
    } else if (hint && hint.stage === "done") {
      setState("done");
      whoName.textContent = hint.name || "";
      whoSub.textContent = hint.prompt || "Done";
    } else {
      setState("idle");
      whoName.textContent = "Step up to the camera";
      whoSub.textContent = "";
    }
  }

  async function poll() {
    if (busy) return;
    try {
      const { candidate, hint } = await (await fetch("/api/candidate")).json();
      render(candidate, hint);
    } catch (e) { /* transient */ }
  }

  async function stamp(direction) {
    if (!current || busy) return;
    busy = true;
    btnPrimary.disabled = btnSecondary.disabled = true;
    try {
      const r = await fetch("/api/stamp", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ direction }),
      });
      const body = await r.json();
      if (!r.ok) {
        setState("failed");
        whoName.textContent = "Couldn't record that";
        whoSub.textContent = body.detail || "Step up to the camera and try again";
        return;
      }
      showConfirm(body.record, direction);
    } catch (e) {
      setState("failed");
      whoName.textContent = "Couldn't record that";
      whoSub.textContent = "Check the connection and try again";
    } finally {
      setTimeout(() => { busy = false; poll(); }, 1800);
    }
  }

  function showConfirm(rec, direction) {
    confirmName.textContent = rec.name;
    confirmDir.textContent = direction === "in" ? "Checked in" : "Checked out";
    confirmDir.className = "confirm-dir" + (direction === "out" ? " out" : "");
    confirmTime.textContent = rec.ts.slice(11, 16);
    confirm.hidden = false;
    setState("done");
    setTimeout(() => { confirm.hidden = true; }, 1600);
  }

  btnPrimary.addEventListener("click", () => stamp(btnPrimary.dataset.dir));
  btnSecondary.addEventListener("click", () => stamp(btnSecondary.dataset.dir));
  btnRetry.addEventListener("click", async () => {
    btnRetry.hidden = true;
    try { await fetch("/api/retry", { method: "POST" }); } catch (e) { /* transient */ }
    poll();
  });

  poll();
  setInterval(poll, 200);

  // --- camera health, polled far less often — a webcam open is not cheap --- //
  async function checkCamera() {
    try {
      const s = await (await fetch("/api/status")).json();
      camDown.hidden = s.camera_ok;
    } catch (e) { /* transient */ }
  }
  checkCamera();
  setInterval(checkCamera, 3000);
}

// --- camera picker (small, corner-anchored — see kiosk.css) --------- //
function initKioskCameraPicker() {
  const sel = document.getElementById("camSelect");
  const refresh = document.getElementById("camRefresh");
  if (!sel) return;

  async function load() {
    if (document.activeElement === sel) return;
    sel.disabled = true;
    try {
      const { cameras, current } = await (await fetch("/api/cameras")).json();
      // Real device names, straight through: capture addresses the camera by
      // name, so nothing here is translated into a position that could drift.
      sel.innerHTML = cameras.length
        ? cameras.map(c => `<option value="${esc(c.name)}" ${c.name === current ? "selected" : ""}>${
            esc(c.name)
          }</option>`).join("")
        : `<option value="">no cameras found</option>`;
    } catch (e) {
      sel.innerHTML = `<option>unavailable</option>`;
    } finally {
      sel.disabled = false;
    }
  }

  sel.addEventListener("change", async () => {
    const name = sel.value;
    if (!name) return;
    sel.disabled = true;
    try {
      await fetch("/api/camera", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
    } finally {
      setTimeout(() => { sel.disabled = false; load(); }, 1500);
    }
  });
  refresh?.addEventListener("click", load);

  load();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
