"""The sync run: push dirty units, report review outcomes, pull and deliver results."""

import hashlib
import json
import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from . import exclusions, notifications, state
from .adapters import registry
from .client import APIError, Client
from .conf import get_config
from .models import HeldBack, Suggestion, SuggestionStatus, SyncRun, SyncState
from .suggestions import review_payload, unreported

logger = logging.getLogger(__name__)


@dataclass
class Report:
    pushed: int = 0
    unchanged: int = 0
    deleted: int = 0
    push_errors: list = field(default_factory=list)
    reviews_reported: int = 0
    delivered: int = 0
    undeliverable: dict = field(default_factory=dict)  # adapter prefix → count
    held_back: int = 0
    notified: bool = False
    qa_mode: str = ""
    warnings: list = field(default_factory=list)


def snapshot_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def capabilities():
    """What this installation can handle (cloud ADR 0014): `qa_errors` only if every adapter
    shows QA errors to editors; `whole_unit` if any adapter needs units in one piece."""
    adapters = registry.all()
    result = []
    if adapters and all("qa_errors" in adapter.capabilities for adapter in adapters):
        result.append("qa_errors")
    if any("whole_unit" in adapter.capabilities for adapter in adapters):
        result.append("whole_unit")
    return result


def handshake(client, report=None):
    config = get_config()
    response = client.handshake(
        adapters=[adapter.name for adapter in registry.all()],
        source_language=config.source_language,
        languages=config.languages,
        capabilities=capabilities(),
    )
    if report is not None:
        report.qa_mode = response.get("qa_mode", "")
    return response.get("warnings", [])


def backfill(adapter_names=None):
    """Mark every object of the adapters dirty; the next sync pushes what changed."""
    count = 0
    for adapter in registry.all():
        if adapter_names and adapter.name not in adapter_names:
            continue
        for obj in adapter.iter_objects():
            state.mark_dirty(adapter, obj)
            count += 1
    return count


def estimate(adapter_names=None):
    """Units, segments and source characters the adapters would push (no HTTP)."""
    units = segments = characters = 0
    for adapter in registry.all():
        if adapter_names and adapter.name not in adapter_names:
            continue
        for obj in adapter.iter_objects():
            unit = adapter.snapshot(obj)
            if unit is not None:
                unit = exclusions.apply(unit)
            if unit is None:
                continue
            units += 1
            segments += len(unit.segments)
            characters += sum(len(segment.text) for segment in unit.segments)
    return units, segments, characters


def push(client, report, keys=None, prefix=None):
    """Push dirty units (`keys`: only these external keys; `prefix`: keys starting with it)."""
    batch_size = get_config().batch_size
    pending = SyncState.objects.filter(dirty=True).order_by("pk")
    if keys is not None:
        pending = pending.filter(external_key__in=keys)
    if prefix:
        pending = pending.filter(external_key__startswith=prefix)
    pending = list(pending)
    batch = []
    for sync_state in pending:
        adapter = registry.get(sync_state.adapter)
        if adapter is None:
            continue  # adapter not installed here (any more)
        obj = None if sync_state.deleted else adapter.get_object(sync_state.external_key)
        if obj is None:
            _delete_remote(client, sync_state, report)
            continue
        unit = adapter.snapshot(obj)
        if unit is not None:
            unit = exclusions.apply(unit)
        if unit is None:  # no source content (any more), or excluded from translation
            if sync_state.pushed_at:
                _delete_remote(client, sync_state, report, keep_state=True)
            else:
                _clear(sync_state)
            continue
        first_push = sync_state.pushed_at is None
        digest = snapshot_hash(unit.as_payload(with_translations=False))
        if not first_push and digest == sync_state.snapshot_hash:
            _clear(sync_state)
            report.unchanged += 1
            continue
        batch.append((sync_state, digest, unit.as_payload(with_translations=first_push)))
        if len(batch) == batch_size:
            _send(client, batch, report)
            batch = []
    if batch:
        _send(client, batch, report)


def _send(client, batch, report):
    try:
        results = client.push_units(payload for _, _, payload in batch)
    except APIError as error:
        for sync_state, _, _ in batch:
            _failed(sync_state, str(error), report)
        if error.status in (0, 401):
            raise  # unreachable or bad key: stop this run
        return
    now = timezone.now()
    for (sync_state, digest, _), result in zip(batch, results, strict=True):
        if result.get("ok"):
            _clear(sync_state, snapshot_hash=digest, pushed_at=now, error="")
            report.pushed += 1
        else:
            _failed(sync_state, result.get("error") or "rejected", report)


def _clear(sync_state, **changes):
    """Mark pushed, unless the object changed again meanwhile (then it stays dirty)."""
    updated = SyncState.objects.filter(pk=sync_state.pk, version=sync_state.version).update(
        dirty=False, **changes
    )
    if not updated and changes:
        changes.pop("error", None)
        SyncState.objects.filter(pk=sync_state.pk).update(**changes)


