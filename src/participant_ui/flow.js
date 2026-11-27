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
 *   trial (the practice shows it display-only, as an example); the questions before the practice and at the end are the form of questionnaire.js. The scenario stage
 *   is drawn by scenario_view.js; on the practice scenario, the guided tour of tutorial.js runs over it before the
 *   participant can continue.
 *
 * Inputs:
 *   The link token, read from location.pathname (/p/<token>); it is never written into a query string.
 *   JSON from GET /api/stage?t=<token>: {stage: {key, kind, index, trial_id?, practice?, trial?, trials?} | null,
 *   progress: {completed, total}}. The consent stage also carries consent: {markdown, checkbox, sha256}, an
 *   assessment stage carries assessment (see assessment.js), and the completion stage carries debriefing:
 *   {markdown}, the closing text (src/study_materials/debriefing.md), which the server sends with no earlier
 *   stage. Once every stage is complete the stage is null and the payload itself carries debriefing. The
 *   stage identifies a trial by its trial id only; the server decides what that trial shows.
 *
 * Outputs:
 *   The stage drawn into #stage and its continue button into #advance; POST /api/stage/complete to advance (at
 *   consent, with {agreed: true, sha256} so the server records the agreement to the exact text shown; at a
 *   trial's assessment, with the answers, which the server stores against the trial; at the practice assessment,
 *   with nothing; at a questionnaire, with its answers).
 *   For a trial, the mounted scenario view and its interaction events, logged with the link token under that
 *   trial's id (the server records when the trial started), including scenario_visible: how long the
 *   conversation was on screen while the page was visible.
 *
 * Dependencies:
 *   scenario_view.js (ScenarioView), interaction.js (Interaction), tutorial.js (PracticeTour), assessment.js
 *   (AssessmentForm), questionnaire.js (QuestionnaireForm), and through them
 *   markdown.js, previews.js, workspace.js, skills.js. participant_server.py (/api/stage, /api/stage/complete and
 *   the content routes).
 */
(function () {
  "use strict";
  const token = decodeURIComponent(location.pathname.replace(/^\/p\//, ""));
  const host = document.getElementById("stage");
  const bar = document.getElementById("advance");
  const progress = document.getElementById("progress");
  let view = null; // the mounted ScenarioView, so it can be unmounted when the stage changes
  let tour = null; // the running practice tour (tutorial.js), so it can be removed when the stage changes
  let visible = null; // ends the conversation's visible-time measure (scenario stages only); see trackVisible
  const PRACTICE_INTRO = "This page shows an example of the questions you will be asked after each conversation. "
    + "You do not need to answer them at this stage.";
  Interaction.setLink(token);

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
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

  /** Measure how long the conversation is on screen while the page is visible, and send it as scenario_visible
   *  when the participant continues. A page that is hidden or closed (a reload, a closed tab) sends the time so
   *  far and starts counting again, so a reloaded conversation adds to its total instead of losing it. Returns
   *  the function that sends the last part and stops measuring. */
  function trackVisible() {
    Interaction.startTimer("conversation");
    const send = () => Interaction.log("scenario_visible", { duration_ms: Interaction.stopTimer("conversation") });
    const onHide = () => {
      send();
      Interaction.startTimer("conversation");
    };
    window.addEventListener("pagehide", onHide);
    return () => {
      window.removeEventListener("pagehide", onHide);
      send();
    };
  }

  function endVisible() {
    if (visible) {
      visible();
      visible = null;
    }
  }

  function clearStage() {
    endVisible();
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
   *  trial. After a trial, Continue checks the answers first (and says what is missing); the server checks them
   *  again, stores them with the trial, and only then moves the participant on. A refusal keeps the form and its
   *  answers. The practice shows the same questions as an example only: they cannot be answered, and Continue
   *  moves on without sending anything. */
  function assessmentScreen(stage) {
    if (!stage.assessment) return fail();
    if (stage.practice) {
      AssessmentForm.render(host, stage.assessment, "Practice Questions", { displayOnly: true, intro: PRACTICE_INTRO });
      setButton("Continue", () => advance(stage.key));
      return;
    }
    const form = AssessmentForm.render(host, stage.assessment, "Your Assessment");
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

  /** A questionnaire: the one before the practice, or the one at the end of the session (questionnaire.js). As
   *  with the assessment, the button checks the answers first, the server checks and stores them, and a refusal
   *  keeps the form and its answers. Only the last questionnaire's button says Finish. */
  function questionnaireScreen(stage) {
    if (!stage.questionnaire) return fail();
    const form = QuestionnaireForm.render(host, stage.questionnaire);
    const button = setButton(stage.key === "questionnaire" ? "Finish" : "Continue", () => {
      const result = form.collect();
      form.showMessage("");
      form.showErrors(result.errors);
      if (result.errors.length) {
        button.disabled = false;
        return;
      }
      submit(stage.key, { answers: result.answers }, () => {
        form.showMessage("Your answers could not be saved. Please check them and try again, or tell the researcher.");
        button.disabled = false;
      });
    });
  }

  /** The last screen: the closing text the server sent (Markdown, drawn like the consent text). Reaching it
   *  completes the final stage, which is what marks the participant finished in the database; there is nothing
   *  to draw afterwards, so the answer is not redrawn. Reopening the link lands here again with the text sent in
   *  the payload instead (`text`); a plain thank-you is shown only if neither carries it. */
  function completionScreen(stage, text) {
    const closing = (stage && stage.debriefing) || text;
    const box = el("article", "stage-card material");
    if (closing) {
      box.innerHTML = renderMarkdown(closing.markdown); // renderMarkdown escapes all HTML in the text
    } else {
      box.appendChild(el("h1", null, "Thank you"));
      box.appendChild(el("p", null, "You have completed the study. You may now close this page."));
    }
    host.appendChild(box);
    if (!stage) return;
    fetch("/api/stage/complete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token, stage_key: stage.key }),
    }).catch(() => {});
  }

  /** A trial: name the trial events are logged under and mount the real scenario view. The practice trial
   *  also runs the guided tour (tutorial.js) over it, and its Continue stays disabled until the tour is finished:
   *  the tour blocks clicks outside its spotlight, and this also stops a keyboard user tabbing past it. */
  function scenarioScreen(stage) {
    Interaction.setTrial(stage.trial_id);
    const mounted = ScenarioView.mount(host, { query: "t=" + encodeURIComponent(token), panel: {} });
    view = mounted;
    // The participant cannot continue until the conversation is actually on the screen.
    const button = setButton(stage.practice ? "Continue to the practice questions" : "Continue", () => {
      endVisible(); // the viewing time ends when they continue, not when the next screen arrives
      advance(stage.key);
    }, false);
    mounted.ready.then((ok) => {
      if (view !== mounted) return; // the stage changed while the conversation was loading
      if (!ok) return fail("This conversation could not be loaded. Please tell the researcher.");
      visible = trackVisible();
      if (!stage.practice) {
        button.disabled = false;
        return;
      }
      tour = PracticeTour.start({ host: host, onFinish: () => {
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
      return completionScreen(null, payload && payload.debriefing);
    }
    progress.textContent = stage.practice ? "Practice"
      : (stage.trials ? "Conversation " + stage.trial + " of " + stage.trials : "");
    const screen = SCREENS[stage.kind];
    if (!screen) return fail("This page could not be shown. Please tell the researcher.");
    screen(stage);
  }

  fetch("/api/stage?t=" + encodeURIComponent(token)).then(asJson).then(draw, fail);
})();
