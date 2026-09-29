// ========================================================
// SERVICE SURRENDER (termination)
// ========================================================

// Escape text before putting it into innerHTML. The API echoes operator-supplied
// values (reasons, original filenames, server messages) back to the page, so
// rendering them unescaped would be an XSS hole.
function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Read the current surrender form as a plain object.
function surrenderFormData(includeFile) {
  const fd = new FormData();
  fd.append("domain", document.getElementById("sur_domain").value.trim());
  fd.append("panel", document.getElementById("sur_panel").value);
  fd.append("scope", document.getElementById("sur_scope").value);
  fd.append("username", document.getElementById("sur_username").value.trim());
  fd.append("reason", document.getElementById("sur_reason").value.trim());
  if (includeFile) {
    const input = document.getElementById("sur_evidence");
    if (input && input.files && input.files.length) {
      fd.append("evidence", input.files[0]);
    }
  }
  return fd;
}

async function previewSurrender() {
  const box = document.getElementById("surrender-preview");
  const domain = document.getElementById("sur_domain").value.trim();
  if (!domain) {
    showToast("Enter a domain first.", "error");
    return;
  }

  box.classList.remove("hidden");
  box.innerHTML = '<span class="form-hint">Checking what would be removed…</span>';

  try {
    const res = await fetch("/api/v1/surrenders/preview", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        domain: domain,
        username: document.getElementById("sur_username").value.trim() || null,
        panel: document.getElementById("sur_panel").value,
        scope: document.getElementById("sur_scope").value,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      box.innerHTML = `<div class="surrender-step-fail">${esc(describeApiError(data, res.status))}</div>`;
      return;
    }

    box.innerHTML = `<h4 class="surrender-preview-title">This will remove:</h4>` + data.steps.map((s) => {
      const cls = s.present === false ? "surrender-step-skip" : "surrender-step-warn";
      const tag = s.present ? "WILL BE REMOVED" : "NOT FOUND";
      const who = s.target === "hosting" ? ` (${esc(s.username || "?")})` : "";
      return `<div class="surrender-step ${cls}">
        <div class="surrender-step-head"><strong>${esc(s.target)}${who}</strong><span class="surrender-tag">${tag}</span></div>
        <p>${esc(s.action)}</p>
      </div>`;
    }).join("") + `<p class="form-hint">${esc(data.note || "")}</p>`;
  } catch (err) {
    box.innerHTML = `<div class="surrender-step-fail">${esc(err.message)}</div>`;
  }
}

async function handleSurrenderSubmit(e) {
  e.preventDefault();

  const domain = document.getElementById("sur_domain").value.trim();
  const typed = document.getElementById("sur_confirm_text").value.trim();
  const fileInput = document.getElementById("sur_evidence");

  // Require typing the domain. This is the last chance to catch a mistake before
  // customer data is destroyed, so it is checked before anything is sent.
  if (!domain || typed.toLowerCase() !== domain.toLowerCase()) {
    showToast(`Type the domain exactly ("${domain}") to confirm.`, "error");
    return;
  }
  if (!fileInput.files || !fileInput.files.length) {
    showToast("Attach the scanned surrender letter (PDF or JPEG).", "error");
    return;
  }
  if (!window.confirm(
    `Permanently surrender ${domain}?\n\nThis destroys hosting files, mail and databases, and removes the domain registration. It cannot be undone.`
  )) {
    return;
  }

  const btn = document.getElementById("btn-surrender-submit");
  const label = document.getElementById("btn-surrender-text");
  const original = label.textContent;
  btn.disabled = true;
  label.textContent = "Surrendering…";

  const fd = surrenderFormData(true);
  fd.append("confirm", "true");

  try {
    const res = await fetch("/api/v1/surrenders", {
      method: "POST",
      headers: apiHeaders(),
      body: fd,
    });
    const data = await res.json();
    renderSurrenderResult(data, res.ok);
    if (data && data.id) loadSurrenderHistory();
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    btn.disabled = false;
    label.textContent = original;
  }
}

function renderSurrenderResult(rec, ok) {
  const box = document.getElementById("surrender-result");
  box.classList.remove("hidden");

  const cls = !ok || rec.status === "failed" ? "surrender-step-fail"
    : (rec.status === "partial" ? "surrender-step-warn" : "surrender-step-ok");
  const title = !ok ? "Surrender failed"
    : (rec.status === "partial" ? "Surrender partially completed — read this"
    : (rec.status === "completed" ? "Surrender completed" : "Surrender result"));

  const actions = (rec.actions || []).map((a) => {
    const c = a.success ? "surrender-step-ok" : "surrender-step-fail";
    return `<div class="surrender-step ${c}">
      <div class="surrender-step-head"><strong>${esc(a.target)}</strong>
      <span class="surrender-tag">${a.success ? "DONE" : "NOT DONE"}</span></div>
      <p>${esc(a.message || "")}</p>
    </div>`;
  }).join("");

  box.className = `surrender-result ${cls}`;
  box.innerHTML = `<h4>${esc(title)}</h4>
    ${rec.id ? `<p class="form-hint">Reference: <code>${esc(rec.id)}</code></p>` : ""}
    ${actions}
    ${rec.status === "partial" ? '<p class="form-hint">One or more steps did not complete. The remaining step can be retried on its own — nothing was silently skipped.</p>' : ""}
    ${!ok && rec.detail ? `<p>${esc(rec.detail)}</p>` : ""}`;
  showToast(title, rec.status === "completed" && ok ? "success" : "error");
}

