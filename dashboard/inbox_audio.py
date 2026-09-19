"""Voice-note storage: files on the data volume, never BLOBs in SQLite.

Why files (the decision, not a preference): the backup snapshots and
sha256-dedupes the database on every change. Megabytes of per-note audio inside
it would make every snapshot byte-unique, defeat the dedup entirely, and push
megabytes off-box per voice note. Blobs would also turn the read role's inbox
writes into long transactions — the exact contention the two-file split exists
to avoid.

Layout, relative to ``settings.inbox_audio_dir``::

    <yyyy>/<mm>/<id>.<ext>

The path is built from the SERVER-GENERATED item id and nothing else. A client
filename is never read, never stored and never joined onto a path — which is
what makes directory traversal not a thing to defend against here so much as a
thing that cannot be expressed.

Two independent checks gate an upload, and both must pass: the declared
``Content-Type`` must be in the allow-list, AND the bytes must actually start
with that container's magic. A declared type alone is attacker-controlled
metadata; magic bytes alone would let an unknown type through under a name the
browser will later sniff. (The response also sends ``X-Content-Type-Options:
nosniff`` — see :mod:`.inbox`.)

The container is READ-ONLY apart from ``/app/data``, and runs as uid 10001, so
everything here writes under the data volume and nowhere else.
"""

from __future__ import annotations

import hashlib
import os
import re
import time as _time
from dataclasses import dataclass

#: Extension → the mime types a browser may legitimately declare for it.
#: Chrome/Android MediaRecorder emits ``audio/webm;codecs=opus``; iOS Safari
#: emits ``audio/mp4`` (AAC). Both are known to decode under the Mac's ffmpeg,
#: which is what the Whisper worker shells out to. ``audio/x-m4a`` is the one
#: alias worth carrying: some iOS builds report it for the same bytes.
CONTAINERS: dict[str, tuple[str, ...]] = {
    "webm": ("audio/webm",),
    "m4a": ("audio/mp4", "audio/x-m4a"),
    "ogg": ("audio/ogg",),
    "wav": ("audio/wav",),
}
ALLOWED_MIMES = tuple(sorted({m for v in CONTAINERS.values() for m in v}))
EXTENSIONS = tuple(CONTAINERS)

#: The relative path shape, asserted on the way back OUT of the database too.
#: Nothing else can ever become a filesystem path in this module.
REL_PATH_RE = re.compile(r"^\d{4}/\d{2}/[0-9a-f]{32}\.(?:%s)$"
                         % "|".join(EXTENSIONS))

#: Smallest plausible recording. A zero- or few-byte part is a recorder that
#: never started, and storing it would put an unplayable row on the board.
MIN_BYTES = 64

#: The PRIVACY CEILING, as a multiple of ``INBOX_AUDIO_RETENTION_DAYS``: audio
#: this old is deleted whatever its transcript status and whether or not it was
#: reviewed. See :func:`prune_audio`.
AUDIO_CEILING_MULTIPLE = 2

#: An interrupted ``save`` leaves ``<id>.<ext>.part`` behind (the write is
#: atomic via ``os.replace``, so the real name is never half-written — but the
#: temp file survives a crash). ``sweep_orphans`` deliberately refuses to delete
#: anything it does not recognise, and a ``.part`` file does not match
#: :data:`REL_PATH_RE`, so without this they accumulate for ever. An hour is far
#: longer than any upload and short enough to matter.
PART_SUFFIX = ".part"
PART_MAX_AGE_S = 3600


class AudioRejected(ValueError):
    """Base class: the upload is not acceptable. ``status`` is the HTTP code."""
    status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AudioTooLarge(AudioRejected):
    status = 413


class AudioTypeRejected(AudioRejected):
    status = 415


@dataclass(frozen=True)
class StoredAudio:
    path: str            # relative to the audio dir; what goes in the DB
    bytes: int
    mime: str
    sha256: str
    ext: str

    def as_row(self, secs: float | None = None) -> dict:
        return {"path": self.path, "bytes": self.bytes, "mime": self.mime,
                "sha256": self.sha256, "secs": secs}


def normalise_mime(raw: str | None) -> str:
    """``audio/webm;codecs=opus`` → ``audio/webm``. Parameters are the browser's
    business; the allow-list is about the container."""
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip().lower()


def sniff(data: bytes) -> str | None:
    """The container the BYTES are, by magic number, or None.

    - WebM/Matroska: the EBML header ``1A 45 DF A3``
    - Ogg:           ``OggS``
    - WAV:           ``RIFF`` … ``WAVE``
    - MP4/M4A:       a ``ftyp`` box at offset 4
    """
    if len(data) < 12:
        return None
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[4:8] == b"ftyp":
        return "m4a"
    return None


