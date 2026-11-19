/*
 * Purpose:
 *   Render one stimulus as a chatbot conversation: user message, (high
 *   transparency only) sequential activity cards, then the final response.
 *
 * Inputs:
 *   Query params ?s=<stimulus_id>&c=low|high; JSON from /api/stimulus.
 *
 * Outputs:
 *   DOM inside #chat. Text is inserted with textContent; only the final
 *   response / agent messages go through renderMarkdown (which escapes HTML).
 *
 * Dependencies:
 *   markdown.js (renderMarkdown), participant_server.py (API).
 */
(function () {
  "use strict";
  const chat = document.getElementById("chat");

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function userMessage(text) {
    const row = el("div", "row user");
    row.appendChild(el("div", "bubble user-bubble", text));
    return row;
  }

  function markdownBubble(md, cls) {
    const b = el("div", "bubble assistant-bubble " + (cls || ""));
    b.innerHTML = renderMarkdown(md);
    return b;
  }

  function activityCard(step) {
    const card = el("div", "card " + step.outcome.status);
    const head = el("div", "card-head");
    head.appendChild(el("span", "step-num", String(step.step)));
    head.appendChild(el("span", "action", step.action));
    head.appendChild(el("code", "tool", step.tool));
    card.appendChild(head);
    if (step.arguments.length) {
      const dl = el("dl", "args");
      step.arguments.forEach((a) => {
        dl.appendChild(el("dt", null, a.name));
        dl.appendChild(el("dd", null, a.value + (a.truncated ? " …" : "")));
      });
      card.appendChild(dl);
    }
    const out = el("div", "outcome");
    out.appendChild(el("span", "status", step.outcome.status === "error" ? "Error" : "Result"));
    out.appendChild(el("span", "summary", step.outcome.summary));
    card.appendChild(out);
    return card;
  }

  function render(view) {
    chat.replaceChildren(userMessage(view.user_request));
    const reply = el("div", "row assistant");
    const col = el("div", "assistant-col");
    // "trace" is only present in the high-transparency response.
    (view.trace || []).forEach((s) => {
      col.appendChild(s.type === "action" ? activityCard(s) : markdownBubble(s.text, "interim"));
    });
    col.appendChild(markdownBubble(view.final_response, "final"));
    reply.appendChild(col);
    chat.appendChild(reply);
  }

  const p = new URLSearchParams(location.search);
  fetch("/api/stimulus?s=" + encodeURIComponent(p.get("s") || "") + "&c=" + encodeURIComponent(p.get("c") || ""))
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then(render)
    .catch(() => chat.replaceChildren(el("p", "error-msg", "This conversation could not be loaded.")));
})();
