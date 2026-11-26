#!/usr/bin/env python
"""Prepare and append the gap ledger (``gaps.jsonl``) of one ``oss:review`` or ``oss:resolve`` run.

## Purpose

    Every review and resolve run records its confidence gaps in its durable run directory as gap-ledger records, so
    gaps can be counted across runs by pull request and gap identity. The record writer (the review consolidator, or
    the parent for a terminal reject and for resolve) decides the gap text; everything that is not a judgment comes
    from here: the run identity (repository, pull request, head, run id, installed plugin version, review route), the
    carry-forward of an earlier run's open gaps on an identical head, the repository's known hardware limits, and the
    filling of that identity into staged records before ``gap_ledger.py`` validates and appends them. A model never
    retypes the identity, so a record cannot disagree with the run that wrote it.

## Scope

    ``prepare`` writes ``<dir>/gap-identity.json`` (exactly ``subject`` and ``run``, the shape ``gap_ledger.py carry``
    takes), then carries the open gaps of the newest earlier ledger of the same repository and pull request in a
    sibling run directory, when ``gap_ledger.carry_forward`` finds it eligible (same head, run kind, plugin and plugin
    version). The first ``prepare`` of a run fixes its identity: a later call reuses ``gap-identity.json`` and carries
    nothing again once the ledger holds records. The complete output is saved to ``gap-prepare.txt`` for a writer
    that did not run the block. ``append`` fills ``schema``, ``schema_version``, ``subject`` and ``run`` in memory,
    then appends through ``gap_ledger.append_staged``, which validates the extended ledger and deletes the staged
    files after success. The known-limits file is ``.notes/known-limits.json`` in the main worktree
    of the reviewed repository. ``prepare`` and the staging step only read it, and only when it exists. After a
    review run's records are appended, ``append`` registers each newly observed absent hardware class of the current
    host in it through ``gap_ledger.register_known_limits``, creating the file on the first registration; resolve
    runs never write it. The repository is stored and compared case-insensitively.

    Route: ``none`` for resolve; for review ``direct`` when the run's agent-watch file lists at least one spawned
    agent, otherwise ``none`` (a terminal reject or a run that dispatched no reviewer). Claude review never lets the
    parent stand in for a reviewer, so ``parent-substitute`` is never written here.

## Usage

    python gap_emit.py prepare --kind review --dir REPORT_DIR --plugin-root ROOT --pr N --pr-url URL
        --head-file REPORT_DIR/head-sha.txt [--watch RUN_DIR/agent-watch-review.tsv] [--known-limits FILE]
    python gap_emit.py prepare --kind resolve --dir RUN_DIR --plugin-root ROOT [--pr N] [--pr-url URL] --head SHA
    python gap_emit.py append --dir DIR [--known-limits FILE]

## Outputs

    ``prepare`` prints ``GAP_LEDGER=<path>``, ``GAP_STAGE=<path>``, ``GAP_ROUTE=<route>``, one ``GAP_CARRY=`` line
    (``carried N from <run id>`` or ``fresh (<reason>)``), one ``GAP_CARRIED=<id> · <class> · <summary>`` line per
    carried gap in the saved output (a bounded preview on stdout), a ``GAP_CARRIED_LEDGER=`` path when open carried
    gaps exist, and one ``GAP_KNOWN_LIMIT=<id> · <hardware class> · <reason>`` line per listed limit. ``append``
    prints ``GAP_LEDGER=appended N → <path>``, ``GAP_LEDGER=nothing staged``, or ``GAP_LEDGER=refused`` followed by
    one validation error per line. Every problem that keeps a ledger from being written prints a ``⚠`` line.

## Failure

    ``prepare`` exits 0 on every outcome: a missing identity, an ineligible or unreadable earlier ledger, and an I/O
    failure are printed, never a stop in the run. ``append`` exits 1 only when validation refuses the staged records
    (nothing is appended and the staged file stays for one correction); every other outcome exits 0. Argument errors
    exit 2 through argparse.

## Used by

    ``skills/review/modes/agent-launch.md`` (prepare before consolidation),
    ``skills/review/modes/acceptance-gate.md`` (terminal reject), ``skills/review/templates/consolidator-prompt.md``
    (append) and ``skills/resolve/modes/final-report.md`` (prepare and append), following
    ``skills/_shared/gap-emission.md``.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import gap_ledger

#: Ledger, staged record and identity file names inside the run directory.
LEDGER_NAME = "gaps.jsonl"
IDENTITY_NAME = "gap-identity.json"
#: Saved ``prepare`` output, read by a writer that did not run the block (the review consolidator).
PREPARE_NAME = "gap-prepare.txt"
#: Known-limits file, relative to the reviewed repository's main worktree.
KNOWN_LIMITS_RELATIVE = Path(".notes") / "known-limits.json"
#: Plugin name written into every record.
PLUGIN_NAME = "oss"
#: Carried-gap lines printed at most, so a runaway ledger cannot flood the caller's context.
MAX_PRINTED = 50
_KnownLimits = tuple[str, tuple[gap_ledger.KnownLimit, ...]]
#: Pull request URL ``https://<host>/<owner>/<repo>/pull/<n>``.
_PR_URL = re.compile(r"^https://[^/\s]+/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/\d+/?$")
#: Remote URL forms: ``git@host:owner/repo(.git)``, ``ssh://git@host/owner/repo(.git)``, ``https://host/owner/repo``.
_REMOTE = re.compile(r"[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
#: Run directory names ``run-<NNN>`` sort by number, not text.
_RUN_NUMBER = re.compile(r"^run-(\d+)$")
#: Characters a run id may not carry.
_RUN_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]")

EXIT_OK = 0
EXIT_REFUSED = 1


class RunKind(str, Enum):
    """Workflow whose run directory holds the ledger."""

    REVIEW = "review"
    RESOLVE = "resolve"


@dataclass(frozen=True)
class IdentityInputs:
    """Values the caller's fixed block passes from the run's own state files.

    Attributes:
        kind: Review or resolve.
        run_dir: The run's durable directory, where the ledger lives.
        plugin_root: Installed plugin root holding ``.claude-plugin/plugin.json``.
        pr: Pull request number text, or empty for a run without one.
        pr_url: Pull request URL, or empty.
        head: Head commit, or empty when ``head_file`` holds it.
        head_file: File whose first line is the head commit, or ``None``.
        watch: Review agent-watch file, or ``None``.
    """

    kind: RunKind
    run_dir: Path
    plugin_root: Path
    pr: str = ""
    pr_url: str = ""
    head: str = ""
    head_file: Path | None = None
    watch: Path | None = None


class IdentityUnavailableError(Exception):
    """A value the identity needs is missing or malformed, so no ledger can be written for this run."""


def repo_from_url(url: str) -> str | None:
    """Return ``owner/name`` from a pull request URL or a git remote URL.

    Examples:
        >>> repo_from_url("https://github.com/octo/demo/pull/12")
        'octo/demo'
        >>> repo_from_url("git@github.com:octo/demo.git")
        'octo/demo'
        >>> repo_from_url("not a url") is None
        True
    """
    text = url.strip()
    match = _PR_URL.match(text) or (_REMOTE.search(text) if ("/" in text or ":" in text) else None)
    if match is None:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def _git(*args: str) -> str:
    """Return the stripped stdout of a read-only git command in the current directory, or empty on any failure."""
    git = shutil.which("git")
    if git is None:
        return ""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell; git resolved through shutil.which
            [git, *args], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def main_worktree_root() -> Path | None:
    """Return the main worktree of the repository at the current directory, so a linked worktree finds ``.notes``."""
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        return Path(common).parent
    top = _git("rev-parse", "--show-toplevel")
    return Path(top) if top else None


def default_known_limits(run_dir: Path) -> Path | None:
    """Locate the reviewed repository's registry, including before its first registration."""
    root = main_worktree_root()
    if root is None:
        root = next((parent.parent for parent in run_dir.resolve().parents if parent.name == ".reports"), None)
    return root / KNOWN_LIMITS_RELATIVE if root is not None else None