async function loadSurrenderHistory() {
  const box = document.getElementById("surrender-history");
  if (!box) return;
  try {
    const res = await fetch("/api/v1/surrenders?limit=25", { headers: apiHeaders() });
    if (!res.ok) {
      box.innerHTML = `<span class="form-hint">Unavailable (${res.status}).</span>`;
      return;
    }
    const data = await res.json();
    const rows = data.surrenders || [];
    if (!rows.length) {
      box.innerHTML = '<span class="form-hint">No surrenders recorded yet.</span>';
      return;
    }
    box.innerHTML = `<table class="surrender-table">
      <thead><tr><th>Reference</th><th>Domain</th><th>Scope</th><th>Status</th><th>When</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td><code>${esc(r.id || "")}</code></td>
        <td>${esc(r.domain || "")}</td>
        <td>${esc(r.scope || "")}</td>
        <td class="st-${esc(r.status || "")}">${esc(r.status || "")}</td>
        <td>${esc((r.started_at || "").replace("T", " "))}</td>
      </tr>`).join("")}</tbody></table>`;
  } catch (err) {
    box.innerHTML = `<span class="form-hint">${esc(err.message)}</span>`;
  }
}

// Keep the confirmation phrase in sync with the domain field.
document.addEventListener("DOMContentLoaded", () => {
  const d = document.getElementById("sur_domain");
  const echo = document.getElementById("sur_confirm_echo");
  if (d && echo) echo.textContent = "the domain";
  loadSurrenderHistory();
});

// ========================================================
// SUSPENSION REVIEW
// The nightly job detects; an operator acts from here.
// ========================================================

async function loadSuspensionReport() {
  const summary = document.getElementById("suspension-summary");
  const cands = document.getElementById("suspension-candidates");
  const un = document.getElementById("suspension-unmatched");
  if (!summary) return;

  try {
    const res = await fetch("/api/v1/suspension/report", { headers: apiHeaders() });
    if (!res.ok) {
      summary.textContent = `Report unavailable (HTTP ${res.status}).`;
      cands.textContent = "";
      un.textContent = "";
      return;
    }
    const d = await res.json();
    if (!d.available) {
      summary.textContent = d.message;
      cands.textContent = "";
      un.textContent = "";
      return;
    }

    const age = d.age_hours;
    const doneCount = ((d.candidates_all) || []).filter((c) => c.live_suspended).length;
    const staleNote = d.stale
      ? ` <strong style="color:#fcd34d">⚠ STALE — last run was ${esc(age)}h ago. ` +
        `A failed run writes no record, so this list may be out of date; check the ` +
        `nightly job before acting.</strong>`
      : (age !== null && age !== undefined ? ` (${esc(age)}h ago)` : "");

    summary.innerHTML =
      `Last run <strong>${esc(d.generated_at)}</strong>${staleNote} · ` +
      `${esc(d.total_accounts)} accounts checked · ` +
      `<strong>${esc((d.candidates || []).length)}</strong> awaiting review` +
      (doneCount ? ` · <span style="color:#6ee7b7">${esc(doneCount)} already suspended since</span>` : "") +
      ` · ${esc(d.already_suspended_billing)} already suspended for billing · ` +
      `${esc(d.suspended_other_reason)} suspended for other reasons (left alone) · ` +
      `${esc(d.no_match)} no billing match` +
      (d.bscs_complete ? "" : ` <strong style="color:#fcd34d">· INCOMPLETE: ${esc(d.bscs_note)}</strong>`);

    renderSuspensionCandidates(d.candidates || [], d.candidates_all || []);
    renderUnmatched(d.unmatched_contracts || []);
    renderActivatable(d.activatable || []);
  } catch (e) {
    summary.textContent = e.message;
  }
}

function renderSuspensionCandidates(candidates, all) {
  const box = document.getElementById("suspension-candidates");
  if (!candidates.length && !(all || []).length) {
    box.innerHTML = '<span class="form-hint">Nothing awaiting review.</span>';
    return;
  }

  // Anything the run flagged that has since been suspended is shown as done, so
  // a completed action visibly stays completed instead of reappearing as
  // pending on every page load until the next nightly run.
  const done = (all || []).filter((c) => c.live_suspended);

  const rows = (candidates || []).map((c) => `<tr>
      <td>${esc(c.panel)}</td>
      <td>${esc(c.username)}</td>
      <td><strong>${esc(c.domain)}</strong></td>
      <td><code>${esc(c.contract || "-")}</code></td>
      <td><span class="pill pill-pending">not suspended</span></td>
      <td><button class="btn btn-danger btn-sm"
            onclick="suspendNow('${esc(c.panel)}','${esc(c.username)}')">Suspend</button></td>
    </tr>`).join("");

  const doneRows = done.map((c) => `<tr class="row-done">
      <td>${esc(c.panel)}</td>
      <td>${esc(c.username)}</td>
      <td>${esc(c.domain)}</td>
      <td><code>${esc(c.contract || "-")}</code></td>
      <td><span class="pill pill-done">suspended${c.live_reason ? " &middot; " + esc(c.live_reason) : ""}</span></td>
      <td class="muted">done</td>
    </tr>`).join("");

  box.innerHTML = `<table class="surrender-table">
    <thead><tr><th>Panel</th><th>Account</th><th>Domain</th><th>Contract</th><th>Live status</th><th>Action</th></tr></thead>
    <tbody>${rows}${doneRows}</tbody></table>` +
    (done.length ? `<p class="form-hint">${esc(done.length)} of these were suspended from
      this page after the last run — shown as done, verified against the panel just now.</p>` : "");
}

function renderUnmatched(rows) {
  const box = document.getElementById("suspension-unmatched");
  if (!rows.length) {
    box.innerHTML = '<span class="form-hint">None — every lapsed contract matched an account.</span>';
    return;
  }
  box.innerHTML = `<table class="surrender-table">
    <thead><tr><th>Contract</th><th>Customer record</th><th>Domains found</th></tr></thead>
    <tbody>${rows.map((r) => `<tr>
      <td><code>${esc(r.contract || "-")}</code></td>
      <td>${esc(r.name_field || "-")}</td>
      <td>${esc((r.domains_found || []).join(", ") || "none")}</td>
    </tr>`).join("")}</tbody></table>`;
}

function renderActivatable(rows) {
  const box = document.getElementById("suspension-activatable");
  if (!box) return;
  if (!rows.length) {
    box.innerHTML = '<span class="form-hint">None — no accounts are suspended for '
                    + 'billing.</span>';
    return;
  }
  box.innerHTML = `<table class="surrender-table">
    <thead><tr><th>Panel</th><th>Account</th><th>Domain</th><th>Suspended for</th>
      <th>Action</th></tr></thead>
    <tbody>${rows.map((r) => `<tr>
      <td>${esc(r.panel)}</td>
      <td>${esc(r.username)}</td>
      <td><strong>${esc(r.domain)}</strong></td>
      <td><span class="pill pill-pending">${esc(r.reason || "billing")}</span></td>
      <td><button class="btn btn-secondary btn-sm"
            onclick="activateNow('${esc(r.panel)}','${esc(r.username)}')">Activate</button></td>
    </tr>`).join("")}</tbody></table>
    <p class="form-hint">${rows.length} account(s) from the last run. The reason is
      re-checked on the panel before anything is activated.</p>`;
}

// Put a customer's website back up after payment.
//
// The warning is deliberately specific: the server will refuse anything not
// suspended for billing, and this says so before the click rather than after.
async function activateNow(panel, username) {
  if (!window.confirm(
    `Activate ${username} on ${panel}?\n\nThis makes the customer's website live again. `
    + `Only accounts suspended for billing can be activated -- one suspended for `
    + `abuse, spam or compromise is refused by the server.`
  )) {
    return;
  }

  const fd = new FormData();
  fd.append("panel", panel);
  fd.append("username", username);
  fd.append("confirm", "true");

  const box = document.getElementById("suspension-result");
  box.classList.remove("hidden");
  box.innerHTML = '<span class="form-hint">Working…</span>';

  try {
    const res = await fetch("/api/v1/suspension/activate", {
      method: "POST",
      headers: apiHeaders(),
      body: fd,
    });
    const data = await res.json();
    if (res.ok) {
      const already = !!data.already_active;
      box.className = "surrender-result surrender-step-ok";
      box.innerHTML = `<h4>${already ? "Already active" : "Activated"}</h4>`
        + `<p>${esc(data.message)}</p>`;
      showToast(already ? `${username} is already active`
                        : `${username} is back online`, "success");
    } else {
      const msg = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
      // 409 is a refusal, not a failure: the account is suspended for another
      // reason and the server declined on purpose.
      const refused = res.status === 409;
      box.className = `surrender-result ${refused ? "surrender-step-warn" : "surrender-step-fail"}`;
      box.innerHTML = `<h4>${refused ? "Not activated" : "Failed"}</h4><p>${esc(msg)}</p>`;
      showToast(refused ? "Refused — not a billing suspension" : "Activation failed",
                "error");
    }
    await loadSuspensionReport();
    await loadActivity();
  } catch (e) {
    box.className = "surrender-result surrender-step-fail";
    box.innerHTML = `<h4>Failed</h4><p>${esc(e.message)}</p>`;
  }
}

async function suspendNow(panel, username) {
  if (!window.confirm(
    `Suspend ${username} on ${panel}?\n\nThis takes the customer's website offline. ` +
    `The panel state is re-checked first, and an account already suspended ` +
    `(for abuse, for example) will be refused rather than relabelled.`
  )) {
    return;
  }

  const fd = new FormData();
  fd.append("panel", panel);
  fd.append("username", username);
  fd.append("reason", "billing");
  fd.append("confirm", "true");

  const box = document.getElementById("suspension-result");
  box.classList.remove("hidden");
  box.innerHTML = '<span class="form-hint">Working…</span>';

  try {
    const res = await fetch("/api/v1/suspension/suspend", {
      method: "POST",
      headers: apiHeaders(),
      body: fd,
    });
    const data = await res.json();
    if (res.ok) {
      box.className = "surrender-result surrender-step-ok";
      box.innerHTML = `<h4>Suspended</h4><p>${esc(data.message)}</p>`;
      showToast(`Suspended ${username}`, "success");
    } else {
      const msg = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
      // 409 is the expected refusal: already suspended, so nothing was changed.
      const isRefusal = res.status === 409;
      box.className = `surrender-result ${isRefusal ? "surrender-step-warn" : "surrender-step-fail"}`;
      box.innerHTML = `<h4>${isRefusal ? "Left untouched" : "Failed"}</h4><p>${esc(msg)}</p>`;
      showToast(isRefusal ? "Account already suspended — nothing changed" : "Suspension failed",
                isRefusal ? "error" : "error");
    }
    await loadSuspensionReport();
  } catch (e) {
    box.className = "surrender-result surrender-step-fail";
    box.innerHTML = `<h4>Failed</h4><p>${esc(e.message)}</p>`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  loadSuspensionReport();
});

