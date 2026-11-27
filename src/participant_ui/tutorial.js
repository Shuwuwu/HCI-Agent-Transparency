/*
 * Purpose:
 *   The guided practice tour: a step-by-step walk through the study interface on the practice conversation. Each
 *   step dims the page, cuts a spotlight around one interface element, and anchors a tooltip with an arrow to it.
 *   The spotlit element stays live, so participants click and try the real interface as they go; clicks anywhere
 *   else are blocked until the tour ends.
 *
 *   The tour describes what each part of the interface shows and how to use it. This file is sent to participants'
 *   browsers as it is, comments included, so the rules for its wording are kept in the README instead.
 *
 *   Two pieces:
 *     Tour          the engine (overlay, spotlight, tooltip placement, navigation). It knows nothing about the
 *                   study; it is given a list of steps.
 *     PracticeTour  the practice steps (the STEPS table below) and how they are started from the flow page.
 *
 * Inputs:
 *   PracticeTour.start({host, onFinish}) once the practice conversation and the workspace panel have been drawn:
 *     host      the element the scenario view is mounted in (#stage);
 *     onFinish  called once, when the participant completes the last step.
 *   The workspace panel is always open beside the conversation, so no step opens or closes it.
 *   The steps are read from the page as it is drawn, never from the server: a step with an include() test, such
 *   as the activity-card step, is part of the tour only when its element is on the page.
 *
 * Outputs:
 *   An overlay appended to <body> while the tour runs; it is removed by stop() or when the tour finishes. The tour
 *   logs nothing itself. Interface events triggered by clicking the real interface during the tour (a tab, an
 *   item, a skill document, an activity card) are logged as usual by interaction.js, under the practice trial's
 *   id, and the server marks them as practice (participant_flow.log_event).
 *
 * Dependencies:
 *   None at load time. Expects the DOM of scenario_view.js, workspace.js and skills.js (the selectors in STEPS).
 *   Loaded by flow.html before flow.js, which starts it on the practice scenario stage only.
 *
 * Usage:
 *   const tour = PracticeTour.start({ host, onFinish: () => { ... } });   tour.stop();
 */