def plugin_version(plugin_root: Path) -> str:
    """Return the installed plugin version from ``.claude-plugin/plugin.json``.

    Raises:
        IdentityUnavailableError: The manifest is missing, unreadable or carries no version string.
    """
    manifest = plugin_root / ".claude-plugin" / "plugin.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError) as error:
        raise IdentityUnavailableError(f"plugin version unreadable from {manifest}: {error}") from error
    if not isinstance(version, str) or not version:
        raise IdentityUnavailableError(f"no version in {manifest}")
    return version


def review_route(watch: Path | None) -> str:
    """Return ``direct`` when the agent-watch file lists a spawned agent, otherwise ``none``.

    Examples:
        >>> review_route(None)
        'none'
    """
    if watch is None or not watch.is_file():
        return gap_ledger.Route.NONE.value
    try:
        rows = [line for line in watch.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError):
        return gap_ledger.Route.NONE.value
    return gap_ledger.Route.DIRECT.value if rows else gap_ledger.Route.NONE.value


def run_id_for(kind: RunKind, run_dir: Path, pr: int | None) -> str:
    """Return a run id unique within the repository: the PR and run number for review, the timestamp for resolve.

    Examples:
        >>> run_id_for(RunKind.REVIEW, Path(".reports/review/pr-12/run-003"), 12)
        'pr-12-run-003'
        >>> run_id_for(RunKind.RESOLVE, Path(".reports/resolve/2026-11-25T10-00-00Z"), 12)
        'resolve-2026-11-25T10-00-00Z'
    """
    stem = f"pr-{pr}-{run_dir.name}" if kind is RunKind.REVIEW and pr is not None else f"{kind.value}-{run_dir.name}"
    return _RUN_ID_UNSAFE.sub("-", stem)[:128]


