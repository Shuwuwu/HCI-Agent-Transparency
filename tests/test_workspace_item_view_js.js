// Tests for the logging of src/participant_ui/workspace.js: nothing for the default view of the always-open panel,
// tab choices, and the item viewing times (workspace_item_view), measured with the
// visibility-aware timers of src/participant_ui/interaction.js. Run: node tests/test_workspace_item_view_js.js
//
// Both files are loaded unchanged into a minimal fake DOM: just enough elements for workspace.js to draw its
// panel and for the test to click its buttons, a controllable clock (performance.now) and page visibility, and a
// fetch that records what interaction.js posts to /api/log instead of sending it.
const assert = require("assert");

// ---- fake environment -------------------------------------------------------------------------------------
let clock = 0;
const visibilityListeners = [];
function el(tag) {
  return {
    tag, className: "", textContent: "", children: [], onclick: null, scrollTop: 0, style: {}, dataset: {},
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    replaceChildren(...cs) { this.children = cs; },
    setAttribute() {},
  };
}
globalThis.window = globalThis;
globalThis.document = {
  hidden: false,
  createElement: el,
  createTextNode: (text) => ({ textContent: text, children: [] }),
  addEventListener(type, fn) { if (type === "visibilitychange") visibilityListeners.push(fn); },
};
Object.defineProperty(globalThis, "performance", { value: { now: () => clock }, configurable: true });
const posted = [];
globalThis.fetch = (url, opts) => {
  if (url === "/api/log") posted.push(JSON.parse(opts.body));
  return Promise.resolve();
};
// The Skills tab's own documents are not under test; a stand-in keeps workspace.js from fetching skills.
globalThis.SkillsView = { create: () => ({ render() {}, leave() {}, pause() {}, resume() {} }) };
require("../src/participant_ui/previews.js");
require("../src/participant_ui/interaction.js");
require("../src/participant_ui/workspace.js");
Interaction.setLink("token-for-tests");
Interaction.setTrial("P001-t01");

function setHidden(hidden) {
  document.hidden = hidden;
  visibilityListeners.forEach((fn) => fn());
}
const text = (node) => (node.textContent || "") + node.children.map(text).join("");
function find(node, cls, label) {
  if (node.className.split(" ").includes(cls) && (label === undefined || text(node).includes(label))) return node;
  for (const c of node.children) {
    const hit = c.children ? find(c, cls, label) : null;
    if (hit) return hit;
  }
  return null;
}
function click(container, cls, label) {
  const button = find(container, cls, label);
  assert.ok(button && button.onclick, "no " + cls + " button " + (label || ""));
  button.onclick();
}
const types = () => posted.map((e) => e.type);
const views = () => posted.filter((e) => e.type === "workspace_item_view")
  .map((e) => [e.tab, e.item_id, e.duration_ms]);
const reset = () => { posted.length = 0; };

// ---- a small workspace: an email, a sent email, a calendar event and a file share the id "1" ----------------
const email = (id, status, subject) => ({ id_: id, status, subject, sender: "a@x.com", recipients: ["b@x.com"],
  cc: [], bcc: [], body: "Body of " + subject, timestamp: "2024-05-15T10:00:00", attachments: [] });
const data = {
  account: { email: "emma@x.com", current_day: "2024-05-15" },
  state: {
    emails: [email("1", "received", "Inbox mail"), email("2", "sent", "Sent mail")],
    events: [{ id_: "1", title: "Team sync", start_time: "2024-05-15T09:00:00", end_time: "2024-05-15T10:00:00",
               all_day: false, status: "confirmed", location: "Room 1", participants: ["a@x.com"], description: "d" }],
    files: [{ id_: "1", filename: "notes.txt", content: "hello", owner: "emma@x.com",
              last_modified: "2024-05-14T10:00:00", shared_with: {}, size: 5 }],
  },
};
const container = el("aside");
const panel = WorkspaceSidebar.init(container, data, "t=x", {});

