"""Adapters map a site's content to units and segments (API v1) and write translations back.

Every adapter owns an `external_key` prefix (its `name`, e.g. `parler:`). The core registers
the installed adapters, marks their objects dirty when they change, pushes snapshots and hands
each pulled result to the adapter that owns it. `ModelAdapter` covers libraries that keep
translations on model fields (django-parler, django-modeltranslation); django CMS and Wagtail
adapters live in their own packages.
"""

import re
from dataclasses import asdict, dataclass, field

from django.apps import apps
from django.db import models
from django.utils import translation
from django.utils.text import slugify

from .conf import get_config

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
# Rich-text field classes from common editors (djangocms-text, CKEditor, TinyMCE, Wagtail).
HTML_FIELD_CLASSES = {
    "HTMLField",
    "RichTextField",
    "RichTextUploadingField",
    "CKEditor5Field",
    "TinyMCEField",
}
_MARKUP = re.compile(r"^\s*<[a-zA-Z][^>]*>.*</[a-zA-Z0-9]+>\s*$", re.DOTALL)
KIND_HINTS = (
    ("meta_description", "meta_description"),
    ("meta_title", "title"),
    ("seo_title", "title"),
    ("title", "title"),
    ("headline", "title"),
    ("name", "title"),
    ("alt", "alt_text"),
    ("caption", "caption"),
    ("link_text", "link_text"),
    ("description", "body"),
    ("abstract", "body"),
    ("body", "body"),
    ("content", "body"),
    ("text", "body"),
)


@dataclass
class Segment:
    key: str
    text: str
    format: str = "plain"
    field: str = ""
    kind: str = ""
    max_length: int | None = None
    translations: dict | None = None
    # Structure (plugin trees, nested blocks): the parent segment/container and the order.
    parent_key: str = ""
    position: int | None = None

    def as_payload(self, with_translations=True):
        data = {k: v for k, v in asdict(self).items() if v not in (None, "", {})}
        if not with_translations:
            data.pop("translations", None)
        return data


@dataclass
class Unit:
    external_key: str
    adapter: str
    source_language: str
    segments: list = field(default_factory=list)
    label: str = ""
    path: str = ""
    writable_languages: list | None = None
    metadata: dict = field(default_factory=dict)

    def as_payload(self, with_translations=True):
        data = {
            "external_key": self.external_key,
            "adapter": self.adapter,
            "label": self.label[:500],
            "path": self.path[:2000],
            "source_language": self.source_language,
            "writable_languages": self.writable_languages,
            "metadata": self.metadata,
            "segments": [s.as_payload(with_translations) for s in self.segments],
        }
        return {k: v for k, v in data.items() if v is not None}


class Adapter:
    """Base for all adapters; see `ModelAdapter` for model-field libraries."""

    name = ""
    # What the adapter can handle (cloud ADR 0014): "qa_errors" (shows QA errors to editors),
    # "whole_unit" (needs a unit's translations in one piece per language).
    capabilities = frozenset()
    # Status of a suggestion once written: model adapters apply for good ("applied", reported
    # as approved); draft-based adapters write a draft ("drafted": approved on publishing).
    applied_status = "applied"

    def watched_models(self):
        """`{model: owner(instance) -> object or None}`: saves of these models mark the owning
        object dirty; deleting an object of `owner_models()` deletes its unit."""
        return {}

    def owner_models(self):
        return []

    def iter_objects(self):
        """Every object that should be synced (backfill)."""
        return iter(())

    def external_key(self, obj):
        raise NotImplementedError

    def get_object(self, external_key):
        raise NotImplementedError

    def snapshot(self, obj):
        """The unit for `obj` (source language, all segments), or None if it has no source
        content to translate."""
        raise NotImplementedError

    def writable_languages(self):
        return list(get_config().languages)

    def deliver(self, results):
        """Take results (dicts from `/results`); return the ids that may be acknowledged."""
        raise NotImplementedError

    def status_after_write(self, obj):
        """The suggestion status once written to `obj` (see `applied_status`)."""
        return self.applied_status

    def slug_values(self, obj, language, values):
        """Further values to write along with `values` (e.g. derived slugs)."""
        return {}


