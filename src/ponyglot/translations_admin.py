"""Translation dialogs in the Django admin: for one object, all objects of a model, the site.

- `PonyglotAdminMixin` (add it to a model admin, before the library's admin class) puts a
  "Ponyglot translations" button on the change page (this object) and on the change list (all
  objects of the model)::

      @admin.register(Product)
      class ProductAdmin(PonyglotAdminMixin, TabbedTranslationAdmin): ...

  Its change page also shows waiting suggestions below their fields (parler: on the
  language's tab), to apply into the form or reject; saving the form records the decisions.

- The translation dashboard (Ponyglot › Translation dashboard, permission
  `ponyglot.use_dashboard`) covers the whole site and can trigger site-wide runs.

Each dialog shows, per language, what is missing, what waits to be applied and what is held by
a quality check, with the next step. General actions sit in the submit row (django CMS shows
them in its modal footer); links to the Ponyglot platform appear only where something needs
doing there.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.options import IS_POPUP_VAR
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect
from django.template.loader import select_template
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from . import exclusions, state, sync
from . import suggestions as service
from .adapters import registry
from .client import APIError, Client, ConfigurationError
from .conf import get_config
from .models import Suggestion, SuggestionStatus, SyncState

# When no translations were fetched for this long, opening a dialog fetches them first, and
# "Fetch translations now" is offered.
FETCH_AFTER = timedelta(minutes=15)
ACTIVE_JOB = ("queued", "running")


def adapter_for(model):
    """The adapter that syncs `model`, or None."""
    for adapter in registry.all():
        if model in getattr(adapter, "synced_models", list)():
            return adapter
    return None


def model_prefix(adapter, model):
    """The key prefix of all objects of a model, e.g. `parler:blog.post:`."""
    return f"{adapter.name}:{model._meta.label_lower}:"


def overdue(run):
    return run is None or timezone.now() - run.finished_at > FETCH_AFTER


@dataclass
class Scope:
    """What a dialog covers: one object (`key`), the objects of a model (`prefix`), or the
    whole site (neither)."""

    title: str
    key: str = ""
    prefix: str = ""
    adapter: object = None
    obj: object = None
    model: object = None
    back_url: str = ""
    back_label: str = ""
    urls: dict = field(default_factory=dict)

    @property
    def kind(self):
        return "object" if self.key else ("model" if self.prefix else "site")

    def suggestions(self):
        pending = Suggestion.objects.filter(status=SuggestionStatus.PENDING)
        if self.key:
            return pending.filter(external_key=self.key)
        if self.prefix:
            return pending.filter(external_key__startswith=self.prefix)
        return pending

    def review_url(self, language):
        params = {"language": language, "status__exact": SuggestionStatus.PENDING}
        if self.key:
            params["external_key"] = self.key
        elif self.prefix:
            params["external_key__startswith"] = self.prefix
        return f"{reverse('admin:ponyglot_suggestion_changelist')}?{urlencode(params)}"

    def last_fetch(self):
        if self.key or self.prefix:
            return sync.last_fetch(self.key or self.prefix)
        return sync.last_run()

    def refresh(self, client):
        """Fetch this scope's translations (the site: a full sync round)."""
        if self.key or self.prefix:
            return sync.refresh(client, self.key or None, prefix=self.prefix or None)
        return sync.run(client)

    def relevant_job(self, job):
        units = job.get("units")
        if self.key:
            return units is None and not job.get("prefix") or self.key in (units or [])
        if self.prefix:
            return job.get("prefix") == self.prefix or any(
                u.startswith(self.prefix) for u in units or []
            )
        return True


def _counts(scope, client):
    """`({language: {"untranslated", "held", "review", "attention_url"}}, error)` from the cloud."""
    try:
        if scope.key:
            data = client.unit_status(scope.key)
            result = {}
            for lang in data["languages"]:
                code = lang["code"]
                states = [s["states"].get(code, "missing") for s in data["segments"]]
                result[code] = {
                    "untranslated": sum(1 for s in states if s in ("missing", "stale")),
                    "held": sum(
                        1
                        for s in data["segments"]
                        if code in s.get("held_back", []) or s["states"].get(code) == "attention"
                    ),
                    "review": sum(1 for s in states if s == "review"),
                    "attention_url": lang.get("attention_url", ""),
                }
            return result, ""
        data = client.site_status(prefix=scope.prefix or None)
        return {
            lang["code"]: {
                "untranslated": lang.get("missing", 0) + lang.get("stale", 0),
                "held": lang.get("attention", 0),
                "review": lang.get("review", 0),
                "attention_url": "",
            }
            for lang in data["languages"]
        }, ""
    except (APIError, ConfigurationError) as error:
        if getattr(error, "status", None) == 404:
            return None, _("Not sent to Ponyglot yet.")
        return None, str(error)