// The panel is on screen from the start with the Mail inbox, but nothing is logged or timed for that default view.
panel.resume();
clock += 5000;
panel.pause();
panel.resume();
assert.deepStrictEqual(types(), []);

// Clicking the Mail tab while it is still the default chooses it: an open event, then its time runs.
click(container, "tab", "Mail");
assert.deepStrictEqual(posted.map((e) => [e.type, e.tab]), [["workspace_tab_open", "mail"]]);
clock += 300;
click(container, "tab", "Mail"); // the tab already chosen: nothing new
assert.deepStrictEqual(types(), ["workspace_tab_open"]);
reset();

// Open an email and go back to the list: one view with its visible time; workspace_item_open is unchanged.
click(container, "item-btn", "Inbox mail");
clock += 1000;
click(container, "back");
assert.deepStrictEqual(types(), ["workspace_item_open", "workspace_item_view"]);
assert.deepStrictEqual(posted[0], Object.assign({}, posted[0], { tab: "mail", item_id: "1", trial_id: "P001-t01",
                                                                  token: "token-for-tests" }));
assert.deepStrictEqual(views(), [["mail", "1", 1000]]);
reset();

// Time while the browser tab is hidden does not count; switching workspace tab ends the item view first.
click(container, "item-btn", "Inbox mail");
clock += 500;
setHidden(true);
clock += 60000;
setHidden(false);
clock += 200;
click(container, "tab", "Calendar");
assert.deepStrictEqual(types(), ["workspace_item_open", "workspace_item_view", "workspace_tab_view",
                                 "workspace_tab_open"]);
assert.deepStrictEqual(views(), [["mail", "1", 700]]);
assert.strictEqual(posted[2].duration_ms, 300 + 1000 + 700); // Mail's time from when it was chosen
reset();

// A pause (page hidden or closed, conversation left) ends the views; resuming on the same item starts a new one
// without a new item_open or tab_open.
click(container, "item-btn", "Team sync");
clock += 300;
panel.pause();
clock += 5000; // paused: not viewing
panel.resume();
clock += 400;
panel.pause();
assert.deepStrictEqual(views(), [["calendar", "1", 300], ["calendar", "1", 400]]);
assert.strictEqual(types().filter((t) => t === "workspace_item_open").length, 1);
assert.strictEqual(types().filter((t) => t === "workspace_tab_open").length, 0);
assert.ok(types().indexOf("workspace_item_view") < types().indexOf("workspace_tab_view")); // item ends first
reset();

// Drive (resumed on the calendar event, still selected), and the page being hidden for the rest of a view.
panel.resume();
clock += 50;
click(container, "tab", "Drive");
click(container, "item-btn", "notes.txt");
clock += 250;
setHidden(true);
clock += 9000;
panel.pause(); // scenario_view.js pauses the panel on pagehide
assert.deepStrictEqual(views(), [["calendar", "1", 50], ["drive", "1", 250]]);
setHidden(false);
reset();

// A sent email (the folder chips show on the list only); switching to Skills ends the view, and the Skills tab
// never produces an item view.
panel.resume(); // resumes on the Drive file, still selected
clock += 20;
click(container, "tab", "Mail");
click(container, "chip", "Sent");
click(container, "item-btn", "Sent mail");
clock += 150;
click(container, "tab", "Skills");
clock += 1000;
panel.pause();
assert.deepStrictEqual(views(), [["drive", "1", 20], ["mail", "2", 150]]);
assert.ok(types().includes("skills_tab_open"));
assert.deepStrictEqual(posted.filter((e) => e.type === "skills_tab_view").map((e) => e.duration_ms), [1000]);
reset();

// A deep-linked item that is not in the snapshot is not timed (the panel shows the list instead).
const missing = WorkspaceSidebar.init(el("aside"), data, "t=x", { tab: "drive", item: "99" });
missing.resume();
clock += 100;
missing.pause();
assert.deepStrictEqual(views(), []);

// The panel has no close button.
assert.strictEqual(find(container, "ws-close"), null);

console.log("workspace item view tests passed");
