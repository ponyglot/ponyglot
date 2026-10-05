"""The `ponyglot` command for projects without Django settings: `ponyglot po sync`.

Configuration from the environment: `PONYGLOT_API_KEY` (required) and `PONYGLOT_API_URL`
(default: the hosted cloud). Django projects use `manage.py ponyglot` for their content.
"""

import argparse
import os
import sys

from .client import APIError, Client, ConfigurationError
from .conf import DEFAULT_API_URL


def parser():
    main = argparse.ArgumentParser(prog="ponyglot", description="Ponyglot command line.")
    groups = main.add_subparsers(dest="group", required=True)
    po = groups.add_parser("po", help="gettext catalogs (.po)")
    actions = po.add_subparsers(dest="action", required=True)
    sync = actions.add_parser(
        "sync",
        help="Push the catalogs, approve merged translations, translate, write .po and .mo.",
    )
    sync.add_argument(
        "locale_dirs", nargs="*", default=["locale"], help="locale directories (default: locale)"
    )
    sync.add_argument(
        "--domain",
        action="append",
        dest="domains",
        help="gettext domain (repeatable; default: django and djangojs)",
    )
    sync.add_argument("--source-language", default="en", help="language of the msgids (en)")
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
    return main


def main(argv=None, *, transport=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    options = parser().parse_args(argv)
    try:
        import polib  # noqa: F401
    except ImportError:
        print('polib is missing: pip install "ponyglot[gettext]"', file=err)
        return 2
    from . import po

    api_key = os.environ.get("PONYGLOT_API_KEY", "")
    if not api_key:
        print("Set PONYGLOT_API_KEY (the site's API key from the dashboard).", file=err)
        return 2
    try:
        client = Client(
            api_key,
            os.environ.get("PONYGLOT_API_URL") or DEFAULT_API_URL,
            timeout=30,
            transport=transport,
        )
        report = po.sync(
            client,
            options.locale_dirs,
            domains=options.domains or po.DEFAULT_DOMAINS,
            source_language=options.source_language,
            translate=not options.no_translate,
            confirm=options.yes,
            fuzzy=options.fuzzy,
            compile_mo=not options.no_mo,
            wait=options.wait,
        )
    except ConfigurationError as error:
        print(f"ponyglot: {error}", file=err)
        return 2
    except (APIError, po.POError) as error:
        print(f"ponyglot: {error}", file=err)
        return 1
    job = report.job
    print(
        f"{report.catalogs} catalog(s) · approved {report.approved}, corrected "
        f"{report.corrected} · written {report.written}"
        + (f" · job {job['id']}: {job['status']}" if job else ""),
        file=out,
    )
    for warning in report.warnings:
        print(f"! {warning}", file=err)
    return 0


if __name__ == "__main__":
    sys.exit(main())