def check(data: bytes, declared_mime: str | None, max_bytes: int) -> str:
    """Validate an upload and return its extension, or raise ``AudioRejected``.

    Size is checked FIRST so an oversized body is a 413 rather than being
    sniffed; the route also caps ``request.max_content_length`` per request, so
    this is the second of two independent size gates, not the only one.
    """
    size = len(data)
    if size > max_bytes:
        raise AudioTooLarge(f"audio is {size} bytes, over the {max_bytes} limit")
    if size < MIN_BYTES:
        raise AudioRejected("audio is empty — the recorder never captured "
                            "anything")
    mime = normalise_mime(declared_mime)
    if mime not in ALLOWED_MIMES:
        raise AudioTypeRejected(
            f"unsupported audio type {mime or '(none)'} "
            f"(allowed: {', '.join(ALLOWED_MIMES)})")
    actual = sniff(data)
    if actual is None:
        raise AudioTypeRejected("audio does not start with a recognised "
                                "container header")
    if mime not in CONTAINERS[actual]:
        # A declared type that disagrees with the bytes is either a broken
        # recorder or somebody trying to land a file under a type a browser
        # would later sniff differently. Neither is worth storing.
        raise AudioTypeRejected(
            f"declared {mime} but the bytes are a {actual} container")
    return actual


def relative_path(item_id: str, ext: str, created_at: str) -> str:
    """``<yyyy>/<mm>/<id>.<ext>`` — from the server-generated id ONLY.

    ``created_at`` is an ISO stamp this app wrote; only its first 7 characters
    are used, and they are re-validated by :data:`REL_PATH_RE` before the path
    is ever joined.
    """
    year, month = created_at[:4], created_at[5:7]
    path = f"{year}/{month}/{item_id}.{ext}"
    if not REL_PATH_RE.match(path):
        raise AudioRejected("refusing to build an audio path from "
                            "unrecognised input")
    return path


def _resolve(audio_dir: str, rel_path: str) -> str:
    """Absolute path, refusing anything that is not the exact shape above.

    Belt and braces: the regex means no traversal sequence can be present at
    all, and the containment check means even a symlinked audio dir cannot
    escape. ``rel_path`` comes from our own database, which is precisely why
    it is checked — a store nobody validates on the way out is a store one bad
    write turns into a file read.
    """
    if not isinstance(rel_path, str) or not REL_PATH_RE.match(rel_path):
        raise AudioRejected(f"not a valid audio path: {rel_path!r}")
    root = os.path.realpath(audio_dir)
    full = os.path.realpath(os.path.join(root, rel_path))
    if full != root and not full.startswith(root + os.sep):
        raise AudioRejected("audio path escapes the audio directory")
    return full


def save(audio_dir: str, *, item_id: str, data: bytes, declared_mime: str | None,
         max_bytes: int, created_at: str) -> StoredAudio:
    """Validate, then write. Raises ``AudioRejected`` before touching disk."""
    ext = check(data, declared_mime, max_bytes)
    rel = relative_path(item_id, ext, created_at)
    full = _resolve(audio_dir, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, full)          # never a half-written file under the real name
    return StoredAudio(path=rel, bytes=len(data), mime=normalise_mime(declared_mime),
                       sha256=hashlib.sha256(data).hexdigest(), ext=ext)


def open_path(audio_dir: str, rel_path: str) -> str | None:
    """The absolute path if the file is really there, else None (→ 410/404)."""
    try:
        full = _resolve(audio_dir, rel_path)
    except AudioRejected:
        return None
    return full if os.path.isfile(full) else None


def mime_for(rel_path: str) -> str:
    ext = rel_path.rsplit(".", 1)[-1]
    return CONTAINERS.get(ext, ("application/octet-stream",))[0]


def delete(audio_dir: str, rel_path: str) -> bool:
    try:
        full = _resolve(audio_dir, rel_path)
    except AudioRejected:
        return False
    try:
        os.remove(full)
        return True
    except FileNotFoundError:
        return True            # already gone: the outcome we wanted
    except OSError:
        return False