def detect_format(model_field, value, label):
    """`html` for rich-text fields and markup values, else `plain`; `FIELD_FORMATS` wins."""
    override = get_config().field_formats.get(label.lower())
    if override:
        return override
    if type(model_field).__name__ in HTML_FIELD_CLASSES:
        return "html"
    if isinstance(model_field, models.TextField) and _MARKUP.match(value or ""):
        return "html"
    return "plain"


def kind_for(field_name):
    name = field_name.lower()
    for hint, kind in KIND_HINTS:
        if hint in name:
            return kind
    return ""


def is_translatable_field(model_field):
    """Text fields only; slugs, URLs, emails and choices would break links or data."""
    if not isinstance(model_field, (models.CharField, models.TextField)):
        return False
    if isinstance(model_field, (models.SlugField, models.URLField, models.EmailField)):
        return False
    return not model_field.choices


class ModelAdapter(Adapter):
    """Translations stored on model fields. Subclasses implement the library specifics."""

    capabilities = frozenset({"qa_errors"})  # the suggestion admin shows QA errors

    def models(self):
        """Models the library translates (before exclusions)."""
        raise NotImplementedError

    def translated_field_names(self, model):
        raise NotImplementedError

    def source_value(self, obj, field_name):
        raise NotImplementedError

    def target_value(self, obj, field_name, language):
        raise NotImplementedError

    def target_languages_with_content(self, obj):
        raise NotImplementedError

    def write(self, obj, language, values):
        """Store `{field_name: text}` as `language` and save."""
        raise NotImplementedError

    def slug_taken(self, obj, field_name, language, slug):
        """Whether another object already uses `slug` in `language`."""
        raise NotImplementedError

    def form_field_name(self, form, field_name, language):
        """The admin form field that edits `field_name` in `language`, or None if `form` doesn't
        (suggestions are shown next to it on the change page)."""
        return None

    # --- Common behaviour ----------------------------------------------------------------

    def synced_models(self):
        excluded = get_config().exclude_models
        return [
            model
            for model in self.models()
            if model._meta.label_lower not in excluded and self.fields(model)
        ]

    def owner_models(self):
        return self.synced_models()

    def fields(self, model):
        """`{field_name: model field}` of the translatable text fields that are synced, in
        declaration order (libraries may keep them in sets; the order must be stable)."""
        excluded = get_config().exclude_fields
        result = {}
        for name in sorted(self.translated_field_names(model), key=self._order_key(model)):
            model_field = self._model_field(model, name)
            if model_field is None or not is_translatable_field(model_field):
                continue
            if f"{model._meta.label_lower}.{name}" in excluded:
                continue
            result[name] = model_field
        return result

    def slug_fields(self, model):
        """`{slug field name: (model field, source field name)}` of translated slugs.

        Slugs aren't sent to the cloud (engines translate `delta-sync-explained` poorly);
        they're derived from the applied title instead (`slug_values`). A slug is filled when
        empty and follows its title while it was derived from it; hand-written slugs stay.
        """
        config = get_config()
        synced = self.fields(model)
        titles = [name for name in synced if kind_for(name) == "title"]
        result = {}
        for name in self.translated_field_names(model):
            model_field = self._model_field(model, name)
            label = f"{model._meta.label_lower}.{name}"
            if not isinstance(model_field, models.SlugField) or label in config.exclude_fields:
                continue
            source = config.slug_sources.get(label) or (titles[0] if titles else None)
            if source in synced:
                result[name] = (model_field, source)
        return result

    def slug_values(self, obj, language, values):
        """Slugs to write along with `values` (`{field: text}` about to be applied)."""
        updates = {}
        for name, (model_field, source) in self.slug_fields(type(obj)).items():
            if source not in values:
                continue
            allow_unicode = getattr(model_field, "allow_unicode", False)
            current = self.target_value(obj, name, language) or ""
            old_title = self.target_value(obj, source, language) or ""
            if current and current != slugify(old_title, allow_unicode=allow_unicode):
                continue  # written by hand: keep it
            slug = self._unique_slug(obj, name, language, model_field, values[source])
            if slug and slug != current:
                updates[name] = slug
        return updates

    def _unique_slug(self, obj, name, language, model_field, text):
        allow_unicode = getattr(model_field, "allow_unicode", False)
        max_length = model_field.max_length or 50
        base = slugify(text, allow_unicode=allow_unicode)[:max_length].strip("-")
        if not base:
            return ""
        slug, number = base, 2
        while self.slug_taken(obj, name, language, slug):
            suffix = f"-{number}"
            slug = base[: max_length - len(suffix)].rstrip("-") + suffix
            number += 1
        return slug

    def _order_key(self, model):
        def key(name):
            model_field = self._model_field(model, name)
            return (getattr(model_field, "creation_counter", 0), name)

        return key

    def _model_field(self, model, name):
        try:
            return model._meta.get_field(name)
        except Exception:  # noqa: BLE001 (library-specific proxies)
            return None

    def iter_objects(self):
        for model in self.synced_models():
            yield from model._default_manager.order_by("pk").iterator()

    def external_key(self, obj):
        return f"{self.name}:{obj._meta.label_lower}:{obj.pk}"

    def get_object(self, external_key):
        try:
            prefix, label, pk = external_key.split(":", 2)
            model = apps.get_model(label)
        except (ValueError, LookupError):
            return None
        if prefix != self.name or model not in self.synced_models():
            return None
        return model._default_manager.filter(pk=pk).first()

    def field_label(self, obj, field_name):
        return f"{obj._meta.label_lower}.{field_name}"

    def field_format(self, obj, field_name, value=None):
        model_field = self.fields(type(obj))[field_name]
        if value is None:
            value = self.source_value(obj, field_name)
        return detect_format(model_field, value, self.field_label(obj, field_name))

    def snapshot(self, obj):
        source = get_config().source_language
        segments = []
        other_languages = [
            language
            for language in self.target_languages_with_content(obj)
            if language != source and language in self.writable_languages()
        ]
        for name, model_field in self.fields(type(obj)).items():
            text = self.source_value(obj, name)
            if not text or not str(text).strip():
                continue
            text = str(text)
            translations = {}
            for language in other_languages:
                value = self.target_value(obj, name, language)
                if value and str(value).strip():
                    translations[language] = str(value)
            segments.append(
                Segment(
                    key=name,
                    text=text,
                    format=detect_format(model_field, text, self.field_label(obj, name)),
                    field=name,
                    kind=kind_for(name),
                    max_length=getattr(model_field, "max_length", None),
                    translations=translations or None,
                )
            )
        if not segments:
            return None
        return Unit(
            external_key=self.external_key(obj),
            adapter=self.name,
            source_language=source,
            segments=segments,
            label=self.label(obj),
            path=self.path(obj),
            writable_languages=self.unit_writable_languages(),
            metadata={"model": obj._meta.label_lower},
        )

    def unit_writable_languages(self):
        """Languages this unit can store, if the adapter restricts them (None: all)."""
        return None

    def label(self, obj):
        with translation.override(get_config().source_language):
            return str(obj)

    def path(self, obj):
        get_absolute_url = getattr(obj, "get_absolute_url", None)
        if get_absolute_url is None:
            return ""
        try:
            with translation.override(get_config().source_language):
                return get_absolute_url() or ""
        except Exception:  # noqa: BLE001 (a broken get_absolute_url must not stop the sync)
            return ""

    def deliver(self, results):
        from .suggestions import store_results

        return store_results(self, results)


class Registry:
    def __init__(self):
        self._adapters = {}

    def register(self, adapter):
        if not KEY_PATTERN.match(f"{adapter.name}:x"):
            raise ValueError(f"Invalid adapter name {adapter.name!r}")
        self._adapters[adapter.name] = adapter
        return adapter

    def unregister(self, name):
        self._adapters.pop(name, None)

    def get(self, name):
        return self._adapters.get(name)

    def for_key(self, external_key):
        return self._adapters.get(external_key.split(":", 1)[0])

    def all(self):
        return list(self._adapters.values())


registry = Registry()
