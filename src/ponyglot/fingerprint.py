"""Source fingerprints, as the cloud computes them (API v1, "Fingerprints").

The cloud's value is authoritative; the connector uses the same rules only to notice changes
and to refuse applying a suggestion whose source changed since it was translated. The rules
are frozen for API v1:

1. Unicode NFC normalization.
2. CRLF and CR become LF.
3. `plain` and `markdown`: trailing whitespace is removed from every line, runs of spaces and
   tabs inside a line become one space, leading and trailing blank lines are removed.
4. `html` and `rich_text`: every run of whitespace (including line breaks) becomes one space,
   then the text is trimmed. Markup is not re-serialized.
5. Lowercase hex SHA-256 of the UTF-8 encoded result.
"""

import hashlib
import re
import unicodedata

WHITESPACE_INSENSITIVE_FORMATS = {"html", "rich_text"}
_INLINE_SPACE = re.compile(r"[ \t]+")
_ANY_SPACE = re.compile(r"\s+")


def normalize(text, format="plain"):
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    if format in WHITESPACE_INSENSITIVE_FORMATS:
        return _ANY_SPACE.sub(" ", text).strip()
    lines = [_INLINE_SPACE.sub(" ", line).rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip("\n").strip()


def fingerprint(text, format="plain"):
    return hashlib.sha256(normalize(text, format).encode()).hexdigest()
