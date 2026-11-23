#!/usr/bin/env python3
"""Resolve a PR review's invoking checkout and locate retained reports without changing repository state.

## Purpose

Standalone reviews collect source from the caller's repository. The collector creates their detached Git worktrees. This
module also recognizes report repositories retained by earlier releases so readers can verify their PR and caller.

## Scope

The caller's local remotes determine which GitHub repository it can review. Git administration paths must match the
checkout they identify. In-place remediation can keep reports in an ordinary directory under its own Git administration
directory, outside files visible in the working tree. Historical location receipts are read-only.

## Usage

Run review_workspace.py --checkout PATH --target URL to resolve the original caller for an admitted PR. The legacy
command name remains available to existing launchers. Use workspace_caller to authenticate historical evidence.

## Outputs

The command prints the caller's absolute checkout path. Helpers identify report locations from Git registration,
independently of identities supplied in mutable reports.

## Failure

A mismatched remote or invalid Git registration fails before a checkout is selected.

## Used by

Review entry, report discovery, cleanup replay and remediation safety checks share these caller and report identities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

# Allow installed consumers that load this module by path to use the same canonical PR grammar as run creation.
sys.pycache_prefix = str(Path(__file__).resolve())
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from create_run import PR_URL_PATTERN  # noqa: E402


def _namespace(*, platform_default: bool = False) -> Path:
    """Locate persistent per-user storage on the current host."""
    override = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not platform_default and override.is_absolute() and ".." not in override.parts:
        state = override
    elif sys.platform == "darwin":
        state = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        state = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        state = Path.home() / ".local" / "state"
    if not state.is_absolute() or ".." in state.parts:
        raise ValueError("review workspace state directory must be absolute")
    return state / "codex-rig" / "review-workspaces"


def _check_path(path: Path, namespace: Path, *, regular_file: bool = False) -> None:
    """Reject redirected components and writable or foreign-owned managed storage before following a child."""
    private_parent = False
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            return
        expected = stat.S_ISREG if regular_file and part == path else stat.S_ISDIR
        if (
            not expected(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or (regular_file and part == path and info.st_nlink > 1)
        ):
            raise ValueError("redirected or invalid review workspace path")
        if hasattr(os, "getuid") and part.is_relative_to(namespace.parent.parent):
            if info.st_uid != os.getuid() or (not private_parent and info.st_mode & 0o022):
                raise ValueError("review workspace storage must be owned by the user and private from other writers")
            # A private ancestor prevents access by other users even when the caller's umask is group-friendly.
            if stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o077:
                private_parent = True


def workspace_namespace(workspace: Path) -> Path | None:
    """Identify a managed path that requires caller and PR authentication before use."""
    platforms = {_namespace(), _namespace(platform_default=True)}
    absolute = workspace.absolute()
    for location in dict.fromkeys((absolute, absolute.resolve())):
        for ancestor in (location, *location.parents):
            if ancestor not in platforms:
                if ancestor.parts[-2:] != ("codex-rig", "review-workspaces"):
                    continue
                relative = location.relative_to(ancestor)
                if relative.parts and re.fullmatch(r"[a-f0-9]{64}", relative.parts[0]):
                    # A previous custom state root is authenticated by workspace_caller's independent reverse lookup.
                    if location != absolute:
                        raise ValueError("redirected or invalid review workspace path")
                    return ancestor
                # The suffix also occurs in ordinary source trees. Only Git's actual metadata directory can own
                # this namespace; the configured caller and target must still authenticate its direct child.
                parent = ancestor.parent.parent
                try:
                    common = _git(
                        parent, "rev-parse", "--path-format=absolute", "--git-common-dir", env=_git_environment()
                    )
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
                if Path(common) != parent.resolve(strict=True):
                    continue
            if location != absolute:
                raise ValueError("redirected or invalid review workspace path")
            return ancestor
    return None


def _require_registered_storage(caller: Path, own: Path, common: Path) -> None:
    """Permit repository-local writes only in ordinary or reciprocally registered linked-checkout metadata."""
    marker = caller / ".git"
    if marker.is_symlink() or common != common.resolve(strict=True):
        raise ValueError("invalid review workspace caller Git directory")
    if marker.is_dir():
        if own != marker or common != marker:
            raise ValueError("invalid review workspace caller Git directory")
    else:
        # Git can follow an arbitrary gitfile. A linked worktree must have a matching reverse registration.
        registration = own / "gitdir"
        if (
            own.parent != common / "worktrees"
            or own != own.resolve(strict=True)
            or registration.is_symlink()
            or (own / registration.read_text(encoding="utf-8").strip()).resolve(strict=True) != marker
        ):
            raise ValueError("invalid review workspace caller registration")


def _checkout_directories(caller: Path) -> tuple[Path, Path]:
    """Resolve the worktree's own and shared Git directories and verify how the caller points to them."""
    top, own, common = (
        Path(path)
        for path in _git(
            caller,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--absolute-git-dir",
            "--git-common-dir",
            env=_git_environment(),
        ).splitlines()
    )
    if top != caller:
        raise ValueError("checkout must identify its Git worktree root")
    marker = caller / ".git"
    if marker.is_file():
        if marker.is_symlink() or getattr(marker.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("invalid review workspace caller Git directory")
        line = marker.read_bytes().rstrip(b"\r\n")
        if not line.startswith(b"gitdir: "):
            raise ValueError("invalid review workspace caller gitfile")
        # Git resolves redirects before printing its administrative directory. Inspect each lexical component first,
        # including components before legitimate '..' in submodule gitfiles, to distinguish those layouts.
        named = caller / os.fsdecode(line.removeprefix(b"gitdir: "))
        for part in (*reversed(named.parents), named):
            info = part.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("redirected review workspace caller gitfile")
        if named.resolve(strict=True) != own:
            raise ValueError("invalid review workspace caller gitfile")
    if marker.is_dir() or own != common or common.name == ".git":
        _require_registered_storage(caller, own, common)
    return own, common


def checkout_report_root(caller: Path) -> Path | None:
    """Identify report storage with an independent reverse registration for the invoking checkout."""
    own, common = _checkout_directories(caller)
    if own == common and own != caller / ".git":
        # A separate Git directory or submodule need not identify its worktree in the reverse direction.
        return None
    root = own / "codex-rig" / "reports"
    # Linked worktree administration lives below the shared repository's worktrees directory.
    _check_path(root, common / "codex-rig" / "reports")
    return root


def report_root_caller(root: Path) -> Path | None:
    """Authenticate a managed report directory via Git's own worktree registration, independently of report data."""
    if root.parts[-2:] != ("codex-rig", "reports"):
        return None
    own = root.parent.parent
    if own.name == ".git":
        caller = own.parent
    elif own.parent.name == "worktrees" and (own / "gitdir").is_file():
        _check_path(own / "gitdir", root, regular_file=True)
        marker = Path((own / "gitdir").read_text(encoding="utf-8").strip())
        caller = (own / marker).resolve(strict=True).parent
    else:
        return None
    _check_path(root, root)
    if checkout_report_root(caller) != root:
        raise ValueError("invalid review report Git registration")
    return caller


def historical_workspace(caller: Path, target: str) -> Path | None:
    """Locate existing historical evidence through its retained caller/PR identity without creating state."""
    if not PR_URL_PATTERN.fullmatch(target):
        raise ValueError("expected a canonical GitHub PR URL")
    caller = caller.resolve()
    identity = os.path.normcase(caller.as_posix()) + "\0" + target.lower()
    name = hashlib.sha256(identity.encode("utf-8", errors="surrogateescape")).hexdigest()
    default = _namespace(platform_default=True) / name
    candidates = {_namespace() / name, default}
    local = None
    common = None
    if os.path.lexists(caller / ".git"):
        if workspace_namespace(caller) is not None:
            raise ValueError("historical review workspace must identify an original caller")
        own, common = _checkout_directories(caller)
        if own == common and own != caller / ".git":
            registry = default.parent.parent / "review-locations"
        else:
            local = common / "codex-rig" / "review-workspaces" / name
            registry = common / "codex-review-locations"
            candidates.add(local)
        # Retained receipts may point to a former XDG state directory that the current process cannot discover.
        location = registry / name / "location"
        if os.path.lexists(location):
            _check_path(location, (local or default).parent, regular_file=True)
            bound = Path(os.fsdecode(location.read_bytes()))
            if (
                not bound.is_absolute()
                or ".." in bound.parts
                or bound.parts[-3:] != ("codex-rig", "review-workspaces", name)
            ):
                raise ValueError("invalid retained review workspace location")
            if any(path != bound and os.path.lexists(path) for path in candidates):
                raise ValueError("ambiguous retained review workspaces")
            candidates = {bound}
    retained = [path for path in candidates if os.path.lexists(path)]
    if not retained:
        return None
    if len(retained) != 1:
        raise ValueError("ambiguous retained review workspaces")
    workspace = retained[0]
    if workspace != local and (workspace.is_relative_to(caller) or (common and workspace.is_relative_to(common))):
        raise ValueError("invalid retained review workspace location")
    _check_path(workspace / ".reports" / "codex" / "code-review", workspace.parent)
    return workspace


def workspace_caller(workspace: Path, target: str) -> Path | None:
    """Verify and recover the caller for a private checkout, returning None for ordinary repositories."""
    namespace = workspace_namespace(workspace)
    if namespace is None:
        return None
    workspace = workspace.absolute()
    common = workspace / ".git"
    for path in (common / "objects", common / "refs", common / "objects" / "info"):
        _check_path(path, namespace)
    for path in (common / "HEAD", common / "info" / "exclude"):
        _check_path(path, namespace, regular_file=True)
    if any(os.path.lexists(path) for path in (common / "commondir", common / "objects" / "info" / "alternates")):
        raise ValueError("review workspace may not share Git metadata or object storage")
    config = workspace / ".git" / "config"
    _check_path(config, namespace, regular_file=True)
    if workspace.parent != namespace or not config.is_file():
        raise ValueError("missing or invalid private review workspace identity")
    # Read just the file covered by the integrity verifier, without resolving includes or inheriting Git overrides.
    try:
        records = (
            _git(
                workspace,
                "config",
                "--no-includes",
                "--file",
                str(config),
                "--null",
                "--get-regexp",
                r"^codexrig\.review(caller|target)$",
                env=_git_environment(),
            )
            .removesuffix("\0")
            .split("\0")
        )
    except subprocess.CalledProcessError as error:
        raise ValueError("missing or invalid private review workspace identity") from error
    fields = dict(record.split("\n", 1) for record in records)
    caller = Path(fields.get("codexrig.reviewcaller", ""))
    if (
        len(records) != 2
        or not caller.is_absolute()
        or caller.resolve() != caller
        or fields.get("codexrig.reviewtarget") != target.lower()
        or historical_workspace(caller, target) != workspace
    ):
        raise ValueError("private review workspace identity mismatch")
    return caller


def _git_environment() -> dict[str, str]:
    """Keep the host executable environment while removing inherited repository and config overrides."""
    return {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}


def _git(directory: Path, *args: str, env: dict[str, str]) -> str:
    """Run a bounded local Git command with no shell and retain its output only upon success."""
    result = subprocess.run(  # noqa: S603 - fixed Git operations and positional argv, never a shell
        ["git", *args],  # noqa: S607 - use the host Git executable
        cwd=directory,
        env=env,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return os.fsdecode(result.stdout).rstrip("\r\n")


def caller_root(checkout: Path) -> Path:
    """Resolve the original checkout's Git top level independently of inherited repository selectors."""
    root = Path(_git(checkout, "rev-parse", "--show-toplevel", env=_git_environment())).resolve(strict=True)
    if not checkout.resolve(strict=True).is_relative_to(root):
        raise ValueError("checkout does not belong to its configured Git worktree")
    return root


def _fetch_transport(raw: str, target: str) -> str:
    """Build a credential-free URL for the exact admitted repository and the caller's Git transport."""
    target_match = PR_URL_PATTERN.fullmatch(target)
    if target_match is None:
        raise ValueError("expected a canonical GitHub PR URL")
    repository = f"{target_match['owner']}/{target_match['repository']}".lower()
    if any(ord(char) < 32 or char.isspace() for char in raw):
        raise ValueError("invalid caller fetch transport")
    raw = re.sub(r"^(?:git@)?github\.com:", "ssh://git@github.com/", raw, flags=re.IGNORECASE)
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise ValueError("invalid caller fetch transport") from None
    if (
        parsed.netloc.rsplit("@", 1)[-1].lower() != "github.com"
        or parsed.path.lower().removesuffix("/") not in {f"/{repository}", f"/{repository}.git"}
        or "?" in raw
        or "#" in raw
    ):
        raise ValueError("unsupported caller fetch transport or repository")
    # Use only the already validated target when constructing URLs. Raw userinfo never enters workspace config or logs.
    if parsed.scheme == "https":
        return f"https://github.com/{repository}.git"
    if parsed.scheme == "ssh" and parsed.username in {None, "git"} and parsed.password is None:
        return f"git@github.com:{repository}.git"
    raise ValueError("unsupported caller fetch transport")


def caller_fetch_url(caller: Path, target: str) -> str:
    """Admit the configured caller remote and return only its canonical GitHub SSH or HTTPS fetch URL."""
    if not PR_URL_PATTERN.fullmatch(target):
        raise ValueError("expected a canonical GitHub PR URL")
    selector = Path(__file__).with_name("select-git-remote.py")
    match = subprocess.run(  # noqa: S603 - installed sibling script and validated canonical target
        [sys.executable, "-B", str(selector), "--expected-url", target, "--cwd", str(caller)],
        env=_git_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if match.returncode:
        raise ValueError("no matching GitHub remote in the invoking checkout")
    selected = json.loads(match.stdout)
    if not isinstance(selected, dict) or not isinstance(selected.get("remote_url"), str):
        raise ValueError("invalid caller remote selection")
    return _fetch_transport(selected["remote_url"], target)


def prepare_workspace(checkout: Path, target: str) -> Path:
    """Resolve the admitted PR's original caller for compatibility with earlier entry commands."""
    source = caller_root(checkout)
    _, common = _checkout_directories(source)
    caller = workspace_caller(source, target) or source
    if caller == source and common.name == ".git":
        caller = workspace_caller(common.parent, target) or source
    caller_fetch_url(caller, target)
    return caller


def main() -> int:
    """Print the verified caller's existing checkout, or a diagnostic without exposing caller remote configuration."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    try:
        print(prepare_workspace(args.checkout, args.target))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"review workspace preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
