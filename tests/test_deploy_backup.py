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


# --- the audio mirror deletes, so its guards are load-bearing ----------------------
#
# The two DBs are additive: nothing this script does can remove an off-box copy of a
# transcript. The AUDIO tree is the deliberate exception — Graham asked for Delete and the
# privacy ceiling to actually reach Drive, so this is the one place the backup can destroy
# data. These tests pin the guards that stand between "mirror a deletion" and "mirror a wipe".
#
# NOTE: these are static assertions on the script text, in the style of the test above. The
# guards were additionally driven end-to-end against fake docker/rclone binaries during
# development (normal run, single deletion, mass-delete refusal, missing dir, override), but
# that harness is not checked in — see the follow-up issue.


def test_the_audio_mirror_can_delete_but_only_through_the_guarded_path():
    body = _read("backup.sh")
    # Deletion of remote extras happens in exactly one function...
    assert "delete_remote_extras()" in body
    # ...and that function is only ever reached from the audio push, never from the DB path.
    audio = body[body.index("push_audio()"):]
    assert "delete_remote_extras" in audio
    before_audio = body[:body.index("push_audio()")]
    assert "delete_remote_extras " not in before_audio


def test_a_mass_deletion_is_refused_rather_than_mirrored():
    body = _read("backup.sh")
    # A proportional drop guard, expressed as a percentage that is itself validated.
    assert "AUDIO_MAX_DROP_PCT" in body
    assert 'require_positive_int AUDIO_MAX_DROP_PCT' in body
    # The comparison must be integer arithmetic against the PREVIOUS count, and must only
    # apply once a previous count exists (a first run has nothing to compare against).
    assert "prev > 0 && count * 100 < prev * (100 - AUDIO_MAX_DROP_PCT)" in body
    # Refusing must leave the off-box copies alone and say so.
    assert "REFUSING to mirror" in body


def test_an_empty_local_tree_never_wipes_a_populated_drive():
    """The degenerate case: a lost volume must not read as 'Graham deleted everything'."""
    body = _read("backup.sh")
    assert "REFUSING to delete them all" in body


def test_a_deliberate_purge_has_exactly_one_override_and_it_is_off_by_default():
    body = _read("backup.sh")
    assert 'AUDIO_ALLOW_MASS_DELETE="${AUDIO_ALLOW_MASS_DELETE:-0}"' in body
    # Both refusal sites must honour the same override, or one of them is unescapable.
    assert body.count('"${AUDIO_ALLOW_MASS_DELETE}" != "1"') == 2


def test_only_a_clean_mirror_advances_the_stored_count():
    """The subtle one. If a run that REFUSED (or half-failed) still recorded the new, lower
    count, the drop guard would compare the next run against a number Drive never reflected —
    so the second run would see no drop and happily mirror the wipe the first one prevented.
    The remembered count must therefore move only on a fully clean mirror."""
    body = _read("backup.sh")
    audio = body[body.index("push_audio()"):body.index("delete_remote_extras()")]
    assert 'count_file="${STATE_DIR}/last_audio_count"' in audio
    # The write must be guarded by rc == 0, and the guard must open before the write.
    assert "if (( rc == 0 )); then" in audio
    assert audio.index("if (( rc == 0 )); then") < audio.index('> "${count_file}"')
    # A refusal returns before ever reaching the write.
    assert audio.index("REFUSING to mirror") < audio.index("if (( rc == 0 )); then")
    # And a failed deletion pass must set rc, not be swallowed.
    assert "delete_remote_extras" in audio and "|| rc=1" in audio
