/*
 * Purpose:
 *   A questionnaire screen (the one before the practice, and the one at the end of the session), as one form,
 *   under the title the server sends with it. Three kinds of item: a rating scale (radio-button stops,
 *   none selected until the participant picks one, optionally with an answer off the scale, such as "Did not
 *   consult", drawn below it), a single choice from a list, and free text. The form only
 *   collects answers and says which required ones are missing; the server checks them again and stores them.
 *
 * Inputs:
 *   QuestionnaireForm.render(container, spec): `spec` is the stage's questionnaire as sent by the server:
 *   {title, intro, placeholder, sha256, items: [{id, type: "scale"|"choice"|"text", text, required, min?, max?, anchors?,
 *   not_applicable?: {id, label}, options?, max_chars?}, ...]} in the order to ask them.
 *
 * Outputs:
 *   The form drawn into `container`. render() returns {collect, showErrors, showMessage}:
 *     collect()          {answers: {sha256, items: {id: value}}, errors: [{id, message}]}; a scale value is a
 *                        number (or the not_applicable id when that was picked), a choice its option id, text a
 *                        string; unanswered items are left out.
 *     showErrors(errors) marks those items and focuses the first.
 *     showMessage(text)  a message for the whole form.
 *
 * Dependencies:
 *   None. Loaded by flow.html before flow.js, which mounts it on each questionnaire stage. Uses the classes of the
 *   form in assessment.js (style.css), so both look the same.
 */
(function (root) {
  "use strict";

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function radio(name, value, id) {
    const input = el("input");
    input.type = "radio";
    input.name = name;
    input.value = value;
    input.id = id;
    return input;
  }

  const BUILDERS = {
    scale(set, id, item) {
      const scale = el("div", "scale");
      scale.appendChild(el("div", "scale-track"));
      for (let v = item.min; v <= item.max; v++) {
        const stop = el("label", "scale-stop");
        stop.append(radio(id, String(v), id + "-" + v), el("span", "scale-dot"), el("span", "scale-num", String(v)));
        const anchor = item.anchors && item.anchors[String(v)];
        stop.appendChild(el("span", "scale-anchor", anchor || ""));
        if (!anchor) stop.lastChild.setAttribute("aria-hidden", "true");
        scale.appendChild(stop);
      }
      set.appendChild(scale);
      if (item.not_applicable) {
        // Same radio group as the stops, so picking it clears the scale and the other way round.
        const na = item.not_applicable;
        const list = el("div", "q-choices q-inline q-scale-na");
        const wrap = el("label", "q-choice");
        wrap.append(radio(id, na.id, id + "-" + na.id), el("span", null, na.label));
        list.appendChild(wrap);
        set.appendChild(list);
      }
    },
    choice(set, id, item) {
      const list = el("div", "q-choices");
      item.options.forEach((o) => {
        const wrap = el("label", "q-choice");
        wrap.append(radio(id, o.id, id + "-" + o.id), el("span", null, o.label));
        list.appendChild(wrap);
      });
      set.appendChild(list);
    },
    text(set, id, item) {
      const box = el("textarea", "q-text-input");
      box.id = id + "-text";
      box.name = id;
      box.rows = 4;
      box.maxLength = item.max_chars;
      set.querySelector("legend").id = id + "-legend";
      box.setAttribute("aria-labelledby", id + "-legend");
      set.appendChild(box);
    },
  };

  function render(container, spec) {
    const form = el("form", "assessment stage-card");
    form.noValidate = true;
    form.addEventListener("submit", (e) => e.preventDefault());
    form.appendChild(el("h1", null, spec.title));
    if (spec.placeholder) {
      const box = el("div", "placeholder");
      box.appendChild(el("strong", "placeholder-tag", "Placeholder"));
      box.appendChild(document.createTextNode(" These questions will be replaced."));
      form.appendChild(box);
    }
    if (spec.intro) form.appendChild(el("p", "assessment-intro", spec.intro));
    const message = el("p", "assessment-message");
    message.hidden = true;
    const sets = {};
    spec.items.forEach((item) => {
      const id = "questionnaire-" + item.id;
      const set = el("fieldset", "q q-item-" + item.type);
      set.appendChild(el("legend", "q-text", item.text + (item.required ? "" : " (optional)")));
      BUILDERS[item.type](set, id, item);
      const error = el("p", "q-error");
      error.hidden = true;
      set.appendChild(error);
      sets[item.id] = set;
      form.appendChild(set);
    });
    form.appendChild(message);
    container.appendChild(form);

    function value(item) {
      const set = sets[item.id];
      if (item.type === "text") return set.querySelector("textarea").value.trim();
      const picked = set.querySelector("input:checked");
      if (!picked) return "";
      if (item.type !== "scale" || (item.not_applicable && picked.value === item.not_applicable.id)) return picked.value;
      return Number(picked.value);
    }

    function collect() {
      const items = {}, errors = [];
      spec.items.forEach((item) => {
        const v = value(item);
        if (v === "") {
          if (item.required) errors.push({ id: item.id, message: "Please answer this question." });
        } else {
          items[item.id] = v;
        }
      });
      return { answers: { sha256: spec.sha256, items: items }, errors: errors };
    }

    function showErrors(errors) {
      Object.values(sets).forEach((set) => {
        set.classList.remove("q-invalid");
        set.querySelector(".q-error").hidden = true;
      });
      errors.forEach((e) => {
        const set = sets[e.id];
        set.classList.add("q-invalid");
        const line = set.querySelector(".q-error");
        line.textContent = e.message;
        line.hidden = false;
      });
      if (errors.length) {
        const first = sets[errors[0].id].querySelector("input, textarea");
        if (first) first.focus();
      }
    }

    function showMessage(text) {
      message.textContent = text;
      message.hidden = !text;
    }

    return { collect, showErrors, showMessage };
  }

  root.QuestionnaireForm = { render };
})(window);
