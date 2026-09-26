"""backlog.txt parsing + the inbox-backlog sub-probe, under /usr/bin/python3 (3.9), stdlib only.

Two things here are load-bearing beyond "does it parse":

1. **Key stability.** The key is a hash of the ``What:`` text because the file has no ids. If
   the hash moves for a cosmetic reason — a re-wrap, a case change, trailing whitespace — every
   row is archived and re-created, losing its reviewed flag and its linked issues.
2. **A delivery failure is this job's, not mac-probe's.** ``mac-probe`` means "the Mac is
   awake and probing", and the dashboard mutes every other Mac job's LATE alert while it is
   LATE. A 502 from the public host must never be able to reach into that.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import backlog, mac_probe  # noqa: E402
from probes.common import Logger, ProbeError  # noqa: E402

SAMPLE = """Backlog — things to do someday, no urgency

Format: one task per entry, separated by ---.

---

What: ⭐ PRIORITY — HTTPS everywhere at the ORIGIN
Why: The apex QA pass found the zone setting had been off the whole time, so plain http
  reached the origins.
Issues: km-tracker #85 · todoist-points #21

---

What: Review Gmail trash for permanent deletion
Notes: Only delete clearly promotional content.

---

What: ✅ DONE 2026-09-12 — Landing page at the apex
Why: Kept in place rather than deleted, by convention.
"""


def _log(tmp_path):
    return Logger(str(tmp_path / "probe.log"), echo=False)


# --- parsing ----------------------------------------------------------------------
def test_entries_are_split_on_the_separator_and_keyed_by_their_what_line():
    entries = backlog.parse_backlog(SAMPLE)
    assert [e.what for e in entries] == [
        "⭐ PRIORITY — HTTPS everywhere at the ORIGIN",
        "Review Gmail trash for permanent deletion",
        "✅ DONE 2026-09-12 — Landing page at the apex"]
    assert all(e.key.startswith("backlog:") and len(e.key) == 8 + 16 for e in entries)
    assert len({e.key for e in entries}) == 3


def test_the_header_preamble_is_not_an_entry():
    """The file opens with prose above the first separator and no `What:` at all."""
    assert backlog.parse_backlog("just some prose\n\nand more\n") == []
    assert len(backlog.parse_backlog(SAMPLE)) == 3


def test_the_body_leads_with_the_what_line_and_keeps_the_rest_for_context():
    first = backlog.parse_backlog(SAMPLE)[0]
    # The server derives the row TITLE from the first line, so the What value must be it.
    assert first.text.splitlines()[0] == first.what
    assert "Why: The apex QA pass" in first.text
    assert "Issues: km-tracker #85" in first.text
    assert "What:" not in first.text            # the label itself is not content


def test_the_key_survives_a_rewrap_and_a_case_change_but_not_an_edit():
    """Exactly the three cases the normalisation exists for."""
    base = "What: Replace rclone's shared OAuth client_id with our own\n"
    rewrapped = ("What: Replace rclone's shared OAuth\n"
                 "  client_id with our own\n")
    recased = "What: REPLACE RCLONE'S SHARED OAuth client_id with OUR OWN\n"
    respaced = "What:  Replace rclone's   shared OAuth client_id with our own   \n"
    edited = "What: Replace rclone's shared OAuth client_id with our own (urgent)\n"
    key = backlog.parse_backlog(base)[0].key
    for variant in (rewrapped, recased, respaced):
        assert backlog.parse_backlog(variant)[0].key == key, variant
    assert backlog.parse_backlog(edited)[0].key != key


def test_a_colon_mid_sentence_does_not_start_a_new_field():
    text = ("---\nWhat: Do the thing\n"
            "Why: he said this: and then that, which is one field\n")
    entry, = backlog.parse_backlog(text)
    assert entry.what == "Do the thing"
    assert "he said this: and then that" in entry.text


def test_a_separator_must_be_exactly_three_dashes():
    """A wrapped line starting with dashes is content, not a boundary."""
    text = "---\nWhat: One\nNotes: ok\n----\nstill the same entry\n"
    assert [e.what for e in backlog.parse_backlog(text)] == ["One"]


def test_duplicate_entries_are_sent_once():
    text = "---\nWhat: Same thing\n---\nWhat: same   THING\n"
    assert len(backlog.parse_backlog(text)) == 1


def test_control_characters_are_stripped_the_way_the_server_strips_them():
    entry, = backlog.parse_backlog("---\nWhat: clean\x00ish\x1b[31m text\n")
    assert "\x00" not in entry.text and "\x1b" not in entry.text
    assert entry.key == backlog.normalise_backlog_key("cleanish[31m text")


def test_the_real_backlog_file_parses_if_it_is_there():
    """Not a fixture — the actual file this mirrors. Skipped in CI, where it does not exist."""
    path = backlog.DEFAULT_BACKLOG_FILE
    if not os.path.isfile(path):
        pytest.skip("backlog.txt is not on this machine")
    entries = backlog.read_backlog(path)
    assert len(entries) >= 10
    assert len({e.key for e in entries}) == len(entries)
    assert all(e.text.splitlines()[0] == e.what for e in entries)


# --- the empty-file guard ---------------------------------------------------------
def test_an_empty_or_missing_file_raises_rather_than_syncing_nothing(tmp_path):
    """"The file was unreadable" and "Graham emptied the backlog" arrive identical, and one
    of them must not archive every mirrored row."""
    missing = tmp_path / "nope.txt"
    with pytest.raises(ProbeError, match="not found"):
        backlog.read_backlog(str(missing))
    empty = tmp_path / "backlog.txt"
    empty.write_text("")
    with pytest.raises(ProbeError, match="refusing to sync"):
        backlog.read_backlog(str(empty))
    empty.write_text("just a header, no entries\n")
    with pytest.raises(ProbeError, match="refusing to sync"):
        backlog.read_backlog(str(empty))


def test_the_payload_asserts_completeness_because_the_parse_refused_to_be_empty():
    payload = backlog.build_payload(backlog.parse_backlog(SAMPLE))
    assert payload["complete"] is True
    assert len(payload["items"]) == 3
    assert set(payload["items"][0]) == {"key", "text"}      # no project → key omitted


# --- the sub-probe ----------------------------------------------------------------
def _cfg(tmp_path, **extra):
    path = tmp_path / "backlog.txt"
    path.write_text(SAMPLE)
    cfg = {"INBOX_URL": "https://dash.example.com/", "INBOX_TOKEN": "tok",
           "INBOX_BACKLOG_FILE": str(path), "PROBE_HTTP_TIMEOUT": "10"}
    cfg.update(extra)
    return cfg


def test_the_sub_probe_posts_the_complete_payload_and_reports_ok(tmp_path, monkeypatch):
    calls = []

    def fake(url, token, method="GET", body=None, **kw):
        calls.append((url, token, method, body))
        return {"synced": 3, "archived": 1, "complete": True}

    monkeypatch.setattr(mac_probe, "api_json", fake)
    (job, ping), = mac_probe.probe_inbox_backlog(_cfg(tmp_path), _log(tmp_path))
    assert job == "inbox-backlog"
    url, token, method, body = calls[0]
    assert url == "https://dash.example.com/api/v1/inbox/mirror/backlog"
    assert (token, method) == ("tok", "POST") and body["complete"] is True
    assert len(body["items"]) == 3
    assert ping["status"] == "ok"
    assert ping["metrics"] == {"entries": 3, "synced": 3, "archived": 1}


def test_a_delivery_failure_is_this_jobs_failure_not_mac_probes(tmp_path, monkeypatch):
    """The whole reason this returns a ping instead of raising. If it raised, mac-probe would
    go FAIL for a Cloudflare 502 — and while mac-probe is FAIL/LATE the dashboard suppresses
    every other Mac job's alert, so a public-host blip would mute the backup alarms."""
    def boom(*a, **k):
        raise ProbeError("POST https://dash.example.com/... failed after 3 attempts: 502")

    monkeypatch.setattr(mac_probe, "api_json", boom)
    (job, ping), = mac_probe.probe_inbox_backlog(_cfg(tmp_path), _log(tmp_path))
    assert job == "inbox-backlog" and ping["status"] == "fail"
    assert "502" in ping["note"]


