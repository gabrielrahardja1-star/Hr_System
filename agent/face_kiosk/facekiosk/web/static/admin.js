"use strict";

// --- custom dialog, replacing native alert()/confirm() -------------- //
function showDialog({ title, body, confirmText = "Confirm", danger = false, cancellable = true }) {
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "dialog-overlay";
    overlay.innerHTML = `
      <div class="dialog" role="alertdialog" aria-modal="true" aria-labelledby="dlgTitle">
        <h3 id="dlgTitle">${esc(title)}</h3>
        <p>${esc(body)}</p>
        <div class="dialog-actions">
          ${cancellable ? '<button type="button" class="btn" data-act="cancel">Cancel</button>' : ""}
          <button type="button" class="btn ${danger ? "danger" : "primary"}" data-act="ok">${esc(confirmText)}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const okBtn = overlay.querySelector('[data-act="ok"]');
    const cancelBtn = overlay.querySelector('[data-act="cancel"]');
    function close(result) {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    function onKey(e) { if (e.key === "Escape") close(false); }
    okBtn.addEventListener("click", () => close(true));
    cancelBtn?.addEventListener("click", () => close(false));
    document.addEventListener("keydown", onKey);
    okBtn.focus();
  });
}
function showAlert(text) {
  return showDialog({ title: "Notice", body: text, confirmText: "OK", cancellable: false }).then(() => {});
}

// --- dashboard: live roster refresh --------------------------------- //
function renderRosterRows(roster) {
  if (!roster.length) {
    return `<tr><td colspan="7" class="empty">No check-ins yet today.</td></tr>`;
  }
  return roster.map(row => `
    <tr data-uid="${esc(row.uid)}">
      <td>${esc(row.name)}</td>
      <td class="mut">${esc(row.emp_id || row.uid)}</td>
      <td>${row.check_in ? row.check_in.slice(11, 19) : "—"}</td>
      <td>${row.check_out ? row.check_out.slice(11, 19) : "—"}</td>
      <td class="mut">${row.hours != null ? row.hours.toFixed(2) : "—"}</td>
      <td class="mut">${row.sightings}</td>
      <td><button class="btn sm danger" data-void="${esc(row.uid)}" data-name="${esc(row.name)}">Void last</button></td>
    </tr>`).join("");
}

function startRosterRefresh(day) {
  const tbody = document.querySelector("#roster tbody");
  if (!tbody) return;
  async function tick() {
    try {
      const r = await fetch("/api/roster?date=" + encodeURIComponent(day));
      const { roster } = await r.json();
      tbody.innerHTML = renderRosterRows(roster);
      wireVoidButtons(day);
    } catch (e) { /* transient */ }
  }
  setInterval(tick, 5000);
}

function wireForgetButtons() {
  document.querySelectorAll("[data-forget]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const ok = await showDialog({
        title: `Forget ${btn.dataset.name}?`,
        body: "This deletes their face data and sightings.",
        confirmText: "Forget", danger: true,
      });
      if (!ok) return;
      const r = await fetch(`/api/people/${encodeURIComponent(btn.dataset.forget)}/forget`, { method: "POST" });
      if (r.ok) btn.closest("tr").remove();
      else showAlert("Could not remove: " + (await r.text()));
    });
  });
}

function wireVoidButtons(day) {
  document.querySelectorAll("[data-void]").forEach(btn => {
    if (btn.dataset.wired) return;
    btn.dataset.wired = "1";
    btn.addEventListener("click", async () => {
      const ok = await showDialog({
        title: "Void the last tap?",
        body: `Deletes ${btn.dataset.name}'s most recent check-in/out today. This can't be undone.`,
        confirmText: "Void", danger: true,
      });
      if (!ok) return;
      const r = await fetch("/api/roster/void", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uid: btn.dataset.void, date: day }),
      });
      if (!r.ok) { showAlert("Nothing to void."); return; }
      const r2 = await fetch("/api/roster?date=" + encodeURIComponent(day));
      const { roster } = await r2.json();
      document.querySelector("#roster tbody").innerHTML = renderRosterRows(roster);
      wireVoidButtons(day);
    });
  });
}

function wireSyncButton() {
  const btn = document.getElementById("syncBtn");
  const status = document.getElementById("syncStatus");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    status.textContent = "Syncing…";
    try {
      const r = await fetch("/api/sync", { method: "POST" });
      const result = await r.json();
      if (!r.ok || result.ok === false) {
        status.textContent = "Sync failed: " + (result.error || "unknown error");
      } else {
        status.textContent = `Synced ${result.accepted} (${result.duplicate} already synced)`;
      }
    } catch (err) {
      status.textContent = "Sync failed: " + err;
    } finally {
      btn.disabled = false;
    }
  });
}

function wirePeopleFilter() {
  const input = document.getElementById("peopleFilter");
  const rows = document.querySelectorAll("#people tbody tr[data-search]");
  if (!input || !rows.length) return;
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    rows.forEach(tr => { tr.hidden = q.length > 0 && !tr.dataset.search.includes(q); });
  });
}

// --- register page --------------------------------------------------- //
function registerPage(minShots, maxShots) {
  const form = document.getElementById("reg");
  const savedPanel = document.getElementById("savedPanel");
  const savedMsg = document.getElementById("savedMsg");
  const camStream = document.getElementById("camStream");
  const capBtn = document.getElementById("capture");
  const saveBtn = document.getElementById("save");
  const cancelBtn = document.getElementById("cancel");
  const countEl = document.getElementById("shotcount");
  const thumbsEl = document.getElementById("thumbs");
  const msg = document.getElementById("msg");
  let token = null;
  let thumbs = [];   // dataURLs, index-aligned with the server's pending shot list

  function snapshotThumb() {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 96; canvas.height = 96;
      const ctx = canvas.getContext("2d");
      const iw = camStream.naturalWidth || 1, ih = camStream.naturalHeight || 1;
      const side = Math.min(iw, ih);
      ctx.drawImage(camStream, (iw - side) / 2, (ih - side) / 2, side, side, 0, 0, 96, 96);
      return canvas.toDataURL("image/jpeg", 0.7);
    } catch (e) { return null; }   // canvas can throw on a not-yet-loaded frame — just skip the thumb
  }

  function render() {
    const shots = thumbs.length;
    countEl.textContent = shots;
    thumbsEl.innerHTML = thumbs.map((src, i) => `
      <span class="thumb">${src ? `<img src="${src}" alt="shot ${i + 1}">` : ""}
        <button type="button" data-del="${i}" aria-label="Delete shot ${i + 1}">×</button>
      </span>`).join("");
    thumbsEl.querySelectorAll("[data-del]").forEach(btn => {
      btn.addEventListener("click", () => deleteShot(parseInt(btn.dataset.del, 10)));
    });

    const ready = shots >= minShots;
    saveBtn.disabled = !ready;
    capBtn.disabled = shots >= maxShots;
    // Hierarchy follows the moment: capturing is the primary action until
    // there are enough shots, then Save takes over.
    saveBtn.classList.toggle("primary", ready);
    capBtn.classList.toggle("primary", !ready);
  }

  function say(text, kind) { msg.textContent = text; msg.className = "msg " + (kind || ""); }

  capBtn.addEventListener("click", async () => {
    capBtn.disabled = true; say("capturing…");
    const shot = snapshotThumb();
    try {
      const r = await fetch("/api/capture", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const body = await r.json();
      if (!r.ok) { say(body.detail || "capture failed", "bad"); return; }
      token = body.token;
      thumbs[body.shots - 1] = shot;
      thumbs.length = body.shots;
      render();
      say(`shot ${body.shots} captured`, "ok");
    } catch (e) { say("capture failed", "bad"); }
    finally { render(); }
  });

  async function deleteShot(index) {
    if (!token) return;
    try {
      const r = await fetch("/api/capture/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, index }),
      });
      if (!r.ok) { say("could not delete that shot", "bad"); return; }
      thumbs.splice(index, 1);
      render();
    } catch (e) { say("could not delete that shot", "bad"); }
  }

  cancelBtn.addEventListener("click", async () => {
    if (thumbs.length > 0) {
      const ok = await showDialog({
        title: "Discard shots?",
        body: `This clears ${thumbs.length} captured shot(s) and the name/ID you've entered.`,
        confirmText: "Discard", danger: true,
      });
      if (!ok) return;
    }
    if (token) await fetch("/api/enroll/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    token = null; thumbs = []; form.reset(); render(); say("");
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
      form.hidden = true;
      savedMsg.textContent = `Saved ${body.person.name} (${body.person.emp_id || body.person.uid}).`;
      savedPanel.hidden = false;
    } catch (e) { say("save failed", "bad"); saveBtn.disabled = false; }
  });

  render();
}

