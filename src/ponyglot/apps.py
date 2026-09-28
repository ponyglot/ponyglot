from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PonyglotConfig(AppConfig):
    name = "ponyglot"
    verbose_name = _("Ponyglot")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 (registers system checks)