def test_a_delivery_failure_does_not_reach_the_mac_probe_heartbeat(tmp_path, monkeypatch):
    """End-to-end version of the above, through main(): mac-probe stays `ok` and only
    inbox-backlog is red."""
    def boom(*a, **k):
        raise ProbeError("502 from the public host")

    monkeypatch.setattr(mac_probe, "api_json", boom)
    for name in ("probe_pa_backup", "probe_minecraft_offload", "probe_mac_disk",
                 "probe_drive_mirror"):
        if name == "probe_pa_backup":
            monkeypatch.setattr(mac_probe, name, lambda cfg, state, log: ([], None))
        else:
            monkeypatch.setattr(mac_probe, name, lambda cfg, log: [])
    sent = []
    monkeypatch.setattr(mac_probe, "send_ping",
                        lambda u, t, j, b, timeout=10: sent.append((j, b)) or (200, "{}"))
    env = tmp_path / "env"
    path = tmp_path / "backlog.txt"
    path.write_text(SAMPLE)
    env.write_text("\n".join([
        "DASHBOARD_URL=http://box:8081", "INGEST_TOKEN=t",
        "INBOX_URL=https://dash.example.com", "INBOX_TOKEN=tok",
        "INBOX_BACKLOG_FILE=%s" % path,
        "PROBE_LOG_FILE=%s" % (tmp_path / "p.log"),
        "PROBE_STATE_FILE=%s" % (tmp_path / "s.json")]) + "\n")
    rc = mac_probe.main(["--env", str(env), "--quiet"])
    assert rc == 0                                   # the RUN succeeded; one job did not
    by_job = dict(sent)
    assert by_job["inbox-backlog"]["status"] == "fail"
    assert by_job["mac-probe"]["status"] == "ok"
    assert by_job["mac-probe"]["metrics"]["subprobes_failed"] == 0


