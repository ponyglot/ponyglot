from django.contrib import admin
from modeltranslation.admin import TabbedTranslationAdmin
from parler.admin import TranslatableAdmin

from ponyglot.translations_admin import PonyglotAdminMixin

from .models import Article, Product


@admin.register(Article)
class ArticleAdmin(PonyglotAdminMixin, TranslatableAdmin):
    pass


@admin.register(Product)
class ProductAdmin(PonyglotAdminMixin, TabbedTranslationAdmin):
    pass
