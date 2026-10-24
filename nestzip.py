#!/usr/bin/env python3
"""nestzip - zip a folder that doesn't fit twice on your disk.

Files go into the archive batch by batch. Each batch is written, flushed to
the physical disk and read back with its CRC-32 checked; only then are the
originals deleted. The extra space needed is one batch, not the whole folder.

Run the same command again after a crash, a power cut or Ctrl+C: it resumes.
"""
import argparse
import contextlib
import fnmatch
import os
import re
import shutil
import stat
import struct
import sys
import time
import zipfile
import zlib
from typing import NamedTuple

if os.name == "nt":
    import msvcrt
else:
    import fcntl

__version__ = "1.2.1"

SIDECAR = ".nestzip"  # next to the archive: a copy of its last known-good central directory
SIDECAR_HEADER = struct.Struct("<8sQI")  # magic, where the central directory starts, CRC-32 of the copy
SIDECAR_MAGIC = b"nestzip\x02"
END_RECORD_SIZE = 22  # the smallest possible end of a zip file
CHUNK = 1 << 20
# Local header + central directory record + its two copies in the sidecar, with room for
# zip64 extras. Also covers tiny files NTFS keeps inside the MFT, whose deletion frees nothing.
ENTRY_OVERHEAD = 1024
SIZE_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}

EXIT_NO_SPACE = 3
EXIT_PAUSED = 130


class Item(NamedTuple):
    path: str  # on disk
    name: str  # in the archive, "/"-separated; empty folders end with "/"
    size: int


class NestzipError(Exception):
    """A problem the user has to look at. Nothing unsafe has been deleted."""


def parse_size(text):
    """'256MB', '1.5G', '0' -> a number of bytes."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d*)?)\s*([KMGT]?)B?\s*", text, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid size {text!r} (examples: 512MB, 2GB, 0)")
    number, unit = match.groups()
    return int(float(number) * SIZE_UNITS[unit.upper()])


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def duration(seconds):
    """12.4 s, 2 min 05 s, 1 h 03 min."""
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, seconds = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def progress(done, total, elapsed):
    """', 45% done, about 2 min 05 s left', or nothing before there's a rate to go by."""
    if not done or done >= total:
        return ""
    return f", {100 * done // total}% done, about {duration(elapsed * (total - done) / done)} left"


def flush_to_disk(f):
    f.flush()
    if sys.platform == "darwin":  # a plain fsync stops at the drive's cache on macOS
        fcntl.fcntl(f.fileno(), fcntl.F_FULLFSYNC)
    else:
        os.fsync(f.fileno())


def fsync_dir(path):
    if os.name != "nt":  # Windows can't open a directory; NTFS journals the rename itself
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def is_link(path):
    """A symlink, or a Windows junction, which os.path.islink() doesn't report."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    reparse_point = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    return stat.S_ISLNK(info.st_mode) or bool(reparse_point)


def is_inside(path, folder):
    """True if path is folder or anything below it. Both must already be real paths."""
    try:
        common = os.path.commonpath([path, folder])
    except ValueError:  # different drives on Windows
        return False
    return os.path.normcase(common) == os.path.normcase(folder)


def is_excluded(name, patterns):
    """True if the name in the archive, or only its last part, matches an --exclude pattern."""
    last = name.rstrip("/").rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(last, p) for p in patterns)


def scan(folder, exclude=()):
    """Walk the folder without ever going through a link.

    Returns the items to pack, what is left alone (with the reason), and every
    real folder seen, parents before children.
    """
    items, skipped, folders = [], [], []

    def unreadable(error):
        skipped.append((error.filename, f"can't be read: {error.strerror}"))

    for root, dirs, names in os.walk(folder, onerror=unreadable):
        dirs.sort()
        names.sort()
        folders.append(root)
        rel = os.path.relpath(root, folder).replace(os.sep, "/")
        prefix = "" if rel == "." else rel + "/"
        if prefix and not dirs and not names:
            if problem := name_problem(prefix):
                skipped.append((root, problem))
            else:
                items.append(Item(root, prefix, 0))

        for d in list(dirs):
            path = os.path.join(root, d)
            if is_link(path):  # links can point outside the folder: never follow them
                dirs.remove(d)
                skipped.append((path, "link"))
            elif is_excluded(prefix + d, exclude):
                dirs.remove(d)
                skipped.append((path, "excluded"))

        for n in names:
            path = os.path.join(root, n)
            if is_link(path) or not os.path.isfile(path):
                skipped.append((path, "link or special file"))
            elif is_excluded(prefix + n, exclude):
                skipped.append((path, "excluded"))
            elif problem := name_problem(prefix + n):
                skipped.append((path, problem))
            else:
                items.append(Item(path, prefix + n, os.path.getsize(path)))
    return items, skipped, folders


def name_problem(name):
    """Why a name can't go safely into a zip that any system can open, or None."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return "name isn't valid UTF-8"
    if "\\" in name:  # only possible on Linux and macOS; Windows tools read it as a folder separator
        return "name contains a backslash"
    return None