def test_an_unreadable_backlog_file_is_also_reported_against_this_job(tmp_path, monkeypatch):
    monkeypatch.setattr(mac_probe, "api_json",
                        lambda *a, **k: pytest.fail("must not POST when the parse failed"))
    cfg = _cfg(tmp_path, INBOX_BACKLOG_FILE=str(tmp_path / "gone.txt"))
    (job, ping), = mac_probe.probe_inbox_backlog(cfg, _log(tmp_path))
    assert job == "inbox-backlog" and ping["status"] == "fail" and "not found" in ping["note"]


def test_an_unconfigured_inbox_posts_nothing_at_all(tmp_path, monkeypatch):
    """No INBOX_URL/INBOX_TOKEN = the Inbox is not deployed on this Mac. Posting a healthy
    heartbeat would be a lie, and posting to a job id this deployment's jobs.yml has never
    heard of is a 404 that WOULD count against mac-probe."""
    monkeypatch.setattr(mac_probe, "api_json",
                        lambda *a, **k: pytest.fail("must not POST when unconfigured"))
    for cfg in (_cfg(tmp_path, INBOX_URL=""), _cfg(tmp_path, INBOX_TOKEN="")):
        assert mac_probe.probe_inbox_backlog(cfg, _log(tmp_path)) == []


def test_dry_run_never_writes_to_the_real_inbox(tmp_path, monkeypatch):
    """Every other sub-probe only reads, so --dry-run is free for them. This one WRITES."""
    monkeypatch.setattr(mac_probe, "api_json",
                        lambda *a, **k: pytest.fail("--dry-run must not POST"))
    (job, ping), = mac_probe.probe_inbox_backlog(_cfg(tmp_path), _log(tmp_path), True)
    assert job == "inbox-backlog" and ping["status"] == "ok"
    assert ping["metrics"]["entries"] == 3


def test_it_is_registered_as_a_sub_probe():
    assert "inbox-backlog" in mac_probe.SUBPROBES
    # Appended, never inserted: --only takes these as choices and the order is the run order.
    assert mac_probe.SUBPROBES[:4] == ("pa-backup", "minecraft-offload", "mac-disk",
                                       "drive-mirror")
