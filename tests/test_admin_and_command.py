"""Review admin, the `ponyglot` management command, the client and system checks."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ponyglot import sync
from ponyglot.adapters import registry
from ponyglot.admin import word_diff
from ponyglot.checks import check_settings
from ponyglot.client import APIError, Client
from ponyglot.models import Suggestion
from tests.testapp.models import Article

pytestmark = pytest.mark.django_db

parler = registry.get("parler")


@pytest.fixture
def command(cloud, monkeypatch):
    monkeypatch.setattr("ponyglot.client.time.sleep", lambda seconds: None)
    monkeypatch.setattr("ponyglot.management.commands.ponyglot.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "ponyglot.management.commands.ponyglot.Client", lambda: Client(transport=cloud)
    )

    def run(*args):
        out = StringIO()
        call_command("ponyglot", *args, stdout=out, stderr=out)
        return out.getvalue()

    return run


@pytest.fixture
def suggestion(api, cloud, article):
    cloud.add_result(parler.external_key(article), "title", "de", "Preise", source="Pricing")
    sync.run(api)
    return Suggestion.objects.get()


# --- Admin -----------------------------------------------------------------------------------


def test_word_diff_escapes_and_marks_changes():
    html = word_diff("Preise <b>alt</b>", "Preise <b>neu</b>")
    assert "<del>&lt;b&gt;alt&lt;/b&gt;</del>" in html
    assert "<ins>&lt;b&gt;neu&lt;/b&gt;</ins>" in html


def test_admin_shows_source_diff_and_applies_edited_text(client, admin_user, suggestion, article):
    client.force_login(admin_user)
    url = f"/admin/ponyglot/suggestion/{suggestion.pk}/change/"
    page = client.get(url).content.decode()
    assert "Pricing" in page  # source
    assert "<ins>Preise</ins>" in page
    assert 'name="_ponyglot_apply"' in page

    client.post(url, {"final_text": "Preisliste", "_ponyglot_apply": "1"})

    suggestion.refresh_from_db()
    assert (suggestion.status, suggestion.final_text) == ("applied", "Preisliste")
    assert Article.objects.language("de").get(pk=article.pk).title == "Preisliste"
    # Decided suggestions are read-only.
    assert 'name="_ponyglot_apply"' not in client.get(url).content.decode()


def test_admin_reject_and_bulk_actions(client, admin_user, suggestion):
    client.force_login(admin_user)
    client.post(
        f"/admin/ponyglot/suggestion/{suggestion.pk}/change/",
        {"final_text": suggestion.text, "_ponyglot_reject": "1"},
    )
    suggestion.refresh_from_db()
    assert suggestion.status == "rejected"


def test_admin_bulk_apply_warns_about_changed_sources(client, admin_user, suggestion, article):
    article.set_current_language("en")
    article.title = "Prices"
    article.save()
    client.force_login(admin_user)
    response = client.post(
        "/admin/ponyglot/suggestion/",
        {"action": "apply_selected", "_selected_action": [suggestion.pk]},
        follow=True,
    )
    assert "its source changed" in response.content.decode()
    suggestion.refresh_from_db()
    assert suggestion.status == "outdated"


def test_admin_changed_source_is_flagged(client, admin_user, suggestion, article):
    article.set_current_language("en")
    article.title = "Prices"
    article.save()
    client.force_login(admin_user)
    page = client.get(f"/admin/ponyglot/suggestion/{suggestion.pk}/change/").content.decode()
    assert "The source changed since this was translated" in page


# --- Command ---------------------------------------------------------------------------------


def test_check(command, cloud, article):
    output = command("check")
    assert "Connected to site “Test site”. Target languages: de, fr" in output
    assert cloud.handshakes[0]["source_language"] == "en"


def test_backfill_dry_run_needs_no_cloud(command, cloud, article, product):
    output = command("backfill", "--dry-run")
    assert "2 units, 4 segments" in output
    assert cloud.requests == []


def test_sync_and_backfill(command, cloud, article, product):
    assert "pushed 2" in command("sync")
    output = command("backfill")
    assert "2 objects queued" in output and "unchanged 2" in output


def orphan(cloud, key, label="Gone"):
    """A unit the cloud has, e.g. from another database pushing to the same site."""
    cloud.units[key] = {"external_key": key, "label": label, "segments": []}


def test_prune_lists_then_deletes_units_whose_object_is_gone(command, cloud, api, article):
    sync.run(api)
    gone = f"parler:testapp.article:{article.pk + 100}"
    orphan(cloud, gone, "Old pricing")
    orphan(cloud, "gettext:django")  # no such adapter here
    Suggestion.objects.create(
        result_id="r1", adapter="parler", external_key=gone, field="title", language="de", text="x"
    )

    output = command("prune")
    assert f"{gone}  Old pricing" in output and "Run with --yes" in output
    assert "1 'gettext' unit(s) left alone" in output
    assert cloud.deleted == []

    assert "1 unit(s) deleted in Ponyglot." in command("prune", "--yes")
    assert cloud.deleted == [gone]
    assert Suggestion.objects.get(external_key=gone).status == "outdated"
    assert parler.external_key(article) in cloud.units
    assert "Nothing to prune." in command("prune")


def test_prune_leaves_an_adapter_alone_when_none_of_its_objects_exist(command, cloud, article):
    orphan(cloud, "modeltranslation:testapp.product:998")
    output = command("prune", "--yes")
    assert "None of the 'modeltranslation' units exist here" in output
    assert cloud.deleted == []
    command("prune", "--adapter", "modeltranslation", "--yes")
    assert cloud.deleted == ["modeltranslation:testapp.product:998"]


def test_translate_needs_confirmation_for_large_jobs(command, cloud, api, article):
    sync.run(api)
    cloud.confirm_above = 10
    with pytest.raises(CommandError, match="--yes"):
        command("translate")
    output = command("translate", "--yes", "--wait")
    assert "job 1: succeeded" in output
    assert "ponyglot sync" in output


def test_translate_estimate(command, cloud, api, article):
    sync.run(api)
    output = command("translate", "--estimate")
    assert "billable characters" in output and "1 from translation memory" in output
    assert cloud.jobs == {}


def test_command_reports_api_errors(command, cloud, article):
    cloud.fail_next = [503] * 10
    with pytest.raises(CommandError, match="HTTP 503"):
        command("sync")


# --- Client and checks -----------------------------------------------------------------------


def test_client_requires_a_key(settings):
    settings.PONYGLOT = {}
    with pytest.raises(Exception, match="API_KEY"):
        Client()


def test_client_error_carries_code(cloud, api):
    cloud.fail_next = [402]
    with pytest.raises(APIError) as error:
        api.site_status()
    assert error.value.status == 402


def test_checks(settings):
    assert check_settings(None) == []
    settings.PONYGLOT = {"SOURCE_LANGUAGE": "es"}
    ids = {message.id for message in check_settings(None)}
    assert ids == {"ponyglot.W001", "ponyglot.E001"}


def test_non_json_answers_become_api_errors(cloud):
    def html_page(method, url, headers, body, timeout):
        return 404, b"\n<!doctype html><title>Not Found</title>"

    client = Client(transport=html_page)
    with pytest.raises(APIError) as error:
        client.unit_status("djangocms:cms.pagecontent:1")
    assert error.value.status == 404
    assert "not JSON" in str(error.value) and "API_URL" in str(error.value)


def test_api_url_without_v1_is_flagged(settings):
    settings.PONYGLOT = {**settings.PONYGLOT, "API_URL": "https://ponyglot-staging.example/app"}
    assert "ponyglot.W003" in {message.id for message in check_settings(None)}


def test_results_can_be_requested_for_one_unit(cloud):
    cloud.results = [
        {"id": 1, "unit": "a", "segment": "t", "language": "de", "text": "x"},
        {"id": 2, "unit": "b", "segment": "t", "language": "de", "text": "y"},
    ]
    client = Client(transport=cloud)
    assert [r["id"] for r in client.results(unit="b")] == [2]
    assert [r["id"] for r in client.results()] == [1, 2]