def batches(items, limit):
    batch, size = [], 0
    for item in items:
        if batch and size + item.size > limit:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item.size
    if batch:
        yield batch


def create_empty_archive(archive):
    with open(archive, "xb") as f:  # "x": never write through a link someone left at that path
        zipfile.ZipFile(f, "w").close()
        flush_to_disk(f)


def is_leftover(archive):
    """An archive with no entries and no sidecar: a first run stopped before packing anything."""
    if is_link(archive) or not os.path.isfile(archive):
        return False
    if os.path.getsize(archive) == 0:
        return True
    try:
        with zipfile.ZipFile(archive) as zf:
            return not zf.infolist()
    except zipfile.BadZipFile:
        return False


def save_sidecar(archive):
    """Copy the archive's central directory and end records to the sidecar, atomically."""
    # shortcut: start_dir is an undocumented ZipFile attribute (there since Python 2), re-check on new Pythons
    with zipfile.ZipFile(archive) as zf:
        start = zf.start_dir
    with open(archive, "rb") as f:
        f.seek(start)
        tail = f.read()
    tmp = archive + SIDECAR + ".tmp"
    with contextlib.suppress(FileNotFoundError):
        os.remove(tmp)  # left by a crash
    with open(tmp, "xb") as f:
        f.write(SIDECAR_HEADER.pack(SIDECAR_MAGIC, start, zlib.crc32(tail)) + tail)
        flush_to_disk(f)
    os.replace(tmp, archive + SIDECAR)
    fsync_dir(os.path.dirname(archive))


def restore_archive(archive):
    """Cut off a half-written batch and put back the last good central directory."""
    sidecar = archive + SIDECAR
    with open(sidecar, "rb") as f:
        data = f.read()
    tail = data[SIDECAR_HEADER.size:]
    valid = len(tail) >= END_RECORD_SIZE
    if valid:
        magic, start, crc = SIDECAR_HEADER.unpack_from(data)
        valid = magic == SIDECAR_MAGIC and zlib.crc32(tail) == crc and start <= os.path.getsize(archive)
    if not valid:  # checked before truncating: a bad offset would destroy packed files
        raise NestzipError(
            f"{sidecar} is damaged, was made by nestzip 1.0, or doesn't belong to {archive}. "
            "Nothing was changed; keep both files."
        )
    with open(archive, "r+b") as f:
        f.truncate(start)
        f.seek(start)
        f.write(tail)
        flush_to_disk(f)


def file_crc(path):
    crc = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            crc = zlib.crc32(chunk, crc)
    return crc


def verify(zf, item):
    """Read an entry back: its size must match the scanned file, its CRC-32 its own data."""
    info = zf.getinfo(item.name)
    if info.file_size != item.size:
        raise NestzipError(
            f"{item.path} changed while it was being packed ({item.size} -> {info.file_size} bytes). "
            "Close the program using it."
        )
    with zf.open(info) as entry:  # raises BadZipFile at the end if the CRC-32 doesn't match
        while entry.read(CHUNK):
            pass


def check_inside(path, folder):
    """Stop if path now resolves outside folder, the real path of the folder being packed.

    The scan never follows links, but one could be swapped in since, so this runs
    right before each file is read and right before it is deleted.
    """
    if not is_inside(os.path.realpath(path), folder):
        raise NestzipError(f"{path} now leads outside {folder}, stopping")


def remove(path, folder):
    """Delete a packed original."""
    check_inside(path, folder)
    if os.path.isdir(path):
        os.rmdir(path)
        return
    try:
        os.remove(path)
    except PermissionError:
        if os.name != "nt":
            raise
        os.chmod(path, stat.S_IWRITE)  # read-only files on Windows
        os.remove(path)


def commit(archive, batch, method, folder):
    """Append a batch, make it durable, check it, and only then delete the originals."""
    try:
        with open(archive, "r+b") as f:
            # strict_timestamps=False: files dated before 1980 (often 1970) are clamped instead of refused
            with zipfile.ZipFile(f, "a", method, allowZip64=True, strict_timestamps=False) as zf:
                for item in batch:
                    check_inside(item.path, folder)
                    zf.write(item.path, item.name)
            flush_to_disk(f)
        with zipfile.ZipFile(archive) as zf:
            for item in batch:
                verify(zf, item)
    except BaseException:
        restore_archive(archive)  # back to the last good state; no original was deleted yet
        raise
    save_sidecar(archive)
    for item in batch:
        remove(item.path, folder)


