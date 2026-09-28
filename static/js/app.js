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

let REG_SPEC = null;          // the 23-field spec from the API
let REG_EXTENSIONS = null;    // the registry's own extension dropdown
const REG_TOUCHED = new Set();   // fields the operator has edited by hand

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

function regInput(name) {
  return document.querySelector(`[data-reg="${name}"]`);
}

// The value a field takes when the operator has not overridden it. This mirrors
// build_registry_payload in nic_client.py: a derived field follows its parent,
// a Bhutan Telecom default is used where the registry needs one, and the two
// computed fields come from the domain.
function regFallback(name, field) {
  switch (name) {
    case "domain": {
      const [base] = splitDomainExt(currentDomain(), REG_EXTENSIONS || []);
      return base;
    }
    case "ext": {
      const [, ext] = splitDomainExt(currentDomain(), REG_EXTENSIONS || []);
      return ext;
    }
    case "reg_renewal": {
      const box = document.getElementById("renewal_date");
      return (box && box.value) || "";
    }
    default: break;
  }
  if (field.source === "derived" && field.derived_from) {
    return regFallback(field.derived_from, regField(field.derived_from) || {});
  }
  if (field.source === "default") return field.default || "";
  return REG_PLACEHOLDER[name] || "";
}

function regField(name) {
  return (REG_SPEC && REG_SPEC.fields || []).find((f) => f.name === name);
}

function currentDomain() {
  const el = document.getElementById("domain");
  return ((el && el.value) || "").trim();
}

// Repaint every field the operator has not touched. Called whenever the domain
// or a parent field changes, so the copied contacts stay in step.
function syncRegistryFields() {
  if (!REG_SPEC) return;
  REG_SPEC.fields.forEach((f) => {
    if (REG_TOUCHED.has(f.name)) return;
    const el = regInput(f.name);
    if (!el) return;
    const value = regFallback(f.name, f);
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
    if (group) group.classList.toggle("is-touched", REG_TOUCHED.has(f.name));
  });
  renderRegistryMissing();
}

// The resolved values, as the payload will be submitted. Blank entries are
// omitted so the server applies its own fallback rather than being handed "".
function registryFieldValues() {
  const out = {};
  if (!REG_SPEC) return out;
  REG_SPEC.fields.forEach((f) => {
    const el = regInput(f.name);
    if (!el) return;
    const v = (el.value || "").trim();
    if (v) out[f.name] = v;
  });
  return out;
}

// Required fields left empty. nic.bt.bt rejects the whole submission if any of
// them is blank, so this is checked before the hosting account is created rather
// than after.
function missingRegistryFields() {
  if (!REG_SPEC) return [];
  return REG_SPEC.fields
    .filter((f) => f.required)
    .filter((f) => {
      const el = regInput(f.name);
      return !el || !(el.value || "").trim();
    })
    .map((f) => f.name);
}

function renderRegistryMissing() {
  const box = document.getElementById("nic-reg-missing");
  if (!box || !REG_SPEC) return;
  const missing = missingRegistryFields();
  box.hidden = missing.length === 0;
  box.textContent = missing.length
    ? `Still empty — nic.bt.bt will reject the submission: ${missing.join(", ")}`
    : "";
}

async function loadRegistryForm() {
  const box = document.getElementById("nic-reg-form");
  if (!box) return;

  const specRes = await fetch("/api/v1/nic/field-spec", { headers: apiHeaders() });
  if (!specRes.ok) {
    box.innerHTML = `<p class="form-hint">Could not load the registry's field list (HTTP ${specRes.status}).</p>`;
    return;
  }
  REG_SPEC = await specRes.json();

  // The extension list is the registry's own dropdown, not a copy of it.
  try {
    const extRes = await fetch("/api/v1/nic/extensions", { headers: apiHeaders() });
    const data = extRes.ok ? await extRes.json() : {};
    REG_EXTENSIONS = (data.extensions || []).filter(Boolean);
  } catch (e) {
    REG_EXTENSIONS = [];
  }

  const groups = (REG_SPEC.groups || []).map(([key, label]) => {
    const fields = REG_SPEC.fields.filter((f) => f.group === key);
    if (!fields.length) return "";
    const rows = fields.map((f) => regRow(f)).join("");
    return `<div class="nic-reg-group">
              <h4 class="nic-reg-group-title">${esc(label)}
                <span class="nic-reg-group-count">${fields.length}</span>
              </h4>
              <div class="nic-reg-grid">${rows}</div>
            </div>`;
  }).join("");

  box.innerHTML = groups;
  box.addEventListener("input", onRegistryEdit);
  box.addEventListener("change", onRegistryEdit);
  syncRegistryFields();
}

