"""The sync round against a fake cloud: push, delete, results → suggestions, reviews."""

import pytest

from ponyglot import suggestions, sync
from ponyglot.adapters import registry
from ponyglot.client import APIError
from ponyglot.models import Suggestion, SuggestionStatus, SyncState
from tests.testapp.models import Article

pytestmark = pytest.mark.django_db

parler = registry.get("parler")


def test_first_push_includes_existing_translations_then_only_changes(api, cloud, article):
    article.set_current_language("de")
    article.title = "Preise"
    article.save()

    report = sync.run(api)

    key = parler.external_key(article)
    assert report.pushed == 1
    assert cloud.units[key]["segments"][0]["translations"] == {"de": "Preise"}
    assert cloud.handshakes[0]["adapters"] == ["parler", "modeltranslation"]
    assert not SyncState.objects.filter(dirty=True).exists()

    # Saving without changes: skipped. A source change: pushed again, without translations.
    article.save()
    assert sync.run(api).unchanged == 1
    article.set_current_language("en")
    article.title = "Prices"
    article.save()
    assert sync.run(api).pushed == 1
    assert cloud.units[key]["segments"][0] == {
        "key": "title",
        "text": "Prices",
        "format": "plain",
        "field": "title",
        "kind": "title",
        "max_length": 200,
    }


def test_deleted_objects_delete_their_unit(api, cloud, article):
    sync.run(api)
    key = parler.external_key(article)
    article.delete()
    report = sync.run(api)
    assert report.deleted == 1
    assert cloud.deleted == [key]
    assert not SyncState.objects.filter(external_key=key).exists()


def test_rejected_units_stay_dirty_with_the_error(api, cloud, article):
    cloud.reject_keys.add(parler.external_key(article))
    report = sync.run(api)
    assert report.push_errors == [(parler.external_key(article), "invalid")]
    state = SyncState.objects.get(external_key=parler.external_key(article))
    assert state.dirty and state.error == "invalid"


def test_an_edit_during_the_push_stays_dirty(api, cloud, article, monkeypatch):
    original = api.push_units

    def push_and_edit(units):
        units = list(units)
        article.set_current_language("en")
        article.title = "Edited meanwhile"
        article.save()
        return original(units)

    monkeypatch.setattr(api, "push_units", push_and_edit)
    sync.run(api)
    assert SyncState.objects.get(external_key=parler.external_key(article)).dirty


def test_temporary_errors_are_retried(api, cloud, article):
    cloud.fail_next = [503, 502]
    assert sync.run(api).pushed == 1


def test_unreachable_cloud_raises(api, cloud, article):
    cloud.fail_next = [503] * 10
    with pytest.raises(APIError):
        sync.run(api)
    assert SyncState.objects.filter(dirty=True).count() == 1  # nothing lost


def test_results_become_suggestions_and_are_acknowledged(api, cloud, article):
    sync.run(api)
    key = parler.external_key(article)
    first = cloud.add_result(key, "title", "de", "Preise", source="Pricing")
    cloud.add_result("parler:testapp.article:999", "title", "de", "Weg", source="Gone")
    cloud.add_result("djangocms:page:1", "title", "de", "Seite", source="Page")

    report = sync.run(api)

    assert report.delivered == 2
    assert report.undeliverable == {"djangocms": 1}  # left for the django CMS connector
    assert "djangocms" not in " ".join(cloud.acked)
    suggestion = Suggestion.objects.get()
    assert (suggestion.result_id, suggestion.field, suggestion.language, suggestion.text) == (
        first["id"],
        "title",
        "de",
        "Preise",
    )
    # Nothing is written to the article before an editor applies it.
    assert (
        not Article.objects.language("de")
        .filter(pk=article.pk, translations__language_code="de")
        .exists()
    )


