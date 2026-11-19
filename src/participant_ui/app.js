/*
 * Purpose:
 *   Render one stimulus as a chatbot conversation: user message, (high
 *   transparency only) sequential activity cards, then the final response.
 *
 * Inputs:
 *   Query params ?s=<stimulus folder>&c=low|high (and optional pid=<participant id>); JSON from /api/stimulus.
 *   The workspace panel loads /api/workspace and (in its Skills tab) /api/skills(/skill) for the folder with no
 *   condition parameter: they are the same in both conditions. Researcher-preview parameters: w=<tab>[&i=<id>]
 *   opens the panel on that tab, preview=1 turns interaction logging off.
 *
 * Outputs:
 *   DOM inside #chat. Text is inserted with textContent; only the final
 *   response / agent messages go through renderMarkdown (which escapes HTML).
 *
 * Dependencies:
 *   markdown.js (renderMarkdown), workspace.js (WorkspaceSidebar), skills.js (via workspace.js), interaction.js
 *   (Interaction), participant_server.py (API).
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
  const folder = p.get("s") || "";
  const condition = p.get("c") || "";

  // ---- workspace panel (Mail | Calendar | Drive | Skills); not opened automatically ------------------------------
  const button = document.getElementById("ws-toggle");
  let controller = null;
  let isOpen = false;

  function setOpen(open) {
    if (!controller || isOpen === open) return;
    isOpen = open;
    document.body.classList.toggle("workspace-open", open);
    button.setAttribute("aria-expanded", String(open));
    if (open) {
      Interaction.log("workspace_panel_open");
      Interaction.startTimer("panel");
      controller.opened();
    } else {
      controller.closed();
      Interaction.log("workspace_panel_close", { duration_ms: Interaction.stopTimer("panel") });
    }
  }
  button.onclick = () => setOpen(!isOpen);
  // A closing page ends the running views, so durations are not lost.
  window.addEventListener("pagehide", () => setOpen(false));

  function start(data) {
    controller = WorkspaceSidebar.init(document.getElementById("workspace"), data, folder, () => setOpen(false));
    button.hidden = false;
    if (p.get("w")) setOpen(true); // researcher-preview deep link only; participants open the panel themselves
  }
  // The snapshot may be missing for a scenario; the panel (with its Skills tab) is offered either way.
  fetch("/api/workspace?s=" + encodeURIComponent(folder))
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then(start, () => start(null));

  Interaction.log("session_start", Object.assign({ stimulus: folder, condition },
    Interaction.participantId ? { pid: Interaction.participantId } : {}));
  fetch("/api/stimulus?s=" + encodeURIComponent(folder) + "&c=" + encodeURIComponent(condition))
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then(render)
    .catch(() => chat.replaceChildren(el("p", "error-msg", "This conversation could not be loaded.")));
})();
