// Drive the nic.bt.bt registry form the way an operator would, and check that the
// technical and billing contacts cost nothing.
//
// Those four fields are copies of the registrant. build_registry_payload has
// always resolved them server-side -- an empty value is dropped and the parent is
// used -- so the payload was always going to be right. What was wrong was the form
// beside it: four empty boxes listed in a red "nic.bt.bt will reject the
// submission" message that needed no action at all, for work that was never
// necessary. An operator taught to ignore that box ignores a real one too.
//
// This drives the real class under a stubbed DOM, typing through onEdit the way
// a browser does -- setting .value directly skips the touched-marking and sync()
// repaints over the top.
//
// Needs node; the Python test skips without it.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");

const wanted = ["RegistryForm", "splitDomainExt"];
let pulled = "";
for (const name of wanted) {
  const re = new RegExp("^(async )?(function|class) " + name + "\\b", "m");
  const m = re.exec(src);
  if (!m) { console.error("MISSING " + name); process.exit(2); }
  let i = src.indexOf("{", m.index + m[0].length - 1);
  let depth = 0, j = i;
  for (; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) break; }
  }
  pulled += src.slice(m.index, j + 1) + "\n";
}

const REG_SPEC = {
  groups: [["domain", "DOMAIN"], ["contact", "REGISTRANT / CONTACT"],
           ["technical", "TECHNICAL CONTACT"], ["billing", "BILLING CONTACT"]],
  fields: [
    { name: "domain", group: "domain", source: "derived", required: false },
    { name: "ext", group: "domain", source: "derived", required: false },
    { name: "registrar", group: "domain", source: "default", required: true, default: "DrukNet" },
    { name: "reg_renewal", group: "domain", source: "customer", required: true },
    { name: "customername", group: "contact", source: "customer", required: true },
    { name: "address", group: "contact", source: "customer", required: true, default: "Thimphu, Bhutan" },
    { name: "postalcode", group: "contact", source: "customer", required: true, default: "-" },
    { name: "telephone", group: "contact", source: "customer", required: true, default: "+975" },
    { name: "email", group: "contact", source: "customer", required: true },
    { name: "country", group: "contact", source: "default", required: true, default: "BT" },
    { name: "tech_name", group: "technical", source: "derived", derived_from: "customername", required: true },
    { name: "tech_email", group: "technical", source: "derived", derived_from: "email", required: true },
    { name: "billing_name", group: "billing", source: "derived", derived_from: "customername", required: true },
    { name: "billing_email", group: "billing", source: "derived", derived_from: "email", required: true },
  ],
};
let REG_EXTENSIONS = ["bt"];

