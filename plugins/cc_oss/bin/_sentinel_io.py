#!/usr/bin/env python
"""_sentinel_io.py — create session sentinel files privately, never writing through a planted file or symlink.

Private module (leading underscore): sibling ``bin/`` scripts import it; it has no command line, and
``check_orphaned_bin.py`` skips it like every underscore module.

The skills carry state across Bash calls in ``${TMPDIR:-/tmp}/<name>-${CSID}`` files, and later steps act on what they
read back: a branch to push, a report path, a review mode, a judge verdict. ``TMPDIR`` may be the machine-wide ``/tmp``
and the names are predictable, so a plain ``Path.write_text`` follows a symlink another local user planted at that name,
or writes into a file that user owns and can rewrite before the reader runs. ``write_private_text`` removes whatever
sits at the path, then creates the file with ``O_CREAT | O_EXCL | O_NOFOLLOW`` and mode ``0600``. In a sticky ``/tmp``
another user's entry cannot be removed, so the exclusive create fails and the caller stops rather than publish a value
someone else controls.

Byte-identical copies ship in each plugin that writes sentinels (``propagate_shared.py`` MANIFEST). Consumers — oss:
build_analyse_paths.py, detect_codemap.py, detect_thread_type.py, parse_analyse_args.py, parse_release_envelopes.py,
resolve_pr_refs.py, resolve_preflight.py; research: check_output_within_root.py, codemap-flag.py, find_judge_verdict.py,
fortify_next_variant.py, parse_kaggle_args.py.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Exclusive create that refuses a symlink at the final path component. ``O_BINARY`` (Windows only) stops the C runtime
#: from turning each LF into CRLF: a stray carriage return would end up inside the shell variable a reader fills.
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
#: Owner read/write only; Windows ignores everything but the write bit.
_PRIVATE_MODE = 0o600


def write_private_text(path: Path, text: str) -> None:
    """Replace ``path`` with a fresh file only this user can read, holding ``text``.

    Args:
        path: Sentinel file; an existing file or symlink at this path is removed first, never written through.
        text: Content, written as UTF-8 with LF line endings on every OS.

    Raises:
        PermissionError: The entry at ``path`` cannot be removed — owned by another user in a sticky directory, or a
            directory.
        FileExistsError: Another process created ``path`` between the removal and the exclusive create.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     target = Path(d) / "sentinel-doctest"
        ...     write_private_text(target, "first\\n")
        ...     write_private_text(target, "second\\n")
        ...     target.read_bytes()
        b'second\\n'
    """
    try:
        path.unlink(missing_ok=True)
    except (PermissionError, IsADirectoryError) as exc:  # a directory: EPERM on macOS, EISDIR on Linux
        raise PermissionError(
            f"refusing to write sentinel {path}: the existing entry cannot be replaced (owned by another user, or a "
            "directory) — remove it, or point TMPDIR at a directory only you can write"
        ) from exc
    with os.fdopen(os.open(path, _CREATE_FLAGS, _PRIVATE_MODE), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