def _head(inputs: IdentityInputs) -> str:
    """Return the head commit from the argument or the first line of the head file."""
    head = inputs.head.strip()
    if not head and inputs.head_file is not None:
        try:
            head = inputs.head_file.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, UnicodeDecodeError, IndexError):
            head = ""
    return head


def build_identity(inputs: IdentityInputs) -> dict[str, Any]:
    """Return the run identity ``{"subject": …, "run": …}`` from the caller's state values.

    Raises:
        IdentityUnavailableError: Repository, head or plugin version cannot be established.
    """
    pr = int(inputs.pr) if re.fullmatch(r"[1-9]\d*", inputs.pr.strip()) else None
    repo = repo_from_url(inputs.pr_url) if inputs.pr_url.strip() else None
    repo = gap_ledger.repo_key(repo or repo_from_url(_git("remote", "get-url", "origin")))
    if repo is None:
        raise IdentityUnavailableError("repository unknown (no PR URL and no origin remote)")
    head = _head(inputs)
    if not re.fullmatch(r"[0-9a-f]{7,64}", head):
        raise IdentityUnavailableError(f"head commit unavailable or malformed: {head!r}")
    route = gap_ledger.Route.NONE.value if inputs.kind is RunKind.RESOLVE else review_route(inputs.watch)
    identity = {
        "subject": {"repo": repo, "pr": pr, "head": head},
        "run": {
            "run_id": run_id_for(inputs.kind, inputs.run_dir, pr),
            "kind": inputs.kind.value,
            "harness": gap_ledger.Harness.CLAUDE.value,
            "plugin": PLUGIN_NAME,
            "plugin_version": plugin_version(inputs.plugin_root),
            "route": route,
        },
    }
    errors = gap_ledger.identity_errors(identity)
    if errors:
        raise IdentityUnavailableError("; ".join(errors))
    return identity


def _sibling_order(path: Path) -> tuple[int, int, str]:
    """Sort key putting ``run-<N>`` directories in numeric order and other names (timestamps) in text order."""
    match = _RUN_NUMBER.match(path.name)
    return (0, int(match.group(1)), "") if match else (1, 0, path.name)


