# 0002 — Excluding content from translation; QA errors and editor notifications

Status: accepted and implemented (2026-09-28). Context: djangocms-ponyglot ADR 0001 and
ponyglot-cloud ADR 0014.

## Decision

1. **Exclusions** (owner decision D): any synced object (a django CMS page, a parler blog post,
   a modeltranslation product) can be excluded from translation, for all languages or some.
   - Stored in the core (`Exclusion`: adapter, external key, languages or "all"), so every
     adapter gets it for free.
   - Excluded for all languages → the unit is deleted in the cloud and no longer pushed.
     Excluded for some → those languages are left out of the unit's `writable_languages`.
   - Set from the admin (action "Exclude from translation") and, in djangocms-ponyglot, from the
     toolbar. Removing an exclusion marks the object dirty for the next sync.
   - `EXCLUDE_MODELS` / `EXCLUDE_FIELDS` remain for whole models and fields.
2. **QA errors on suggestions** ("Editors fix" mode): error-flagged suggestions show the issues
   in the admin; applying one unchanged needs confirmation ("Apply despite QA errors"), edited
   text is applied (the cloud checks approved text again). Bulk apply skips them.
3. **Editor notifications**: `PONYGLOT["NOTIFY_EDITORS"]` (default off; `True`: users who may
   change suggestions, or a list of addresses) sends one "translations ready for review" email
   per sync through the site's own email setup. Choosing recipients per content (e.g. a page's
   last editor) is left to djangocms-ponyglot.
4. Capabilities in the handshake: `qa_errors` when every installed adapter shows QA errors (the
   model adapters do, through the suggestion admin); `whole_unit` when any adapter needs it
   (djangocms-ponyglot will).
5. Translations the cloud holds back for QA review are mirrored locally (`HeldBack`, replaced by
   every sync from `GET /attention`) for editors' UIs; the admin lists them with the reason.
6. Exclusions and held-back items are managed from the admin's sync-state list (actions) and
   exclusion list; connectors call `ponyglot.exclusions.exclude` / `include`.
