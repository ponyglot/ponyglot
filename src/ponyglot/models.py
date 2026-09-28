from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class SyncState(models.Model):
    """What the cloud knows about one unit, and whether it needs pushing (ADR 0001).

    Saving content only marks the row dirty (same transaction as the save); `ponyglot sync`
    does the HTTP work. `version` grows with every change, so a push that raced with an edit
    doesn't clear the newer change.
    """

    external_key = models.CharField(max_length=255, unique=True)
    adapter = models.CharField(max_length=50)
    dirty = models.BooleanField(default=True, db_index=True)
    deleted = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    snapshot_hash = models.CharField(max_length=64, blank=True)
    pushed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("sync state")
        verbose_name_plural = _("sync states")

    def __str__(self):
        return self.external_key


class SuggestionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    APPLIED = "applied", _("Applied")
    REJECTED = "rejected", _("Rejected")
    SUPERSEDED = "superseded", _("Superseded")
    OUTDATED = "outdated", _("Source changed")


class Suggestion(models.Model):
    """A machine translation waiting for an editor (model adapters: parler, modeltranslation).

    Nothing is written to the translated model until an editor applies it. Applied and
    rejected suggestions are reported to the cloud by the next sync (`reported_at`).
    """

    result_id = models.CharField(max_length=64, unique=True)
    adapter = models.CharField(max_length=50)
    external_key = models.CharField(max_length=255, db_index=True)
    field = models.CharField(max_length=255)
    language = models.CharField(max_length=15)
    text = models.TextField(help_text=_("The translation as delivered."))
    final_text = models.TextField(
        blank=True, help_text=_("What the editor applied, if they changed the text.")
    )
    format = models.CharField(max_length=20, default="plain")
    source_fingerprint = models.CharField(max_length=64)
    origin = models.CharField(max_length=20, blank=True)
    qa = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20, choices=SuggestionStatus.choices, default=SuggestionStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reported_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("translation suggestion")
        verbose_name_plural = _("translation suggestions")
        indexes = [models.Index(fields=["status", "reported_at"])]

    def __str__(self):
        return f"{self.external_key} {self.field} [{self.language}]"

    @property
    def applied_text(self):
        return self.final_text or self.text

    @property
    def has_errors(self):
        return any(issue.get("severity") == "error" for issue in self.qa or [])

    @property
    def edited(self):
        return bool(self.final_text) and self.final_text != self.text


class Exclusion(models.Model):
    """Content kept out of translation, for all languages or some (ADR 0002).

    Everything excluded: the unit is removed from the cloud and no longer pushed. Some languages:
    they're left out of the unit's writable languages, so no job translates into them.
    """

    adapter = models.CharField(max_length=50)
    external_key = models.CharField(max_length=255, unique=True)
    languages = models.JSONField(
        default=list, blank=True, help_text=_("Language codes; empty means all languages.")
    )
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("exclusion from translation")
        verbose_name_plural = _("exclusions from translation")

    def __str__(self):
        return f"{self.external_key} ({', '.join(self.languages) or _('all languages')})"

    @property
    def all_languages(self):
        return not self.languages


class HeldBack(models.Model):
    """A translation the cloud holds back for QA review (a copy of `GET /attention`, replaced
    by every sync), so editors can see what is missing and why."""

    external_key = models.CharField(max_length=255, db_index=True)
    label = models.CharField(max_length=500, blank=True)
    key = models.CharField(max_length=500)
    language = models.CharField(max_length=15)
    issues = models.JSONField(default=list, blank=True)
    since = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["external_key", "key", "language"]
        verbose_name = _("translation waiting for QA review")
        verbose_name_plural = _("translations waiting for QA review")

    def __str__(self):
        return f"{self.label or self.external_key} · {self.key} [{self.language}]"
