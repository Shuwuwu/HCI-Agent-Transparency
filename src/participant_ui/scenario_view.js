/*
 * Purpose:
 *   Reusable view of ONE conversation: the chat (the user request, then any activity cards and in-between agent
 *   messages, then the final response) next to the read-only workspace panel (Mail | Calendar | Drive | Skills).
 *   It draws exactly what /api/stimulus returns: activity cards and in-between messages appear when the response
 *   has a "trace" list, and the chat goes straight from the request to the final response when it has none.
 *   Each activity card starts collapsed, showing its header (step number, action sentence, tool, chevron); its
 *   header is a button that expands or collapses that card's details (the arguments and the result) on its own.
 *   The messages between cards are always shown, in the order they were written.
 *   Mounted by flow.js for each conversation of the flow and by app.js on the researcher /chat page; unmount()
 *   ends running view timers, so no viewing time is lost.
 *
 * Inputs:
 *   ScenarioView.mount(container, options):
 *     container  element the view is drawn into (it gets the "layout" class; its previous content is replaced).
 *     options    {query: the query string identifying the scenario for the content endpoints — "t=<token>"
 *                 in the participant flow (the server works out the conversation from the participant's
 *                 current stage), or the researcher preview's own query (see app.js),
 *                 toggle: the button that opens/closes the workspace panel (kept hidden until the panel is ready),
 *                 panel: optional {tab, item, syncUrl} for researcher deep links (see workspace.js)}.
 *   JSON from /api/stimulus?<query> ({user_request, final_response, trace?}) and /api/workspace?<query>.
 *
 * Outputs:
 *   DOM inside `container`. Text is inserted with textContent; only the final response and agent messages go
 *   through renderMarkdown (which escapes HTML). Interaction events for the panel (workspace_panel_open/close).
 *   Activity-card events, each logged with Interaction.log and also dispatched on `container` as a "trace-card"
 *   DOM event whose `detail` is {type, card_id, client_ms[, duration_ms]}:
 *     trace_card_expand    a card was opened;
 *     trace_card_collapse  a card was closed;
 *     trace_card_view      how long a card was open (duration_ms, counted only while the page is visible), sent
 *                          when it is closed and, for a card still open, when the conversation is left.
 *   card_id is "step-<n>", the card's displayed step number, so with the trial id it identifies the card.
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

  let cardCount = 0; // distinct element ids across mounts

  /** One activity card: a header button (step number, action sentence, tool name, chevron) and, collapsed until
   *  the header is clicked, the details (arguments and outcome summary) of one step. */
  function activityCard(step) {
    const cardId = "step-" + step.step;
    const card = el("div", "card " + step.outcome.status);
    card.dataset.cardId = cardId;
    const head = el("button", "card-head");
    head.type = "button";
    head.setAttribute("aria-expanded", "false");
    head.appendChild(el("span", "step-num", String(step.step)));
    head.appendChild(el("span", "action", step.action));
    head.appendChild(el("code", "tool", step.tool));
    const chevron = el("span", "card-chevron");
    chevron.setAttribute("aria-hidden", "true");
    head.appendChild(chevron);
    card.appendChild(head);
    const body = el("div", "card-body");
    body.id = "card-body-" + ++cardCount;
    body.hidden = true;
    head.setAttribute("aria-controls", body.id);
    if (step.arguments.length) {
      const dl = el("dl", "args");
      step.arguments.forEach((a) => {
        dl.appendChild(el("dt", null, a.name));
        dl.appendChild(el("dd", null, a.value + (a.truncated ? " …" : "")));
      });
      body.appendChild(dl);
    }
    const out = el("div", "outcome");
    out.appendChild(el("span", "status", step.outcome.status === "error" ? "Error" : "Result"));
    out.appendChild(el("span", "summary", step.outcome.summary));
    body.appendChild(out);
    card.appendChild(body);
    return card;
  }

  /**
   * Make the activity cards inside `chat` expandable, each on its own, and report what happens to them as
   * "trace-card" events on `container` (see the header). Returns {flush, end}: flush() reports how long every
   * card that is still open has been open (on page hide; the cards keep working if the page is shown again),
   * and end() does that and stops listening (on unmount).
   * Viewing time uses Interaction's timers, which only count while the page is visible.
   */
  function traceCards(chat, container) {
    const open = new Set();
    const emit = (type, cardId, extra) => {
      I.log(type, Object.assign({ card_id: cardId }, extra || {}));
      container.dispatchEvent(new CustomEvent("trace-card", {
        bubbles: true, detail: Object.assign({ type: type, card_id: cardId, client_ms: Date.now() }, extra || {}) }));
    };
    const viewed = (cardId) => {
      open.delete(cardId);
      emit("trace_card_view", cardId, { duration_ms: I.stopTimer("card:" + cardId) });
    };
    function onClick(e) {
      const head = e.target.closest(".card-head");
      if (!head || !chat.contains(head)) return;
      const card = head.closest(".card"), cardId = card.dataset.cardId;
      const expand = head.getAttribute("aria-expanded") !== "true";
      head.setAttribute("aria-expanded", String(expand));
      card.classList.toggle("expanded", expand);
      card.querySelector(".card-body").hidden = !expand;
      if (expand) {
        open.add(cardId);
        I.startTimer("card:" + cardId);
        emit("trace_card_expand", cardId);
      } else {
        emit("trace_card_collapse", cardId);
        viewed(cardId);
      }
    }
    chat.addEventListener("click", onClick);
    const flush = () => [...open].forEach(viewed);
    return {
      flush: flush,
      end() {
        flush();
        chat.removeEventListener("click", onClick);
      },
    };
  }

  /** Draw the conversation: the request, the `view.trace` entries if there are any, then the final response. */
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
    const query = options.query || "";
    const toggle = options.toggle;
    let alive = true;      // false after unmount: late responses are then ignored
    let controller = null; // workspace panel, once its data has arrived
    let isOpen = false;
    let cards = null;      // the activity cards' controller, once the conversation is drawn

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
    // A closing page ends the running views, so durations are not lost.
    const onPageHide = () => {
      setOpen(false);
      if (cards) cards.flush();
    };
    toggle.addEventListener("click", onToggle);
    window.addEventListener("pagehide", onPageHide);

    function startPanel(data) {
      if (!alive) return;
      controller = root.WorkspaceSidebar.init(panel, data, query, () => setOpen(false), options.panel || {});
      toggle.hidden = false;
      if (options.panel && options.panel.tab) setOpen(true); // researcher deep link only; participants open it themselves
    }
    // The snapshot may be missing for a scenario; the panel (with its Skills tab) is offered either way.
    getJson("/api/workspace?" + query).then(startPanel, () => startPanel(null));

    const ready = getJson("/api/stimulus?" + query)
      .then((view) => {
        if (alive) {
          renderConversation(chat, view);
          cards = traceCards(chat, container);
        }
        return alive;
      })
      .catch(() => {
        if (alive) chat.replaceChildren(el("p", "error-msg", "This conversation could not be loaded."));
        return false;
      });

    function unmount() {
      if (!alive) return;
      setOpen(false);
      if (cards) cards.end();
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