def prior_ledger(run_dir: Path, subject: dict[str, Any]) -> tuple[gap_ledger.Ledger | None, list[str]]:
    """Find the newest matching earlier ledger and report the first unusable ledger encountered.

    Only directories that sort before this run are considered; head, kind and plugin version are left to
    ``gap_ledger.carry_forward`` so the caller can name why a carry was not eligible.
    """
    parent, own = run_dir.parent, _sibling_order(run_dir)
    try:
        siblings = sorted(
            (path for path in parent.iterdir() if path.is_dir() and not path.is_symlink()), key=_sibling_order
        )
    except OSError as error:
        return None, [f"⚠ GAP_CARRY=prior runs unreadable ({error})"]
    warnings: list[str] = []
    for candidate in reversed(siblings):
        path = candidate / LEDGER_NAME
        if _sibling_order(candidate) >= own or not (path.is_file() or path.is_symlink()):
            continue
        try:
            ledger = gap_ledger.load_ledger(path)
        except gap_ledger.LedgerError as error:
            warnings = warnings or [f"⚠ GAP_CARRY=earlier ledger unreadable ({str(error)[:200]})"]
            continue
        if ledger.errors:
            warnings = warnings or [f"⚠ GAP_CARRY=earlier ledger refused ({ledger.errors[0][:200]})"]
        first = next(iter(ledger.records), {})
        prior_subject = first.get("subject")
        if isinstance(prior_subject, dict):
            same_repo = gap_ledger.repo_key(prior_subject.get("repo")) == gap_ledger.repo_key(subject["repo"])
            if same_repo and prior_subject.get("pr") == subject["pr"]:
                return ledger, warnings
    return None, warnings


def _write_identity(path: Path, identity: dict[str, Any]) -> None:
    """Write the identity file, refusing a symlinked destination."""
    if path.is_symlink():
        raise gap_ledger.LedgerError(f"refusing symlinked identity file: {path}")
    path.write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")


def _carry(run_dir: Path, identity: dict[str, Any], ledger: Path, known: _KnownLimits | None) -> list[str]:
    """Carry the earlier run's open gaps and return the ``GAP_CARRY``/``GAP_CARRIED`` lines to print."""
    prior, warnings = prior_ledger(run_dir, identity["subject"])
    if prior is None:
        return [*warnings, "GAP_CARRY=fresh (no earlier ledger for this pull request)"]
    if prior.errors:
        return [*warnings, "GAP_CARRY=fresh (matching earlier ledger refused)"]
    try:
        carried = gap_ledger.carry_forward(prior.path, identity, ledger, known_limits=known)
    except gap_ledger.CarryIneligibleError as error:
        return [*warnings, f"GAP_CARRY=fresh ({error})"]
    except gap_ledger.LedgerInvalidError as error:
        return [*warnings, f"⚠ GAP_CARRY=fresh (earlier ledger refused: {str(error)[:200]})"]
    prior_run = prior.records[0]["run"]["run_id"]
    inventory = _carried_lines(gap_ledger.load_ledger(ledger)) if carried else []
    return [*warnings, f"GAP_CARRY=carried {carried} from {prior_run}", *inventory]


def _load_limits(limits: Path | None, repo: str) -> tuple[_KnownLimits | None, list[str]]:
    """Read the applicable registry once and return its observations or a warning."""
    if limits is None or not (limits.exists() or limits.is_symlink()):
        return None, []
    try:
        listed_repo, listed = gap_ledger.load_known_limits(limits)
    except (gap_ledger.LedgerError, gap_ledger.LedgerInvalidError, OSError) as error:
        return None, [f"⚠ known-limits file ignored ({limits}): {str(error)[:200]}"]
    if gap_ledger.repo_key(listed_repo) != gap_ledger.repo_key(repo):
        return None, [f"⚠ known-limits file ignored ({limits}): repository {listed_repo} differs from {repo}"]
    return (listed_repo, tuple(listed.values())), []


def _carried_lines(ledger: gap_ledger.Ledger) -> list[str]:
    """Expose the ledger path and every open carried record of this run."""
    if ledger.errors:
        raise gap_ledger.LedgerInvalidError(ledger.errors[0])
    records = gap_ledger.latest_by_id(ledger.records).values()
    carried = [
        record
        for record in records
        if record.get("carried_from") and record.get("status", gap_ledger.Status.OPEN.value) == "open"
    ]
    if not carried:
        return []
    return [
        f"GAP_CARRIED_LEDGER={ledger.path}",
        *(f"GAP_CARRIED={item['id']} · {item['class']} · {item['summary']}" for item in carried),
    ]