// A DOM stub good enough for the form: elements with value, class lists, and a
// parent chain so closest("...") and querySelectorAll work.
function makeEl(tag, attrs) {
  attrs = attrs || {};
  const el = {
    tagName: (tag || "div").toUpperCase(), value: attrs.value || "",
    textContent: "", innerHTML: "", dataset: {}, hidden: false, options: [],
    _classes: new Set((attrs.className || "").split(" ").filter(Boolean)),
    children: [], parentNode: null, attrs: attrs,
  };
  el.className = attrs.className || "";
  el.classList = {
    add: (...c) => c.forEach((x) => el._classes.add(x)),
    remove: (...c) => c.forEach((x) => el._classes.delete(x)),
    contains: (c) => el._classes.has(c),
    toggle: (c, on) => (on === undefined
      ? (el._classes.has(c) ? el._classes.delete(c) : el._classes.add(c))
      : (on ? el._classes.add(c) : el._classes.delete(c))),
  };
  el.className = Array.from(el._classes).join(" ");
  el.closest = (sel) => (el._classes.has(sel.replace(/^\./, "")) ? el : (el.parentNode && el.parentNode.closest ? el.parentNode.closest(sel) : null));
  el.querySelector = (sel) => (sel.startsWith("[data-reg=")
    ? el._byName && el._byName[sel.match(/"([^"]+)"/)[1]] : null);
  el.querySelectorAll = () => [];
  el.insertBefore = () => {};
  el.appendChild = (c) => { el.children.push(c); c.parentNode = el; return c; };
  el.setAttribute = (k, v) => { el[k] = v; };
  el.getAttribute = (k) => el[k];
  el.removeEventListener = () => {};
  return el;
}

const root = makeEl("div");
const byName = {};
root._byName = byName;
const inputs = {};
for (const f of REG_SPEC.fields) {
  const el = makeEl(f.name === "ext" || f.name === "registrar" ? "select" : "input");
  el["data-reg"] = f.name;          // onEdit reads this to know which field was typed in
  inputs[f.name] = el;
  byName[f.name] = el;
  el.closest = (sel) => (sel === ".nic-reg-field" ? makeEl("div", { className: "nic-reg-field" }) : null);
}
const domainInput = makeEl("input", { value: "wankbt.bt" });
byName.domain = domainInput;
domainInput["data-reg"] = "domain";
inputs.domain = domainInput;

global.document = {
  getElementById: (id) => (id === "domain" ? domainInput : makeEl("div")),
  createElement: (t) => makeEl(t),
  addEventListener: () => {},
};
global.window = { localStorage: { getItem: () => null, setItem() {} } };
global.localStorage = global.window.localStorage;

// The placeholders the form reads for defaults.
const REG_PLACEHOLDER = {
  customername: "", email: "", address: "e.g. customer name",
  postalcode: "", telephone: "", country: "", reg_renewal: "",
};
// A direct eval keeps class/const bindings to itself, so hand them out.
const { RegistryForm: FormCtor } =
  eval(pulled + "; ({ RegistryForm })");

// The load() method renders into this.root and wires listeners; the listeners are
// no-ops in the stub, so call the pieces directly.
function esc(v) {
  return String(v === undefined || v === null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
// load() fetches the spec and renders; stub the fetch and let it run for real,
// rather than lifting its body out of the class where `this` is wrong.
function regField(name) {
  return REG_SPEC.fields.find((f) => f.name === name) || {};
}
global.fetch = async () => ({ ok: true, json: async () => REG_SPEC });
const form = new FormCtor({ root, domainInput, missingEl: makeEl("div") });
form.load = null;   // the real one needs fetch + a DOM; drive the parts we care about
form.loaded = true;
form.sync();
form.refreshMirrorGroups();

// Typing fires an input event, and onEdit marks the field touched so sync()
// stops repainting over it. Setting .value directly skips that, and the next
// sync() wipes what was typed -- which is a harness bug that looks exactly like
// the real one.
function type(name, value) {
  inputs[name].value = value;
  form.onEdit({ target: inputs[name] });
  form.sync();
}

const fails = [];
function check(label, cond) {
  console.log(`  ${cond ? "ok  " : "FAIL"} ${label}`);
  if (!cond) fails.push(label);
}

// 1. Nothing typed yet: the four copies must NOT be reported as blocking.
check("empty form lists no derived field as missing",
  !form.missing().some((n) => n.startsWith("tech_") || n.startsWith("billing_")));

// 2. Type the customer name and email, the way the operator would.
type("customername", "Karma Enterprises");
type("email", "karma@example.bt");
form.refreshMirrorGroups();

const m = form.missing();
check("customer name + email satisfy technical and billing",
  !m.includes("tech_name") && !m.includes("tech_email") &&
  !m.includes("billing_name") && !m.includes("billing_email"));
check("technical really did get filled in",
  inputs.tech_name.value === "Karma Enterprises" &&
  inputs.tech_email.value === "karma@example.bt");
check("billing really did get filled in",
  inputs.billing_name.value === "Karma Enterprises" &&
  inputs.billing_email.value === "karma@example.bt");

// 3. They mirror the registrant, so both groups should be collapsible.
check("technical reported as mirroring", form.groupMirrors("technical"));
check("billing reported as mirroring", form.groupMirrors("billing"));

// 4. Once made different, it must stop claiming to mirror.
type("tech_name", "N. Dorji");
check("a changed technical contact stops mirroring", !form.groupMirrors("technical"));
check("billing still mirrors", form.groupMirrors("billing"));

// 5. A genuinely required field with no source is still reported.
check("an unrelated empty required field is still listed",
  form.missing().includes("reg_renewal"));

if (fails.length) { console.error("\nFAILED: " + fails.join("; ")); process.exit(1); }
console.log("  ALL NIC FORM CHECKS OK");
