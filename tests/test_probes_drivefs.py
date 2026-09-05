"""drive-mirror: queries against a fixture sqlite that mirrors the real DriveFS mirror_sqlite.db
schema (verified 2026-09-04). No DriveFS install needed."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import drivefs  # noqa: E402
from probes.common import ProbeError  # noqa: E402

SCHEMA = """
CREATE TABLE mirror_item (local_stable_id INTEGER PRIMARY KEY, stable_id INTEGER NOT NULL, inode INTEGER NOT NULL,
  volume TEXT NOT NULL, parent_local_stable_id INTEGER, local_filename TEXT, cloud_filename TEXT,
  local_mtime_ms INTEGER, cloud_mtime_ms INTEGER, local_md5_checksum TEXT, cloud_md5_checksum TEXT,
  local_size INTEGER, cloud_size INTEGER, local_type INTEGER NOT NULL, cloud_type INTEGER NOT NULL,
  local_version INTEGER NOT NULL, cloud_version INTEGER NOT NULL, storage_policy INTEGER NOT NULL,
  shared BOOLEAN, read_only BOOLEAN, target_version INTEGER, is_root BOOLEAN,
  UNIQUE (stable_id, parent_local_stable_id), UNIQUE (parent_local_stable_id, local_filename));
CREATE TABLE pending_uploads (local_stable_id INTEGER PRIMARY KEY, stable_id INTEGER UNIQUE NOT NULL);
CREATE TABLE queued_uploads (stable_id INTEGER PRIMARY KEY, local_stable_id INTEGER NOT NULL, size INTEGER NOT NULL,
  md5_checksum TEXT NOT NULL, mtime_ms INTEGER NOT NULL);
CREATE TABLE pending_deletes (local_stable_id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL);
CREATE TABLE root_config(root_id INTEGER PRIMARY KEY, root_state INTEGER NOT NULL, local_stable_id INTEGER,
  item_id TEXT, is_my_drive BOOLEAN);
