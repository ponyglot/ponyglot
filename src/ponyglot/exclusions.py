"""Keeping content out of translation (ADR 0002): for all languages or some.

Adapters and UIs (the admin, the django CMS toolbar) call `exclude` and `include`; the next
sync applies the change: a fully excluded unit is removed from the cloud, a partly excluded one
gets fewer writable languages.
"""

from django.db import transaction
from django.utils import timezone

from . import state
from .conf import get_config
from .models import Exclusion, Suggestion, SuggestionStatus


def get(external_key):
    return Exclusion.objects.filter(external_key=external_key).first()


@transaction.atomic
def exclude(adapter, obj, languages=None, *, user=None, note=""):
    """Exclude `obj` from translation into `languages` (None or empty: all languages)."""
    key = adapter.external_key(obj)
    languages = sorted(set(languages or []))
    Exclusion.objects.update_or_create(
        external_key=key,
        defaults={
            "adapter": adapter.name,
            "languages": languages,
            "note": note,
            "created_by": user if getattr(user, "pk", None) else None,
        },
    )
    pending = Suggestion.objects.filter(external_key=key, status=SuggestionStatus.PENDING)
    if languages:
        pending = pending.filter(language__in=languages)
    pending.update(status=SuggestionStatus.OUTDATED, decided_at=timezone.now())
    state.mark_dirty(adapter, obj)


@transaction.atomic
def include(adapter, obj):
    """Translate `obj` again in all languages."""
    if Exclusion.objects.filter(external_key=adapter.external_key(obj)).delete()[0]:
        state.mark_dirty(adapter, obj)


def excluded_languages(external_key):
    """None if not excluded; an empty set if excluded entirely; else the excluded codes."""
    exclusion = get(external_key)
    if exclusion is None:
        return None
    return set(exclusion.languages)


def apply(unit):
    """The unit as it may be pushed: None if excluded entirely, else with excluded languages
    removed from its writable languages."""
    excluded = excluded_languages(unit.external_key)
    if excluded is None:
        return unit
    if not excluded:
        return None
    config = get_config()
    writable = unit.writable_languages
    if writable is None:
        writable = [code for code in config.languages if code != unit.source_language]
    unit.writable_languages = [code for code in writable if code not in excluded]
    return unit


def is_excluded(external_key, language):
    excluded = excluded_languages(external_key)
    return excluded is not None and (not excluded or language in excluded)
