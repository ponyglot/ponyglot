"""gettext catalogs through Ponyglot: `ponyglot po sync` (ADR 0018 in ponyglot-cloud).

A catalog is one domain in one locale directory (`locale/<lang>/LC_MESSAGES/django.po` for
every language) and one unit in the cloud; each entry is a segment. `msgctxt` is the
segment's context, `#.` comments are notes for the translator, and a plural is a second
segment linked to its singular. Languages without two plural forms (pl, ru, ar, ja, …) get a
unit of their own per catalog (`…:plurals:<language>`) with one segment per form; their context
and notes say which counts the form is for. Each run:

1. pushes the catalogs; existing translations are imported, never retranslated;
2. reports what was merged: a delivered translation that is in the catalog unchanged is
   approved, one with other wording is approved as corrected by a person. An update of an
   approved translation (`replaces`, e.g. after an edit in the translation memory) waits
   while the catalog still has the text it replaces;
3. requests translation of what is missing or stale and waits for the job;
4. writes the translations still awaiting approval into the catalogs (`fuzzy`: flagged
   fuzzy, so they only count once someone removes the flag; also those whose placeholders
   `compilemessages` would refuse) and compiles the `.mo` files.

Translations awaiting approval aren't acknowledged: every run gets them again until they
are in the catalogs it reads. Run it on a checkout of the default branch and commit the result
to a review branch; merging is the approval. Moved lines (`#:` references) don't matter:
entries are matched by `msgctxt` + `msgid`. `ponyglot po check` fails when translations are
missing, fuzzy or have broken placeholders (for CI).

Needs `polib` (`pip install "ponyglot[gettext]"`), not Django settings.
"""

import gettext
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
PLURALS_UNIT = ":plurals:"
DEFAULT_DOMAINS = ("django", "djangojs")
ACTIVE = ("queued", "running")
REVIEW_BATCH = 500
MAX_NOTES = 2000
MAX_CONTEXT = 500
PUSH_BATCH = 100


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

    def plurals_key(self, language):
        """The unit of a language's plural forms, for languages without two."""
        return f"{self.external_key}{PLURALS_UNIT}{language}"

    @property
    def unit_keys(self):
        return [self.external_key] + [
            self.plurals_key(language)
            for language, po in sorted(self.files.items())
            if nplurals(po) != 2
        ]


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


def plural_examples(po, limit=6):
    """For each plural form, the first counts it is used for: `[[1, 21, 31, …], [2, 3, 4, …], …]`
    (empty lists without a readable `Plural-Forms` rule)."""
    examples = [[] for _ in range(nplurals(po))]
    match = re.search(r"plural\s*=\s*([^;]+)", po.metadata.get("Plural-Forms", ""))
    try:
        rule = gettext.c2py(match.group(1).strip()) if match else None
    except (ValueError, RecursionError):
        rule = None
    if rule is not None:
        for n in range(1000):
            form = rule(n)
            if 0 <= form < len(examples) and len(examples[form]) < limit:
                examples[form].append(n)
    return examples


def form_key(key, form):
    return f"{key}:form:{form}"


def _entries(po):
    return (entry for entry in po if not entry.obsolete and entry.msgid)


def _stored(entry, form):
    """The entry's translation of a form as it is in the catalog, fuzzy or not."""
    if form is None:
        return entry.msgstr
    return entry.msgstr_plural.get(form, "")


def _current(entry, form):
    """The entry's approved translation (not fuzzy) of a form, or ""."""
    return "" if entry.fuzzy else _stored(entry, form)


def index(po):
    """`{segment key: (entry, plural form or None)}` of a catalog. Plurals of languages without
    two forms are keyed by form (the segments of the language's plurals unit)."""
    entries = {}
    forms = nplurals(po)
    for entry in _entries(po):
        key = segment_key(entry.msgid, entry.msgctxt)
        if entry.msgid_plural and forms == 2:
            entries[key] = (entry, 0)
            entries[key + PLURAL] = (entry, 1)
        elif entry.msgid_plural:
            for form in range(forms):
                entries[form_key(key, form)] = (entry, form)
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
                    continue  # in the language's plurals unit
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