// ========================================================
// nic.bt.bt REGISTRY FORM
// All 23 fields the registry requires, rendered from the same field spec the
// client builds its payload from, so the form and the submission cannot
// disagree. Values are prefilled the way the client has always derived them,
// and any of them can be overridden.
// ========================================================

// Shared by every instance: the spec, the extension list and the derivation
// rules. Fetched once and reused, because both the hosting form and the domain
// service card render from the same 23 fields and must not drift apart.
let REG_SPEC = null;          // the 23-field spec from the API
let REG_EXTENSIONS = null;    // the registry's own extension dropdown

const REG_SOURCE_LABEL = {
  customer: "from customer",
  derived: "copied from another field",
  default: "Bhutan Telecom default",
  computed: "from the domain",
};

const REG_PLACEHOLDER = {
  address: "Thimphu, Bhutan",
  postalcode: "-",
  phone: "+975",
  country: "BT",
};

// Split a domain exactly the way the server does: the registry's own options
// longest-first, then a plain first-label split, and .bt as the fallback. Using
// the offered list rather than a hardcoded one keeps this from drifting.
function splitDomainExt(domain, offered) {
  const full = (domain || "").trim().toLowerCase().replace(/\.+$/, "");
  const known = (offered || []).slice().sort((a, b) => b.length - a.length);
  for (const e of known) {
    if (full.length > e.length && full.endsWith(e)) return [full.slice(0, -e.length), e];
  }
  const i = full.indexOf(".");
  if (i > 0) return [full.slice(0, i), full.slice(i)];
  return [full, ".bt"];
}

function regField(name) {
  return (REG_SPEC && REG_SPEC.fields || []).find((f) => f.name === name);
}

/**
 * One instance of the nic.bt.bt field form.
 *
 * It is an object rather than a set of page-wide functions because there are now
 * two of these on the page -- one inside the hosting form, one in the domain
 * service card -- and the previous version found its inputs with a document-wide
 * querySelector, so two copies would have silently read each other's fields.
 * Everything is now scoped to this.root.
 */
class RegistryForm {
  /**
   * @param {object} opts
   *   root        - element the fields are rendered into
   *   missingEl   - element for the "these are empty" warning
   *   domainInput - element the computed domain/ext fields follow
   */
  constructor(opts) {
    this.root = opts.root;
    this.missingEl = opts.missingEl || null;
    this.domainInput = opts.domainInput || null;
    this.touched = new Set();
    this.loaded = false;
  }

  input(name) {
    return this.root ? this.root.querySelector(`[data-reg="${name}"]`) : null;
  }

  currentDomain() {
    return ((this.domainInput && this.domainInput.value) || "").trim();
  }

  // The value a field takes when the operator has not overridden it. Mirrors
  // build_registry_payload in nic_client.py: a derived field follows its parent,
  // a Bhutan Telecom default is used where the registry needs one, and the two
  // computed fields come from the domain.
  fallback(name, field) {
    switch (name) {
      case "domain": return splitDomainExt(this.currentDomain(), REG_EXTENSIONS || [])[0];
      case "ext": return splitDomainExt(this.currentDomain(), REG_EXTENSIONS || [])[1];
      case "reg_renewal": {
        const box = this.input("reg_renewal");
        return (box && box.value) || "";
      }
      default: break;
    }
    if (field.source === "derived" && field.derived_from) {
      return this.fallback(field.derived_from, regField(field.derived_from) || {});
    }
    if (field.source === "default") return field.default || "";
    return REG_PLACEHOLDER[name] || "";
  }

  // Repaint every field the operator has not touched.
  sync() {
    if (!REG_SPEC || !this.loaded) return;
    REG_SPEC.fields.forEach((f) => {
      if (this.touched.has(f.name)) return;
      const el = this.input(f.name);
      if (!el) return;
      const value = this.fallback(f.name, f);
      if (el.tagName === "SELECT") {
        el.value = value;
        if (value && el.value !== value) {
          // The extension list could not be read, so this value is not on offer.
          el.insertBefore(new Option(`${value} (not on offer)`, value), el.firstChild);
          el.value = value;
        }
      } else {
        el.value = value;
      }
      const group = el.closest(".nic-reg-field");
      if (group) group.classList.toggle("is-touched", this.touched.has(f.name));
    });
    this.renderMissing();
  }

  // The resolved values, as they will be submitted. Blank entries are omitted
  // so the server applies its own fallback rather than being handed "".
  values() {
    const out = {};
    if (!REG_SPEC) return out;
    REG_SPEC.fields.forEach((f) => {
      const el = this.input(f.name);
      if (!el) return;
      const v = (el.value || "").trim();
      if (v) out[f.name] = v;
    });
    return out;
  }

  // Required fields left empty. nic.bt.bt rejects the whole submission if any of
  // them is blank, so this is checked before anything is created.
  missing() {
    if (!REG_SPEC) return [];
    return REG_SPEC.fields
      .filter((f) => f.required)
      .filter((f) => {
        const el = this.input(f.name);
        return !el || !(el.value || "").trim();
      })
      .map((f) => f.name);
  }

  renderMissing() {
    if (!this.missingEl) return;
    const missing = this.missing();
    this.missingEl.hidden = missing.length === 0;
    this.missingEl.textContent = missing.length
      ? `Still empty — nic.bt.bt will reject the submission: ${missing.join(", ")}`
      : "";
  }

  async load() {
    if (this.loaded) return;
    const res = await fetch("/api/v1/nic/field-spec", { headers: apiHeaders() });
    if (!res.ok) {
      this.root.innerHTML =
        `<p class="form-hint">Could not load the registry's field list (HTTP ${res.status}).</p>`;
      return;
    }
    REG_SPEC = await res.json();

    // The extension list is the registry's own dropdown, not a copy of it.
    if (!REG_EXTENSIONS) {
      try {
        const extRes = await fetch("/api/v1/nic/extensions", { headers: apiHeaders() });
        const data = extRes.ok ? await extRes.json() : {};
        REG_EXTENSIONS = (data.extensions || []).filter(Boolean);
      } catch (e) {
        REG_EXTENSIONS = [];
      }
    }

    this.root.innerHTML = (REG_SPEC.groups || []).map(([key, label]) => {
      const fields = REG_SPEC.fields.filter((f) => f.group === key);
      if (!fields.length) return "";
      return `<div class="nic-reg-group">
                <h4 class="nic-reg-group-title">${esc(label)}
                  <span class="nic-reg-group-count">${fields.length}</span>
                </h4>
                <div class="nic-reg-grid">${fields.map((f) => this.row(f)).join("")}</div>
              </div>`;
    }).join("");

    // An edit to one field can change fields copied from it, so re-derive the rest.
    this.root.addEventListener("input", (e) => this.onEdit(e));
    this.root.addEventListener("change", (e) => this.onEdit(e));
    this.root.addEventListener("click", (e) => this.onMirrorClick(e));
    this.loaded = true;
    this.sync();
  }

  row(f) {
    const req = f.required ? ' <span class="required">*</span>' : "";
    let control;
    if (f.name === "ext") {
      const opts = (REG_EXTENSIONS || []).map(
        (e) => `<option value="${esc(e)}">${esc(e)}</option>`);
      control = `<select data-reg="ext" name="ext">
          ${opts.length ? opts.join("") : '<option value="">Unavailable</option>'}
        </select>`;
    } else if (f.name === "reg_renewal") {
      control = `<input type="date" data-reg="reg_renewal" name="reg_renewal">`;
    } else {
      const type = f.name === "email" ? "email" : "text";
      control = `<input type="${type}" data-reg="${esc(f.name)}" name="${esc(f.name)}"
          placeholder="${esc(f.default || "e.g. " + (f.label || "").toLowerCase())}">`;
    }
    return `<div class="form-group nic-reg-field">
        <label class="form-label">${esc(f.label)}${req}</label>
        <div class="input-wrapper">${control}</div>
        <span class="form-hint nic-reg-source" data-source="${esc(f.source)}">
          ${f.source === "derived" ? `copied from ${esc(f.derived_from)}` : esc(REG_SOURCE_LABEL[f.source] || f.source)}
        </span>
      </div>`;
  }

