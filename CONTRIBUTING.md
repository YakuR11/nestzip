# Contributing

Thanks for wanting to help! nestzip is a small project and a learning one, so questions are welcome too.

## Before you change code

nestzip deletes files, so one rule comes first: a file is only deleted once its copy in the archive is flushed to disk and checked. Read "Is it safe?" and "How it works" in the [README](README.md) before touching `commit()`, `save_sidecar()` or `restore_archive()`.

## Workflow

1. Fork the repository and create a branch.
2. Make your change in `nestzip.py`. It stays one file, standard library only.
3. Add a test to `test_nestzip.py` for every fix or new behavior.
4. Run the tests: `python -m unittest -v`
5. Open a pull request that says what changed and why. CI runs the tests on Windows, macOS and Linux.

## Reporting bugs

Use the [bug report form](https://github.com/YakuR11/nestzip/issues/new/choose). A bug that can make nestzip delete data goes through [SECURITY.md](SECURITY.md) instead, so it can be fixed before it is public.
