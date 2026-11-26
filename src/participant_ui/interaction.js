/*
 * Purpose:
 *   Records participant interactions (skill documentation opened and for how long, workspace panels used) by
 *   posting small events to /api/log, where the server stores them apart from all participant-facing content.
 *   Also provides visibility-aware timers: time while the browser tab is hidden does not count as viewing time.
 *
 * Inputs:
 *   Calls from app.js, flow.js, workspace.js and skills.js: Interaction.log(type, fields),
 *   Interaction.startTimer(key), Interaction.stopTimer(key), Interaction.setSession(id). URL parameters: pid
 *   (optional participant id), preview=1 (log nothing; for researchers inspecting the interface).
 *
 * Outputs:
 *   POST /api/log with {session, type, client_ms, ...fields}; the allowed types and fields are those of
 *   interaction_log.py. Nothing is shown to the participant and no response is read.
 *
 * Dependencies:
 *   participant_server.py (POST /api/log), interaction_log.py (validation on the server).
 */
(function (root) {
  "use strict";
  const params = new URLSearchParams(location.search);
  const enabled = params.get("preview") !== "1";
  // One session id per page by default. In the experiment flow each trial is its own session, so flow.js
  // replaces it with that trial's id (P001-t03) before the scenario is mounted; the server stamps the same id on
  // the session_start event it writes itself, which is what ties the events of a trial together.
  let session = (crypto.randomUUID && crypto.randomUUID()) ||
    ("s-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10));

  /** Name the session after the trial being shown. Ignored for anything that is not a non-empty string. */
  function setSession(id) {
    if (typeof id === "string" && id) session = id;
  }

  /** Send one event; failures are ignored so logging can never disturb the participant. */
  function log(type, fields) {
    if (!enabled) return;
    try {
      fetch("/api/log", {
        method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ session, type, client_ms: Date.now() }, fields || {})),
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

  root.Interaction = { log, startTimer, stopTimer, setSession, enabled,
                       get session() { return session; },
                       participantId: params.get("pid") };
})(window);
