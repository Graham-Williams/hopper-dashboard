"""The box backup: its unit templates, and the guard that stops it eating the good copies.

Three classes of failure are pinned here, all of them ones that fail SILENTLY in production:

1. **An unrendered placeholder.** `deploy/box/*.service` are templates. Hand-copied to
   /etc/systemd/system, systemd rejects `@@REPO@@/...` with "Neither a valid executable name
   nor an absolute path", the unit fails on every tick, and the thing it was monitoring goes
   dark. That exact mistake took out the box container heartbeat on 2026-09-12.
2. **A timer without its heartbeat.** An unmonitored backup fails quietly and the board keeps
   showing the job as it last was — the precise failure this repo exists to catch.
3. **An all-empty snapshot.** The container runs its schema migration on every boot, so a
   remounted-empty /app/data gives a valid, integrity-ok, fully-tabled, COMPLETELY empty DB.
   Snapshotting it succeeds and then rotates every real copy out of the ring and both Drive
   tiers.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOX = os.path.join(REPO, "deploy", "box")
VERIFY = os.path.join(BOX, "verify_snapshot.py")
SHELL_SCRIPTS = ["backup.sh", "install.sh", "uninstall.sh", "containers_probe.sh"]


def _read(*parts):
    with open(os.path.join(BOX, *parts), encoding="utf-8") as fh:
        return fh.read()


def service_templates():
    return sorted(n for n in os.listdir(BOX) if n.endswith(".service"))


# --- templates --------------------------------------------------------------------
def test_every_service_template_has_all_of_its_placeholders_rendered_by_install():
    """Not "install.sh mentions sed" — every distinct @@NAME@@ in every shipped unit must
    appear on the left of a substitution in install.sh, and install.sh must refuse to move a
    file that still contains one."""
    install = _read("install.sh")
    assert service_templates(), "no unit templates found — did the layout change?"
    for name in service_templates():
        body = _read(name)
        placeholders = set(re.findall(r"@@[A-Z_]+@@", body))
        assert placeholders, "%s has no placeholders; is it still a template?" % name
        for ph in placeholders:
            assert "s|%s|" % ph in install, "%s: install.sh never renders %s" % (name, ph)
        assert "TEMPLATE" in body, "%s must say so in its header" % name
    # The guard that turns a missed substitution into a loud failure instead of a broken unit.
    assert install.count("grep -q '@@'") >= 2


def test_timers_and_their_units_come_in_pairs():
    for name in service_templates():
        timer = name.replace(".service", ".timer")
        assert os.path.isfile(os.path.join(BOX, timer)), "%s has no timer" % name


def test_the_backup_timer_is_never_installed_without_its_heartbeat():
    """The invariant, asserted on the shipped files AND on install.sh's shape: the drop-in is
    installed in the same loop iteration as the unit, so neither can ship alone."""
    drop_in = os.path.join(BOX, "hopper-dashboard-backup.service.d", "heartbeat.conf")
    assert os.path.isfile(drop_in)
    conf = _read("hopper-dashboard-backup.service.d", "heartbeat.conf")
    assert "ExecStopPost=-/usr/bin/curl" in conf
    assert "/api/v1/ping/hopper-dashboard-backup" in conf
    # A missing env file or a failed curl must not change the unit's own result.
    assert "EnvironmentFile=-/etc/hopper-dashboard/ingest.env" in conf
    install = _read("install.sh")
    block = install.split('for u in "${OWN_UNITS[@]}"; do', 1)[1].split("done", 1)[0]
    assert "$u.service" in block and "$u.timer" in block
    assert "heartbeat.conf" in block


def test_the_uninstaller_removes_exactly_what_the_installer_added():
    uninstall = _read("uninstall.sh")
    for unit in ("dashboard-containers", "hopper-dashboard-backup"):
        assert unit in uninstall
    # The drop-in loop must name it too — removing the unit while leaving an orphaned
    # heartbeat.conf behind would leave a drop-in directory for a unit that no longer exists.
    drop_in_loop = re.search(r"for u in ([a-z0-9 -]+); do\n(?:.*\n)*?done", uninstall).group(1)
    assert "hopper-dashboard-backup" in drop_in_loop


def test_the_ping_id_in_the_drop_in_matches_the_shipped_job_registry():
    from dashboard.registry import load_registry
    from tests.conftest import EXAMPLE_JOBS
    ids = {j.id for j in load_registry(EXAMPLE_JOBS)}
    conf = _read("hopper-dashboard-backup.service.d", "heartbeat.conf")
    posted = re.search(r"/api/v1/ping/([a-z0-9-]+)", conf).group(1)
    assert posted in ids, "the drop-in posts to %r, which jobs.example.yml does not declare" % posted


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_the_shell_scripts_parse(name):
    path = os.path.join(BOX, name)
    assert subprocess.run(["bash", "-n", path]).returncode == 0


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shellcheck_is_clean_where_shellcheck_exists(name):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    r = subprocess.run(["shellcheck", "-S", "warning", os.path.join(BOX, name)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_backup_sh_snapshots_inside_the_container_and_never_syncs():
    """The two facts that make this script correct rather than merely present."""
    body = _read("backup.sh")
    assert "docker exec" in body and ".backup(d)" in body
    # rclone sync would let a local prune bug — or a wiped volume — delete the off-box copy
    # in ONE blunt command, with the DB ring sitting next to it. The audio tree does mirror
    # deletions (see the guard tests below), but via an explicit, logged, guarded delete pass.
    assert "rclone sync" not in body
    assert "rclone copy" in body
    # Both DBs and the audio tree.
    assert "/app/data/dashboard.db" in body and "/app/data/inbox.db" in body
    assert "/app/data/inbox/audio" in body
    # The daily long-tail tier, so a corruption noticed a day later still has a clean copy.
    assert "daily" in body and "DAILY_RETENTION" in body


# --- the empty-snapshot guard -----------------------------------------------------
def _db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE inbox_items (id TEXT)")
    conn.execute("CREATE TABLE inbox_issues (id TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO inbox_items VALUES (?)", (str(i),))
    conn.commit()
    conn.close()
    return str(path)


def _verify(snapshot, prev="", allow_empty="0"):
    return subprocess.run([sys.executable, VERIFY, snapshot, prev, allow_empty],
                          capture_output=True, text=True).returncode


def test_a_populated_snapshot_is_kept(tmp_path):
    assert _verify(_db(tmp_path / "s.db", 3)) == 0


def test_an_empty_snapshot_is_refused_when_the_previous_one_had_data(tmp_path):
    """rc 3 — its own code, because this is the realistic silent disaster: a remounted-empty
    /app/data makes the container recreate a valid, integrity-ok, empty DB on start."""
    prev = _db(tmp_path / "prev.db", 5)
    empty = _db(tmp_path / "new.db", 0)
    assert _verify(empty, prev) == 3


def test_an_empty_snapshot_is_accepted_when_bootstrapping(tmp_path):
    """No previous snapshot at all = a first run against a fresh deployment. Refusing would
    make the backup impossible to start."""
    assert _verify(_db(tmp_path / "new.db", 0), "") == 0


def test_an_empty_snapshot_is_accepted_when_the_previous_was_empty_too(tmp_path):
    prev = _db(tmp_path / "prev.db", 0)
    assert _verify(_db(tmp_path / "new.db", 0), prev) == 0


def test_the_override_exists_for_a_deliberate_wipe(tmp_path):
    prev = _db(tmp_path / "prev.db", 5)
    assert _verify(_db(tmp_path / "new.db", 0), prev, "1") == 0


def test_a_schemaless_or_corrupt_snapshot_is_rejected(tmp_path):
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    assert _verify(str(bare)) == 1
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database" * 40)
    assert _verify(str(junk)) != 0


def test_reading_the_previous_snapshot_creates_no_sidecars(tmp_path):
    """It is opened immutable on purpose: a normal open would create <snap>-wal/-shm beside
    it (snapshots inherit the live DB's WAL mode) and the prune glob never reaps those."""
    prev = tmp_path / "prev.db"
    conn = sqlite3.connect(str(prev))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id TEXT)")
    conn.execute("INSERT INTO t VALUES ('x')")
    conn.commit()
    conn.close()
    before = set(os.listdir(tmp_path))
    _verify(_db(tmp_path / "new.db", 0), str(prev))
    strays = {n for n in set(os.listdir(tmp_path)) - before if n.startswith("prev.db-")}
    assert strays == set()


