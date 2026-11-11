"""Serve real Git upload-pack requests locally for the SSH transport fixture."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys


def main() -> int:
    """Run the upload-pack command supplied by Git without a network connection."""
    command = shlex.split(sys.argv[-1])
    if command[0] != "git-upload-pack":
        raise ValueError("only upload-pack is supported")
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", command[-1]):
        command[-1] = command[-1][1:]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
