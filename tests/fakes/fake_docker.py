#!/usr/bin/env python3
"""A fake `docker`, good enough to drive deploy/box/backup.sh end to end.

The real script's audio mirror is the only place in this repo where a backup can DELETE data,
it runs unattended every five minutes, and what it deletes is the only off-box copy of
recordings of Graham's voice. Asserting on the script's *text* cannot tell you whether the
guards work; running it against a fake container and a fake remote and looking at what is
left can. Hence this.

A container filesystem is a host directory (``FAKE_DOCKER_ROOT``); a container path is that
path under it. Nothing here talks to a real dockerd, and the module refuses to run if the
root is not set, so it can never be mistaken for the real binary on a $PATH.

Fault injection, all opt-in via the environment:
  FAKE_DOCKER_RUNNING=false      `docker inspect` reports the container as stopped
  FAKE_DOCKER_AUDIO_COUNT=<n>    the in-container file count LIES and reports n. This is not
                                 exotic: the real command is `os.walk`, which counts symlinks,
                                 while the staged tree is measured with `find -type f`, which
                                 does not — the two disagree with no fault injected at all.
  FAKE_DOCKER_CP_LIMIT=<n>       `docker cp` of a tree copies only the first n files and still
                                 EXITS 0 — an interrupted copy that looks like a success.
  FAKE_DOCKER_EXEC_STDOUT_LIMIT=<n>
                                 `docker exec ... python3 -` passes only the first n bytes of
                                 the program's stdout through — a stream cut short.
  FAKE_DOCKER_EXEC_RC=<n>        ...and then exits n (default: the program's own exit code, so
                                 a cut-short stream can also EXIT 0 and look like a success).
  FAKE_DOCKER_EXEC_STDOUT_EXTRA=<text>
                                 ...appends these bytes to the program's stdout (too LONG).
  FAKE_DOCKER_EXEC_STDOUT_FLIP=<offset>
                                 ...flips the byte at that offset (same size, different bytes).
  FAKE_DOCKER_EXEC_KILLED=1      the exec dies as if SIGKILLed mid-snapshot: exit 137, nothing
                                 on stdout, and the temp it had made LEFT on the container's /tmp.
  FAKE_DOCKER_EXEC_SIGTERM=1     a REAL SIGTERM, mid-snapshot: the program's stdout is not read,
                                 so (given a snapshot bigger than a pipe buffer) it blocks writing
                                 it with its temp on /tmp; then it is sent SIGTERM, and whatever
                                 it leaves is what a SIGTERM leaves. Exits 128 + 15 like docker.
  FAKE_DOCKER_EXEC_ONLY=<name>   apply the four faults above only to the snapshot of that DB
                                 (the exec's NAME); default: every one.
  FAKE_DOCKER_TMPFS_BYTES=<n>    the size of the container's /tmp tmpfs (default 64 MiB, as in
                                 docker-compose.yml): disk_usage() on it reports n in total and
                                 what its files really hold as used.

Faithful to the real container where it matters: the app's container is `read_only: true`
with a TMPFS on /tmp (docker-compose.yml), and `docker cp` cannot read a file on a tmpfs
mount — the daemon answers "Could not find the file ... in container". So `docker cp` FROM
any path under /tmp fails here exactly as it does on the box (it is how every real backup
run failed while this fake let it pass). Each container has its OWN /tmp (<root>/tmp, where
a program's TMPDIR points too), so anything a snapshot leaves there is visible to a test,
and that /tmp has the tmpfs's size limit.
"""
import glob
import os
import shutil
import signal
import subprocess
import sys
import time

ROOT = os.environ.get("FAKE_DOCKER_ROOT")
if not ROOT:
    sys.stderr.write("fake docker: FAKE_DOCKER_ROOT is not set; refusing to run\n")
    sys.exit(97)


def host(path):
    return os.path.join(ROOT, path.lstrip("/"))


#: Mounted as tmpfs in the real container; `docker cp` cannot read from it.
TMPFS = ("/tmp",)

#: Loaded into every program the fake runs "in the container" (via PYTHONPATH): it makes
#: shutil.disk_usage() report the container /tmp as a tmpfs of FAKE_DOCKER_TMPFS_BYTES.
SITECUSTOMIZE = """
import collections, os, shutil
_tmpfs = os.environ.get("FAKE_DOCKER_TMPFS_DIR")
if _tmpfs:
    _limit = int(os.environ.get("FAKE_DOCKER_TMPFS_BYTES", str(64 * 1024 * 1024)))
    _real = shutil.disk_usage
    _usage = collections.namedtuple("usage", "total used free")

    def _disk_usage(path, _real=_real):
        if os.path.realpath(path) != os.path.realpath(_tmpfs):
            return _real(path)
        used = sum(os.path.getsize(os.path.join(d, f))
                   for d, _, files in os.walk(_tmpfs) for f in files)
        return _usage(_limit, used, max(0, _limit - used))

    shutil.disk_usage = _disk_usage
"""


def on_tmpfs(path):
    return any(path == m or path.startswith(m + "/") for m in TMPFS)


