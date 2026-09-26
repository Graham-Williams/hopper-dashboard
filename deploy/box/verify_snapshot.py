#!/usr/bin/env python3
"""Verify a copied-out DB snapshot before backup.sh keeps it. Stdlib only, host-side.

    verify_snapshot.py <snapshot> <previous-snapshot-or-empty> <allow-empty 0|1>

Exit codes:
    0  keep it
    1  corrupt or structurally wrong — do not keep it
    3  EVERY user table is empty while the PREVIOUS snapshot had data

3 is its own code because it is the realistic silent disaster, not a theoretical one. The
container's start-up runs the schema migration on every boot, so if /app/data is ever
remounted empty the app immediately recreates a valid, integrity-ok, fully-tabled, COMPLETELY
EMPTY database. Snapshotting that succeeds, reports success, and then rotates every real copy
out of the local ring and both Drive tiers. Row counts (not bytes) are what is compared, so a
legitimate VACUUM cannot false-positive.

With NO previous snapshot an empty DB is legitimate — a first run against a fresh deployment —
and refusing would break bootstrapping. This only ever refuses a visible REGRESSION.
"""
import pathlib
import sqlite3
import sys

EMPTY_RC = 3


def warn(msg):
    sys.stderr.write("snapshot verification: %s\n" % msg)


def has_any_row(conn):
    """True if ANY user table holds at least one row.

    Table names come from the DB's own sqlite_master and cannot be parameterized (SQLite
    cannot bind an identifier), so each is identifier-quoted with doubled quotes. sqlite_*
    internal tables are bookkeeping, not content. Stops at the first non-empty table.
    """
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")]
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        if conn.execute("SELECT EXISTS(SELECT 1 FROM %s)" % quoted).fetchone()[0]:
            return True
    return False


def main(argv):
    path = argv[1]
    prev_path = argv[2] if len(argv) > 2 else ""
    allow_empty = len(argv) > 3 and argv[3] == "1"

    conn = sqlite3.connect(path)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            warn("integrity_check failed on the copied-out snapshot")
            return 1
        # An empty-but-valid DB passes integrity_check, so assert it has a schema at all.
        if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0:
            warn("snapshot contains no tables")
            return 1
        snapshot_has_rows = has_any_row(conn)
    finally:
        conn.close()

    if snapshot_has_rows:
        return 0
    if allow_empty:
        warn("no rows in any table; ALLOW_EMPTY_SNAPSHOT=1 — accepting it")
        return 0
    if not prev_path:
        warn("no rows in any table, and no previous snapshot to compare against — "
             "accepting it (first run / bootstrap)")
        return 0
    try:
        # immutable=1 reads the previous snapshot's bytes directly and creates nothing.
        # Opening it normally would create <snapshot>-wal/-shm beside it (snapshots inherit
        # the live DB's WAL journal mode), and those sidecars are NOT reaped by the prune
        # glob. pathlib → URI so a "?" or "#" in the path is percent-encoded, never parsed
        # as a URI parameter.
        uri = pathlib.Path(prev_path).resolve(strict=True).as_uri() + "?immutable=1"
        prev = sqlite3.connect(uri, uri=True)
        try:
            prev_has_rows = has_any_row(prev)
        finally:
            prev.close()
    except Exception as exc:  # unreadable/corrupt previous snapshot — nothing to compare
        warn("no rows in any table and the previous snapshot could not be read (%s) — "
             "accepting it" % exc)
        return 0

    if prev_has_rows:
        return EMPTY_RC
    warn("no rows in any table, and neither had the previous one — accepting it")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
