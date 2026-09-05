"""Destination-lag probes built on ``rclone check --one-way --combined -``.

``--combined -`` prints one line per file to stdout:
  ``= path``  identical on both sides
  ``- path``  missing on the destination (the lag we care about)
  ``* path``  present on both but different (edited since last upload)
  ``+ path``  only on destination (not emitted with --one-way)
  ``! path``  error reading/hashing
Exit code is 1 when differences exist — that is NOT a failure of the probe. Anything else
non-zero (or a timeout) is.

Nothing here uploads: ``check`` is read-only on both sides.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from probes.common import RCLONE_TIMEOUT_S, ProbeError, run_cmd

# Filters copied VERBATIM from scripts/backup-personal-assistant.sh (personal-assistant → Drive).
# The two '+' rules MUST precede '- .env*' so the template and example survive the secret sweep.
PA_BACKUP_FILTERS: List[str] = [
    "+ .env.1pass",
    "+ .env.example",
    "- .env*",
    "- .DS_Store",
    "- .git/**",
    "- __pycache__/**",
    "- node_modules/**",
    "- *.mov",
    "- *.mp4",
    "- *.mkv",
    "- minecraft-channel/recordings/**",
]

# Filters copied VERBATIM from the third `rclone copy` in scripts/backup-personal-assistant.sh
# (~/.claude → gdrive:Backups/claude-config). Secrets (sessions/**, *.key) and churn dirs are excluded
# there for good reasons; the check must mirror them or it would report "missing" forever.
CLAUDE_CONFIG_FILTERS: List[str] = [
    "- sessions/**",
    "- cache/**",
    "- paste-cache/**",
    "- shell-snapshots/**",
    "- file-history/**",
    "- session-env/**",
    "- downloads/**",
    "- jobs/**",
    "- plugins/**",
    "- backups/**",
    "- daemon/**",
    "- daemon*",
    "- .last-*",
    "- *.key",
    "- .DS_Store",
]

# The trees the nightly backup script copies (name, local path relative to $HOME, remote subpath
# under the Backups remote, filters). The 4th copy in the script (dotfiles: an --include allowlist at
# --max-depth 1 of $HOME, ~1.5 KB) is deliberately NOT checked here — a whole-$HOME listing for four
# files is not worth it and its filter semantics differ from the other three.
PA_BACKUP_TREES: List[Tuple[str, str, str, List[str]]] = [
    ("personal-assistant", "personal-assistant", "personal-assistant", PA_BACKUP_FILTERS),
    ("hopper-memory", ".claude/projects/-Users-graham-personal-assistant/memory", "hopper-memory", []),
    ("claude-config", ".claude", "claude-config", CLAUDE_CONFIG_FILTERS),
]

# Excludes copied from scripts/offload-recordings.sh (COMMON array) + its --min-age default.
OFFLOAD_EXCLUDES: List[str] = [".DS_Store", ".tmp*/**", "delete-after-confirm/**"]
OFFLOAD_MIN_AGE = "15m"

# Fallback if the offload script can't be read/parsed. Keep in sync with its PAIRS array.
DEFAULT_OFFLOAD_PAIRS: List[Tuple[str, str, str]] = [
    ("recordings", "Gremlins/recordings", "stage"),
    ("world backups", "Gremlins/world backups", "nostage"),
    ("replays", "Gremlins/replays", "nostage"),
]

PAIR_RE = re.compile(r'^\s*"([^"|]+)\|([^"|]+)\|(stage|nostage)"\s*$', re.M)


def parse_pairs_from_script(text: str) -> List[Tuple[str, str, str]]:
    """Extract the PAIRS=( "local|remote|stage" ... ) entries from offload-recordings.sh."""
    m = re.search(r"PAIRS=\((.*?)\)", text, re.S)
    if not m:
        return []
    return [(a.strip(), b.strip(), c) for a, b, c in PAIR_RE.findall(m.group(1))]


def load_offload_pairs(script_path: Optional[str]) -> List[Tuple[str, str, str]]:
    if script_path and os.path.exists(script_path):
        try:
            with open(script_path, "r", encoding="utf-8") as fh:
                pairs = parse_pairs_from_script(fh.read())
            if pairs:
                return pairs
        except OSError:
            pass
    return list(DEFAULT_OFFLOAD_PAIRS)


@dataclass
class CheckResult:
    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)   # '-' missing on destination
    differ: List[str] = field(default_factory=list)    # '*' differ
    errors: List[str] = field(default_factory=list)    # '!' could not check
    extra: List[str] = field(default_factory=list)     # '+' only on destination

    @property
    def lag_files(self) -> int:
        """missing + differ. For manual offload jobs both count as "not safely on Drive"; for the
        nightly copy tree the dashboard treats only ``missing`` as stale (see mac_probe.probe_pa_backup)."""
        return len(self.missing) + len(self.differ)

    @property
    def lag_paths(self) -> List[str]:
        return self.missing + self.differ


def parse_combined(text: str) -> CheckResult:
    res = CheckResult()
    buckets = {"=": res.matched, "-": res.missing, "*": res.differ, "!": res.errors, "+": res.extra}
    for line in text.splitlines():
        if len(line) < 3 or line[1] != " ":
            continue
        marker, path = line[0], line[2:]
        if marker in buckets and path:
            buckets[marker].append(path)
    return res


def sum_local_sizes(src_dir: str, rel_paths: List[str]) -> Tuple[int, int]:
    """Total bytes of the given files under src_dir. Returns (bytes, files_counted); files that
    vanished between the check and the stat are skipped."""
    total = 0
    counted = 0
    for rel in rel_paths:
        p = os.path.join(src_dir, rel)
        try:
            total += os.path.getsize(p)
            counted += 1
        except OSError:
            continue
    return total, counted


def parse_size_json(text: str) -> Tuple[Optional[int], Optional[int]]:
    """``rclone size --json`` → (count, bytes)."""
    import json

    try:
        d = json.loads(text)
        return int(d.get("count")), int(d.get("bytes"))
    except (ValueError, TypeError, AttributeError):
        return None, None


def summarize_stderr(stderr: str, limit: int = 3) -> str:
    """Pick the useful NOTICE/ERROR lines out of rclone's stderr for a note."""
    keep = []
    for line in stderr.splitlines():
        if "NOTICE:" in line or "ERROR" in line or "Failed" in line:
            keep.append(re.sub(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} ", "", line).strip())
    return " | ".join(keep[:limit])


# ---------------------------------------------------------------------------
# rclone invocations (thin; the parsing above is what gets unit-tested)
# ---------------------------------------------------------------------------
def rclone_check(
    rclone: str,
    src: str,
    dst: str,
    filters: Optional[List[str]] = None,
    excludes: Optional[List[str]] = None,
    min_age: Optional[str] = None,
    size_only: bool = False,
    timeout: float = RCLONE_TIMEOUT_S,
) -> CheckResult:
    """``size_only`` compares sizes instead of hashes. Use it for the multi-GB recording trees: hashing tens
    of GB of .mkv every hour blows the probe timeout and shows up as a spurious ``mac-probe FAIL``, and a
    recording is either fully on Drive or not there at all. The small pa-backup trees keep the checksum check
    so a silently corrupted copy is still caught."""
    argv = [rclone, "check", src, dst, "--one-way", "--combined", "-"]
    if size_only:
        argv.append("--size-only")
    for f in filters or []:
        argv += ["--filter", f]
    for e in excludes or []:
        argv += ["--exclude", e]
    if min_age:
        argv += ["--min-age", min_age]
    rc, out, err = run_cmd(argv, timeout=timeout)
    if rc not in (0, 1):
        raise ProbeError("rclone check %s → %s failed (rc=%s): %s" % (src, dst, rc, summarize_stderr(err) or err.strip()[-300:]))
    res = parse_combined(out)
    if rc == 1 and res.lag_files == 0 and not res.errors:
        # differences reported but nothing in the combined list: surface stderr so it's debuggable
        raise ProbeError("rclone check %s → %s rc=1 with empty combined output: %s" % (src, dst, summarize_stderr(err)))
    return res


def rclone_size(rclone: str, remote_path: str, timeout: float = RCLONE_TIMEOUT_S) -> Tuple[Optional[int], Optional[int]]:
    rc, out, err = run_cmd([rclone, "size", remote_path, "--json"], timeout=timeout)
    if rc != 0:
        raise ProbeError("rclone size %s failed (rc=%s): %s" % (remote_path, rc, summarize_stderr(err) or err.strip()[-300:]))
    return parse_size_json(out)


def disk_free(path: str) -> Dict[str, int]:
    st = os.statvfs(path)
    return {
        "disk_free_bytes": st.f_bavail * st.f_frsize,
        "disk_total_bytes": st.f_blocks * st.f_frsize,
    }