def cp_tree(src_dir, dst_dir):
    limit = int(os.environ.get("FAKE_DOCKER_CP_LIMIT", "-1"))
    copied = 0
    for dirpath, _, files in os.walk(src_dir):
        for name in sorted(files):
            if limit >= 0 and copied >= limit:
                continue                        # partial copy, and we still exit 0
            rel = os.path.relpath(os.path.join(dirpath, name), src_dir)
            out = os.path.join(dst_dir, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(os.path.join(dirpath, name), out)
            copied += 1


def main(argv):
    if not argv:
        return 2
    cmd = argv[0]

    if cmd == "inspect":
        print(os.environ.get("FAKE_DOCKER_RUNNING", "true"))
        return 0

    if cmd == "cp":
        src, dst = argv[1], argv[2]
        if ":" in src:
            container, path = src.split(":", 1)
            if on_tmpfs(path):
                sys.stderr.write("Error response from daemon: Could not find the file %s "
                                 "in container %s\n" % (path, container))
                return 1
            if path.endswith("/."):
                src_dir = host(path[:-2])
                if not os.path.isdir(src_dir):
                    sys.stderr.write("fake docker cp: no such directory\n")
                    return 1
                cp_tree(src_dir, dst)
                return 0
            if not os.path.isfile(host(path)):
                sys.stderr.write("fake docker cp: no such file\n")
                return 1
            shutil.copy2(host(path), dst)
            return 0
        return 1

    if cmd != "exec":
        sys.stderr.write("fake docker: unsupported command %r\n" % cmd)
        return 2

    # docker exec [-i] [-e K=V]... <container> <argv...>
    env = dict(os.environ)
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-e":
            k, _, v = argv[i + 1].partition("=")
            env[k] = v
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    rest = argv[i + 1:]                          # argv[i] is the container name
    if not rest:
        return 2

    if rest[0] == "test" and rest[1] == "-d":
        return 0 if os.path.isdir(host(rest[2])) else 1

    if rest[0] == "rm":
        for p in rest[1:]:
            if p.startswith("-"):
                continue
            try:
                os.remove(host(p))
            except OSError:
                pass
        return 0

    if rest[0] == "mktemp":
        template = rest[1]
        d = os.path.dirname(template)
        os.makedirs(host(d), exist_ok=True)
        import tempfile
        base = os.path.basename(template)
        prefix, _, suffix = base.partition("XXXXXX")
        fd, real = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=host(d))
        os.close(fd)
        print(os.path.join(d, os.path.basename(real)))   # the CONTAINER-side path
        return 0

    if rest[0] == "python3":
        for key in ("SRC", "DST", "DIR", "SNAPDIR"):
            if key in env:
                env[key] = host(env[key])
        # This container's own /tmp: where SNAPDIR and TMPDIR point, sized like the tmpfs.
        os.makedirs(host("/tmp"), exist_ok=True)
        env["TMPDIR"] = host("/tmp")
        env["FAKE_DOCKER_TMPFS_DIR"] = host("/tmp")
        site_dir = os.path.join(ROOT, ".fake-site")
        os.makedirs(site_dir, exist_ok=True)
        with open(os.path.join(site_dir, "sitecustomize.py"), "w") as fh:
            fh.write(SITECUSTOMIZE)
        env["PYTHONPATH"] = site_dir
        if len(rest) >= 3 and rest[1] == "-c":
            override = os.environ.get("FAKE_DOCKER_AUDIO_COUNT")
            if override is not None:
                print(override)
                return 0
            return subprocess.call([sys.executable, "-c", rest[2]], env=env)
        if len(rest) >= 2 and rest[1] == "-":
            faults = [k for k in ("FAKE_DOCKER_EXEC_STDOUT_LIMIT", "FAKE_DOCKER_EXEC_STDOUT_EXTRA",
                                  "FAKE_DOCKER_EXEC_STDOUT_FLIP", "FAKE_DOCKER_EXEC_KILLED",
                                  "FAKE_DOCKER_EXEC_SIGTERM")
                      if k in os.environ]
            only = os.environ.get("FAKE_DOCKER_EXEC_ONLY")
            if not faults or (only and env.get("NAME") != only):
                return subprocess.call([sys.executable, "-"], env=env, stdin=sys.stdin)
            if os.environ.get("FAKE_DOCKER_EXEC_SIGTERM"):
                child = subprocess.Popen([sys.executable, "-"], env=env, stdin=sys.stdin,
                                         stdout=subprocess.PIPE)
                temps = os.path.join(env["SNAPDIR"], "*_snap.*.db")
                deadline = time.time() + 30
                while not glob.glob(temps) and child.poll() is None and time.time() < deadline:
                    time.sleep(0.01)
                time.sleep(0.2)                  # let it fill the pipe and block writing
                if child.poll() is None:
                    child.send_signal(signal.SIGTERM)
                child.stdout.read()              # drain only now, so it can exit
                rc = child.wait()
                return 128 - rc if rc < 0 else rc
            child = subprocess.run([sys.executable, "-"], env=env, stdin=sys.stdin,
                                   stdout=subprocess.PIPE)
            if os.environ.get("FAKE_DOCKER_EXEC_KILLED"):
                # What a SIGKILL between the temp's creation and its cleanup leaves behind.
                left = os.path.join(env["SNAPDIR"], "%s_snap.k1ll3d.db" % env.get("NAME", "x"))
                with open(left, "wb") as fh:
                    fh.write(b"half a snapshot" * 64)
                return 137
            data = child.stdout
            if "FAKE_DOCKER_EXEC_STDOUT_LIMIT" in os.environ:
                data = data[:int(os.environ["FAKE_DOCKER_EXEC_STDOUT_LIMIT"])]
            if "FAKE_DOCKER_EXEC_STDOUT_EXTRA" in os.environ:
                data += os.environ["FAKE_DOCKER_EXEC_STDOUT_EXTRA"].encode()
            if "FAKE_DOCKER_EXEC_STDOUT_FLIP" in os.environ:
                at = int(os.environ["FAKE_DOCKER_EXEC_STDOUT_FLIP"])
                data = data[:at] + bytes([data[at] ^ 0xFF]) + data[at + 1:]
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            return int(os.environ.get("FAKE_DOCKER_EXEC_RC", child.returncode))
    sys.stderr.write("fake docker exec: unsupported %r\n" % (rest,))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
