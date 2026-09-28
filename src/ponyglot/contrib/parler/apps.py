from django.apps import AppConfig


class PonyglotParlerConfig(AppConfig):
    name = "ponyglot.contrib.parler"
    label = "ponyglot_parler"
    verbose_name = "Ponyglot: django-parler"

    def ready(self):
        from ponyglot.adapters import registry
        from ponyglot.state import connect_signals

        from .adapter import ParlerAdapter

        connect_signals(registry.register(ParlerAdapter()))
