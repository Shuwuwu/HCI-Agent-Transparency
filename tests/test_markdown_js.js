// Tests for src/participant_ui/markdown.js. Run: node tests/test_markdown_js.js
const assert = require("assert");
const { renderMarkdown: md } = require("../src/participant_ui/markdown.js");

assert.strictEqual(md("**hi**"), "<p><strong>hi</strong></p>");
assert.ok(md("a *b* c").includes("<em>b</em>"));
assert.ok(md("# T").startsWith("<h1>T</h1>"));
assert.ok(md("`x<y`").includes("<code>x&lt;y</code>"));
// HTML is escaped, links restricted to safe schemes
assert.ok(!md("<script>alert(1)</script>").includes("<script>"));
assert.ok(!md("[a](javascript:alert(1))").includes("<a "));
assert.ok(md("[a](https://x.org)").includes('<a href="https://x.org"'));
// Nested list under numbered item (shape used in the user_task_1 response)
const html = md("1. **Team Sync**\n   - **Description:** Weekly\n   - **Time:** 10\n\n2. **Lunch**\n   - **Time:** 12");
assert.strictEqual((html.match(/<ol>/g) || []).length, 1);
assert.strictEqual((html.match(/<ul>/g) || []).length, 2);
assert.strictEqual((html.match(/<li>/g) || []).length, 5);
assert.ok(html.includes("<strong>Description:</strong> Weekly"));
// Every saved final response renders without raw markdown bold markers
const fs = require("fs"), path = require("path");
const dir = path.join(__dirname, "..", "src", "data", "stimuli");
for (const d of fs.readdirSync(dir)) {
  const f = path.join(dir, d, "low.json");
  if (!fs.existsSync(f)) continue;
  const out = md(JSON.parse(fs.readFileSync(f)).final_response);
  assert.ok(!/\*\*/.test(out.replace(/<code>.*?<\/code>/gs, "")), d + " has unrendered **");
}
console.log("markdown.js tests passed");
