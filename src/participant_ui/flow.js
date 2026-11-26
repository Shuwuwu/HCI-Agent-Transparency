/*
 * Purpose:
 *   Page script of the participant flow (flow.html, served at /p/<token>). It asks the server which stage the
 *   participant is to be shown, draws that stage, and asks the server to complete it when they continue. The
 *   page keeps no record of where the participant is: every stage comes from /api/stage, so reloading the page
 *   or reopening the link resumes on the same stage, and the stage can only be advanced one step at a time.
 *
 *   The consent screen shows the real consent text (src/study_materials/consent.md, sent by the server with the
 *   stage) and its agreement checkbox; ticking it and continuing is what stores the participant's consent. The
 *   assessment after each conversation is the shared form of assessment.js, the same for the practice and every
 *   trial; the questionnaire is still a placeholder. The scenario stage is drawn by scenario_view.js;
 *   on the practice scenario, the guided tour of tutorial.js runs over it before the participant can continue.
 *
 * Inputs:
 *   The link token, read from location.pathname (/p/<token>); it is never written into a query string.
 *   JSON from GET /api/stage?t=<token>: {stage: {key, kind, index, trial_id?, practice?, trial?, trials?} | null,
 *   progress: {completed, total}}. The consent stage also carries consent: {markdown, checkbox, sha256}, and an
 *   assessment stage carries assessment (see assessment.js). The
 *   stage identifies a trial by its trial id only; the server decides what that trial shows.
 *
 * Outputs:
 *   The stage drawn into #stage and its continue button into #advance; POST /api/stage/complete to advance (at
 *   consent, with {agreed: true, sha256} so the server records the agreement to the exact text shown; at an
 *   assessment, with the answers, which the server stores against the trial).
 *   For a trial, the mounted scenario view and its interaction events, logged under that trial's id as the
 *   session (the matching session_start event is written by the server).
 *
 * Dependencies:
 *   scenario_view.js (ScenarioView), interaction.js (Interaction), tutorial.js (PracticeTour), assessment.js
 *   (AssessmentForm), and through them
 *   markdown.js, previews.js, workspace.js, skills.js. participant_server.py (/api/stage, /api/stage/complete and
 *   the content routes).
 */
(function () {
  "use strict";
  const token = decodeURIComponent(location.pathname.replace(/^\/p\//, ""));
  const host = document.getElementById("stage");
  const bar = document.getElementById("advance");
  const progress = document.getElementById("progress");
  const toggle = document.getElementById("ws-toggle");
  let view = null; // the mounted ScenarioView, so it can be unmounted when the stage changes
  let tour = null; // the running practice tour (tutorial.js), so it can be removed when the stage changes

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
    if (tour) {
      tour.stop();
      tour = null;
    }
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

  /** Ask the server to complete `key` and draw whatever stage comes back. `extra` is merged into the request
   *  body (the consent agreement); everything that decides what is recorded is checked again on the server. */
  function advance(key, extra) {
    fetch("/api/stage/complete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ token: token, stage_key: key }, extra || {})),
    }).then(asJson).then(draw, fail);
  }

  /** Like advance(), for a stage whose answers the participant should not lose: if the server refuses the
   *  submission (400), `onRefused` is called and the page stays as it is; any other failure is shown as usual. */
  function submit(key, extra, onRefused) {
    fetch("/api/stage/complete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ token: token, stage_key: key }, extra)),
    }).then(asJson).then(draw, (err) => (err && err.status === 400 ? onRefused() : fail(err)));
  }

  /** A card screen: heading, optional lead paragraph, a placeholder box, and a continue button. */
  function card(heading, lead, note, label, key) {
    const box = el("div", "stage-card");
    box.appendChild(el("h1", null, heading));
    if (lead) box.appendChild(el("p", null, lead));
    box.appendChild(placeholder(note));
    host.appendChild(box);
    setButton(label, () => advance(key));
  }

  /** The information letter and consent form. Continue stays disabled until the box is ticked, and the server
   *  refuses the consent stage without the agreement anyway, so this check is a convenience, not the guard. */
  function consentScreen(stage) {
    const consent = stage.consent;
    if (!consent) return fail();
    const box = el("article", "stage-card material");
    box.innerHTML = renderMarkdown(consent.markdown); // renderMarkdown escapes all HTML in the text
    const tick = el("label", "consent-check");
    const check = el("input");
    check.type = "checkbox";
    check.id = "consent-agree";
    tick.append(check, el("span", null, consent.checkbox));
    box.appendChild(tick);
    host.appendChild(box);
    const button = setButton("Continue", () => advance(stage.key, { consent: { agreed: true, sha256: consent.sha256 } }), false);
    // Say why Continue is unavailable, to sighted users under the button and to screen readers through it.
    const hint = el("p", "advance-hint", "Tick the box above to continue.");
    hint.id = "consent-hint";
    button.setAttribute("aria-describedby", hint.id);
    bar.appendChild(hint);
    check.onchange = () => {
      button.disabled = !check.checked;
      hint.hidden = check.checked; // hidden keeps its line (style.css), so the button does not move when ticked
      // aria-describedby is read even from a hidden element, so the link goes with the hint.
      if (check.checked) button.removeAttribute("aria-describedby");
      else button.setAttribute("aria-describedby", hint.id);
    };
  }

  /** The questions after a conversation: the shared form of assessment.js, the same for the practice and every
   *  trial. Continue checks the answers first (and says what is missing); the server checks them again, stores
   *  them with the trial, and only then moves the participant on. A refusal keeps the form and its answers. */
  function assessmentScreen(stage) {
    if (!stage.assessment) return fail();
    const form = AssessmentForm.render(host, stage.assessment, stage.practice ? "Practice questions" : "Your assessment");
    const button = setButton("Continue", () => {
      const result = form.collect();
      form.showMessage("");
      if (result.errors.length) {
        form.showErrors(result.errors);
        button.disabled = false;
        return;
      }
      submit(stage.key, { answers: result.answers }, () => {
        form.showMessage("Your answers could not be saved. Please check them and try again, or tell the researcher.");
        button.disabled = false;
      });
    });
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
  /** A trial: name the interaction session after the trial and mount the real scenario view. The practice trial
   *  also runs the guided tour (tutorial.js) over it, and its Continue stays disabled until the tour is finished:
   *  the tour blocks clicks outside its spotlight, and this also stops a keyboard user tabbing past it. */
  function scenarioScreen(stage) {
    Interaction.setSession(stage.trial_id);
    const mounted = ScenarioView.mount(host, { query: "t=" + encodeURIComponent(token), toggle: toggle, panel: {} });
    view = mounted;
    // The participant cannot continue until the conversation is actually on the screen.
    const button = setButton(stage.practice ? "Continue to the practice questions" : "Continue", () => advance(stage.key), false);
    mounted.ready.then((ok) => {
      if (view !== mounted) return; // the stage changed while the conversation was loading
      if (!ok) return fail("This conversation could not be loaded. Please tell the researcher.");
      if (!stage.practice) {
        button.disabled = false;
        return;
      }
      tour = PracticeTour.start({ host: host, toggle: toggle, onFinish: () => {
        tour = null;
        button.disabled = false;
        button.focus();
      } });
    });
  }

  const SCREENS = {
    consent: consentScreen,
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
