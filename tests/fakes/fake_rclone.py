#!/usr/bin/env python3
"""A fake `rclone` whose "remote" is a directory (``FAKE_RCLONE_ROOT``).

Implements only the verbs deploy/box/backup.sh actually uses — listremotes, mkdir, lsf, copy,
deletefile, rmdirs — and implements them with the behaviours the script depends on, including
the one that matters most: **lsf on a directory that does not exist FAILS**, the way the real
one does. That is not a detail. The script used to swallow that failure and read it as "the
remote is empty, nothing to delete", which is how a run could log a clean mirror while the
remote kept files nobody reconciled.

Fault injection, opt-in:
  FAKE_RCLONE_LSF_FAIL=<substr>     any `lsf` whose remote path contains substr exits 3
  FAKE_RCLONE_DELETE_FAIL=<substr>  any `deletefile` whose path contains substr exits 1
  FAKE_RCLONE_COPY_FAIL=<substr>    any `copy` whose destination contains substr exits 1
"""
import os
import shutil
import sys

ROOT = os.environ.get("FAKE_RCLONE_ROOT")
if not ROOT:
    sys.stderr.write("fake rclone: FAKE_RCLONE_ROOT is not set; refusing to run\n")
    sys.exit(97)
REMOTE_NAME = os.environ.get("FAKE_RCLONE_REMOTE", "gdrive")


def local_path(spec):
    remote, _, path = spec.partition(":")
    if not _:
        return None
    if remote != REMOTE_NAME:
        sys.stderr.write("fake rclone: unknown remote %r\n" % remote)
        sys.exit(3)
    return os.path.join(ROOT, path)


def injected(var, needle):
    sub = os.environ.get(var)
    return bool(sub) and sub in needle


def walk_files(root):
    out = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(out)


def main(argv):
    if not argv:
        return 2
    cmd, rest = argv[0], argv[1:]
    flags = [a for a in rest if a.startswith("--")]
    pos = []
    skip = False
    for idx, a in enumerate(rest):
        if skip:
            skip = False
            continue
        if a == "--include":
            skip = True
            continue
        if a.startswith("--"):
            continue
        pos.append(a)
    include = None
    for idx, a in enumerate(rest):
        if a == "--include" and idx + 1 < len(rest):
            include = rest[idx + 1]

    if cmd == "listremotes":
        print("%s:" % REMOTE_NAME)
        return 0

    if cmd == "mkdir":
        os.makedirs(local_path(pos[0]), exist_ok=True)
        return 0

    if cmd == "lsf":
        spec = pos[0]
        if injected("FAKE_RCLONE_LSF_FAIL", spec):
            sys.stderr.write("fake rclone: injected listing failure\n")
            return 3
        path = local_path(spec)
        if not os.path.isdir(path):
            sys.stderr.write("fake rclone: directory not found\n")
            return 3
        if "--recursive" in flags:
            names = walk_files(path)
        else:
            names = sorted(os.listdir(path))
            if "--files-only" in flags:
                names = [n for n in names if os.path.isfile(os.path.join(path, n))]
            else:
                names = [n + "/" if os.path.isdir(os.path.join(path, n)) else n
                         for n in names]
        if include:
            import fnmatch
            names = [n for n in names if fnmatch.fnmatch(n, include)]
        for n in names:
            print(n)
        return 0

    if cmd == "copy":
        src, dstspec = pos[0], pos[1]
        if injected("FAKE_RCLONE_COPY_FAIL", dstspec):
            sys.stderr.write("fake rclone: injected copy failure\n")
            return 1
        dst = local_path(dstspec)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for rel in walk_files(src):
                out = os.path.join(dst, rel)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                shutil.copy2(os.path.join(src, rel), out)
        elif os.path.isfile(src):
            shutil.copy2(src, os.path.join(dst, os.path.basename(src)))
        else:
            sys.stderr.write("fake rclone: no such source %r\n" % src)
            return 1
        return 0

    if cmd == "deletefile":
        spec = pos[0]
        if injected("FAKE_RCLONE_DELETE_FAIL", spec):
            sys.stderr.write("fake rclone: injected delete failure\n")
            return 1
        path = local_path(spec)
        if not os.path.isfile(path):
            sys.stderr.write("fake rclone: no such file\n")
            return 1
        os.remove(path)
        return 0

    if cmd == "rmdirs":
        root = local_path(pos[0])
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if dirpath == root:
                continue
            if not dirnames and not filenames:
                os.rmdir(dirpath)
        return 0

    sys.stderr.write("fake rclone: unsupported command %r\n" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