def dialog_context(request, scope):
    """Everything the dialog template shows for a scope."""
    fetched, jobs = None, []
    try:
        client = Client()
        if overdue(scope.last_fetch()) and scope.kind != "site":
            fetched = scope.refresh(client)
        counts, cloud_error = _counts(scope, client)
        if counts is not None:
            jobs = [job for job in client.jobs() if scope.relevant_job(job)][:5]
    except ConfigurationError as error:
        counts, cloud_error = None, str(error)
    except APIError as error:
        counts, cloud_error = None, str(error)
    running = [job for job in jobs if job.get("status") in ACTIVE_JOB]
    pending = list(scope.suggestions())
    names = dict(settings.LANGUAGES)
    source = get_config().source_language
    languages = []
    for code in get_config().languages:
        if code == source:
            continue
        cloud = (counts or {}).get(code, {})
        waiting = [s for s in pending if s.language == code]
        lang = {
            "code": code,
            "name": names.get(code, code.upper()),
            "excluded": bool(scope.key) and exclusions.is_excluded(scope.key, code),
            "untranslated": cloud.get("untranslated", 0),
            "held": cloud.get("held", 0),
            "attention_url": cloud.get("attention_url", ""),
            "waiting": len(waiting),
            "waiting_errors": sum(1 for s in waiting if s.has_errors),
            "review_url": scope.review_url(code),
            "translating": any(
                job.get("languages") is None or code in job["languages"] for job in running
            ),
        }
        lang["done"] = counts is not None and not (
            lang["excluded"] or lang["untranslated"] or lang["held"] or lang["waiting"]
        )
        languages.append(lang)
    popup = IS_POPUP_VAR in request.GET
    return {
        "scope": scope,
        "title": scope.title,
        "languages": languages,
        "counts": counts,
        "cloud_error": cloud_error,
        "fetched": fetched,
        "jobs": jobs,
        "translating": bool(running),
        "translate_languages": [
            lang["code"]
            for lang in languages
            if not lang["excluded"] and (lang["untranslated"] or counts is None)
        ],
        "fetch_due": overdue(scope.last_fetch()),
        "all_done": bool(languages) and all(lang["done"] for lang in languages),
        "can_apply": request.user.has_perm("ponyglot.change_suggestion"),
        "is_popup": popup,
        "popup_qs": f"?{IS_POPUP_VAR}=1" if popup else "",
    }


def dialog_action(request, scope, action):
    """translate / apply / fetch for a scope; returns a response or None (redirect back)."""
    if request.method != "POST":
        return None
    try:
        if action == "translate":
            return _translate(request, scope)
        if action == "fetch":
            report = scope.refresh(Client())
            messages.success(
                request,
                ngettext(
                    "%(n)s translation received.", "%(n)s translations received.", report.delivered
                )
                % {"n": report.delivered},
            )
        elif action == "apply":
            _apply(request, scope, request.POST.get("language", ""))
        else:
            raise Http404
    except (APIError, ConfigurationError) as error:
        messages.error(request, _("Ponyglot: %s") % error)
    return None


def _translate(request, scope):
    """Send what changed (objects never sent included), then request translation."""
    client = Client()
    if scope.obj is not None and not SyncState.objects.filter(external_key=scope.key).exists():
        state.mark_dirty(scope.adapter, scope.obj)
    elif scope.model is not None:
        known = set(
            SyncState.objects.filter(external_key__startswith=scope.prefix).values_list(
                "external_key", flat=True
            )
        )
        for obj in scope.model._default_manager.iterator():
            if scope.adapter.external_key(obj) not in known:
                state.mark_dirty(scope.adapter, obj)
    sync.push(client, sync.Report(), keys=[scope.key] if scope.key else None, prefix=scope.prefix)
    languages = request.POST.getlist("languages")
    payload = {"type": "translate", "units": [scope.key]} if scope.key else {"type": "delta"}
    if scope.prefix:
        payload["prefix"] = scope.prefix
    if languages:
        payload["languages"] = languages
    if request.POST.get("confirm"):
        payload["confirm"] = True
    try:
        job = client.create_job(**payload)
    except APIError as error:
        if error.code != "confirmation_required":
            raise
        popup = IS_POPUP_VAR in request.GET
        return TemplateResponse(
            request,
            "admin/ponyglot/dialog_confirm.html",
            {
                "title": _("Confirm translation"),
                "scope": scope,
                "estimate": error.data.get("estimate", {}),
                "languages": languages,
                "is_popup": popup,
                "popup_qs": f"?{IS_POPUP_VAR}=1" if popup else "",
            },
        )
    messages.success(
        request,
        _(
            "Translation requested (%(segments)s texts). The translations arrive as suggestions "
            "to apply."
        )
        % {"segments": job.get("estimated_segments", 0)},
    )
    return None


