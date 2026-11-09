"""`ponyglot po sync`: gettext catalogs through the cloud (no Django settings needed)."""

import io

import polib
import pytest

from ponyglot import po
from ponyglot.cli import main
from ponyglot.client import Client
from ponyglot.testing import FakeCloud

KEY = "gettext:django:locale"

HEADER = """msgid ""
msgstr ""
"Language: {language}\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Plural-Forms: {plurals}\\n"

"""

ENTRIES = """#. Translators: the month
msgctxt "month name"
msgid "May"
msgstr "{may}"

#: dashboard/views.py
msgid "Settings"
msgstr "{settings}"

msgid "%(count)s site"
msgid_plural "%(count)s sites"
msgstr[0] "{site}"
msgstr[1] "{sites}"

#, fuzzy
#| msgid "Old text"
msgid "New text"
msgstr "Alter Text"

#, python-format
msgid "Hello %(name)s"
msgstr "{hello}"
"""

POLISH = "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);"


def write_catalog(root, language, plurals="nplurals=2; plural=(n != 1);", **texts):
    values = {"may": "", "settings": "", "site": "", "sites": "", "hello": "", **texts}
    path = root / "locale" / language / "LC_MESSAGES" / "django.po"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER.format(language=language, plurals=plurals) + ENTRIES.format(**values))
    return path


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_catalog(tmp_path, "de", settings="Einstellungen")
    write_catalog(tmp_path, "pt_BR", settings="Configurações")
    return tmp_path


@pytest.fixture
def cloud():
    return FakeCloud()


def client(cloud):
    return Client("pg_test", "https://api.ponyglot.test/v1", timeout=5, transport=cloud)


def by_key(snapshot):
    return {segment["text"]: segment for segment in snapshot["segments"]}


def test_a_catalog_is_one_unit(project, cloud):
    report = po.sync(client(cloud), ["locale"], translate=False)
    assert report.catalogs == 1
    assert cloud.handshakes[0]["adapters"] == ["gettext"]
    assert cloud.handshakes[0]["languages"] == ["de", "pt-br"]
    unit = cloud.units[KEY]
    assert unit["label"] == "locale/django.po"
    assert unit["writable_languages"] == ["de", "pt-br"]
    segments = by_key(unit)
    assert segments["May"]["context"] == "month name"
    assert segments["May"]["notes"] == "Translators: the month"
    assert segments["May"]["key"] == po.segment_key("May", "month name") != po.segment_key("May")
    # Existing translations are imported; fuzzy ones aren't.
    assert segments["Settings"]["translations"] == {"de": "Einstellungen", "pt-br": "Configurações"}
    assert "translations" not in segments["New text"]
    plural = segments["%(count)s sites"]
    assert plural["parent_key"] == segments["%(count)s site"]["key"]
    assert plural["kind"] == "plural" and plural["key"].endswith(":plural")


def test_translations_are_written_and_compiled(project, cloud):
    segments = None

    def result(language, text, source):
        key = segments[source]["key"]
        return cloud.add_result(KEY, key, language, text, source=source)

    po.sync(client(cloud), ["locale"], translate=False)
    segments = by_key(cloud.units[KEY])
    result("de", "Mai", "May")
    result("de", "%(count)s Website", "%(count)s site")
    result("de", "%(count)s Websites", "%(count)s sites")
    result("de", "Neuer Text", "New text")
    report = po.sync(client(cloud), ["locale"])
    assert report.written == 4 and report.job["status"] == "succeeded"
    assert cloud.jobs[1]["prefix"] == "gettext:"

    catalog = polib.pofile(str(project / "locale/de/LC_MESSAGES/django.po"))
    entries = {entry.msgid: entry for entry in catalog}
    assert entries["May"].msgstr == "Mai" and entries["May"].msgctxt == "month name"
    assert entries["%(count)s site"].msgstr_plural == {
        0: "%(count)s Website",
        1: "%(count)s Websites",
    }
    new = entries["New text"]
    assert (new.msgstr, new.fuzzy, new.previous_msgid) == ("Neuer Text", False, None)
    assert entries["Settings"].occurrences == [("dashboard/views.py", "")]  # untouched
    compiled = polib.mofile(str(project / "locale/de/LC_MESSAGES/django.mo"))
    assert {e.msgid: e.msgstr for e in compiled}["New text"] == "Neuer Text"
    # Not acknowledged: awaiting approval (the pull request), they come again.
    assert cloud.acked == [] and cloud.reviews == []