(function (root) {
  "use strict";

  const PAD = 6;     // space between an element and the spotlight edge
  const GAP = 14;    // space between the spotlight and the tooltip (the arrow sits in it)
  const EDGE = 12;   // closest a tooltip may come to the window edge
  const ADVANCE_DELAY_MS = 450; // after a "try it" action succeeds, so the participant sees the result first

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  /** The element's box, or null when it is missing or not displayed (zero size). */
  function visibleRect(element) {
    if (!element || !element.isConnected) return null;
    const r = element.getBoundingClientRect();
    return r.width > 0 && r.height > 0 ? r : null;
  }

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  /**
   * Where the tooltip goes for a spotlight rectangle `r` (null: no target) and a tooltip of size w x h in a
   * vw x vh window. Tries below, above, right, then left of the target, taking the first side with room; if no
   * side has room (a target filling the screen, such as the workspace panel on a phone) the tooltip is docked at
   * the bottom of the window without an arrow. Returns {left, top, side, arrow} where `side` is the side of the
   * target the tooltip is on and `arrow` the arrow's offset along the tooltip edge that faces the target.
   */
  function place(r, w, h, vw, vh) {
    if (!r) return { left: (vw - w) / 2, top: Math.max(EDGE, (vh - h) / 2), side: "none" };
    const cx = (r.left + r.right) / 2, cy = (r.top + r.bottom) / 2;
    const fitsX = (left) => clamp(left, EDGE, vw - w - EDGE);
    const fitsY = (top) => clamp(top, EDGE, vh - h - EDGE);
    if (vh - r.bottom >= h + GAP + EDGE) {
      const left = fitsX(cx - w / 2);
      return { left, top: r.bottom + GAP, side: "bottom", arrow: clamp(cx - left, 18, w - 18) };
    }
    if (r.top >= h + GAP + EDGE) {
      const left = fitsX(cx - w / 2);
      return { left, top: r.top - GAP - h, side: "top", arrow: clamp(cx - left, 18, w - 18) };
    }
    if (vw - r.right >= w + GAP + EDGE) {
      const top = fitsY(cy - h / 2);
      return { left: r.right + GAP, top, side: "right", arrow: clamp(cy - top, 18, h - 18) };
    }
    if (r.left >= w + GAP + EDGE) {
      const top = fitsY(cy - h / 2);
      return { left: r.left - GAP - w, top, side: "left", arrow: clamp(cy - top, 18, h - 18) };
    }
    return { left: (vw - w) / 2, top: vh - h - EDGE, side: "none" };
  }

  /**
   * Run a tour over `steps`. A step is
   * {title, text, intro?, target?, fallback?, fallbackText?, before?, done?, action?}:
   *   intro         an optional paragraph shown above the text and its action label;
   *   target()      the element to spotlight, looked up again on every frame (panels re-render their DOM);
   *                 a step without one is a centred message;
   *   fallback()    spotlit instead while target() is not on screen, with fallbackText added to the text;
   *   before()      run when the step is entered (for example, to show the tab a step is about);
   *   done()        an action the step invites ("Try it: ..."); once it becomes true the tour moves on by itself.
   *                 It must have been false when the step began, so going Back never skips a step at once;
   *   action        the label shown before the text of a step with done(), e.g. "Try it".
   * Next is always available, so no one is stuck on a step they cannot or do not want to try.
   * `onFinish` runs when the last step's button is pressed. Returns {stop}.
   */
  function run(steps, onFinish) {
    const overlay = el("div", "tour");
    const shades = [0, 1, 2, 3].map(() => overlay.appendChild(el("div", "tour-shade")));
    const ring = overlay.appendChild(el("div", "tour-ring"));
    const tip = overlay.appendChild(el("div", "tour-tip"));
    tip.setAttribute("role", "dialog");
    tip.setAttribute("aria-labelledby", "tour-title");
    tip.setAttribute("aria-describedby", "tour-intro tour-text"); // a missing or hidden intro is simply skipped
    tip.tabIndex = -1;
    const arrow = tip.appendChild(el("div", "tour-arrow"));
    const count = tip.appendChild(el("p", "tour-count"));
    const title = tip.appendChild(el("h2", "tour-title"));
    title.id = "tour-title";
    const intro = tip.appendChild(el("p", "tour-intro")); // a step's optional lead-in, above its instruction
    intro.id = "tour-intro";
    intro.hidden = true;
    const text = tip.appendChild(el("p", "tour-text"));
    text.id = "tour-text";
    const nav = tip.appendChild(el("div", "tour-nav"));
    const back = nav.appendChild(el("button", "tour-back", "Back"));
    const next = nav.appendChild(el("button", "tour-next", "Next"));
    back.type = next.type = "button";
    document.body.appendChild(overlay);

    let index = -1, frame = 0, timer = 0, startedDone = false, last = "", usingFallback = null;

    function show(i) {
      clearTimeout(timer);
      timer = 0;
      index = i;
      const step = steps[i];
      if (step.before) step.before();
      startedDone = !!(step.done && step.done());
      usingFallback = null;
      count.textContent = "Step " + (i + 1) + " of " + steps.length;
      title.textContent = step.title;
      setText(step, false);
      back.disabled = i === 0;
      next.textContent = i === steps.length - 1 ? "Finish tour" : "Next";
      last = ""; // force a fresh layout
      tip.classList.remove("tour-in");
      void tip.offsetWidth; // restart the fade-in
      tip.classList.add("tour-in");
      const target = step.target && step.target();
      if (visibleRect(target)) target.scrollIntoView({ block: "nearest", inline: "nearest" });
      layout(); // at once, not only on the next frame (frames pause while the page is in the background)
      tip.focus({ preventScroll: true }); // screen readers announce the new step (role=dialog, labelled)
    }

    /** Fill the tooltip's body: the step's optional intro paragraph, then its instruction (with the bold action
     *  label, such as "Try it now:", when the step has one) and, while the step's target is not on screen, the
     *  fallback note. Steps without an intro look exactly as before. */
    function setText(step, fallback) {
      intro.textContent = step.intro || "";
      intro.hidden = !step.intro;
      text.replaceChildren();
      if (step.action && !fallback) text.appendChild(el("strong", "tour-action", step.action + ": "));
      text.appendChild(document.createTextNode(step.text));
      if (fallback && step.fallbackText) text.appendChild(el("span", "tour-fallback", " " + step.fallbackText));
    }

    /** Lay the overlay out for the current step, writing to the DOM only when something moved. It runs on every
     *  scroll (the page's or a panel's), resize and change to the page (a panel opening, a tab switching, content
     *  re-rendering), and on every animation frame as a backstop, so the spotlight follows its element whatever
     *  moves it. The events matter: browsers slow or pause animation frames for pages they consider hidden. */
    function layout() {
      const step = steps[index];
      if (step.done && !timer) {
        const now = !!step.done();
        if (now && !startedDone) timer = setTimeout(() => (index === steps.indexOf(step) ? go(1) : 0), ADVANCE_DELAY_MS);
        if (!now) startedDone = false; // undone after starting done: the next completion counts
      }
      let rect = step.target ? visibleRect(step.target()) : null;
      const fallback = step.target && !rect && step.fallback ? visibleRect(step.fallback()) : null;
      if (fallback) rect = fallback;
      if (usingFallback !== !!fallback) {
        usingFallback = !!fallback;
        setText(step, usingFallback);
      }
      const vw = document.documentElement.clientWidth, vh = window.innerHeight;
      const hole = rect && {
        left: clamp(rect.left - PAD, 0, vw), top: clamp(rect.top - PAD, 0, vh),
        right: clamp(rect.right + PAD, 0, vw), bottom: clamp(rect.bottom + PAD, 0, vh),
      };
      const w = tip.offsetWidth, h = tip.offsetHeight;
      const key = [vw, vh, w, h, hole && [hole.left, hole.top, hole.right, hole.bottom].map(Math.round)].join();
      if (key === last) return;
      last = key;

      // Four shades around the hole dim the page and block clicks outside it; the hole itself is empty, so the
      // spotlit element receives clicks as normal. Without a target, one shade covers everything.
      const boxes = hole
        ? [[0, 0, vw, hole.top], [0, hole.bottom, vw, vh - hole.bottom],
           [0, hole.top, hole.left, hole.bottom - hole.top], [hole.right, hole.top, vw - hole.right, hole.bottom - hole.top]]
        : [[0, 0, vw, vh], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]];
      boxes.forEach(([x, y, bw, bh], k) => Object.assign(shades[k].style, {
        left: x + "px", top: y + "px", width: Math.max(0, bw) + "px", height: Math.max(0, bh) + "px" }));
      ring.hidden = !hole;
      if (hole) Object.assign(ring.style, { left: hole.left + "px", top: hole.top + "px",
        width: hole.right - hole.left + "px", height: hole.bottom - hole.top + "px" });

      const spot = place(hole && { left: hole.left, top: hole.top, right: hole.right, bottom: hole.bottom }, w, h, vw, vh);
      tip.style.left = Math.round(spot.left) + "px";
      tip.style.top = Math.round(spot.top) + "px";
      tip.dataset.side = spot.side;
      arrow.hidden = spot.side === "none";
      // The arrow sits on the tooltip edge that faces the target, pointing at the target's centre.
      arrow.style.left = arrow.style.top = "";
      if (spot.side === "bottom" || spot.side === "top") arrow.style.left = spot.arrow + "px";
      else if (spot.side !== "none") arrow.style.top = spot.arrow + "px";
    }

    function tick() {
      layout();
      frame = requestAnimationFrame(tick);
    }

    function go(delta) {
      const i = index + delta;
      if (i >= steps.length) return finish();
      if (i >= 0) show(i);
    }

    const relayout = () => { if (index >= 0) layout(); };
    // The tour's own writes to the overlay are not changes to the page.
    const changes = new MutationObserver((records) => { if (records.some((r) => !overlay.contains(r.target))) relayout(); });
    changes.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
    window.addEventListener("scroll", relayout, { capture: true, passive: true }); // capture: panel scrolls too
    window.addEventListener("resize", relayout);

    function stop() {
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      changes.disconnect();
      window.removeEventListener("scroll", relayout, { capture: true });
      window.removeEventListener("resize", relayout);
      overlay.remove();
    }

    function finish() {
      stop();
      if (onFinish) onFinish();
    }

    back.onclick = () => go(-1);
    next.onclick = () => go(1);
    show(0);
    tick();
    return { stop };
  }

  root.Tour = { run, place };

  // ---- the practice tour ---------------------------------------------------------------------------------------
  // Before changing any wording below, read the tour's wording rules in the README.

  function startPractice(options) {
    const host = options.host;
    const q = (selector) => host.querySelector(selector) || document.querySelector(selector);
    const tab = (label) => [...document.querySelectorAll("#workspace .tab")].find((b) => b.textContent === label);

    const STEPS = [
      { title: "Practice Tutorial",
        text: "Explore the interface using a practice conversation. "
          + "Interact with highlighted areas and use Next and Back to navigate." },
      { title: "User Request",
        text: "This is what Emma asked the assistant to do.",
        target: () => q(".chat .user-bubble") },
      { title: "Assistant Activity",
          intro: "These steps show what the assistant did, in chronological order. The tool used for each step is shown in the top-right corner.",
          action: "Try it now",
          text: "Click the highlighted step to view its details. Click again to close it.",
        target: () => q(".chat .card"),
        include: () => !!q(".chat .card") },
      { title: "Assistant Response",
        text: "This is the assistant's reply to Emma.",
        target: () => q(".chat .assistant-bubble.final") },
      { title: "Recorded Conversation",
        text: "All conversations are prerecorded, so you cannot send messages to the assistant.",
        target: () => q(".composer") },
      { title: "Emma's Workspace",
        text: "This panel shows Emma's digital workspace. It stays open beside the conversation, and each side "
          + "scrolls on its own.",
        target: () => q("#workspace") },
      { title: "Workspace Tabs",
        text: "Switch between Mail, Calendar, and Drive to explore Emma's workspace as it was before "
          + "the assistant began the task.",
        target: () => q("#workspace .tabs") },
      { title: "Opening an Item",
        text: "Click an email, event or file to view it. Use ← Back to list to return. Mail has separate "
          + "Inbox and Sent folders.",
        target: () => q("#workspace .ws-body"),
        // Reached with Back from the Skills steps, the panel would still show Skills, not what this step describes.
        before: () => { const s = tab("Skills"); if (s && s.classList.contains("on")) tab("Mail").click(); } },
      { title: "Skills", action: "Try it",
        text: "Open the Skills tab. It lists the skills the assistant was given for this task.",
        target: () => tab("Skills"),
        done: () => !!tab("Skills") && tab("Skills").classList.contains("on") },
      { title: "Viewing a Skill",
        text: "Click a skill to view its documentation. Use ← All skills to return to the list.",
        target: () => q("#workspace .ws-body"),
        before: () => { const s = tab("Skills"); if (s && !s.classList.contains("on")) s.click(); } },
      { title: "Tutorial Complete",
        text: "You can explore the practice conversation further before continuing to the practice questions. "
          + "The remaining conversations follow the same format.",
        target: () => document.querySelector("#advance button") },
    ].filter((step) => !step.include || step.include());

    return run(STEPS, options.onFinish);
  }

  root.PracticeTour = { start: startPractice };
})(window);