CREATE TABLE cloud_relations(a INTEGER); CREATE TABLE shortcut_relations(a INTEGER); CREATE TABLE machine_root(a INTEGER);
"""


def _item(conn, lid, parent, name, lsize, csize, lmd5, cmd5, is_root=0):
    conn.execute(
        "INSERT INTO mirror_item VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lid, lid + 100, lid, "VOL", parent, name, name, 0, 0, lmd5, cmd5, lsize, csize, 1, 1, 1, 1, 0, 0, 0, None, is_root),
    )


def make_db(path, pending=0, queued=0, deletes=0, mismatched=0, roots=("Documents", "Desktop", "minecraft-channel")):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    lid = 1
    for i, r in enumerate(roots):
        _item(conn, lid, None, r, None, None, None, None, is_root=1)
        conn.execute("INSERT INTO root_config VALUES (?,?,?,?,?)", (i + 3, 1, lid, None, 0))
        lid += 1
    for i in range(10):  # in-sync files
        _item(conn, lid, 1, "ok-%d.txt" % i, 100 + i, 100 + i, "aa", "aa")
        lid += 1
    for i in range(mismatched):
        if i % 2:
            _item(conn, lid, 1, "size-%d.txt" % i, 10, 20, "aa", "aa")
        else:
            _item(conn, lid, 1, "md5-%d.txt" % i, 10, 10, "aa", "bb")
        lid += 1
    for i in range(pending):
        conn.execute("INSERT INTO pending_uploads VALUES (?,?)", (900 + i, 9900 + i))
    for i in range(queued):
        conn.execute("INSERT INTO queued_uploads VALUES (?,?,?,?,?)", (800 + i, 8800 + i, 5, "x", 0))
    for i in range(deletes):
        conn.execute("INSERT INTO pending_deletes VALUES (?,?)", (700 + i, 3))
    conn.commit()
    conn.close()


@pytest.fixture
def drivefs_dir(tmp_path):
    acct = tmp_path / "DriveFS" / "102791939742096881960"
    acct.mkdir(parents=True)
    (tmp_path / "DriveFS" / "Logs").mkdir()  # non-numeric siblings must be ignored
    return tmp_path / "DriveFS"


def test_caught_up(drivefs_dir):
    make_db(str(drivefs_dir / "102791939742096881960" / "mirror_sqlite.db"))
    st = drivefs.probe_drive_mirror(str(drivefs_dir))
    assert st.caught_up
    assert st.pending == 0 and st.mismatch == 0 and st.items == 13
    assert st.roots == ["Documents", "Desktop", "minecraft-channel"]
    m = st.metrics(now=st.db_mtime_epoch + 42)
    assert m["pending"] == 0 and m["mismatch"] == 0 and m["roots"] == 3
    assert m["roots_list"] == "Documents,Desktop,minecraft-channel" and m["caught_up"] is True
    assert m["db_age_s"] == 42


def test_pending_and_mismatch(drivefs_dir):
    make_db(str(drivefs_dir / "102791939742096881960" / "mirror_sqlite.db"), pending=2, queued=3, deletes=1, mismatched=4)
    st = drivefs.probe_drive_mirror(str(drivefs_dir))
    assert not st.caught_up
    assert (st.pending_uploads, st.queued_uploads, st.pending_deletes) == (2, 3, 1)
    assert st.pending == 6 and st.mismatch == 4
    assert st.metrics()["caught_up"] is False


def test_null_sizes_on_roots_do_not_count_as_mismatch(drivefs_dir):
    # roots/folders have NULL local_size/cloud_size; NULL != NULL is NULL (falsy) in SQL, so they
    # must not inflate `mismatch` — pin that behaviour.
    make_db(str(drivefs_dir / "102791939742096881960" / "mirror_sqlite.db"), roots=("Documents",))
    st = drivefs.probe_drive_mirror(str(drivefs_dir))
    assert st.mismatch == 0 and st.roots == ["Documents"]


def test_wal_and_shm_are_copied_not_opened_in_place(drivefs_dir, monkeypatch):
    db = drivefs_dir / "102791939742096881960" / "mirror_sqlite.db"
    make_db(str(db))
    (drivefs_dir / "102791939742096881960" / "mirror_sqlite.db-wal").write_bytes(b"")
    (drivefs_dir / "102791939742096881960" / "mirror_sqlite.db-shm").write_bytes(b"")
    opened = []
    real_connect = drivefs.sqlite3.connect

    def spy(path, *a, **k):
        opened.append(path)
        return real_connect(path, *a, **k)

    monkeypatch.setattr(drivefs.sqlite3, "connect", spy)
    st = drivefs.probe_drive_mirror(str(drivefs_dir))
    assert st.caught_up
    assert opened and all(not p.startswith(str(drivefs_dir)) for p in opened), opened
    assert all(p.endswith("mirror_sqlite.db") for p in opened)


def test_missing_db_raises_clear_error(tmp_path):
    with pytest.raises(ProbeError) as ei:
        drivefs.probe_drive_mirror(str(tmp_path / "DriveFS-not-there"))
    assert "no DriveFS mirror db" in str(ei.value)
    assert drivefs.find_mirror_db(str(tmp_path)) is None


def test_picks_newest_account_when_several(drivefs_dir):
    a = drivefs_dir / "102791939742096881960" / "mirror_sqlite.db"
    b_dir = drivefs_dir / "555"
    b_dir.mkdir()
    b = b_dir / "mirror_sqlite.db"
    make_db(str(a))
    make_db(str(b), pending=1)
    os.utime(str(a), (1_000_000, 1_000_000))
    os.utime(str(b), (2_000_000, 2_000_000))
    assert drivefs.find_mirror_db(str(drivefs_dir)) == str(b)


def test_schema_drift_raises(drivefs_dir):
    db = drivefs_dir / "102791939742096881960" / "mirror_sqlite.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE mirror_item (x)")
    conn.commit()
    conn.close()
    with pytest.raises(ProbeError) as ei:
        drivefs.probe_drive_mirror(str(drivefs_dir))
    assert "schema drift" in str(ei.value) and "pending_uploads" in str(ei.value)


def test_not_a_database_raises(drivefs_dir):
    (drivefs_dir / "102791939742096881960" / "mirror_sqlite.db").write_bytes(b"this is not sqlite" * 10)
    with pytest.raises(ProbeError):
        drivefs.probe_drive_mirror(str(drivefs_dir))