def test_merged_translations_are_approved(project, cloud):
    po.sync(client(cloud), ["locale"], translate=False)
    segments = by_key(cloud.units[KEY])
    may = cloud.add_result(KEY, segments["May"]["key"], "de", "Mai", source="May")
    text = cloud.add_result(KEY, segments["New text"]["key"], "de", "Neu", source="New text")
    pending = cloud.add_result(KEY, segments["May"]["key"], "pt-br", "Maio", source="May")
    path = project / "locale/de/LC_MESSAGES/django.po"
    catalog = polib.pofile(str(path))
    for entry in catalog:
        if entry.msgid == "May":
            entry.msgstr = "Mai"  # merged as delivered
        if entry.msgid == "New text":
            entry.msgstr, entry.flags = "Neuer Text", []  # reworded in review
    catalog.save(str(path))

    report = po.sync(client(cloud), ["locale"], translate=False)
    assert (report.approved, report.corrected) == (1, 1)
    assert cloud.reviews == [
        {"id": may["id"], "outcome": "approved"},
        {"id": text["id"], "outcome": "approved", "text": "Neuer Text"},
    ]
    assert report.written == 1  # the Portuguese one, still awaiting approval
    assert pending["id"] not in {r["id"] for r in cloud.reviews}


def test_fuzzy_mode_flags_new_translations(project, cloud):
    po.sync(client(cloud), ["locale"], translate=False)
    key = by_key(cloud.units[KEY])["May"]["key"]
    cloud.add_result(KEY, key, "de", "Mai", source="May")
    po.sync(client(cloud), ["locale"], translate=False, fuzzy=True)
    entry = polib.pofile(str(project / "locale/de/LC_MESSAGES/django.po")).find(
        "May", msgctxt="month name"
    )
    assert entry.msgstr == "Mai" and entry.fuzzy
    # Still fuzzy: not approved on the next run.
    po.sync(client(cloud), ["locale"], translate=False, fuzzy=True)
    assert cloud.reviews == []


def test_languages_without_two_plural_forms_get_a_unit_of_forms(tmp_path, monkeypatch, cloud):
    monkeypatch.chdir(tmp_path)
    write_catalog(tmp_path, "pl", plurals=POLISH, site="%(count)s witryna")
    write_catalog(tmp_path, "ja", plurals="nplurals=1; plural=0;")
    po.sync(client(cloud), ["locale"], translate=False)
    assert list(cloud.units) == [KEY, f"{KEY}:plurals:ja", f"{KEY}:plurals:pl"]
    # The catalog's own plural segments are for two-form languages: nothing imported.
    assert "translations" not in by_key(cloud.units[KEY])["%(count)s site"]

    polish = cloud.units[f"{KEY}:plurals:pl"]
    assert polish["writable_languages"] == ["pl"]
    assert polish["label"] == "locale/django.po (pl plural forms)"
    one, few, many = polish["segments"]
    singular_key = po.segment_key("%(count)s site")
    assert [one["key"], few["key"], many["key"]] == [
        f"{singular_key}:form:{form}" for form in range(3)
    ]
    assert (one["text"], few["text"], many["text"]) == (
        "%(count)s site",
        "%(count)s sites",
        "%(count)s sites",
    )
    # Contexts keep the forms apart in the translation memory; notes say which counts.
    assert [segment["context"] for segment in (one, few, many)] == [
        "plural form 1 of 3",
        "plural form 2 of 3",
        "plural form 3 of 3",
    ]
    assert few["notes"].startswith("Plural form 2 of 3 in pl, used for counts like 2, 3, 4, 22")
    assert "Singular: '%(count)s site'; plural: '%(count)s sites'." in many["notes"]
    assert one["translations"] == {"pl": "%(count)s witryna"}
    (japanese,) = cloud.units[f"{KEY}:plurals:ja"]["segments"]
    assert japanese["text"] == "%(count)s site"  # the only form, used for 1 too

    for segment, text in zip((few, many), ("%(count)s witryny", "%(count)s witryn"), strict=True):
        cloud.add_result(f"{KEY}:plurals:pl", segment["key"], "pl", text, source=segment["text"])
    # A translation of the catalog's plural segment isn't written in Polish.
    plural_key = by_key(cloud.units[KEY])["%(count)s sites"]["key"]
    cloud.add_result(KEY, plural_key, "pl", "witryny", source="%(count)s sites")
    report = po.sync(client(cloud), ["locale"], translate=False)
    assert (report.written, report.warnings) == (2, [])
    entry = polib.pofile(str(tmp_path / "locale/pl/LC_MESSAGES/django.po")).find("%(count)s site")
    assert entry.msgstr_plural == {
        0: "%(count)s witryna",
        1: "%(count)s witryny",
        2: "%(count)s witryn",
    }
    report = po.sync(client(cloud), ["locale"], translate=False)  # merged
    assert (report.approved, report.written) == (2, 0)


