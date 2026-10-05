// Exercise the suspension card's render paths under a stubbed DOM.
//
// Written after a change that compiled and then threw at runtime: a missing
// concatenation operator made JavaScript read a string as a function, so
// `node --check` passed and the page threw
//
//     (intermediate value)(intermediate value)(intermediate value) is not a function
//
// Only executing the function finds that. Needs node; skipped without it.
//
// Exercise loadSuspensionReport's rendering with a stubbed DOM, because the bug
// that prompted this was a runtime error: a missing concatenation operator made
// JS parse a string as a call, so it compiled and then threw.
//
// A syntax check alone would have passed the broken version.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");

// Pull out the functions under test plus their helpers.
const wanted = ["loadSuspensionReport", "renderSuspensionCandidates",
                "renderUnmatched", "renderActivatable", "esc", "describeApiError",
                "apiHeaders", "apiToken", "dnsBadge", "showToast", "updateAuthBanner"];
let pulled = "";
for (const name of wanted) {
  const re = new RegExp("^(async )?function " + name + "\\(", "m");
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

const els = {};
function el(id) {
  return els[id] || (els[id] = {
    id, innerHTML: "", textContent: "", className: "", hidden: true,
    style: {}, dataset: {}, querySelector: () => null, appendChild() {},
    classList: { add() {}, remove() {}, contains: () => false },
  });
}
global.document = {
  getElementById: el,
  createElement: () => el("created"),
  querySelector: () => el("q"),
  addEventListener() {},
};
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.window = { localStorage: global.localStorage,
                  confirm: () => true, scrollTo() {} };
global.fetch = async () => ({ ok: true, json: async () => ({}) });
global.showToast = () => {};
global.setTimeout = () => {};

eval(pulled);

// Two payloads: the failed-attempt path (the one that was broken) and a clean
// successful run.
const failed = {
  available: true, generated_at: "2026-10-01T14:36:48+06:00", age_hours: 0.1,
  stale: false, total_accounts: 356, candidates: [], candidates_all: [],
  already_suspended_billing: 122, suspended_other_reason: 15, no_match: 218,
  bscs_complete: true, bscs_note: "",
  last_attempt_at: "2026-10-01T14:37:38+06:00", last_attempt_age_hours: 0.1,
  last_attempt_ok: false, last_attempt_detail: "crashed: RuntimeError: boom",
  last_attempt_traceback: "Traceback...\nRuntimeError: boom",
  no_attempt_since: false, never_attempted: false,
  unmatched_contracts: [], activatable: [],
};
const notRunning = Object.assign({}, failed, {
  last_attempt_ok: true, last_attempt_detail: "completed",
  last_attempt_traceback: "", last_attempt_age_hours: 66.5, no_attempt_since: true,
});
const neverRan = Object.assign({}, failed, {
  last_attempt_ok: false, last_attempt_detail: "", last_attempt_traceback: "",
  last_attempt_age_hours: null, never_attempted: true, no_attempt_since: false,
});
const clean = Object.assign({}, failed, {
  last_attempt_ok: true, last_attempt_detail: "completed",
  last_attempt_traceback: "", no_attempt_since: false,
});

(async () => {
  for (const [label, payload] of [["failed", failed], ["not running", notRunning],
                                 ["never run", neverRan], ["clean", clean]]) {
    for (const k of Object.keys(els)) delete els[k];
    els["suspension-summary"] = el("suspension-summary");
    els["suspension-summary"].innerHTML = "";
    global.fetch = async () => ({ ok: true, json: async () => payload });
    try {
      await loadSuspensionReport();
      const html = els["suspension-summary"].innerHTML;
      // The function catches its own errors and puts them in textContent, so an
      // empty innerHTML is where a thrown error hides.
      const msg = els["suspension-summary"].textContent;
      if (!html || html.indexOf("is not a function") >= 0) {
        console.error("FAIL [" + label + "]: html=" + JSON.stringify(html) +
                      " textContent=" + JSON.stringify(msg));
        process.exit(1);
      }
      console.log("  OK [" + label + "] " +
        (html.indexOf("<details") >= 0 ? "(traceback shown)" :
         html.indexOf("HAS NEVER RUN") >= 0 ? "(never run)" :
         html.indexOf("NOT RUNNING") >= 0 ? "(not running)" : "(clean)"));
    } catch (e) {
      console.error("FAIL [" + label + "]: threw " + e.message);
      process.exit(1);
    }
  }
  console.log("  ALL RENDER PATHS OK");
})();
