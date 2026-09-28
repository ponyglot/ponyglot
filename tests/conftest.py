import pytest

from ponyglot.client import Client
from ponyglot.testing import FakeCloud


@pytest.fixture
def cloud():
    return FakeCloud()


@pytest.fixture
def api(cloud, monkeypatch):
    monkeypatch.setattr("ponyglot.client.time.sleep", lambda seconds: None)
    return Client(transport=cloud)


@pytest.fixture
def article(db):
    from tests.testapp.models import Article

    article = Article(status="draft")
    article.set_current_language("en")
    article.title = "Pricing"
    article.slug = "pricing"
    article.body = "<p>€79 per site and month</p>"
    article.internal_note = "do not sync"
    article.save()
    return article


@pytest.fixture
def product(db):
    from tests.testapp.models import Product

    return Product.objects.create(name_en="Mug", description_en="A big mug.", sku="MUG-1")
