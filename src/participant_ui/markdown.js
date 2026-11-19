/*
 * Purpose:
 *   Minimal, dependency-free Markdown-to-HTML renderer for the agent's final
 *   response (and interim agent messages) in the participant chatbot UI.
 *
 * Inputs:
 *   A Markdown string. Supported: headings, paragraphs, bold, italic, inline
 *   code, fenced code blocks, nested bullet/numbered lists, blockquotes,
 *   horizontal rules and http(s)/mailto links. Anything else is shown as text.
 *
 * Outputs:
 *   An HTML string. All input is HTML-escaped before any markup is added, so
 *   the result is safe to assign to innerHTML.
 *
 * Dependencies:
 *   None. Loaded by index.html in the browser and by tests/test_markdown_js.js
 *   in Node (CommonJS export at the bottom).
 *
 * Usage:
 *   const html = renderMarkdown("**Hello**");
 */
(function (root) {
  "use strict";

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ESC[c]);

  /** Render inline markup (code, bold, italic, links) in one line of text. */
  function renderInline(text) {
    const codes = [];
    // Pull out code spans first so their content is not formatted further.
    let s = text.replace(/`([^`]+)`/g, (_, c) => {
      codes.push("<code>" + escapeHtml(c) + "</code>");
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    s = escapeHtml(s);
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
      // url is already escaped; only allow safe schemes.
      if (!/^(https?:\/\/|mailto:)/i.test(url)) return m;
      return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + "</a>";
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\w)/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[+i]);
  }

  const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

  /** Render a Markdown string to safe HTML. */
  function renderMarkdown(src) {
    const lines = String(src).replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      if (/^\s*$/.test(line)) { i++; continue; }

      // Fenced code block
      if (/^\s*```/.test(line)) {
        const buf = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
        i++; // closing fence
        out.push("<pre><code>" + escapeHtml(buf.join("\n")) + "</code></pre>");
        continue;
      }

      const h = /^(#{1,6})\s+(.*)$/.exec(line);
      if (h) {
        out.push("<h" + h[1].length + ">" + renderInline(h[2]) + "</h" + h[1].length + ">");
        i++; continue;
      }

      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }

      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push("<blockquote>" + renderMarkdown(buf.join("\n")) + "</blockquote>");
        continue;
      }

      if (LIST_RE.test(line)) {
        const block = [];
        while (i < lines.length) {
          if (LIST_RE.test(lines[i])) { block.push(lines[i++]); continue; }
          // Indented continuation lines (and blank lines inside a list) stay in the list.
          if (/^\s+\S/.test(lines[i])) { block.push(lines[i++]); continue; }
          if (/^\s*$/.test(lines[i]) && i + 1 < lines.length &&
              (LIST_RE.test(lines[i + 1]) || /^\s+\S/.test(lines[i + 1]))) { i++; continue; }
          break;
        }
        out.push(renderList(block));
        continue;
      }

      // Paragraph: consecutive lines until a blank line or another block start.
      const buf = [];
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*```/.test(lines[i]) &&
             !/^#{1,6}\s/.test(lines[i]) && !LIST_RE.test(lines[i]) && !/^\s*>/.test(lines[i])) {
        buf.push(lines[i++]);
      }
      out.push("<p>" + buf.map((l) => renderInline(l.trim())).join("<br>") + "</p>");
    }
    return out.join("\n");
  }

  /** Render a run of list lines as nested <ul>/<ol> using indentation depth. */
  function renderList(block) {
    const root = { children: [], indent: -1 };
    const stack = [root];
    let last = null;
    for (const line of block) {
      const m = LIST_RE.exec(line);
      if (!m) { // continuation text belongs to the previous item
        if (last) last.text += " " + line.trim();
        continue;
      }
      const indent = m[1].replace(/\t/g, "    ").length;
      const item = { indent, ordered: /\d/.test(m[2]), text: m[3], children: [] };
      while (stack.length > 1 && stack[stack.length - 1].indent >= indent) stack.pop();
      stack[stack.length - 1].children.push(item);
      stack.push(item);
      last = item;
    }
    return listHtml(root.children);
  }

  function listHtml(items) {
    if (!items.length) return "";
    const tag = items[0].ordered ? "ol" : "ul";
    return "<" + tag + ">" + items.map((it) =>
      "<li>" + renderInline(it.text) + listHtml(it.children) + "</li>").join("") + "</" + tag + ">";
  }

  root.renderMarkdown = renderMarkdown;
  if (typeof module !== "undefined" && module.exports) module.exports = { renderMarkdown };
})(typeof window !== "undefined" ? window : globalThis);
