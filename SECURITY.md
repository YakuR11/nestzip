# Security policy

nestzip deletes the files it packs, so a bug can mean lost data. Anything that could make it delete a file that isn't safely in the archive, or touch anything outside the folder being packed, is treated as a security issue.

## Reporting

Please report it privately through [GitHub's private vulnerability reporting](https://github.com/YakuR11/nestzip/security/advisories/new), not in a public issue. Include the nestzip version (`nestzip --version`), your operating system and filesystem, and the steps to reproduce.

## Supported versions

Only the latest release gets fixes.
