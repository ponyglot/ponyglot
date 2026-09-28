"""Exclusions, QA errors on suggestions, held-back translations, editor emails (ADR 0002)."""

import pytest
from django.core import mail

from ponyglot import exclusions, suggestions, sync
from ponyglot.adapters import registry
from ponyglot.models import Exclusion, HeldBack, Suggestion, SyncState
from tests.testapp.models import Article

pytestmark = pytest.mark.django_db

parler = registry.get("parler")
ERROR = {"code": "links", "severity": "error", "message": "Links: missing /pricing/"}
WARNING = {"code": "length", "severity": "warning", "message": "Much longer than the source."}


def key(article):
    return parler.external_key(article)


# --- Handshake -------------------------------------------------------------------------------


def test_model_adapters_declare_qa_errors(api, cloud, article):
    cloud.qa_mode = "separate"
    report = sync.run(api)
    assert cloud.handshakes[0]["capabilities"] == ["qa_errors"]
    assert report.qa_mode == "separate"


# --- Exclusions ------------------------------------------------------------------------------


def test_excluding_everything_removes_the_unit(api, cloud, article, admin_user):
    sync.run(api)
    cloud.add_result(key(article), "title", "de", "Preise", source="Pricing")
    sync.run(api)
    exclusions.exclude(parler, article, user=admin_user)

    report = sync.run(api)

    assert report.deleted == 1 and cloud.deleted == [key(article)]
    assert Suggestion.objects.get().status == "outdated"
    article.save()  # later edits aren't pushed
    assert sync.run(api).pushed == 0
    assert key(article) not in cloud.units

    exclusions.include(parler, article)
    assert sync.run(api).pushed == 1
    assert key(article) in cloud.units


def test_excluding_some_languages_narrows_writable_languages(api, cloud, article):
    exclusions.exclude(parler, article, ["de"])
    sync.run(api)
    assert cloud.units[key(article)]["writable_languages"] == ["fr"]
    assert sync.estimate() == (1, 2, len("Pricing<p>€79 per site and month</p>"))


def test_results_for_excluded_languages_are_dropped(api, cloud, article):
    sync.run(api)
    exclusions.exclude(parler, article, ["de"])
    cloud.add_result(key(article), "title", "de", "Preise", source="Pricing")
    cloud.add_result(key(article), "title", "fr", "Tarifs", source="Pricing")
    sync.run(api)
    assert list(Suggestion.objects.values_list("language", flat=True)) == ["fr"]
    assert len(cloud.acked) == 2


def test_excluded_entirely_is_left_out_of_the_estimate(article, product):
    exclusions.exclude(parler, article)
    units, _, _ = sync.estimate()
    assert units == 1


def test_admin_actions_exclude_and_include(client, admin_user, api, cloud, article):
    sync.run(api)
    state = SyncState.objects.get(external_key=key(article))
    client.force_login(admin_user)
    client.post(
        "/admin/ponyglot/syncstate/",
        {"action": "exclude_selected", "_selected_action": [state.pk]},
    )
    assert Exclusion.objects.get().external_key == key(article)
    page = client.get("/admin/ponyglot/syncstate/").content.decode()
    assert "all languages" in page

    client.post(
        "/admin/ponyglot/syncstate/",
        {"action": "include_selected", "_selected_action": [state.pk]},
    )
    assert not Exclusion.objects.exists()


def test_editing_an_exclusion_in_the_admin_marks_the_object(client, admin_user, api, article):
    exclusions.exclude(parler, article)
    sync.run(api)
    exclusion = Exclusion.objects.get()
    client.force_login(admin_user)
    client.post(f"/admin/ponyglot/exclusion/{exclusion.pk}/delete/", {"post": "yes"})
    assert not Exclusion.objects.exists()
    assert SyncState.objects.get(external_key=key(article)).dirty


# --- QA errors on suggestions ----------------------------------------------------------------


@pytest.fixture
def flagged(api, cloud, article):
    cloud.add_result(key(article), "title", "de", "Preise", source="Pricing", qa=[ERROR])
    cloud.add_result(
        key(article),
        "body",
        "de",
        "<p>79 €</p>",
        source="<p>€79 per site and month</p>",
        format="html",
        qa=[WARNING],
    )
    sync.run(api)
    return Suggestion.objects.get(field="title")


