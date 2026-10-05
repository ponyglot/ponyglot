# Changelog

## Unreleased (0.1.0)

First release: the Django library that connects a site to Ponyglot. Requires Django 5.2+.

### Sync

- Saving content only marks it as changed; `manage.py ponyglot sync` sends changes, reports
  editors' decisions and fetches finished translations (`--loop 60` as a worker).
- `ponyglot backfill` sends existing content once; existing translations are imported, so
  nothing is translated twice.
- `ponyglot check`, `status` and `translate` (`--estimate`, `--wait`).
- Sync rounds are recorded (Ponyglot › Sync runs, the last 100), also when editors fetch the
  translations of one object or model.
- System checks: missing API key, source language, `API_URL` not ending in `/v1`
  (`ponyglot.W003`). Answers that aren't from the API give a clear error instead of a crash.

### Models (parler, modeltranslation)

- Every registered model's text fields are synced (not slugs, URLs, emails or choices).
- Translations arrive as suggestions (Ponyglot › Translation suggestions): source, current
  translation and a word diff; edit, apply or reject, also in bulk. Suggestions whose source
  changed can't be applied and are translated again.
- Translated slugs follow the applied title unless written by hand (`SLUG_SOURCES`).
- QA errors shown on suggestions (applying them needs an edit or *Apply despite QA errors*);
  translations held back for a separate QA review are listed (Ponyglot › Translations waiting
  for QA review).
- `PonyglotAdminMixin` for model admins (parler's `TranslatableAdmin`, modeltranslation's
  `TranslationAdmin` and `TabbedTranslationAdmin`):
  - a *Ponyglot translations* button on the change page (this object) and the change list
    (all objects of the model): per language what is missing, waiting or held by QA, with
    Translate, Apply and Review;
  - waiting translations shown below their fields on the change form: Apply puts one into
    the field, saving the form records the decision.
- Ponyglot › Translation dashboard (permission *Can use the translation dashboard*): the whole
  site per language, sync history, content types with what waits; translate everything
  missing, sync now.
- The translation dashboard reads top to bottom: languages, the link to the documents that
  need attention, the content types, and jobs and sync runs folded away.
- Translation dashboard › *Which documents need attention*: every document with translations
  missing, outdated, waiting to be applied, in a draft or held by QA, per language, filtered
  by content type and language. Each links to where it's handled (its dialog, or a
  connector's status page); select documents to translate them or apply their waiting
  translations. Page-like content comes first (django CMS, then Wagtail;
  `Adapter.overview_order`), then the models. Changes not sent yet and failed sends are counted. Needs the cloud's
  `GET /units`.

### Control

- Exclusions: keep objects (or some of their languages) out of translation from the admin;
  whole models and fields with `EXCLUDE_MODELS` / `EXCLUDE_FIELDS`.
- Emails to editors when new translations arrive (`NOTIFY_EDITORS`, `SITE_URL`).

### For connector authors

- Adapter API for other content systems (`Adapter`, `ModelAdapter`, drafts with
  `status_after_write`, segment structure with `parent_key` and `position`).
- `Adapter.content_kinds()` (filters in the documents overview) and
  `Adapter.translation_url(key)` (where editors handle an object's translations).
- `ponyglot.testing.FakeCloud`: an in-memory cloud for tests.
