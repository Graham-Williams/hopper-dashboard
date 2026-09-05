"""pa-backup log-line parsing and state-json dedup."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import backup_log  # noqa: E402
from probes.common import build_ping  # noqa: E402

SUCCESS = "2026-09-04 03:00:47 backup complete"
FAILURE = "2026-09-03 03:00:12 ERROR: backup FAILED (exit 1) -- see /Users/g/Library/Logs/hopper-backup.err.log"
NO_RCLONE = "2026-08-19 03:00:01 ERROR: rclone not found (checked /opt/homebrew/bin, /usr/local/bin, PATH); skipping."


def test_parse_success():
    e = backup_log.parse_line(SUCCESS)
    assert e.status == "ok" and e.timestamp == "2026-09-04 03:00:47" and e.message == "backup complete"
    assert e.finished_at_iso.startswith("2026-09-04T03:00:47")
    assert e.reason == "pushed"


def test_parse_failure_variants():
    for line in (FAILURE, NO_RCLONE):
        e = backup_log.parse_line(line)
        assert e.status == "fail", line
        assert e.message.startswith("ERROR:")
        assert e.reason == "error"


def test_parse_garbage_line_is_unknown():
    e = backup_log.parse_line("not a log line at all")
    assert e.status == "unknown" and e.timestamp is None and e.raw == "not a log line at all"
    assert e.finished_at_iso is None
    fields = backup_log.entry_to_ping_fields(e)
    assert fields["status"] == "fail" and fields["reason"] == "unparseable-log" and "finished_at" not in fields


def test_parse_timestamp_but_unrecognised_message():
    e = backup_log.parse_line("2026-09-04 03:00:47 something new happened")
    assert e.status == "unknown" and e.timestamp == "2026-09-04 03:00:47"


def test_parse_blank_returns_none():
    assert backup_log.parse_line("") is None
    assert backup_log.parse_line("   \n") is None


def test_last_nonempty_line_and_empty_file(tmp_path):
    p = tmp_path / "hopper-backup.log"
    assert backup_log.read_last_entry(str(p)) is None  # missing
    p.write_text("")
    assert backup_log.read_last_entry(str(p)) is None  # empty
    p.write_text("\n\n   \n")
    assert backup_log.read_last_entry(str(p)) is None  # whitespace only
    p.write_text(FAILURE + "\n" + SUCCESS + "\n\n")
    e = backup_log.read_last_entry(str(p))
    assert e.status == "ok" and e.raw == SUCCESS


def test_last_line_of_large_file_reads_tail_only(tmp_path):
    p = tmp_path / "big.log"
    with open(p, "w") as fh:
        for i in range(20000):
            fh.write("2026-01-01 00:00:00 backup complete #%d\n" % i)
        fh.write(FAILURE + "\n")
    e = backup_log.read_last_entry(str(p))
    assert e.status == "fail"


def test_entry_to_ping_fields_builds_valid_ping():
    e = backup_log.parse_line(SUCCESS)
    body = build_ping(**backup_log.entry_to_ping_fields(e))
    assert body["status"] == "ok" and body["reason"] == "pushed" and body["note"] == "backup complete"
    assert body["finished_at"].startswith("2026-09-04T03:00:47")


# --- dedup via state ------------------------------------------------------------
def test_state_dedup_same_line_not_rereported():
    e = backup_log.parse_line(SUCCESS)
    state = {}
    assert backup_log.is_new(e, state)
    state = backup_log.mark_reported(e, state, "2026-09-04T04:00:00-04:00")
    assert state[backup_log.STATE_KEY]["last_reported_line"] == SUCCESS
    assert not backup_log.is_new(e, state)
    # same status, new timestamp → a new run → report again
    e2 = backup_log.parse_line("2026-09-05 03:00:44 backup complete")
    assert backup_log.is_new(e2, state)
    # garbage/corrupt state → treated as new (fail open, never silently suppress)
    assert backup_log.is_new(e, {backup_log.STATE_KEY: "junk"})
    assert backup_log.is_new(e, {"other": {}})


def test_mark_reported_does_not_mutate_input():
    e = backup_log.parse_line(SUCCESS)
    state = {"keep": 1}
    new = backup_log.mark_reported(e, state, "now")
    assert "keep" in new and backup_log.STATE_KEY not in state