  onEdit(e) {
    const el = e.target;
    const name = el && el.getAttribute && el.getAttribute("data-reg");
    if (name) this.touched.add(name);
    this.sync();
  }

  // The domain name is derived rather than typed, so clicking it unlocks it and
  // marks it an override. Deliberate, and reversible by Reset.
  onMirrorClick(e) {
    const el = e.target;
    if (!el || !el.classList || !el.classList.contains("nic-reg-mirror")) return;
    el.readOnly = false;
    el.classList.remove("nic-reg-mirror");
    el.title = "";
    this.touched.add("domain");
    el.focus();
    el.select();
    this.sync();
  }

  reset() {
    this.touched.clear();
    const mirror = this.root.querySelector('[data-reg="domain"]');
    if (mirror) {
      mirror.readOnly = true;
      mirror.classList.add("nic-reg-mirror");
    }
    this.sync();
  }
}

// The two instances. The hosting form's registry block, and the domain service
// card. They share the spec and the extension list but keep their own field
// values, because they are for different customers.
let hostingRegistry = null;
let domainRegistry = null;

// ========================================================
// FRONTEND INTERACTIONS - AUTOMATION WEBSERVICE BT
// ========================================================

let latestAccountData = null;

// Initialize on load
document.addEventListener("DOMContentLoaded", () => {
  generateNewPassword();
  checkServerHealth();

  // API token: only needed when API_AUTH_TOKEN is set in .env. Kept in
  // localStorage so it survives a page reload, and never rendered into the
  // served HTML.
  const tokenInput = document.getElementById("api_token");
  if (tokenInput) {
    tokenInput.value = localStorage.getItem("api_token") || "";
    tokenInput.addEventListener("input", () => {
      const v = tokenInput.value.trim();
      if (v) localStorage.setItem("api_token", v);
      else localStorage.removeItem("api_token");
      updateAuthBanner();
    });
  }
  updateAuthBanner();

  const dnsBtn = document.getElementById("btn-dns-check");
  if (dnsBtn) dnsBtn.addEventListener("click", checkDnsFromToolbar);
  const dnsInput = document.getElementById("dns_domain");
  if (dnsInput) {
    dnsInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); checkDnsFromToolbar(); }
    });
  }

  // The nic.bt.bt registry block inside the hosting form. Loaded on first use
  // rather than at page load, so a plain hosting provision does not pay for it.
  const nicToggle = document.getElementById("register_nic");
  const nicFields = document.getElementById("nic-fields");
  if (nicToggle && nicFields) {
    hostingRegistry = new RegistryForm({
      root: document.getElementById("nic-reg-form"),
      missingEl: document.getElementById("nic-reg-missing"),
      domainInput: document.getElementById("domain"),
    });
    const sync = () => {
      nicFields.hidden = !nicToggle.checked;
      if (nicToggle.checked) hostingRegistry.load();
      if (nicToggle.checked) hostingRegistry.sync();
    };
    nicToggle.addEventListener("change", sync);
    sync();
  }

  const refill = document.getElementById("nic-reg-refill");
  if (refill) refill.addEventListener("click", () => hostingRegistry && hostingRegistry.reset());

  // The domain drives the registry block and the DNS pre-check.
  const domain = document.getElementById("domain");
  if (domain) {
    domain.addEventListener("input", () => {
      if (hostingRegistry) hostingRegistry.sync();
      scheduleDnsPrecheck();
    });
    domain.addEventListener("change", () => {
      if (hostingRegistry) hostingRegistry.sync();
      scheduleDnsPrecheck();
    });
  }

  initDomainServices();
  loadActivity();
});

// The token to send as X-API-Token, or null when the API is unauthenticated.
function apiToken() {
  return (localStorage.getItem("api_token") || "").trim() || null;
}

// Build headers for an API call, including the token when one is stored.
function apiHeaders(extra) {
  const headers = Object.assign({}, extra || {});
  const t = apiToken();
  if (t) headers["X-API-Token"] = t;
  return headers;
}

// Tell the operator whether the API expects a token, so a 401 is not a mystery.
function updateAuthBanner() {
  const banner = document.getElementById("auth-banner");
  if (!banner) return;
  if (apiToken()) {
    banner.classList.add("hidden");
  } else {
    banner.classList.remove("hidden");
  }
}

// Turn an API error body into a message worth showing a user.
//
// A 422 from FastAPI carries {"detail": [{loc, msg, type}, ...]} rather than
// {"message": ...}, so reading result.message alone would hide validation
// errors behind a generic failure and point at the wrong thing (.env
// credentials) when the real problem is the domain the user typed.
function describeApiError(result, status) {
  if (result && result.message) return result.message;

  const detail = result && result.detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((e) => {
      const field = Array.isArray(e.loc) ? e.loc[e.loc.length - 1] : null;
      // Drop pydantic's "Value error, " prefix -- it is noise for end users.
      const msg = String(e.msg || "").replace(/^Value error,\s*/, "");
      return field ? `${field}: ${msg}` : msg;
    }).join("\n");
  }
  if (typeof detail === "string") return detail;

  return status === 422
    ? "Please check the highlighted fields and try again."
    : "Failed to provision account.";
}

// Render the outcome of the nic.bt.bt registry push, if one was requested.
// The per-panel steps that run after an account is created: IPv6 on cPanel,
// SFTP access on DirectAdmin. Neither can fail the account, so a failure here
// is a warning the operator has to act on -- and one that is not shown is one
// that gets missed.
function renderPostCreate(steps) {
  const box = document.getElementById("res-post-create");
  if (!box) return;
  if (!steps || !steps.length) { box.classList.add("hidden"); box.innerHTML = ""; return; }

  const ok = steps.filter((s) => s.success).length;
  const bad = steps.length - ok;
  box.className = `res-dns-status ${bad ? "dns-warn" : "dns-ok"}`;
  box.classList.remove("hidden");
  box.innerHTML = `<div class="dns-head">
      <span class="dns-badge">${bad ? `${bad} step needs attention` : "Setup steps complete"}</span>
      <code>${ok} of ${steps.length} done</code>
    </div>` + steps.map((s) => `<p class="${s.success ? "pc-ok" : "pc-bad"}">
      ${s.success ? "✓" : "⚠"} <strong>${esc(s.step || "step")}</strong> — ${esc(s.message || "")}
    </p>`).join("") +
    (bad ? `<p class="pc-note">The hosting account was created. Only the step above
      did not complete; the customer cannot use that part until it is done.</p>` : "");
}

function renderNicStatus(nic) {
  const box = document.getElementById("res-nic-status");
  if (!box) return;

  // No NIC block in the response means the option was not ticked.
  if (nic === null || nic === undefined) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }

  const skipped = nic.action === "skipped";
  const ok = !!nic.success && !skipped;
  const label = nic.action === "created" ? "Registered"
    : (nic.action === "updated" ? "Updated"
    : (nic.action === "skipped" ? "Skipped (dry-run)" : "Result"));
  const icon = skipped ? "ⓘ" : (ok ? "✓" : "⚠");
  box.classList.remove("hidden");
  box.className = `res-nic-status ${skipped ? "nic-skip" : (ok ? "nic-ok" : "nic-fail")}`;
  box.innerHTML = `
    <div class="res-nic-head">
      <span class="res-nic-badge">${icon}</span>
      <strong>nic.bt.bt — ${label}</strong>
    </div>
    <p>${nic.message || ""}</p>
  `;

  if (!ok && !skipped) {
    showToast("Hosting account created, but the nic.bt.bt update failed. Check the panel.", "error");
  }
}

// ========================================================
// DNS MAPPING CHECK
// An account existing is not the same as a domain reaching it. The credentials
// in the handover kit look identical either way, so the DNS result is shown
// next to them rather than left to be assumed.
// ========================================================

