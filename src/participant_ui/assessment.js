/*
 * Purpose:
 *   The questions a participant answers after each conversation, as one reusable form: the practice questions and
 *   every trial's assessment are drawn by this same component, so the wording, order and layout are identical
 *   everywhere; only the heading differs. The four questions, in order:
 *     1. whether the assistant performed the requested task safely (Yes / No);
 *     2. confidence, on a five-stop slider whose stops are radio buttons, so nothing is selected until the
 *        participant picks a stop: a rating is never recorded by default;
 *     3. the reasoning (free text);
 *     4. the information that influenced the answer (checkboxes; only the options the server sends). An option
 *        marked "specify" (Other) shows a text field while it is ticked; the text is required only when the
 *        option is also marked "specify_required".
 *   The form only collects and checks answers for the participant's benefit; the server checks them again and is
 *   the one that stores them.
 *
 * Inputs:
 *   AssessmentForm.render(container, spec, heading): `spec` is the stage's assessment as sent by the server:
 *   {intro, sha256, questions: [{id, text, required, options?, min?, max?, anchors?, max_chars?}, ...]} in the
 *   order to ask them (participant_flow.assessment_for builds it from src/study_materials/assessment.json).
 *
 * Outputs:
 *   The form drawn into `container`. render() returns {collect, showErrors, showMessage}:
 *     collect()          {answers, errors}: answers as POST /api/stage/complete expects them
 *                        ({performed_safely, confidence, reasoning, sources, sources_other, sha256}) and the
 *                        list of questions
 *                        still unanswered or invalid ([{id, message}]).
 *     showErrors(errors) marks those questions, describes the problem next to each, and focuses the first.
 *     showMessage(text)  a message for the whole form (for example, the server refused the answers).
 *
 * Dependencies:
 *   None. Loaded by flow.html before flow.js, which mounts it on every assessment stage.
 */
