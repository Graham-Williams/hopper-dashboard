"""The backlog mirror key is computed TWICE, in two languages of the same codebase, and the
two must agree byte for byte.

``probes/backlog.py`` runs on the Mac under a stock /usr/bin/python3 with nothing but the
stdlib; ``dashboard/inbox_db.py`` runs in the container with Flask. Neither can import the
other, so the derivation is duplicated — and a duplicated derivation drifts unless something
watches it. This is that something.

Drift is not a cosmetic bug: the key IS the row's identity. If the Mac starts hashing
differently, the very next sync archives every mirrored backlog row (their keys vanish from
the payload) and re-creates them under new keys, losing every `reviewed` tick and every
linked GitHub issue on them.
"""
import os

import pytest

from dashboard import inbox_db
from probes import backlog

# Whitespace, case, punctuation, emoji status markers, control characters, and the exact
# shapes the real file contains. Each is a way the two implementations could diverge.
CORPUS = [
    "Do the thing",
    "  leading and trailing space  ",
    "MiXeD CaSe WiTh  Double  Spaces",
    "⭐ PRIORITY (Graham, 2026-09-12) — HTTPS everywhere at the ORIGIN",
    "✅ DONE 2026-08-21 — Passwordless/unattended Tailscale SSH to the box",
    "💤 PARKED (was ⭐ PRIORITY) — Automate the SMS-bridge re-pairing",
    "🔁 Seasonal deep security sweep — quarterly",
    "⚠️ Replace rclone's SHARED Google Drive OAuth client_id",
    "tabs\tand\nnewlines\tcollapsed",
    "control \x00 chars \x1b and \x7f delete",
    "unicode: naïve café — em-dash, ’curly’, “quoted”",
    "a" * 30000,                       # past MAX_TEXT, so truncation has to match too
    "x",
]


@pytest.mark.parametrize("what", CORPUS, ids=range(len(CORPUS)))
def test_the_two_key_derivations_agree(what):
    assert backlog.normalise_backlog_key(what) == inbox_db.normalise_backlog_key(what)


@pytest.mark.parametrize("value", CORPUS + [None, 42])
def test_the_two_text_normalisations_agree(value):
    """``clean_text`` is the half of the derivation that is easiest to get subtly wrong —
    which control characters survive, whether the strip happens before or after the cap."""
    assert backlog.clean_text(value) == inbox_db.clean_text(value)


def test_the_shared_constants_have_not_drifted():
    assert backlog.MAX_TEXT == inbox_db.MAX_TEXT
    assert backlog.MIRROR_BACKLOG == inbox_db.MIRROR_BACKLOG


def test_the_probe_key_is_the_one_the_endpoint_would_accept():
    """The endpoint takes the client's key at face value as long as it carries the right
    prefix, and only falls back to deriving one from the whole body text (which would be a
    DIFFERENT key). So the prefix check is part of the contract, not a formality."""
    key = backlog.normalise_backlog_key("Do the thing")
    assert key.startswith(inbox_db.MIRROR_BACKLOG + ":")
    assert key != inbox_db.normalise_backlog_key("Do the thing\nWhy: because")


def test_the_real_backlog_file_round_trips_through_both(tmp_path):
    """The corpus above is synthetic; this is the actual file. Skipped in CI."""
    path = backlog.DEFAULT_BACKLOG_FILE
    if not os.path.isfile(path):
        pytest.skip("backlog.txt is not on this machine")
    entries = backlog.read_backlog(path)
    assert entries
    for entry in entries:
        assert entry.key == inbox_db.normalise_backlog_key(entry.what)


def test_a_parsed_entry_survives_the_endpoints_own_validation():
    """Whatever the parser emits has to pass the endpoint's cleaning unchanged, or the row
    the board shows is not the row the Mac sent."""
    sample = ("---\nWhat: ⭐ Do the thing\nWhy: because it matters\n"
              "  and it wrapped\nNotes: fine\n")
    entry, = backlog.parse_backlog(sample)
    assert inbox_db.clean_text(entry.text, inbox_db.MAX_TEXT) == entry.text
    assert inbox_db.derive_title(entry.text) == "⭐ Do the thing"