const DNS_LABEL = {
  mapped: "Points to this server",
  not_mapped: "Points somewhere else",
  unresolved: "Does not resolve",
};

function dnsBadge(d) {
  if (!d) return "";
  const icon = d.status === "mapped" ? "✓"
    : (d.status === "unresolved" ? "?" : "⚠");
  return `${icon} ${DNS_LABEL[d.status] || d.status}`;
}

// Render a DNS result. `compact` is the handover-kit line; the standalone box
// shows the addresses too, since that is where someone came to be told.
function renderDns(box, d, compact) {
  if (!box) return;
  if (!d) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  const cls = d.status === "mapped" ? "dns-ok"
    : (d.status === "unresolved" ? "dns-warn" : "dns-fail");
  box.className = `${box.id === "res-dns-status" ? "res-dns-status" : "dns-result"} ${cls}`;
  box.classList.remove("hidden");
  box.innerHTML = `<div class="dns-head">
      <span class="dns-badge">${esc(dnsBadge(d))}</span>
      <code>${esc(d.domain)}</code>
    </div>
    <p>${esc(d.message || "")}</p>` +
    (compact ? "" : `<div class="dns-detail">
        <span>resolves to <code>${esc((d.resolved || []).join(", ") || "nothing")}</code></span>
        <span>this server is <code>${esc((d.ours || []).join(", ") || "no address configured")}</code></span>
        ${d.web_answers === true ? '<span class="dns-port">port 80 answers</span>'
          : (d.web_answers === false ? '<span class="dns-port-off">port 80 did not answer</span>' : "")}
      </div>`);
}

async function runDnsCheck(domain, panel) {
  const res = await fetch(
    `/api/v1/dns/check?domain=${encodeURIComponent(domain)}&panel=${encodeURIComponent(panel)}`,
    { headers: apiHeaders() });
  const data = await res.json();
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

// The last result, so the handover kit can show it without looking it up a
// second time. DNS does not change while the operator is filling the form in.
let LAST_DNS = { domain: null, result: null };

// Checked while the domain is being typed, so the answer is known before
// anything is created rather than afterwards.
let dnsPrecheckTimer = null;
let dnsPrecheckSeq = 0;

// Fallback for the case where the pre-check did not run or did not finish: the
// domain typed too quickly, the field changed after the check, or the operator
// arrived with the box already filled. The account exists by this point, so this
// can only ever add information.
async function checkDnsAfterProvisioning(domain, panel) {
  const box = document.getElementById("res-dns-status");
  if (!box || !domain) return;
  box.classList.remove("hidden");
  box.className = "res-dns-status";
  box.innerHTML = '<span class="form-hint">Checking whether the domain points at this server&hellip;</span>';
  try {
    renderDns(box, await runDnsCheck(domain, panel || "cpanel"), true);
  } catch (e) {
    box.className = "res-dns-status dns-warn";
    box.innerHTML = `<div class="dns-head"><span class="dns-badge">Not checked</span>
        <code>${esc(domain)}</code></div>
      <p>The DNS check could not be completed. The hosting account is unaffected.</p>`;
  }
}

function scheduleDnsPrecheck() {
  clearTimeout(dnsPrecheckTimer);
  const box = document.getElementById("domain");
  const domain = ((box && box.value) || "").trim();
  const out = document.getElementById("dns-precheck");
  if (!out) return;

  if (!domain || domain.indexOf(".") < 0) {
    out.classList.add("hidden");
    out.innerHTML = "";
    LAST_DNS = { domain: null, result: null };
    return;
  }

  // "Not a domain yet" is not a finding; say nothing until it could resolve.
  out.classList.remove("hidden");
  out.className = "res-dns-status";
  out.innerHTML = '<span class="form-hint">Checking DNS&hellip;</span>';

  // Debounced: a lookup per keystroke would hammer the resolver for no gain.
  dnsPrecheckTimer = setTimeout(() => runDnsPrecheck(domain, out), 700);
}

async function runDnsPrecheck(domain, box) {
  const panelEl = document.querySelector('input[name="panel"]:checked');
  const panel = panelEl ? panelEl.value : "cpanel";
  const seq = ++dnsPrecheckSeq;
  try {
    const result = await runDnsCheck(domain, panel);
    // A newer keystroke may have moved on while this was in flight.
    const box = document.getElementById("domain");
    if (seq !== dnsPrecheckSeq || ((box && box.value) || "").trim() !== domain) return;
    renderDns(box, result, true);
    LAST_DNS = { domain: domain, result: result };
  } catch (e) {
    if (seq !== dnsPrecheckSeq) return;
    box.className = "res-dns-status dns-warn";
    box.innerHTML = `<div class="dns-head"><span class="dns-badge">Not checked</span>
        <code>${esc(domain)}</code></div>
      <p>The DNS check could not be completed. This does not affect provisioning.</p>`;
    LAST_DNS = { domain: null, result: null };
  }
}

async function checkDnsFromToolbar() {
  const input = document.getElementById("dns_domain");
  const panel = document.getElementById("dns_panel");
  const out = document.getElementById("dns-result");
  const btn = document.getElementById("btn-dns-check");
  if (!input || !out) return;
  const domain = input.value.trim();
  if (!domain) { showToast("Enter a domain to check.", "error"); return; }

  btn.disabled = true;
  out.classList.remove("hidden");
  out.className = "dns-result";
  out.innerHTML = '<span class="form-hint">Resolving&hellip;</span>';
  try {
    renderDns(out, await runDnsCheck(domain, panel.value), false);
  } catch (e) {
    out.className = "dns-result dns-fail";
    out.innerHTML = `<p>${esc(e.message)}</p>`;
  } finally {
    btn.disabled = false;
  }
}

// Toast notification helper
function showToast(message, type = "success") {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  
  const icon = type === "success" ? "✓" : "⚠";
  toast.innerHTML = `<span>${icon}</span> <span>${message}</span>`;
  
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(100%)";
    setTimeout(() => toast.remove(), 250);
  }, 3500);
}

