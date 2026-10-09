#!/usr/bin/env python3
"""Confirm a Claude plugin sync left every plugin on the marketplace version, and name sessions it cannot reach.

Purpose:
    ``make sync-claude`` reinstalls every managed plugin, but several of its steps tolerate failure on purpose (an
    uninstall that reports "not installed", an external refresh that may be offline). Without a final check a plugin
    can stay on an older version while the sync still prints success, and a Claude Code process started before the
    install keeps the copies it loaded until it is restarted. This helper closes both gaps.
Scope:
    Read-only. ``verify`` reads ``installed_plugins.json``, ``known_marketplaces.json``, the registered marketplace
    clone's catalog and plugin manifests, and asks ``git`` for the clone's and the remote's ``HEAD``. ``sessions``
    lists processes with ``ps`` and reads their working directory with ``lsof`` or ``/proc``. Nothing is written,
    installed, or restarted.
Usage:
    ``python3 scripts/verify_claude_plugins.py verify --installed <installed_plugins.json> --known-marketplaces
    <known_marketplaces.json> --marketplace borda-ai-rig --plugins foundry oss [--report-only caveman@caveman]
    [--expect-remote <url>]`` and ``python3 scripts/verify_claude_plugins.py sessions --installed
    <installed_plugins.json>``.
Outputs:
    One ``✓``/``⚠``/``✗`` line per check on stdout. ``verify`` names each install record by scope (user, or project and
    local with their project path), the installed and the expected version, and the command that fixes a mismatch.
    ``sessions`` lists each older Claude Code process with pid, start time, and working directory, then the restart
    step.
Failure:
    ``verify`` exits 1 when any managed plugin is missing, on a different version than its marketplace declares,
    installed into a missing or orphaned directory, declared without any version, or when the marketplace clone is
    behind ``--expect-remote``. ``--report-only`` plugins get the same checks downgraded to warnings. ``sessions``
    always exits 0 and prints nothing on hosts without ``ps`` (native Windows).
Used by:
    The root ``Makefile`` targets ``verify-claude-plugins`` and ``warn-stale-claude-sessions``, both part of
    ``sync-claude``. Tests in ``tests/test_makefile_sync.py`` drive it with scratch registries and canned ``ps`` output.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

#: Seconds allowed for one local probe (``git rev-parse``, ``ps``, ``lsof``).
PROBE_TIMEOUT_SECONDS = 15
#: Seconds allowed for ``git ls-remote`` against the marketplace remote.
REMOTE_TIMEOUT_SECONDS = 60
#: Install scope ``make sync-claude`` writes; every managed plugin must have a record in it.
SYNC_SCOPE = "user"
#: ``ps -o lstart=`` prints a fixed five-field timestamp in the C locale, e.g. ``Wed Oct  8 09:07:36 2026``.
LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"
LSTART_FIELDS = 5

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class Status(str, Enum):
    """Outcome mark of one check line."""

    OK = "✓"
    WARN = "⚠"
    FAIL = "✗"


@dataclass(frozen=True)
class Finding:
    """One printed check result."""

    status: Status
    text: str

    def render(self) -> str:
        """Return the indented output line.

        Examples:
            >>> Finding(Status.OK, "foundry@m [user]: 1.0").render()
            '  ✓ foundry@m [user]: 1.0'
        """
        return f"  {self.status.value} {self.text}"

    def downgraded(self) -> Finding:
        """Return this finding with a failure turned into a warning, for report-only plugins.

        Examples:
            >>> Finding(Status.FAIL, "x").downgraded().status is Status.WARN
            True
        """
        return Finding(Status.WARN, self.text) if self.status is Status.FAIL else self


class RegistryError(Exception):
    """A registry or catalog file the check needs cannot be read."""


@dataclass(frozen=True)
class Marketplace:
    """A registered marketplace clone: where it lives, its ``HEAD``, and the version it declares per plugin."""

    root: Path
    head: str | None
    versions: dict[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessProbe:
    """Host facilities the session listing depends on, injectable so tests never inspect real processes."""

    run: Runner = subprocess.run
    platform: str = sys.platform
    which: Callable[[str], str | None] = shutil.which


@dataclass(frozen=True)
class ClaudeProcess:
    """One running Claude Code CLI process."""

    pid: int
    started: float
    cwd: str


def read_json_object(path: Path, label: str) -> dict:
    """Load one JSON object from ``path``, raising ``RegistryError`` with ``label`` when it is absent or malformed.

    Examples:
        >>> try:
        ...     read_json_object(Path("missing-registry.json"), "registry")
        ... except RegistryError as error:
        ...     print(error)
        registry not found: missing-registry.json
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RegistryError(f"{label} not found: {path}") from error
    except (OSError, ValueError) as error:
        raise RegistryError(f"{label} unreadable: {path} ({error})") from error
    if not isinstance(data, dict):
        raise RegistryError(f"{label} is not a JSON object: {path}")
    return data


