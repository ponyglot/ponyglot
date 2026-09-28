from django.apps import AppConfig


class PonyglotModeltranslationConfig(AppConfig):
    name = "ponyglot.contrib.modeltranslation"
    label = "ponyglot_modeltranslation"
    verbose_name = "Ponyglot: django-modeltranslation"

    def ready(self):
        from ponyglot.adapters import registry
        from ponyglot.state import connect_signals

        from .adapter import ModeltranslationAdapter

        connect_signals(registry.register(ModeltranslationAdapter()))