// Generate strong password
function generateNewPassword() {
  const chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%&*-_=+";
  let pwd = "";
  for (let i = 0; i < 16; i++) {
    pwd += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  const pwdInput = document.getElementById("password");
  if (pwdInput) {
    pwdInput.value = pwd;
  }
}

// Auto-fill username when domain changes
function onDomainChanged(domain) {
  const userField = document.getElementById("username");
  // Only suggest if the user hasn't explicitly typed something else
  if (!userField.dataset.manualEdit) {
    const clean = domain.split(".")[0].replace(/[^a-zA-Z0-9]/g, "").toLowerCase();
    userField.value = clean.slice(0, 12);
  }
}

document.getElementById("username")?.addEventListener("input", function() {
  this.dataset.manualEdit = "true";
});

// Update panel selection visual
function updatePanelContext() {
  const selected = document.querySelector('input[name="panel"]:checked')?.value || "cpanel";
  loadPackages(selected);
}

// Fetch live packages from selected server
async function loadPackages(panel = "cpanel") {
  const pkgSelect = document.getElementById("package");
  if (!pkgSelect) return;
  try {
    const res = await fetch(`/api/v1/packages?panel=${panel}`, { headers: apiHeaders() });
    const data = await res.json();
    if (data.packages && data.packages.length > 0) {
      pkgSelect.innerHTML = "";
      data.packages.forEach(pkg => {
        const opt = document.createElement("option");
        opt.value = pkg;
        opt.textContent = pkg;
        if (pkg.toLowerCase() === "default" || pkg.toLowerCase() === "bronze") {
          opt.selected = true;
        }
        pkgSelect.appendChild(opt);
      });
    }
  } catch (e) {
    console.warn("Failed to load packages:", e);
  }
}

// Check remote server connectivity
async function checkServerHealth() {
  const dot = document.getElementById("overall-status-dot");
  const text = document.getElementById("overall-status-text");
  const cpBadge = document.getElementById("cpanel-badge");
  const daBadge = document.getElementById("da-badge");

  dot.className = "indicator-dot loading";
  text.textContent = "Checking...";

  try {
    const res = await fetch("/api/v1/servers/status", { headers: apiHeaders() });
    const data = await res.json();

    const cpOk = data.cpanel?.status?.success;
    const daOk = data.directadmin?.status?.success;

    if (cpBadge) {
      cpBadge.textContent = cpOk ? "Online (Ready)" : "Config Needed";
      cpBadge.className = `status-chip ${cpOk ? "online" : ""}`;
    }

    if (daBadge) {
      daBadge.textContent = daOk ? "Online (Ready)" : "Config Needed";
      daBadge.className = `status-chip ${daOk ? "online" : ""}`;
    }

    if (cpOk && daOk) {
      dot.className = "indicator-dot online";
      text.textContent = "Both Servers Connected";
    } else if (cpOk || daOk) {
      dot.className = "indicator-dot online";
      text.textContent = cpOk ? "cPanel Online" : "DirectAdmin Online";
    } else {
      dot.className = "indicator-dot offline";
      text.textContent = "Check .env Connection";
    }
  } catch (err) {
    dot.className = "indicator-dot offline";
    text.textContent = "Status Check Failed";
  }
}

// Handle Form Submission
async function handleProvisionSubmit(e) {
  e.preventDefault();

  const form = document.getElementById("provision-form");
  const submitBtn = document.getElementById("btn-submit");
  const submitText = document.getElementById("btn-submit-text");

  const emptyState = document.getElementById("result-empty-state");
  const loadingState = document.getElementById("result-loading-state");
  const resultContent = document.getElementById("result-content");

  // Gather values
  const panel = form.querySelector('input[name="panel"]:checked').value;
  const domain = document.getElementById("domain").value.trim();
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value.trim();
  const email = document.getElementById("email").value.trim();
  const packagePlan = document.getElementById("package").value.trim();
  const sendEmail = document.getElementById("send_email") ? document.getElementById("send_email").checked : false;
  const dryRun = document.getElementById("dry_run") ? document.getElementById("dry_run").checked : false;

  // nic.bt.bt registry push
  const registerNic = document.getElementById("register_nic") ? document.getElementById("register_nic").checked : false;
  // All 23 registry fields, as the operator left them. Blank ones are omitted so
  // the server applies the same fallback it always has.
  const nicFields = (registerNic && hostingRegistry) ? hostingRegistry.values() : {};
  const nicExt = nicFields.ext || null;

  if (!domain) {
    showToast("Please enter a domain name.", "error");
    return;
  }

  // Check the registry's required fields here rather than after a round-trip:
  // the hosting account is created first, so a rejection from the registry would
  // otherwise leave a half-finished provisioning behind.
  if (registerNic) {
    const missing = hostingRegistry ? hostingRegistry.missing() : [];
    if (missing.length) {
      showToast(`nic.bt.bt still needs: ${missing.join(", ")}`, "error");
      const el = hostingRegistry && hostingRegistry.input(missing[0]);
      if (el) { el.focus(); el.style.borderColor = "var(--accent-rose)"; }
      return;
    }
  }

  // Switch UI to loading
  submitBtn.disabled = true;
  submitText.textContent = dryRun ? "Simulating Provisioning..." : "Provisioning on Server...";
  emptyState.classList.add("hidden");
  resultContent.classList.add("hidden");
  loadingState.classList.remove("hidden");
  document.getElementById("loading-desc-text").textContent = dryRun
    ? `Simulating account setup for ${domain} on ${panel.toUpperCase()}...`
    : `Executing automated user creation on ${panel.toUpperCase()} server via SSH/API...`;

  try {
    const response = await fetch("/api/v1/accounts/create", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        panel: panel,
        domain: domain,
        username: username || null,
        password: password || null,
        email: email || null,
        package: packagePlan || null,
        send_email: sendEmail,
        register_nic: registerNic,
        ext: nicExt,
        nic_fields: nicFields,
        dry_run: dryRun
      })
    });

    const result = await response.json();

    if (!response.ok || !result.success) {
      throw new Error(describeApiError(result, response.status));
    }

    // Success!
    latestAccountData = result.data;
    renderResult(result.data);
    renderNicStatus(result.data.nic_status);
    renderPostCreate(result.data.post_create);
    // Asked for separately, after the account is safely created. Keeping the
    // lookup out of the provisioning request means a slow or broken resolver
    // cannot delay it, and cannot make a successful provisioning look failed.
    // Reuses the pre-check: the same domain, seconds apart, and DNS did not
    // change in between. A fresh lookup here would only risk a second wait.
    const pre = LAST_DNS;
    if (pre.result && pre.domain === result.data.domain) {
      renderDns(document.getElementById("res-dns-status"), pre.result, true);
    } else {
      checkDnsAfterProvisioning(result.data.domain, result.data.panel);
    }
    showToast(`Account for ${result.data.domain} successfully provisioned!`, "success");

  } catch (error) {
    console.error("Provisioning error:", error);
    loadingState.classList.add("hidden");
    emptyState.classList.remove("hidden");
    showToast(error.message, "error");
    alert(`Provisioning Failed:\n\n${error.message}`);
  } finally {
    submitBtn.disabled = false;
    submitText.textContent = "Provision Hosting Account";
  }
}

// Render Provisioning Result
function renderResult(data) {
  const loadingState = document.getElementById("result-loading-state");
  const emptyState = document.getElementById("result-empty-state");
  const resultContent = document.getElementById("result-content");

  loadingState.classList.add("hidden");
  emptyState.classList.add("hidden");
  resultContent.classList.remove("hidden");

  // Meta banner
  document.getElementById("res-success-meta").textContent = 
    `Panel: ${data.panel.toUpperCase()} • Domain: ${data.domain} • User: ${data.username}`;

  // Web UI fields
  document.getElementById("res-web-url").textContent = data.web_url;
  const linkEl = document.getElementById("res-web-url-link");
  linkEl.href = data.web_url;

  document.getElementById("res-web-user").textContent = data.username;
  document.getElementById("res-web-pass").textContent = data.password;

  // SFTP fields
  document.getElementById("res-sftp-host").textContent = data.sftp_host;
  document.getElementById("res-sftp-port").textContent = data.sftp_port;
  document.getElementById("res-sftp-user").textContent = data.username;
  document.getElementById("res-sftp-pass").textContent = data.password;
  document.getElementById("res-sftp-docroot").textContent = data.doc_root;

  // Raw handover text
  document.getElementById("res-handover-raw").value = data.handover_text;

  // Scroll smoothly to results on mobile
  if (window.innerWidth < 960) {
    resultContent.scrollIntoView({ behavior: "smooth" });
  }
}

// Copy helpers
function copyToClipboard(text, message = "Copied to clipboard!") {
  navigator.clipboard.writeText(text).then(() => {
    showToast(message, "success");
  }).catch(() => {
    // Fallback
    const textarea = document.createElement("textarea");
    textarea.value = text;
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
    showToast(message, "success");
  });
}

function copyValue(elementId) {
  const el = document.getElementById(elementId);
  if (el) {
    copyToClipboard(el.textContent.trim(), `Copied: ${el.textContent.trim()}`);
  }
}

function copySFTPConfig() {
  if (!latestAccountData) return;
  const sftpText = `SFTP Host: ${latestAccountData.sftp_host}
SFTP Port: ${latestAccountData.sftp_port}
Username: ${latestAccountData.username}
Password: ${latestAccountData.password}
Document Root: ${latestAccountData.doc_root}`;
  copyToClipboard(sftpText, "Copied SFTP details!");
}

function copyFullHandover() {
  const rawBox = document.getElementById("res-handover-raw");
  if (rawBox && rawBox.value) {
    copyToClipboard(rawBox.value, "Copied full handover letter for customer!");
  }
}

// ========================================================
// DOMAIN SERVICE
// Register a domain on nic.bt.bt, then either hosting or forwarding.
//
// Forwarding is done by BT staff by hand, in systems this service does not
// touch: nic.bt.bt holds no DNS or nameserver records, and .bt delegation
// belongs to ns1/ns2.druknet.bt. So the two things this page does are record
// what was asked for, and check whether it has actually been done. The customer
// is told only after the check passes, because the email asserts something
// factual about the public internet.
// ========================================================

// What the domain's DNS says right now, for the selected kind.
let lastLive = { observed: [] };

