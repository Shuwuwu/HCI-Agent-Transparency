/*
 * Purpose:
 *   Read-only workspace panel of the participant chatbot with four equal tabs: Mail, Calendar, Drive and Skills
 *   (the agent's skill documentation, see skills.js). Mail, Calendar and Drive show the workspace as it was
 *   BEFORE the agent ran (the server never sends the state after), each with a list and a readable preview.
 *   Mail is the default tab. Identical in the low and the high transparency condition (it never reads the
 *   condition or the action log). Logs which tabs and items are opened and for how long (interaction.js); the
 *   Skills tab is logged with its own event types, separate from the workspace tabs.
 *
 * Inputs:
 *   JSON from /api/workspace?s=<stimulus folder>: {account, state}, or null when no snapshot exists for this
 *   conversation (then Mail/Calendar/Drive show a notice and Skills still works). Options from the caller
 *   (researcher preview only, all optional; app.js takes them from the URL parameters w= and i=):
 *   {tab: "mail"|"calendar"|"drive"|"skills", item: <item id>, syncUrl: true to keep w=/i= in the URL in sync}.
 *   The panel never reads the URL itself, so it can be mounted by any page.
 *
 * Outputs:
 *   DOM inside #workspace. Every value is inserted with textContent, never as HTML.
 *
 * Dependencies:
 *   previews.js (WorkspacePreviews), skills.js (SkillsView), interaction.js. Started by scenario_view.js through
 *   WorkspaceSidebar.init().
 */
