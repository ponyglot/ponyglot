"""The `PONYGLOT` setting, with defaults.

    PONYGLOT = {
        "API_KEY": env("PONYGLOT_API_KEY"),   # from the dashboard (required)
        "SOURCE_LANGUAGE": "en",              # default: LANGUAGE_CODE
    }

Optional keys:

- `API_URL`: default `https://api.ponyglot.app/v1`.
- `LANGUAGES`: language codes the site can store; default: the codes in `LANGUAGES`.
- `EXCLUDE_MODELS`: `["app_label.ModelName", …]` never synced.
- `EXCLUDE_FIELDS`: `["app_label.ModelName.field", …]` never synced.
- `FIELD_FORMATS`: `{"app_label.ModelName.field": "html"}` overrides the detected format.
- `SLUG_SOURCES`: `{"app_label.ModelName.slug": "title"}`, the field a translated slug follows
  (default: the model's first title-like field). Slugs in `EXCLUDE_FIELDS` aren't touched.
- `TIMEOUT`: seconds per API request (default 30).
- `BATCH_SIZE`: units per push request (default 50, at most 100).
"""

from dataclasses import dataclass

from django.conf import settings

DEFAULT_API_URL = "https://api.ponyglot.app/v1"


@dataclass(frozen=True)
class Config:
    api_key: str
    api_url: str
    source_language: str
    languages: tuple
    exclude_models: frozenset
    exclude_fields: frozenset
    field_formats: dict
    slug_sources: dict
    timeout: float
    batch_size: int


def _label(value):
    return value.lower()


def get_config():
    """The current configuration; read on every call so tests can override settings."""
    options = getattr(settings, "PONYGLOT", {}) or {}
    languages = options.get("LANGUAGES") or [code for code, _ in settings.LANGUAGES]
    return Config(
        api_key=options.get("API_KEY") or "",
        api_url=(options.get("API_URL") or DEFAULT_API_URL).rstrip("/"),
        source_language=options.get("SOURCE_LANGUAGE") or settings.LANGUAGE_CODE,
        languages=tuple(languages),
        exclude_models=frozenset(_label(m) for m in options.get("EXCLUDE_MODELS", ())),
        exclude_fields=frozenset(_label(f) for f in options.get("EXCLUDE_FIELDS", ())),
        field_formats={_label(k): v for k, v in options.get("FIELD_FORMATS", {}).items()},
        slug_sources={_label(k): v for k, v in options.get("SLUG_SOURCES", {}).items()},
        timeout=float(options.get("TIMEOUT", 30)),
        batch_size=max(1, min(100, int(options.get("BATCH_SIZE", 50)))),
    )