def test_an_unreadable_previous_snapshot_does_not_block_the_backup(tmp_path):
    """Nothing to compare against is not evidence of a regression — and a backup that refuses
    to run because an OLD file rotted is a backup that has stopped."""
    prev = tmp_path / "prev.db"
    prev.write_bytes(b"corrupt")
    assert _verify(_db(tmp_path / "new.db", 0), str(prev)) == 0


# --- the Mac transcription agent --------------------------------------------------
MAC = os.path.join(REPO, "deploy", "mac")


def _mac(*parts):
    with open(os.path.join(MAC, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_transcribe_agent_injects_path_for_ffmpeg():
    """THE trap. mlx-whisper's load_audio shells out to a BARE `ffmpeg` resolved from PATH,
    and launchd's PATH is /usr/bin:/bin:/usr/sbin:/sbin — no /opt/homebrew/bin. Without this
    line every transcription fails deep inside load_audio looking like a corrupt recording,
    and only under launchd: run the same command in a shell and it works."""
    plist = _mac("com.hopper.inbox-transcribe.plist")
    assert "<key>PATH</key>" in plist
    assert "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" in plist
    # install.sh refuses to install a plist that has lost it.
    assert "/opt/homebrew/bin" in _mac("install.sh")


def test_the_transcribe_agent_runs_often_enough_to_be_the_only_path_to_text():
    """5 minutes, not the probe's hour: this worker is the ONLY thing that turns a recording
    into readable text, so its interval IS how long Graham waits to read back what he said."""
    plist = _mac("com.hopper.inbox-transcribe.plist")
    interval = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", plist)
    assert interval and int(interval.group(1)) == 300
    assert "<key>RunAtLoad</key>" in plist and "<key>LowPriorityIO</key>" in plist
    # The stock interpreter, with mlx reached by subprocess — probes/ stays stdlib-only.
    assert "/usr/bin/python3" in plist
    assert "probes/inbox_transcribe.py" in plist


def test_the_mac_templates_are_rendered_and_never_take_a_token_on_the_cli():
    install = _mac("install.sh")
    for plist in ("com.hopper.dashboard-probe.plist", "com.hopper.inbox-transcribe.plist"):
        for ph in set(re.findall(r"@@[A-Z_]+@@", _mac(plist))):
            assert "s|%s|" % ph in install, "%s: install.sh never renders %s" % (plist, ph)
    # A token on the command line lands in shell history and in `ps`. Both are refused, and
    # both are prompted for with a hidden read instead.
    assert "--token|--inbox-token)" in install
    assert install.count("read -r -s -p") >= 2


def test_the_mac_uninstaller_knows_about_both_agents():
    uninstall = _mac("uninstall.sh")
    assert "com.hopper.dashboard-probe" in uninstall
    assert "com.hopper.inbox-transcribe" in uninstall
    assert "--inbox-only" in uninstall


# --- the audio mirror deletes, so its guards are driven for real -------------------
#
# The two DBs are additive: nothing this script does can remove an off-box copy of a
# transcript. The AUDIO tree is the deliberate exception — Graham asked for Delete and the
# privacy ceiling to actually reach Drive, so this is the one place the backup can destroy
# data, it runs unattended every five minutes, and what it deletes is the only off-box copy
# of recordings of his voice.
#
# ⚠️ THESE USED TO BE STRING ASSERTIONS ON THE SCRIPT'S TEXT, AND THAT WAS THE PROBLEM. They
# asserted that a guard expression was *present*, which is not the same claim as "the guard
# refuses". A security gate that ran the script against fake binaries found five separate
# ways the mirror over-deleted — an open brake on exactly the runs that needed it, a
# sub-threshold drip that walked a tree to zero, a percentage that disabled itself at 100, a
# brake measuring one set while the deletion used another, and a failed listing reported as a
# clean mirror — and the text assertions caught NONE of them, because every one of those
# expressions was still right there in the file.
#
# So this is a behavioural harness: a fake `docker` and a fake `rclone` on $PATH, the real
# deploy/box/backup.sh, and assertions about what is LEFT IN THE FAKE REMOTE afterwards.
# Nothing here touches a real container, a real rclone, or Drive; the fakes refuse to run at
# all unless their sandbox root is set in the environment.

FAKES = os.path.join(REPO, "tests", "fakes")
#: Everything the script shells out to, beyond the two we fake.
_NEEDED = ["find", "comm", "awk", "sed", "sort", "install", "date", "chmod", "cat", "wc", "tr"]


def _harness_skip_reason():
    """Why this module cannot run here, or None. Skips the way the shellcheck tests do."""
    missing = [b for b in _NEEDED if shutil.which(b) is None]
    if missing:
        return "missing shell utilities: %s" % ", ".join(missing)
    if shutil.which("bash") is None:
        return "no bash on PATH"
    for name in ("fake_docker.py", "fake_rclone.py"):
        if not os.path.isfile(os.path.join(FAKES, name)):
            return "test fakes are missing (%s)" % name
    return None


_SKIP = _harness_skip_reason()
audio_harness = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")

# A GNU-behaviour `mktemp` and `sha256sum`, shimmed onto the harness PATH.
#
# backup.sh runs on a Linux box. Two of its calls are written to GNU semantics and a BSD
# userland does something different rather than failing outright: `mktemp .snapshot.XXXXXX.db`
# returns the template VERBATIM on BSD (there the X's must be trailing), and `sha256sum` may
# not exist at all. Neither has anything to do with what is under test, and letting them
# decide whether the deletion guards get exercised would be the same mistake these tests were
# rewritten to fix — a suite that silently measures nothing. Shim them; test the guards
# everywhere. (Their real-world portability is the box's business, and the box is Linux.)
_MKTEMP_SHIM = "\n".join([
    "#!/usr/bin/env python3",
    "import os, sys, tempfile",
    "args = sys.argv[1:]",
    "want_dir = '-d' in args",
    "cand = [a for a in args if not a.startswith('-')]",
    "tmpl = cand[0] if cand else os.path.join(tempfile.gettempdir(), 'tmp.XXXXXX')",
    "d, base = os.path.dirname(tmpl) or '.', os.path.basename(tmpl)",
    "prefix, sep, suffix = base.partition('XXXXXX')",
    "if not sep:",
    "    prefix, suffix = base, ''",
    "if want_dir:",
    "    print(tempfile.mkdtemp(prefix=prefix, suffix=suffix, dir=d))",
    "else:",
    "    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=d)",
    "    os.close(fd)",
    "    print(path)",
    "",
])

_SHA256_SHIM = "\n".join([
    "#!/usr/bin/env python3",
    "import hashlib, sys",
    "for path in sys.argv[1:]:",
    "    h = hashlib.sha256()",
    "    with open(path, 'rb') as fh:",
    "        for block in iter(lambda: fh.read(65536), b''):",
    "            h.update(block)",
    "    print('%s  %s' % (h.hexdigest(), path))",
    "",
])


class Box:
    """One throwaway box: a fake container filesystem, a fake remote, and backup.sh."""

    def __init__(self, tmp_path):
        self.root = str(tmp_path)
        self.container = os.path.join(self.root, "container")
        self.remote = os.path.join(self.root, "remote")
        self.backup_root = os.path.join(self.root, "backups")
        self.bin = os.path.join(self.root, "bin")
        self.box = os.path.join(self.root, "box")
        self.audio = os.path.join(self.container, "app", "data", "inbox", "audio")
        for d in (self.audio, self.remote, self.bin, self.box):
            os.makedirs(d, exist_ok=True)

        # The script under test, byte for byte — copied out so that a real
        # deploy/box/.env.backup on a developer's machine can never be sourced into a run.
        for name in ("backup.sh", "verify_snapshot.py"):
            shutil.copy2(os.path.join(BOX, name), os.path.join(self.box, name))
        os.chmod(os.path.join(self.box, "backup.sh"), 0o755)
        self.script = os.path.join(self.box, "backup.sh")

        self._shim("docker", os.path.join(FAKES, "fake_docker.py"))
        self._shim("rclone", os.path.join(FAKES, "fake_rclone.py"))
        self._write_exec(os.path.join(self.bin, "mktemp"), _MKTEMP_SHIM)
        self._write_exec(os.path.join(self.bin, "sha256sum"), _SHA256_SHIM)

        for name in ("dashboard", "inbox"):
            self._make_db(os.path.join(self.container, "app", "data", name + ".db"), name)

    # --- construction helpers ---------------------------------------------------
    def _write_exec(self, path, body):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def _shim(self, name, target):
        self._write_exec(os.path.join(self.bin, name),
                         '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, target))

    def _make_db(self, path, table):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS %s (id INTEGER PRIMARY KEY, v TEXT)" % table)
        conn.execute("INSERT INTO %s (v) VALUES ('seed')" % table)
        conn.commit()
        conn.close()

    # --- the fake world ---------------------------------------------------------
    def add_audio(self, *names):
        for n in names:
            with open(os.path.join(self.audio, n), "w", encoding="utf-8") as fh:
                fh.write("audio:" + n)

    def add_audio_n(self, count, start=0):
        names = ["rec%03d.webm" % i for i in range(start, start + count)]
        self.add_audio(*names)
        return names

    def remove_audio(self, *names):
        for n in names:
            os.remove(os.path.join(self.audio, n))

    def keep_only(self, count):
        """Delete all but the first `count` recordings, by name order."""
        names = sorted(os.listdir(self.audio))
        self.remove_audio(*names[count:])

    def seed_remote_audio(self, *names):
        d = os.path.join(self.remote, "hopper-dashboard-backups", "audio")
        os.makedirs(d, exist_ok=True)
        for n in names:
            with open(os.path.join(d, n), "w", encoding="utf-8") as fh:
                fh.write("audio:" + n)

    def remote_audio(self):
        d = os.path.join(self.remote, "hopper-dashboard-backups", "audio")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def remote_dbs(self):
        d = os.path.join(self.remote, "hopper-dashboard-backups")
        if not os.path.isdir(d):
            return []
        return sorted(n for n in os.listdir(d) if n.endswith(".db"))

    def remote_daily(self):
        d = os.path.join(self.remote, "hopper-dashboard-backups", "daily")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def stored_count(self):
        p = os.path.join(self.backup_root, "state", "last_audio_count")
        if not os.path.isfile(p):
            return None
        with open(p, encoding="utf-8") as fh:
            return fh.read().strip()

    def age_high_water(self, seconds):
        """Backdate the windowed high-water mark, so its expiry can be exercised by a test
        that necessarily runs in well under a second."""
        p = os.path.join(self.backup_root, "state", "audio_high_water")
        with open(p, encoding="utf-8") as fh:
            count, epoch = fh.read().split()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("%s %d\n" % (count, int(epoch) - int(seconds)))

    # --- running it -------------------------------------------------------------
    def run(self, expect=None, **env):
        e = dict(os.environ)
        for stray in ("AUDIO_ALLOW_MASS_DELETE", "AUDIO_MAX_DROP_PCT",
                      "AUDIO_MAX_DROP_FILES", "AUDIO_DROP_WINDOW_MIN", "BACKUP_AUDIO"):
            e.pop(stray, None)
        e.update({
            "PATH": self.bin + os.pathsep + e.get("PATH", "/usr/bin:/bin"),
            "HOME": self.root,
            "LC_ALL": "C",
            "FAKE_DOCKER_ROOT": self.container,
            "FAKE_RCLONE_ROOT": self.remote,
            "BACKUP_CONTAINER": "fake-dashboard",
            "BACKUP_ROOT": self.backup_root,
            "RCLONE_DEST": "gdrive:hopper-dashboard-backups",
            "DRIVE_PUSH_INTERVAL_MIN": "1",
        })
        for k, v in env.items():
            e[k] = str(v)
        # The 15-minute Drive throttle is real and deliberate, and it is not what any of
        # these tests are about: without this, a second run in the same minute would be a
        # no-op and every multi-run scenario below would be asserting on nothing.
        try:
            os.remove(os.path.join(self.backup_root, "state", "last_drive_push.epoch"))
        except OSError:
            pass
        proc = subprocess.run([self.script], env=e, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=300)
        out = proc.stdout.decode("utf-8", "replace")
        if expect is not None and proc.returncode != expect:
            raise AssertionError("expected rc=%s, got %s:\n%s"
                                 % (expect, proc.returncode, out))
        return proc.returncode, out


@pytest.fixture()
def box(tmp_path):
    if _SKIP:
        pytest.skip(_SKIP)
    return Box(tmp_path)


# --- the harness itself has to be trustworthy -------------------------------------
@audio_harness
def test_the_harness_really_drives_the_script(box):
    """If this fails, every assertion below is measuring nothing."""
    box.add_audio("a.webm", "b.webm")
    rc, out = box.run(expect=0)
    assert "backup.sh:" in out and "done (2 DB snapshot(s))" in out
    assert box.remote_audio() == ["a.webm", "b.webm"]
    assert len(box.remote_dbs()) == 2          # dashboard_*.db + inbox_*.db
    assert len(box.remote_daily()) == 2


# --- the happy path ----------------------------------------------------------------
@audio_harness
def test_a_normal_run_mirrors_and_a_normal_deletion_reaches_drive(box):
    box.add_audio("a.webm", "b.webm", "c.webm")
    box.run(expect=0)
    assert box.remote_audio() == ["a.webm", "b.webm", "c.webm"]
    # Graham deletes one voice note. Delete must mean deleted everywhere — that is the whole
    # reason this one tree mirrors instead of accumulating.
    box.remove_audio("b.webm")
    rc, out = box.run(expect=0)
    assert box.remote_audio() == ["a.webm", "c.webm"]
    assert "deleted b.webm" in out
    assert box.stored_count() == "2"


@audio_harness
def test_the_reported_count_is_the_count_actually_mirrored(box):
    box.add_audio_n(4)
    box.run(expect=0)
    box.add_audio("late.webm")
    rc, out = box.run(expect=0)
    assert len(box.remote_audio()) == 5
    assert "audio: 5 recording(s) mirrored" in out
    assert box.stored_count() == "5"


# --- the DBs stay additive, in every scenario --------------------------------------
@audio_harness
def test_the_databases_never_lose_an_off_box_copy_however_the_audio_run_ends(box):
    """The invariant the whole backup rests on: whatever the audio mirror does — mirror,
    refuse, fail its listing, half-copy — no DB snapshot already on Drive is ever removed."""
    box.add_audio_n(30)
    box.run(expect=0)
    first = set(box.remote_dbs())
    assert first
    seen = set(first)
    db = os.path.join(box.container, "app", "data", "inbox.db")

    def a_round(label, **env):
        conn = sqlite3.connect(db)             # a new row => a genuinely new snapshot
        conn.execute("INSERT INTO inbox (v) VALUES (?)", (label,))
        conn.commit()
        conn.close()
        box.run(**env)
        now = set(box.remote_dbs())
        assert seen <= now, "%s removed an off-box DB snapshot: %s" % (
            label, sorted(seen - now))
        seen.update(now)

    a_round("clean-mirror")
    box.keep_only(1)
    a_round("refused-mass-deletion")
    a_round("unreadable-remote", FAKE_RCLONE_LSF_FAIL="audio")
    box.add_audio_n(30, start=100)
    a_round("partial-copy-out", FAKE_DOCKER_CP_LIMIT=2)
    a_round("recovered")
    assert len(seen) > len(first), "the DB ring never grew; the scenarios did nothing"


# --- BLOCKER: no stored count must not mean "no brake" -----------------------------
@audio_harness
def test_with_no_stored_count_the_baseline_comes_from_the_remote_not_from_nothing(box):
    """THE blocker. `prev` used to fall back to a host directory that nothing ever created,
    so it was 0 — and a baseline of 0 opens the brake completely — on precisely the runs that
    need it most: the first run after deploy, a cleared state dir, a changed BACKUP_ROOT or
    HOME, and a rebuild-from-Drive. Measured before the fix: a container holding 1 file
    against a remote holding 20 deleted 19 of them and logged a clean mirror."""
    box.seed_remote_audio(*["old%03d.webm" % i for i in range(20)])
    box.add_audio("survivor.webm")
    assert box.stored_count() is None
    rc, out = box.run()
    assert rc != 0
    assert len(box.remote_audio()) == 20, "the unguarded first run deleted the remote"
    assert "REFUSING" in out
    assert box.stored_count() is None, "a refused run must not record a baseline"


@audio_harness
def test_a_first_run_against_a_matching_remote_still_mirrors_a_real_deletion(box):
    """The flip side: taking the baseline from the remote must not freeze the mirror. A
    genuine single deletion still propagates on a run with no remembered count."""
    box.seed_remote_audio("a.webm", "b.webm", "c.webm")
    box.add_audio("a.webm", "c.webm")
    rc, out = box.run(expect=0)
    assert box.remote_audio() == ["a.webm", "c.webm"]
    assert box.stored_count() == "2"


@audio_harness
def test_a_baseline_that_cannot_be_established_deletes_nothing_and_records_nothing(box):
    """"Could not list the remote" is not "the remote is empty". With no stored count and no
    usable listing the run uploads and stops — and deliberately does NOT write a count,
    because recording a baseline it just failed to verify is how the next run would be handed
    a licence to delete whatever it could not see."""
    box.seed_remote_audio("ghost.webm")
    box.add_audio("a.webm", "b.webm")
    rc, out = box.run(FAKE_RCLONE_LSF_FAIL="audio")
    assert rc != 0
    assert "could not be listed" in out
    assert "ghost.webm" in box.remote_audio()
    assert box.stored_count() is None
    # ...and the upload still happened. Data safety first; deletion can wait a cycle.
    assert set(["a.webm", "b.webm"]).issubset(set(box.remote_audio()))


# --- SHOULD-FIX: an absolute limit, independent of the percentage ------------------
@audio_harness
def test_the_absolute_limit_refuses_a_big_delete_the_percentage_would_allow(box):
    """A percentage cannot see "a lot of files at once", only "a large share". With the
    percentage wound right open, the file-count brake must still refuse on its own."""
    box.add_audio_n(40)
    box.run(expect=0, AUDIO_MAX_DROP_PCT=99)
    box.keep_only(10)                                   # 30 deletions in a single run
    rc, out = box.run(AUDIO_MAX_DROP_PCT=99, AUDIO_MAX_DROP_FILES=25)
    assert rc != 0
    assert "AUDIO_MAX_DROP_FILES" in out
    assert len(box.remote_audio()) == 40, "nothing may be deleted once a run is refused"
    assert box.stored_count() == "40"


@audio_harness
def test_a_sub_threshold_drip_is_caught_by_the_windowed_high_water_mark(box):
    """The drip: 45% per run against a 50% brake never trips, and a tree walks to zero in a
    few five-minute ticks — 1024 files reach 1 in fifty minutes. The percentage is therefore
    measured against the highest count seen in the window, not only against the previous run,
    so cumulative loss is visible even when no single step is large enough to trip anything."""
    box.add_audio_n(32)
    loose = {"AUDIO_MAX_DROP_FILES": 10000}             # isolate the proportional brake
    box.run(expect=0, **loose)
    box.keep_only(18)                                   # 43.75% — under the per-run brake
    rc, out = box.run(**loose)
    assert rc == 0, out
    assert len(box.remote_audio()) == 18
    box.keep_only(10)                                   # another 44% — but 69% off the mark
    rc, out = box.run(**loose)
    assert rc != 0, "the drip walked straight past the brake:\n%s" % out
    assert "down from 32" in out
    assert len(box.remote_audio()) == 18, "the drip reached the remote"


@audio_harness
def test_the_high_water_mark_ages_out_so_the_brake_does_not_seize(box):
    """It is a window, not a permanent ceiling: once the mark has aged out, a tree that
    shrank legitimately goes on mirroring without anyone reaching for an override."""
    box.add_audio_n(32)
    loose = {"AUDIO_MAX_DROP_FILES": 10000}
    box.run(expect=0, **loose)
    box.keep_only(18)
    box.run(expect=0, **loose)
    box.keep_only(10)
    # Exactly the step the test above refuses — but with the mark backdated past its window,
    # so the baseline falls back to the previous run and the same step is allowed.
    box.age_high_water(2 * 3600)
    rc, out = box.run(AUDIO_DROP_WINDOW_MIN=60, **loose)
    assert rc == 0, out
    assert len(box.remote_audio()) == 10


# --- SHOULD-FIX: the percentage is a percentage ------------------------------------
@audio_harness
@pytest.mark.parametrize("bad", ["100", "200", "0", "abc", "99999999999999999999"])
def test_a_percentage_outside_1_to_99_is_refused_before_anything_runs(box, bad):
    """`require_positive_int` was the wrong validator. 100 makes the comparison
    `count * 100 < prev * 0` — never true, so the brake is silently OFF; 200 makes the
    right-hand side negative, which is off AND inverted; a 20-digit value overflows the
    arithmetic. Measured: with PCT=100 and with PCT=200, a 95% drop went through in silence."""
    box.add_audio_n(20)
    box.run(expect=0)
    box.keep_only(1)
    rc, out = box.run(AUDIO_MAX_DROP_PCT=bad)
    assert rc != 0
    assert "AUDIO_MAX_DROP_PCT" in out
    assert len(box.remote_audio()) == 20, "a bad percentage let a 95% drop through"


@audio_harness
def test_a_leading_zero_percentage_is_base_ten_not_octal(box):
    """`050` must mean 50, not octal 40. The two differ in exactly one place — a 45% drop —
    so that is what is measured. Nothing here is theoretical: in bash, `(( 050 ))` is 40."""
    box.add_audio_n(100)
    loose = {"AUDIO_MAX_DROP_FILES": 10000}
    box.run(expect=0, AUDIO_MAX_DROP_PCT="050", **loose)
    box.keep_only(55)                                   # a 45% drop
    rc, out = box.run(AUDIO_MAX_DROP_PCT="050", **loose)
    assert rc == 0, "050 was read as octal 40, which refuses a 45% drop:\n%s" % out
    assert len(box.remote_audio()) == 55


# --- SHOULD-FIX: the brake and the deletion must measure the same set --------------
@audio_harness
def test_a_container_count_that_disagrees_with_the_staged_tree_refuses(box):
    """No injected fault is needed for this in production: the in-container count is
    `os.walk`, which counts symlinks, while the staged tree is measured with `find -type f`,
    which does not. Measured: 4 real files + 6 symlinks reported 10, the guard stayed silent,
    and 6 of 10 remote recordings were deleted under a log line reading "10 mirrored"."""
    box.add_audio_n(10)
    box.run(expect=0)
    box.keep_only(4)
    rc, out = box.run(FAKE_DOCKER_AUDIO_COUNT=10)       # the container still claims 10
    assert rc != 0
    assert "do not describe the same set" in out
    assert len(box.remote_audio()) == 10
    assert box.stored_count() == "10"


@audio_harness
def test_a_partial_copy_out_that_exits_zero_deletes_nothing(box):
    """An interrupted `docker cp` that still returns 0 looks exactly like "Graham deleted
    most of his recordings". Measured before the fix: 18 of 20 deleted."""
    box.add_audio_n(20)
    box.run(expect=0)
    rc, out = box.run(FAKE_DOCKER_CP_LIMIT=2)
    assert rc != 0
    assert "do not describe the same set" in out
    assert len(box.remote_audio()) == 20
    assert box.stored_count() == "20"


# --- SHOULD-FIX: a failed listing is not a clean mirror ----------------------------
@audio_harness
def test_an_unreadable_remote_listing_is_never_reported_as_a_clean_mirror(box):
    """`rclone lsf ... 2>/dev/null | sort || true` made "could not list" indistinguishable
    from "listed it, nothing extra". Measured: the run logged "4 recording(s) mirrored ...
    (deletions included)" while the remote still held 5, and advanced the stored count to
    4 — the corrupted baseline that lets the NEXT run mirror a wipe."""
    box.add_audio_n(5)
    box.run(expect=0)
    assert box.stored_count() == "5"
    box.keep_only(4)
    rc, out = box.run(FAKE_RCLONE_LSF_FAIL="audio")
    assert rc != 0
    assert "deletions included" not in out
    assert len(box.remote_audio()) == 5, "nothing should have been reconciled"
    assert box.stored_count() == "5", "a failed listing must not advance the baseline"


@audio_harness
def test_a_failed_deletion_does_not_advance_the_stored_count_either(box):
    box.add_audio_n(5)
    box.run(expect=0)
    box.keep_only(4)                                    # rec004 is the extra
    rc, out = box.run(FAKE_RCLONE_DELETE_FAIL="rec004")
    assert rc != 0
    assert box.stored_count() == "5"


# --- SHOULD-FIX: the override authorises ONE purge ---------------------------------
@audio_harness
def test_the_override_must_name_the_resulting_count_and_authorises_only_that_purge(box):
    """It is an ordinary env var read from .env.backup, so "re-run once with it set" is
    advice a file cannot enforce — left behind, a boolean disables every brake for ever with
    nothing but a WARN line. Carrying the expected count makes it self-expiring: once that
    purge has happened, the value describes a state that already exists."""
    box.add_audio_n(5)
    box.run(expect=0)
    box.keep_only(1)
    # 1. Refused without it.
    rc, out = box.run()
    assert rc != 0 and len(box.remote_audio()) == 5
    # 2. The WRONG count does not authorise it either.
    rc, out = box.run(AUDIO_ALLOW_MASS_DELETE=4)
    assert rc != 0, out
    assert len(box.remote_audio()) == 5
    # 3. The right count purges, once.
    rc, out = box.run(AUDIO_ALLOW_MASS_DELETE=1)
    assert rc == 0, out
    assert len(box.remote_audio()) == 1
    assert box.stored_count() == "1"
    # 4. LEFT IN PLACE, it does not authorise the NEXT purge. This is the whole point.
    box.keep_only(0)
    rc, out = box.run(AUDIO_ALLOW_MASS_DELETE=1)
    assert rc != 0, "a stale override authorised a second, different purge:\n%s" % out
    assert len(box.remote_audio()) == 1
    assert box.stored_count() == "1"


@audio_harness
def test_an_empty_container_never_wipes_a_populated_remote_without_naming_zero(box):
    """The degenerate case: a lost volume must not read as "Graham deleted everything"."""
    box.add_audio_n(3)
    box.run(expect=0)
    box.keep_only(0)
    wide_open = {"AUDIO_MAX_DROP_PCT": 99, "AUDIO_MAX_DROP_FILES": 10000}
    rc, out = box.run(**wide_open)
    assert rc != 0
    assert len(box.remote_audio()) == 3
    rc, out = box.run(AUDIO_ALLOW_MASS_DELETE=0, **wide_open)
    assert rc == 0, out
    assert box.remote_audio() == []


# --- the missing tree is never a deletion ------------------------------------------
@audio_harness
def test_a_missing_audio_directory_skips_the_sync_entirely(box):
    """A fresh volume, the wrong container, or a mistyped CONTAINER_AUDIO_DIR. None of them
    is a deletion, and the run must still succeed so the DB snapshots keep flowing."""
    box.add_audio_n(4)
    box.run(expect=0)
    shutil.rmtree(box.audio)
    rc, out = box.run(expect=0)
    assert "SKIPPING the audio sync entirely" in out
    assert len(box.remote_audio()) == 4
    assert box.stored_count() == "4"
    assert len(box.remote_dbs()) == 2, "the DB half must be unaffected"


@audio_harness
def test_backup_audio_zero_leaves_the_remote_audio_tree_completely_alone(box):
    box.add_audio_n(4)
    box.run(expect=0)
    box.keep_only(0)
    rc, out = box.run(expect=0, BACKUP_AUDIO=0)
    assert len(box.remote_audio()) == 4


# --- and the one shape the old text assertions were right about --------------------
@audio_harness
def test_no_blunt_whole_tree_mirror_verb_exists_anywhere_in_the_script():
    """Still worth asserting on the text: an `rclone sync`/`bisync`/`--delete-*` would be one
    edit away from being pointed at the DB ring, where deletion must be impossible rather
    than merely guarded. The deletes here are ours — one file at a time, logged, after the
    guards — and `delete_remote_extras` is the only function that can perform one."""
    body = _read("backup.sh")
    for verb in ("rclone sync", "rclone bisync", "rclone move", "--delete-during",
                 "--delete-before", "--delete-after"):
        assert verb not in body, "%s would delete a tree in one uninspectable step" % verb
    calls = [ln.strip() for ln in body.splitlines()
             if "rclone deletefile" in ln and not ln.lstrip().startswith("#")]
    assert len(calls) == 2, calls           # prune_remote's, and delete_remote_extras' own
    before_audio = body[:body.index("push_audio()")]
    assert "delete_remote_extras " not in before_audio
