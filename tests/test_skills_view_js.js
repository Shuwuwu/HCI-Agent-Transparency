// Tests for the skill-document viewing times of src/participant_ui/skills.js: skill_doc_open when a document is
// shown, skill_doc_view when it is left, and pause()/resume() (the always-open workspace panel is paused when the
// page is hidden or closed and when the conversation is left), which end and restart the timing without a new
// skill_doc_open. Run: node tests/test_skills_view_js.js
//
// skills.js and interaction.js are loaded unchanged into a minimal fake DOM with a controllable clock
// (performance.now) and a fetch that serves a fixed skill list and document and records what is posted to /api/log.
const assert = require("assert");

let clock = 0;
function el(tag) {
  return {
    tag, className: "", textContent: "", innerHTML: "", children: [], onclick: null,
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...cs) { this.children = cs; },
  };
}
globalThis.window = globalThis;
globalThis.document = { hidden: false, createElement: el, addEventListener() {} };
Object.defineProperty(globalThis, "performance", { value: { now: () => clock }, configurable: true });
globalThis.renderMarkdown = (md) => md;
const posted = [];
const json = (body) => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
globalThis.fetch = (url, opts) => {
  if (url === "/api/log") { posted.push(JSON.parse(opts.body)); return Promise.resolve(); }
  if (url.startsWith("/api/skills?")) return json([{ id: "email-lookup", name: "Email lookup", description: "d" }]);
  if (url.startsWith("/api/skill?")) return json({ id: "email-lookup", name: "Email lookup", description: "d", markdown: "# Doc" });
  return Promise.reject(new Error("unexpected " + url));
};
require("../src/participant_ui/interaction.js");
require("../src/participant_ui/skills.js");
Interaction.setLink("token-for-tests");
Interaction.setTrial("P001-t01");

const tick = () => new Promise((resolve) => setImmediate(resolve));
const find = (node, cls) => node.className.split(" ").includes(cls) ? node
  : node.children.reduce((hit, c) => hit || (c.children ? find(c, cls) : null), null);
const log = () => posted.map((e) => [e.type, e.duration_ms]);

(async () => {
  let body = el("div");
  const view = SkillsView.create("t=x", () => { body = el("div"); view.render(body); });
  view.render(body);
  await tick();
  find(body, "item-btn").onclick(); // open the document
  await tick();
  assert.deepStrictEqual(log(), [["skill_doc_open", undefined]]);

  // A pause ends the view and keeps the document shown; time while paused does not count; resume restarts it.
  clock += 400;
  view.pause();
  clock += 10000;
  view.resume();
  clock += 250;
  view.leave(); // another tab chosen
  assert.deepStrictEqual(log(), [["skill_doc_open", undefined], ["skill_doc_view", 400], ["skill_doc_view", 250]]);

  // Pausing with no document open, or twice, logs nothing.
  posted.length = 0;
  view.pause();
  view.pause();
  view.resume();
  assert.deepStrictEqual(log(), []);
  console.log("skills view tests passed");
})().catch((e) => { console.error(e); process.exit(1); });