def _existing_identity(run_dir: Path) -> dict[str, Any] | None:
    """Read the frozen identity, refusing an invalid file without replacing it."""
    path = run_dir / IDENTITY_NAME
    if path.is_symlink():
        raise IdentityUnavailableError(f"refusing symlinked identity file: {path}")
    if not path.exists():
        return None
    try:
        if not path.is_file() or path.stat().st_size > gap_ledger.MAX_FILE_BYTES:
            raise IdentityUnavailableError(f"identity file is not a bounded regular file: {path}")
        identity = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        raise IdentityUnavailableError(f"invalid identity file {path}: {error}") from error
    errors = gap_ledger.identity_errors(identity)
    if errors:
        raise IdentityUnavailableError(f"invalid identity: {errors[0]}")
    return identity


def _prepare_lines(inputs: IdentityInputs, limits: Path | None) -> list[str]:
    """Write or reuse the run identity, carry once, and return the lines describing the run's ledger."""
    ledger = inputs.run_dir / LEDGER_NAME
    lines = [f"GAP_LEDGER={ledger}", f"GAP_STAGE={ledger}.rec"]
    identity = _existing_identity(inputs.run_dir)
    if identity is None:
        identity = build_identity(inputs)
        _write_identity(inputs.run_dir / IDENTITY_NAME, identity)
    lines.append(f"GAP_ROUTE={identity['run']['route']}")
    known, warnings = _load_limits(limits, identity["subject"]["repo"])
    current = gap_ledger.load_ledger(ledger)
    if current.errors:
        raise gap_ledger.LedgerInvalidError(current.errors[0])
    if current.records:
        lines += ["GAP_CARRY=done earlier in this run", *_carried_lines(current)]
    else:
        lines += _carry(inputs.run_dir, identity, ledger, known)
    limit_lines = (
        [f"GAP_KNOWN_LIMIT={item.id} · {item.hardware_class} · {item.reason}" for item in known[1]] if known else []
    )
    return lines + limit_lines + warnings


def prepare(inputs: IdentityInputs, limits_arg: Path | None) -> list[str]:
    """Write the run identity and complete carry inventory, then return a bounded preview.

    The identity is fixed by the first ``prepare`` of a run: a later call (a terminal reject after the fan-out) reuses
    it, so every record of the run keeps one route. The full ``gap-prepare.txt`` remains available to the writer.
    """
    try:
        limits = limits_arg if limits_arg is not None else default_known_limits(inputs.run_dir)
        lines = _prepare_lines(inputs, limits)
    except (IdentityUnavailableError, gap_ledger.LedgerInvalidError) as error:
        lines = [f"⚠ GAP_LEDGER=unavailable ({error}) — this run records no gap ledger"]
    except (gap_ledger.LedgerError, OSError, KeyError, TypeError) as error:
        lines = [f"⚠ GAP_LEDGER=unavailable (I/O: {str(error)[:200]}) — this run records no gap ledger"]
    saved = inputs.run_dir / PREPARE_NAME
    try:
        if not saved.is_symlink():
            saved.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    except OSError as error:
        lines.append(f"⚠ {PREPARE_NAME} not written: {error}")
    preview = []
    carried = 0
    for line in lines:
        if line.startswith("GAP_CARRIED="):
            carried += 1
            if carried > MAX_PRINTED:
                continue
        preview.append(line)
    return preview


def _fill(
    record: dict[str, Any], identity: dict[str, Any], limits: tuple[gap_ledger.KnownLimit, ...]
) -> dict[str, Any]:
    """Attach the run identity and normalize the staged gap using the canonical table.

    Examples:
        >>> _fill({"id": "x"}, {"subject": {"repo": "o/r"}, "run": {"run_id": "r"}}, ())["schema"]
        'gap-ledger'
    """
    defaults = {
        "schema": gap_ledger.SCHEMA,
        "schema_version": gap_ledger.SCHEMA_VERSION,
        "subject": identity["subject"],
        "run": identity["run"],
    }
    filled = {**record, **defaults}
    gap_id = filled.get("id")
    hardware = filled.get("hardware_class")
    if (
        filled.get("class") == "hardware-absent"
        and isinstance(hardware, str)
        and hardware in {item.value for item in gap_ledger.HardwareClass}
    ):
        gap_id = gap_ledger.gap_id("<class>-absent", hardware)
        filled["host_class"] = gap_ledger.host_class_fields(gap_ledger.current_host_class())
    canonical = gap_ledger.lookup_gap_id(gap_id) if isinstance(gap_id, str) else None
    valid_classes = {item.value for item in gap_ledger.GapClass}
    valid_owners = {item.value for item in gap_ledger.Owner}
    if (
        canonical
        and filled.get("class", canonical[0]) in tuple(valid_classes)
        and filled.get("owner", canonical[1]) in tuple(valid_owners)
    ):
        filled["id"] = gap_id
        filled["class"], filled["owner"] = canonical
    if isinstance(filled.get("known_limit"), bool):
        filled.pop("known_limit")
    return gap_ledger.apply_known_limit(filled, limits)


