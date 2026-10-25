"""Publishing right away, on the requesting editor's behalf (cloud ADR 0022).

When requesting a translation of chosen content (an object's dialog, selected documents), an
editor who may publish it can tick "Publish the translation right away" (pre-ticked with
`PONYGLOT["PUBLISH_BY_DEFAULT"]`). The job then carries `publish` and the editor's id
(`publish_as`), and so does every result. The adapters write the results as always, then
publish one object × language as that editor if:

- every result of it in the delivery asked for it, for the same editor, who is still active
  and may still publish it;
- no result has a QA error;
- what goes live is only what was requested: no earlier translation still waits for review in
  the same draft, and nobody else changed the draft (each adapter checks its own drafts).

Otherwise the draft or suggestion stays for an editor, and the sync report says why. Published
translations are marked `published` (not `applied`): the cloud records them as published
without review and keeps them out of the translation memory until someone confirms them.
"""

import logging
import threading
from contextlib import contextmanager

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext as _

from .conf import get_config
from .models import Suggestion, SuggestionStatus

logger = logging.getLogger(__name__)
FIELD = "publish"  # the request form's checkbox
_local = threading.local()


def default():
    """Whether the checkbox starts ticked."""
    return get_config().publish_by_default


def choice(user, adapter, obj):
    """The checkbox for a dialog: `{"offered", "checked"}` (offered if `user` may publish)."""
    offered = bool(adapter and obj is not None and adapter.can_publish(user, obj))
    return {"offered": offered, "checked": offered and default()}


def requested(request):
    return request.method == "POST" and request.POST.get(FIELD) == "1"


def job_options(request):
    """What to add to a translate job's payload when the editor asked to publish right away."""
    if not requested(request):
        return {}
    return {"publish": True, "publish_as": str(request.user.pk)}


def editor(results):
    """`(user, reason)` for one object × language's results: the editor to publish as, or None
    with a reason if publishing was asked for but can't happen; `(None, "")` if not asked."""
    flags = [(bool(result.get("publish")), result.get("publish_as") or "") for result in results]
    if not any(publish for publish, _editor in flags):
        return None, ""
    editors = {editor_id for _publish, editor_id in flags}
    if not all(publish for publish, _editor in flags) or len(editors) != 1 or "" in editors:
        return None, _("not every translation was requested to be published, by one editor")
    (user_id,) = editors
    User = get_user_model()
    try:
        user = User._default_manager.filter(pk=user_id, is_active=True).first()
    except (ValueError, ValidationError):
        user = None
    if user is None:
        return None, _("the requesting editor no longer exists or is inactive")
    issues = [issue for result in results for issue in result.get("qa") or []]
    if any(issue.get("severity") == "error" for issue in issues):
        return None, _("a quality check found an error")
    return user, ""


def others_waiting(external_key, language, result_ids):
    """Whether the object's draft in `language` holds translations still waiting for review
    besides these results (they would go live unreviewed)."""
    return (
        Suggestion.objects.filter(
            external_key=external_key, language=language, status=SuggestionStatus.DRAFTED
        )
        .exclude(result_id__in=list(result_ids))
        .exists()
    )


def mark(suggestions, user):
    """Mark written suggestions as published by `user` (reported as `published`)."""
    return suggestions.update(
        status=SuggestionStatus.PUBLISHED, decided_at=timezone.now(), decided_by=user
    )


def not_published(external_key, language, reason):
    """Record why a requested publication didn't happen (the sync report lists it)."""
    message = _("Not published: %(key)s [%(language)s]: %(reason)s") % {
        "key": external_key,
        "language": language,
        "reason": reason,
    }
    logger.info(message)
    notes = getattr(_local, "notes", None)
    if notes is not None:
        notes.append(message)


@contextmanager
def collect():
    """The `not_published` messages recorded inside the block."""
    outer = getattr(_local, "notes", None)
    _local.notes = found = []
    try:
        yield found
    finally:
        _local.notes = outer
        if outer is not None:
            outer.extend(found)
