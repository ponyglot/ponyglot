"""Model adapters: discovery, snapshots and writing (django-parler, django-modeltranslation)."""

import pytest

from ponyglot.adapters import kind_for, registry
from ponyglot.models import SyncState
from tests.testapp.models import Article, Plain, Product

pytestmark = pytest.mark.django_db

parler = registry.get("parler")
modeltranslation = registry.get("modeltranslation")


def test_models_are_discovered():
    assert parler.synced_models() == [Article]
    assert modeltranslation.synced_models() == [Product]
    assert Plain not in parler.synced_models() + modeltranslation.synced_models()


def test_only_translatable_text_fields_are_synced():
    # slug (would change URLs) and the field in EXCLUDE_FIELDS are skipped
    assert list(parler.fields(Article)) == ["title", "body"]
    assert list(modeltranslation.fields(Product)) == ["name", "description"]


def test_parler_snapshot(article):
    article.set_current_language("de")
    article.title = "Preise"
    article.save()

    unit = parler.snapshot(Article.objects.get(pk=article.pk))

    assert unit.external_key == f"parler:testapp.article:{article.pk}"
    assert (unit.label, unit.path, unit.source_language) == (
        "Pricing",
        f"/articles/{article.pk}/",
        "en",
    )
    title, body = unit.segments
    assert (title.key, title.text, title.format, title.kind, title.max_length) == (
        "title",
        "Pricing",
        "plain",
        "title",
        200,
    )
    assert title.translations == {"de": "Preise"}
    assert (body.format, body.kind, body.translations) == ("html", "body", None)
    assert unit.writable_languages is None


def test_parler_object_without_source_content_has_no_unit(db):
    article = Article()
    article.set_current_language("de")
    article.title = "Nur Deutsch"
    article.save()
    assert parler.snapshot(article) is None


def test_modeltranslation_snapshot_limits_writable_languages(product):
    product.name_de = "Tasse"
    product.save()
    unit = modeltranslation.snapshot(product)
    assert unit.external_key == f"modeltranslation:testapp.product:{product.pk}"
    assert unit.writable_languages == ["de"]  # fr has no columns
    assert unit.segments[0].translations == {"de": "Tasse"}


def test_objects_are_found_by_key(article, product):
    assert parler.get_object(parler.external_key(article)) == article
    assert modeltranslation.get_object(modeltranslation.external_key(product)) == product
    assert parler.get_object("parler:testapp.article:999") is None
    assert parler.get_object("parler:testapp.plain:1") is None  # not a synced model
    assert parler.get_object("modeltranslation:testapp.product:1") is None
    assert parler.get_object("garbage") is None


def test_write(article, product):
    parler.write(article, "de", {"title": "Preise", "body": "<p>79 €</p>"})
    article = Article.objects.language("de").get(pk=article.pk)
    assert (article.title, article.body) == ("Preise", "<p>79 €</p>")

    modeltranslation.write(product, "de", {"name": "Tasse"})
    product.refresh_from_db()
    assert (product.name_de, product.name_en) == ("Tasse", "Mug")


def test_saving_marks_the_unit_dirty(article, product):
    keys = set(SyncState.objects.filter(dirty=True).values_list("external_key", flat=True))
    assert keys == {parler.external_key(article), modeltranslation.external_key(product)}

    SyncState.objects.update(dirty=False)
    article.set_current_language("en")
    article.title = "Prices"
    article.save_translations()  # only the translation row: still marks the article
    state = SyncState.objects.get(external_key=parler.external_key(article))
    assert state.dirty and state.version > 1

    key = parler.external_key(article)
    article.delete()
    assert SyncState.objects.get(external_key=key).deleted


def test_kind_hints():
    assert kind_for("meta_description") == "meta_description"
    assert kind_for("seo_title") == "title"
    assert kind_for("image_alt") == "alt_text"
    assert kind_for("sku") == ""


def test_field_format_override(settings, article):
    settings.PONYGLOT = {**settings.PONYGLOT, "FIELD_FORMATS": {"testapp.Article.title": "html"}}
    assert parler.snapshot(article).segments[0].format == "html"