def test_errors_block_applying_until_edited_or_confirmed(flagged, admin_user, article):
    result = suggestions.apply(Suggestion.objects.all(), admin_user)
    assert result == (1, 0, 1)  # the warning applies, the error waits
    flagged.refresh_from_db()
    assert flagged.status == "pending"

    assert suggestions.apply([flagged], admin_user, texts={flagged.pk: "Preisliste"}).applied == 1
    assert Article.objects.language("de").get(pk=article.pk).title == "Preisliste"


def test_confirmed_errors_are_applied(flagged, admin_user):
    assert suggestions.apply([flagged], admin_user, confirm_errors=True).applied == 1


def test_admin_asks_to_confirm_errors(client, admin_user, flagged, article):
    client.force_login(admin_user)
    url = f"/admin/ponyglot/suggestion/{flagged.pk}/change/"
    page = client.get(url).content.decode()
    assert 'name="_ponyglot_confirm"' in page
    assert "ponyglot-qa-error" in page and "Links: missing /pricing/" in page

    response = client.post(url, {"final_text": "Preise", "_ponyglot_apply": "1"}, follow=True)
    assert "has a QA error" in response.content.decode()
    flagged.refresh_from_db()
    assert flagged.status == "pending"

    client.post(url, {"final_text": "Preise", "_ponyglot_apply": "1", "_ponyglot_confirm": "on"})
    flagged.refresh_from_db()
    assert flagged.status == "applied"


def test_list_shows_qa_severity(client, admin_user, flagged):
    client.force_login(admin_user)
    page = client.get("/admin/ponyglot/suggestion/").content.decode()
    assert "error" in page and "warning" in page


# --- Held back for QA review -----------------------------------------------------------------


def test_held_back_translations_are_mirrored(api, cloud, article, client, admin_user):
    cloud.qa_mode = "separate"
    cloud.attention = [
        {
            "unit": key(article),
            "label": "Pricing",
            "key": "title",
            "language": "de",
            "issues": [ERROR],
            "since": "2026-09-28T10:00:00Z",
        }
    ]
    assert sync.run(api).held_back == 1
    item = HeldBack.objects.get()
    assert (item.label, item.key, item.language) == ("Pricing", "title", "de")

    client.force_login(admin_user)
    page = client.get("/admin/ponyglot/suggestion/", follow=True).content.decode()
    assert "1 translation is waiting for a QA review" in page
    page = client.get("/admin/ponyglot/heldback/").content.decode()
    assert "Links: missing /pricing/" in page

    cloud.attention = []
    sync.run(api)
    assert not HeldBack.objects.exists()


def test_an_older_cloud_without_attention_is_fine(api, cloud, article):
    cloud.has_attention = False
    assert sync.run(api).held_back == 0


# --- Editor emails ---------------------------------------------------------------------------


def test_no_editor_emails_by_default(api, cloud, article, admin_user):
    cloud.add_result(key(article), "title", "de", "Preise", source="Pricing")
    assert not sync.run(api).notified
    assert mail.outbox == []


def test_editors_are_told_about_new_suggestions(api, cloud, article, admin_user, settings):
    settings.PONYGLOT = {
        **settings.PONYGLOT,
        "NOTIFY_EDITORS": True,
        "SITE_URL": "https://example.com/",
    }
    cloud.add_result(key(article), "title", "de", "Preise", source="Pricing", qa=[ERROR])
    cloud.add_result(key(article), "title", "fr", "Tarifs", source="Pricing")

    assert sync.run(api).notified
    [message] = mail.outbox
    assert message.to == [admin_user.email]
    assert message.subject == "2 translations ready for review"
    assert "- de: 1" in message.body and "- fr: 1" in message.body
    assert "1 of them have a QA error" in message.body
    assert "https://example.com/admin/ponyglot/suggestion/?status__exact=pending" in message.body

    assert not sync.run(api).notified  # nothing new


def test_editor_email_list(api, cloud, article, settings):
    settings.PONYGLOT = {**settings.PONYGLOT, "NOTIFY_EDITORS": ["team@example.com"]}
    cloud.add_result(key(article), "title", "de", "Preise", source="Pricing")
    sync.run(api)
    assert mail.outbox[0].to == ["team@example.com"]
