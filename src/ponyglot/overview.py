"""The documents that need attention (Ponyglot › Translation dashboard › Documents): every
object with translations missing, outdated, waiting for an editor or held by a quality check,
per language, with a link to where it's handled and bulk actions to close the gaps.

The cloud knows each document's state (`GET /units`); this site adds what it knows locally:
translations waiting here to be applied, and changes not sent yet.

Shared by the Django admin and other admins (wagtail-ponyglot's Wagtail dashboard): links into
the Django admin are left out when it isn't installed.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.options import IS_POPUP_VAR
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.template.response import TemplateResponse
from django.urls import NoReverseMatch, reverse
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from . import publishing, sync
from . import suggestions as service
from .adapters import registry
from .client import APIError, Client, ConfigurationError
from .conf import get_config
from .models import Suggestion, SuggestionStatus, SyncState

PAGE_SIZE = 50
ACTIONS = ("translate", "apply")
CONFIRM_TEMPLATE = "admin/ponyglot/documents_confirm.html"


def admin_url(name):
    """A Django admin URL, or "" where the Django admin isn't installed (e.g. Wagtail only)."""
    try:
        return reverse(name)
    except NoReverseMatch:
        return ""


def content_kinds():
    """`[(key prefix, label)]` of all adapters, for the content type filter: page-like
    systems first (by `overview_order`: django CMS, then Wagtail), then the models."""
    return [kind for adapter in _ordered_adapters() for kind in adapter.content_kinds()]


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
    review_url = admin_url("admin:ponyglot_suggestion_changelist")
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
                    + urlencode({"external_key": key, "language": code, "status__exact": "pending"})
                    if review_url
                    else "",
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


def _ordered_adapters():
    """Page-like content systems first (by `overview_order`: django CMS, then Wagtail), then
    the rest (models)."""
    return sorted(registry.all(), key=lambda a: (a.overview_order is None, a.overview_order or 0))


def _page(client, prefix, language, cursor, limit=None):
    """One page of documents: `(units, next cursor)`. Without a content type filter, the
    adapters' documents come one adapter after the other, so the cursor is
    `<adapter index>:<cloud cursor>`."""
    limit = limit or PAGE_SIZE
    prefixes = [prefix] if prefix else [f"{a.name}:" for a in _ordered_adapters()]
    index, _sep, inner = (cursor or "0:").partition(":")
    index = int(index) if index.isdigit() else 0
    units = []
    while index < len(prefixes):
        page = client.units_needing_work(
            prefix=prefixes[index],
            language=language or None,
            cursor=inner or None,
            limit=limit - len(units),
        )
        units += page["units"]
        inner = page.get("next_cursor") or ""
        if inner:  # the page is full
            break
        index += 1
        if len(units) == limit:
            break
    return units, (f"{index}:{inner}" if index < len(prefixes) else None)


def context(request):
    prefix, language = _filters(request)
    cursor = request.GET.get("cursor") or None
    languages = target_languages()
    units, next_cursor, cloud_error = [], None, ""
    try:
        units, next_cursor = _page(Client(), prefix, language, cursor)
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
    sync_states = admin_url("admin:ponyglot_syncstate_changelist")
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
        "failed_url": f"{sync_states}?{urlencode({'error__gt': ''})}" if sync_states else "",
        "action_query": query(cursor=cursor or "") or "?",
        "can_apply": request.user.has_perm("ponyglot.change_suggestion"),
        # Permission is checked per document when its translation arrives.
        "publish": {"offered": True, "checked": publishing.default(), "per_document": True},
        "is_popup": IS_POPUP_VAR in request.GET,
    }


def action(request, *, confirm_template=CONFIRM_TEMPLATE):
    """Run the bulk action (POST `action`) on the selected documents; returns a response or
    None (back to the list). Translations above the cloud's threshold render
    `confirm_template` first."""
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
            return _translate(request, keys, language, confirm_template)
        if name == "apply":
            _apply(request, keys, language)
    except (APIError, ConfigurationError) as error:
        messages.error(request, _("Ponyglot: %s") % error)
    return None


def _translate(request, keys, language, confirm_template=CONFIRM_TEMPLATE):
    """Send the documents' changes, then request their translation."""
    client = Client()
    sync.push(client, sync.Report(), keys=keys)
    payload = {"type": "translate", "units": keys, **publishing.job_options(request)}
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
            confirm_template,
            {
                "title": _("Confirm translation"),
                "estimate": error.data.get("estimate", {}),
                "keys": keys,
                "publish_requested": publishing.requested(request),
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
