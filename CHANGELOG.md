# Changelog

Versions follow [semantic versioning](https://semver.org).

## 1.2.0 - 2026-10-24

- **Windows**: every release now has a standalone `nestzip.exe`, no Python needed. It is built by GitHub Actions from the tagged commit, smoke-tested, and comes with a build attestation: `gh attestation verify nestzip.exe --repo YakuR11/nestzip`.
- **Progress**: each batch shows how much is done and about how long is left, and the end shows the total time.
- **`--exclude PATTERN`** leaves matching files and folders alone: not packed, not deleted. Repeatable, for example `--exclude "*.tmp" --exclude node_modules`.

## 1.1.0 - 2026-10-24

- Symlinks and Windows junctions are never followed: nothing outside the folder is ever packed or deleted.
- The sidecar file is checksummed, two runs on the same archive are refused, and whole drives are refused.
- Files dated before 1980 no longer stop a run.
- `pack` exits with code 3 when space is short, and errors are one clear line instead of a traceback.
- `--store` works with `check`, and sizes like `1.5G` are accepted.
- Tests on Windows, macOS and Linux, and crash tests with real kills.

## 1.0.0 - 2026-10-24

First version: packs a folder batch by batch, deleting originals once they are flushed to disk and checked, and resumes after a crash.
