// ========================================================
// FRONTEND INTERACTIONS - AUTOMATION WEBSERVICE BT
// ========================================================

let latestAccountData = null;

// Initialize on load
document.addEventListener("DOMContentLoaded", () => {
  generateNewPassword();
  checkServerHealth();

  // Show the nic.bt.bt contact fields only when the registry push is requested.
  const nicToggle = document.getElementById("register_nic");
  const nicFields = document.getElementById("nic-fields");
  if (nicToggle && nicFields) {
    const sync = () => {
      nicFields.hidden = !nicToggle.checked;
    };
    nicToggle.addEventListener("change", sync);
    sync();
  }
});

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
    const res = await fetch(`/api/v1/packages?panel=${panel}`);
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
    const res = await fetch("/api/v1/servers/status");
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
  const customerName = (document.getElementById("customer_name") || {}).value || "";
  const phone = (document.getElementById("phone") || {}).value || "";
  const address = (document.getElementById("address") || {}).value || "";

  if (!domain) {
    showToast("Please enter a domain name.", "error");
    return;
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        panel: panel,
        domain: domain,
        username: username || null,
        password: password || null,
        email: email || null,
        package: packagePlan || null,
        send_email: sendEmail,
        register_nic: registerNic,
        customer_name: customerName || null,
        phone: phone || null,
        address: address || null,
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
