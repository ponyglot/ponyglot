"""gettext catalogs through Ponyglot: `ponyglot po sync` (ADR 0018 in ponyglot-cloud).

A catalog is one domain in one locale directory (`locale/<lang>/LC_MESSAGES/django.po` for
every language) and one unit in the cloud; each entry is a segment. `msgctxt` is the
segment's context, `#.` comments are notes for the translator, and a plural is a second
segment linked to its singular. Each run:

1. pushes the catalogs; existing translations are imported, never retranslated;
2. reports what was merged: a delivered translation that is in the catalog unchanged is
   approved, one with other wording is approved as corrected by a person. An update of an
   approved translation (`replaces`, e.g. after an edit in the translation memory) waits
   while the catalog still has the text it replaces;
3. requests translation of what is missing or stale and waits for the job;
4. writes the translations still awaiting approval into the catalogs (`fuzzy`: flagged
   fuzzy, so they only count once someone removes the flag) and compiles the `.mo` files.

Translations awaiting approval aren't acknowledged: every run gets them again until they
are in the catalogs it reads. Run it on a checkout of the default branch and commit the result
to a review branch; merging is the approval. Moved lines (`#:` references) don't matter:
entries are matched by `msgctxt` + `msgid`.

Needs `polib` (`pip install "ponyglot[gettext]"`), not Django settings.
"""

import hashlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .client import APIError

ADAPTER = "gettext"
PLURAL = ":plural"
DEFAULT_DOMAINS = ("django", "djangojs")
ACTIVE = ("queued", "running")
REVIEW_BATCH = 500
MAX_NOTES = 2000


class POError(Exception):
    """The catalogs can't be synced (nothing found, a push or job refused)."""


def segment_key(msgid, msgctxt=None):
    """Stable key of an entry: its context and text (a hash; msgids can be long)."""
    raw = msgid if msgctxt is None else f"{msgctxt}\x04{msgid}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def language_code(directory_name):
    """`pt_BR` → `pt-br` (Django's locale directory names → language codes)."""
    return directory_name.replace("_", "-").lower()


@dataclass
class Catalog:
    """One domain in one locale directory, with a `.po` file per language."""

    locale_dir: Path
    domain: str
    paths: dict = field(default_factory=dict)  # language → path of the .po file
    files: dict = field(default_factory=dict)  # language → polib.POFile
    changed: set = field(default_factory=set)  # languages written to

    @property
    def label(self):
        return f"{self.locale_dir.as_posix()}/{self.domain}.po"

    @property
    def external_key(self):
        location = re.sub(r"[^A-Za-z0-9_.\-]", ".", self.locale_dir.as_posix()).strip(".")
        return f"{ADAPTER}:{self.domain}:{location or 'locale'}"


def find_catalogs(locale_dirs, domains=DEFAULT_DOMAINS, source_language="en"):
    """The catalogs in `locale_dirs`, loaded; the source language's own catalog is skipped."""
    import polib

    catalogs = []
    for directory in locale_dirs:
        directory = Path(directory)
        if directory.is_absolute():
            relative = os.path.relpath(directory)
            directory = Path(relative) if not relative.startswith("..") else directory
        for domain in domains:
            catalog = Catalog(directory, domain)
            for path in sorted(directory.glob(f"*/LC_MESSAGES/{domain}.po")):
                language = language_code(path.parent.parent.name)
                if language == source_language.lower():
                    continue
                catalog.paths[language] = path
                catalog.files[language] = polib.pofile(str(path))
            if catalog.files:
                catalogs.append(catalog)
    return catalogs


def nplurals(po):
    match = re.search(r"nplurals\s*=\s*(\d+)", po.metadata.get("Plural-Forms", ""))
    return int(match.group(1)) if match else 2


def _entries(po):
    return (entry for entry in po if not entry.obsolete and entry.msgid)


