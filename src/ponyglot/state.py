"""Marking units dirty or deleted when content changes (signal receivers call these)."""

from django.db import IntegrityError, transaction
from django.db.models import F

from .models import SyncState


def mark_dirty(adapter, obj):
    _mark(adapter.name, adapter.external_key(obj), deleted=False)


def mark_deleted(adapter, obj):
    _mark(adapter.name, adapter.external_key(obj), deleted=True)


def _mark(adapter_name, external_key, *, deleted):
    changes = {"dirty": True, "deleted": deleted, "version": F("version") + 1}
    if SyncState.objects.filter(external_key=external_key).update(**changes):
        return
    try:
        with transaction.atomic():
            SyncState.objects.create(
                external_key=external_key, adapter=adapter_name, dirty=True, deleted=deleted
            )
    except IntegrityError:  # created concurrently
        SyncState.objects.filter(external_key=external_key).update(**changes)


def connect_signals(adapter):
    """Mark objects dirty on save and deleted on delete (called from the adapter's app)."""
    from django.core.exceptions import ObjectDoesNotExist
    from django.db.models.signals import post_delete, post_save

    owners = set(adapter.owner_models())

    def owner_of(sender, instance):
        try:
            owner = adapter.watched_models()[sender](instance)
        except ObjectDoesNotExist:  # the owner is being deleted; its own signal handles it
            return None
        return owner if owner is not None and owner.pk is not None else None

    def saved(sender, instance, raw=False, **kwargs):
        if raw:  # loaddata
            return
        owner = owner_of(sender, instance)
        if owner is not None:
            mark_dirty(adapter, owner)

    def deleted(sender, instance, **kwargs):
        if sender in owners:
            mark_deleted(adapter, instance)
            return
        owner = owner_of(sender, instance)
        if owner is not None:
            mark_dirty(adapter, owner)

    for model in adapter.watched_models():
        uid = f"ponyglot:{adapter.name}:{model._meta.label_lower}"
        post_save.connect(saved, sender=model, weak=False, dispatch_uid=uid)
        post_delete.connect(deleted, sender=model, weak=False, dispatch_uid=uid)
