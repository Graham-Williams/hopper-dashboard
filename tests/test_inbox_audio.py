"""The audio file store: the allow-list, the magic-byte check, and the fact
that a path can only ever be built from a server-generated id."""

from __future__ import annotations

import os

import pytest

from dashboard import inbox_audio as audio

ID = "a" * 32
CREATED = "2026-09-19T12:00:00Z"

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 200
OGG = b"OggS" + b"\x00" * 200
WAV = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 200
M4A = b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 200


@pytest.fixture
def audio_dir(tmp_path):
    return str(tmp_path / "inbox" / "audio")


# --------------------------------------------------------------------------- #
# Type + size
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("data,mime,ext", [
    (WEBM, "audio/webm;codecs=opus", "webm"),   # Chrome / Android
    (M4A, "audio/mp4", "m4a"),                  # iOS Safari
    (M4A, "audio/x-m4a", "m4a"),                # ...and the alias some builds use
    (OGG, "audio/ogg", "ogg"),
    (WAV, "audio/wav", "wav"),
])
def test_both_browser_formats_are_accepted(data, mime, ext):
    """iOS Safari emits mp4/AAC and Chrome/Android webm/opus. BOTH must work or
    the feature is dead on one of the two devices Graham uses."""
    assert audio.check(data, mime, 1_000_000) == ext


def test_an_unlisted_type_is_415():
    with pytest.raises(audio.AudioTypeRejected) as exc:
        audio.check(b"%PDF-1.4" + b"\x00" * 200, "application/pdf", 1_000_000)
    assert exc.value.status == 415


def test_a_declared_type_that_lies_about_the_bytes_is_415():
    """The allow-list alone would let anything through under a name the browser
    later sniffs differently; magic bytes alone would accept an unknown type."""
    with pytest.raises(audio.AudioTypeRejected, match="but the bytes are"):
        audio.check(WEBM, "audio/wav", 1_000_000)
    with pytest.raises(audio.AudioTypeRejected, match="recognised container"):
        audio.check(b"GIF89a" + b"\x00" * 200, "audio/webm", 1_000_000)


def test_oversize_is_413_and_is_checked_before_sniffing():
    with pytest.raises(audio.AudioTooLarge) as exc:
        audio.check(WEBM * 100, "audio/webm", 100)
    assert exc.value.status == 413
    # Even rubbish bytes get the SIZE verdict, not the type one.
    with pytest.raises(audio.AudioTooLarge):
        audio.check(b"x" * 500, "text/plain", 100)


def test_an_empty_recording_is_rejected():
    with pytest.raises(audio.AudioRejected, match="never captured"):
        audio.check(b"", "audio/webm", 1_000_000)
    with pytest.raises(audio.AudioRejected):
        audio.check(WEBM[:20], "audio/webm", 1_000_000)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def test_the_path_is_built_from_the_server_id_only(audio_dir):
    stored = audio.save(audio_dir, item_id=ID, data=WEBM,
                        declared_mime="audio/webm", max_bytes=1_000_000,
                        created_at=CREATED)
    assert stored.path == f"2026/09/{ID}.webm"
    assert stored.bytes == len(WEBM) and stored.mime == "audio/webm"
    assert len(stored.sha256) == 64
    assert os.path.isfile(os.path.join(audio_dir, stored.path))
    assert not os.path.exists(os.path.join(audio_dir, stored.path + ".part"))


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd", "2026/09/../../etc/passwd",
    "/etc/passwd", "2026/09/x.webm", "2026/09/" + ID + ".sh",
    "2026/9/" + ID + ".webm", ID + ".webm", "", None, 7,
])
def test_traversal_is_not_expressible(audio_dir, bad):
    """A client filename is never read, so the only paths that exist are ones
    this module wrote — and even those are re-validated on the way back out."""
    assert audio.open_path(audio_dir, bad) is None
    assert audio.delete(audio_dir, bad) is False


def test_relative_path_refuses_a_non_id():
    with pytest.raises(audio.AudioRejected):
        audio.relative_path("../evil", "webm", CREATED)
    with pytest.raises(audio.AudioRejected):
        audio.relative_path(ID, "sh", CREATED)


def test_open_path_and_delete(audio_dir):
    stored = audio.save(audio_dir, item_id=ID, data=OGG,
                        declared_mime="audio/ogg", max_bytes=1_000_000,
                        created_at=CREATED)
    assert audio.open_path(audio_dir, stored.path)
    assert audio.mime_for(stored.path) == "audio/ogg"
    assert audio.delete(audio_dir, stored.path) is True
    assert audio.open_path(audio_dir, stored.path) is None
    # Deleting something already gone is the outcome we wanted, not an error.
    assert audio.delete(audio_dir, stored.path) is True


def test_sweep_orphans_removes_only_what_it_recognises(audio_dir):
    keep = audio.save(audio_dir, item_id="b" * 32, data=WEBM,
                      declared_mime="audio/webm", max_bytes=1_000_000,
                      created_at=CREATED)
    orphan = audio.save(audio_dir, item_id="c" * 32, data=WEBM,
                        declared_mime="audio/webm", max_bytes=1_000_000,
                        created_at=CREATED)
    stranger = os.path.join(audio_dir, "2026", "09", "NOTES.txt")
    with open(stranger, "w", encoding="utf-8") as fh:
        fh.write("something a human put here")
    removed = audio.sweep_orphans(audio_dir, known={keep.path})
    assert removed == [orphan.path]
    assert audio.open_path(audio_dir, keep.path)
    assert os.path.exists(stranger)          # a sweeper that eats the unfamiliar
    assert audio.sweep_orphans(str(audio_dir) + "-missing", known=set()) == []


# --------------------------------------------------------------------------- #
# The sweep's one named exception
# --------------------------------------------------------------------------- #

def test_the_sweep_collects_stale_part_files_and_nothing_else(tmp_path):
    """`save` writes `<id>.<ext>.part` and then `os.replace`s it into place, so
    the real name is never half-written — but an interrupted write leaves the
    temp file behind. It can never match REL_PATH_RE, and the sweep deliberately
    refuses to delete what it does not recognise, so before this they were
    immortal.

    The age clause is the load-bearing half: a sweep that happened to run during
    an upload would otherwise delete a file that is still being written."""
    import os
    import time

    from dashboard import inbox_audio
    root = tmp_path / "audio" / "2026" / "09"
    root.mkdir(parents=True)
    ident = "a" * 32
    stale = root / f"{ident}.webm.part"
    fresh = root / f"{'b' * 32}.webm.part"
    keeper = root / f"{'c' * 32}.webm"
    stranger = root / "notes.txt"            # nothing this module ever wrote
    odd = root / "not-an-id.part"            # `.part` but not our shape
    for path in (stale, fresh, keeper, stranger, odd):
        path.write_bytes(b"x")
    old = time.time() - 2 * inbox_audio.PART_MAX_AGE_S
    os.utime(stale, (old, old))
    os.utime(odd, (old, old))

    removed = inbox_audio.sweep_orphans(str(tmp_path / "audio"),
                                        known={"2026/09/" + "c" * 32 + ".webm"})
    assert removed == ["2026/09/" + ident + ".webm.part"]
    assert not stale.exists()
    # Everything else is left exactly alone: a sweeper that deletes what it does
    # not recognise is a sweeper that will one day eat something else.
    for path in (fresh, keeper, stranger, odd):
        assert path.exists(), path
