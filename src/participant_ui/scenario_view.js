/*
 * Purpose:
 *   Reusable participant view of ONE scenario in ONE transparency condition: the chat (user request, the activity
 *   cards and in-between agent messages in the high condition only, then the final response) next to the
 *   read-only workspace panel (Mail | Calendar | Drive | Skills). Both conditions use exactly this component; the
 *   only difference is the "trace" the server includes in the high condition's /api/stimulus response.
 *   Mounted by app.js on the standalone /chat page; written so that a later experiment flow can mount it for each
 *   trial and unmount it afterwards (unmount ends running view timers, so no viewing time is lost).
 *
 * Inputs:
 *   ScenarioView.mount(container, options):
 *     container  element the view is drawn into (it gets the "layout" class; its previous content is replaced).
 *     options    {stimulus: folder id, condition: "low" | "high",
 *                 toggle: the button that opens/closes the workspace panel (kept hidden until the panel is ready),
 *                 panel: optional {tab, item, syncUrl} for researcher deep links (see workspace.js)}.
 *   JSON from /api/stimulus?s=&c= and /api/workspace?s= (no condition parameter: same in both conditions).
 *
 * Outputs:
 *   DOM inside `container`. Text is inserted with textContent; only the final response and agent messages go
 *   through renderMarkdown (which escapes HTML). Interaction events for the panel (workspace_panel_open/close).
 *   mount() returns {ready, unmount}: `ready` is a Promise that resolves to true once the conversation is drawn
 *   (false if it could not be loaded); unmount() closes the panel, removes listeners and clears the container.
 *
 * Dependencies:
 *   markdown.js (renderMarkdown), workspace.js (WorkspaceSidebar; skills.js through it), interaction.js
 *   (Interaction), participant_server.py (API).
 */
(function (root) {
  "use strict";
  const I = root.Interaction;

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

  /** One action of the execution trace: neutral action sentence, tool name, arguments and outcome summary. */
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

  /** Draw the conversation; `view.trace` exists only in the high condition, so the low view has no cards. */
  function renderConversation(chat, view) {
    chat.replaceChildren(userMessage(view.user_request));
    const reply = el("div", "row assistant");
    const col = el("div", "assistant-col");
    (view.trace || []).forEach((s) => {
      col.appendChild(s.type === "action" ? activityCard(s) : markdownBubble(s.text, "interim"));
    });
    col.appendChild(markdownBubble(view.final_response, "final"));
    reply.appendChild(col);
    chat.appendChild(reply);
  }

  const getJson = (url) => fetch(url).then((r) => (r.ok ? r.json() : Promise.reject(r.status)));

  function mount(container, options) {
    const stimulus = options.stimulus || "";
    const condition = options.condition || "";
    const toggle = options.toggle;
    let alive = true;      // false after unmount: late responses are then ignored
    let controller = null; // workspace panel, once its data has arrived
    let isOpen = false;

    container.classList.add("layout");
    const chatCol = el("div", "chat-col");
    const chat = el("main", "chat");
    chat.setAttribute("aria-live", "polite");
    const composer = el("footer", "composer");
    const input = el("input");
    input.type = "text";
    input.disabled = true;
    input.placeholder = "This is a recorded conversation";
    input.setAttribute("aria-label", "Message input (disabled)");
    composer.appendChild(input);
    chatCol.append(chat, composer);
    const panel = el("aside", "workspace");
    panel.id = "workspace";
    panel.setAttribute("aria-label", "Workspace (read-only)");
    container.replaceChildren(chatCol, panel);
    toggle.hidden = true;
    toggle.setAttribute("aria-controls", "workspace");
    toggle.setAttribute("aria-expanded", "false");

    function setOpen(open) {
      if (!controller || isOpen === open) return;
      isOpen = open;
      container.classList.toggle("workspace-open", open);
      toggle.setAttribute("aria-expanded", String(open));
      if (open) {
        I.log("workspace_panel_open");
        I.startTimer("panel");
        controller.opened();
      } else {
        controller.closed();
        I.log("workspace_panel_close", { duration_ms: I.stopTimer("panel") });
      }
    }
    const onToggle = () => setOpen(!isOpen);
    const onPageHide = () => setOpen(false); // a closing page ends the running views, so durations are not lost
    toggle.addEventListener("click", onToggle);
    window.addEventListener("pagehide", onPageHide);

    function startPanel(data) {
      if (!alive) return;
      controller = root.WorkspaceSidebar.init(panel, data, stimulus, () => setOpen(false), options.panel || {});
      toggle.hidden = false;
      if (options.panel && options.panel.tab) setOpen(true); // researcher deep link only; participants open it themselves
    }
    // The snapshot may be missing for a scenario; the panel (with its Skills tab) is offered either way.
    getJson("/api/workspace?s=" + encodeURIComponent(stimulus)).then(startPanel, () => startPanel(null));

    const ready = getJson("/api/stimulus?s=" + encodeURIComponent(stimulus) + "&c=" + encodeURIComponent(condition))
      .then((view) => { if (alive) renderConversation(chat, view); return alive; })
      .catch(() => {
        if (alive) chat.replaceChildren(el("p", "error-msg", "This conversation could not be loaded."));
        return false;
      });

    function unmount() {
      if (!alive) return;
      setOpen(false);
      alive = false;
      toggle.removeEventListener("click", onToggle);
      window.removeEventListener("pagehide", onPageHide);
      toggle.hidden = true;
      container.classList.remove("workspace-open");
      container.replaceChildren();
    }

    return { ready, unmount };
  }

  root.ScenarioView = { mount };
})(window);