def sort_out_resumed(archive, items):
    """Split the scan into files still to pack and files packed before the interruption but not deleted."""
    todo, packed = [], []
    with zipfile.ZipFile(archive) as zf:
        entries = {info.filename: info for info in zf.infolist()}
    # Folders the last run emptied are already in the archive through their files.
    folders_in_archive = {name[: i + 1] for name in entries for i, char in enumerate(name) if char == "/"}
    for item in items:
        info = entries.get(item.name)
        if item.name in folders_in_archive:
            packed.append(item)
        elif info is None:
            todo.append(item)
        elif item.name.endswith("/") or (info.file_size == item.size and info.CRC == file_crc(item.path)):
            packed.append(item)
        else:
            sys.exit(f"nestzip: {item.name} is already in the archive with different content, fix it by hand")
    return todo, packed


def deflate_bound(size):
    """Worst case deflate output, for data that doesn't compress (zlib's deflateBound)."""
    return size + (size >> 12) + (size >> 14) + (size >> 25) + 13


def plan(items, folder, archive, limit, keep_free, method):
    """Print the space check. Returns True if the run fits."""

    def packed_size(item):
        data = item.size if method == zipfile.ZIP_STORED else deflate_bound(item.size)
        return data + ENTRY_OVERHEAD + 3 * len(item.name.encode())

    total = sum(item.size for item in items)
    overhead = sum(packed_size(item) - item.size for item in items)  # how much the archive can outgrow the originals
    largest = max((sum(packed_size(item) for item in batch) for batch in batches(items, limit)), default=0)
    out_dir = os.path.dirname(archive)
    same_disk = os.stat(folder).st_dev == os.stat(out_dir).st_dev
    need = (largest if same_disk else total) + overhead + (1 << 20) + keep_free
    free = shutil.disk_usage(out_dir).free

    print(f"{folder}: {len(items)} items, {human(total)}")
    print(f"  a normal zip needs  {human(total + overhead + keep_free)} free")
    print(f"  nestzip needs       {human(need)} free (largest batch {human(largest)}, {human(keep_free)} kept free)")
    if not same_disk:
        print("  note: the archive is on another disk, so deleting the originals frees nothing there")
    print(f"  available           {human(free)}")
    return need <= free


@contextlib.contextmanager
def single_instance(lock_path):
    """Hold an OS lock so two runs never work on the same archive. The OS drops it if we crash."""
    # O_NOFOLLOW: a link planted at that path must not make us create a file elsewhere
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise NestzipError("another nestzip is already working on this archive") from None
    try:
        yield
    finally:
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(lock_path)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="nestzip", description="Zip a folder in place: each original is deleted once it is safely in the archive."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="cmd", required=True)
    for name, summary in (("check", "show whether it fits, change nothing"), ("pack", "pack and delete the originals")):
        cmd = commands.add_parser(name, help=summary, description=summary)
        cmd.add_argument("folder")
        cmd.add_argument("-o", "--output", help="archive path (default: <folder>.zip next to the folder)")
        cmd.add_argument("--batch", type=parse_size, default="256MB", help="data packed before each delete step (default 256MB)")
        cmd.add_argument("--keep-free", type=parse_size, default="1GB", help="free space never used (default 1GB)")
        cmd.add_argument("--store", action="store_true", help="no compression (fast, for videos and photos)")
        cmd.add_argument(
            "--exclude", action="append", default=[], metavar="PATTERN",
            help='leave matching files and folders alone, e.g. "*.tmp" or "node_modules" (repeatable)',
        )
        if name == "pack":
            cmd.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    return parser.parse_args(argv)


def resolve_paths(args):
    """The folder and archive paths, refusing anything dangerous before a single file is touched."""
    folder = os.path.abspath(args.folder)
    real_folder = os.path.realpath(folder)
    if os.path.dirname(real_folder) == real_folder:
        sys.exit(f"nestzip: {args.folder} is a whole drive, refusing to pack it")
    archive = os.path.abspath(args.output or folder + ".zip")
    if not os.path.isdir(os.path.dirname(archive)):
        sys.exit(f"nestzip: the folder for {archive} doesn't exist")
    if is_inside(os.path.realpath(archive), real_folder):
        sys.exit("nestzip: the archive can't be inside the folder being packed")
    return folder, real_folder, archive