def append(run_dir: Path, limits_arg: Path | None) -> tuple[int, list[str]]:
    """Normalize records in memory and append them while preserving rejected stages byte for byte."""
    ledger = run_dir / LEDGER_NAME
    staged = gap_ledger.staged_files(ledger)
    if not staged:
        return EXIT_OK, ["GAP_LEDGER=nothing staged — no new gaps this run"]
    try:
        identity = _existing_identity(run_dir)
        if identity is None:
            raise IdentityUnavailableError(f"missing identity file: {run_dir / IDENTITY_NAME}")
        path = limits_arg if limits_arg is not None else default_known_limits(run_dir)
        repo = identity["subject"]["repo"]
        known, warnings = _load_limits(path, repo)
        limits = known[1] if known else ()
        records: list[dict[str, Any]] = []

        def fill(record: dict[str, Any]) -> dict[str, Any]:
            """Remember the normalized rows for registration after a successful append."""
            filled = _fill(record, identity, limits)
            records.append(filled)
            return filled

        appended, errors = gap_ledger.append_staged(ledger, transform=fill, known_limits=known)
    except (IdentityUnavailableError, OSError, ValueError, gap_ledger.LedgerError) as error:
        return EXIT_OK, [f"⚠ GAP_LEDGER=unavailable ({str(error)[:200]}) — staged records kept at {staged[0]}"]
    if errors:
        return EXIT_REFUSED, ["GAP_LEDGER=refused — correct the staged records and run this block once more", *errors]
    lines = [f"GAP_LEDGER=appended {appended} → {ledger}", *warnings]
    if path is not None and identity["run"]["kind"] == RunKind.REVIEW.value:
        registered, error = gap_ledger.register_known_limits(path, repo, records)
        lines.extend(f"GAP_KNOWN_LIMIT_REGISTERED={gap_id}" for gap_id in registered)
        if error:
            lines.append(f"⚠ GAP_KNOWN_LIMIT_REGISTRATION=skipped ({error[:200]})")
    return EXIT_OK, lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and append the gap ledger of an oss review or resolve run.")
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--kind", choices=[kind.value for kind in RunKind], required=True)
    prep.add_argument("--dir", dest="run_dir", type=Path, required=True)
    prep.add_argument("--plugin-root", type=Path, required=True)
    prep.add_argument("--pr", default="")
    prep.add_argument("--pr-url", default="")
    prep.add_argument("--head", default="")
    prep.add_argument("--head-file", type=Path, default=None)
    prep.add_argument("--watch", type=Path, default=None)
    prep.add_argument("--known-limits", type=Path, default=None)
    app = commands.add_parser("append")
    app.add_argument("--dir", dest="run_dir", type=Path, required=True)
    app.add_argument("--known-limits", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; see the module docstring for subcommands, outputs and exit codes."""
    args = _build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        # ⚠, · and → must print on a non-UTF-8 console (native Windows)
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    # an empty sentinel arrives as "" and would point at the current directory
    if not args.run_dir.name or not args.run_dir.is_dir():
        print(f"⚠ GAP_LEDGER=unavailable (run directory missing: {args.run_dir}) — this run records no gap ledger")
        return EXIT_OK
    if args.command == "prepare":
        inputs = IdentityInputs(
            kind=RunKind(args.kind),
            run_dir=args.run_dir,
            plugin_root=args.plugin_root,
            pr=args.pr,
            pr_url=args.pr_url,
            head=args.head,
            head_file=args.head_file,
            watch=args.watch,
        )
        print("\n".join(prepare(inputs, args.known_limits)))
        return EXIT_OK
    code, lines = append(args.run_dir, args.known_limits)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