def test_fuzzy_plurals_keep_every_form(project, cloud):
    po.sync(client(cloud), ["locale"], translate=False)
    segments = by_key(cloud.units[KEY])
    translations = {"%(count)s site": "%(count)s Website", "%(count)s sites": "%(count)s Websites"}
    for source, text in translations.items():
        cloud.add_result(KEY, segments[source]["key"], "de", text, source=source)
    po.sync(client(cloud), ["locale"], translate=False, fuzzy=True)
    entry = polib.pofile(str(project / "locale/de/LC_MESSAGES/django.po")).find("%(count)s site")
    assert entry.fuzzy
    assert entry.msgstr_plural == {0: "%(count)s Website", 1: "%(count)s Websites"}


def test_broken_placeholders_are_written_as_fuzzy(project, cloud):
    po.sync(client(cloud), ["locale"], translate=False)
    key = by_key(cloud.units[KEY])["Hello %(name)s"]["key"]
    cloud.add_result(KEY, key, "de", "Hallo %(nom)s", source="Hello %(name)s")
    report = po.sync(client(cloud), ["locale"], translate=False)
    assert report.warnings == [
        "locale/de/LC_MESSAGES/django.po: 'Hello %(name)s': placeholders don't match "
        "(unexpected %(nom)s, %(name)s missing); written as fuzzy."
    ]
    entry = polib.pofile(str(project / "locale/de/LC_MESSAGES/django.po")).find("Hello %(name)s")
    assert (entry.msgstr, entry.fuzzy) == ("Hallo %(nom)s", True)


@pytest.mark.parametrize(
    ("flags", "msgid", "text", "form", "problem"),
    [
        (["python-format"], "%(count)s of %(total)s", "%(total)s von %(count)s", None, ""),
        (["python-format"], "%(count)d", "%(count)s", None, "%(count)s instead of %(count)d"),
        (["python-format"], "%s of %d", "%s von %d", None, ""),
        (["python-format"], "%s of %d", "%d von %s", None, "expects %s %d"),
        (["python-format"], "100%% of %(name)s", "100%% von %(name)s", None, ""),
        (["python-format"], "%(count)s site", "eine Website", 0, ""),  # a plural form may drop it
        (["python-brace-format"], "{name} has {count}", "{name} hat {count}", None, ""),
        (
            ["python-brace-format"],
            "{user.name}",
            "{name}",
            None,
            "unexpected {name}, {user} missing",
        ),
        ([], "%(name)s", "%(nom)s", None, ""),  # not flagged: not checked, like msgfmt
    ],
)
def test_placeholder_problems(flags, msgid, text, form, problem):
    entry = polib.POEntry(msgid=msgid, flags=flags)
    if form is not None:
        entry.msgid_plural = msgid
    expected = f"placeholders don't match ({problem})" if problem else ""
    assert po.placeholder_problem(entry, text, form) == expected


def test_check(project):
    path = project / "locale/de/LC_MESSAGES/django.po"
    write_catalog(project / "fr", "fr")  # elsewhere
    checks = po.check(["locale"])
    assert [(c.path.as_posix(), c.untranslated, c.fuzzy, c.ok) for c in checks] == [
        ("locale/de/LC_MESSAGES/django.po", 3, 1, False),
        ("locale/pt_BR/LC_MESSAGES/django.po", 3, 1, False),
    ]
    path.unlink()
    write_catalog(
        project,
        "de",
        may="Mai",
        settings="Einstellungen",
        site="%(count)s Website",
        sites="%(count)s Websites",
        hello="Hallo %(nom)s",
    )
    catalog = polib.pofile(str(path))
    catalog.find("New text").flags.remove("fuzzy")
    catalog.find("New text").msgstr = "Neuer Text"
    catalog.save(str(path))
    (de, _) = po.check(["locale"])
    assert (de.untranslated, de.fuzzy) == (0, 0)
    assert de.errors == [
        ("Hello %(name)s", "placeholders don't match (unexpected %(nom)s, %(name)s missing)")
    ]


