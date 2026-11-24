/*
 * Purpose:
 *   Page script of the standalone chat page (/chat): reads the stimulus and condition from the URL and mounts the
 *   reusable scenario view (scenario_view.js) into the page. The rendering itself lives in scenario_view.js so
 *   that other pages can show a scenario the same way.
 *
 * Inputs:
 *   Query params ?s=<stimulus folder>&c=low|high (and optional pid=<participant id>). Researcher-preview
 *   parameters: w=<tab>[&i=<id>] opens the panel on that tab (and keeps the URL in sync with it), preview=1 turns
 *   interaction logging off (interaction.js).
 *
 * Outputs:
 *   The mounted scenario view inside #scenario; a session_start interaction event.
 *
 * Dependencies:
 *   scenario_view.js (ScenarioView), interaction.js (Interaction), and through them markdown.js, previews.js,
 *   workspace.js, skills.js.
 */
(function () {
  "use strict";
  const p = new URLSearchParams(location.search);
  const stimulus = p.get("s") || "";
  const condition = p.get("c") || "";
  const tab = p.get("w");

  Interaction.log("session_start", Object.assign({ stimulus, condition },
    Interaction.participantId ? { pid: Interaction.participantId } : {}));
  ScenarioView.mount(document.getElementById("scenario"), {
    stimulus, condition,
    toggle: document.getElementById("ws-toggle"),
    panel: tab ? { tab, item: p.get("i"), syncUrl: true } : {},
  });
})();