def _text(value: object) -> str | None:
    """Return ``value`` when it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def manifest_version(root: Path, source: object) -> str | None:
    """Return the ``version`` of the plugin manifest under a relative-path catalog ``source``, if any.

    Examples:
        >>> manifest_version(Path("/nonexistent"), {"source": "github"}) is None
        True
    """
    if not isinstance(source, str):
        return None
    try:
        manifest = json.loads((root / source / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _text(manifest.get("version")) if isinstance(manifest, dict) else None


def declared_versions(root: Path) -> dict[str, str | None]:
    """Return the version the marketplace clone at ``root`` declares for each plugin it lists.

    Claude Code takes a plugin's version from its manifest first, then from its catalog entry; a plugin with neither
    maps to None, because no version string can then confirm the installed copy is current.
    """
    catalog = read_json_object(root / ".claude-plugin" / "marketplace.json", "marketplace catalog")
    entries = catalog.get("plugins")
    return {
        entry["name"]: manifest_version(root, entry.get("source")) or _text(entry.get("version"))
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict) and _text(entry.get("name"))
    }


def _git(args: Sequence[str], run: Runner, timeout: float) -> str | None:
    """Run one git query and return its first output field, or None when git fails or is absent."""
    try:
        result = run(["git", *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = result.stdout.split()
    return fields[0] if result.returncode == 0 and fields else None


def load_marketplace(known: dict, name: str, run: Runner) -> Marketplace:
    """Resolve one registered marketplace to its clone, ``HEAD``, and declared plugin versions."""
    entry = known.get(name)
    location = _text(entry.get("installLocation")) if isinstance(entry, dict) else None
    if location is None:
        raise RegistryError(f"marketplace {name} is not registered (no installLocation in known_marketplaces.json)")
    root = Path(location)
    head = _git(["-C", str(root), "rev-parse", "HEAD"], run, PROBE_TIMEOUT_SECONDS)
    return Marketplace(root=root, head=head, versions=declared_versions(root))


def install_records(installed: dict, plugin_id: str) -> list[dict]:
    """Return every install record of ``plugin_id``, accepting the wrapped and the bare registry layout.

    Examples:
        >>> install_records({"plugins": {"a@m": [{"scope": "user"}]}}, "a@m")
        [{'scope': 'user'}]
        >>> install_records({"plugins": {}}, "a@m")
        []
    """
    plugins = installed.get("plugins", installed)
    records = plugins.get(plugin_id) if isinstance(plugins, dict) else None
    if isinstance(records, dict):
        records = [records]
    return [record for record in records or [] if isinstance(record, dict)]


def _scope(record: dict) -> str:
    """Return a record's scope label, with the project path for project and local scopes."""
    scope = _text(record.get("scope")) or "unknown"
    project = _text(record.get("projectPath"))
    return f"{scope} {project}" if project else scope


def _fix(plugin_id: str, record: dict) -> str:
    """Return the command that brings one install record to the marketplace version."""
    scope = _text(record.get("scope")) or SYNC_SCOPE
    project = _text(record.get("projectPath"))
    if scope == SYNC_SCOPE or project is None:
        return f"claude plugin update {plugin_id}"
    return f"in {project}: claude plugin update {plugin_id} --scope {scope}"


def check_record(plugin_id: str, record: dict, expected: str, head: str | None) -> list[Finding]:
    """Check one install record against the marketplace version and the clone's ``HEAD``.

    Examples:
        >>> [f.status.value for f in check_record("a@m", {"scope": "user", "version": "1"}, "2", None)]
        ['✗']
    """
    label = f"{plugin_id} [{_scope(record)}]"
    version = _text(record.get("version")) or "unknown"
    if version != expected:
        return [
            Finding(
                Status.FAIL, f"{label}: installed {version}, marketplace has {expected} → {_fix(plugin_id, record)}"
            )
        ]
    install_path = _text(record.get("installPath"))
    if install_path is None or not Path(install_path).is_dir():
        return [Finding(Status.FAIL, f"{label}: install dir missing ({install_path}) → {_fix(plugin_id, record)}")]
    if (Path(install_path) / ".orphaned_at").exists():
        return [Finding(Status.FAIL, f"{label}: install dir is marked orphaned → {_fix(plugin_id, record)}")]
    commit = _text(record.get("gitCommitSha"))
    findings = [Finding(Status.OK, f"{label}: {version}" + (f" ({commit[:12]})" if commit else ""))]
    if commit and head and commit != head:
        findings.append(
            Finding(
                Status.WARN,
                f"{label}: recorded commit {commit[:12]} is not marketplace HEAD {head[:12]}; the version string "
                "matches, but the files may predate HEAD if the plugin changed without a version bump",
            )
        )
    return findings