def sweep_orphans(audio_dir: str, known: set[str],
                  now: float | None = None) -> list[str]:
    """Files on disk no row points at any more — the other half of the
    reconciliation that choosing files over BLOBs costs.

    Returns the relative paths removed. Anything whose name is not the exact
    shape this module writes is LEFT ALONE: this walks a directory inside the
    data volume, and a sweeper that deletes what it does not recognise is a
    sweeper that will one day eat something else.

    The ONE named exception is ``<id>.<ext>.part``, which :func:`save` writes
    and then ``os.replace``s into place. An interrupted write leaves one behind,
    it can never match :data:`REL_PATH_RE`, and so it used to be immortal. It is
    collected only once it is older than :data:`PART_MAX_AGE_S`, so a sweep that
    happens to run during an upload cannot delete a file that is still being
    written.
    """
    removed: list[str] = []
    if not os.path.isdir(audio_dir):
        return removed
    cutoff = (now if now is not None else _time.time()) - PART_MAX_AGE_S
    for dirpath, _dirnames, filenames in os.walk(audio_dir):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, audio_dir)
            if os.sep != "/":                       # pragma: no cover - posix only
                rel = rel.replace(os.sep, "/")
            if not REL_PATH_RE.match(rel):
                if not _is_stale_part(rel, full, cutoff):
                    continue
            elif rel in known:
                continue
            try:
                os.remove(full)
                removed.append(rel)
            except OSError:
                continue
    return removed


def _is_stale_part(rel: str, full: str, cutoff: float) -> bool:
    """A leftover temp file from an interrupted write, old enough to be dead.

    Both halves matter: the name must be exactly what :func:`save` writes
    (``<the real relative path>.part``), so nothing else in the tree can be
    caught by this, and it must predate the cutoff, so an upload in flight is
    never swept out from under itself.
    """
    if not rel.endswith(PART_SUFFIX):
        return False
    if not REL_PATH_RE.match(rel[:-len(PART_SUFFIX)]):
        return False
    try:
        return os.path.getmtime(full) < cutoff
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Scheduler-side maintenance (never in a request path)
# --------------------------------------------------------------------------- #
#
# Lives HERE rather than in `inbox.py` for a concrete reason: `inbox.py` imports
# `web`, and `services` (which the scheduler drives) is imported BY `web` — so a
# scheduler that reached into `inbox.py` would close an import cycle. This module
# depends only on `inbox_db`, which depends on nothing of ours but `db`.

def prune_audio(settings, now: float | None = None) -> dict:
    """Delete audio whose transcript is safe, then reconcile files ↔ rows.

    Two rules, and they answer two different questions (see
    ``inbox_db.prunable_audio``):

    * **"we no longer need it"** — all three of: the transcript is
      Whisper-quality, Graham has reviewed it, and it is past
      ``INBOX_AUDIO_RETENTION_DAYS``. By then the words are in the database and
      therefore in the database backup, which is what makes deleting the only
      recording of them tolerable.
    * **"we may no longer keep it"** — the PRIVACY CEILING at twice the
      retention window, regardless of transcript status or review state. All
      three conditions above are things that can simply never happen (Graham
      never ticks Reviewed; Whisper failed; the Mac worker never ran), and
      without this backstop a retention setting that reads like a maximum
      behaves like a minimum and a recording of his voice is kept for ever.

    The reconciliation is the price of storing audio as files rather than BLOBs,
    and it runs in BOTH directions: a row pointing at a file that is gone has its
    ``audio_path`` cleared (otherwise the one control Graham taps to check a
    transcript 404s), and a file no row points at is removed. Deliberately not
    shaped like ``db.prune``, whose correlated DELETE was measured at 113 s; these
    are bounded, indexed statements over a few hundred rows.
    """
    from . import inbox_db

    now = _time.time() if now is None else now
    retention_s = settings.inbox_audio_retention_days * 86400
    cutoff = inbox_db.to_iso(now - retention_s)
    # The ceiling. Twice the convenience window: long enough that a note
    # waiting on a slow review is not snatched away, short enough that "for
    # ever" is off the table.
    hard_cutoff = inbox_db.to_iso(now - AUDIO_CEILING_MULTIPLE * retention_s)
    audio_dir = settings.inbox_audio_dir
    pruned = cleared = 0
    conn = inbox_db.connect(settings.inbox_db_path)
    try:
        for row in inbox_db.prunable_audio(conn, cutoff, hard_cutoff):
            delete(audio_dir, row["audio_path"])
            with conn:
                inbox_db.mark_audio_pruned(conn, row["id"])
            pruned += 1
        for row in inbox_db.dangling_audio_items(conn):
            if open_path(audio_dir, row["audio_path"]) is None:
                with conn:
                    inbox_db.clear_audio_path(conn, row["id"])
                cleared += 1
        known = inbox_db.known_audio_paths(conn)
    finally:
        conn.close()
    orphans = sweep_orphans(audio_dir, known, now)
    return {"pruned": pruned, "cleared": cleared, "orphans": len(orphans),
            "tree_bytes": tree_bytes(audio_dir)}


def tree_bytes(audio_dir: str) -> int:
    """Total bytes under the audio tree — a metric for the mirror's heartbeat."""
    total = 0
    for dirpath, _dirs, files in os.walk(audio_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total
