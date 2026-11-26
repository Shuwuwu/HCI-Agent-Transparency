/*
 * Purpose:
 *   Page script of the researcher scenario page (/chat, served only with --researcher-preview): reads the
 *   stimulus and condition from the URL and mounts the reusable scenario view (scenario_view.js). Participants
 *   never load this page — they are served flow.html, which mounts the same view for one trial at a time and
 *   identifies the scenario by their link token instead.
 *
 * Inputs:
 *   Query params ?s=<stimulus folder>&c=low|high. w=<tab>[&i=<id>] opens the panel on that tab (and keeps the URL
 *   in sync with it).
 *
 * Outputs:
 *   The mounted scenario view inside #scenario. Nothing is logged: interaction.js sends events only for a
 *   participant link (flow.js), so a researcher preview leaves no record.
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

  ScenarioView.mount(document.getElementById("scenario"), {
    query: "s=" + encodeURIComponent(stimulus) + "&c=" + encodeURIComponent(condition),
    toggle: document.getElementById("ws-toggle"),
    panel: tab ? { tab, item: p.get("i"), syncUrl: true } : {},
  });
})();
