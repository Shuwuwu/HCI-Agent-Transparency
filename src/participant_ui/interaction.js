/*
 * Purpose:
 *   Records what a participant does on the page besides reading the conversation (the workspace panel and its
 *   tabs, items and documents opened and for how long, activity cards opened and for how long) by posting small
 *   events to /api/log. Also provides visibility-aware timers: time while the browser tab is hidden does not
 *   count as viewing time.
 *
 * Inputs:
 *   Calls from flow.js, scenario_view.js, workspace.js and skills.js: Interaction.log(type, fields),
 *   Interaction.startTimer(key), Interaction.stopTimer(key), Interaction.setLink(token), Interaction.setTrial(id).
 *
 * Outputs:
 *   POST /api/log with {token, trial_id, type, client_ms, ...fields}; the allowed types and fields are those of
 *   interaction_log.py. Nothing is sent until flow.js has given the link token and the trial id, so a page opened
 *   outside a participant link records nothing. Nothing is shown to the participant and no response is read.
 *
 * Dependencies:
 *   participant_server.py (POST /api/log), interaction_log.py and participant_flow.py (checks on the server).
 */
(function (root) {
  "use strict";
  let token = null; // the participant's link token (flow.js)
  let trial = null; // the trial the page is showing; the server accepts events only for the current one

  /** Give the link token of the participant this page belongs to. Ignored unless a non-empty string. */
  function setLink(value) {
    if (typeof value === "string" && value) token = value;
  }

  /** Name the trial being shown. Events sent while the next stage loads still carry the trial they belong to. */
  function setTrial(id) {
    if (typeof id === "string" && id) trial = id;
  }

  /** Send one event; failures are ignored so logging can never disturb the participant. */
  function log(type, fields) {
    if (!token || !trial) return;
    try {
      fetch("/api/log", {
        method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ token: token, trial_id: trial, type: type, client_ms: Date.now() },
          fields || {})),
      }).catch(() => {});
    } catch (e) { /* ignore */ }
  }

  // Timers accumulate only while the page is visible.
  const timers = new Map();
  const now = () => performance.now();
  function startTimer(key) { timers.set(key, { acc: 0, since: document.hidden ? null : now() }); }
  /** Stop a timer and return the visible milliseconds (0 if it was never started). */
  function stopTimer(key) {
    const t = timers.get(key);
    if (!t) return 0;
    timers.delete(key);
    return Math.round(t.acc + (t.since === null ? 0 : now() - t.since));
  }
  document.addEventListener("visibilitychange", () => {
    timers.forEach((t) => {
      if (document.hidden && t.since !== null) { t.acc += now() - t.since; t.since = null; }
      else if (!document.hidden && t.since === null) t.since = now();
    });
  });

  root.Interaction = { log, startTimer, stopTimer, setLink, setTrial,
                       get trial() { return trial; } };
})(window);