(function (root) {
  "use strict";
  const P = root.WorkspacePreviews;
  const I = root.Interaction;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  const names = (list) => list.map(P.displayName).join(", ");

  // ---- Mail -------------------------------------------------------------------------------------------------
  function mailList(state, ui) {
    const box = ui.folder; // "received" | "sent"
    const bar = el("div", "chips");
    [["received", "Inbox"], ["sent", "Sent"]].forEach(([key, label]) => {
      const n = state.emails.filter((e) => e.status === key).length;
      const b = el("button", "chip" + (box === key ? " on" : ""), label + " (" + n + ")");
      b.onclick = () => ui.set({ folder: key, item: null });
      bar.appendChild(b);
    });
    const list = el("ul", "items");
    state.emails.filter((e) => e.status === box)
      .sort((a, b) => b.timestamp.localeCompare(a.timestamp) || (+b.id_ - +a.id_))
      .forEach((e) => {
        const who = box === "sent" ? "To: " + names(e.recipients) : P.displayName(e.sender);
        const li = el("li", "item");
        const row = el("button", "item-btn");
        row.appendChild(el("div", "item-top", who));
        row.appendChild(el("div", "item-title", e.subject));
        row.appendChild(el("div", "item-sub", P.dateLabel(e.timestamp) + " · " + e.body.replace(/\s+/g, " ").slice(0, 70)));
        row.onclick = () => ui.set({ item: e.id_, folder: e.status });
        li.appendChild(row);
        list.appendChild(li);
      });
    return [bar, list];
  }

  function field(label, value) {
    const row = el("div", "meta-row");
    row.appendChild(el("span", "meta-k", label));
    row.appendChild(el("span", "meta-v", value));
    return row;
  }

  function mailDetail(e) {
    const d = el("article", "detail");
    d.appendChild(el("h2", "detail-title", e.subject));
    const meta = el("div", "meta");
    meta.appendChild(field("From", e.sender));
    meta.appendChild(field("To", e.recipients.join(", ")));
    if (e.cc.length) meta.appendChild(field("Cc", e.cc.join(", ")));
    if (e.bcc.length) meta.appendChild(field("Bcc", e.bcc.join(", ")));
    meta.appendChild(field("Date", P.dateTimeLabel(e.timestamp)));
    if (e.status === "draft") meta.appendChild(field("Status", "Draft"));
    d.appendChild(meta);
    d.appendChild(el("div", "mail-body", e.body));
    if (e.attachments.length) {
      d.appendChild(el("div", "attachments", "Attachments: " + e.attachments.map((a) =>
        typeof a === "object" ? "calendar event: " + a.title : "drive file ID " + a).join(", ")));
    }
    return d;
  }

  // ---- Calendar ---------------------------------------------------------------------------------------------
  function eventTime(e) {
    return e.all_day ? "All day" : P.timeLabel(e.start_time) + "–" + P.timeLabel(e.end_time);
  }

  function calendarList(state, ui, today) {
    const wrap = el("div", "agenda");
    P.groupEventsByDay(state.events).forEach((g) => {
      const h = el("h3", "day" + (g.day === today ? " today" : ""), P.dateLabel(g.day + "T00:00:00") + (g.day === today ? " · today" : ""));
      wrap.appendChild(h);
      const list = el("ul", "items");
      g.events.forEach((e) => {
        const li = el("li", "item");
        const row = el("button", "item-btn" + (e.status === "canceled" ? " canceled" : ""));
        row.appendChild(el("div", "item-top", eventTime(e) + (e.status === "canceled" ? " · canceled" : "")));
        row.appendChild(el("div", "item-title", e.title));
        if (e.location) row.appendChild(el("div", "item-sub", e.location));
        row.onclick = () => ui.set({ item: e.id_ });
        li.appendChild(row);
        list.appendChild(li);
      });
      wrap.appendChild(list);
    });
    return [wrap];
  }

  function eventDetail(e) {
    const d = el("article", "detail");
    d.appendChild(el("h2", "detail-title", e.title));
    const meta = el("div", "meta");
    meta.appendChild(field("When", P.dateLabel(e.start_time) + (e.all_day ? ", all day" : ", " + eventTime(e))));
    if (e.status === "canceled") meta.appendChild(field("Status", "Canceled"));
    if (e.location) meta.appendChild(field("Where", e.location));
    meta.appendChild(field("Participants", e.participants.join(", ")));
    d.appendChild(meta);
    d.appendChild(el("div", "mail-body", e.description));
    return d;
  }

  // ---- Drive ------------------------------------------------------------------------------------------------
  const ICONS = { document: "📄", spreadsheet: "📊", presentation: "📽️", text: "📃" };
  const KIND_LABEL = { document: "Document", spreadsheet: "Spreadsheet", presentation: "Presentation", text: "Text file" };

  function driveList(state, ui) {
    const list = el("ul", "items");
    state.files.slice().sort((a, b) => a.filename.localeCompare(b.filename)).forEach((f) => {
      const kind = P.fileKind(f.filename);
      const li = el("li", "item");
      const row = el("button", "item-btn file-row");
      row.appendChild(el("span", "file-icon", ICONS[kind]));
      const text = el("span", "file-text");
      text.appendChild(el("div", "item-title", f.filename));
      text.appendChild(el("div", "item-sub", "ID " + f.id_ + " · " + P.dateLabel(f.last_modified) + " · " + f.size + " characters"));
      row.appendChild(text);
      row.onclick = () => ui.set({ item: f.id_ });
      li.appendChild(row);
      list.appendChild(li);
    });
    return [list];
  }

  function filePreview(f) {
    const kind = P.fileKind(f.filename);
    const d = el("article", "detail");
    d.appendChild(el("h2", "detail-title", ICONS[kind] + " " + f.filename));
    const meta = el("div", "meta");
    meta.appendChild(field("Type", KIND_LABEL[kind]));
    meta.appendChild(field("File ID", f.id_));
    meta.appendChild(field("Owner", f.owner));
    meta.appendChild(field("Last modified", P.dateTimeLabel(f.last_modified)));
    const shared = Object.entries(f.shared_with);
    if (shared.length) meta.appendChild(field("Shared with", shared.map(([a, p]) => a + " (" + p + ")").join(", ")));
    d.appendChild(meta);
    if (kind === "spreadsheet") {
      const t = P.parseTable(f.content);
      const wrap = el("div", "sheet-wrap");
      const table = el("table", "sheet");
      const head = el("tr");
      head.appendChild(el("th", "rownum", ""));
      t.header.forEach((h) => head.appendChild(el("th", null, h)));
      table.appendChild(head);
      t.rows.forEach((r, i) => {
        const tr = el("tr");
        tr.appendChild(el("td", "rownum", String(i + 1)));
        r.forEach((c) => tr.appendChild(el("td", null, c)));
        table.appendChild(tr);
      });
      wrap.appendChild(table);
      d.appendChild(wrap);
    } else if (kind === "document") {
      const page = el("div", "doc-page");
      P.paragraphs(f.content).forEach((p, i) => {
        const para = el("p");
        const cut = p.indexOf("\n");
        if (i === 0 && cut > 0) { // first line of the document is shown as its title; the text is unchanged
          para.appendChild(el("span", "doc-title", p.slice(0, cut)));
          para.appendChild(document.createTextNode(p.slice(cut)));
        } else {
          para.textContent = p;
        }
        page.appendChild(para);
      });
      d.appendChild(page);
    } else {
      d.appendChild(el("div", "mail-body", f.content));
    }
    return d;
  }

  // ---- Sidebar shell ----------------------------------------------------------------------------------------
  const TABS = [["mail", "Mail"], ["calendar", "Calendar"], ["drive", "Drive"], ["skills", "Skills"]];
  const ITEM_KEY = { mail: "emails", calendar: "events", drive: "files" };

  /**
   * Create the sidebar controller. `options` = {tab, item, syncUrl} (all optional; see the header). Returns
   * {opened(), closed()}, called by scenario_view.js when the panel opens/closes.
   */
  function init(container, data, folder, onClose, options) {
    const opts = options || {};
    const state = data ? data.state : null;
    const skillsView = root.SkillsView.create(folder, () => render());
    const ui = {
      tab: TABS.some((t) => t[0] === opts.tab) ? opts.tab : "mail",
      folder: "received", item: opts.item || null,
      set(change) {
        const before = { tab: ui.tab, item: ui.item };
        Object.assign(ui, change);
        if (ui.tab !== before.tab) { endTabView(before.tab); beginTabView(); }
        if (ui.item && ui.item !== before.item) I.log("workspace_item_open", { tab: ui.tab, item_id: String(ui.item) });
        sync(); render();
      },
    };
    let viewing = false;
    // Skills-tab events are separate from the Mail/Calendar/Drive ones.
    function beginTabView() {
      viewing = true;
      if (ui.tab === "skills") I.log("skills_tab_open"); else I.log("workspace_tab_open", { tab: ui.tab });
      I.startTimer("tab");
    }
    function endTabView(tab) {
      if (!viewing) return;
      viewing = false;
      if (tab === "skills") skillsView.leave();
      const duration_ms = I.stopTimer("tab");
      if (tab === "skills") I.log("skills_tab_view", { duration_ms }); else I.log("workspace_tab_view", { tab, duration_ms });
    }
    const today = data ? data.account.current_day : null;
    if (state && ui.tab === "mail" && ui.item) { // open the folder that holds a deep-linked email
      const linked = state.emails.find((e) => e.id_ === ui.item);
      if (linked) ui.folder = linked.status;
    }

    function sync() { // keep a researcher-preview URL shareable: tab and open item
      if (!opts.syncUrl) return;
      const p = new URLSearchParams(location.search);
      p.set("w", ui.tab);
      ui.item ? p.set("i", ui.item) : p.delete("i");
      history.replaceState(null, "", "?" + p.toString());
    }

    function render() {
      container.replaceChildren();
      const head = el("div", "ws-head");
      head.appendChild(el("div", "ws-title", data ? "Workspace · " + data.account.email : "Workspace"));
      const close = el("button", "ws-close", "✕");
      close.setAttribute("aria-label", "Close workspace");
      close.onclick = onClose;
      head.appendChild(close);
      container.appendChild(head);

      const tabs = el("div", "tabs");
      TABS.forEach(([key, label]) => {
        const b = el("button", "tab" + (ui.tab === key ? " on" : ""), label);
        b.onclick = () => ui.set({ tab: key, item: null });
        tabs.appendChild(b);
      });
      container.appendChild(tabs);

      const body = el("div", "ws-body");
      container.appendChild(body);
      if (ui.tab === "skills") { skillsView.render(body); return; }
      if (!state) { body.appendChild(el("p", "missing", "This part of the workspace is not available in the prototype yet.")); return; }
      const items = state[ITEM_KEY[ui.tab]];
      const open = ui.item && items.find((x) => x.id_ === ui.item);
      if (open) {
        const back = el("button", "back", "← Back to list");
        back.onclick = () => ui.set({ item: null });
        body.appendChild(back);
        body.appendChild(ui.tab === "mail" ? mailDetail(open) : ui.tab === "calendar" ? eventDetail(open) : filePreview(open));
      } else {
        if (ui.item) body.appendChild(el("p", "missing", "This item does not exist in the workspace."));
        const parts = ui.tab === "mail" ? mailList(state, ui) : ui.tab === "calendar" ? calendarList(state, ui, today)
          : driveList(state, ui);
        parts.forEach((p) => body.appendChild(p));
      }
      body.scrollTop = 0;
    }

    render();
    return {
      opened() { render(); beginTabView(); },
      closed() { endTabView(ui.tab); },
    };
  }

  root.WorkspaceSidebar = { init };
})(window);
