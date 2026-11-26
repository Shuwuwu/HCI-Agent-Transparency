/*
 * Purpose:
 *   Page script of the participant flow (flow.html, served at /p/<token>). It asks the server which stage the
 *   participant is to be shown, draws that stage, and asks the server to complete it when they continue. The
 *   page keeps no record of where the participant is: every stage comes from /api/stage, so reloading the page
 *   or reopening the link resumes on the same stage, and the stage can only be advanced one step at a time.
 *
 *   The screens for consent, the instructions, the assessment and the questionnaire are placeholders: the study
 *   materials are not written yet, and participant responses are not collected or stored anywhere until the data
 *   collection block. Only the scenario stage is the real thing, drawn by scenario_view.js.
 *
 * Inputs:
 *   The link token, read from location.pathname (/p/<token>); it is never written into a query string.
 *   JSON from GET /api/stage?t=<token>: {stage: {key, kind, index, trial_id?, practice?, trial?, trials?} | null,
 *   progress: {completed, total}}. The stage deliberately carries no stimulus folder, scenario name or
 *   transparency condition (participant_flow.py explains why), so this script cannot reveal them.
 *
 * Outputs:
 *   The stage drawn into #stage and its continue button into #advance; POST /api/stage/complete to advance.
 *   For a trial, the mounted scenario view and its interaction events, logged under that trial's id as the
 *   session (the matching session_start event is written by the server, which knows the condition).
 *
 * Dependencies:
 *   scenario_view.js (ScenarioView), interaction.js (Interaction), and through them markdown.js, previews.js,
 *   workspace.js, skills.js. participant_server.py (/api/stage, /api/stage/complete and the content routes).
 */
