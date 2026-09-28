"""Review admin for suggestions: see the source and a diff, edit, apply or reject."""

import difflib
import re

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from . import exclusions
from . import suggestions as service
from .adapters import registry
from .models import Exclusion, HeldBack, Suggestion, SuggestionStatus, SyncState

_TOKENS = re.compile(r"\s+|[^\s]+")


def word_diff(old, new):
    """HTML with <del>/<ins> for the words that differ (all text escaped)."""
    old_tokens, new_tokens = _TOKENS.findall(old or ""), _TOKENS.findall(new or "")
    parts = []
    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            parts.append(escape("".join(old_tokens[a1:a2])))
            continue
        if a2 > a1:
            parts.append(f"<del>{escape(''.join(old_tokens[a1:a2]))}</del>")
        if b2 > b1:
            parts.append(f"<ins>{escape(''.join(new_tokens[b1:b2]))}</ins>")
    return mark_safe("".join(parts))  # noqa: S308 (every part is escaped above)


class SuggestionForm(forms.ModelForm):
    class Meta:
        model = Suggestion
        fields = ["final_text"]
        labels = {"final_text": _("Text to apply")}
        widgets = {"final_text": forms.Textarea(attrs={"rows": 6, "cols": 80})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.instance.final_text:
            self.initial["final_text"] = self.instance.text


@admin.register(Suggestion)
class SuggestionAdmin(admin.ModelAdmin):
    form = SuggestionForm
    change_form_template = "admin/ponyglot/suggestion/change_form.html"
    list_display = ["external_key", "field", "language", "short_text", "qa_flag", "status"]
    list_display_links = ["external_key", "field"]
    list_filter = ["status", "language", "adapter"]
    search_fields = ["external_key", "field", "text"]
    actions = ["apply_selected", "reject_selected"]
    readonly_fields = ["object_link", "source_text", "current_text", "diff", "qa_issues", "status"]
    fieldsets = [
        (None, {"fields": ["object_link", "status", "source_text", "current_text", "diff"]}),
        (_("Suggestion"), {"fields": ["qa_issues", "final_text"]}),
    ]

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("decided_by")

    @admin.display(description=_("translation"))
    def short_text(self, obj):
        return (obj.applied_text[:80] + "…") if len(obj.applied_text) > 80 else obj.applied_text

    @admin.display(description=_("QA"))
    def qa_flag(self, obj):
        if obj.has_errors:
            return format_html('<strong class="errornote">{}</strong>', _("error"))
        return _("warning") if obj.qa else "–"

    def _object(self, obj):
        adapter = service.adapter_for(obj)
        return adapter, (adapter.get_object(obj.external_key) if adapter else None)

    @admin.display(description=_("object"))
    def object_link(self, obj):
        _adapter, target = self._object(obj)
        if target is None:
            return format_html("{} ({})", obj.external_key, _("deleted"))
        return f"{target} · {obj.field} → {obj.language}"

    @admin.display(description=_("source"))
    def source_text(self, obj):
        adapter, target = self._object(obj)
        text = adapter.source_value(target, obj.field) if target is not None else ""
        pending = target is not None and obj.status == SuggestionStatus.PENDING
        if pending and not service.is_current(obj, adapter, target):
            return format_html(
                "{}<p><strong>{}</strong></p>",
                text or "",
                _("The source changed since this was translated; it can't be applied."),
            )
        return text or "–"

    @admin.display(description=_("current translation"))
    def current_text(self, obj):
        adapter, target = self._object(obj)
        value = adapter.target_value(target, obj.field, obj.language) if target else None
        return value or "–"

    @admin.display(description=_("changes"))
    def diff(self, obj):
        adapter, target = self._object(obj)
        current = adapter.target_value(target, obj.field, obj.language) if target else ""
        return word_diff(current or "", obj.applied_text)

    @admin.display(description=_("QA"))
    def qa_issues(self, obj):
        if not obj.qa:
            return "–"
        return format_html_join(
            "",
            '<div class="ponyglot-qa-{}"><strong>{}</strong> {}: {}</div>',
            (
                (i.get("severity"), _("Error") if i.get("severity") == "error" else _("Warning"))
                + (i.get("code"), i.get("message"))
                for i in obj.qa
            ),
        )

    def has_change_permission(self, request, obj=None):
        allowed = super().has_change_permission(request, obj)
        return allowed and (obj is None or obj.status == SuggestionStatus.PENDING)

    def response_change(self, request, obj):
        if "_ponyglot_reject" in request.POST:
            self._report(request, 0, 0, service.reject([obj], request.user))
            return HttpResponseRedirect("../../")
        if "_ponyglot_apply" in request.POST:
            text = obj.final_text or obj.text
            result = service.apply(
                [obj],
                request.user,
                texts={obj.pk: text},
                confirm_errors="_ponyglot_confirm" in request.POST,
            )
            self._report(request, result.applied, result.outdated, 0, result.blocked)
            if result.blocked:
                return HttpResponseRedirect(".")
            return HttpResponseRedirect("../../")
        return super().response_change(request, obj)

    def changelist_view(self, request, extra_context=None):
        waiting = HeldBack.objects.count()
        if waiting:
            self.message_user(
                request,
                ngettext(
                    "%d translation is waiting for a QA review in Ponyglot; it arrives once "
                    "it's resolved.",
                    "%d translations are waiting for a QA review in Ponyglot; they arrive once "
                    "they're resolved.",
                    waiting,
                )
                % waiting,
                messages.INFO,
            )
        return super().changelist_view(request, extra_context)

    def save_model(self, request, obj, form, change):
        # Only the edited text is saved here; applying/rejecting happens in response_change.
        if obj.final_text == obj.text:
            obj.final_text = ""
        super().save_model(request, obj, form, change)

    @admin.action(description=_("Apply selected suggestions"), permissions=["change"])
    def apply_selected(self, request, queryset):
        result = service.apply(list(queryset), request.user)
        self._report(request, result.applied, result.outdated, 0, result.blocked)

    @admin.action(description=_("Reject selected suggestions"), permissions=["change"])
    def reject_selected(self, request, queryset):
        self._report(request, 0, 0, service.reject(list(queryset), request.user))

    def _report(self, request, applied, outdated, rejected, blocked=0):
        if blocked:
            self.message_user(
                request,
                ngettext(
                    "%d suggestion was not applied: it has a QA error. Fix the text, or open "
                    "it and confirm applying it anyway.",
                    "%d suggestions were not applied: they have QA errors. Fix the text, or "
                    "open them and confirm applying them anyway.",
                    blocked,
                )
                % blocked,
                messages.ERROR,
            )
        if applied:
            self.message_user(
                request,
                ngettext("%d suggestion applied.", "%d suggestions applied.", applied) % applied,
                messages.SUCCESS,
            )
        if rejected:
            self.message_user(
                request,
                ngettext("%d suggestion rejected.", "%d suggestions rejected.", rejected)
                % rejected,
                messages.SUCCESS,
            )
        if outdated:
            self.message_user(
                request,
                ngettext(
                    "%d suggestion was not applied: its source changed; it will be "
                    "translated again.",
                    "%d suggestions were not applied: their source changed; they will be "
                    "translated again.",
                    outdated,
                )
                % outdated,
                messages.WARNING,
            )


@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin):
    """Everything that is synced; exclude content from translation here."""

    list_display = ["external_key", "adapter", "excluded", "dirty", "pushed_at", "error"]
    list_filter = ["adapter", "dirty", "deleted"]
    search_fields = ["external_key"]
    readonly_fields = [f.name for f in SyncState._meta.fields]
    actions = ["exclude_selected", "include_selected"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_exclude_permission(self, request):
        return request.user.has_perm("ponyglot.add_exclusion")

    @admin.display(description=_("excluded"))
    def excluded(self, obj):
        exclusion = exclusions.get(obj.external_key)
        if exclusion is None:
            return "–"
        return ", ".join(exclusion.languages) or _("all languages")

    def _objects(self, queryset):
        for sync_state in queryset:
            adapter = registry.get(sync_state.adapter)
            obj = adapter.get_object(sync_state.external_key) if adapter else None
            if obj is not None:
                yield adapter, obj

    @admin.action(description=_("Exclude from translation"), permissions=["exclude"])
    def exclude_selected(self, request, queryset):
        count = 0
        for adapter, obj in self._objects(queryset):
            exclusions.exclude(adapter, obj, user=request.user)
            count += 1
        self.message_user(
            request,
            ngettext(
                "%d object excluded from translation; the next sync removes it from Ponyglot.",
                "%d objects excluded from translation; the next sync removes them from Ponyglot.",
                count,
            )
            % count,
            messages.SUCCESS,
        )

    @admin.action(description=_("Translate again (remove exclusion)"), permissions=["exclude"])
    def include_selected(self, request, queryset):
        count = 0
        for adapter, obj in self._objects(queryset):
            exclusions.include(adapter, obj)
            count += 1
        self.message_user(
            request,
            ngettext("%d object is translated again.", "%d objects are translated again.", count)
            % count,
            messages.SUCCESS,
        )


@admin.register(Exclusion)
class ExclusionAdmin(admin.ModelAdmin):
    list_display = ["external_key", "adapter", "languages", "note", "created_by", "created_at"]
    list_filter = ["adapter"]
    search_fields = ["external_key", "note"]
    readonly_fields = ["adapter", "external_key", "created_by", "created_at"]

    def has_add_permission(self, request):
        return False  # use "Exclude from translation" on the synced content

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        _mark_changed(obj)

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        _mark_changed(obj)

    def delete_queryset(self, request, queryset):
        changed = list(queryset)
        super().delete_queryset(request, queryset)
        for obj in changed:
            _mark_changed(obj)


def _mark_changed(exclusion):
    """Push the object again with its new exclusion (or none)."""
    from .state import _mark

    _mark(exclusion.adapter, exclusion.external_key, deleted=False)


@admin.register(HeldBack)
class HeldBackAdmin(admin.ModelAdmin):
    """What the cloud holds back for QA review, and why (refreshed by every sync)."""

    list_display = ["label_or_key", "key", "language", "reason", "since"]
    list_filter = ["language"]
    search_fields = ["external_key", "label", "key"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description=_("content"))
    def label_or_key(self, obj):
        return obj.label or obj.external_key

    @admin.display(description=_("why"))
    def reason(self, obj):
        return "; ".join(issue.get("message", "") for issue in obj.issues) or "–"
