"""Same rules and vectors as the cloud (ponyglot-cloud tests/test_fingerprint.py, API v1)."""

import hashlib

from ponyglot.fingerprint import fingerprint, normalize


def test_known_value_is_frozen():
    assert fingerprint("Hello  world \n") == hashlib.sha256(b"Hello world").hexdigest()


def test_unicode_is_nfc_normalized():
    assert fingerprint("Café") == fingerprint("Café")


def test_line_endings():
    assert normalize("a\r\nb\rc") == "a\nb\nc"


def test_plain_keeps_line_breaks_but_collapses_spaces():
    assert normalize("  one \t two  \n\nthree   \n\n") == "one two\n\nthree"
    assert fingerprint("a\nb") != fingerprint("a b")


def test_markdown_keeps_line_breaks():
    assert fingerprint("# Title\n\n- item", "markdown") != fingerprint("# Title - item", "markdown")


def test_html_ignores_whitespace_layout():
    assert fingerprint("<p>Hello\n   world</p>\n", "html") == fingerprint(
        "<p>Hello world</p>", "html"
    )
    assert fingerprint("<p>Hello</p>", "html") != fingerprint('<p class="x">Hello</p>', "html")