def plurals_snapshot(catalog, language, source_language="en"):
    """The unit of a language's plural forms (one segment per form) for languages without two.
    The context keeps the forms apart in the translation memory; the notes say which counts
    each form is for."""
    po = catalog.files[language]
    forms = nplurals(po)
    examples = plural_examples(po)
    segments = []
    for entry in _entries(po):
        if not entry.msgid_plural:
            continue
        key = segment_key(entry.msgid, entry.msgctxt)
        for form in range(forms):
            counts = examples[form]
            used = f", used for counts like {', '.join(map(str, counts))}" if counts else ""
            note = (
                f"Plural form {form + 1} of {forms} in {language}{used}. "
                f"Singular: {entry.msgid!r}; plural: {entry.msgid_plural!r}."
            )
            context = f"plural form {form + 1} of {forms}"
            if entry.msgctxt:
                context = f"{entry.msgctxt} · {context}"[-MAX_CONTEXT:]
            segment = {
                "key": form_key(key, form),
                "text": entry.msgid if 1 in counts else entry.msgid_plural,
                "context": context,
                "notes": "\n\n".join(filter(None, [note, entry.comment]))[:MAX_NOTES],
                "kind": "plural",
            }
            if translation := _current(entry, form):
                segment["translations"] = {language: translation}
            segments.append(segment)
    return {
        "external_key": catalog.plurals_key(language),
        "adapter": ADAPTER,
        "label": f"{catalog.label} ({language} plural forms)",
        "source_language": source_language,
        "writable_languages": [language],
        "segments": segments,
    }


# Placeholders as `msgfmt --check-format` (compilemessages) checks them, by entry flag.
PYTHON_FORMAT = re.compile(
    r"%(?:\((?P<name>[^)]*)\))?[#0 +-]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?(?P<conversion>[a-zA-Z%])"
)
BRACE_FORMAT = re.compile(r"\{\{|\}\}|\{(?P<field>[^{}!:]*)(?:![rsa])?(?::[^{}]*)?\}")


def _python_placeholders(text):
    named, positional = {}, []
    for match in PYTHON_FORMAT.finditer(text):
        if match["conversion"] == "%":
            continue
        if match["name"] is None:
            positional.append(match[0])
        else:
            named[match["name"]] = f"%({match['name']}){match['conversion']}"
    return named, positional


def _brace_placeholders(text):
    named, positional = {}, []
    for match in BRACE_FORMAT.finditer(text):
        if match["field"] is None:
            continue  # {{ or }}
        name = re.split(r"[.\[]", match["field"], maxsplit=1)[0]
        if name:
            named[name] = f"{{{name}}}"
        else:
            positional.append("{}")
    return named, positional


PLACEHOLDERS = {"python-format": _python_placeholders, "python-brace-format": _brace_placeholders}


def placeholder_problem(entry, text, form=None):
    """Why `compilemessages` would refuse `text` as the entry's translation, or "". A plural form
    may leave placeholders out (e.g. "one site" for 1)."""
    problems = []
    for flag, parse in PLACEHOLDERS.items():
        if flag not in entry.flags:
            continue
        sources = [parse(entry.msgid)]
        if form is not None and entry.msgid_plural:
            sources.append(parse(entry.msgid_plural))
        named, positional = parse(text)
        allowed = {name: spec for source_named, _ in sources for name, spec in source_named.items()}
        for name, spec in named.items():
            if name not in allowed:
                problems.append(f"unexpected {spec}")
            elif allowed[name] != spec:
                problems.append(f"{spec} instead of {allowed[name]}")
        if form is None:
            required = sources[0][0]
            problems += [f"{spec} missing" for name, spec in required.items() if name not in named]
        expected = [source_positional for _, source_positional in sources]
        if positional != expected[0] and not (form is not None and positional in [*expected, []]):
            problems.append(f"expects {' '.join(expected[-1]) or 'no unnamed placeholders'}")
    return f"placeholders don't match ({', '.join(problems)})" if problems else ""


@dataclass
class Report:
    catalogs: int = 0
    approved: int = 0  # merged unchanged
    corrected: int = 0  # merged with other wording
    written: int = 0  # awaiting approval, written into the catalogs
    job: dict | None = None
    warnings: list = field(default_factory=list)


def _results(client, catalog):
    for unit in catalog.unit_keys:
        for result in client.results(unit=unit):
            if result.get("unit") == unit:  # a cloud without the unit filter sends all
                yield result


def _merged(client, catalogs, report):
    """Report delivered translations that are in the catalogs now as approved."""
    reviews = []
    for catalog in catalogs:
        indexes = {language: index(po) for language, po in catalog.files.items()}
        for result in _results(client, catalog):
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