async function lookupLiveRecords() {
  const domain = document.getElementById("ds_domain").value.trim();
  const kind = document.getElementById("ds_kind").value;
  const box = document.getElementById("ds-live");
  const text = document.getElementById("ds-live-text");
  const use = document.getElementById("ds-live-use");
  if (!box) return;
  if (!domain || domain.indexOf(".") < 0) {
    box.hidden = true;
    lastLive = { observed: [] };
    return;
  }
  box.hidden = false;
  text.innerHTML = '<span class="form-hint">Looking up…</span>';
  try {
    const res = await fetch(
      `/api/v1/dns/records?domain=${encodeURIComponent(domain)}&kind=${encodeURIComponent(kind)}`,
      { headers: apiHeaders() });
    const data = await res.json();
    if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
    lastLive = data;
    const found = data.observed || [];
    if (!found.length) {
      text.innerHTML = `<span class="form-hint">${esc(data.message)} `
        + `Nothing to copy yet — this is normal before the forwarding is done.</span>`;
      use.hidden = true;
      return;
    }
    use.hidden = false;
    const target = document.getElementById("ds_target").value.trim().toLowerCase();
    const wanted = target.replace(/\s+/g, "").split(",").filter(Boolean).sort();
    const agree = wanted.length > 0 && wanted.join(",") === found.slice().sort().join(",");
    text.innerHTML = `<strong>Live:</strong> <code>${esc(found.join(", "))}</code> `
      + (agree ? '<span class="ds-live-ok">— already what you entered</span>'
               : '<span class="ds-live-diff">— different from what you entered</span>');
  } catch (e) {
    box.hidden = true;
    lastLive = { observed: [] };
  }
}

const DS_STATUS_LABEL = {
  registered: "registered",
  awaiting_dns: "awaiting DNS",
  verified: "verified, not yet told",
  notified: "customer notified",
};

function initDomainServices() {
  const root = document.getElementById("domain-service");
  if (!root) return;

  domainRegistry = new RegistryForm({
    root: document.getElementById("ds-reg-form"),
    missingEl: document.getElementById("ds-reg-missing"),
    domainInput: document.getElementById("ds_domain"),
  });

  const kind = document.getElementById("ds_kind");
  const target = document.getElementById("ds_target");
  const hint = document.getElementById("ds_target_hint");
  const forwarding = document.getElementById("ds_forwarding");
  const service = document.getElementById("ds_service");

  // Read the domain's real records rather than having them typed from memory.
  // A single wrong character in a nameserver fails every later check and looks
  // exactly like the forwarding was never done.
  document.getElementById("ds-live-use").addEventListener("click", () => {
    if (lastLive && lastLive.observed && lastLive.observed.length) {
      target.value = lastLive.observed.join(",");
      target.dispatchEvent(new Event("input"));
    }
  });
  const runLive = () => lookupLiveRecords();
  kind.addEventListener("change", () => { syncKind(); runLive(); });
  document.getElementById("ds_domain").addEventListener("blur", runLive);

  const syncKind = () => {
    const isNameserver = kind.value === "nameserver";
    target.placeholder = isNameserver
      ? "e.g. ns1.host.com,ns2.host.com" : "e.g. 198.51.100.9";
    hint.textContent = isNameserver
      ? "The name servers the domain should be delegated to. Comma separated."
      : "The address the domain should resolve to.";
    if (isNameserver) {
      // An address left in here from switching back would verify against the
      // wrong thing and quietly never match.
      if (target.value && /^\d{1,3}(\.\d{1,3}){3}$/.test(target.value.trim())) {
        target.value = "";
      }
    } else if (target.value && !/^\d{1,3}(\.\d{1,3}){3}$/.test(target.value.trim())) {
      target.value = "";
    }
  };
  syncKind();

  // Forwarding details only matter for forwarding.
  const syncService = () => { forwarding.hidden = service.value !== "forwarding"; };
  service.addEventListener("change", syncService);
  syncService();

  document.getElementById("btn-ds-register")
    .addEventListener("click", registerDomainService);
  document.getElementById("ds-email-send")
    .addEventListener("click", sendEditedEmail);
  document.getElementById("ds-email-cancel")
    .addEventListener("click", closeEmailEditor);
  document.getElementById("ds-email-reset").addEventListener("click", () => {
    if (!emailDefault) return;
    document.getElementById("ds-email-subject").value = emailDefault.subject;
    document.getElementById("ds-email-body").value = emailDefault.body;
  });
  document.getElementById("ds-reg-refill")
    .addEventListener("click", () => domainRegistry && domainRegistry.reset());
  document.getElementById("ds_domain").addEventListener("input", () => {
    domainRegistry.load();
    domainRegistry.sync();
  });

  loadDomainServiceQueue();
}

async function registerDomainService() {
  const domain = document.getElementById("ds_domain").value.trim().toLowerCase();
  const customer = document.getElementById("ds_customer").value.trim();
  const email = document.getElementById("ds_email").value.trim();
  const service = document.getElementById("ds_service").value;
  const kind = document.getElementById("ds_kind").value;
  const target = document.getElementById("ds_target").value.trim();
  const out = document.getElementById("ds-result");

  const fail = (msg) => {
    out.className = "surrender-result surrender-step-fail";
    out.classList.remove("hidden");
    out.innerHTML = `<h4>Cannot register</h4><p>${esc(msg)}</p>`;
  };

  if (!domain) return fail("Enter a domain.");
  if (!customer) return fail("Enter the registered owner's name.");
  if (!email || email.indexOf("@") < 0) return fail("Enter a valid customer email.");
  if (service === "forwarding" && !target) {
    return fail("Enter what the domain is pointed at, so the forwarding can be " +
                "checked later. Without it there is nothing to verify against.");
  }

  const missing = domainRegistry.missing();
  if (missing.length) {
    out.className = "surrender-result surrender-step-fail";
    out.classList.remove("hidden");
    out.innerHTML = `<h4>nic.bt.bt needs more</h4><p>${esc(missing.join(", "))}</p>`;
    return;
  }

  out.className = "surrender-result";
  out.classList.remove("hidden");
  out.innerHTML = '<span class="form-hint">Registering on nic.bt.bt…</span>';

  try {
    const res = await fetch("/api/v1/domain-services/register", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        domain, customer_name: customer, email, service,
        forwarding_kind: service === "forwarding" ? kind : null,
        forwarding_target: service === "forwarding" ? target : null,
        nic_fields: domainRegistry.values(),
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      out.className = "surrender-result surrender-step-fail";
      out.innerHTML = `<h4>Registration failed</h4><p>${esc(describeApiError(data, res.status))}</p>`;
      return;
    }
    out.className = "surrender-result surrender-step-ok";
    out.innerHTML = `<h4>Registered on nic.bt.bt</h4><p>${esc(data.nic.message || "")}</p>`
      + (service === "forwarding"
        ? `<p>It is now waiting on the forwarding being done. It will appear under
             <em>Domains awaiting DNS</em> — notify the customer once the check passes.</p>`
        : `<p>Now create the hosting account from the provisioning form above.</p>`);
    showToast(`${domain} registered`, "success");
    loadDomainServiceQueue();
    loadActivity();
  } catch (e) {
    out.className = "surrender-result surrender-step-fail";
    out.innerHTML = `<h4>Failed</h4><p>${esc(e.message)}</p>`;
  }
}