def check_plugin(plugin_id: str, records: list[dict], expected: str | None, head: str | None) -> list[Finding]:
    """Check every install record of one plugin, plus the presence of the scope the sync installs.

    Examples:
        >>> check_plugin("a@m", [], "1", None)[0].text
        'a@m: not installed → claude plugin install a@m'
    """
    if not records:
        return [Finding(Status.FAIL, f"{plugin_id}: not installed → claude plugin install {plugin_id}")]
    if expected is None:
        return [
            Finding(
                Status.FAIL,
                f"{plugin_id}: the marketplace declares no version for it, so the installed copy cannot be confirmed",
            )
        ]
    findings = [finding for record in records for finding in check_record(plugin_id, record, expected, head)]
    if not any(record.get("scope") == SYNC_SCOPE for record in records):
        findings.append(
            Finding(Status.FAIL, f"{plugin_id}: no {SYNC_SCOPE}-scope install → claude plugin install {plugin_id}")
        )
    return findings


def check_freshness(marketplace: Marketplace, remote: str, run: Runner) -> Finding:
    """Compare the marketplace clone's ``HEAD`` with the remote's, so a stale clone cannot vouch for old installs."""
    remote_head = _git(["ls-remote", remote, "HEAD"], run, REMOTE_TIMEOUT_SECONDS)
    if remote_head is None or marketplace.head is None:
        missing = remote if remote_head is None else f"HEAD of {marketplace.root}"
        return Finding(Status.WARN, f"cannot read {missing}; marketplace clone freshness unchecked")
    if marketplace.head != remote_head:
        return Finding(
            Status.FAIL,
            f"marketplace clone is at {marketplace.head[:12]} but {remote} HEAD is {remote_head[:12]}; versions were "
            "checked against an older catalog → rerun make sync-claude",
        )
    return Finding(Status.OK, f"marketplace clone matches {remote} HEAD ({remote_head[:12]})")


def verify(args: argparse.Namespace, run: Runner) -> list[Finding]:
    """Run every install check; each ``--report-only`` plugin's failures come back as warnings."""
    installed = read_json_object(args.installed, "installed_plugins.json")
    known = read_json_object(args.known_marketplaces, "known_marketplaces.json")
    targets = [(f"{name}@{args.marketplace}", False) for name in args.plugins]
    targets += [(plugin_id, True) for plugin_id in args.report_only]
    marketplaces: dict[str, Marketplace | RegistryError] = {}
    findings: list[Finding] = []
    for plugin_id, report_only in targets:
        name, _, market = plugin_id.partition("@")
        if market not in marketplaces:
            try:
                marketplaces[market] = load_marketplace(known, market, run)
            except RegistryError as error:
                marketplaces[market] = error
        resolved = marketplaces[market]
        if isinstance(resolved, RegistryError):
            results = [Finding(Status.FAIL, f"{plugin_id}: {resolved}")]
        else:
            results = check_plugin(
                plugin_id, install_records(installed, plugin_id), resolved.versions.get(name), resolved.head
            )
        findings += [result.downgraded() for result in results] if report_only else results
    managed = marketplaces.get(args.marketplace)
    if args.expect_remote and isinstance(managed, Marketplace):
        findings.insert(0, check_freshness(managed, args.expect_remote, run))
    return findings


def run_verify(args: argparse.Namespace, run: Runner = subprocess.run) -> int:
    """Print every verify finding and a summary line; return 1 when any check failed."""
    try:
        findings = verify(args, run)
    except RegistryError as error:
        findings = [Finding(Status.FAIL, str(error))]
    for finding in findings:
        print(finding.render())
    failures = sum(finding.status is Status.FAIL for finding in findings)
    if failures:
        print(
            f"✗ {failures} install problem(s): a plugin may load an older version than its marketplace declares; "
            "fix the lines above, then rerun make sync-claude"
        )
        return 1
    print("✓ every installed plugin matches its marketplace version")
    return 0


