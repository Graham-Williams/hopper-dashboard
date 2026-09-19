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
"""
import os
import shutil
import subprocess
import sys

ROOT = os.environ.get("FAKE_DOCKER_ROOT")
if not ROOT:
    sys.stderr.write("fake docker: FAKE_DOCKER_ROOT is not set; refusing to run\n")
    sys.exit(97)


def host(path):
    return os.path.join(ROOT, path.lstrip("/"))


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
            _, path = src.split(":", 1)
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
        for key in ("SRC", "DST", "DIR"):
            if key in env:
                env[key] = host(env[key])
        if len(rest) >= 3 and rest[1] == "-c":
            override = os.environ.get("FAKE_DOCKER_AUDIO_COUNT")
            if override is not None:
                print(override)
                return 0
            return subprocess.call([sys.executable, "-c", rest[2]], env=env)
        if len(rest) >= 2 and rest[1] == "-":
            return subprocess.call([sys.executable, "-"], env=env, stdin=sys.stdin)
    sys.stderr.write("fake docker exec: unsupported %r\n" % (rest,))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
