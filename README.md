# nestzip

[![tests](https://github.com/YakuR11/nestzip/actions/workflows/tests.yml/badge.svg)](https://github.com/YakuR11/nestzip/actions/workflows/tests.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Windows, macOS, Linux](https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-blue)
[![MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Zip a folder that doesn't fit twice on your disk.**

A 200 GB folder needs about 200 GB of *extra* free space to zip with 7-Zip, WinRAR or `zip`, because the originals and the archive exist at the same time until the very end. nestzip packs the folder batch by batch and deletes each original as soon as it is safely in the archive, so it only needs room for one batch (256 MB by default) plus a safety margin.

It's the reverse of [HatchZip](https://github.com/xevrion/hatchzip), which does the same trick for extracting.

```
> nestzip check Backups --batch 64MB --keep-free 100MB
C:\Users\me\Backups: 1200 items, 285.6 MB
  a normal zip needs  386.9 MB free
  nestzip needs       166.6 MB free (largest batch 64.3 MB, 100.0 MB kept free)
  available           71.8 GB
```

The result is a normal `.zip` that Windows, macOS, 7-Zip, WinRAR and `unzip` open as usual.

## Install

One file, Python 3.10 or newer, no dependencies.

```
pipx install git+https://github.com/YakuR11/nestzip
```

That gives you the `nestzip` command. Or run it straight from a clone:

```
git clone https://github.com/YakuR11/nestzip.git
cd nestzip
python nestzip.py --help
```

## Usage

```
nestzip check MyFolder                  # can it fit? changes nothing
nestzip pack MyFolder                   # -> MyFolder.zip, MyFolder is gone at the end
nestzip pack MyFolder -o D:\Archives\my.zip
nestzip pack Videos --store             # no compression, much faster for videos and photos
```

| Option | Effect |
| --- | --- |
| `-o, --output` | Archive path. Default: `<folder>.zip` next to the folder. |
| `--batch <size>` | How much is packed before each delete step. Default `256MB`. Bigger is faster, smaller needs less space. |
| `--keep-free <size>` | Free space nestzip never uses. Default `1GB`. |
| `--store` | No compression. |
| `-y, --yes` | No confirmation prompt (`pack` only). |

Sizes are written like `512MB`, `1.5G` or `0`.

Exit codes: `0` success, `1` error, `2` bad command line, `3` not enough space (nothing changed), `130` paused with Ctrl+C.

## Stop and resume

Ctrl+C, crash, power cut, disk filled by something else: run the **same command** again and it picks up where it stopped.

While a run is unfinished there is a small `MyFolder.zip.nestzip` file next to the archive. **Don't delete it**: it holds the last known-good index of the archive.

## Is it safe?

nestzip deletes your files, so it's built around one rule: a file is only deleted once its copy in the archive is on the physical disk and checked.

For each batch:

1. the files are appended to the archive,
2. the archive is flushed to the physical disk (`fsync`, `F_FULLFSYNC` on macOS),
3. every new entry is read back and its CRC-32 and size are checked,
4. the archive's index (central directory) is copied to the sidecar file, atomically, with its own checksum,
5. only then are the originals deleted.

If anything fails before step 5, the half-written batch is cut off and the last good index is put back, so no original has been deleted and the archive is valid. On resume, files that are already in the archive but weren't deleted yet are checked again (CRC-32 of the original against the archive) before they're removed. If a file was edited in between, nestzip stops and deletes nothing.

It also refuses, before touching anything:

- a run that doesn't fit in the free space,
- a whole drive (`C:\`, `/`), or an archive inside the folder being packed,
- a second nestzip working on the same archive at the same time,
- a damaged sidecar, instead of trusting it.

And it leaves these alone, listed in its output:

- **links**: symlinks, Windows junctions and other reparse points are never followed, so nothing outside the folder is ever packed or deleted, and every path is checked again right before it is read and deleted,
- devices, sockets and other special files,
- names other systems can't read back safely: invalid UTF-8, or a `\` in a Linux or macOS file name.

Empty folders are kept in the archive. Files dated before 1980, which zip can't store, get the 1980 date in the archive.

What it can't protect against: a disk that lies about `fsync`, or a file that's being written by another program while nestzip packs it (it notices a size change and stops, but close your apps first).

If the archive is on a **different disk** than the folder, deleting the originals frees nothing on the archive's disk: `check` tells you, and it then needs the full size like any other tool.

## How it works

A zip file is a list of entries followed by an index (the central directory) at the very end. Appending to a zip means writing new entries over the old index and writing a new index after them. nestzip saves a copy of the last good index (and its offset) in the sidecar after each batch. To recover, it truncates the archive at that offset and writes the saved index back: everything from the interrupted batch disappears, everything before it is intact.

## Measured

1200 files, 285.6 MB (two thirds random data, one third text), `--batch 64MB`: packed in 10 to 15 s into a 191.7 MB archive, every file byte-identical, on Windows 11 with an AMD Ryzen 9 5900HX and an NVMe SSD.

Crash safety: the process was killed (`TerminateProcess`, `SIGKILL`) at random moments until each run finished, 400 files and 5 empty folders per run. That was 463 kills over 18 runs on Windows and 549 kills over 11 runs on Linux (Python 3.10 and 3.15), and every entry came out byte-identical each time.

## Tests

```
python -m unittest -v
```

Covers a full pack, a power cut in the middle of a batch, kills while deleting and right after starting, a file edited between two runs, a disk that's too full, a damaged sidecar, two runs at once, the source folder moved away mid-run, links and junctions pointing outside the folder, links planted next to the archive, and files dated 1970. CI runs them on Windows, macOS and Linux.

## License

MIT
