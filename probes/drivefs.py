"""drive-mirror: is Google Drive for Desktop's *mirror* of the Mac folders caught up?

Reads DriveFS's own bookkeeping — ``~/Library/Application Support/Google/DriveFS/<account-id>/
mirror_sqlite.db`` — after COPYING ``.db``, ``-wal`` and ``-shm`` to a temp dir. The WAL is live;
never open the originals in place. Schema verified 2026-09-04 (DriveFS on macOS):

  pending_uploads(local_stable_id, stable_id)
  queued_uploads(stable_id, local_stable_id, size, md5_checksum, mtime_ms)
  pending_deletes(local_stable_id, root_id)
  mirror_item(local_stable_id, stable_id, ..., local_filename, cloud_filename,
              local_md5_checksum, cloud_md5_checksum, local_size, cloud_size, ..., is_root)
  root_config(root_id, root_state, local_stable_id, item_id, is_my_drive)

Caught up == pending == 0 and mismatch == 0. ``root_config.item_id`` is NULL in practice, so the
root names come from joining ``mirror_item`` on ``local_stable_id``.
"""
from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from probes.common import ProbeError

DEFAULT_DRIVEFS_DIR = os.path.expanduser("~/Library/Application Support/Google/DriveFS")
REQUIRED_TABLES = ("pending_uploads", "queued_uploads", "pending_deletes", "mirror_item", "root_config")


@dataclass
class MirrorState:
    pending_uploads: int
    queued_uploads: int
    pending_deletes: int
    mismatch: int
    items: int
    roots: List[str] = field(default_factory=list)
    db_mtime_epoch: Optional[float] = None

    @property
    def pending(self) -> int:
        return self.pending_uploads + self.queued_uploads + self.pending_deletes

    @property
    def caught_up(self) -> bool:
        return self.pending == 0 and self.mismatch == 0

    def metrics(self, now: Optional[float] = None) -> Dict[str, object]:
        m: Dict[str, object] = {
            "pending": self.pending,
            "pending_uploads": self.pending_uploads,
            "queued_uploads": self.queued_uploads,
            "pending_deletes": self.pending_deletes,
            "mismatch": self.mismatch,
            "items": self.items,
            "roots": len(self.roots),
            "roots_list": ",".join(self.roots),
            "caught_up": self.caught_up,
        }
        if self.db_mtime_epoch is not None:
            m["db_age_s"] = int(max(0.0, (now if now is not None else time.time()) - self.db_mtime_epoch))
        return m


def find_mirror_db(drivefs_dir: str = DEFAULT_DRIVEFS_DIR) -> Optional[str]:
    """The account dir is the numeric Google account id; there may be more than one — pick the
    most recently modified mirror db."""
    cands = glob.glob(os.path.join(drivefs_dir, "[0-9]*", "mirror_sqlite.db"))
    if not cands:
        return None
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return cands[0]


def copy_db_set(db_path: str, dest_dir: str) -> str:
    """Copy db + -wal + -shm (those that exist) into dest_dir; return the copied db path."""
    base = os.path.basename(db_path)
    for suffix in ("", "-wal", "-shm"):
        src = db_path + suffix
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest_dir, base + suffix))
    return os.path.join(dest_dir, base)


def query_mirror_db(db_path: str) -> MirrorState:
    """Run the queries against an (already copied) db. Raises ProbeError on schema drift."""
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise ProbeError("DriveFS mirror db schema drift: missing table(s) %s" % ", ".join(missing))

        def count(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])

        pu = count("SELECT count(*) FROM pending_uploads")
        qu = count("SELECT count(*) FROM queued_uploads")
        pd = count("SELECT count(*) FROM pending_deletes")
        mismatch = count(
            "SELECT count(*) FROM mirror_item "
            "WHERE local_size != cloud_size OR local_md5_checksum != cloud_md5_checksum"
        )
        items = count("SELECT count(*) FROM mirror_item")
        roots: List[str] = []
        for root_id, item_id, name in conn.execute(
            "SELECT r.root_id, r.item_id, m.local_filename FROM root_config r "
            "LEFT JOIN mirror_item m ON m.local_stable_id = r.local_stable_id ORDER BY r.root_id"
        ):
            roots.append(name or item_id or ("root-%s" % root_id))
    except sqlite3.DatabaseError as e:
        raise ProbeError("DriveFS mirror db unreadable: %s" % e)
    finally:
        conn.close()
    return MirrorState(pu, qu, pd, mismatch, items, roots)


def probe_drive_mirror(drivefs_dir: str = DEFAULT_DRIVEFS_DIR) -> MirrorState:
    """Locate → copy to a temp dir → query → clean up. Raises ProbeError when Drive isn't
    installed / no mirror db exists (the caller turns that into a 'fail' ping)."""
    db = find_mirror_db(drivefs_dir)
    if not db:
        raise ProbeError(
            "no DriveFS mirror db found under %s (Google Drive for Desktop not installed, not signed in, "
            "or mirroring disabled)" % drivefs_dir
        )
    mtime = os.path.getmtime(db)
    tmp = tempfile.mkdtemp(prefix="hopper-drivefs-")
    try:
        copied = copy_db_set(db, tmp)
        state = query_mirror_db(copied)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    state.db_mtime_epoch = mtime
    return state