def _current(entry, form):
    """The entry's approved translation (not fuzzy) of a form, or ""."""
    if entry.fuzzy:
        return ""
    if form is None:
        return entry.msgstr
    return entry.msgstr_plural.get(form, "")


def index(po):
    """`{segment key: (entry, plural form or None)}` of a catalog."""
    entries = {}
    for entry in _entries(po):
        key = segment_key(entry.msgid, entry.msgctxt)
        if entry.msgid_plural:
            entries[key] = (entry, 0)
            entries[key + PLURAL] = (entry, 1)
        else:
            entries[key] = (entry, None)
    return entries


def snapshot(catalog, source_language="en"):
    """The unit snapshot of a catalog: segments from all languages, their translations."""
    segments = {}
    for language, po in catalog.files.items():
        two_forms = nplurals(po) == 2
        for entry in _entries(po):
            key = segment_key(entry.msgid, entry.msgctxt)
            forms = [(key, entry.msgid, 0 if entry.msgid_plural else None)]
            if entry.msgid_plural:
                forms.append((key + PLURAL, entry.msgid_plural, 1))
            for form_key, text, form in forms:
                segment = segments.get(form_key)
                if segment is None:
                    segment = segments[form_key] = {"key": form_key, "text": text}
                    if entry.msgctxt:
                        segment["context"] = entry.msgctxt
                    if entry.comment:
                        segment["notes"] = entry.comment[:MAX_NOTES]
                    if form == 1:
                        segment.update(parent_key=key, kind="plural")
                    segment["translations"] = {}
                if form is not None and not two_forms:
                    continue  # one segment per form covers two-form languages only
                if translation := _current(entry, form):
                    segment["translations"][language] = translation
    for segment in segments.values():
        if not segment["translations"]:
            del segment["translations"]
    return {
        "external_key": catalog.external_key,
        "adapter": ADAPTER,
        "label": catalog.label,
        "source_language": source_language,
        "writable_languages": sorted(catalog.files),
        "segments": list(segments.values()),
    }


@dataclass
class Report:
    catalogs: int = 0
    approved: int = 0  # merged unchanged
    corrected: int = 0  # merged with other wording
    written: int = 0  # awaiting approval, written into the catalogs
    job: dict | None = None
    warnings: list = field(default_factory=list)


def _merged(client, catalogs, report):
    """Report delivered translations that are in the catalogs now as approved."""
    reviews = []
    for catalog in catalogs:
        indexes = {language: index(po) for language, po in catalog.files.items()}
        for result in client.results(unit=catalog.external_key):
            if result.get("unit") != catalog.external_key:
                continue  # a cloud without the unit filter
            found = indexes.get(result["language"], {}).get(result["key"])
            current = _current(*found) if found else ""
            if not current or current == result.get("replaces"):
                continue  # not merged (yet); an update while the catalog has the old text
            review = {"id": result["id"], "outcome": "approved"}
            if current != result["text"]:
                review["text"] = current  # corrected in review
                report.corrected += 1
            else:
                report.approved += 1
            reviews.append(review)
    for start in range(0, len(reviews), REVIEW_BATCH):
        client.report_reviews(reviews[start : start + REVIEW_BATCH])


def _translate(client, *, confirm, wait, poll, sleep, report):
    try:
        job = client.create_job(type="delta", prefix=f"{ADAPTER}:", confirm=confirm)
    except APIError as error:
        if error.code == "confirmation_required":
            estimate = error.data.get("estimate") or {}
            raise POError(
                f"The job needs confirmation ({estimate.get('estimated_characters', 0):,} "
                "billable characters): run again with --yes."
            ) from error
        raise
    deadline = time.monotonic() + wait
    while job.get("status") in ACTIVE and time.monotonic() < deadline:
        sleep(poll)
        job = client.job(job["id"])
    if job.get("status") in ACTIVE:
        report.warnings.append(
            f"Job {job['id']} is still {job['status']}; its translations come with the next run."
        )
    for error in job.get("errors", []):
        report.warnings.append(f"{error.get('language') or '*'}: {error.get('message')}")
    report.job = job


