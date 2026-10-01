/* Ponyglot suggestions on a change form: each pending translation is shown below its field.
 * "Apply" puts it into the field (and a title's slug into the slug field), "Reject" marks it;
 * nothing is stored until the form is saved, which records the decisions (hidden inputs
 * `_ponyglot_apply` / `_ponyglot_reject`). */
(function () {
  "use strict";

  const script = document.getElementById("ponyglot-suggestions");
  if (!script) {
    return;
  }
  const data = JSON.parse(script.textContent);
  const labels = data.labels;
  const entries = [];

  function field(name) {
    return document.querySelector(`#content-main form [name="${CSS.escape(name)}"]`);
  }

  function setValue(input, value) {
    input.value = value;
    const editor = window.CKEDITOR && input.id && window.CKEDITOR.instances[input.id];
    if (editor) {
      editor.setData(value);
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function plainText(suggestion) {
    if (suggestion.format !== "html") {
      return suggestion.text;
    }
    // Parsed inertly (no scripts, no requests); shown as text.
    return new DOMParser().parseFromString(suggestion.text, "text/html").body.textContent;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function button(text, onClick, primary) {
    const node = element("button", primary ? "button default" : "button", text);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  class Entry {
    constructor(suggestion, input) {
      this.suggestion = suggestion;
      this.input = input;
      this.form = input.form;
      this.state = "open";
      this.previous = null;
      this.marker = null;
      this.box = element("div", "ponyglot-suggestion");
      this.box.lang = suggestion.language;
      const host = input.closest(".fieldBox") || input.closest(".flex-container");
      if (host && host.classList.contains("fieldBox")) {
        host.append(this.box);
      } else if (host) {
        host.after(this.box);
      } else {
        input.after(this.box);
      }
      this.render();
    }

    render() {
      const suggestion = this.suggestion;
      this.box.replaceChildren();
      this.box.dataset.state = this.state;
      const head = element("div", "ponyglot-suggestion-head", labels.suggestion);
      this.box.append(head);
      if (this.state === "open") {
        this.box.append(element("div", "ponyglot-suggestion-text", plainText(suggestion)));
        for (const issue of suggestion.qa) {
          const error = issue.severity === "error";
          const line = element("div", error ? "ponyglot-qa-error" : "ponyglot-qa-warning");
          line.append(element("strong", "", (error ? labels.error : labels.warning) + " "));
          line.append(document.createTextNode(issue.message));
          this.box.append(line);
        }
        const actions = element("div", "ponyglot-suggestion-actions");
        actions.append(
          button(labels.apply, () => this.apply(true), true),
          button(labels.reject, () => this.reject())
        );
        this.box.append(actions);
      } else {
        const done = element("div", "ponyglot-suggestion-actions");
        done.append(
          element("span", "", this.state === "applied" ? labels.applied : labels.rejected),
          button(labels.undo, () => this.undo())
        );
        this.box.append(done);
      }
    }

    mark(name) {
      this.marker = element("input");
      this.marker.type = "hidden";
      this.marker.name = name;
      this.marker.value = this.suggestion.id;
      this.form.append(this.marker);
    }

    apply(ask) {
      if (this.state !== "open") {
        return;
      }
      if (ask && this.suggestion.has_errors && !window.confirm(labels.confirm_errors)) {
        return;
      }
      this.previous = { [this.suggestion.input]: this.input.value };
      setValue(this.input, this.suggestion.text);
      for (const [name, slug] of Object.entries(this.suggestion.slugs)) {
        const slugInput = field(name);
        if (slugInput) {
          this.previous[name] = slugInput.value;
          setValue(slugInput, slug);
        }
      }
      this.mark("_ponyglot_apply");
      this.state = "applied";
      this.render();
      updateSummaries();
    }

    reject() {
      this.mark("_ponyglot_reject");
      this.state = "rejected";
      this.render();
      updateSummaries();
    }

    undo() {
      if (this.state === "applied" && this.previous) {
        for (const [name, value] of Object.entries(this.previous)) {
          const input = field(name);
          if (input) {
            setValue(input, value);
          }
        }
      }
      this.marker?.remove();
      this.marker = null;
      this.previous = null;
      this.state = "open";
      this.render();
      updateSummaries();
    }
  }

  for (const suggestion of data.suggestions) {
    const input = field(suggestion.input);
    if (input) {
      entries.push(new Entry(suggestion, input));
    }
  }
  if (!entries.length) {
    return;
  }

  // One line per language on top of the form: how many wait, and "Apply all" (QA errors are
  // left for a decision one by one).
  const languages = new Map();
  for (const entry of entries) {
    const code = entry.suggestion.language;
    if (!languages.has(code)) {
      languages.set(code, { name: entry.suggestion.language_name, entries: [] });
    }
    languages.get(code).entries.push(entry);
  }
  const summary = element("div", "ponyglot-summary");
  for (const language of languages.values()) {
    language.line = element("div", "ponyglot-summary-line");
    summary.append(language.line);
  }
  const firstFieldset = entries[0].form.querySelector("fieldset");
  (firstFieldset || entries[0].box).before(summary);

  function updateSummaries() {
    for (const language of languages.values()) {
      const open = language.entries.filter((entry) => entry.state === "open");
      language.line.replaceChildren();
      language.line.hidden = !open.length;
      language.line.append(
        element(
          "span",
          "",
          labels.summary.replace("%(language)s", language.name).replace("%(n)s", open.length)
        )
      );
      if (open.some((entry) => !entry.suggestion.has_errors)) {
        language.line.append(
          button(
            labels.apply_all,
            () => {
              for (const entry of open) {
                if (!entry.suggestion.has_errors) {
                  entry.apply(false);
                }
              }
            },
            true
          )
        );
      }
    }
    summary.hidden = !entries.some((entry) => entry.state === "open");
  }

  // A form shown again (validation errors) keeps the decisions made before: the field values
  // came back with it; only the markers need restoring.
  for (const entry of entries) {
    const state = data.applied.includes(entry.suggestion.id)
      ? "applied"
      : data.rejected.includes(entry.suggestion.id) && "rejected";
    if (state) {
      entry.mark(state === "applied" ? "_ponyglot_apply" : "_ponyglot_reject");
      entry.state = state;
      entry.render();
    }
  }
  updateSummaries();
})();