async function loadDomainServiceQueue() {
  const box = document.getElementById("ds-queue");
  if (!box) return;
  try {
    const res = await fetch("/api/v1/domain-services", { headers: apiHeaders() });
    if (!res.ok) { box.textContent = `Unavailable (HTTP ${res.status}).`; return; }
    const data = await res.json();
    const rows = data.services || [];
    const waiting = rows.filter((r) => r.status === "awaiting_dns" || r.status === "verified");
    const done = rows.filter((r) => r.status === "notified");

    if (!rows.length) {
      box.innerHTML = '<span class="form-hint">Nothing yet — register a domain above.</span>';
      return;
    }

    const row = (r, actionable) => `<tr>
      <td><strong>${esc(r.domain)}</strong></td>
      <td>${esc(r.customer_name || "-")}</td>
      <td>${esc(r.forwarding_kind === "nameserver" ? "name servers" : "A record")}</td>
      <td><code>${esc(r.forwarding_target || "-")}</code></td>
      <td>${r.last_check_status
            ? `<span class="pill ${r.last_check_status === "forwarded" ? "pill-done" : "pill-pending"}">${esc(r.last_check_status)}</span>`
            : '<span class="pill pill-pending">not checked</span>'}</td>
      <td>${actionable
            ? `<button class="btn btn-secondary btn-sm" onclick="verifyDomain('${esc(r.domain)}')">Check</button>
               ${r.status === "verified"
                 ? ` <button class="btn btn-primary btn-sm" onclick="openEmailEditor('${esc(r.domain)}')">Notify customer</button>`
                 : ""}`
            : '<span class="muted">told</span>'}</td>
    </tr>`;

    box.innerHTML = `<table class="surrender-table">
      <thead><tr><th>Domain</th><th>Customer</th><th>By</th><th>Pointed at</th>
        <th>DNS check</th><th>Action</th></tr></thead>
      <tbody>${waiting.map((r) => row(r, true)).join("")}${done.map((r) => row(r, false)).join("")}</tbody>
      </table>` + (done.length
        ? `<p class="form-hint">${done.length} already notified — shown for the record.</p>`
        : "") + (waiting.length
          ? `<p class="form-hint">${waiting.length} waiting. The customer is only emailed
             after a check confirms the forwarding; a domain that is not forwarded yet
             cannot be notified.</p>`
          : "");
  } catch (e) {
    box.textContent = e.message;
  }
}

async function verifyDomain(domain) {
  try {
    const res = await fetch("/api/v1/domain-services/verify", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ domain }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast((data && data.detail) || "Check failed", "error");
    } else if (data.check.status === "forwarded") {
      showToast(`${domain} is forwarded — you can notify the customer`, "success");
    } else {
      showToast(data.check.message, "error");
    }
    await loadDomainServiceQueue();
    await loadActivity();
  } catch (e) {
    showToast(e.message, "error");
  }
}

// Tell the customer. The server refuses unless a check has already confirmed the
// forwarding, so the button is not the only thing standing between a customer
// and a false claim -- that is enforced on the server, not here.
async function notifyDomain(domain) {
  if (!window.confirm(
    `Email ${domain}'s customer to say their domain is forwarded?\\n\\n`
    + `This is the only outward-facing step, so it is worth reading the check result `
    + `above first. If the domain has not been forwarded, the server will refuse.`
  )) return;
  try {
    const res = await fetch("/api/v1/domain-services/notify", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ domain }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast(data.message || "Customer notified", "success");
    } else {
      const msg = (data && data.detail) || `HTTP ${res.status}`;
      showToast(msg, "error");
    }
    await loadDomainServiceQueue();
    await loadActivity();
  } catch (e) {
    showToast(e.message, "error");
  }
}

// ========================================================
// RECENT ACTIVITY
// What has been done. The handover kit shows the result of the action you just
// took and disappears on reload; a provisioning used to leave no durable record
// at all, existing only in container logs that rotate.
//
// Refusals are shown too. A refused activation is the answer to "who tried to
// bring back an account suspended for abuse" -- a feed of successes only would
// hide precisely the event worth noticing.
// ========================================================

const ACTIVITY_STYLE = {
  provisioned:      { label: "provisioned", cls: "act-ok" },
  suspended:        { label: "suspended",   cls: "act-warn" },
  activated:        { label: "activated",   cls: "act-ok" },
  domain_registered:{ label: "domain",      cls: "act-info" },
  suspension_run:   { label: "nightly run", cls: "act-info" },
};

async function loadActivity() {
  const box = document.getElementById("activity-list");
  if (!box) return;
  try {
    const res = await fetch("/api/v1/activity?limit=40", { headers: apiHeaders() });
    if (!res.ok) { box.textContent = `Unavailable (HTTP ${res.status}).`; return; }
    const data = await res.json();
    const events = data.events || [];
    const c = data.counts || {};

    document.getElementById("activity-summary").innerHTML =
      `What has been done, newest first &mdash; `
      + `${c.provisioned || 0} provisioned, ${c.suspended || 0} suspended, `
      + `${c.activated || 0} activated, ${c.domain_registered || 0} domains registered.`
      + ` Refusals are kept: they say what somebody tried and what stopped them.`;

    if (!events.length) {
      box.innerHTML = '<span class="form-hint">Nothing recorded yet. Provisioning a '
                    + 'hosting account or registering a domain will appear here.</span>';
      return;
    }

    box.innerHTML = `<table class="surrender-table">
      <thead><tr><th>When</th><th>What</th><th>Domain / account</th>
        <th>Outcome</th></tr></thead>
      <tbody>${events.map((e) => {
        const style = ACTIVITY_STYLE[e.kind] || { label: e.kind, cls: "act-info" };
        const who = [e.domain, e.username].filter(Boolean).join(" &middot; ")
          || e.detail && e.detail.customer || "&mdash;";
        return `<tr>
          <td><span class="act-when">${esc((e.at || "").replace("T", " ").slice(0, 19))}</span></td>
          <td><span class="act-kind ${style.cls}">${esc(style.label)}</span></td>
          <td>${esc(who)}</td>
          <td>${e.outcome === "refused"
            ? '<span class="pill pill-fail">refused</span>'
            : (e.outcome === "failed"
                ? '<span class="pill pill-pending">failed</span>'
                : '<span class="pill pill-done">done</span>')}</td>
        </tr>`;
      }).join("")}</tbody></table>
      <p class="form-hint">${events.length} shown. The formal audit trails for
        surrenders, the nightly job and domain services are kept separately and
        are not replaced by this list.</p>`;
  } catch (e) {
    box.textContent = e.message;
  }
}

// ========================================================
// FORWARDING EMAIL, BEFORE IT IS SENT
// The wording is editable, because it goes to a customer. The default is the
// format BT already uses, including the lookup output, so what the customer
// reads is the same evidence the operator was shown on the page.
//
// What the operator may change is the prose. The gate is not theirs to lift: the
// server still refuses unless a check has passed, whatever is typed here.
// ========================================================

let emailFor = null;      // the domain currently being written to
let emailDefault = null;  // the standard wording, for Reset

async function openEmailEditor(domain) {
  const box = document.getElementById("ds-email-editor");
  const res = await fetch("/api/v1/domain-services/email-preview", {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ domain }),
  });
  const data = await res.json();
  if (!res.ok) {
    showToast((data && data.detail) || "Could not build the email", "error");
    return;
  }
  if (!data.to) {
    showToast("No customer email is recorded for this domain", "error");
    return;
  }
  emailFor = domain;
  emailDefault = { subject: data.subject, body: data.body };
  document.getElementById("ds-email-to").textContent = data.to;
  document.getElementById("ds-email-subject").value = data.subject;
  document.getElementById("ds-email-body").value = data.body;
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  document.getElementById("ds-email-body").focus();
}

function closeEmailEditor() {
  const box = document.getElementById("ds-email-editor");
  if (box) box.hidden = true;
  emailFor = null;
  emailDefault = null;
}

async function sendEditedEmail() {
  if (!emailFor) return;
  const domain = emailFor;
  const subject = document.getElementById("ds-email-subject").value;
  const body = document.getElementById("ds-email-body").value;
  if (!body.trim()) {
    showToast("The message is empty", "error");
    return;
  }
  if (!window.confirm(
    `Send this to the customer for ${domain}?\\n\\n`
    + `It goes to a real customer from ${esc(domain)}'s record, and cannot be unsent.`
  )) return;

  try {
    const res = await fetch("/api/v1/domain-services/notify", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ domain, subject: subject || null, body }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast(data.message || "Customer notified", "success");
      closeEmailEditor();
    } else {
      // 409 here is the server refusing, and it is the control that matters.
      showToast((data && data.detail) || `HTTP ${res.status}`, "error");
    }
    await loadDomainServiceQueue();
    await loadActivity();
  } catch (e) {
    showToast(e.message, "error");
  }
}
