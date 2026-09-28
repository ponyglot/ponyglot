"""Ponyglot core: one translation layer for your Django stack.

API client, sync, the suggestion table and review admin, and the django-parler and
django-modeltranslation adapters (`ponyglot.contrib.*`). See https://ponyglot.app.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ponyglot")
except PackageNotFoundError:  # running from a source checkout without installing
    __version__ = "0.0.0"
