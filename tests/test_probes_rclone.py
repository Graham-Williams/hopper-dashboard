"""rclone `check --combined` parsing → lag bytes/files; PAIRS extraction from the offload script.
No rclone is invoked."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import rclone_check  # noqa: E402
from probes.common import ProbeError  # noqa: E402

COMBINED = """= gameplay recordings/2026-08-20 21-03-11.mkv
- gameplay recordings/2026-09-03 20-11-40.mkv
- webcam/webcam-2026-09-03 20-11-40.mkv
* world/notes.txt
! webcam/broken.mkv
+ only-on-drive.mkv
= webcam/webcam-2026-08-20 21-03-11.mkv
garbage line without marker
"""


def test_parse_combined_buckets():
    r = rclone_check.parse_combined(COMBINED)
    assert r.matched == ["gameplay recordings/2026-08-20 21-03-11.mkv", "webcam/webcam-2026-08-20 21-03-11.mkv"]
    assert r.missing == ["gameplay recordings/2026-09-03 20-11-40.mkv", "webcam/webcam-2026-09-03 20-11-40.mkv"]
    assert r.differ == ["world/notes.txt"]
    assert r.errors == ["webcam/broken.mkv"]
    assert r.extra == ["only-on-drive.mkv"]
    assert r.lag_files == 3
    assert r.lag_paths == r.missing + r.differ


def test_parse_combined_empty_and_caught_up():
    assert rclone_check.parse_combined("").lag_files == 0
    r = rclone_check.parse_combined("= a\n= b\n")
    assert r.lag_files == 0 and len(r.matched) == 2


def test_sum_local_sizes(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.mkv").write_bytes(b"x" * 1000)
    (tmp_path / "b.mkv").write_bytes(b"y" * 24)
    total, counted = rclone_check.sum_local_sizes(str(tmp_path), ["sub/a.mkv", "b.mkv", "vanished.mkv"])
    assert total == 1024 and counted == 2


def test_lag_from_combined_end_to_end(tmp_path):
    (tmp_path / "gameplay recordings").mkdir()
    (tmp_path / "gameplay recordings" / "new.mkv").write_bytes(b"z" * 5000)
    (tmp_path / "old.mkv").write_bytes(b"z" * 7)
    r = rclone_check.parse_combined("= old.mkv\n- gameplay recordings/new.mkv\n")
    lag_bytes, _ = rclone_check.sum_local_sizes(str(tmp_path), r.lag_paths)
    assert (r.lag_files, lag_bytes) == (1, 5000)


def test_parse_size_json():
    assert rclone_check.parse_size_json('{"count":713,"bytes":7239904,"sizeless":0}') == (713, 7239904)
    assert rclone_check.parse_size_json("not json") == (None, None)
    assert rclone_check.parse_size_json("[]") == (None, None)


def test_summarize_stderr_strips_timestamps():
    err = ("2026/09/04 23:33:41 ERROR : INVENTORY.md: sizes differ\n"
           "2026/09/04 23:33:42 NOTICE: Google drive root 'Backups/personal-assistant': 3 differences found\n"
           "2026/09/04 23:33:42 DEBUG : noise\n")
    s = rclone_check.summarize_stderr(err)
    assert s.startswith("ERROR : INVENTORY.md") and "3 differences found" in s and "DEBUG" not in s


# --- PAIRS from the offload script ----------------------------------------------
SCRIPT = '''
PAIRS=(
  "recordings|Gremlins/recordings|stage"
  "world backups|Gremlins/world backups|nostage"
  "replays|Gremlins/replays|nostage"
)
DO_UPLOAD=1
'''


def test_parse_pairs_from_script():
    assert rclone_check.parse_pairs_from_script(SCRIPT) == rclone_check.DEFAULT_OFFLOAD_PAIRS


def test_parse_pairs_missing_block_falls_back(tmp_path):
    assert rclone_check.parse_pairs_from_script("echo hi") == []
    assert rclone_check.load_offload_pairs(str(tmp_path / "nope.sh")) == rclone_check.DEFAULT_OFFLOAD_PAIRS
    p = tmp_path / "offload.sh"
    p.write_text('PAIRS=(\n  "clips|Gremlins/clips|stage"\n)\n')
    assert rclone_check.load_offload_pairs(str(p)) == [("clips", "Gremlins/clips", "stage")]


def test_pa_filters_match_backup_script_order():
    # The two '+' rules must precede '- .env*' or the template/example get swept by the secret rule.
    f = rclone_check.PA_BACKUP_FILTERS
    assert f.index("+ .env.1pass") < f.index("- .env*") and f.index("+ .env.example") < f.index("- .env*")
    assert "- minecraft-channel/recordings/**" in f


# --- rclone_check wrapper with a stubbed run_cmd ---------------------------------
def test_rclone_check_rc1_with_differences(monkeypatch):
    calls = {}

    def fake_run(argv, timeout, cwd=None):
        calls["argv"] = argv
        return 1, "= a\n- b\n", "NOTICE: 1 differences found"

    monkeypatch.setattr(rclone_check, "run_cmd", fake_run)
    r = rclone_check.rclone_check("/x/rclone", "/src", "gdrive:dst", filters=["- .git/**"], excludes=[".DS_Store"], min_age="15m")
    assert r.missing == ["b"]
    a = calls["argv"]
    assert a[:4] == ["/x/rclone", "check", "/src", "gdrive:dst"] and "--one-way" in a and "--combined" in a
    assert a[a.index("--filter") + 1] == "- .git/**" and a[a.index("--exclude") + 1] == ".DS_Store"
    assert a[a.index("--min-age") + 1] == "15m"
    assert "copy" not in a and "sync" not in a  # read-only, always


def test_rclone_check_other_rc_raises(monkeypatch):
    monkeypatch.setattr(rclone_check, "run_cmd", lambda argv, timeout, cwd=None: (3, "", "2026/09/04 00:00:00 NOTICE: Failed to check: directory not found"))
    with pytest.raises(ProbeError) as ei:
        rclone_check.rclone_check("/x/rclone", "/src", "gdrive:dst")
    assert "directory not found" in str(ei.value)


def test_rclone_check_timeout_raises(monkeypatch):
    monkeypatch.setattr(rclone_check, "run_cmd", lambda argv, timeout, cwd=None: (-1, "", "timeout after 120s"))
    with pytest.raises(ProbeError):
        rclone_check.rclone_check("/x/rclone", "/src", "gdrive:dst")


def test_rclone_size_wrapper(monkeypatch):
    monkeypatch.setattr(rclone_check, "run_cmd", lambda argv, timeout, cwd=None: (0, '{"count":2,"bytes":10}', ""))
    assert rclone_check.rclone_size("/x/rclone", "gdrive:x") == (2, 10)


def test_disk_free_shape(tmp_path):
    d = rclone_check.disk_free(str(tmp_path))
    assert d["disk_free_bytes"] > 0 and d["disk_total_bytes"] >= d["disk_free_bytes"]