def test_check_needs_no_key(project, monkeypatch):
    monkeypatch.delenv("PONYGLOT_API_KEY", raising=False)
    out, err = io.StringIO(), io.StringIO()
    assert main(["po", "check", "--domain", "django"], out=out, err=err) == 1
    assert out.getvalue().splitlines() == [
        "locale/de/LC_MESSAGES/django.po: 3 untranslated, 1 fuzzy",
        "locale/pt_BR/LC_MESSAGES/django.po: 3 untranslated, 1 fuzzy",
        "2 catalog file(s) checked: 2 need work.",
    ]
    for path in project.glob("locale/*/LC_MESSAGES/django.po"):
        catalog = polib.pofile(str(path))
        for entry in catalog:
            entry.flags = [flag for flag in entry.flags if flag != "fuzzy"]
            if entry.msgid_plural:
                entry.msgstr_plural = {0: "%(count)s x", 1: "%(count)s y"}
            else:
                entry.msgstr = entry.msgstr or entry.msgid
        catalog.save(str(path))
    out = io.StringIO()
    assert main(["po", "check"], out=out, err=err) == 0
    assert out.getvalue() == "2 catalog file(s) checked: all translated.\n"


def test_manage_py_uses_locale_paths(project, cloud, settings, monkeypatch):
    from django.core.management import CommandError, call_command

    settings.LOCALE_PATHS = [project / "locale"]
    monkeypatch.setattr("ponyglot.management.commands.ponyglot.Client", lambda: client(cloud))
    out = io.StringIO()
    call_command("ponyglot", "po", "sync", "--no-translate", "--no-mo", stdout=out, stderr=out)
    assert "1 catalog(s)" in out.getvalue()
    assert list(cloud.units) == [KEY]
    with pytest.raises(CommandError, match="Some catalogs need work"):
        call_command("ponyglot", "po", "check", stdout=out)
    assert "locale/de/LC_MESSAGES/django.po: 3 untranslated, 1 fuzzy" in out.getvalue()
    settings.LOCALE_PATHS = []
    with pytest.raises(CommandError, match="LOCALE_PATHS is empty"):
        call_command("ponyglot", "po", "check")


def test_large_jobs_need_confirmation(project, cloud):
    cloud.confirm_above = 1
    with pytest.raises(po.POError, match="--yes"):
        po.sync(client(cloud), ["locale"])
    po.sync(client(cloud), ["locale"], confirm=True)
    assert cloud.jobs


def test_a_running_job_is_picked_up_next_time(project, cloud):
    cloud.job_status = "running"
    sleeps = []
    report = po.sync(client(cloud), ["locale"], wait=0, sleep=sleeps.append)
    assert report.warnings == ["Job 1 is still running; its translations come with the next run."]


def test_external_keys_are_valid_for_nested_directories(tmp_path, monkeypatch, cloud):
    monkeypatch.chdir(tmp_path)
    write_catalog(tmp_path / "app", "de")
    po.sync(client(cloud), [str(tmp_path / "app" / "locale")], translate=False)
    assert list(cloud.units) == ["gettext:django:app.locale"]
    assert cloud.units["gettext:django:app.locale"]["label"] == "app/locale/django.po"


def test_the_command(project, cloud, monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.delenv("PONYGLOT_API_KEY", raising=False)
    assert main(["po", "sync"], transport=cloud, out=out, err=err) == 2
    assert "PONYGLOT_API_KEY" in err.getvalue()

    monkeypatch.setenv("PONYGLOT_API_KEY", "pg_test")
    monkeypatch.setenv("PONYGLOT_API_URL", "https://api.ponyglot.test/v1")
    assert main(["po", "sync", "--no-mo"], transport=cloud, out=out, err=err) == 0
    assert "1 catalog(s)" in out.getvalue() and "job 1: succeeded" in out.getvalue()
    assert not (project / "locale/de/LC_MESSAGES/django.mo").exists()
    assert main(["po", "sync", "nowhere"], transport=cloud, out=out, err=err) == 1
    assert "No catalogs" in err.getvalue()


def test_updates_replace_the_approved_text(project, cloud):
    """An edited translation memory entry: the update is written while the catalog still has
    the replaced text, and approved once merged."""
    po.sync(client(cloud), ["locale"], translate=False)
    key = by_key(cloud.units[KEY])["Settings"]["key"]
    update = cloud.add_result(
        KEY, key, "de", "Einstellungen ändern", source="Settings", replaces="Einstellungen"
    )
    report = po.sync(client(cloud), ["locale"], translate=False)
    assert (report.approved, report.corrected, report.written) == (0, 0, 1)
    path = project / "locale/de/LC_MESSAGES/django.po"
    assert polib.pofile(str(path)).find("Settings").msgstr == "Einstellungen ändern"

    report = po.sync(client(cloud), ["locale"], translate=False)  # merged
    assert report.approved == 1
    assert cloud.reviews == [{"id": update["id"], "outcome": "approved"}]
