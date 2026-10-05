"""The documents that need attention (Translation dashboard › Documents)."""

import pytest
from django.contrib.auth.models import Permission, User

from ponyglot import sync
from ponyglot.client import Client
from ponyglot.models import Suggestion, SyncState

pytestmark = pytest.mark.django_db

URL = "/admin/ponyglot/translationdashboard/documents/"


@pytest.fixture
def cloud_docs(cloud, api, monkeypatch, product, article):
    for module in ("overview", "translations_admin"):
        monkeypatch.setattr(f"ponyglot.{module}.Client", lambda: Client(transport=cloud))
    sync.run(api)  # both objects are in the cloud, nothing translated
    return cloud


def keys(article=None, product=None):
    return (
        f"parler:testapp.article:{article.pk}" if article else None,
        f"modeltranslation:testapp.product:{product.pk}" if product else None,
    )


def all_ok(cloud, key, language):
    for segment in cloud.units[key]["segments"]:
        cloud.states[(key, segment["key"], language)] = "ok"


def test_lists_documents_with_work_per_language(admin_client, cloud_docs, product, article):
    article_key, product_key = keys(article, product)
    for language in ("de", "fr"):
        all_ok(cloud_docs, product_key, language)
    cloud_docs.states[(article_key, "title", "fr")] = "attention"
    html = admin_client.get(URL).content.decode()
    assert "Pricing" in html and "Mug" not in html  # the product is up to date
    assert f'href="/admin/testapp/article/{article.pk}/ponyglot/"' in html  # its dialog
    assert "2 missing" in html  # German: title and body
    assert "1 held by QA" in html and "attention/?unit=" in html  # French title, links out


def test_waiting_suggestions_link_to_the_review(admin_client, api, cloud_docs, article):
    article_key, _ = keys(article)
    cloud_docs.add_result(article_key, "title", "de", "Preise", source="Pricing")
    sync.run(api)
    cloud_docs.states[(article_key, "title", "de")] = "review"
    html = admin_client.get(URL).content.decode()
    assert "1 to apply" in html and "/admin/ponyglot/suggestion/?external_key=" in html


def test_filters_by_content_type_and_language(admin_client, cloud_docs, product, article):
    article_key, product_key = keys(article, product)
    html = admin_client.get(f"{URL}?prefix=modeltranslation:testapp.product:").content.decode()
    assert "Mug" in html and "Pricing" not in html
    html = admin_client.get(f"{URL}?language=fr").content.decode()
    assert "Pricing" in html and "Mug" not in html  # products have no French columns
    assert ">French <" in html and ">German <" not in html
    assert ("GET", "/units", None) in cloud_docs.requests


def test_bulk_translate_and_apply(admin_client, api, cloud_docs, product, article):
    article_key, product_key = keys(article, product)
    response = admin_client.post(
        f"{URL}?language=de", {"action": "translate", "keys": [article_key]}, follow=True
    )
    assert "Translation requested for 1 document" in response.content.decode()
    job = list(cloud_docs.jobs.values())[-1]
    assert (job["type"], job["units"], job["languages"]) == ("translate", [article_key], ["de"])

    cloud_docs.add_result(product_key, "name", "de", "Becher", source="Mug")
    sync.run(api)
    response = admin_client.post(
        URL, {"action": "apply", "keys": [product_key, "nonsense:key"]}, follow=True
    )
    assert "1 translation applied." in response.content.decode()
    product.refresh_from_db()
    assert product.name_de == "Becher"
    assert not Suggestion.objects.filter(status="pending").exists()


def test_large_translations_need_confirmation(admin_client, cloud_docs, article):
    article_key, _ = keys(article)
    cloud_docs.confirm_above = 0
    response = admin_client.post(URL, {"action": "translate", "keys": [article_key]})
    html = response.content.decode()
    assert "needs your confirmation" in html and 'name="confirm"' in html
    assert not cloud_docs.jobs
    admin_client.post(URL, {"action": "translate", "keys": [article_key], "confirm": "1"})
    assert list(cloud_docs.jobs.values())[-1]["units"] == [article_key]


def test_unsent_and_failed_documents_are_counted(admin_client, cloud_docs, article):
    SyncState.objects.update(dirty=True)
    SyncState.objects.filter(external_key=keys(article)[0]).update(error="HTTP 422: invalid")
    html = admin_client.get(URL).content.decode()
    assert "1 document couldn" in html and "2 changes are sent with the next sync." in html


def test_needs_the_dashboard_permission(client, cloud_docs, article):
    staff = User.objects.create_user("editor", password="x", is_staff=True)
    client.force_login(staff)
    assert client.get(URL).status_code == 403
    assert client.post(URL, {"action": "translate", "keys": [keys(article)[0]]}).status_code == 403
    assert not cloud_docs.jobs

    staff.user_permissions.add(Permission.objects.get(codename="use_dashboard"))
    html = client.get(URL).content.decode()
    assert "Pricing" in html
    assert 'value="apply"' not in html  # applying needs the suggestion permission
    dashboard = client.get("/admin/ponyglot/translationdashboard/").content.decode()
    assert URL in dashboard and f"{URL}?prefix=parler%3Atestapp.article%3A" in dashboard


def test_cloud_errors_are_shown(admin_client, cloud_docs):
    cloud_docs.fail_next = [401]
    html = admin_client.get(URL).content.decode()
    assert "errornote" in html and "HTTP 401" in html
