# ponyglot

Ponyglot core: one translation layer for your Django stack. This package holds the API client,
the sync, the review admin and the **django-parler** and **django-modeltranslation** adapters.
The django CMS and Wagtail connectors (`djangocms-ponyglot`, `wagtail-ponyglot`) build on it.

**Early access, pre-alpha.** APIs may change. See https://ponyglot.app.
Requires Django 5.2 or newer and Python 3.10 or newer.

Ponyglot keeps your content's translations consistent (one glossary and translation memory),
current (only what changed is retranslated) and reviewed: **nothing is published
automatically**. Model translations arrive as suggestions that an editor applies in the admin.

## Install

```sh
pip install "ponyglot[parler]"            # or ponyglot[modeltranslation]
```

```python
INSTALLED_APPS += [
    "ponyglot",
    "ponyglot.contrib.parler",  # or "ponyglot.contrib.modeltranslation"
]
PONYGLOT = {
    "API_KEY": env("PONYGLOT_API_KEY"),  # create one for this site in the dashboard
    "SOURCE_LANGUAGE": "en",  # default: LANGUAGE_CODE
}
```

```sh
python manage.py migrate
python manage.py ponyglot check           # settings and connection
python manage.py ponyglot backfill        # first push of all existing content
```

Every model registered with django-parler or django-modeltranslation is synced: its text
fields (not slugs, URLs, emails or fields with choices). Existing translations are imported on
the first push, so nothing is translated twice.

## Keep in sync

Saving content only marks it as changed; the HTTP work happens in `ponyglot sync`. Run it
periodically, e.g. every few minutes from cron, or as a worker process:

```sh
python manage.py ponyglot sync            # push changes, report reviews, fetch translations
python manage.py ponyglot sync --loop 60  # the same, every 60 seconds (worker process)
```

## Translate and review

```sh
python manage.py ponyglot translate --estimate   # what it would cost
python manage.py ponyglot translate --wait       # translate everything missing or stale
python manage.py ponyglot sync                   # fetch the translations
python manage.py ponyglot status                 # per language: ✓ / stale / review / missing
```

Translations appear in the admin under **Ponyglot › Translation suggestions** with the source
text, the current translation and a diff. Edit if needed, then **Apply** or **Reject** (also as
bulk actions). A suggestion whose source changed in the meantime can't be applied; it is
translated again. The next sync reports your decisions, and approved texts (including your
corrections) feed the translation memory.

## Keep content out of translation

In the admin, **Ponyglot › Sync states** lists everything that is synced. The action **Exclude
from translation** removes objects from Ponyglot (the next sync deletes them there); **Translate
again** undoes it. To exclude only some languages, edit the exclusion under **Exclusions from
translation**. Connectors such as `djangocms-ponyglot` offer the same from their toolbar
(`ponyglot.exclusions.exclude` / `include`). Whole models and fields: `EXCLUDE_MODELS`,
`EXCLUDE_FIELDS`.

## QA errors

Every translation goes through Ponyglot's quality checks (links, placeholders, HTML, length,
glossary). Your plan and site settings decide who fixes errors:

- **Editors fix them** (default on most plans): the suggestion arrives with the error shown. Fix
  the text and apply, or tick *Apply despite QA errors*. Bulk apply skips suggestions with
  errors. The cloud checks applied text again.
- **Separate QA review**: translations with errors wait in the Ponyglot dashboard until a
  reviewer resolves them. **Ponyglot › Translations waiting for QA review** lists them with the
  reason (refreshed by every sync), and the suggestion list says how many are waiting.

## Emails to editors

With `NOTIFY_EDITORS`, every sync that brings new suggestions sends one email ("3 translations
ready for review", per language, with QA errors counted) through your site's email setup:
`True` for everyone who may change suggestions, or a list of addresses. Set `SITE_URL` for an
absolute link to the admin.

## Settings

| Key | Default | |
|---|---|---|
| `API_KEY` | – | required |
| `API_URL` | `https://api.ponyglot.app/v1` | |
| `SOURCE_LANGUAGE` | `LANGUAGE_CODE` | |
| `LANGUAGES` | codes in `LANGUAGES` | languages the site can store |
| `EXCLUDE_MODELS` | `[]` | e.g. `["shop.InternalNote"]` |
| `EXCLUDE_FIELDS` | `[]` | e.g. `["blog.Post.subtitle"]` |
| `FIELD_FORMATS` | detected | e.g. `{"blog.Post.lead": "html"}` |
| `SLUG_SOURCES` | first title field | the field a translated slug follows, e.g. `{"blog.Post.slug": "headline"}` |
| `NOTIFY_EDITORS` | `False` | `True` or a list of addresses, see *Emails to editors* |
| `SITE_URL` | – | e.g. `https://example.com`, for links in emails |
| `TIMEOUT` | `30` | seconds per request |
| `BATCH_SIZE` | `50` | units per push (≤ 100) |

## Translated slugs

Slugs aren't sent for translation. Applying a translated title also sets the target-language
slug (`slugify`, unique per language, within `max_length`):

- an empty slug is filled,
- a slug that was derived from the old title follows the new one,
- a slug written by hand stays.

Your views must find objects by the slug of the active language, e.g. with django-parler's
`TranslatableSlugMixin` (it also redirects fallback-language URLs) under `i18n_patterns`, and
link other languages with `{% get_translated_url %}`. When a slug follows a changed title, the
object's old URL in that language stops working unless you add a redirect. To keep a model's
slugs out of this, list them in `EXCLUDE_FIELDS`.

## Development

```sh
make install   # uv sync
make check     # ruff, migrations check, tests (SQLite, no services needed)
make matrix    # tests on Django 5.2 and 6.0 across Python 3.10–3.14
```

Design decisions: [docs/decisions](docs/decisions/). License: BSD-3-Clause.
Contact: hello@ponyglot.app
