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
"""


def write_catalog(root, language, plurals="nplurals=2; plural=(n != 1);", **texts):
    values = {"may": "", "settings": "", "site": "", "sites": "", **texts}
    path = root / "locale" / language / "LC_MESSAGES" / "django.po"
    path.parent.mkdir(parents=True)
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


def test_plurals_need_two_forms(tmp_path, monkeypatch, cloud):
    monkeypatch.chdir(tmp_path)
    write_catalog(tmp_path, "ja", plurals="nplurals=1; plural=0;", site="%(count)s サイト")
    po.sync(client(cloud), ["locale"], translate=False)
    plural_key = by_key(cloud.units["gettext:django:locale"])["%(count)s sites"]["key"]
    assert "translations" not in by_key(cloud.units[KEY])["%(count)s site"]
    cloud.add_result(KEY, plural_key, "ja", "サイト", source="%(count)s sites")
    report = po.sync(client(cloud), ["locale"], translate=False)
    assert report.written == 0
    assert report.warnings == [
        "ja: plurals not written (the language doesn't have two plural forms)."
    ]


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