def is_claude_cli(argv: Sequence[str]) -> bool:
    """Return True when a process argv is the Claude Code CLI, native binary or Node entry point.

    Examples:
        >>> is_claude_cli(["/Users/me/.local/bin/claude", "--resume"])
        True
        >>> is_claude_cli(["/Applications/Claude.app/Contents/MacOS/Claude"])
        False
        >>> is_claude_cli(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])
        True
    """
    if not argv:
        return False
    if Path(argv[0]).name == "claude":
        return True
    return len(argv) > 1 and argv[1].replace("\\", "/").endswith("claude-code/cli.js")


def parse_ps(text: str) -> list[tuple[int, float]]:
    """Parse ``ps -A -o pid= -o lstart= -o args=`` output into ``(pid, start epoch)`` of Claude Code processes.

    Examples:
        >>> claude = "  7 Wed Oct  8 09:07:36 2026 claude --resume"
        >>> editor = "  9 Wed Oct  8 09:07:36 2026 /usr/bin/vim"
        >>> rows = parse_ps(claude + "\\n" + editor + "\\n")
        >>> [pid for pid, _ in rows]
        [7]
    """
    processes = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) <= 1 + LSTART_FIELDS or not fields[0].isdigit():
            continue
        if not is_claude_cli(fields[1 + LSTART_FIELDS :]):
            continue
        try:
            started = time.mktime(time.strptime(" ".join(fields[1 : 1 + LSTART_FIELDS]), LSTART_FORMAT))
        except ValueError:
            continue
        processes.append((int(fields[0]), started))
    return processes


def process_cwd(pid: int, probe: ProcessProbe) -> str:
    """Return a process's working directory, or an empty string where the host cannot tell."""
    if probe.platform.startswith("linux"):
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return ""
    if probe.which("lsof") is None:
        return ""
    try:
        result = probe.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return next((line[1:] for line in result.stdout.splitlines() if line.startswith("n")), "")


def list_claude_processes(probe: ProcessProbe) -> list[ClaudeProcess] | None:
    """Return running Claude Code CLI processes, or None on hosts that cannot list processes."""
    if probe.platform == "win32" or probe.which("ps") is None:
        return None
    try:
        result = probe.run(
            ["ps", "-A", "-o", "pid=", "-o", "lstart=", "-o", "args="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "LC_ALL": "C"},
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [ClaudeProcess(pid, started, process_cwd(pid, probe)) for pid, started in parse_ps(result.stdout)]


def run_sessions(args: argparse.Namespace, probe: ProcessProbe | None = None) -> int:
    """Warn about Claude Code processes started before ``installed_plugins.json`` was last written; always return 0."""
    try:
        installed_at = args.installed.stat().st_mtime
    except OSError:
        return 0
    processes = list_claude_processes(probe or ProcessProbe())
    if processes is None:
        return 0
    stale = sorted((process for process in processes if process.started < installed_at), key=lambda p: p.started)
    if not stale:
        print("  ✓ no running Claude Code process predates this install")
        return 0
    print(
        f"  ⚠ {len(stale)} running Claude Code process(es) started before this install; "
        "each keeps the plugin versions it loaded until it is restarted:"
    )
    for process in stale:
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(process.started))
        print(f"    pid {process.pid} · started {started} · {process.cwd or 'cwd unknown'}")
    print("  → restart each: exit it, then run claude --resume <session-id> (or claude --continue) in its directory")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the ``verify`` or ``sessions`` subcommand and its registry paths."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify", help="compare installed plugin versions with the marketplace")
    check.add_argument("--installed", type=Path, required=True, help="installed_plugins.json")
    check.add_argument("--known-marketplaces", type=Path, required=True, help="known_marketplaces.json")
    check.add_argument("--marketplace", required=True, help="marketplace of the managed plugins")
    check.add_argument("--plugins", nargs="+", required=True, metavar="NAME", help="managed plugin names")
    check.add_argument("--report-only", nargs="*", default=[], metavar="NAME@MARKETPLACE", help="warn-only plugins")
    check.add_argument("--expect-remote", default="", metavar="URL", help="remote the marketplace clone must match")
    sessions = commands.add_parser("sessions", help="list Claude Code processes older than the last install")
    sessions.add_argument("--installed", type=Path, required=True, help="installed_plugins.json")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one subcommand and return its exit status."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    return run_verify(args) if args.command == "verify" else run_sessions(args)


if __name__ == "__main__":
    raise SystemExit(main())