// --- top bar: camera picker ----------------------------------------- //
function initCameraPicker() {
  const sel = document.getElementById("camSelect");
  const refresh = document.getElementById("camRefresh");
  if (!sel) return;

  async function load() {
    if (document.activeElement === sel) return;  // don't yank the menu mid-choice
    sel.disabled = true;
    try {
      const { cameras, current } = await (await fetch("/api/cameras")).json();
      // Real device names — capture selects by name, so nothing is mapped
      // onto a position that reshuffles when a device is plugged or unplugged.
      sel.innerHTML = cameras.length
        ? cameras.map(c =>
            `<option value="${esc(c.name)}" ${c.name === current ? "selected" : ""}>${esc(c.name)}</option>`
          ).join("")
        : `<option value="">no cameras found</option>`;
    } catch (e) {
      sel.innerHTML = `<option>camera list unavailable</option>`;
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
      setTimeout(() => { sel.disabled = false; load(); }, 1500);  // give the switch time to land
    }
  });
  refresh?.addEventListener("click", load);

  load();
}

// --- top bar: status dot (cheap — no camera opens, safe to poll) --- //
function startStatusPoll() {
  const box = document.getElementById("statusBox");
  if (!box) return;
  async function tick() {
    try {
      const s = await (await fetch("/api/status")).json();
      let line;
      if (s.camera_ok) line = `<span class="dot ok"></span> ${esc(s.camera)} · ${s.fps} fps`;
      else if (s.error) line = `<span class="dot bad"></span> ${esc(s.error)}`;
      else line = `<span class="dot warn"></span> starting camera…`;
      box.innerHTML = `${line} · ${s.enrolled} enrolled`;
    } catch (e) { /* transient */ }
  }
  tick();
  setInterval(tick, 3000);
}

// --- shared ------------------------------------------------------- //
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
