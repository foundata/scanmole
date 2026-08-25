"""Classify a `git status` snapshot for the release artifact validator.

The only pure piece of the tree-state check `scripts/release-check.sh`
runs before trusting `dist/`: whether the working tree differs from HEAD
in nothing but a plain modification to one of the three prepared README
files. Everything else -- I/O, tag lookups, artifact metadata -- stays in
the shell script's embedded Python, where it does not need this level of
scrutiny.
"""

from __future__ import annotations

ALLOWED_MODIFIED_PATHS: frozenset[str] = frozenset(
    {
        "README.md",
        "packages/scanmole/README.md",
        "packages/scanmole-gui/README.md",
    }
)
"""The only paths the tree may differ from HEAD in, and only by
modification: the READMEs the release preparation step rewrites in
place. An addition, deletion, rename or any other status on one of
these paths is refused exactly like any other unexpected change."""

_ALLOWED_MODIFICATION_CODES = frozenset({"M ", " M", "MM"})
"""Porcelain XY codes that mean "modified", staged, unstaged or both."""


def classify_tree_state(
    porcelain_z: str, allowed: frozenset[str] = ALLOWED_MODIFIED_PATHS
) -> list[str]:
    """Return one diagnostic per disallowed change in a `git status` snapshot.

    ``porcelain_z`` is the raw output of
    ``git status --porcelain=v1 -z --untracked-files=all``: NUL-terminated
    records with no path quoting, so a path containing spaces, quotes or
    non-ASCII characters parses exactly like any other. A rename or copy
    record carries a second NUL-terminated field (the original path),
    consumed here to keep subsequent records aligned; the record is
    refused unconditionally regardless of either path, since permission
    is granted only for a plain modification of an allowed path, never a
    rename onto or off of one. An untracked path is refused regardless of
    location: nothing about a path's position predicts what a build
    backend's own file-inclusion globs will later match on disk. An
    ignored path (``!!``, only ever present if the caller passed
    ``--ignored``) is skipped, never refused, so a cache directory does
    not become an error merely because it exists.

    Returns an empty list when the tree differs from HEAD in nothing but
    a modification, staged, unstaged or both, of a path in ``allowed``.
    """
    errors: list[str] = []
    fields = porcelain_z.split("\0")
    if fields and fields[-1] == "":
        fields.pop()

    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 3:
            errors.append(f"unparseable git status record: {record!r}")
            continue
        code, path = record[:2], record[3:]

        if "R" in code or "C" in code:
            # A rename/copy record's second field is the original path;
            # skip it without inspecting it, since a rename is refused
            # whatever either side names.
            if index < len(fields):
                index += 1
            errors.append(
                f"tree differs from HEAD beyond the prepared READMEs: {code} {path}"
            )
            continue

        if code == "??":
            errors.append(f"untracked file would enter the artifacts: {path}")
            continue

        if code == "!!":
            continue

        if code in _ALLOWED_MODIFICATION_CODES and path in allowed:
            continue

        errors.append(
            f"tree differs from HEAD beyond the prepared READMEs: {code} {path}"
        )

    return errors
