"""The documents that need attention (Ponyglot › Translation dashboard › Documents): every
object with translations missing, outdated, waiting for an editor or held by a quality check,
per language, with a link to where it's handled and bulk actions to close the gaps.

The cloud knows each document's state (`GET /units`); this site adds what it knows locally:
translations waiting here to be applied, and changes not sent yet.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.options import IS_POPUP_VAR
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from . import suggestions as service
from . import sync
from .adapters import registry
from .client import APIError, Client, ConfigurationError
from .conf import get_config
from .models import Suggestion, SuggestionStatus, SyncState

PAGE_SIZE = 50
ACTIONS = ("translate", "apply")


def content_kinds():
    """`[(key prefix, label)]` of all adapters, for the content type filter."""
    return [kind for adapter in registry.all() for kind in adapter.content_kinds()]


def target_languages():
    names = dict(settings.LANGUAGES)
    source = get_config().source_language
    return [
        {"code": code, "name": names.get(code, code.upper())}
        for code in get_config().languages
        if code != source
    ]


def _filters(request):
    kinds = dict(content_kinds())
    codes = {lang["code"] for lang in target_languages()}
    prefix = request.GET.get("prefix", "")
    language = request.GET.get("language", "")
    return (prefix if prefix in kinds else ""), (language if language in codes else "")


def _kind_label(kinds, key):
    """The label of the most specific content kind `key` belongs to."""
    matches = [(prefix, label) for prefix, label in kinds if key.startswith(prefix)]
    return max(matches, key=lambda kind: len(kind[0]))[1] if matches else key.split(":")[0]


def _rows(units, languages, language_filter):
    """Table rows: per document, a cell per target language with what needs doing."""
    keys = [unit["external_key"] for unit in units]
    waiting = {
        (row["external_key"], row["language"]): row["n"]
        for row in Suggestion.objects.filter(external_key__in=keys, status=SuggestionStatus.PENDING)
        .values("external_key", "language")
        .annotate(n=Count("id"))
    }
    kinds = content_kinds()
    review_url = reverse("admin:ponyglot_suggestion_changelist")
    codes = [
        lang["code"] for lang in languages if not language_filter or lang["code"] == language_filter
    ]
    rows = []
    for unit in units:
        key = unit["external_key"]
        adapter = registry.for_key(key)
        cloud = {lang["code"]: lang for lang in unit["languages"]}
        cells = []
        for code in codes:
            counts = cloud.get(code, {})
            to_apply = waiting.get((key, code), 0)
            cells.append(
                {
                    "code": code,
                    "untranslated": counts.get("missing", 0) + counts.get("stale", 0),
                    "missing": counts.get("missing", 0),
                    "stale": counts.get("stale", 0),
                    "to_apply": to_apply,
                    "apply_url": f"{review_url}?"
                    + urlencode(
                        {"external_key": key, "language": code, "status__exact": "pending"}
                    ),
                    # Delivered, but not here as suggestions: in a draft (django CMS, Wagtail).
                    "with_editors": max(counts.get("review", 0) - to_apply, 0),
                    "held": counts.get("attention", 0),
                    "held_url": counts.get("attention_url", ""),
                }
            )
        rows.append(
            {
                "key": key,
                "label": unit.get("label") or key,
                "path": unit.get("path", ""),
                "kind": _kind_label(kinds, key),
                "url": adapter.translation_url(key) if adapter else "",
                "dashboard_url": unit.get("dashboard_url", ""),
                "cells": cells,
            }
        )
    return rows


def context(request):
    prefix, language = _filters(request)
    cursor = request.GET.get("cursor") or None
    languages = target_languages()
    units, next_cursor, cloud_error = [], None, ""
    try:
        page = Client().units_needing_work(
            prefix=prefix or None, language=language or None, cursor=cursor, limit=PAGE_SIZE
        )
        units, next_cursor = page["units"], page.get("next_cursor")
    except (APIError, ConfigurationError) as error:
        cloud_error = str(error)

    def query(**changes):
        params = {"prefix": prefix, "language": language, **changes}
        if IS_POPUP_VAR in request.GET:
            params[IS_POPUP_VAR] = 1
        return "?" + urlencode({k: v for k, v in params.items() if v})

    unsent = SyncState.objects.filter(dirty=True)
    failed = SyncState.objects.exclude(error="")
    if prefix:
        unsent, failed = (
            unsent.filter(external_key__startswith=prefix),
            failed.filter(external_key__startswith=prefix),
        )
    sync_states = reverse("admin:ponyglot_syncstate_changelist")
    return {
        "title": _("Documents that need attention"),
        "kinds": [{"prefix": p, "label": label} for p, label in content_kinds()],
        "languages": languages,
        "shown_languages": [lang for lang in languages if not language or lang["code"] == language],
        "prefix": prefix,
        "language": language,
        "rows": _rows(units, languages, language),
        "cloud_error": cloud_error,
        "first_page": bool(cursor),
        "first_url": query(),
        "next_url": query(cursor=next_cursor) if next_cursor else "",
        "unsent": unsent.count(),
        "failed": failed.count(),
        "failed_url": f"{sync_states}?{urlencode({'error__gt': ''})}",
        "action_query": query(cursor=cursor or "") or "?",
        "can_apply": request.user.has_perm("ponyglot.change_suggestion"),
        "is_popup": IS_POPUP_VAR in request.GET,
    }


def action(request):
    """Run the bulk action (POST `action`) on the selected documents; returns a response or
    None (back to the list)."""
    name = request.POST.get("action", "")
    if name not in ACTIONS:
        messages.warning(request, _("Choose an action."))
        return None
    keys = [key for key in request.POST.getlist("keys") if registry.for_key(key)]
    language = _filters(request)[1]
    if not keys:
        messages.warning(request, _("Select the documents first."))
        return None
    try:
        if name == "translate":
            return _translate(request, keys, language)
        if name == "apply":
            _apply(request, keys, language)
    except (APIError, ConfigurationError) as error:
        messages.error(request, _("Ponyglot: %s") % error)
    return None


def _translate(request, keys, language):
    """Send the documents' changes, then request their translation."""
    client = Client()
    sync.push(client, sync.Report(), keys=keys)
    payload = {"type": "translate", "units": keys}
    if language:
        payload["languages"] = [language]
    if request.POST.get("confirm"):
        payload["confirm"] = True
    try:
        job = client.create_job(**payload)
    except APIError as error:
        if error.code != "confirmation_required":
            raise
        return TemplateResponse(
            request,
            "admin/ponyglot/documents_confirm.html",
            {
                "title": _("Confirm translation"),
                "estimate": error.data.get("estimate", {}),
                "keys": keys,
                "action_url": request.get_full_path(),
                "back_url": request.get_full_path(),
                "is_popup": IS_POPUP_VAR in request.GET,
            },
        )
    messages.success(
        request,
        ngettext(
            "Translation requested for %(n)s document (%(segments)s texts).",
            "Translation requested for %(n)s documents (%(segments)s texts).",
            len(keys),
        )
        % {"n": len(keys), "segments": job.get("estimated_segments", 0)},
    )
    return None


def _apply(request, keys, language):
    """Apply the documents' waiting translations (QA errors excepted)."""
    if not request.user.has_perm("ponyglot.change_suggestion"):
        raise PermissionDenied
    pending = Suggestion.objects.filter(external_key__in=keys, status=SuggestionStatus.PENDING)
    if language:
        pending = pending.filter(language=language)
    result = service.apply(list(pending), request.user)
    messages.success(
        request,
        ngettext("%(n)s translation applied.", "%(n)s translations applied.", result.applied)
        % {"n": result.applied},
    )
    if result.blocked:
        messages.warning(
            request,
            ngettext(
                "%(n)s has a QA error: review it before applying.",
                "%(n)s have QA errors: review them before applying.",
                result.blocked,
            )
            % {"n": result.blocked},
        )
    if result.outdated:
        messages.warning(
            request,
            ngettext(
                "%(n)s was outdated (the source changed) and will be translated again.",
                "%(n)s were outdated (the source changed) and will be translated again.",
                result.outdated,
            )
            % {"n": result.outdated},
        )