(function () {
  "use strict";
  const token = decodeURIComponent(location.pathname.replace(/^\/p\//, ""));
  const host = document.getElementById("stage");
  const bar = document.getElementById("advance");
  const progress = document.getElementById("progress");
  const toggle = document.getElementById("ws-toggle");
  let view = null; // the mounted ScenarioView, so it can be unmounted when the stage changes

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  /** A dashed box standing in for study material that has not been written yet. */
  function placeholder(what) {
    const box = el("div", "placeholder");
    box.appendChild(el("strong", "placeholder-tag", "Placeholder"));
    box.appendChild(document.createTextNode(what));
    return box;
  }

  /** Replace the continue bar with one button; `onClick` is called once and the button then stays disabled. */
  function setButton(label, onClick, enabled) {
    const button = el("button", null, label);
    button.disabled = enabled === false;
    button.onclick = () => {
      button.disabled = true; // a second click cannot submit the stage twice
      onClick();
    };
    bar.replaceChildren(button);
    bar.hidden = false;
    return button;
  }

  function clearStage() {
    if (view) {
      view.unmount();
      view = null;
    }
    host.classList.remove("layout");
    host.replaceChildren();
    bar.replaceChildren();
    bar.hidden = true;
    toggle.hidden = true;
  }

  /** Show why the flow stopped. Only the 409 message is the server's own wording: it is the one failure a
   *  participant can be told something useful about (their sequence changed and a researcher must look). */
  function fail(err) {
    clearStage();
    progress.textContent = "";
    const message = err && err.status === 409 && err.error ? err.error
      : err && err.status === 404 ? "This link is not valid. Please check the link you were given, or tell the researcher."
      : "This page could not be loaded. Please tell the researcher.";
    host.appendChild(el("p", "flow-error", message));
  }

  const asJson = (r) => (r.ok ? r.json()
    : r.json().catch(() => ({})).then((b) => Promise.reject({ status: r.status, error: b.error })));

  /** Ask the server to complete `key` and draw whatever stage comes back. */
  function advance(key) {
    fetch("/api/stage/complete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token, stage_key: key }),
    }).then(asJson).then(draw, fail);
  }

  /** A card screen: heading, optional lead paragraph, a placeholder box, and a continue button. */
  function card(heading, lead, note, label, key, options) {
    const box = el("div", "stage-card");
    box.appendChild(el("h1", null, heading));
    if (lead) box.appendChild(el("p", null, lead));
    box.appendChild(placeholder(note));
    const extra = (options || {}).extra;
    if (extra) box.appendChild(extra);
    host.appendChild(box);
    const button = setButton(label, () => advance(key), !(options || {}).holdUntilChecked);
    return button;
  }

  function consentScreen(stage) {
    const tick = el("label", "consent-check");
    const check = el("input");
    check.type = "checkbox";
    check.id = "consent-agree";
    tick.appendChild(check);
    tick.appendChild(el("span", null, "I have read the information above and agree to take part."));
    const button = card("Consent", "Please read the information below before you begin.",
      " The approved consent form goes here, together with the information letter and the contact details of the"
      + " research team.", "Continue", stage.key, { extra: tick, holdUntilChecked: true });
    check.onchange = () => { button.disabled = !check.checked; };
  }

  function instructionsScreen(stage) {
    card("Before you start", "You will see a series of recorded conversations with a workspace assistant.",
      " The study instructions go here: what the assistant is, what the participant is asked to judge about each"
      + " conversation, and how long the session takes.", "Continue", stage.key);
  }

  function assessmentScreen(stage) {
    const what = stage.practice
      ? " The practice assessment items go here, so the participant answers one set before the trials begin."
      : " The scenario assessment items go here. Responses are not collected yet: the questionnaire storage and"
        + " the response records are built in the data collection block.";
    card(stage.practice ? "Practice questions" : "Your assessment",
      "Please answer these questions about the conversation you have just read.", what, "Continue", stage.key);
  }

  function questionnaireScreen(stage) {
    card("A few last questions", "These questions are about the session as a whole, not one conversation.",
      " The post-study questionnaire goes here.", "Finish", stage.key);
  }

  /** The last screen. Reaching it completes the final stage, which is what marks the participant finished in
   *  the database; there is nothing to draw afterwards, so the answer is not redrawn. Reopening the link lands
   *  here again (the stage is then already complete and the repeated submission changes nothing). */
  function completionScreen(stage) {
    const box = el("div", "stage-card");
    box.appendChild(el("h1", null, "Thank you"));
    box.appendChild(el("p", null, "You have completed the study. You may now close this page."));
    box.appendChild(placeholder(" The debriefing text and any compensation details go here."));
    host.appendChild(box);
    if (!stage) return;
    fetch("/api/stage/complete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token, stage_key: stage.key }),
    }).catch(() => {});
  }

  /** A trial: name the interaction session after the trial and mount the real scenario view. */
  function scenarioScreen(stage) {
    Interaction.setSession(stage.trial_id);
    view = ScenarioView.mount(host, { query: "t=" + encodeURIComponent(token), toggle: toggle, panel: {} });
    // The participant cannot continue until the conversation is actually on the screen.
    const button = setButton(stage.practice ? "Continue to the practice questions" : "Continue", () => advance(stage.key), false);
    view.ready.then((ok) => {
      if (ok) button.disabled = false;
      else fail("This conversation could not be loaded. Please tell the researcher.");
    });
  }

  const SCREENS = {
    consent: consentScreen,
    instructions: instructionsScreen,
    scenario: scenarioScreen,
    assessment: assessmentScreen,
    questionnaire: questionnaireScreen,
    completion: completionScreen,
  };

  /** Draw one {stage, progress} payload. A null stage means every stage has been completed. */
  function draw(payload) {
    clearStage();
    const stage = payload && payload.stage;
    if (!stage) {
      progress.textContent = "";
      return completionScreen();
    }
    progress.textContent = stage.practice ? "Practice"
      : (stage.trials ? "Conversation " + stage.trial + " of " + stage.trials : "");
    const screen = SCREENS[stage.kind];
    if (!screen) return fail("This page could not be shown. Please tell the researcher.");
    screen(stage);
  }

  fetch("/api/stage?t=" + encodeURIComponent(token)).then(asJson).then(draw, fail);
})();