function regRow(f) {
  const hint = REG_SOURCE_LABEL[f.source] || f.source;
  const req = f.required ? ' <span class="required">*</span>' : "";
  let control;
  if (f.name === "ext") {
    const opts = (REG_EXTENSIONS || []).map((e) => `<option value="${esc(e)}">${esc(e)}</option>`);
    control = `<select data-reg="ext" name="ext">
        ${opts.length ? opts.join("") : '<option value="">Unavailable</option>'}
      </select>`;
  } else if (f.name === "reg_renewal") {
    control = `<input type="date" data-reg="reg_renewal" name="reg_renewal" id="renewal_date">`;
  } else {
    const type = f.name === "email" ? "email" : "text";
    control = `<input type="${type}" data-reg="${esc(f.name)}" name="${esc(f.name)}"
        placeholder="${esc(f.default || "e.g. " + (f.label || "").toLowerCase())}">`;
  }
  return `<div class="form-group nic-reg-field">
      <label class="form-label" for="reg-${esc(f.name)}">${esc(f.label)}${req}</label>
      <div class="input-wrapper">${control}</div>
      <span class="form-hint nic-reg-source" data-source="${esc(f.source)}">
        ${f.source === "derived" ? `copied from ${esc(f.derived_from)}` : esc(hint)}
      </span>
    </div>`;
}

// An edit to one field can change fields copied from it, so re-derive the rest.
function onRegistryEdit(e) {
  const el = e.target;
  const name = el && el.getAttribute && el.getAttribute("data-reg");
  if (name) REG_TOUCHED.add(name);
  syncRegistryFields();
}

document.addEventListener("DOMContentLoaded", () => {
  // Load the registry's field list only when the operator asks for a registry
  // push, so a normal hosting provision does not pay for it.
  const nicToggle = document.getElementById("register_nic");
  const nicFields = document.getElementById("nic-fields");
  if (nicToggle && nicFields) {
    let loaded = false;
    const sync = () => {
      nicFields.hidden = !nicToggle.checked;
      if (nicToggle.checked && !loaded) {
        loaded = true;
        loadRegistryForm();
      }
      if (nicToggle.checked) syncRegistryFields();
    };
    nicToggle.addEventListener("change", sync);
    sync();
  }

  // The domain drives the two computed fields and anything copied from them.
  const domain = document.getElementById("domain");
  if (domain) {
    domain.addEventListener("input", syncRegistryFields);
    domain.addEventListener("change", syncRegistryFields);
  }

  const refill = document.getElementById("nic-reg-refill");
  if (refill) {
    refill.addEventListener("click", () => {
      REG_TOUCHED.clear();
      syncRegistryFields();
    });
  }
});

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

  // The nic.bt.bt toggle is wired once, in the registry form section above,
  // which also loads the field list. Wiring it again here would hide the block
  // independently of the load and call a function that no longer exists.
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

async function checkDnsAfterProvisioning(domain, panel) {
  const box = document.getElementById("res-dns-status");
  if (!box || !domain) return;
  box.classList.remove("hidden");
  box.className = "res-dns-status";
  box.innerHTML = '<span class="form-hint">Checking whether the domain points at this server&hellip;</span>';
  try {
    renderDns(box, await runDnsCheck(domain, panel || "cpanel"), true);
  } catch (e) {
    // Never worth alarming anyone about: the account exists either way.
    box.className = "res-dns-status dns-warn";
    box.innerHTML = `<div class="dns-head"><span class="dns-badge">Not checked</span>
        <code>${esc(domain)}</code></div>
      <p>The DNS check could not be completed. The hosting account is unaffected.</p>`;
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
  const nicFields = registerNic ? registryFieldValues() : {};
  const nicExt = nicFields.ext || null;

  if (!domain) {
    showToast("Please enter a domain name.", "error");
    return;
  }

  // Check the registry's required fields here rather than after a round-trip:
  // the hosting account is created first, so a rejection from the registry would
  // otherwise leave a half-finished provisioning behind.
  if (registerNic) {
    const missing = missingRegistryFields();
    if (missing.length) {
      showToast(`nic.bt.bt still needs: ${missing.join(", ")}`, "error");
      const el = regInput(missing[0]);
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
    // Asked for separately, after the account is safely created. Keeping the
    // lookup out of the provisioning request means a slow or broken resolver
    // cannot delay it, and cannot make a successful provisioning look failed.
    checkDnsAfterProvisioning(result.data.domain, result.data.panel);
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
