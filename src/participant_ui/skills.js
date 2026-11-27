/*
 * Purpose:
 *   The Skills tab of the workspace panel: lists the skills the agent was given for this task and shows the
 *   Markdown documentation of the one the participant chooses. Nothing is opened, loaded or highlighted
 *   automatically: the list is fetched when the tab is first selected and a document only when a skill is clicked.
 *
 * Inputs:
 *   /api/skills?<query> and /api/skill?<query>&k=<skill id>, where <query> identifies the scenario the way
 *   scenario_view.js documents it; markdown.js (renderMarkdown).
 *
 * Outputs:
 *   DOM inside the body element given to render(); interaction events skill_doc_open / skill_doc_view (with
 *   visible duration) via interaction.js. A document still open when the panel is paused gets a skill_doc_view
 *   then, and another for the time after it is resumed.
 *
 * Dependencies:
 *   markdown.js, interaction.js. Used by workspace.js, which owns the tabs.
 */
(function (root) {
  "use strict";
  const I = root.Interaction;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  /**
   * Create the Skills view for one conversation. `redraw` is called when an asynchronous load finishes while
   * the tab is showing. Returns {render(body), leave(), pause(), resume()}: render draws the current state (list
   * or document) into `body`; leave() ends a running document view and returns to the list (another tab chosen);
   * pause() ends the running document view but keeps the document on screen (page hidden or closed, conversation
   * left), and resume() starts timing it again without a new skill_doc_open.
   */
  function create(query, redraw) {
    let skills = null;   // list from the server, loaded on first render
    let loading = false;
    let failed = false;
    let doc = null;      // {id, name, description, markdown} while a document is displayed
    let timing = false;  // the displayed document's viewing timer runs
    let paused = false;  // between pause() and resume(): nothing is timed

    function stopTiming() {
      if (!timing) return;
      timing = false;
      I.log("skill_doc_view", { skill_id: doc.id, duration_ms: I.stopTimer("skill:" + doc.id) });
    }
    function startTiming() {
      if (timing || paused || doc === null) return;
      timing = true;
      I.startTimer("skill:" + doc.id);
    }
    function endDocView() {
      stopTiming();
      doc = null;
    }

    function openDoc(id) {
      fetch("/api/skill?" + query + "&k=" + encodeURIComponent(id))
        .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((d) => {
          // The document counts as opened only once it is actually displayed.
          endDocView();
          doc = d;
          I.log("skill_doc_open", { skill_id: id });
          startTiming();
          redraw();
        })
        .catch(() => { failed = true; redraw(); });
    }

    function render(body) {
      if (failed) { body.appendChild(el("p", "missing", "The skill documentation could not be loaded.")); return; }
      if (doc) {
        const back = el("button", "back", "← All skills");
        back.onclick = () => { endDocView(); redraw(); };
        body.appendChild(back);
        body.appendChild(el("h2", "detail-title", doc.name));
        if (doc.description) body.appendChild(el("p", "skill-desc", doc.description));
        const md = el("div", "skill-md");
        md.innerHTML = renderMarkdown(doc.markdown); // renderMarkdown escapes all HTML in its input
        body.appendChild(md);
        return;
      }
      if (skills === null) {
        body.appendChild(el("p", "missing", "Loading…"));
        if (!loading) {
          loading = true;
          fetch("/api/skills?" + query)
            .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
            .then((list) => { skills = list; redraw(); })
            .catch(() => { failed = true; redraw(); });
        }
        return;
      }
      const list = el("ul", "items");
      skills.forEach((s) => {
        const li = el("li", "item");
        const b = el("button", "item-btn");
        b.appendChild(el("div", "item-title", s.name));
        b.appendChild(el("div", "item-sub wrap", s.description));
        b.onclick = () => openDoc(s.id);
        li.appendChild(b);
        list.appendChild(li);
      });
      body.appendChild(list);
    }

    return {
      render,
      leave() { endDocView(); },
      pause() { stopTiming(); paused = true; },
      resume() { paused = false; startTiming(); },
    };
  }

  root.SkillsView = { create };
})(window);
