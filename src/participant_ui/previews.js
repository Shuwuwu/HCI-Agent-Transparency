/*
 * Purpose:
 *   Pure helper functions (no DOM) that turn workspace records into what the sidebar shows: the kind of a
 *   drive file from its name, a table from spreadsheet text, date/time labels, and calendar events grouped
 *   by day. Kept separate from workspace.js so they can be tested in Node.
 *
 * Inputs:
 *   Records as served by /api/workspace (emails, events, files with the fields of AgentDojo's workspace).
 *
 * Outputs:
 *   Plain objects/strings. Nothing is invented: text is shown exactly as stored.
 *
 * Dependencies:
 *   None. Loaded by index.html in the browser and by tests/test_previews_js.js in Node.
 */
(function (root) {
  "use strict";

  /** "document" | "spreadsheet" | "presentation" | "text" from a file name's extension. */
  function fileKind(filename) {
    const ext = (filename.split(".").pop() || "").toLowerCase();
    if (ext === "docx" || ext === "doc") return "document";
    if (ext === "xlsx" || ext === "xls" || ext === "csv") return "spreadsheet";
    if (ext === "pptx" || ext === "ppt") return "presentation";
    return "text";
  }

  /**
   * Split spreadsheet text (one row per line, comma separated, first line = header) into {header, rows}.
   * The stored text is not quoted CSV: a value may contain commas (for example free-text feedback), so each
   * row is split at most (columns - 1) times and the remainder stays in the last column. Blank lines are
   * skipped. Rows with fewer values are padded with empty cells; nothing is dropped or invented.
   */
  function parseTable(content) {
    const lines = String(content).split(/\r?\n/).filter((l) => l.trim() !== "");
    if (!lines.length) return { header: [], rows: [] };
    const header = lines[0].split(",").map((h) => h.trim());
    const n = header.length;
    const rows = lines.slice(1).map((line) => {
      const cells = [];
      let rest = line;
      for (let i = 0; i < n - 1; i++) {
        const k = rest.indexOf(",");
        if (k < 0) break;
        cells.push(rest.slice(0, k));
        rest = rest.slice(k + 1);
      }
      cells.push(rest);
      while (cells.length < n) cells.push("");
      return cells;
    });
    return { header, rows };
  }

  /** Split document text into paragraphs (blank-line separated); keeps line breaks inside a paragraph. */
  function paragraphs(content) {
    return String(content).split(/\n{2,}/).map((p) => p.replace(/\n+$/, "")).filter((p) => p.trim() !== "");
  }

  const pad = (n) => String(n).padStart(2, "0");
  /** Parse an ISO string without time zone ("2024-05-19T10:00:00") as a local wall-clock date. */
  function parseLocal(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(iso);
    return new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0));
  }
  const dayKey = (iso) => iso.slice(0, 10);
  const timeLabel = (iso) => { const d = parseLocal(iso); return pad(d.getHours()) + ":" + pad(d.getMinutes()); };
  const dateLabel = (iso) => parseLocal(iso).toLocaleDateString("en-US",
    { weekday: "short", year: "numeric", month: "short", day: "numeric" });
  const dateTimeLabel = (iso) => dateLabel(iso) + ", " + timeLabel(iso);

  /** Events sorted by start time and grouped by day: [{day: "2024-05-19", events: [...]}, ...]. */
  function groupEventsByDay(events) {
    const sorted = events.slice().sort((a, b) => a.start_time.localeCompare(b.start_time) || (+a.id_ - +b.id_));
    const groups = [];
    for (const e of sorted) {
      const day = dayKey(e.start_time);
      if (!groups.length || groups[groups.length - 1].day !== day) groups.push({ day, events: [] });
      groups[groups.length - 1].events.push(e);
    }
    return groups;
  }

  /** "Name <addr>"-free display of an address: the part before @, dots to spaces, title-cased. */
  function displayName(address) {
    return address.split("@")[0].replace(/[._]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  const api = { fileKind, parseTable, paragraphs, parseLocal, dayKey, timeLabel, dateLabel, dateTimeLabel,
                groupEventsByDay, displayName };
  root.WorkspacePreviews = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
