"""Emails to editors when new translations are ready for review (`NOTIFY_EDITORS`, ADR 0002).

Sent through the site's own email setup, at most one per sync, summarizing what arrived.
"""

import logging
from collections import Counter

from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse

from .conf import get_config

logger = logging.getLogger(__name__)


def recipients():
    setting = get_config().notify_editors
    if not setting:
        return []
    if isinstance(setting, (list, tuple)):
        return list(setting)
    users = get_user_model().objects.with_perm(
        "ponyglot.change_suggestion", include_superusers=True
    )
    return sorted({user.email for user in users.filter(is_active=True) if user.email})


def review_url():
    try:
        path = reverse("admin:ponyglot_suggestion_changelist") + "?status__exact=pending"
    except NoReverseMatch:  # the admin isn't installed
        return ""
    return get_config().site_url + path


def notify(suggestions):
    """Tell editors about `suggestions` (new, pending). Returns whether an email was sent."""
    to = recipients()
    if not suggestions or not to:
        return False
    context = {
        "count": len(suggestions),
        "languages": sorted(Counter(s.language for s in suggestions).items()),
        "errors": sum(1 for s in suggestions if s.has_errors),
        "url": review_url(),
    }
    try:
        send_mail(
            subject=render_to_string("ponyglot/email/ready_subject.txt", context).strip(),
            message=render_to_string("ponyglot/email/ready_message.txt", context),
            from_email=None,
            recipient_list=to,
        )
    except Exception:  # an email problem must not fail the sync
        logger.exception("Ponyglot: sending the review notification failed")
        return False
    return True
