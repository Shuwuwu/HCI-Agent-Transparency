// Tests for src/participant_ui/previews.js. Run: node tests/test_previews_js.js
const assert = require("assert");
const fs = require("fs"), path = require("path");
const P = require("../src/participant_ui/previews.js");

assert.strictEqual(P.fileKind("a.docx"), "document");
assert.strictEqual(P.fileKind("feedback.xlsx"), "spreadsheet");
assert.strictEqual(P.fileKind("x.pptx"), "presentation");
assert.strictEqual(P.fileKind("notes.txt"), "text");

// Commas in the last column stay in that column (feedback text is not quoted in the stored data).
const t = P.parseTable("name,email,rating,feedback\nJohn Doe,j@x.com,3,Amazing product, but slow.\nA,b,1,\n\n");
assert.deepStrictEqual(t.header, ["name", "email", "rating", "feedback"]);
assert.deepStrictEqual(t.rows[0], ["John Doe", "j@x.com", "3", "Amazing product, but slow."]);
assert.deepStrictEqual(t.rows[1], ["A", "b", "1", ""]);
assert.strictEqual(t.rows.length, 2);
assert.deepStrictEqual(P.parseTable("a,b\nonly").rows, [["only", ""]]);  // padded, nothing dropped

assert.deepStrictEqual(P.paragraphs("a\nb\n\n\nc\n"), ["a\nb", "c"]);
assert.strictEqual(P.timeLabel("2024-05-19T10:05:00"), "10:05");
assert.strictEqual(P.dayKey("2024-05-19T10:05:00"), "2024-05-19");
assert.strictEqual(P.displayName("david.smith@bluesparrowtech.com"), "David Smith");
const groups = P.groupEventsByDay([
  { id_: "2", start_time: "2024-05-20T09:00:00" }, { id_: "1", start_time: "2024-05-19T10:00:00" },
  { id_: "3", start_time: "2024-05-19T08:00:00" }]);
assert.deepStrictEqual(groups.map((g) => [g.day, g.events.map((e) => e.id_)]), [["2024-05-19", ["3", "1"]], ["2024-05-20", ["2"]]]);

// Every spreadsheet in the committed workspace parses into rectangular rows with no empty header cell.
const dir = path.join(__dirname, "..", "src", "data", "workspace");
let sheets = 0;
for (const d of fs.readdirSync(dir)) {
  const ws = JSON.parse(fs.readFileSync(path.join(dir, d, "workspace.json")));
  for (const state of [ws.before, ws.after]) for (const f of state.files) {
    if (P.fileKind(f.filename) !== "spreadsheet") continue;
    const tab = P.parseTable(f.content); sheets++;
    assert.ok(tab.header.every((h) => h !== ""), f.filename);
    assert.ok(tab.rows.every((r) => r.length === tab.header.length), f.filename);
  }
}
assert.ok(sheets > 0);
console.log("previews.js tests passed (" + sheets + " spreadsheets parsed)");