def _apply(request, scope, language):
    """Apply the scope's waiting translations in one language (QA errors excepted)."""
    if not request.user.has_perm("ponyglot.change_suggestion"):
        raise PermissionDenied
    result = service.apply(list(scope.suggestions().filter(language=language)), request.user)
    if result.applied:
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


def _with_popup(request, url):
    return f"{url}?{IS_POPUP_VAR}=1" if IS_POPUP_VAR in request.GET else url


def _extend(response, template_name, **context):
    """Render `response` with `template_name`, which extends the template it had."""
    names = response.template_name
    base = select_template([names] if isinstance(names, str) else names)
    response.template_name = template_name
    response.context_data.update(ponyglot_base=base.template, **context)
    return response


def _ids(request, name):
    return [int(value) for value in request.POST.getlist(name) if value.isdigit()]


def _form_data(request, suggestions):
    """What the change form's script needs: suggestions, the decisions a re-shown form (with
    errors) had made, and its labels."""
    return {
        "suggestions": suggestions,
        "applied": _ids(request, "_ponyglot_apply"),
        "rejected": _ids(request, "_ponyglot_reject"),
        "labels": {
            "suggestion": _("Ponyglot suggestion"),
            "apply": _("Apply"),
            "reject": _("Reject"),
            "undo": _("Undo"),
            "apply_all": _("Apply all"),
            "applied": _("Applied; saved with the form."),
            "rejected": _("Rejected; recorded when the form is saved."),
            "error": _("QA error"),
            "warning": _("QA warning"),
            "confirm_errors": _("This translation has a QA error. Apply it anyway?"),
            "summary": _("Ponyglot suggestions for %(language)s: %(n)s"),
        },
    }