def _write(entry, form, text, fuzzy):
    if form is None:
        entry.msgstr = text
    else:
        if entry.fuzzy:  # the other form belongs to the old msgid: untranslated until delivered
            entry.msgstr_plural = dict.fromkeys(entry.msgstr_plural, "")
        entry.msgstr_plural[form] = text
    entry.previous_msgctxt = entry.previous_msgid = entry.previous_msgid_plural = None
    if fuzzy and "fuzzy" not in entry.flags:
        entry.flags.append("fuzzy")
    if not fuzzy and "fuzzy" in entry.flags:
        entry.flags.remove("fuzzy")


def _deliver(client, catalogs, *, fuzzy, report):
    """Write the translations awaiting approval into the catalogs."""
    skipped_plurals = set()
    for catalog in catalogs:
        indexes = {language: index(po) for language, po in catalog.files.items()}
        for result in client.results(unit=catalog.external_key):
            language = result["language"]
            if result.get("unit") != catalog.external_key or language not in indexes:
                continue
            found = indexes[language].get(result["key"])
            if found is None:
                continue  # the entry is gone from the catalog
            entry, form = found
            if form is not None and nplurals(catalog.files[language]) != 2:
                skipped_plurals.add(language)
                continue
            if _current(entry, form) == result["text"] and fuzzy == entry.fuzzy:
                continue
            _write(entry, form, result["text"], fuzzy)
            catalog.changed.add(language)
            report.written += 1
    for language in sorted(skipped_plurals):
        report.warnings.append(
            f"{language}: plurals not written (the language doesn't have two plural forms)."
        )


def save(catalogs, *, compile_mo=True):
    # polib wraps long lines a little differently from GNU gettext (makemessages): if its
    # msgcat is installed, it rewrites the files the way makemessages does, without noise in
    # diffs.
    msgcat = shutil.which("msgcat")
    for catalog in catalogs:
        for language, po in catalog.files.items():
            path = catalog.paths[language]
            if language in catalog.changed:
                po.save(str(path))
                if msgcat:
                    subprocess.run([msgcat, "-o", str(path), str(path)], check=True)  # noqa: S603
            if compile_mo:
                po.save_as_mofile(str(path.with_suffix(".mo")))


def sync(
    client,
    locale_dirs,
    *,
    domains=DEFAULT_DOMAINS,
    source_language="en",
    translate=True,
    confirm=False,
    fuzzy=False,
    compile_mo=True,
    wait=1800,
    poll=5,
    sleep=time.sleep,
):
    """Push the catalogs, approve what was merged, translate, write; returns a `Report`."""
    catalogs = find_catalogs(locale_dirs, domains, source_language)
    if not catalogs:
        raise POError(
            f"No catalogs ({', '.join(f'{d}.po' for d in domains)}) in "
            f"{', '.join(str(d) for d in locale_dirs)}."
        )
    report = Report(catalogs=len(catalogs))
    languages = sorted({language for catalog in catalogs for language in catalog.files})
    answer = client.handshake(
        adapters=[ADAPTER], source_language=source_language, languages=languages
    )
    report.warnings += answer.get("warnings", [])
    pushed = client.push_units([snapshot(catalog, source_language) for catalog in catalogs])
    refused = [f"{item['external_key']}: {item.get('error')}" for item in pushed if not item["ok"]]
    if refused:
        raise POError("The cloud refused " + "; ".join(refused))
    _merged(client, catalogs, report)
    if translate:
        _translate(client, confirm=confirm, wait=wait, poll=poll, sleep=sleep, report=report)
    _deliver(client, catalogs, fuzzy=fuzzy, report=report)
    save(catalogs, compile_mo=compile_mo)
    return report
