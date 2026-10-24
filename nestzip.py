#!/usr/bin/env python3
"""nestzip - zip a folder that doesn't fit twice on your disk.

Files go into the archive batch by batch. Each batch is written, flushed to
the physical disk and re-read with its CRC-32 checked; only then are the
originals deleted. Peak extra space is one batch, not the whole folder.

Run the same command again after a crash, a power cut or Ctrl+C: it resumes.
"""
import argparse
import os
import shutil
import stat
import struct
import sys
import zipfile
import zlib

SIDE = ".nestzip"  # sidecar: last known-good central directory of the archive
CHUNK = 1 << 20


def parse_size(s):
    s = s.strip().upper()
    for unit, mult in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10), ("B", 1)):
        if s.endswith(unit):
            return int(float(s[: -len(unit)]) * mult)
    return int(s)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fsync_dir(path):
    if os.name != "nt":  # Windows can't open a directory; NTFS journals the rename itself
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def scan(src):
    """Items to pack: (path, name in archive, size). Empty dirs end with '/'."""
    items, skipped = [], []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        names.sort()
        rel = os.path.relpath(root, src).replace(os.sep, "/")
        for n in names:
            p = os.path.join(root, n)
            if os.path.islink(p) or not os.path.isfile(p):
                skipped.append(p)  # symlinks, sockets, devices: left untouched
                continue
            name = n if rel == "." else f"{rel}/{n}"
            items.append((p, name, os.path.getsize(p)))
        skipped += [os.path.join(root, d) for d in dirs if os.path.islink(os.path.join(root, d))]
        if not dirs and not names and rel != ".":
            items.append((root, rel + "/", 0))
    return items, skipped


def batches(items, limit):
    cur, size = [], 0
    for it in items:
        if cur and size + it[2] > limit:
            yield cur
            cur, size = [], 0
        cur.append(it)
        size += it[2]
    if cur:
        yield cur


def save_tail(arc):
    """Copy the archive's central directory + end record to the sidecar, atomically."""
    # shortcut: start_dir is an undocumented ZipFile attribute (stable since Python 2), re-check on new Pythons
    with zipfile.ZipFile(arc) as z:
        start = z.start_dir
    with open(arc, "rb") as f:
        f.seek(start)
        tail = f.read()
    tmp = arc + SIDE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(struct.pack("<Q", start) + tail)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, arc + SIDE)
    fsync_dir(os.path.dirname(os.path.abspath(arc)))


def restore_tail(arc):
    """Cut off any half-written batch and put back the last good central directory."""
    with open(arc + SIDE, "rb") as f:
        start = struct.unpack("<Q", f.read(8))[0]
        tail = f.read()
    with open(arc, "r+b") as f:
        f.truncate(start)
        f.seek(start)
        f.write(tail)
        f.flush()
        os.fsync(f.fileno())


def crc_of(path):
    crc = 0
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            crc = zlib.crc32(b, crc)
    return crc


def verify(z, name, size):
    info = z.getinfo(name)
    if info.file_size != size:
        raise RuntimeError(f"{name}: changed while packing ({size} -> {info.file_size} bytes)")
    with z.open(info) as e:  # ZipExtFile raises BadZipFile if the CRC-32 doesn't match
        while e.read(CHUNK):
            pass


def remove(path):
    if os.path.isdir(path):
        os.rmdir(path)
        return
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)  # read-only files on Windows
        os.remove(path)


def commit(arc, batch, method):
    try:
        with open(arc, "r+b") as f:
            with zipfile.ZipFile(f, "a", method, allowZip64=True) as z:
                for path, name, _ in batch:
                    z.write(path, name)
            f.flush()
            os.fsync(f.fileno())
        with zipfile.ZipFile(arc) as z:
            for _, name, size in batch:
                verify(z, name, size)
    except BaseException:
        restore_tail(arc)  # back to the last good state; no original was deleted yet
        raise
    save_tail(arc)
    for path, _, _ in batch:
        remove(path)


