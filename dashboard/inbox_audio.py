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


def sweep_orphans(audio_dir: str, known: set[str]) -> list[str]:
    """Files on disk no row points at any more — the other half of the
    reconciliation that choosing files over BLOBs costs.

    Returns the relative paths removed. Anything whose name is not the exact
    shape this module writes is LEFT ALONE: this walks a directory inside the
    data volume, and a sweeper that deletes what it does not recognise is a
    sweeper that will one day eat something else.
    """
    removed: list[str] = []
    if not os.path.isdir(audio_dir):
        return removed
    for dirpath, _dirnames, filenames in os.walk(audio_dir):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, audio_dir)
            if os.sep != "/":                       # pragma: no cover - posix only
                rel = rel.replace(os.sep, "/")
            if not REL_PATH_RE.match(rel) or rel in known:
                continue
            try:
                os.remove(full)
                removed.append(rel)
            except OSError:
                continue
    return removed
