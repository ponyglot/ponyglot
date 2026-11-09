"""The `ponyglot` command for projects without Django settings: `ponyglot po sync|check`.

Configuration from the environment: `PONYGLOT_API_KEY` (required for `sync`) and
`PONYGLOT_API_URL` (default: the hosted cloud). Django projects use `manage.py ponyglot` for
their content, and `manage.py ponyglot po …` for the catalogs in `LOCALE_PATHS`.
"""

import argparse
import os
import sys

from .client import APIError, Client, ConfigurationError
from .conf import DEFAULT_API_URL


def add_po_actions(po, *, locale_help):
    """The `sync` and `check` actions of `po` (here and in `manage.py ponyglot`)."""
    actions = po.add_subparsers(dest="po_action", required=True)
    sync = actions.add_parser(
        "sync",
        help="Push the catalogs, approve merged translations, translate, write .po and .mo.",
    )
    check = actions.add_parser(
        "check",
        help="Fail if translations are missing, fuzzy or have broken placeholders (for CI).",
    )
    for action in (sync, check):
        action.add_argument("locale_dirs", nargs="*", help=locale_help)
        action.add_argument(
            "--domain",
            action="append",
            dest="domains",
            help="gettext domain (repeatable; default: django and djangojs)",
        )
        action.add_argument("--source-language", help="language of the msgids (default: en)")
    sync.add_argument(
        "--no-translate", action="store_true", help="don't request translations, only sync"
    )
    sync.add_argument("--yes", action="store_true", help="confirm large jobs")
    sync.add_argument(
        "--fuzzy",
        action="store_true",
        help="write new translations as fuzzy (approved once someone removes the flag)",
    )
    sync.add_argument("--no-mo", action="store_true", help="don't compile .mo files")
    sync.add_argument(
        "--wait", type=int, default=1800, metavar="SECONDS", help="how long to wait for the job"
    )


def parser():
    main = argparse.ArgumentParser(prog="ponyglot", description="Ponyglot command line.")
    groups = main.add_subparsers(dest="group", required=True)
    po = groups.add_parser("po", help="gettext catalogs (.po)")
    add_po_actions(po, locale_help="locale directories (default: locale)")
    return main


def _line(stream, text):
    stream.write(f"{text}\n")


def run_po(options, *, locale_dirs, source_language, client, out, err):
    """`po sync` or `po check` with the parsed `options`; `client()` makes the API client (only
    `sync` connects). True if all is well; raises `ConfigurationError`, `APIError`, `POError`."""
    from . import po

    domains = options.domains or po.DEFAULT_DOMAINS
    if options.po_action == "check":
        checks = po.check(locale_dirs, domains=domains, source_language=source_language)
        for result in checks:
            counts = [
                f"{number} {what}"
                for number, what in (
                    (result.untranslated, "untranslated"),
                    (result.fuzzy, "fuzzy"),
                    (len(result.errors), "with broken placeholders"),
                )
                if number
            ]
            if counts:
                _line(out, f"{result.path.as_posix()}: {', '.join(counts)}")
            for msgid, problem in result.errors:
                _line(out, f"  {msgid!r}: {problem}")
        failed = sum(not result.ok for result in checks)
        _line(
            out,
            f"{len(checks)} catalog file(s) checked: "
            + (f"{failed} need work." if failed else "all translated."),
        )
        return not failed
    report = po.sync(
        client(),
        locale_dirs,
        domains=domains,
        source_language=source_language,
        translate=not options.no_translate,
        confirm=options.yes,
        fuzzy=options.fuzzy,
        compile_mo=not options.no_mo,
        wait=options.wait,
    )
    job = report.job
    _line(
        out,
        f"{report.catalogs} catalog(s) · approved {report.approved}, corrected "
        f"{report.corrected} · written {report.written}"
        + (f" · job {job['id']}: {job['status']}" if job else ""),
    )
    for warning in report.warnings:
        _line(err, f"! {warning}")
    return True


def main(argv=None, *, transport=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    options = parser().parse_args(argv)
    try:
        import polib  # noqa: F401
    except ImportError:
        _line(err, 'polib is missing: pip install "ponyglot[gettext]"')
        return 2
    from . import po

    def client():
        api_key = os.environ.get("PONYGLOT_API_KEY", "")
        if not api_key:
            raise ConfigurationError(
                "Set PONYGLOT_API_KEY (the site's API key from the dashboard)."
            )
        return Client(
            api_key,
            os.environ.get("PONYGLOT_API_URL") or DEFAULT_API_URL,
            timeout=30,
            transport=transport,
        )

    try:
        ok = run_po(
            options,
            locale_dirs=options.locale_dirs or ["locale"],
            source_language=options.source_language or "en",
            client=client,
            out=out,
            err=err,
        )
    except ConfigurationError as error:
        _line(err, f"ponyglot: {error}")
        return 2
    except (APIError, po.POError) as error:
        _line(err, f"ponyglot: {error}")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
