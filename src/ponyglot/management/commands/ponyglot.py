"""`manage.py ponyglot …`: connect, sync, backfill, status and translation jobs."""

import signal
import time

from django.core.management.base import BaseCommand, CommandError

from ponyglot import sync
from ponyglot.adapters import registry
from ponyglot.client import APIError, Client, ConfigurationError
from ponyglot.conf import get_config

SYMBOLS = {"ok": "✓", "stale": "stale", "review": "review", "missing": "·", "attention": "!"}


class Command(BaseCommand):
    help = "Ponyglot: check the connection, sync content, request translations."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        actions.add_parser("check", help="Check settings and the connection (handshake).")
        backfill = actions.add_parser(
            "backfill", help="Queue every object for the next sync (first setup, new models)."
        )
        backfill.add_argument("--adapter", action="append", help="only this adapter (repeatable)")
        backfill.add_argument(
            "--dry-run", action="store_true", help="only count units, segments and characters"
        )
        run = actions.add_parser("sync", help="Push changes, report reviews, pull translations.")
        run.add_argument("--loop", type=int, metavar="SECONDS", help="repeat every SECONDS")
        run.add_argument("--no-pull", action="store_true", help="don't fetch translations")
        status = actions.add_parser("status", help="Site status, or the matrix of one unit.")
        status.add_argument("external_key", nargs="?")
        translate = actions.add_parser("translate", help="Request translation of what changed.")
        translate.add_argument("--language", action="append", dest="languages")
        translate.add_argument("--unit", action="append", dest="units")
        translate.add_argument("--force", action="store_true", help="also up-to-date segments")
        translate.add_argument("--estimate", action="store_true", help="only show the cost")
        translate.add_argument("--yes", action="store_true", help="confirm large jobs")
        translate.add_argument("--wait", action="store_true", help="wait until the job is done")

    def handle(self, *args, action, **options):
        if not registry.all():
            raise CommandError(
                "No adapter installed: add e.g. 'ponyglot.contrib.parler' to INSTALLED_APPS."
            )
        if action == "backfill" and options["dry_run"]:
            return self.estimate(options)
        try:
            client = Client()
            getattr(self, f"do_{action}")(client, **options)
        except ConfigurationError as error:
            raise CommandError(str(error)) from error
        except APIError as error:
            raise CommandError(str(error)) from error

    # --- Actions ---------------------------------------------------------------------------

    def do_check(self, client, **options):
        config = get_config()
        self.stdout.write(f"API: {client.api_url}")
        self.stdout.write(f"Adapters: {', '.join(a.name for a in registry.all())}")
        self.stdout.write(f"Source language: {config.source_language}")
        self.stdout.write(f"Languages: {', '.join(config.languages)}")
        report = sync.Report()
        for warning in sync.handshake(client, report):
            self.stdout.write(self.style.WARNING(f"! {warning}"))
        if report.qa_mode:
            who = "editors fix them" if report.qa_mode == "editors" else "a separate QA review"
            self.stdout.write(f"QA errors: {who} ({report.qa_mode})")
        site = client.request("GET", "/site")
        writable = [
            lang["code"] for lang in site.get("target_languages", []) if lang.get("writable")
        ]
        self.stdout.write(
            self.style.SUCCESS(f"Connected to site “{site.get('name')}”.")
            + f" Target languages: {', '.join(writable) or 'none writable'}"
        )

    def estimate(self, options):
        units, segments, characters = sync.estimate(options.get("adapter"))
        self.stdout.write(
            f"{units} units, {segments} segments, {characters:,} source characters. "
            "Translation memory hits and unchanged content aren't billed; `ponyglot translate "
            "--estimate` shows the cost after syncing."
        )

    def do_backfill(self, client, **options):
        count = sync.backfill(options.get("adapter"))
        self.stdout.write(f"{count} objects queued. Pushing…")
        self.do_sync(client, no_pull=True)

    def do_sync(self, client, loop=None, no_pull=False, **options):
        stopping = []
        if loop:
            signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
        while True:
            started = time.monotonic()
            try:
                report = sync.run(client, do_pull=not no_pull)
                self.print_report(report)
            except APIError as error:
                if not loop:
                    raise
                self.stderr.write(f"Sync failed: {error}")
            if not loop or stopping:
                return
            while not stopping and time.monotonic() - started < loop:
                time.sleep(1)
            if stopping:
                return

    def print_report(self, report):
        for warning in report.warnings:
            self.stdout.write(self.style.WARNING(f"! {warning}"))
        self.stdout.write(
            f"pushed {report.pushed}, unchanged {report.unchanged}, deleted {report.deleted}, "
            f"reviews reported {report.reviews_reported}, translations received "
            f"{report.delivered}"
        )
        if report.held_back:
            self.stdout.write(f"{report.held_back} translation(s) waiting for a QA review")
        for key, message in report.push_errors:
            self.stderr.write(f"! {key}: {message}")
        for prefix, count in report.undeliverable.items():
            self.stderr.write(
                f"! {count} translation(s) for '{prefix}' units wait for an adapter that isn't "
                "installed here."
            )

    def do_status(self, client, external_key=None, **options):
        if external_key is None:
            data = client.site_status()
            self.stdout.write(
                f"{data['units_total']} units, {data['segments_total']} segments, glossary: "
                f"{data.get('glossary_terms', 0)} terms"
            )
            for lang in data.get("languages", []):
                self.stdout.write(
                    f"  {lang['code']:6} ✓ {lang['ok']}  stale {lang['stale']}  review "
                    f"{lang['review']}  missing {lang['missing']}  attention {lang['attention']}"
                )
            return
        data = client.unit_status(external_key)
        codes = [lang["code"] for lang in data["languages"]]
        self.stdout.write(f"{data['label'] or data['external_key']}  {data.get('path') or ''}")
        self.stdout.write(
            f"{data['segments_needing_work']} of {data['segments_total']} segments need work\n"
        )
        width = max([len(s["key"]) for s in data["segments"]] + [7])
        self.stdout.write(
            "segment".ljust(width) + " " + " ".join(c.upper().ljust(7) for c in codes)
        )
        for segment in data["segments"]:
            cells = " ".join(SYMBOLS[segment["states"][c]].ljust(7) for c in codes)
            self.stdout.write(segment["key"].ljust(width) + " " + cells)

    def do_translate(self, client, **options):
        payload = {"type": "translate" if options["units"] else "delta", "force": options["force"]}
        if options["units"]:
            payload["units"] = options["units"]
        if options["languages"]:
            payload["languages"] = options["languages"]
        if options["estimate"]:
            payload["estimate_only"] = True
            estimate = client.create_job(**payload)
            self.print_estimate(estimate)
            return
        if options["yes"]:
            payload["confirm"] = True
        try:
            job = client.create_job(**payload)
        except APIError as error:
            if error.code == "confirmation_required":
                self.print_estimate(error.data.get("estimate", {}))
                raise CommandError("This job needs confirmation: run again with --yes.") from error
            raise
        if options["wait"]:
            while job["status"] in ("queued", "running"):
                time.sleep(2)
                job = client.job(job["id"])
        self.stdout.write(
            f"job {job['id']}: {job['status']} · {job.get('done_segments', 0)}/"
            f"{job.get('estimated_segments', 0)} segments · {job.get('characters', 0):,} characters"
        )
        for error in job.get("errors", []):
            self.stderr.write(f"! {error.get('language') or '*'}: {error['message']}")
        if options["wait"] and job["status"] == "succeeded":
            self.stdout.write("Run `manage.py ponyglot sync` to fetch the translations.")

    def print_estimate(self, estimate):
        cost = estimate.get("cost") or {}
        self.stdout.write(
            f"{estimate.get('estimated_segments', 0)} segments, "
            f"{estimate.get('estimated_characters', 0):,} billable characters "
            f"({estimate.get('estimated_tm_segments', 0)} from translation memory, free)"
        )
        if cost:
            self.stdout.write(
                f"Included this month: {cost.get('remaining_characters', 0):,} characters left; "
                f"overage: {cost.get('overage_characters', 0):,} characters "
                f"({cost.get('overage_cents', 0) / 100:.2f} {cost.get('currency', 'EUR')})"
            )