def _failed(sync_state, message, report):
    SyncState.objects.filter(pk=sync_state.pk).update(error=message[:2000])
    report.push_errors.append((sync_state.external_key, message))


def _delete_remote(client, sync_state, report, keep_state=False):
    try:
        client.delete_unit(sync_state.external_key)
    except APIError as error:
        _failed(sync_state, str(error), report)
        if error.status in (0, 401):
            raise
        return
    Suggestion.objects.filter(
        external_key=sync_state.external_key, status=SuggestionStatus.PENDING
    ).update(status=SuggestionStatus.OUTDATED, decided_at=timezone.now())
    if keep_state:
        _clear(sync_state, pushed_at=None, snapshot_hash="", error="")
    else:
        SyncState.objects.filter(pk=sync_state.pk, version=sync_state.version).delete()
    report.deleted += 1


def report_reviews(client, report):
    suggestions = list(unreported().select_related("decided_by")[:500])
    if not suggestions:
        return
    client.report_reviews(review_payload(s) for s in suggestions)
    Suggestion.objects.filter(pk__in=[s.pk for s in suggestions]).update(reported_at=timezone.now())
    report.reviews_reported += len(suggestions)


def pull(client, report, unit=None, prefix=None):
    """Fetch and deliver finished translations (`unit`: only that unit's; `prefix`: only
    units whose key starts with it, e.g. one model's)."""
    started = timezone.now()
    by_adapter = {}
    for result in client.results(unit=unit):
        if unit is not None and result["unit"] != unit:
            continue  # a cloud from before the filter sends everything
        if prefix and not result["unit"].startswith(prefix):
            continue
        by_adapter.setdefault(result["unit"].split(":", 1)[0], []).append(result)
    for prefix, results in by_adapter.items():
        adapter = registry.get(prefix)
        if adapter is None:
            report.undeliverable[prefix] = len(results)
            continue
        with transaction.atomic():
            handled = adapter.deliver(results)
        if handled:
            client.acknowledge(handled)
            report.delivered += len(handled)
    new = list(Suggestion.objects.filter(status=SuggestionStatus.PENDING, created_at__gte=started))
    report.notified = notifications.notify(new)


def refresh_held_back(client, report):
    """Mirror the translations the cloud holds back for QA review (for editors' UIs)."""
    try:
        data = client.attention()
    except APIError as error:
        if error.status == 404:  # a cloud without QA modes
            return
        raise
    with transaction.atomic():
        HeldBack.objects.all().delete()
        HeldBack.objects.bulk_create(
            HeldBack(
                external_key=item["unit"],
                label=item.get("label", "")[:500],
                key=item["key"],
                language=item["language"],
                issues=item.get("issues", []),
                since=item.get("since"),
            )
            for item in data.get("items", [])
        )
    report.held_back = len(data.get("items", []))


def run(client=None, *, do_push=True, do_pull=True):
    """One sync round; returns a `Report`. Raises `APIError` if the cloud is unreachable.
    Every round is recorded (`SyncRun`), so UIs can show when translations were last fetched."""
    client = client or Client()
    record = SyncRun.objects.create()
    report = Report()
    try:
        report.warnings = handshake(client, report)
        if do_push:
            push(client, report)
        report_reviews(client, report)
        if do_pull:
            pull(client, report)
            refresh_held_back(client, report)
    except Exception as error:
        _finish(record, report, error=str(error)[:2000])
        raise
    _finish(record, report)
    return report


def refresh(client, external_key=None, *, prefix=None):
    """One unit's round (or all units whose key starts with `prefix`, e.g. one model's), for
    editor UIs when no sync schedule ran lately: push what changed, fetch the translations.
    Recorded for the unit or prefix only (`last_fetch`, not `last_run`)."""
    record = SyncRun.objects.create(unit=external_key or prefix or "")
    report = Report()
    try:
        if external_key:
            push(client, report, keys=[external_key])
            pull(client, report, unit=external_key)
        else:
            push(client, report, prefix=prefix)
            pull(client, report, prefix=prefix)
    except Exception as error:
        _finish(record, report, error=str(error)[:2000])
        raise
    _finish(record, report)
    return report


def _finish(record, report, error=""):
    from dataclasses import asdict

    record.finished_at = timezone.now()
    record.ok = not error
    record.error = error
    record.report = asdict(report)
    record.save()
    stale = SyncRun.objects.values_list("pk", flat=True)[SyncRun.KEEP :]
    SyncRun.objects.filter(pk__in=list(stale)).delete()


def last_run():
    """The latest finished full sync round (a running schedule), or None."""
    return SyncRun.objects.filter(finished_at__isnull=False, unit="").first()


def last_fetch(external_key):
    """When a unit's translations were last fetched: by a full round, for this unit, or for
    its model (a key prefix such as `parler:blog.post:`; pass a prefix to ask about it)."""
    scopes = ["", external_key]
    if not external_key.endswith(":"):
        scopes.append(external_key.rsplit(":", 1)[0] + ":")
    return SyncRun.objects.filter(finished_at__isnull=False, ok=True, unit__in=scopes).first()
