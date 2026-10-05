"""Translation dialogs in the admin: one object, all objects of a model, the dashboard."""

import re

import pytest
from django.contrib.auth.models import Permission, User

from ponyglot import sync
from ponyglot.client import Client
from ponyglot.models import Suggestion, SyncRun

pytestmark = pytest.mark.django_db


@pytest.fixture
def dialogs_cloud(cloud, monkeypatch):
    monkeypatch.setattr("ponyglot.translations_admin.Client", lambda: Client(transport=cloud))
    return cloud


def translate(cloud, key, language="de", text="Becher"):
    for segment in cloud.units[key]["segments"]:
        cloud.add_result(
            key, segment["key"], language, f"{text} {segment['key']}", source=segment["text"]
        )


def footer(html):
    """The submit row as django CMS' modal sees it: buttons only, each with its form."""
    row = html.partition('<div class="submit-row">')[2].partition("</div>")[0]
    assert "<input" not in row  # a hidden input there stops the CMS from collecting buttons
    return re.findall(r'<button[^>]*form="([^"]+)"[^>]*>\s*([^<]+?)\s*</button>', row)


def product_url(product, suffix=""):
    return f"/admin/testapp/product/{product.pk}/{suffix}"


def test_change_form_and_list_have_the_button(admin_client, dialogs_cloud, product, article):
    html = admin_client.get(product_url(product, "change/")).content.decode()
    assert f'href="{product_url(product, "ponyglot/")}"' in html and "Ponyglot translations" in html
    assert "modeltranslation" in html  # still the tabbed admin's form
    html = admin_client.get(f"/admin/testapp/article/{article.pk}/change/").content.decode()
    assert "Ponyglot translations" in html
    html = admin_client.get("/admin/testapp/product/").content.decode()
    assert 'href="/admin/testapp/product/ponyglot/"' in html


def test_object_dialog_translate_fetch_apply(admin_client, dialogs_cloud, api, product):
    html = admin_client.get(product_url(product, "ponyglot/")).content.decode()
    # Nothing fetched lately: opening it sent the product (saved: changed) and fetched.
    assert "2 texts missing or outdated" in html
    assert footer(html) == []  # one language: its own "Translate" button; fetched just now
    assert f'action="{product_url(product, "ponyglot/translate/")}"' in html

    response = admin_client.post(product_url(product, "ponyglot/translate/"), follow=True)
    assert "Translation requested" in response.content.decode()
    key = f"modeltranslation:testapp.product:{product.pk}"
    assert key in dialogs_cloud.units  # sent first

    translate(dialogs_cloud, key)
    SyncRun.objects.all().delete()  # nothing fetched lately: opening the dialog fetches
    html = admin_client.get(product_url(product, "ponyglot/")).content.decode()
    assert "new translations were fetched" in html and "2 translations to apply" in html
    assert "Fetch translations now" not in html  # just fetched

    response = admin_client.post(
        product_url(product, "ponyglot/apply/"), {"language": "de"}, follow=True
    )
    assert "2 translations applied." in response.content.decode()
    product.refresh_from_db()
    assert product.name_de == "Becher name"
    assert not Suggestion.objects.filter(status="pending").exists()


def test_model_dialog_covers_all_objects(admin_client, dialogs_cloud, api, product):
    from tests.testapp.models import Product

    other = Product.objects.create(name_en="Cup", sku="CUP-1")
    sync.backfill()
    sync.run(api)
    html = admin_client.get("/admin/testapp/product/ponyglot/").content.decode()
    assert "Translations of all products" in html
    assert "3 texts missing or outdated" in html  # the mug's name and description, the cup's name
    response = admin_client.post("/admin/testapp/product/ponyglot/translate/", follow=True)
    assert "Translation requested" in response.content.decode()
    job = list(dialogs_cloud.jobs.values())[-1]
    assert (job["type"], job["prefix"]) == ("delta", "modeltranslation:testapp.product:")

    for obj in (product, other):
        translate(dialogs_cloud, f"modeltranslation:testapp.product:{obj.pk}")
    admin_client.post("/admin/testapp/product/ponyglot/fetch/")
    response = admin_client.post(
        "/admin/testapp/product/ponyglot/apply/", {"language": "de"}, follow=True
    )
    assert "translations applied." in response.content.decode()
    other.refresh_from_db()
    assert other.name_de == "Becher name"


def test_dashboard_needs_its_permission(client, admin_client, dialogs_cloud, api, product):
    staff = User.objects.create_user("editor", password="x", is_staff=True)
    client.force_login(staff)
    assert client.get("/admin/ponyglot/translationdashboard/").status_code == 403
    assert "translation dashboard" not in client.get("/admin/").content.decode().lower()

    staff.user_permissions.add(Permission.objects.get(codename="use_dashboard"))
    html = client.get("/admin/ponyglot/translationdashboard/").content.decode()
    assert "Translation dashboard" in html and "No sync has run yet." in html
    assert ("ponyglot-fetch", "Sync now") in footer(html)
    assert "No sync ran lately." in html and "manage.py" not in html
    assert "Products" in html  # content types

    client.post("/admin/ponyglot/translationdashboard/fetch/")
    assert SyncRun.objects.filter(unit="").exists()  # a site-wide round
    response = client.post("/admin/ponyglot/translationdashboard/translate/", follow=True)
    assert "Translation requested" in response.content.decode()
    assert list(dialogs_cloud.jobs.values())[-1]["type"] == "delta"


