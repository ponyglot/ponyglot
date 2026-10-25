"""The documents that need attention (Translation dashboard › Documents)."""

import pytest
from django.contrib.auth.models import Permission, User

from ponyglot import sync
from ponyglot.adapters import Adapter, registry
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


class Pages(Adapter):
    """A page-like content system (as django CMS or Wagtail would be)."""

    def __init__(self, name, order):
        self.name, self.overview_order = name, order

    def translation_url(self, external_key):
        return f"/admin/{self.name}/{external_key.rsplit(':', 1)[1]}/"


@pytest.fixture
def page_systems(cloud_docs):
    systems = [Pages("wagtail", 20), Pages("djangocms", 10)]
    for system in systems:
        registry.register(system)
    for name, label in (("wagtail", "Blog"), ("djangocms", "Home"), ("djangocms", "About")):
        key = f"{name}:page:{label.lower()}"
        cloud_docs.units[key] = {"label": label, "segments": [{"key": "title", "text": label}]}
    yield cloud_docs
    for system in systems:
        registry.unregister(system.name)


def test_dashboard_links_the_documents_and_folds_history(admin_client, page_systems):
    html = admin_client.get("/admin/ponyglot/translationdashboard/").content.decode()
    order = [
        html.index(text)
        for text in ("German", f'href="{URL}"', "Content types", '<details class="history">')
    ]
    assert order == sorted(order)  # languages, the documents link, models, history
    assert "/admin/djangocms/home/" not in html  # no second, partial list of documents
    assert '<details class="history" open' not in html


def test_page_systems_come_first_in_the_filter(admin_client, page_systems):
    html = admin_client.get(URL).content.decode()
    order = [
        html.index(f'value="{prefix}"')
        for prefix in ("djangocms:", "wagtail:", "modeltranslation:testapp.product:")
    ]
    assert order == sorted(order)
    assert html.index("/admin/djangocms/home/") < html.index("/admin/wagtail/blog/")


def test_pages_run_through_the_systems_in_order(admin_client, page_systems, monkeypatch):
    import html as html_lib
    import re

    monkeypatch.setattr("ponyglot.overview.PAGE_SIZE", 2)
    labels, url = [], URL
    while url:
        html = admin_client.get(url).content.decode()
        labels += re.findall(r'name="keys" value="[^"]+" aria-label="([^"]+)"', html)
        found = re.search(r'<a href="([^"]+)">Next page', html)
        url = URL + html_lib.unescape(found.group(1)) if found else ""
    assert labels[:3] == ["Home", "About", "Blog"]  # django CMS, Wagtail, then the models
    assert len(labels) == 5


def test_works_without_the_django_admin(rf, settings, api, cloud_docs, article):
    """Other admins (wagtail-ponyglot) reuse the overview: no links into a Django admin."""
    from ponyglot import overview
    from ponyglot.translations_admin import Scope

    article_key, _ = keys(article)
    cloud_docs.add_result(article_key, "title", "de", "Preise", source="Pricing")
    sync.run(api)
    cloud_docs.states[(article_key, "title", "de")] = "review"
    settings.ROOT_URLCONF = "tests.urls_without_admin"
    request = rf.get("/")
    request.user = User.objects.create_superuser("root", "root@example.com", "pw")
    context = overview.context(request)
    cell = next(row for row in context["rows"] if row["key"] == article_key)["cells"][0]
    assert (cell["to_apply"], cell["apply_url"]) == (1, "")
    assert context["failed_url"] == ""
    assert Scope(title="Site").review_url("de") == ""


def test_selected_documents_can_be_published_right_away(admin_client, cloud_docs, article):
    """ADR 0022: whether the editor may publish each one is checked when it arrives."""
    article_key, _ = keys(article)
    assert "Publish the translation right away" in admin_client.get(URL).content.decode()
    cloud_docs.confirm_above = 0
    page = admin_client.post(URL, {"action": "translate", "keys": [article_key], "publish": "1"})
    assert 'name="publish" value="1"' in page.content.decode()
    admin_client.post(
        URL, {"action": "translate", "keys": [article_key], "publish": "1", "confirm": "1"}
    )
    [job] = cloud_docs.jobs.values()
    assert (job["publish"], job["publish_as"]) == (True, str(User.objects.get().pk))
