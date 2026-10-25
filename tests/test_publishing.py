"""Publishing right away on the requesting editor's behalf (cloud ADR 0022): model adapters."""

import pytest
from django.contrib.auth.models import Permission, User

from ponyglot import sync
from ponyglot.adapters import registry
from ponyglot.client import Client
from ponyglot.models import Suggestion, SuggestionStatus
from tests.testapp.models import Article

pytestmark = pytest.mark.django_db

parler = registry.get("parler")
modeltranslation = registry.get("modeltranslation")


def editor(*codenames, active=True):
    user = User.objects.create_user("ben", password="x", is_staff=True, is_active=active)
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return user


def test_published_as_the_editor_and_reported(api, cloud, product):
    ben = editor("change_product", "change_suggestion")
    key = modeltranslation.external_key(product)
    cloud.add_result(key, "name", "de", "Tasse", source="Mug", publish_as=ben.pk)
    cloud.add_result(key, "description", "de", "Groß.", source="A big mug.", publish_as=ben.pk)

    report = sync.run(api)

    product.refresh_from_db()
    assert (product.name_de, product.description_de) == ("Tasse", "Groß.")
    assert set(Suggestion.objects.values_list("status", "decided_by")) == {("published", ben.pk)}
    assert not report.warnings
    sync.run(api)
    assert {(r["outcome"], r["reviewer"]) for r in cloud.reviews} == {("published", "ben")}


@pytest.mark.parametrize(
    "case, reason",
    [
        ("no permission", "may not publish"),
        ("inactive", "no longer exists or is inactive"),
        ("qa error", "quality check found an error"),
        ("mixed", "not every translation"),
    ],
)
def test_otherwise_it_waits_as_a_suggestion(api, cloud, article, case, reason):
    codenames = () if case == "no permission" else ("change_article", "change_suggestion")
    ben = editor(*codenames, active=case != "inactive")
    key = parler.external_key(article)
    qa = [{"code": "links", "severity": "error", "message": "x"}] if case == "qa error" else None
    cloud.add_result(key, "title", "de", "Preise", source="Pricing", publish_as=ben.pk, qa=qa)
    body = "<p>€79 per site and month</p>"
    cloud.add_result(
        key,
        "body",
        "de",
        "<p>79 €</p>",
        source=body,
        format="html",
        publish_as="" if case == "mixed" else ben.pk,
    )

    report = sync.run(api)

    assert set(Suggestion.objects.values_list("status", flat=True)) == {SuggestionStatus.PENDING}
    assert not Article.objects.filter(translations__language_code="de").exists()
    [warning] = report.warnings
    assert warning.startswith(f"Not published: {key} [de]") and reason in warning


def test_the_object_dialog_offers_it_to_editors_who_may_publish(
    client, admin_client, cloud, monkeypatch, product, settings
):
    monkeypatch.setattr("ponyglot.translations_admin.Client", lambda: Client(transport=cloud))
    url = f"/admin/testapp/product/{product.pk}/ponyglot/"
    html = admin_client.get(url).content.decode()
    assert "Publish the translation right away" in html
    assert 'name="publish" value=""' in html  # not ticked by default
    settings.PONYGLOT = {**settings.PONYGLOT, "PUBLISH_BY_DEFAULT": True}
    assert 'name="publish" value="1"' in admin_client.get(url).content.decode()

    admin_client.post(f"{url}translate/", {"languages": ["de"], "publish": "1"})
    [job] = cloud.jobs.values()
    admin = User.objects.get(username="admin")
    assert (job["publish"], job["publish_as"]) == (True, str(admin.pk))
    assert (
        "Publish the translation right away"
        not in admin_client.get("/admin/testapp/product/ponyglot/").content.decode()
    )  # all objects of the model: not offered

    # May open the dialog (change the product) but not apply suggestions: not offered, and a
    # forged request doesn't publish.
    client.force_login(editor("change_product"))
    assert "Publish the translation right away" not in client.get(url).content.decode()
    client.post(f"{url}translate/", {"languages": ["de"], "publish": "1"})
    assert cloud.jobs[2]["publish"] is False


def test_large_translations_keep_the_choice_through_the_confirmation(
    admin_client, cloud, monkeypatch, product
):
    monkeypatch.setattr("ponyglot.translations_admin.Client", lambda: Client(transport=cloud))
    cloud.confirm_above = 1
    url = f"/admin/testapp/product/{product.pk}/ponyglot/translate/"
    page = admin_client.post(url, {"languages": ["de"], "publish": "1"}).content.decode()
    assert 'name="publish" value="1"' in page and not cloud.jobs
    admin_client.post(url, {"languages": ["de"], "publish": "1", "confirm": "1"})
    assert cloud.jobs[1]["publish"] is True