def _write(entry, form, text, fuzzy, *, outdated=False):
    """`outdated`: the entry is fuzzy from msgmerge, its other forms belong to the old msgid."""
    if form is None:
        entry.msgstr = text
    else:
        if outdated:  # untranslated until delivered
            entry.msgstr_plural = dict.fromkeys(entry.msgstr_plural, "")
        entry.msgstr_plural[form] = text
    entry.previous_msgctxt = entry.previous_msgid = entry.previous_msgid_plural = None
    if fuzzy and "fuzzy" not in entry.flags:
        entry.flags.append("fuzzy")
    if not fuzzy and "fuzzy" in entry.flags:
        entry.flags.remove("fuzzy")


def _deliver(client, catalogs, *, fuzzy, report):
    """Write the translations awaiting approval into the catalogs; those with placeholders
    `compilemessages` would refuse as fuzzy."""
    for catalog in catalogs:
        indexes = {language: index(po) for language, po in catalog.files.items()}
        outdated = {
            id(entry) for po in catalog.files.values() for entry in _entries(po) if entry.fuzzy
        }
        for result in _results(client, catalog):
            found = indexes.get(result["language"], {}).get(result["key"])
            if found is None:
                continue  # the entry is gone from the catalog (or the form from the language)
            entry, form = found
            draft = fuzzy
            if problem := placeholder_problem(entry, result["text"], form):
                draft = True
                report.warnings.append(
                    f"{catalog.paths[result['language']].as_posix()}: {entry.msgid!r}: "
                    f"{problem}; written as fuzzy."
                )
            if _stored(entry, form) == result["text"] and draft == entry.fuzzy:
                continue
            _write(entry, form, result["text"], draft, outdated=id(entry) in outdated)
            outdated.discard(id(entry))
            catalog.changed.add(result["language"])
            report.written += 1


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


def _catalogs(locale_dirs, domains, source_language):
    catalogs = find_catalogs(locale_dirs, domains, source_language)
    if not catalogs:
        raise POError(
            f"No catalogs ({', '.join(f'{d}.po' for d in domains)}) in "
            f"{', '.join(str(d) for d in locale_dirs)}."
        )
    return catalogs


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
    catalogs = _catalogs(locale_dirs, domains, source_language)
    report = Report(catalogs=len(catalogs))
    languages = sorted({language for catalog in catalogs for language in catalog.files})
    answer = client.handshake(
        adapters=[ADAPTER], source_language=source_language, languages=languages
    )
    report.warnings += answer.get("warnings", [])
    snapshots = []
    for catalog in catalogs:
        snapshots.append(snapshot(catalog, source_language))
        snapshots += [
            plurals_snapshot(catalog, language, source_language)
            for language, po in sorted(catalog.files.items())
            if nplurals(po) != 2
        ]
    pushed = []
    for start in range(0, len(snapshots), PUSH_BATCH):
        pushed += client.push_units(snapshots[start : start + PUSH_BATCH])
    refused = [f"{item['external_key']}: {item.get('error')}" for item in pushed if not item["ok"]]
    if refused:
        raise POError("The cloud refused " + "; ".join(refused))
    _merged(client, catalogs, report)
    if translate:
        _translate(client, confirm=confirm, wait=wait, poll=poll, sleep=sleep, report=report)
    _deliver(client, catalogs, fuzzy=fuzzy, report=report)
    save(catalogs, compile_mo=compile_mo)
    return report


@dataclass
class Check:
    """What `ponyglot po check` found in one `.po` file."""

    path: Path
    untranslated: int = 0
    fuzzy: int = 0
    errors: list = field(default_factory=list)  # (msgid, problem)

    @property
    def ok(self):
        return not (self.untranslated or self.fuzzy or self.errors)


def check(locale_dirs, *, domains=DEFAULT_DOMAINS, source_language="en"):
    """A `Check` per `.po` file: untranslated and fuzzy entries, placeholders `compilemessages`
    would refuse. No connection needed."""
    checks = []
    for catalog in _catalogs(locale_dirs, domains, source_language):
        for language, po in sorted(catalog.files.items()):
            result = Check(catalog.paths[language])
            forms = range(nplurals(po))
            for entry in _entries(po):
                if entry.fuzzy:
                    result.fuzzy += 1
                    continue
                texts = [(form, _stored(entry, form)) for form in forms]
                if not entry.msgid_plural:
                    texts = [(None, entry.msgstr)]
                if not all(text for _, text in texts):
                    result.untranslated += 1
                    continue
                for form, text in texts:
                    if problem := placeholder_problem(entry, text, form):
                        result.errors.append((entry.msgid, problem))
                        break
            checks.append(result)
    return checks