def test_refresh_handles_one_unit_only(api, cloud, article, product):
    from ponyglot.models import SyncRun

    sync.run(api)
    article.title = "Prices"
    article.save()
    product.name_en = "Cup"
    product.save()
    key = parler.external_key(article)
    cloud.add_result(key, "title", "de", "Preise", source="Pricing")
    other = registry.get("modeltranslation").external_key(product)
    cloud.add_result(other, "name", "de", "Becher", source="Mug")
    runs = SyncRun.objects.count()

    report = sync.refresh(api, key)

    assert (report.pushed, report.delivered) == (1, 1)
    assert Suggestion.objects.get().external_key == key
    assert SyncState.objects.get(external_key=other).dirty  # left for the next sync round
    # Recorded for the unit: its last fetch moves, the schedule's last round doesn't.
    assert SyncRun.objects.count() == runs + 1
    assert sync.last_fetch(key).unit == key
    assert sync.last_run().unit == ""
    assert sync.last_fetch(other) == sync.last_run()


def test_a_newer_result_supersedes_the_pending_suggestion(api, cloud, article):
    key = parler.external_key(article)
    cloud.add_result(key, "title", "de", "Preise", source="Pricing")
    sync.run(api)
    cloud.add_result(key, "title", "de", "Preisliste", source="Pricing")
    sync.run(api)
    statuses = dict(Suggestion.objects.values_list("text", "status"))
    assert statuses == {"Preise": "superseded", "Preisliste": "pending"}


def test_redelivery_is_idempotent(api, cloud, article, monkeypatch):
    key = parler.external_key(article)
    cloud.add_result(key, "title", "de", "Preise", source="Pricing")
    monkeypatch.setattr(api, "acknowledge", lambda ids: (_ for _ in ()).throw(APIError(503, "x")))
    with pytest.raises(APIError):
        sync.run(api)
    monkeypatch.undo()
    sync.run(api)
    assert Suggestion.objects.count() == 1
    assert len(cloud.acked) == 1


def test_apply_writes_and_reports_approval_with_edits(api, cloud, article, admin_user):
    key = parler.external_key(article)
    cloud.add_result(key, "title", "de", "Preise", source="Pricing")
    cloud.add_result(
        key, "body", "de", "<p>79 €</p>", source="<p>€79 per site and month</p>", format="html"
    )
    sync.run(api)
    title = Suggestion.objects.get(field="title")

    result = suggestions.apply(Suggestion.objects.all(), admin_user, texts={title.pk: "Preisliste"})

    assert result == (2, 0, 0)
    german = Article.objects.language("de").get(pk=article.pk)
    assert (german.title, german.body) == ("Preisliste", "<p>79 €</p>")
    sync.run(api)
    reviews = {r["id"]: r for r in cloud.reviews}
    assert reviews[title.result_id] == {
        "id": title.result_id,
        "outcome": "approved",
        "text": "Preisliste",
        "reviewer": admin_user.username,
    }
    assert len(reviews) == 2
    sync.run(api)
    assert len(cloud.reviews) == 2  # reported once


def test_a_changed_source_is_not_applied(api, cloud, article, admin_user):
    key = parler.external_key(article)
    cloud.add_result(key, "title", "de", "Preise", source="Pricing")
    sync.run(api)
    article.set_current_language("en")
    article.title = "Prices and plans"
    article.save()

    result = suggestions.apply(Suggestion.objects.all(), admin_user)

    assert result == (0, 1, 0)
    assert Suggestion.objects.get().status == SuggestionStatus.OUTDATED
    assert not Article.objects.filter(translations__language_code="de").exists()


def test_reject_is_reported(api, cloud, article, admin_user):
    cloud.add_result(parler.external_key(article), "title", "de", "Preise", source="Pricing")
    sync.run(api)
    assert suggestions.reject(Suggestion.objects.all(), admin_user) == 1
    sync.run(api)
    assert cloud.reviews == [{"id": Suggestion.objects.get().result_id, "outcome": "rejected"}]


def test_modeltranslation_apply(api, cloud, product, admin_user):
    key = registry.get("modeltranslation").external_key(product)
    cloud.add_result(key, "name", "de", "Tasse", source="Mug")
    sync.run(api)
    suggestions.apply(Suggestion.objects.all(), admin_user)
    product.refresh_from_db()
    assert product.name_de == "Tasse"
    assert product.description_de in (None, "")  # untouched: fallbacks keep working


def test_backfill_and_estimate(article, product):
    SyncState.objects.all().delete()
    assert sync.estimate() == (2, 4, len("Pricing<p>€79 per site and month</p>MugA big mug."))
    assert sync.backfill(["parler"]) == 1
    assert list(SyncState.objects.values_list("adapter", flat=True)) == ["parler"]
