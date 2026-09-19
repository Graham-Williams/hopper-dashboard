"""Parse ~/personal-assistant/backlog.txt into the Inbox's mirror payload.

backlog.txt is Hopper's "someday" list: free text, edited by hand, with NO identifiers of any
kind. Entries are separated by a line containing exactly ``---``; each opens with ``What: ``
at column 0, followed by ad-hoc labelled lines (``Why:``, ``How:``, ``Notes:``, ``Issues:``,
``Scope:``, ``Cadence:`` — not a fixed schema) whose wrapped continuations are indented. Status
lives inside the ``What:`` text by convention (``⭐ PRIORITY``, ``🔁``, ``💤 PARKED``,
``✅ DONE <date>``, ``⚠️``), and done entries stay in the file rather than being deleted.

Because there is no id, an entry is addressed only by its ``What:`` text, so the mirror key is
derived from it — sha256 of the lowercased, whitespace-collapsed line, first 16 hex characters,
prefixed ``backlog:``.

⚠️ THAT DERIVATION IS DUPLICATED, DELIBERATELY. ``dashboard/inbox_db.normalise_backlog_key``
computes the same key server-side, and the two must agree byte for byte or every sync would
archive every row and re-create it under a new key. It cannot simply be imported: ``probes/``
is stdlib-only and runs under a stock /usr/bin/python3 that has never heard of Flask. The
parity is pinned instead by ``tests/test_backlog_mirror.py``, which imports BOTH and asserts
they agree over a corpus including the real file.

Python 3.9 compatible, stdlib only.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, List, Optional

from probes.common import ProbeError

#: Matches dashboard/inbox_db.MAX_TEXT. The server truncates to it before hashing, so the
#: client must truncate to the same length before hashing or long entries key differently.
MAX_TEXT = 20000
MIRROR_BACKLOG = "backlog"

DEFAULT_BACKLOG_FILE = os.path.expanduser("~/personal-assistant/backlog.txt")

#: An entry separator is a line that is EXACTLY three dashes. Not a prefix match: a wrapped
#: continuation line could legitimately begin with "---" as an em-dash-ish bullet.
SEPARATOR_RE = re.compile(r"^-{3}$")

#: A label at column 0 starts a new field; anything indented continues the previous one.
#: Kept deliberately loose (the schema is ad-hoc and grows) but anchored and bounded, so a
#: sentence containing a colon mid-line cannot masquerade as a new field.
LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 ()/_'’-]{0,40}):(?:\s(.*))?$")

WHAT_LABEL = "what"

#: A file this small is either truncated or being written; syncing it would archive
#: everything. The endpoint refuses an empty list anyway — this refuses earlier and louder.
MIN_ENTRIES = 1


class Entry(object):
    """One backlog entry: its stable key, its ``What:`` line, and the full text."""

    __slots__ = ("key", "what", "text", "project")

    def __init__(self, key, what, text, project=None):
        self.key = key
        self.what = what
        self.text = text
        self.project = project

    def as_payload(self) -> Dict[str, object]:
        item = {"key": self.key, "text": self.text}
        if self.project:
            item["project"] = self.project
        return item

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Entry(%r, %r)" % (self.key, self.what[:40])


def clean_text(value: Optional[str], max_len: int = MAX_TEXT) -> str:
    """Byte-for-byte the same normalisation as ``dashboard/inbox_db.clean_text``: drop C0
    control characters except tab/newline (and DEL), strip, then truncate."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = "".join(c for c in text
                   if c in "\t\n" or (ord(c) >= 0x20 and ord(c) != 0x7f))
    return text.strip()[:max_len]


def normalise_backlog_key(what: str) -> str:
    """``backlog:<sha256(lowercased, whitespace-collapsed What line)[:16]>``.

    Must stay identical to ``dashboard/inbox_db.normalise_backlog_key`` — see the module
    docstring. Case and whitespace are normalised so re-wrapping a line does not orphan its
    row; any other edit IS a different item, which archives the old row and creates a new one.
    """
    norm = " ".join(clean_text(what, MAX_TEXT).lower().split())
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return "%s:%s" % (MIRROR_BACKLOG, digest)


def _split_entries(text: str) -> List[List[str]]:
    chunks: List[List[str]] = []
    current: List[str] = []
    for line in text.splitlines():
        if SEPARATOR_RE.match(line.rstrip()):
            chunks.append(current)
            current = []
        else:
            current.append(line)
    chunks.append(current)
    return chunks


def _fields(lines: List[str]) -> List[List[str]]:
    """Group a chunk's lines into ``[label-line, continuation…]`` blocks.

    A block starts at a column-0 ``Label:`` line; everything until the next one belongs to it.
    Lines before the first label (the file's header preamble) are dropped.
    """
    blocks: List[List[str]] = []
    for line in lines:
        if LABEL_RE.match(line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    return blocks


def _what_of(block: List[str]) -> str:
    """The ``What:`` value with its wrapped continuation joined onto one line.

    Joining matters: the key is a hash of this string, and folding the continuation in is
    what makes a pure re-wrap of a long ``What:`` line a no-op rather than an archive +
    re-create. Whitespace is collapsed by the hash anyway.
    """
    m = LABEL_RE.match(block[0])
    parts = [(m.group(2) or "").strip()]
    parts.extend(line.strip() for line in block[1:])
    return " ".join(p for p in parts if p)


def parse_backlog(text: str) -> List[Entry]:
    """Every entry in the file, in file order, de-duplicated by key.

    Duplicate keys are dropped rather than sent twice: the endpoint upserts on the key, so a
    duplicate is harmless but the ``synced`` count would lie about how many rows exist.
    """
    out: List[Entry] = []
    seen = set()
    for chunk in _split_entries(text):
        blocks = _fields(chunk)
        if not blocks:
            continue
        what_blocks = [b for b in blocks
                       if (LABEL_RE.match(b[0]).group(1) or "").strip().lower() == WHAT_LABEL]
        if not what_blocks:
            continue
        what = _what_of(what_blocks[0])
        if not what:
            continue
        # The body keeps the rest of the entry verbatim for context, but leads with the What
        # value on its own line so the server's derive_title() names the row after it.
        rest = [line for block in blocks if block is not what_blocks[0] for line in block]
        body = clean_text("\n".join([what] + rest).strip(), MAX_TEXT)
        key = normalise_backlog_key(what)
        if key in seen:
            continue
        seen.add(key)
        out.append(Entry(key=key, what=what, text=body))
    return out


def read_backlog(path: str) -> List[Entry]:
    """Parse the file at ``path``. Raises ``ProbeError`` rather than returning nothing.

    An unreadable, missing, truncated or mid-edit file and a genuinely emptied backlog arrive
    looking identical, and only one of them should archive every mirrored row — so "no
    entries" is an error here, and the endpoint refuses an empty list independently.
    """
    if not os.path.isfile(path):
        raise ProbeError("backlog file not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        raise ProbeError("could not read %s: %s" % (path, e))
    entries = parse_backlog(text)
    if len(entries) < MIN_ENTRIES:
        raise ProbeError("no entries parsed from %s — refusing to sync (an unreadable file "
                         "and an emptied one look the same, and one of them must not "
                         "archive every row)" % path)
    return entries


def build_payload(entries: List[Entry]) -> Dict[str, object]:
    """The ``POST /api/v1/inbox/mirror/backlog`` body.

    ``complete: true`` is this caller asserting it read the WHOLE file — it is what licenses
    the server to archive keys it no longer sees. ``read_backlog`` raising on an empty parse
    is what makes that assertion honest.
    """
    return {"complete": True, "items": [e.as_payload() for e in entries]}
