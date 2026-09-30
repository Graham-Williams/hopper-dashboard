"""deploy/mac/install.sh's drafting block (§1c), RUN in bash against a scratch env file.

Only that block: the rest of the installer touches launchd. The block is cut out between its
own section markers, so a test cannot drift from the shipped script.
"""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess

import pytest

INSTALL = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "mac" / "install.sh"


def _block() -> str:
    text = INSTALL.read_text()
    start = text.index("# --- 1c. Drafting keys")
    end = text.index("# --- 2. plists")
    return text[start:end]


def _run(tmp_path, answer: str):
    env_file = tmp_path / "env"
    if not env_file.exists():
        env_file.write_text("DASHBOARD_URL=x\nINBOX_TOKEN=y\n")
        env_file.chmod(0o600)
    script = tmp_path / "block.sh"
    script.write_text("set -euo pipefail\nWANT_INBOX=1\nENV_FILE=%s\nCONF_DIR=%s\n%s"
                      % (env_file, tmp_path, _block()))
    return subprocess.run(["bash", str(script)], input=answer + "\n", text=True,
                          capture_output=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})


def _fake_claude(tmp_path):
    real = tmp_path / "versions" / "claude-1"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n")
    real.chmod(real.stat().st_mode | stat.S_IXUSR)
    link = tmp_path / "claude"
    os.symlink(real, link)
    return link


@pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="needs bash")
def test_a_relative_claude_bin_is_rejected(tmp_path):
    r = _run(tmp_path, "claude")
    assert r.returncode == 2 and "absolute path" in r.stdout
    assert "INBOX_CLAUDE_BIN" not in (tmp_path / "env").read_text()


@pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="needs bash")
def test_an_absolute_symlink_is_kept_unresolved_and_the_token_file_is_checked(tmp_path):
    link = _fake_claude(tmp_path)
    r = _run(tmp_path, str(link))
    assert r.returncode == 0, r.stderr
    env = (tmp_path / "env").read_text()
    assert "INBOX_CLAUDE_BIN=%s\n" % link in env          # the link, not its target
    assert "does not resolve" not in r.stdout
    assert "no claude token file" in r.stdout              # warns, never fails
    tok = tmp_path / "claude-token"
    tok.write_text("t\n")
    tok.chmod(0o644)
    assert "not 600" in _run(tmp_path, "").stdout
    tok.chmod(0o600)
    assert "token file OK" in _run(tmp_path, "").stdout


@pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="needs bash")
def test_a_dash_leaves_drafting_off(tmp_path):
    r = _run(tmp_path, "-")
    assert r.returncode == 0
    assert "INBOX_CLAUDE_BIN=\n" in (tmp_path / "env").read_text()
