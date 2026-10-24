# nestzip

**Zip a folder that doesn't fit twice on your disk.**

A 200 GB folder needs about 200 GB of *extra* free space to zip with 7-Zip, WinRAR or `zip`, because the originals and the archive exist at the same time until the very end. nestzip packs the folder batch by batch and deletes each original as soon as it is safely in the archive, so it only needs room for one batch (256 MB by default).

It's the reverse of [HatchZip](https://github.com/xevrion/hatchzip), which does the same trick for extracting.

```
$ nestzip check Backups
Backups: 48210 items, 186.4 GB
  normal zip needs        187.4 GB free
  nestzip needs           1.3 GB free (largest batch 256.0 MB + 1.0 GB kept free)
  available               12.7 GB
```

The result is a normal `.zip` that Windows, macOS, 7-Zip, WinRAR and `unzip` open as usual.

## Install

One file, Python 3.8+, no dependencies.

```
git clone https://github.com/YakuR11/nestzip.git
cd nestzip
python nestzip.py --help
```

## Usage

```
python nestzip.py check MyFolder          # can it fit? changes nothing
python nestzip.py pack MyFolder           # -> MyFolder.zip, MyFolder is gone at the end
python nestzip.py pack MyFolder -o D:\Archives\my.zip
python nestzip.py pack Videos --store     # no compression, much faster for videos/photos
```

| Option | Effect |
| --- | --- |
| `-o, --output` | Archive path. Default: `<folder>.zip` next to the folder. |
| `--batch <size>` | How much is packed before each delete step. Default `256MB`. Bigger is faster, smaller needs less space. |
| `--keep-free <size>` | Free space nestzip never uses. Default `1GB`. |
| `--store` | No compression. |
| `-y, --yes` | No confirmation prompt. |

Exit codes: `0` success, `1` error, `3` not enough space (nothing changed), `130` paused with Ctrl+C.

## Stop and resume

Ctrl+C, crash, power cut, disk filled by something else: run the **same command** again and it picks up where it stopped.

While a run is unfinished there is a small `MyFolder.zip.nestzip` file next to the archive. **Don't delete it**: it holds the last known-good index of the archive.

## Is it safe?

nestzip deletes your files, so it's built around one rule: a file is only deleted once its copy in the archive is on the physical disk and checked.

For each batch:

1. the files are appended to the archive,
2. the archive is flushed to disk (`fsync`),
3. every new entry is read back and its CRC-32 and size are checked,
4. the archive's index (central directory) is copied to the sidecar file, atomically,
5. only then are the originals deleted.

If anything fails before step 5, the half-written batch is cut off and the last good index is put back, so no original has been deleted and the archive is valid. On resume, files that are already in the archive but weren't deleted yet are checked again (CRC-32 of the original against the archive) before they're removed. If a file was edited in between, nestzip stops and deletes nothing.

Also:

- the space check runs before anything is touched,
- symlinks, devices and other non-regular files are skipped and left in place,
- empty folders are kept in the archive,
- the archive can't be created inside the folder being packed.

What it can't protect against: a disk that lies about `fsync`, or a file that's being written by another program while nestzip packs it (it'll notice the size change and stop, but close your apps first).

If the archive is on a **different disk** than the folder, deleting the originals frees nothing on the archive's disk: `check` tells you, and it then needs the full size like any other tool.

## How it works

A zip file is a list of entries followed by an index (the central directory) at the very end. Appending to a zip means writing new entries over the old index and writing a new index after them. nestzip saves a copy of the last good index (and its offset) in the sidecar after each batch. To recover, it truncates the archive at that offset and writes the saved index back: everything from the interrupted batch disappears, everything before it is intact.

## Tests

```
python -m unittest test_nestzip -v
```

Covers a full pack, a power cut in the middle of a batch (archive left broken on purpose), a kill in the middle of deletions, a file edited between two runs, and a disk that's too full.

## License

MIT
