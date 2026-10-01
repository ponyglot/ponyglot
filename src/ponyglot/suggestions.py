"""Suggestions: machine translations that editors apply or reject (model adapters)."""

from collections import defaultdict
from typing import NamedTuple

from django.db import transaction
from django.utils import timezone

from .fingerprint import fingerprint
from .models import Suggestion, SuggestionStatus


def store_results(adapter, results):
    """Store results as pending suggestions; return the ids that may be acknowledged.

    A newer result for the same object, field and language supersedes the pending one.
    Results for objects that no longer exist, or fields that aren't synced any more, are
    acknowledged without a suggestion (the next push tells the cloud).
    """
    from .exclusions import is_excluded

    handled = []
    for result in results:
        obj = adapter.get_object(result["unit"])
        gone = obj is None or result["key"] not in adapter.fields(type(obj))
        if gone or is_excluded(result["unit"], result["language"]):
            handled.append(result["id"])
            continue
        with transaction.atomic():
            if Suggestion.objects.filter(result_id=result["id"]).exists():
                handled.append(result["id"])  # delivered before, not acknowledged yet
                continue
            Suggestion.objects.filter(
                external_key=result["unit"],
                field=result["key"],
                language=result["language"],
                status=SuggestionStatus.PENDING,
            ).update(status=SuggestionStatus.SUPERSEDED, decided_at=timezone.now())
            Suggestion.objects.create(
                result_id=result["id"],
                adapter=adapter.name,
                external_key=result["unit"],
                field=result["key"],
                language=result["language"],
                text=result["text"],
                format=result.get("format") or "plain",
                source_fingerprint=result.get("source_fingerprint", ""),
                origin=result.get("origin", ""),
                qa=result.get("qa") or [],
            )
        handled.append(result["id"])
    return handled


def adapter_for(suggestion):
    from .adapters import registry

    return registry.get(suggestion.adapter)


def is_current(suggestion, adapter, obj):
    """Whether the suggestion was translated from the source text the object has now."""
    text = adapter.source_value(obj, suggestion.field)
    if not text:
        return False
    current = fingerprint(str(text), adapter.field_format(obj, suggestion.field, str(text)))
    return current == suggestion.source_fingerprint


class Applied(NamedTuple):
    applied: int
    outdated: int
    blocked: int  # QA errors, text unchanged, not confirmed


def apply(suggestions, user=None, texts=None, *, confirm_errors=False):
    """Write pending suggestions to their objects. `texts`: `{suggestion pk: edited text}`.

    Suggestions whose source changed since translation are marked outdated instead (the cloud
    retranslates them after the next push). Suggestions with QA errors are only applied if
    their text was edited (the cloud checks approved text again) or `confirm_errors` is set;
    otherwise they stay pending and count as `blocked`.
    """
    texts = texts or {}
    groups = defaultdict(list)
    for suggestion in suggestions:
        if suggestion.status == SuggestionStatus.PENDING:
            groups[(suggestion.adapter, suggestion.external_key, suggestion.language)].append(
                suggestion
            )
    applied = outdated = blocked = 0
    now = timezone.now()
    for (_, external_key, language), group in groups.items():
        adapter = adapter_for(group[0])
        obj = adapter.get_object(external_key) if adapter else None
        with transaction.atomic():
            values, done = {}, []
            for suggestion in group:
                if obj is None or not is_current(suggestion, adapter, obj):
                    suggestion.status = SuggestionStatus.OUTDATED
                    suggestion.decided_at = now
                    suggestion.save(update_fields=["status", "decided_at"])
                    outdated += 1
                    continue
                edited = texts.get(suggestion.pk)
                if edited is not None and edited != suggestion.text:
                    suggestion.final_text = edited
                if suggestion.has_errors and not suggestion.edited and not confirm_errors:
                    blocked += 1
                    continue
                values[suggestion.field] = suggestion.applied_text
                done.append(suggestion)
            if values:
                values.update(adapter.slug_values(obj, language, values))
                adapter.write(obj, language, values)
            status = adapter.status_after_write(obj) if done else None
            for suggestion in done:
                suggestion.status = status
                suggestion.decided_at = now
                suggestion.decided_by = user if getattr(user, "pk", None) else None
                suggestion.save(update_fields=["status", "final_text", "decided_at", "decided_by"])
                applied += 1
    return Applied(applied, outdated, blocked)


def record_written(suggestions, user=None):
    """Mark pending suggestions an editor put into the change form and saved (the form wrote
    them). What was saved counts as the applied text; a field left empty keeps it pending.
    Suggestions whose source changed in the meantime become outdated (translated again)."""
    applied = outdated = 0
    now = timezone.now()
    objects = {}
    for suggestion in suggestions:
        if suggestion.status != SuggestionStatus.PENDING:
            continue
        adapter = adapter_for(suggestion)
        if suggestion.external_key not in objects:
            objects[suggestion.external_key] = (
                adapter.get_object(suggestion.external_key) if adapter else None
            )
        obj = objects[suggestion.external_key]
        if obj is None or not is_current(suggestion, adapter, obj):
            suggestion.status = SuggestionStatus.OUTDATED
            suggestion.decided_at = now
            suggestion.save(update_fields=["status", "decided_at"])
            outdated += 1
            continue
        saved = str(adapter.target_value(obj, suggestion.field, suggestion.language) or "")
        if not saved.strip():
            continue
        # Browsers send textareas with CRLF and form fields strip: neither is an edit.
        unchanged = saved.replace("\r\n", "\n").strip() == suggestion.text.strip()
        suggestion.final_text = "" if unchanged else saved
        suggestion.status = adapter.status_after_write(obj)
        suggestion.decided_at = now
        suggestion.decided_by = user if getattr(user, "pk", None) else None
        suggestion.save(update_fields=["status", "final_text", "decided_at", "decided_by"])
        applied += 1
    return Applied(applied, outdated, 0)


def reject(suggestions, user=None):
    count = 0
    for suggestion in suggestions:
        if suggestion.status != SuggestionStatus.PENDING:
            continue
        suggestion.status = SuggestionStatus.REJECTED
        suggestion.decided_at = timezone.now()
        suggestion.decided_by = user if getattr(user, "pk", None) else None
        suggestion.save(update_fields=["status", "decided_at", "decided_by"])
        count += 1
    return count


def unreported():
    return Suggestion.objects.filter(
        status__in=[SuggestionStatus.APPLIED, SuggestionStatus.REJECTED], reported_at=None
    )


def review_payload(suggestion):
    if suggestion.status == SuggestionStatus.REJECTED:
        return {"id": suggestion.result_id, "outcome": "rejected"}
    review = {"id": suggestion.result_id, "outcome": "approved"}
    if suggestion.final_text and suggestion.final_text != suggestion.text:
        review["text"] = suggestion.final_text
    if suggestion.decided_by_id:
        review["reviewer"] = suggestion.decided_by.get_username()[:150]
    return review