(function (root) {
  "use strict";

  let formCount = 0; // distinct ids if a page ever holds more than one form

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  const MESSAGES = {
    performed_safely: "Please choose Yes or No.",
    confidence: "Please choose a rating on the scale.",
    reasoning: "Please explain your reasoning.",
    sources: "Please select at least one.",
    sources_other: "Please specify the other information.",
  };

  /** One question as a fieldset with its legend and an error line; `build(fieldset, prefix)` adds the inputs. */
  function question(q, prefix, build) {
    const set = el("fieldset", "q q-" + q.id);
    set.dataset.question = q.id;
    const legend = el("legend", "q-text", q.text);
    set.appendChild(legend);
    build(set, prefix + "-" + q.id);
    const error = el("p", "q-error");
    error.id = prefix + "-" + q.id + "-error";
    error.hidden = true;
    set.appendChild(error);
    return set;
  }

  function choice(type, name, value, label, id) {
    const wrap = el("label", "q-choice");
    const input = el("input");
    input.type = type;
    input.name = name;
    input.value = value;
    input.id = id;
    wrap.append(input, el("span", null, label));
    return wrap;
  }

  const BUILDERS = {
    performed_safely(set, id, q) {
      const row = el("div", "q-choices q-inline");
      q.options.forEach((o) => row.appendChild(choice("radio", id, o.id, o.label, id + "-" + o.id)));
      set.appendChild(row);
    },
    // A slider of discrete stops. Each stop is a radio button (keyboard: arrow keys; screen readers announce the
    // number and its anchor), and no stop starts selected.
    confidence(set, id, q) {
      const scale = el("div", "scale");
      scale.appendChild(el("div", "scale-track"));
      for (let v = q.min; v <= q.max; v++) {
        const stop = el("label", "scale-stop");
        const input = el("input");
        input.type = "radio";
        input.name = id;
        input.value = String(v);
        input.id = id + "-" + v;
        stop.append(input, el("span", "scale-dot"), el("span", "scale-num", String(v)));
        const anchor = q.anchors && q.anchors[String(v)];
        stop.appendChild(el("span", "scale-anchor", anchor || ""));
        if (!anchor) stop.lastChild.setAttribute("aria-hidden", "true");
        scale.appendChild(stop);
      }
      set.appendChild(scale);
    },
    reasoning(set, id, q) {
      const box = el("textarea", "q-text-input");
      box.id = id + "-text";
      box.name = id;
      box.rows = 5;
      box.maxLength = q.max_chars;
      set.querySelector("legend").id = id + "-legend";
      box.setAttribute("aria-labelledby", id + "-legend");
      set.appendChild(box);
    },
    sources(set, id, q) {
      const list = el("div", "q-choices");
      q.options.forEach((o) => {
        const item = choice("checkbox", id, o.id, o.label, id + "-" + o.id);
        list.appendChild(item);
        if (o.specify) list.appendChild(specifyField(item.querySelector("input"), id + "-" + o.id, o.max_chars));
      });
      set.appendChild(list);
    },
  };

  /** The text field of an option that asks the participant to specify, shown while `box` is ticked. Once it has
   *  appeared, unticking keeps its line (invisible) rather than removing it, so the page cannot shorten and jump
   *  under the participant's pointer. The text is kept if the box is ticked again, but sent only while ticked. */
  function specifyField(box, id, maxChars) {
    const wrap = el("div", "q-specify");
    wrap.hidden = true;
    const input = el("input", "q-specify-input");
    input.type = "text";
    input.id = id + "-text";
    input.name = id + "-text";
    input.maxLength = maxChars;
    input.placeholder = "Description (optional)";
    input.setAttribute("aria-label", "Description of the other information (optional)");
    wrap.appendChild(input);
    box.addEventListener("change", () => {
      if (box.checked) {
        wrap.hidden = false;
        wrap.style.visibility = "";
        input.disabled = false;
        input.focus();
      } else {
        wrap.style.visibility = "hidden";
        input.disabled = true;
      }
    });
    return wrap;
  }

  function render(container, spec, heading) {
    const prefix = "assess" + ++formCount;
    const form = el("form", "stage-card assessment");
    form.noValidate = true;
    form.addEventListener("submit", (e) => e.preventDefault()); // submitted by the page's Continue button
    form.appendChild(el("h1", null, heading));
    if (spec.intro) form.appendChild(el("p", "assessment-intro", spec.intro));
    const sets = {};
    spec.questions.forEach((q) => {
      const set = question(q, prefix, (fs, id) => BUILDERS[q.id](fs, id, q));
      sets[q.id] = set;
      form.appendChild(set);
    });
    const message = el("p", "assessment-message");
    message.setAttribute("role", "alert");
    message.hidden = true;
    form.appendChild(message);
    container.appendChild(form);

    const byId = Object.fromEntries(spec.questions.map((q) => [q.id, q]));
    const picked = (id, type) => [...sets[id].querySelectorAll("input[type=" + type + "]:checked")].map((i) => i.value);

    // Clear a question's error as soon as it is answered.
    form.addEventListener("input", (e) => {
      const set = e.target.closest("fieldset");
      if (set) setError(set, null);
    });

    /** Show or clear one question's error. A cleared error keeps its line (invisible) once it has been shown:
     *  removing it would shorten the page, and a page scrolled to the bottom would then jump under the
     *  participant's pointer, so the next click could land on a different option than the one aimed at. */
    function setError(set, text) {
      const error = set.querySelector(".q-error");
      if (text) {
        error.textContent = text;
        error.hidden = false;
      }
      error.style.visibility = text ? "" : "hidden"; // hidden from screen readers as well
      set.classList.toggle("q-invalid", !!text);
      set.querySelectorAll("input, textarea").forEach((input) => {
        if (text) {
          input.setAttribute("aria-invalid", "true");
          input.setAttribute("aria-describedby", error.id);
        } else {
          input.removeAttribute("aria-invalid");
          input.removeAttribute("aria-describedby");
        }
      });
    }

    function collect() {
      const safely = picked("performed_safely", "radio")[0];
      const rating = picked("confidence", "radio")[0];
      const reasoning = sets.reasoning.querySelector("textarea").value.trim();
      const sources = picked("sources", "checkbox");
      // The text of a ticked "specify" option (Other); nothing is sent for it while it is unticked.
      const specifyBox = sets.sources.querySelector(".q-specify-input:not([disabled])");
      const other = specifyBox && !specifyBox.closest(".q-specify").hidden ? specifyBox.value.trim() : "";
      const otherOption = byId.sources.options.find((o) => o.specify && sources.includes(o.id));
      const otherTicked = !!otherOption;
      const errors = [];
      if (!safely && byId.performed_safely.required) errors.push({ id: "performed_safely" });
      if (!rating) errors.push({ id: "confidence" }); // a rating is never recorded by default
      if (!reasoning && byId.reasoning.required) errors.push({ id: "reasoning" });
      if (!sources.length && byId.sources.required) errors.push({ id: "sources" });
      else if (otherTicked && !other && otherOption.specify_required) {
        errors.push({ id: "sources", message: MESSAGES.sources_other });
      }
      errors.forEach((e) => { e.message = e.message || MESSAGES[e.id]; });
      return {
        answers: { performed_safely: safely, confidence: rating ? Number(rating) : null, reasoning: reasoning,
                   sources: sources, sources_other: otherTicked ? other : "", sha256: spec.sha256 },
        errors: errors,
      };
    }

    function showErrors(errors) {
      Object.values(sets).forEach((set) => setError(set, null));
      errors.forEach((e) => setError(sets[e.id], e.message));
      if (errors.length) {
        const first = sets[errors[0].id];
        first.scrollIntoView({ block: "center" });
        first.querySelector("input, textarea").focus({ preventScroll: true });
      }
    }

    function showMessage(text) {
      message.textContent = text || "";
      message.hidden = !text;
    }

    return { collect: collect, showErrors: showErrors, showMessage: showMessage };
  }

  root.AssessmentForm = { render: render };
})(window);
