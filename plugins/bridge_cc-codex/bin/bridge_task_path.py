"""Choose a temporary task-file path and safely remove consumed scratch files.

Purpose:
    Resolve an absolute path for the Claude Write tool without constructing task text in Bash. Scope: Use TMPDIR or the
    platform temporary directory; never allocate inside the selected workspace. Usage: Run with --workspace PATH to
    print a fresh path, or --cleanup PATH to remove a matching scratch file. Outputs: Allocation prints one path without
    creating it; cleanup removes only a regular matching temporary file. Failure: Invalid session names, workspace-
    contained paths, unsafe cleanup targets and filesystem errors exit nonzero. Used by: Claude Bridge advise, implement
    and review skills; caller-supplied task files remain caller-owned.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

SESSION_PATTERN = r"[A-Za-z0-9_-]{1,128}"
SCRATCH_PATTERN = (
    rf"bridge-task-{SESSION_PATTERN}-[0-9a-f]{{8}}-[0-9a-f]{{4}}-4[0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}\.md"
)


def main(argv: list[str] | None = None) -> int:
    """Print a safe task path or delete one matching temporary scratch file."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--workspace", type=Path)
    mode.add_argument("--cleanup", type=Path)
    args = parser.parse_args(argv)
    try:
        temporary = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve(strict=True)
        if not temporary.is_dir():
            raise ValueError("temporary root must be an existing directory")
        if args.cleanup is not None:
            path = args.cleanup.absolute()
            # Resolve only the parent so a link cannot disguise the requested directory entry.
            if path.parent.resolve() != temporary or re.fullmatch(SCRATCH_PATTERN, path.name) is None:
                raise ValueError("cleanup requires a matching task file directly in the temporary directory")
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError("cleanup requires a regular task file, never a link or directory")
            path.unlink(missing_ok=True)
            return 0
        session = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
        if re.fullmatch(SESSION_PATTERN, session) is None:
            raise ValueError("session identifier must contain 1–128 ASCII letters, digits, underscores or hyphens")
        path = temporary / f"bridge-task-{session}-{uuid4()}.md"
        if path.is_relative_to(args.workspace.resolve()):
            raise ValueError("temporary task path is inside the selected workspace")
        print(path)
    except (OSError, ValueError) as error:
        print(f"bridge task path: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
