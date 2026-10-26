"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  let token = "", generation = 0, parameterRevision = null, staffRevision = null;
  let parameterDirty = false, staffDirty = false, selectedRoster = null;
  const pendingRequests = new Set();
  const context = () => ({generation, token});
  const active = (session) => session.generation === generation && session.token === token;
  const stale = () => Object.assign(new Error("Request belongs to a closed session."), {stale: true});
  const check = (session) => { if (!active(session)) throw stale(); };
  const message = (text, error = false) => { $("message").textContent = text; $("message").className = error ? "error" : ""; };
  const clear = () => {
    generation += 1; token = "";
    for (const controller of pendingRequests) controller.abort();
    pendingRequests.clear(); parameterRevision = staffRevision = null; selectedRoster = null;
    parameterDirty = staffDirty = false; $("access-token").value = "";
    ["hotel-name", "revision", "versions", "calculations", "constraints", "schedule", "roster-title", "roster-status"].forEach((id) => $(id).replaceChildren());
    ["capacity", "checkout", "stayover", "staff-json"].forEach((id) => { $(id).value = ""; });
    $("workspace").hidden = true; $("roster-detail").hidden = true; $("login").hidden = false;
  };
  function report(error, session, login = false) {
    if (error.stale || !active(session)) return;
    if (error.authFailure || login) clear();
    message(error instanceof SyntaxError ? "Staff rows must be valid JSON." : error.message, true);
  }
  async function api(path, options = {}, format = "json", session = context()) {
    check(session);
    const controller = new AbortController(); pendingRequests.add(controller);
    try {
      const response = await fetch("/private/api" + path, {...options, credentials: "omit", cache: "no-store", signal: controller.signal,
        headers: {...(options.headers || {}), Authorization: "Bearer " + session.token}});
      check(session);
      if (response.status === 401 || response.status === 403) throw Object.assign(new Error("Access is unavailable. Sign in again or contact your operator."), {authFailure: true});
      if (response.status === 409) throw new Error("Saved data changed. Your draft was not saved. Reload, then reapply your edits.");
      if (!response.ok) throw new Error(response.status === 422 ? "The submitted values are invalid. Check counts, staff JSON, availability and limits." : "The request could not be completed. Check your operator setup or retry.");
      const result = await (format === "blob" ? response.blob() : response.json());
      check(session); return result;
    } catch (error) { if (!active(session)) throw stale(); throw error; }
    finally { pendingRequests.delete(controller); }
  }
  function applySettings(data, force = false, section = "") {
    if (data.hotel_name) $("hotel-name").textContent = data.hotel_name;
    $("revision").textContent = "Saved settings revision " + data.revision;
    if (force || !parameterDirty || section === "parameters") {
      $("capacity").value = data.parameters.room_capacity ?? "";
      $("checkout").value = data.parameters.checkout_minutes;
      $("stayover").value = data.parameters.stayover_minutes;
      parameterRevision = data.revision; parameterDirty = false;
    }
    if (force || !staffDirty || section === "staff") {
      $("staff-json").value = JSON.stringify(data.staff, null, 2);
      staffRevision = data.revision; staffDirty = false;
    }
  }
  function showRoster(roster) {
    selectedRoster = roster; $("roster-detail").hidden = false;
    $("roster-title").textContent = roster.week_start + " to " + roster.week_end + " · Version " + roster.version;
    $("roster-status").textContent = roster.feasible ? "Covered under this version's saved assumptions; review before operational use." : "NEEDS ATTENTION — uncovered demand or constraints remain.";
    $("roster-status").className = roster.feasible ? "" : "attention";
    $("calculations").replaceChildren();
    (roster.calculations || []).forEach((day) => {
      const p = document.createElement("p");
      p.textContent = `${day.date}: ${day.checkout_rooms} checkouts × ${day.checkout_minutes} min + ${day.stayover_rooms} stayovers × ${day.stayover_minutes} min = ${day.demand_minutes} min. Needed ${day.required_housekeepers}; scheduled ${day.scheduled_housekeepers}; uncovered ${day.uncovered_minutes} min. ${day.status}.`;
      $("calculations").append(p);
    });
    $("constraints").replaceChildren();
    (roster.constraints.length ? roster.constraints : ["None identified under the saved assumptions."]).forEach((text) => { const li = document.createElement("li"); li.textContent = text; $("constraints").append(li); });
    $("schedule").replaceChildren();
    roster.schedule.forEach((row) => {
      const tr = document.createElement("tr");
      [row.display_name, row.date, row.code, row.start, row.end, row.locked ? "Yes" : "No"].forEach((value) => { const td = document.createElement("td"); td.textContent = value ?? ""; tr.append(td); });
      $("schedule").append(tr);
    });
  }
  async function loadVersions(session) {
    const data = await api("/rosters", {}, "json", session); check(session); $("versions").replaceChildren();
    if (!data.versions.length) { $("versions").textContent = "No saved roster versions yet."; return; }
    data.versions.forEach((row) => {
      const div = document.createElement("div"), span = document.createElement("span"), button = document.createElement("button");
      div.className = "version"; span.textContent = `${row.week_start} · Version ${row.version} · ${row.feasible ? "Covered under saved assumptions" : "Needs attention"}`;
      button.textContent = "View version"; button.type = "button";
      button.addEventListener("click", async () => {
        const current = context();
        try { const roster = await api(`/rosters/${encodeURIComponent(row.week_start)}/versions/${row.version}`, {}, "json", current); check(current); showRoster(roster); message(""); }
        catch (error) { report(error, current); }
      });
      div.append(span, button); $("versions").append(div);
    });
  }
  async function reload(session) {
    const data = await api("/settings", {}, "json", session); check(session); applySettings(data, true);
    await loadVersions(session); check(session);
  }
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const entered = $("access-token").value; clear(); token = entered; const current = context();
    try { await reload(current); check(current); $("login").hidden = true; $("workspace").hidden = false; message("Connected to your shared saved hotel settings."); }
    catch (error) { report(error, current, true); }
  });
  $("logout").addEventListener("click", () => { clear(); message("Signed out. The access token and displayed data were cleared."); });
  $("reload").addEventListener("click", async () => {
    if ((parameterDirty || staffDirty) && !window.confirm("Discard unsaved form edits and reload saved data?")) return;
    const current = context();
    try { await reload(current); check(current); message("Saved settings and roster versions refreshed."); } catch (error) { report(error, current); }
  });
  ["capacity", "checkout", "stayover"].forEach((id) => $(id).addEventListener("input", () => { parameterDirty = true; }));
  $("staff-json").addEventListener("input", () => { staffDirty = true; });
  $("parameters-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const current = context();
    const values = {room_capacity: $("capacity").value === "" ? null : Number($("capacity").value), checkout_minutes: Number($("checkout").value), stayover_minutes: Number($("stayover").value)};
    try { const data = await api("/parameters", {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({values, expected_revision: parameterRevision})}, "json", current); check(current); applySettings(data, false, "parameters"); message("Parameters saved to the same store used by WhatsApp. Existing roster versions remain unchanged."); }
    catch (error) { report(error, current); }
  });
  $("staff-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const current = context();
    try { const staff = JSON.parse($("staff-json").value); const data = await api("/staff", {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({staff, expected_revision: staffRevision})}, "json", current); check(current); applySettings(data, false, "staff"); message("Staff saved. Future processing uses the updated availability; existing versions are preserved."); }
    catch (error) { report(error, current); }
  });
  $("download-pdf").addEventListener("click", async () => {
    if (!selectedRoster) return;
    const roster = selectedRoster, current = context();
    try { const blob = await api(`/rosters/${encodeURIComponent(roster.week_start)}/versions/${roster.version}/pdf`, {}, "blob", current); check(current); const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `weekly-roster-${roster.week_start}-v${roster.version}.pdf`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    catch (error) { report(error, current); }
  });
})();
