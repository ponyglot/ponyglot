from django.db import models
from parler.models import TranslatableModel, TranslatedFields


class Article(TranslatableModel):
    """A parler model: title/body are synced; slug, status and the excluded note aren't."""

    translations = TranslatedFields(
        title=models.CharField(max_length=200),
        slug=models.SlugField(blank=True),
        body=models.TextField(blank=True),
        internal_note=models.CharField(max_length=100, blank=True),
    )
    status = models.CharField(max_length=10, default="draft")

    def __str__(self):
        return self.safe_translation_getter("title", any_language=True) or "article"

    def get_absolute_url(self):
        return f"/articles/{self.pk}/"


class Product(models.Model):
    """A modeltranslation model (see translation.py)."""

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=12, blank=True)
    description = models.TextField(blank=True)
    sku = models.CharField(max_length=20, blank=True)

    def __str__(self):
        return self.name or "product"


class Plain(models.Model):
    """Not translated by any library: never synced."""

    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name
