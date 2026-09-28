"""Translated slugs follow the applied title; hand-written slugs stay."""

import pytest

from ponyglot import suggestions, sync
from ponyglot.adapters import registry
from ponyglot.models import Suggestion
from tests.testapp.models import Article, Product

pytestmark = pytest.mark.django_db

parler = registry.get("parler")
modeltranslation = registry.get("modeltranslation")


def german(article):
    return Article.objects.language("de").get(pk=article.pk)


def apply_title(api, cloud, obj, adapter, text, source, user, field="title"):
    cloud.add_result(adapter.external_key(obj), field, "de", text, source=source)
    sync.run(api)
    suggestions.apply(Suggestion.objects.filter(status="pending"), user)


def test_slugs_are_never_pushed(api, cloud, article):
    sync.run(api)
    unit = cloud.units[parler.external_key(article)]
    assert [s["key"] for s in unit["segments"]] == ["title", "body"]


def test_excluded_slug_is_left_alone(api, cloud, article, admin_user, settings):
    settings.PONYGLOT = {
        **settings.PONYGLOT,
        "EXCLUDE_FIELDS": [*settings.PONYGLOT["EXCLUDE_FIELDS"], "testapp.Article.slug"],
    }
    apply_title(api, cloud, article, parler, "Preise", "Pricing", admin_user)
    assert german(article).slug == ""


def test_empty_slug_is_filled_and_follows_the_title(api, cloud, article, admin_user):
    apply_title(api, cloud, article, parler, "Preise & Pläne", "Pricing", admin_user)
    assert german(article).slug == "preise-plane"

    # The source title changes; the new translation moves the slug along.
    article.set_current_language("en")
    article.title = "Prices"
    article.save()
    apply_title(api, cloud, article, parler, "Preisliste", "Prices", admin_user)
    assert german(article).slug == "preisliste"


def test_hand_written_slug_stays(api, cloud, article, admin_user):
    article.set_current_language("de")
    article.title = "Preise"
    article.slug = "unsere-preise"
    article.save()
    article.set_current_language("en")
    apply_title(api, cloud, article, parler, "Preisliste", "Pricing", admin_user)
    assert (german(article).title, german(article).slug) == ("Preisliste", "unsere-preise")


def test_slug_is_unique_per_language(api, cloud, article, admin_user):
    other = Article()
    other.set_current_language("de")
    other.title = "Andere"
    other.slug = "preise"
    other.save()
    apply_title(api, cloud, article, parler, "Preise", "Pricing", admin_user)
    assert german(article).slug == "preise-2"


def test_explicit_slug_source(api, cloud, article, admin_user, settings):
    settings.PONYGLOT = {**settings.PONYGLOT, "SLUG_SOURCES": {"testapp.Article.slug": "body"}}
    apply_title(api, cloud, article, parler, "Preise", "Pricing", admin_user)
    assert german(article).slug == ""  # follows the body, not the title


def test_modeltranslation_slug_respects_max_length(api, cloud, admin_user):
    Product.objects.create(name_en="Cup", slug_de="riesenbecher")
    product = Product.objects.create(name_en="Mug", description_en="A big mug.")
    apply_title(api, cloud, product, modeltranslation, "Riesenbecher", "Mug", admin_user, "name")
    product.refresh_from_db()
    assert product.slug_de == "riesenbech-2"  # max_length 12, unique among products
    assert "slug" not in [
        s["key"] for s in cloud.units[modeltranslation.external_key(product)]["segments"]
    ]