class PonyglotAdminMixin:
    """ "Ponyglot translations" on a model admin's change page and change list."""

    def get_urls(self):
        info = self.opts.app_label, self.opts.model_name
        view = self.admin_site.admin_view
        return [
            path("ponyglot/", view(self.ponyglot_model_view), name="{}_{}_ponyglot".format(*info)),
            path(
                "ponyglot/<slug:action>/",
                view(self.ponyglot_model_action),
                name="{}_{}_ponyglot_action".format(*info),
            ),
            path(
                "<path:object_id>/ponyglot/",
                view(self.ponyglot_object_view),
                name="{}_{}_ponyglot_object".format(*info),
            ),
            path(
                "<path:object_id>/ponyglot/<slug:action>/",
                view(self.ponyglot_object_action),
                name="{}_{}_ponyglot_object_action".format(*info),
            ),
            *super().get_urls(),
        ]

    # --- Buttons -------------------------------------------------------------------------

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        response = super().render_change_form(request, context, add, change, form_url, obj)
        adapter = adapter_for(self.model)
        if obj is None or obj.pk is None or adapter is None:
            return response
        pending = Suggestion.objects.filter(
            external_key=adapter.external_key(obj), status=SuggestionStatus.PENDING
        )
        form = context.get("adminform") and context["adminform"].form
        suggestions = self._form_suggestions(request, adapter, obj, form, pending)
        return _extend(
            response,
            "admin/ponyglot/change_form_button.html",
            ponyglot_url=self._url("ponyglot_object", obj.pk),
            ponyglot_waiting=pending.count(),
            ponyglot_form=_form_data(request, suggestions) if suggestions else None,
        )

    def _form_suggestions(self, request, adapter, obj, form, pending):
        """The pending suggestions the change form has a field for (sources unchanged)."""
        if form is None or not request.user.has_perm("ponyglot.change_suggestion"):
            return []
        names = dict(settings.LANGUAGES)
        result = []
        for suggestion in pending.order_by("created_at"):
            name = adapter.form_field_name(form, suggestion.field, suggestion.language)
            if name not in form.fields or not service.is_current(suggestion, adapter, obj):
                continue
            # A title brings its slug along, as applying it in the review admin would.
            slugs = {}
            values = {suggestion.field: suggestion.text}
            for slug_field, slug in adapter.slug_values(obj, suggestion.language, values).items():
                slug_name = adapter.form_field_name(form, slug_field, suggestion.language)
                if slug_name in form.fields:
                    slugs[slug_name] = slug
            result.append(
                {
                    "id": suggestion.pk,
                    "input": name,
                    "text": suggestion.text,
                    "format": suggestion.format,
                    "language": suggestion.language,
                    "language_name": str(names.get(suggestion.language, suggestion.language)),
                    "slugs": slugs,
                    "qa": [
                        {"severity": i.get("severity", ""), "message": i.get("message", "")}
                        for i in suggestion.qa or []
                    ],
                    "has_errors": suggestion.has_errors,
                }
            )
        return result

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change:
            self._record_form_suggestions(request, obj)

    def _record_form_suggestions(self, request, obj):
        """Suggestions applied or rejected in the change form become decided once it's saved."""
        adapter = adapter_for(self.model)
        apply_ids, reject_ids = _ids(request, "_ponyglot_apply"), _ids(request, "_ponyglot_reject")
        if adapter is None or not (apply_ids or reject_ids):
            return
        if not request.user.has_perm("ponyglot.change_suggestion"):
            return
        pending = Suggestion.objects.filter(
            external_key=adapter.external_key(obj), status=SuggestionStatus.PENDING
        )
        result = service.record_written(list(pending.filter(pk__in=apply_ids)), request.user)
        rejected = service.reject(list(pending.filter(pk__in=reject_ids)), request.user)
        if result.applied:
            messages.success(
                request,
                ngettext(
                    "%(n)s translation suggestion applied.",
                    "%(n)s translation suggestions applied.",
                    result.applied,
                )
                % {"n": result.applied},
            )
        if rejected:
            messages.success(
                request,
                ngettext(
                    "%(n)s translation suggestion rejected.",
                    "%(n)s translation suggestions rejected.",
                    rejected,
                )
                % {"n": rejected},
            )
        if result.outdated:
            messages.warning(
                request,
                ngettext(
                    "%(n)s suggestion was outdated (the source changed) and will be translated "
                    "again.",
                    "%(n)s suggestions were outdated (the source changed) and will be translated "
                    "again.",
                    result.outdated,
                )
                % {"n": result.outdated},
            )

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context)
        adapter = adapter_for(self.model)
        if adapter is None or not hasattr(response, "template_name"):
            return response  # e.g. a redirect after an action
        waiting = Suggestion.objects.filter(
            external_key__startswith=model_prefix(adapter, self.model),
            status=SuggestionStatus.PENDING,
        ).count()
        return _extend(
            response,
            "admin/ponyglot/change_list_button.html",
            ponyglot_url=self._url("ponyglot"),
            ponyglot_waiting=waiting,
        )

    # --- Dialogs -------------------------------------------------------------------------

    def _url(self, name, *args):
        info = self.opts.app_label, self.opts.model_name
        return reverse(f"admin:{info[0]}_{info[1]}_{name}", args=args)

    def _model_scope(self, request):
        adapter = adapter_for(self.model)
        if adapter is None:
            raise Http404
        if not self.has_change_permission(request):
            raise PermissionDenied
        name = self.opts.verbose_name_plural
        return Scope(
            title=_("Translations of all %s") % name,
            prefix=model_prefix(adapter, self.model),
            adapter=adapter,
            model=self.model,
            back_url=_with_popup(request, self._url("changelist")),
            back_label=_("Back to the list"),
            urls={
                action: _with_popup(request, self._url("ponyglot_action", action))
                for action in ("translate", "apply", "fetch")
            },
        )

    def _object_scope(self, request, object_id):
        obj = self.get_object(request, unquote(object_id))
        adapter = adapter_for(self.model)
        if obj is None or adapter is None:
            raise Http404
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        return Scope(
            title=_("Translations of “%s”") % obj,
            key=adapter.external_key(obj),
            adapter=adapter,
            obj=obj,
            back_url=_with_popup(request, self._url("change", obj.pk)),
            back_label=_("Back to “%s”") % obj,
            urls={
                action: _with_popup(request, self._url("ponyglot_object_action", obj.pk, action))
                for action in ("translate", "apply", "fetch")
            },
        )

    def _render(self, request, scope):
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "original": scope.obj,
            **dialog_context(request, scope),
        }
        return TemplateResponse(request, "admin/ponyglot/dialog.html", context)

    def ponyglot_model_view(self, request):
        return self._render(request, self._model_scope(request))

    def ponyglot_object_view(self, request, object_id):
        return self._render(request, self._object_scope(request, object_id))

    def ponyglot_model_action(self, request, action):
        scope = self._model_scope(request)
        response = dialog_action(request, scope, action)
        return self._done(request, response, self._url("ponyglot"))

    def ponyglot_object_action(self, request, object_id, action):
        scope = self._object_scope(request, object_id)
        response = dialog_action(request, scope, action)
        return self._done(request, response, self._url("ponyglot_object", scope.obj.pk))

    def _done(self, request, response, back):
        if response is not None:
            response.context_data.update(self.admin_site.each_context(request), opts=self.opts)
            return response
        return redirect(_with_popup(request, back))


def site_scope(request, admin_url):
    return Scope(
        title=_("Translation dashboard"),
        back_url=_with_popup(request, admin_url),
        urls={
            action: _with_popup(request, f"{admin_url}{action}/")
            for action in ("translate", "apply", "fetch")
        },
    )
