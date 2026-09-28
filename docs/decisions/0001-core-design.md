# 0001 — Core package design

Status: proposed (2026-09-28). Items marked *[owner]* need the owner's confirmation.

## Context

`ponyglot` is the open-source core (BSD-3-Clause) that customer sites install (ponyglot-cloud
ADR 0007): API client, sync, the suggestion table and review admin, and the django-parler and
django-modeltranslation adapters. `djangocms-ponyglot` and `wagtail-ponyglot` build on it. The
contract is the cloud's API v1 (ponyglot-cloud `docs/api.md`); its fingerprint rules are frozen.

## Decision

1. **Versions** *[owner]*: Python ≥ 3.10, Django ≥ 4.2 (LTS; many django CMS sites still run
   it). No runtime dependency besides Django: the HTTP client uses the standard library.
2. **Settings**: one `PONYGLOT` dict. Only `API_KEY` is required; the source language defaults
   to `LANGUAGE_CODE`, target languages to `LANGUAGES`. System checks report problems.
3. **Sync without HTTP in requests.** Saving an object only marks its `SyncState` row dirty,
   in the same database transaction. `manage.py ponyglot sync` (cron, or `--loop 60` as a
   worker) pushes dirty units, reports review outcomes, pulls results and acknowledges them.
   A slow or unreachable cloud never slows down or breaks the site.
4. **Snapshots are skipped when unchanged** (hash of what would be sent). Existing translations
   are sent only on a unit's **first** push (the cloud imports them as approved, ADR 0006 there);
   later they could re-import outdated text after a rejection.
5. **Adapters** register with the core and own an `external_key` prefix (`parler:`,
   `modeltranslation:`, later `djangocms:`, `wagtail:`). The core pulls `/results` once per
   site and hands each result to its adapter. Results of adapters that aren't installed stay
   unacknowledged (another deployment of the same site may handle them) and are reported by
   `ponyglot sync`.
6. **Model adapters (parler, modeltranslation)** discover models automatically (every model the
   library knows) with `EXCLUDE_MODELS` / `EXCLUDE_FIELDS`. Text fields only; slugs, URLs,
   emails and fields with choices are skipped (translating a slug changes URLs) *[owner]*.
   Unit key `<adapter>:<app_label>.<model_name>:<pk>`, segment key = field name.
   Format: `html` for rich-text field classes (djangocms-text, CKEditor) and text fields whose
   value is markup; otherwise `plain`. `FIELD_FORMATS` overrides it.
7. **Suggestions**: results for model adapters become `Suggestion` rows (one pending per object
   × field × language; a newer result supersedes it). Nothing is written to the model until an
   editor applies it in the admin (optionally editing the text), which keeps the promise
   "Nothing is published automatically". Applying is refused when the source changed since
   the translation. Decisions are reported to the cloud (`approved` with the final text,
   `rejected`) by the next sync.
8. **Deleted objects** send `DELETE /units/{key}`; pending suggestions go with them.
9. **Bring your own key** (translate with the site's own DeepL/LLM key, without the cloud; the
   free tier promised on ponyglot.app) is a later backend behind the same sync, not part of
   the first release *[owner]*.

## Consequences

- Sites need a periodic `ponyglot sync` (cron or a worker process); the README says so.
- Language codes are passed as Django uses them; the cloud's site languages must match.
- Fingerprints are computed locally only for change detection and the stale check before
  applying; the cloud's value stays authoritative.
