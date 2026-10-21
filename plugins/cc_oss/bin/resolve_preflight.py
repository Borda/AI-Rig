#!/usr/bin/env python
"""resolve_preflight.py — preflight checks for /oss:resolve Step 1.

Verifies tool availability (bridge@borda-ai-rig optional, gh required), authentication,
and remote state. Pulls latest if remote is ahead. Caches positive
results under ``.temp/state/preflight/`` with a 4-hour TTL so repeat
invocations short-circuit.

Output:
    files — writes CODEX_AVAILABLE and GH_OK to ${TMPDIR:-/tmp}/resolve-preflight-<KEY>-<CSID>
    stderr — human-readable status (echoed to terminal)

Exit codes:
    0 — all required checks passed (bridge absence or opt-out is non-fatal)
    1 — required check failed (gh missing, gh unauthenticated, git pull
        conflict, or other hard error)
    2 — bad/missing required argument (argparse default)

Caller pattern:
    export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
    resolve_preflight.py  # timeout: 15000
    IFS= read -r CODEX_AVAILABLE \
        < "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" 2>/dev/null \
        || CODEX_AVAILABLE="false"
    IFS= read -r GH_OK < "${TMPDIR:-/tmp}/resolve-preflight-GH_OK-${CSID}" 2>/dev/null || GH_OK="true"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from shutil import which

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_bridge
import get_plugin_install_path
from _sentinel_io import write_private_text

_PREFLIGHT_TTL = 14400  # 4 hours in seconds
#: Directory of per-check marker files recording preflight checks that already passed.
_PREFLIGHT_DIR = Path(".temp/state/preflight")


def _preflight_dir() -> Path:
    """Locate the preflight cache directory, anchored at the repository root rather than the working directory.

    Run from a subdirectory, a cwd-relative path would keep a second cache there that no other invocation reads or
    clears. The walk looks for a ``.git`` entry (directory, or file in a linked worktree) and starts no subprocess.

    Returns:
        ``<repository root>/.temp/state/preflight``, or the same path under the working directory outside a repository.

    Examples:
        >>> _preflight_dir().parts[-3:]
        ('.temp', 'state', 'preflight')
    """
    cwd = Path.cwd()
    root = next((candidate for candidate in (cwd, *cwd.parents) if (candidate / ".git").exists()), cwd)
    return root / _PREFLIGHT_DIR


def _preflight_ok(name: str, state_dir: Path | None = None) -> bool:
    """Return True if ``name`` has a valid (non-expired) preflight cache entry.

    Args:
        name: Cache key (e.g. ``"gh"``, ``"bridge"``).
        state_dir: Directory containing ``<name>.ok`` timestamp files; defaults to :func:`_preflight_dir`.

    Returns:
        ``True`` when the cache file is a regular file owned by the current user and its timestamp is within TTL.
        A timestamp in the future is invalid: it would read as a negative age and pass for ever.

    Examples:
        >>> _preflight_ok("nonexistent_key_xyz")
        False
    """
    cache_file = (state_dir or _preflight_dir()) / f"{name}.ok"
    if cache_file.is_symlink() or not cache_file.is_file():
        return False
    try:
        # the cache lives in the working tree, so a checked-out repository can ship its own marker
        if hasattr(os, "getuid") and cache_file.stat().st_uid != os.getuid():
            return False
        ts = int(cache_file.read_text(encoding="utf-8").strip())
        now = int(datetime.now(tz=timezone.utc).timestamp())
        return 0 <= (now - ts) < _PREFLIGHT_TTL
    except (ValueError, OSError):
        return False


def _preflight_pass(name: str, state_dir: Path | None = None) -> None:
    """Write a fresh preflight cache entry for ``name``.

    Args:
        name: Cache key.
        state_dir: Directory to write the ``<name>.ok`` file; defaults to :func:`_preflight_dir`.

    Examples:
        No doctest — filesystem side effects; covered by pytest.
    """
    state_dir = state_dir or _preflight_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{name}.ok").write_text(str(int(datetime.now(tz=timezone.utc).timestamp())), encoding="utf-8")


def _check_bridge() -> bool:
    """Check the optional bridge@borda-ai-rig plugin and report status on stderr.

    Cache key is "bridge", not "codex": the previous build cached "codex" to mean the
    retired Codex rescue plugin, and reusing that key would let a pre-migration entry
    answer a question about a different plugin for the rest of its TTL.

    Returns:
        ``True`` when the bridge plugin is installed and enabled (or cached as such).
    """
    codex_available = False
    if _preflight_ok("bridge"):
        codex_available = True
        print(f"{check_bridge.TARGET_SELECTOR}: ok (cached)", file=sys.stderr)
    else:
        status = check_bridge.bridge_status(Path.home(), Path("."))
        if status == "available":
            _preflight_pass("bridge")
            codex_available = True
            print(f"{check_bridge.TARGET_SELECTOR}: ok (installed and enabled)", file=sys.stderr)
        else:
            print(
                f"{check_bridge.TARGET_SELECTOR}: {status} — complex multi-file action items"
                " will be skipped; simple items implemented via foundry:sw-engineer"
                " (see Step 8 degradation)",
                file=sys.stderr,
            )
    return codex_available


def _check_gh() -> int | None:
    """Check ``gh`` presence and authentication, reporting status on stderr.

    Returns:
        ``1`` when ``gh`` is missing or unauthenticated (error already printed);
        ``None`` on success.
    """
    gh = which("gh")
    if _preflight_ok("gh"):
        print("gh: ok (cached)", file=sys.stderr)
    elif gh:
        auth_proc = subprocess.run(  # noqa: S603
            [gh, "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if auth_proc.returncode == 0:
            _preflight_pass("gh")
            all_output = auth_proc.stdout + auth_proc.stderr
            auth_line = next((ln.strip() for ln in all_output.splitlines() if "Logged in" in ln), "")
            print(f"gh: ok ({auth_line})", file=sys.stderr)
        else:
            print(
                "Pre-flight failed: gh found but not authenticated — run: gh auth login",
                file=sys.stderr,
            )
            return 1
    else:
        print("Pre-flight failed: gh not found — install: brew install gh", file=sys.stderr)
        return 1
    return None


def _sync_git_state(git: str) -> int | None:
    """Fetch origin and pull when the remote is ahead, reporting status on stderr.

    Args:
        git: Absolute path to the git binary (must be non-empty — caller only invokes
            this when ``which("git")`` resolved).

    Returns:
        ``1`` when ``git pull`` hit conflicts (error already printed); ``None`` otherwise.
    """
    remote_proc = subprocess.run(  # noqa: S603
        [git, "remote", "-v"],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    if remote_proc.stdout:
        print(remote_proc.stdout, file=sys.stderr, end="")

    # Always fetch all remotes so origin/$BASE_REF is current before Step 5 merges it.
    # Conditional fetch (gated on current branch having @{u}) left origin/$BASE_REF stale
    # when invoked from a branch with no upstream tracking ref.
    subprocess.run(  # noqa: S603
        [git, "fetch", "origin"],
        capture_output=True,
        check=False,
        timeout=30,
    )

    upstream_proc = subprocess.run(  # noqa: S603
        [git, "rev-parse", "--abbrev-ref", "@{u}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    if upstream_proc.returncode == 0 and upstream_proc.stdout.strip():
        log_proc = subprocess.run(  # noqa: S603
            [git, "log", "HEAD..@{u}", "--oneline"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
        remote_ahead = len([ln for ln in log_proc.stdout.splitlines() if ln.strip()])
        if remote_ahead > 0:
            print(f"Remote is {remote_ahead} commit(s) ahead — running git pull...", file=sys.stderr)
            pull_proc = subprocess.run(  # noqa: S603
                [git, "pull"],
                check=False,
                timeout=60,
            )
            if pull_proc.returncode == 0:
                print("✓ git pull: merged", file=sys.stderr)
            else:
                print(
                    "Pre-flight failed: git pull had conflicts — resolve manually before running /resolve",
                    file=sys.stderr,
                )
                return 1
        else:
            print("✓ git: up to date", file=sys.stderr)
    else:
        print("✓ git: fetched origin (no upstream tracking on current branch — pull skipped)", file=sys.stderr)
    return None


def _write_preflight_outputs(codex_available: bool) -> None:
    """Write CODEX_AVAILABLE and GH_OK to their TMPDIR sentinel files.

    Args:
        codex_available: Value to write to the ``CODEX_AVAILABLE`` sentinel.
    """
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    # TMPDIR counts only when absolute for this host: Windows CI inherits a POSIX-style
    # value with no native directory, and a drive-less path would resolve against
    # whatever drive the process happens to run on.
    native = PureWindowsPath if sys.platform == "win32" else PurePosixPath
    env_tmpdir = os.environ.get("TMPDIR")
    tmpdir = Path(env_tmpdir if env_tmpdir and native(env_tmpdir).is_absolute() else tempfile.gettempdir())
    write_private_text(tmpdir / f"resolve-preflight-CODEX_AVAILABLE-{csid}", f"{str(codex_available).lower()}\n")
    write_private_text(tmpdir / f"resolve-preflight-GH_OK-{csid}", "true\n")


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``resolve_preflight.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``); no flags.

    Returns:
        Exit code: 0 on success; 1 on required-check failure; argparse exits 2 on bad args.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    # Parse args before any network/git call — a bare ``-h/--help`` must print usage
    # and exit without running gh auth / git fetch / git pull.
    argparse.ArgumentParser(
        prog="resolve_preflight.py",
        description="Preflight checks (codex, gh auth, remote state) for /oss:resolve Step 1.",
    ).parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    stale = get_plugin_install_path.stale_root_warning(Path(__file__).resolve().parents[1])
    if stale:
        print(stale, file=sys.stderr)

    codex_available = _check_bridge()

    rc = _check_gh()
    if rc is not None:
        return rc

    git = which("git")
    if git:
        rc = _sync_git_state(git)
        if rc is not None:
            return rc

    _write_preflight_outputs(codex_available)
    return 0


if __name__ == "__main__":
    sys.exit(main())