def test_dialogs_need_change_permission(client, dialogs_cloud, product):
    staff = User.objects.create_user("viewer", password="x", is_staff=True)
    client.force_login(staff)
    assert client.get(product_url(product, "ponyglot/")).status_code == 403
    assert client.get("/admin/testapp/product/ponyglot/").status_code == 403


def test_unsent_objects_are_sent_when_translating(admin_client, dialogs_cloud, product):
    from django.utils import timezone

    from ponyglot.models import SyncState

    # A sync ran lately: the dialog doesn't send or fetch anything by itself.
    SyncRun.objects.create(unit="", ok=True, finished_at=timezone.now())
    SyncState.objects.all().delete()  # as if the product was never sent
    html = admin_client.get(product_url(product, "ponyglot/")).content.decode()
    assert "Not sent to Ponyglot yet." in html
    assert ("ponyglot-translate", "Send for translation") in footer(html)
    admin_client.post(product_url(product, "ponyglot/translate/"))
    assert f"modeltranslation:testapp.product:{product.pk}" in dialogs_cloud.units


# --- Suggestions on the change form ------------------------------------------------------


def suggest(api, cloud, key, field, text, source, language="de", qa=None):
    cloud.add_result(key, field, language, text, source=source, qa=qa)
    sync.run(api)
    return Suggestion.objects.get(external_key=key, field=field, language=language)


def form_suggestions(response):
    data = response.context_data.get("ponyglot_form")
    return {s["input"]: s for s in data["suggestions"]} if data else {}


def form_post(response, **changes):
    """The change form's data as the browser would send it, with `changes`."""
    form = response.context_data["adminform"].form
    data = {name: form[name].value() or "" for name in form.fields}
    data.update(changes)
    return {k: v for k, v in data.items() if v is not None}


def test_change_form_shows_suggestions_modeltranslation(admin_client, api, cloud, product):
    key = f"modeltranslation:testapp.product:{product.pk}"
    sync.run(api)
    name = suggest(api, cloud, key, "name", "Becher", source="Mug")
    suggest(api, cloud, key, "description", "Ein großer Becher.", source="A big mug.")

    response = admin_client.get(product_url(product, "change/"))
    shown = form_suggestions(response)
    assert set(shown) == {"name_de", "description_de"}
    assert shown["name_de"]["slugs"] == {"slug_de": "becher"}  # the title brings its slug
    html = response.content.decode()
    assert 'id="ponyglot-suggestions"' in html and "change_form_suggestions.js" in html

    # "Apply" filled the fields; saving records the suggestion as applied.
    data = form_post(response, name_de="Becher", slug_de="becher", _ponyglot_apply=name.pk)
    response = admin_client.post(product_url(product, "change/"), data, follow=True)
    assert "1 translation suggestion applied." in response.content.decode()
    product.refresh_from_db()
    assert (product.name_de, product.slug_de) == ("Becher", "becher")
    name.refresh_from_db()
    assert (name.status, name.final_text) == ("applied", "")
    assert Suggestion.objects.filter(status="pending").count() == 1  # description: untouched


def test_change_form_suggestions_parler_follow_the_language_tab(admin_client, api, cloud, article):
    key = f"parler:testapp.article:{article.pk}"
    sync.run(api)
    title = suggest(api, cloud, key, "title", "Preise", source="Pricing")
    url = f"/admin/testapp/article/{article.pk}/change/"

    assert form_suggestions(admin_client.get(url)) == {}  # the English tab
    response = admin_client.get(f"{url}?language=de")
    assert set(form_suggestions(response)) == {"title"}

    # The editor changed the text after applying it: the saved text is what was applied.
    data = form_post(response, title="Preise und Pakete\r\n", _ponyglot_apply=title.pk)
    admin_client.post(f"{url}?language=de", data)
    title.refresh_from_db()
    assert (title.status, title.final_text) == ("applied", "Preise und Pakete")


def test_change_form_rejects_and_skips_outdated(admin_client, api, cloud, product):
    key = f"modeltranslation:testapp.product:{product.pk}"
    sync.run(api)
    name = suggest(api, cloud, key, "name", "Becher", source="Mug")
    old = suggest(api, cloud, key, "description", "Alt.", source="An old description.")

    response = admin_client.get(product_url(product, "change/"))
    assert set(form_suggestions(response)) == {"name_de"}  # translated from another source

    data = form_post(response, _ponyglot_reject=name.pk, _ponyglot_apply=old.pk)
    response = admin_client.post(product_url(product, "change/"), data, follow=True)
    assert "1 translation suggestion rejected." in response.content.decode()
    name.refresh_from_db()
    old.refresh_from_db()
    assert (name.status, old.status) == ("rejected", "outdated")


def test_change_form_suggestions_need_the_permission(client, api, cloud, product):
    key = f"modeltranslation:testapp.product:{product.pk}"
    sync.run(api)
    name = suggest(api, cloud, key, "name", "Becher", source="Mug")
    user = User.objects.create_user("editor", password="x", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename="change_product"))
    client.force_login(user)

    response = client.get(product_url(product, "change/"))
    assert form_suggestions(response) == {}
    client.post(
        product_url(product, "change/"),
        form_post(response, name_de="Becher", _ponyglot_apply=name.pk),
    )
    name.refresh_from_db()
    assert name.status == "pending"