def run(args, folder, real_folder, archive):
    sidecar = archive + SIDECAR
    resuming = os.path.exists(sidecar)
    if resuming and not os.path.exists(archive):
        sys.exit(f"nestzip: found {sidecar} but the archive itself is missing")

    if not os.path.isdir(folder):
        if resuming and args.cmd == "pack":
            # Every batch is in, only the final cleanup was cut short (or the folder was moved away).
            restore_archive(archive)
            zipfile.ZipFile(archive).close()
            os.remove(sidecar)
            print(f"Done: {archive}")
            return
        hint = f" (if a previous run finished, {archive} is the result)" if os.path.exists(archive) else ""
        sys.exit(f"nestzip: {args.folder} is not a folder{hint}")

    if os.path.exists(archive) and not resuming and not is_leftover(archive):
        sys.exit(f"nestzip: {archive} already exists and isn't a nestzip in progress")

    if resuming:
        restore_archive(archive)  # check needs it too: a crash can leave a half-written batch
        print(f"Resuming {archive}")
    items, skipped, folders = scan(folder, args.exclude)
    already_packed = []
    if resuming:
        items, already_packed = sort_out_resumed(archive, items)

    method = zipfile.ZIP_STORED if args.store else zipfile.ZIP_DEFLATED
    fits = plan(items, folder, archive, args.batch, args.keep_free, method)
    for path, reason in skipped:
        print(f"  left alone ({reason}): {path}")
    if args.cmd == "check":
        sys.exit(0 if fits else EXIT_NO_SPACE)
    if not fits:
        print("nestzip: not enough free space, nothing was changed", file=sys.stderr)
        sys.exit(EXIT_NO_SPACE)
    if not args.yes:
        try:
            answer = input("Pack and delete the originals as they are saved? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "y":
            sys.exit(1)

    started = time.monotonic()
    if resuming:
        with zipfile.ZipFile(archive) as zf:
            for item in already_packed:
                if not item.name.endswith("/"):  # a folder has no data, and may only exist through its files
                    verify(zf, item)
        for item in already_packed:
            remove(item.path, real_folder)
    else:
        if os.path.exists(archive):  # an empty leftover, see is_leftover()
            os.remove(archive)
        create_empty_archive(archive)
        save_sidecar(archive)

    todo = list(batches(items, args.batch))
    total, done = sum(item.size for item in items), 0
    for i, batch in enumerate(todo, 1):
        commit(archive, batch, method, real_folder)
        size = sum(item.size for item in batch)
        done += size
        elapsed = time.monotonic() - started
        print(f"  [{i}/{len(todo)}] packed {len(batch)} items, {human(size)} freed{progress(done, total, elapsed)}")

    for path in reversed(folders):  # children before their parents
        with contextlib.suppress(OSError):
            os.rmdir(path)  # only succeeds on folders we emptied
    os.remove(sidecar)
    print(f"Done: {archive} ({human(os.path.getsize(archive))}) in {duration(time.monotonic() - started)}")


def interactive():
    """Started with no arguments, e.g. nestzip.exe double-clicked: ask for the folder instead."""
    print(f"nestzip {__version__}: zip a folder that doesn't fit twice on your disk.")
    print("Each file is deleted once it is safely in the archive.\n")
    try:
        folder = input("Drag a folder into this window, or type its path, then press Enter:\n> ")
        folder = folder.strip().strip("\"'")  # dragging a path with spaces adds quotes
        if folder:
            main(["pack", folder])
    except SystemExit as stop:
        if isinstance(stop.code, str):
            print(stop.code, file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        return
    with contextlib.suppress(KeyboardInterrupt, EOFError):
        input("\nPress Enter to close.")  # a double-clicked window would vanish before it could be read


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a file name the console can't show must not stop a run
    if argv is None and len(sys.argv) == 1 and sys.stdin and sys.stdin.isatty():
        return interactive()
    args = parse_args(argv)
    folder, real_folder, archive = resolve_paths(args)
    try:
        with single_instance(archive + SIDECAR + ".lock"):
            run(args, folder, real_folder, archive)
    except KeyboardInterrupt:
        print("\nPaused. Run the same command to continue.", file=sys.stderr)
        sys.exit(EXIT_PAUSED)
    except (OSError, zipfile.BadZipFile, NestzipError) as error:
        sys.exit(
            f"nestzip: {error}\n"
            "Stopped. Nothing that isn't safely in the archive was deleted; fix this and run the same command to continue."
        )


if __name__ == "__main__":
    main()
