from django.conf import settings
from django.core.checks import Error, Warning, register

from .conf import get_config


@register()
def check_settings(app_configs, **kwargs):
    config = get_config()
    messages = []
    if not config.api_key:
        messages.append(
            Warning(
                "PONYGLOT['API_KEY'] is not set; `manage.py ponyglot sync` can't reach the cloud.",
                hint="Create an API key for this site in the Ponyglot dashboard.",
                id="ponyglot.W001",
            )
        )
    if config.source_language not in config.languages:
        messages.append(
            Error(
                f"The source language {config.source_language!r} isn't one of the site's "
                f"languages {list(config.languages)}.",
                hint="Set PONYGLOT['SOURCE_LANGUAGE'] or add it to LANGUAGES.",
                id="ponyglot.E001",
            )
        )
    if not config.api_url.rstrip("/").endswith("/v1"):
        messages.append(
            Warning(
                f"PONYGLOT['API_URL'] is {config.api_url!r}; the API's address ends in /v1.",
                hint="e.g. https://api.ponyglot.app/v1 (the default). On a cloud with path-based "
                "routing: https://<host>/api/v1.",
                id="ponyglot.W003",
            )
        )
    if not getattr(settings, "USE_I18N", True):
        messages.append(Warning("USE_I18N is off.", id="ponyglot.W002"))
    return messages
