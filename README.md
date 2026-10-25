# ponyglot

[![PyPI](https://img.shields.io/pypi/v/ponyglot)](https://pypi.org/project/ponyglot/)
[![Coverage](https://codecov.io/gh/ponyglot/ponyglot/branch/main/graph/badge.svg)](https://codecov.io/gh/ponyglot/ponyglot)
[![Python versions](https://img.shields.io/pypi/pyversions/ponyglot)](https://pypi.org/project/ponyglot/)
[![Django versions](https://img.shields.io/pypi/frameworkversions/django/ponyglot)](https://www.djangoproject.com/)

Ponyglot core: one translation layer for your Django stack. This package holds the API client,
the sync, the review admin and the **django-parler** and **django-modeltranslation** adapters.
The django CMS and Wagtail connectors (`djangocms-ponyglot`, `wagtail-ponyglot`) build on it.

**Early access, alpha.** APIs may change. See https://ponyglot.app.
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

Deleting an object deletes its unit in Ponyglot with the next sync. Units whose object
disappeared otherwise (a restored or reseeded database, a second database pushing to the same
site) stay in Ponyglot and show up in overviews without a link. `ponyglot prune` lists them,
`--yes` deletes them:

```sh
python manage.py ponyglot prune         # units in Ponyglot whose object doesn't exist here
python manage.py ponyglot prune --yes   # delete them in Ponyglot
```

Units of adapters that aren't installed are left alone, and so are all units of an adapter if
none of its objects exist here (likely the wrong database); `--adapter <name>` includes them.

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

## Publish right away

When an editor requests a translation of one object (its dialog) or of selected documents
(the dashboard), they can tick **Publish the translation right away**. It's offered to editors
who may publish the content (models: change permission and `ponyglot.change_suggestion`);
`PUBLISH_BY_DEFAULT = True` pre-ticks it. When the translation arrives, the sync applies it in
the editor's name, unless a QA check found an error, the editor lost the permission, or (CMS
drafts) the draft has other changes; then it waits for review as usual and the sync report
says why. Such translations are reported as *published without review*: the translation memory
only learns them once someone confirms them in Ponyglot. Not offered for a model's or the
site's "translate everything".

## In the admin: translation dialogs and dashboard

Add `PonyglotAdminMixin` to the admins of translated models (before the library's admin class;
works with parler's `TranslatableAdmin` and modeltranslation's `TranslationAdmin` or
`TabbedTranslationAdmin`):

```python
from ponyglot.translations_admin import PonyglotAdminMixin

@admin.register(Product)
class ProductAdmin(PonyglotAdminMixin, TabbedTranslationAdmin):
    ...
```

- **Change page** › *Ponyglot translations*: this object per language: texts missing or
  outdated (**Translate**), translations waiting (**Apply**, or **Review** them one by one),
  texts held by a quality check (a link to the Ponyglot dashboard, the only place where they
  are resolved). If nothing was fetched in 15 minutes, opening it fetches this object's
  translations first.
- **Change list** › *Ponyglot translations*: the same for all objects of the model.
- **Ponyglot › Translation dashboard** (permission *Can use the translation dashboard*): the
  whole site per language, the last sync rounds, every content type with what waits; it can
  translate everything missing and run a sync now. *Which documents need attention* lists
  every document with a gap, per language, to open or to translate and apply in bulk.

Applying needs the permission to change suggestions. Dialogs keep django CMS' modal mode
(`_popup=1`).

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
| `PUBLISH_BY_DEFAULT` | `False` | pre-tick *Publish the translation right away*, see above |

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

## gettext catalogs

`ponyglot po sync` translates `.po` files through Ponyglot, without Django settings:

```console
$ pip install "ponyglot[gettext]"
$ export PONYGLOT_API_KEY=pg_…   # a site with the gettext adapter
$ python manage.py makemessages -l de -l fr
$ ponyglot po sync locale        # --fuzzy, --no-translate, --yes, --domain, --no-mo
```

Each run pushes every catalog (one unit per domain and locale directory; existing
translations are imported), requests translation of what's missing or stale, waits for the job
and writes the results into the `.po` and `.mo` files. Run it on a checkout of your default
branch and commit the result to a review branch: merging is the approval. The next run finds
the translations in the catalogs and reports them approved (changed wording as corrections).
With `--fuzzy`, new translations are flagged fuzzy and count as approved once the flag is gone.

## For connector authors

Adapters subclass `ponyglot.adapters.Adapter` (see `ModelAdapter` and djangocms-ponyglot), set
`capabilities` and, for draft-based systems, `applied_status = "drafted"`. `ponyglot.testing`
has `FakeCloud`, an in-memory API for tests: `Client(transport=FakeCloud())`.

## Development

```sh
make install   # uv sync
make check     # ruff, migrations check, tests (SQLite, no services needed)
make matrix    # tests on Django 5.2, 6.0 and 6.1 across Python 3.10–3.15
```

Design decisions: [docs/decisions](docs/decisions/). License: BSD-3-Clause.
Contact: hello@ponyglot.app