def plan(items, src, arc, limit, keep_free):
    total = sum(s for _, _, s in items)
    peak = max((sum(s for _, _, s in b) for b in batches(items, limit)), default=0)
    out_dir = os.path.dirname(os.path.abspath(arc))
    same_disk = os.stat(src).st_dev == os.stat(out_dir).st_dev
    # deflate can grow incompressible data a tiny bit, plus headers
    need = int((peak if same_disk else total) * 1.01) + (1 << 20) + keep_free
    free = shutil.disk_usage(out_dir).free
    print(f"{src}: {len(items)} items, {human(total)}")
    print(f"  normal zip needs        {human(total + keep_free)} free")
    print(f"  nestzip needs           {human(need)} free (largest batch {human(peak)} + {human(keep_free)} kept free)")
    if not same_disk:
        print("  note: archive is on another disk, so deleting the originals frees nothing there")
    print(f"  available               {human(free)}")
    return need <= free


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nestzip", description="Zip a folder in place: originals are deleted as they are safely packed.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("pack", "check"):
        p = sub.add_parser(cmd)
        p.add_argument("folder")
        p.add_argument("-o", "--output", help="archive path (default: <folder>.zip next to the folder)")
        p.add_argument("--batch", default="256MB", help="data packed before each delete step (default 256MB)")
        p.add_argument("--keep-free", default="1GB", help="free space never used (default 1GB)")
        if cmd == "pack":
            p.add_argument("--store", action="store_true", help="no compression (fast, for videos/photos)")
            p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    a = ap.parse_args(argv)

    src = os.path.abspath(a.folder)
    arc = os.path.abspath(a.output or src.rstrip("/\\") + ".zip")
    if not os.path.isdir(src):
        if os.path.exists(arc + SIDE) and a.cmd == "pack":  # crashed after the last delete
            os.remove(arc + SIDE)
            print(f"Done: {arc}")
            return
        sys.exit(f"nestzip: {a.folder} is not a folder")
    if os.path.commonpath([src, arc]) == src:
        sys.exit("nestzip: the archive can't be inside the folder being packed")
    limit, keep_free = parse_size(a.batch), parse_size(a.keep_free)
    resuming = os.path.exists(arc + SIDE)
    if os.path.exists(arc) and not resuming:
        sys.exit(f"nestzip: {arc} already exists and isn't a nestzip in progress")
    if resuming and not os.path.exists(arc):
        sys.exit(f"nestzip: found {arc + SIDE} but the archive itself is missing")

    if resuming:
        restore_tail(arc)  # also needed by check: a crash can leave a half-written batch
        print(f"Resuming {arc}")
    items, skipped = scan(src)

    if resuming:  # files packed before the interruption but not yet deleted
        with zipfile.ZipFile(arc) as z:
            done = {i.filename: i for i in z.infolist()}
            todo = []
            for path, name, size in items:
                info = done.get(name)
                if info is None:
                    todo.append((path, name, size))
                elif name.endswith("/") or (info.file_size == size and info.CRC == crc_of(path)):
                    if a.cmd == "pack":
                        verify(z, name, size)
                        remove(path)
                else:
                    sys.exit(f"nestzip: {name} is already in the archive with different content, fix it by hand")
        items = todo

    fits = plan(items, src, arc, limit, keep_free)
    for p in skipped:
        print(f"  skipped (not a regular file): {p}")
    if a.cmd == "check":
        sys.exit(0 if fits else 3)
    if not fits:
        sys.exit("nestzip: not enough free space, nothing was changed")
    if not a.yes and input("Pack and delete originals as they are saved? [y/N] ").strip().lower() != "y":
        sys.exit(1)

    if not resuming:
        zipfile.ZipFile(arc, "w").close()  # empty but valid archive
        save_tail(arc)
    method = zipfile.ZIP_STORED if a.store else zipfile.ZIP_DEFLATED
    todo = list(batches(items, limit))
    try:
        for i, b in enumerate(todo, 1):
            commit(arc, b, method)
            print(f"  [{i}/{len(todo)}] packed {len(b)} items, {human(sum(s for _, _, s in b))} freed")
    except KeyboardInterrupt:
        print("\nPaused. Run the same command to continue.")
        sys.exit(130)

    for root, _, _ in os.walk(src, topdown=False):
        try:
            os.rmdir(root)  # only succeeds on dirs we emptied
        except OSError:
            pass
    os.remove(arc + SIDE)
    print(f"Done: {arc} ({human(os.path.getsize(arc))})")


if __name__ == "__main__":
    main()
