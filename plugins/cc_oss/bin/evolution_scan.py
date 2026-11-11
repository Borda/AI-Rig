#!/usr/bin/env python
"""evolution_scan.py — run the ``/oss:analyse evolution`` pipeline against a durable per-repository store.

Ties the evolution modules together: target resolution and the dedicated clone (``evolution_releases``), changelog
linkage (``evolution_changelog``), SZZ blame evidence (``evolution_blame``), the store lock and write protocol
(``evolution_store``), matrix scoring (``evolution_score``) and the report (``evolution_render``). The skill mode file
``skills/analyse/modes/evolution.md`` drives it.

Subcommands:

``scope --args "<CLEAN_ARGS>" --project-root DIR [--break-lock false|true|confirmed]``
    Resolve the target and its store root, sync the dedicated clone under the store lock (only when the target is not
    the current checkout), index every release (read-only) and decide whether the user must choose the scan scope.
    Prints ``KEY=value`` lines (``EVO_ROOT``, ``RELEASES``, ``FIRST``, ``LAST``, ``PRERELEASES``, ``EST_FIXES``,
    ``EST_COST``, ``SAVED_SCOPE``, ``NEWER``, ``ASK_SCOPE``, ``SCOPE_FILE``) and writes the ``analyse-evo-root`` and
    ``analyse-evo-scope`` session sentinels.

``run --root DIR --project-root DIR --today YYYY-MM-DD [--full B] [--adjudicate B] [--bug-labels "a,b"]``
    Take the store lock and run the pipeline: resume unfinished fixes, extend to new releases, recompute records whose
    fingerprints changed, apply overrides and stored LLM verdicts, write the store, then render the report into
    ``.reports/analyse/evolution/<host>-<owner>-<repo>-<today>/``. Prints ``REPORT_FILE``, ``HTML_FILE``,
    ``ADJ_PENDING`` and ``ADJ_REQUEST``, and rewrites the ``analyse-report-file`` sentinel.

``ingest --root DIR --project-root DIR --today YYYY-MM-DD``
    Validate LLM verdicts from ``adjudication-verdicts.json`` against each request's candidate set, store them by
    key, then run again (no new blame; the stored verdicts are reused by key).

``render-request --spec FILE --out FILE``
    Write an ``adjudication-request.json``-shaped list from fix specs (``{fix_id, fix_entry, candidates, evidence}``)
    with the production prompt and key, without a store, clone or lock. Prints ``ITEMS`` and ``ADJ_REQUEST``. Used by
    ``/foundry:calibrate`` fixtures (requires the ``foundry`` plugin) and for replaying a request by hand.

Scope values (sentinel or the ``scope-choice.txt`` answer): ``all``, ``last:<N>``, ``<v1>-><v2>``, ``extend`` (saved
scope extended to the newest release) or ``saved`` (saved scope unchanged).

Exit codes: 0 success; 1 unexpected failure; 2 blocked (a ``! BLOCKED`` line names the reason: lock held, shallow
clone, no release tags, store identity mismatch, unsafe or tracked store, scope missing or invalid).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from shutil import which
from typing import Any, Final

from _evolution_common import (
    ALGO_VERSIONS,
    DEFAULT_BUG_LABELS,
    REAL_KINDS,
    SCHEMA_VERSION,
    EvolutionError,
    StorePaths,
    atomic_write_json,
    canonical_json,
    column_kind,
    parse_version,
    resolve_store_root,
    run_git,
)
from _sentinel_io import write_private_text
from evolution_blame import collect_evidence, evidence_fingerprint
from evolution_changelog import build_entries, discover_changelog, parse_changelog
from evolution_releases import build_release_index, fetch_open_bug_series, is_shallow, resolve_target, sync_clone
from evolution_render import render_report
from evolution_score import (
    DEFAULT_THRESHOLD,
    DEFAULT_WEIGHTS,
    aggregate,
    blame_only_on_pseudo,
    build_columns,
    build_sha_map,
    pseudo_columns,
    score_rows,
)
from evolution_store import (
    LockHeld,
    StoreTrackedError,
    StoreVersionError,
    VerdictError,
    acquire_lock,
    adjudication_key,
    append_adjudication,
    append_fix_result,
    break_lock,
    build_prompt,
    load_adjudications,
    load_store,
    paths_from_root,
    plan_work,
    read_manifest,
    read_overrides,
    release_lock,
    reset_store,
    validate_verdict,
    write_manifest,
    write_matrix_csv,
)

#: Exit code for a run that stopped on a ``! BLOCKED`` condition.
EXIT_BLOCKED: Final = 2

#: Maximum LLM adjudications requested per run; ambiguous rows beyond it land in ``unadjudicated`` and are retried.
ADJ_CAP: Final = 50

#: Model and effort recorded in every adjudication key (the mode file spawns this tier).
ADJ_MODEL: Final = "sonnet"
ADJ_EFFORT: Final = "default"

#: Candidate columns shown to the adjudicator per fix.
ADJ_CANDIDATES: Final = 5

#: Total blame budget per run and per fix, in seconds; fixes past it are ``timed_out`` and retried by the next run.
RUN_BLAME_BUDGET_S: Final = 2400.0
FIX_BLAME_BUDGET_S: Final = 90.0

#: Seconds per fix used for the scope prompt's cost estimate before any blame has been measured.
EST_SECONDS_PER_FIX: Final = 1.5

#: Store-root files outside ``store/`` that this script owns.
TARGET_FILE: Final = "target.json"
SCOPE_FILE: Final = "scope-choice.txt"
ADJ_REQUEST_FILE: Final = "adjudication-request.json"
ADJ_VERDICTS_FILE: Final = "adjudication-verdicts.json"
#: Optional tuned weights from the Phase 0 evaluation: ``{"weights": {...}, "threshold": x, "evaluated": true}``.
WEIGHTS_FILE: Final = "weights.json"


class Blocked(EvolutionError):
    """A condition the user must resolve; printed as ``!

    BLOCKED — <reason>`` with exit code 2.
    """

    def __init__(self, reason: str, **keys: str) -> None:
        """Store the reason and extra ``KEY=value`` lines to print before it."""
        super().__init__(reason)
        self.keys = keys


# --------------------------------------------------------------------------------------------------------------------
# Session sentinels and output
# --------------------------------------------------------------------------------------------------------------------


def _sentinel(name: str) -> Path:
    """Return the per-session sentinel path ``${TMPDIR:-/tmp}/<name>-<CSID>``."""
    base = os.environ.get("TMPDIR") or (tempfile.gettempdir() if os.name == "nt" else "/tmp")  # noqa: S108
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    return Path(base) / f"{name}-{csid}"


def _read_sentinel(name: str) -> str:
    """Return the first line of a session sentinel, or an empty string."""
    try:
        lines = _sentinel(name).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return lines[0].strip() if lines else ""


def _emit(key: str, value: Any) -> None:
    """Print one single-line ``KEY=value`` result for the skill."""
    text = " ".join(str(value).split())
    print(f"{key}={text}")


# --------------------------------------------------------------------------------------------------------------------
# Arguments and scope
# --------------------------------------------------------------------------------------------------------------------


def parse_scan_args(clean_args: str) -> tuple[str | None, str | None]:
    """Split ``evolution [target] [v1->v2]`` into the target and the explicit range.

    Examples:
        >>> parse_scan_args("evolution owner/repo v1.0->v2.0")
        ('owner/repo', 'v1.0->v2.0')
        >>> parse_scan_args("evolution")
        (None, None)
    """
    tokens = clean_args.split()
    if tokens and tokens[0] == "evolution":
        tokens = tokens[1:]
    rng = next((tok for tok in tokens if "->" in tok), None)
    rest = [tok for tok in tokens if tok != rng]
    return (rest[0] if rest else None), rng


def _stable(index: dict[str, Any]) -> list[dict[str, Any]]:
    """Return stable releases in chronological order."""
    return [rel for rel in index["releases"] if not rel["is_prerelease"]]


def _canon(text: str) -> str | None:
    """Return the canonical version of a tag text, or None."""
    version = parse_version(text.strip())
    return version.canonical if version else None


def resolve_scope(value: str, saved: dict[str, Any] | None, index: dict[str, Any]) -> dict[str, Any]:
    """Turn a scope value into ``{"kind", "from", "to"}`` (``to`` None = newest release, fixes on HEAD included).

    Raises:
        Blocked: the value is empty, unknown, or names versions the index does not hold.
    """
    stable = _stable(index)
    if not stable:
        raise Blocked("no stable release tag to scope")
    known = {rel["version"] for rel in index["releases"]}
    value = value.strip()
    if value in ("saved", "extend") and saved:
        return {
            "kind": saved.get("kind", "range"),
            "from": saved["from"],
            "to": None if value == "extend" else saved.get("to"),
        }
    if value == "all":
        return {"kind": "all", "from": stable[0]["version"], "to": None}
    if value.startswith("last:") and value[5:].isdigit() and 1 <= int(value[5:]) <= len(stable):
        return {"kind": "last", "from": stable[-int(value[5:])]["version"], "to": None}
    if "->" in value:
        lo, hi = (_canon(part) for part in value.split("->", 1))
        order = {rel["version"]: rel["order"] for rel in index["releases"]}
        if lo in known and hi in known and order[lo] <= order[hi]:
            return {"kind": "range", "from": lo, "to": hi}
    raise Blocked(f"scope '{value}' is not usable; pass a range such as v1.0.0->v2.0.0 or choose all / last N")


def _in_scope(order: dict[str, int], release: str, scope: dict[str, Any]) -> bool:
    """Return True when a fix release (or ``unreleased``) falls inside the scope."""
    if release == "unreleased":
        return scope["to"] is None
    if release not in order:
        return False
    hi = order[scope["to"]] if scope["to"] else max(order.values())
    return order[scope["from"]] <= order[release] <= hi


def _fold_prereleases(fixes: list[dict[str, Any]], index: dict[str, Any]) -> None:
    """Attribute pre-release fixes to the next stable release on the same line (IDs unchanged)."""
    by_version = {rel["version"]: rel for rel in index["releases"]}
    stable = _stable(index)
    for fix in fixes:
        rel = by_version.get(fix["release"])
        if rel is None or not rel["is_prerelease"]:
            continue
        nxt = next((s for s in stable if s["order"] > rel["order"] and s["line"] == rel["line"]), None)
        nxt = nxt or next((s for s in stable if s["order"] > rel["order"]), None)
        fix["release"] = nxt["version"] if nxt else "unreleased"


# --------------------------------------------------------------------------------------------------------------------
# Lock and target
# --------------------------------------------------------------------------------------------------------------------


@contextmanager
def store_lock(paths: StorePaths, break_mode: str = "false") -> Iterator[None]:
    """Hold the store lock for the block; ``break_mode='confirmed'`` removes a lock the user agreed to break.

    Raises:
        Blocked: the lock is held and was not confirmed for removal.
    """
    try:
        token = acquire_lock(paths)
    except LockHeld as held:
        if break_mode != "confirmed":
            holder = canonical_json(held.holder)
            raise Blocked("store lock held by another run", LOCK_HELD=holder) from held
        break_lock(paths)
        token = acquire_lock(paths)
    try:
        yield
    finally:
        release_lock(paths, token)


def _gh_ok(parts: list[str]) -> bool:
    """Return True when GitHub lookups can run for this target."""
    return parts[0] == "github.com" and which("gh") is not None


def _identity(repo: Path, parts: list[str]) -> dict[str, Any]:
    """Return the store identity: path components plus the sorted root commits."""
    roots = sorted(run_git(repo, "rev-list", "--max-parents=0", "HEAD").split())
    return {"parts": parts, "root_commits": roots}


def _load_target(paths: StorePaths) -> dict[str, Any]:
    """Read the target written by ``scope``; raise Blocked when the scope step never ran."""
    path = paths.root / TARGET_FILE
    if not path.is_file():
        raise Blocked("evolution scope step has not run for this store; rerun the scope step")
    return json.loads(path.read_text(encoding="utf-8"))


def _weights(paths: StorePaths) -> tuple[dict[str, float], float, bool]:
    """Return ``(weights, threshold, provisional)``: tuned values from ``weights.json`` when evaluated, else
    defaults."""
    path = paths.root / WEIGHTS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_WEIGHTS), DEFAULT_THRESHOLD, True
    if not isinstance(data, dict) or data.get("evaluated") is not True:
        return dict(DEFAULT_WEIGHTS), DEFAULT_THRESHOLD, True
    return {**DEFAULT_WEIGHTS, **data.get("weights", {})}, float(data.get("threshold", DEFAULT_THRESHOLD)), False


# --------------------------------------------------------------------------------------------------------------------
# scope
# --------------------------------------------------------------------------------------------------------------------


def _estimate_fixes(repo: Path, index: dict[str, Any]) -> int:
    """Estimate the fix count: changelog "Fixed" bullets when a changelog exists, else ``fix`` commits."""
    found = discover_changelog(repo)
    if found.get("path"):
        text = (repo / found["path"]).read_text(encoding="utf-8", errors="replace")
        sections = parse_changelog(text, found.get("format") or "md")
        counted = sum(len(section.get("fixed", [])) for section in sections.values())
        if counted:
            return counted
    out = run_git(repo, "log", "--oneline", "--no-merges", "-E", "--grep=^fix(\\(.*\\))?!?:", check=False)
    return len(out.splitlines()) or len(index["releases"])


def _scope_decision(
    index: dict[str, Any], saved: dict[str, Any] | None, rng: str | None
) -> tuple[str, bool, list[str]]:
    """Return ``(scope value, ask the user, newer releases than the saved scope)``."""
    stable = _stable(index)
    newer: list[str] = []
    if saved and saved.get("to") is None:
        known = {rel["version"] for rel in index["releases"]}
        newer = [
            rel["version"]
            for rel in stable
            if rel["version"] not in set(saved.get("scanned", [])) and rel["version"] in known
        ]
    if rng:
        return rng, False, newer
    if saved:
        return "saved", bool(newer), newer
    return "", True, newer


def cmd_scope(args: argparse.Namespace) -> int:
    """Run the ``scope`` subcommand."""
    project = Path(args.project_root).resolve()
    target, rng = parse_scan_args(args.args)
    info = resolve_target(target, project)
    paths = resolve_store_root(project / ".evolution", info["parts"])
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.root / SCOPE_FILE).unlink(missing_ok=True)
    if info["dedicated"]:
        with store_lock(paths, args.break_lock):
            info["warnings"] += sync_clone(paths.repo, info["clone_url"], info.get("upstream_url"))
        repo = paths.repo
    else:
        repo = Path(info["checkout"])
    if is_shallow(repo):
        raise Blocked(f"{repo} is a shallow clone; run `git fetch --unshallow` there first")
    index = build_release_index(repo)
    if not _stable(index):
        raise Blocked("no stable release tag found; evolution needs at least one release")
    if rng:
        resolve_scope(rng, None, index)
    atomic_write_json(paths.root / TARGET_FILE, {**info, "repo_dir": str(repo)})
    manifest = read_manifest(paths)
    saved = manifest.get("scope") if manifest else None
    value, ask, newer = _scope_decision(index, saved, rng)
    write_private_text(_sentinel("analyse-evo-root"), f"{paths.root}\n")
    write_private_text(_sentinel("analyse-evo-scope"), f"{'' if ask else value}\n")
    stable = _stable(index)
    est = _estimate_fixes(repo, index)
    for key, val in (
        ("EVO_ROOT", paths.root),
        ("REPO_DIR", repo),
        ("RELEASES", len(stable)),
        ("FIRST", stable[0]["version"]),
        ("LAST", stable[-1]["version"]),
        ("PRERELEASES", len(index["releases"]) - len(stable)),
        ("EST_FIXES", est),
        ("EST_COST", f"about {max(1, round(est * EST_SECONDS_PER_FIX / 60))} min of blame for a full scan"),
        ("SAVED_SCOPE", canonical_json(saved) if saved else ""),
        ("NEWER", ",".join(newer)),
        ("ASK_SCOPE", "true" if ask else "false"),
        ("SCOPE_FILE", paths.root / SCOPE_FILE),
    ):
        _emit(key, val)
    for warning in info["warnings"]:
        _emit("WARNING", warning)
    return 0


# --------------------------------------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------------------------------------


def _scope_value(paths: StorePaths) -> str:
    """Return the scope chosen for this run: the sentinel, else the user's ``scope-choice.txt`` answer."""
    value = _read_sentinel("analyse-evo-scope")
    if value:
        return value
    path = paths.root / SCOPE_FILE
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        if lines and lines[0].strip():
            return lines[0].strip()
    raise Blocked("no scan scope chosen; pass v1->v2 or answer the scope question")


def _collect(
    repo: Path, fixes: list[dict[str, Any]], todo: list[str], ctx: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Blame every fix in ``todo`` within the run budget; bind each to a provisional row as soon as it finishes."""
    paths: StorePaths = ctx["paths"]
    by_id = {fix["entry_id"]: fix for fix in fixes}
    deadline = time.monotonic() + RUN_BLAME_BUDGET_S
    collected: dict[str, dict[str, Any]] = {}
    for fix_id in todo:
        budget = max(0.0, min(FIX_BLAME_BUDGET_S, deadline - time.monotonic()))
        evidence = collect_evidence(repo, by_id[fix_id], budget_s=budget)
        row = score_rows(
            [by_id[fix_id]], {fix_id: evidence}, ctx["columns"], ctx["sha_map"], ctx["index"], **ctx["score_kw"]
        )[0]
        append_fix_result(paths, evidence, row)
        collected[fix_id] = evidence
    return collected


def _real_cells(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a row's cells on real (feature/change) columns."""
    return [cell for cell in row["cells"] if column_kind(cell["column_id"]) == "real"]


def _ambiguous(row: dict[str, Any], threshold: float) -> bool:
    """Return True when the best real-column score is positive but below the threshold."""
    scores = [cell["raw_score"] for cell in _real_cells(row)]
    return bool(scores) and 0 < max(scores) < threshold


def _adjudication_plan(rows: list[dict[str, Any]], ctx: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(verdicts by fix, new requests)``: stored verdicts reused by key, new ones requested up to the cap.

    A row whose blamed lines all land on pseudo-columns is never adjudicated, and a verdict stored for it earlier is
    not reused: its pseudo bucket is already the answer, and an ``unknown`` verdict would move it to the ``unknown``
    residue.
    """
    stored = load_adjudications(ctx["paths"])
    columns = {col["column_id"]: col for col in ctx["columns"]}
    verdicts: dict[str, Any] = {}
    requests: list[dict[str, Any]] = []
    for row in rows:
        if not _ambiguous(row, ctx["threshold"]):
            continue
        fix_id = row["fix_id"]
        if blame_only_on_pseudo(
            ctx["fixes"][fix_id], ctx["evidence"].get(fix_id), ctx["sha_map"], ctx["score_kw"]["weights"]
        ):
            continue
        top = sorted(_real_cells(row), key=lambda c: (-c["raw_score"], c["column_id"]))[:ADJ_CANDIDATES]
        cands = [
            {**columns[c["column_id"]], "signals": {"raw": c["raw_score"], "method": c["method"]}}
            for c in top
            if c["column_id"] in columns
        ]
        item = request_item(row["fix_id"], ctx["fixes"][row["fix_id"]], cands, ctx["evidence"].get(row["fix_id"], {}))
        if item["key"] in stored:
            verdicts[row["fix_id"]] = stored[item["key"]]
        elif ctx["adjudicate"] and len(requests) < ADJ_CAP:
            requests.append(item)
        elif ctx["adjudicate"]:
            verdicts[row["fix_id"]] = {"unadjudicated": True}
    return verdicts, requests


def request_item(
    fix_id: str, fix_entry: dict[str, Any], candidates: list[dict[str, Any]], evidence: dict[str, Any]
) -> dict[str, Any]:
    """Build one adjudication request item exactly as a run writes it into ``adjudication-request.json``.

    Args:
        fix_id: The fix row's ID.
        fix_entry: The fix entry (``entry_id`` required).
        candidates: Candidate columns in request order (``column_id`` required).
        evidence: The fix's blame evidence record.

    Returns:
        ``{fix_id, key, candidates, prompt}``; ``key`` is the reuse key under the adjudicator's model and effort.

    Examples:
        >>> item = request_item("f1", {"entry_id": "e1", "text": "fix"}, [{"column_id": "c1", "title": "t"}], {})
        >>> sorted(item), item["candidates"]
        (['candidates', 'fix_id', 'key', 'prompt'], ['c1'])
    """
    prompt = build_prompt(fix_entry, candidates, evidence)
    return {
        "fix_id": fix_id,
        "key": adjudication_key(prompt, ADJ_MODEL, ADJ_EFFORT),
        "candidates": [candidate["column_id"] for candidate in candidates],
        "prompt": prompt,
    }


def _changed(old: dict[str, Any] | None, new: dict[str, Any]) -> bool:
    """Return True when a row's scored content differs from the stored row."""
    if old is None:
        return True
    skip = {"evidence_count", "evidence_digest"}
    return canonical_json({k: v for k, v in old.items() if k not in skip}) != canonical_json(
        {k: v for k, v in new.items() if k not in skip}
    )


def _write_columns(paths: StorePaths, columns: list[dict[str, Any]]) -> None:
    """Rewrite ``columns.jsonl`` (a derived file) through a temp file and an atomic rename."""
    paths.columns.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.columns.with_name(f".{paths.columns.name}.{os.getpid()}.tmp")
    tmp.write_text("".join(f"{canonical_json(col)}\n" for col in columns), encoding="utf-8", newline="\n")
    os.replace(tmp, paths.columns)


def _bugs(target: dict[str, Any], index: dict[str, Any], labels: str) -> dict[str, Any] | None:
    """Fetch the approximate open-bug series for GitHub targets; None otherwise."""
    parts = target["parts"]
    if not _gh_ok(parts) or len(parts) != 3:
        return None
    label_set = [part.strip() for part in labels.split(",") if part.strip()] or list(DEFAULT_BUG_LABELS)
    return fetch_open_bug_series(parts[1], parts[2], _stable(index), label_set)


def _changelog_state(repo: Path) -> str:
    """Return a digest of the working-tree files entry building reads: changelog, towncrier config, fragments.

    Everything else ``build_entries`` reads comes from git objects covered by HEAD and the tag map. A user checkout can
    carry an uncommitted changelog edit or new fragments, so their current bytes are hashed; unrelated working-tree
    files (including this tool's own store and reports) never invalidate the cache.
    """
    found = discover_changelog(repo)
    towncrier = found.get("towncrier") or {}
    names = [found.get("path"), towncrier.get("config"), *towncrier.get("fragments", [])]
    digest = hashlib.sha256(canonical_json(found).encode("utf-8"))
    for name in names:
        path = repo / name if name else None
        if path is not None and path.is_file() and not path.is_symlink():
            digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _entries_key(repo: Path, index: dict[str, Any], gh_available: bool) -> str:
    """Return the cache key of ``build_entries`` output: every input it reads, plus its own algorithm version.

    Covers the tag map (index fingerprint, which carries the index version), HEAD (the unreleased range and committed
    history), the working-tree changelog inputs, whether GitHub lookups ran, and ``ALGO_VERSIONS["entries"]``. Later
    stages' versions (evidence, scores, the adjudication prompt) are left out, so bumping one of them does not repeat
    the changelog parse and GitHub lookups. GitHub-side edits (a retitled PR, a relabelled issue) are not covered;
    ``--full`` refetches them.
    """
    inputs = {
        "index": index["fingerprint"],
        "head": (index.get("head") or {}).get("sha"),
        "changelog": _changelog_state(repo),
        "gh": gh_available,
        "algo": ALGO_VERSIONS["entries"],
    }
    return hashlib.sha256(canonical_json(inputs).encode("utf-8")).hexdigest()


def _cached_entries(paths: StorePaths, key: str) -> dict[str, Any] | None:
    """Return the entries stored by the previous run when its key matches; None otherwise or on any read failure."""
    try:
        stored = json.loads(paths.index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or stored.get("schema_version") != SCHEMA_VERSION:
        return None
    if stored.get("entries_key") != key or not isinstance(stored.get("entries"), dict):
        return None
    return stored["entries"]


def _prepare(paths: StorePaths, target: dict[str, Any], scope_value: str, full: bool) -> dict[str, Any]:
    """Index, check identity, resolve scope and build entries; reset ``store/`` first for ``--full``.

    Entries (changelog parse, linkage with GitHub lookups, column footprints) are reused from ``index.json`` when their
    inputs are unchanged, so a rescore or ingest makes no GitHub calls and no symbol diffs.
    """
    repo = Path(target["repo_dir"])
    manifest = read_manifest(paths)
    if full:
        reset_store(paths)
    index = build_release_index(repo)
    identity = _identity(repo, target["parts"])
    if manifest and manifest.get("identity") != identity:
        raise Blocked(f"store {paths.root} belongs to a different repository; use another target or delete the store")
    scope = resolve_scope(scope_value, manifest.get("scope") if manifest else None, index)
    gh_available = _gh_ok(target["parts"])
    entries_key = _entries_key(repo, index, gh_available)
    entries = _cached_entries(paths, entries_key)
    if entries is None:
        entries = build_entries(repo, index, gh_available=gh_available)
    order = {rel["version"]: rel["order"] for rel in index["releases"]}
    _fold_prereleases(entries["fixes"], index)
    fixes = [fix for fix in entries["fixes"] if _in_scope(order, fix["release"], scope)]
    return {
        "repo": repo,
        "index": index,
        "identity": identity,
        "scope": scope,
        "entries": entries,
        "entries_key": entries_key,
        "fixes": fixes,
    }


def _score_pipeline(paths: StorePaths, prep: dict[str, Any], adjudicate: bool) -> dict[str, Any]:
    """Collect missing evidence, then score every in-scope fix with overrides and stored verdicts."""
    weights, threshold, provisional = _weights(paths)
    fixes = prep["fixes"]
    columns = build_columns(prep["entries"], prep["index"])
    sha_map = build_sha_map(columns, prep["index"], fixes)
    ctx: dict[str, Any] = {
        "paths": paths,
        "columns": columns,
        "sha_map": sha_map,
        "index": prep["index"],
        "threshold": threshold,
        "adjudicate": adjudicate,
        "fixes": {fix["entry_id"]: fix for fix in fixes},
        "score_kw": {"weights": weights, "threshold": threshold},
    }
    fps = {fix["entry_id"]: evidence_fingerprint(prep["repo"], fix) for fix in fixes}
    stored = load_store(paths)
    plan = plan_work(fixes, fps, stored, scores_fp={})
    evidence = dict(stored["evidence"])
    evidence.update(_collect(prep["repo"], fixes, plan["evidence"], ctx))
    ctx["evidence"] = evidence
    column_ids = [col["column_id"] for col in columns]
    overrides, rejected = read_overrides(paths, list(ctx["fixes"]), column_ids, prep["entries"].get("aliases", {}))
    base = score_rows(fixes, evidence, columns, sha_map, prep["index"], None, overrides, weights, threshold)
    verdicts, requests = _adjudication_plan(base, ctx)
    rows = score_rows(fixes, evidence, columns, sha_map, prep["index"], verdicts, overrides, weights, threshold)
    current = load_store(paths)["rows"]
    for row in rows:
        if _changed(current.get(row["fix_id"]), row):
            append_fix_result(paths, evidence[row["fix_id"]], row)
    return {
        "rows": rows,
        "columns": columns,
        "all_columns": columns + pseudo_columns(rows, prep["index"]),
        "requests": requests,
        "rejected": rejected,
        "weights": weights,
        "threshold": threshold,
        "provisional": provisional,
        "sha_map": sha_map,
    }


def _plugin_version() -> str:
    """Return this plugin's version from its manifest, or ``unknown``."""
    path = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("version", "unknown"))
    except (OSError, ValueError):
        return "unknown"


def _manifest(prep: dict[str, Any], scored: dict[str, Any], labels: str) -> dict[str, Any]:
    """Build the store manifest for this run."""
    scope = dict(prep["scope"])
    order = {rel["version"]: rel["order"] for rel in prep["index"]["releases"]}
    scope["scanned"] = [
        rel["version"] for rel in _stable(prep["index"]) if _in_scope(order, rel["version"], prep["scope"])
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": prep["identity"],
        "scope": scope,
        "weights": scored["weights"],
        "threshold": scored["threshold"],
        "weights_provisional": scored["provisional"],
        "label_set": [part.strip() for part in labels.split(",") if part.strip()] or list(DEFAULT_BUG_LABELS),
        "plugin_version": _plugin_version(),
        "algo_versions": dict(ALGO_VERSIONS),
        "index_fingerprint": prep["index"]["fingerprint"],
        "fixes": len(scored["rows"]),
        "columns": sum(1 for col in scored["columns"] if col.get("kind") in REAL_KINDS),
    }


def _yaml_header(meta: dict[str, str]) -> str:
    """Render the report's YAML metadata block (single-line values, at most 100 characters each)."""
    lines = ["---"]
    for key, value in meta.items():
        text = " ".join(str(value).split()).replace('"', "'")[:100]
        lines.append(f'{key}: "{text}"')
    return "\n".join([*lines, "---", ""])


def _report_meta(ctx: dict[str, Any], report_md: Path) -> dict[str, str]:
    """Build the report header fields from the aggregate and manifest."""
    agg, manifest, parts = ctx["aggregate"], ctx["manifest"], ctx["target"]["parts"]
    totals = agg.get("totals", {})
    residue = float(totals.get("residue_share", 0.0))
    warn = manifest["weights_provisional"] or residue > 0.35 or ctx["pending"] > 0
    scope = manifest["scope"]
    return {
        "Title": f"Release evolution — {'/'.join(parts[1:])}",
        "Date": ctx["today"],
        "Scope": f"{scope['from']} -> {scope['to'] or 'HEAD'} ({len(scope['scanned'])} releases)",
        "Focus": "feature x fix association, fix lineage by origin age, burn-up",
        "Agents": "none (deterministic); LLM adjudication only with --adjudicate",
        "Outcome": f"{'⚠' if warn else '✓'} {totals.get('fixes', 0)} fixes, {totals.get('binary_links', 0)} feature links, residue {residue:.0%}",
        "Confidence": "provisional weights (Phase 0 evaluation pending)"
        if manifest["weights_provisional"]
        else "weights evaluated on labelled fixes",
        "Next steps": "review report.html; correct cells in overrides.csv and rerun to rescore",
        "Path": str(report_md),
    }


def _render(ctx: dict[str, Any], project: Path) -> tuple[Path, Path]:
    """Render the report files and return ``(report.md, report.html)`` paths."""
    parts = ctx["target"]["parts"]
    out_dir = project / ".reports" / "analyse" / "evolution" / f"{'-'.join(parts)}-{ctx['today']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    repo = {"host": parts[0], "owner": parts[1] if len(parts) > 2 else "", "repo": parts[-1]}
    report = {
        "repo": repo,
        "manifest": ctx["manifest"],
        "rows": ctx["rows"],
        "columns": ctx["columns"],
        "aggregate": ctx["aggregate"],
        "bugs": ctx["bugs"],
        "warnings": ctx["warnings"],
    }
    files = render_report(report)
    report_md = out_dir / "report.md"
    files["report.md"] = _yaml_header(_report_meta(ctx, report_md)) + files["report.md"]
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
    return report_md, out_dir / "report.html"


def _warnings(prep: dict[str, Any], scored: dict[str, Any], target: dict[str, Any]) -> list[str]:
    """Collect the run's user-facing warnings."""
    warnings = list(target.get("warnings", [])) + list(prep["entries"].get("warnings", []))
    warnings += [f"override line {rej.get('line')}: {rej.get('reason')}" for rej in scored["rejected"]]
    if not _gh_ok(target["parts"]):
        warnings.append("git-only mode: no PR/issue linkage, no issue text signals, no open-bug series")
    return warnings


def execute_run(paths: StorePaths, project: Path, opts: dict[str, Any]) -> dict[str, Any]:
    """Run the locked pipeline and render; return the printed results."""
    target = _load_target(paths)
    scope_value = _scope_value(paths)
    with store_lock(paths):
        prep = _prepare(paths, target, scope_value, opts["full"])
        scored = _score_pipeline(paths, prep, opts["adjudicate"])
        agg = aggregate(scored["rows"], scored["columns"], prep["index"])
        write_matrix_csv(paths, scored["rows"], scored["all_columns"])
        _write_columns(paths, scored["all_columns"])
        manifest = _manifest(prep, scored, opts["bug_labels"])
        atomic_write_json(
            paths.index,
            {
                "schema_version": SCHEMA_VERSION,
                "index": prep["index"],
                "entries": prep["entries"],
                "entries_key": prep["entries_key"],
                "sha_map": scored["sha_map"],
            },
        )
        write_manifest(paths, manifest)
    request_path = paths.root / ADJ_REQUEST_FILE
    atomic_write_json(request_path, scored["requests"])
    ctx = {
        "target": target,
        "manifest": manifest,
        "rows": scored["rows"],
        "columns": scored["all_columns"],
        "aggregate": agg,
        "bugs": _bugs(target, prep["index"], opts["bug_labels"]),
        "warnings": _warnings(prep, scored, target),
        "pending": len(scored["requests"]),
        "today": opts["today"],
    }
    report_md, html = _render(ctx, project)
    write_private_text(_sentinel("analyse-report-file"), f"{report_md}\n")
    return {
        "REPORT_FILE": report_md,
        "HTML_FILE": html,
        "ADJ_PENDING": len(scored["requests"]),
        "ADJ_REQUEST": request_path,
    }


def _flag(value: str) -> bool:
    """Read a ``true``/``false`` sentinel value."""
    return value.strip().lower() == "true"


def cmd_run(args: argparse.Namespace) -> int:
    """Run the ``run`` subcommand."""
    paths = paths_from_root(Path(args.root))
    opts = {
        "full": _flag(args.full),
        "adjudicate": _flag(args.adjudicate),
        "bug_labels": args.bug_labels,
        "today": args.today,
    }
    for key, value in execute_run(paths, Path(args.project_root).resolve(), opts).items():
        _emit(key, value)
    return 0


def ingest_verdicts(paths: StorePaths) -> tuple[int, int]:
    """Validate and store adjudicator verdicts; return ``(stored, rejected)``."""
    requests = json.loads((paths.root / ADJ_REQUEST_FILE).read_text(encoding="utf-8"))
    by_key = {req["key"]: req for req in requests}
    verdicts_path = paths.root / ADJ_VERDICTS_FILE
    items = json.loads(verdicts_path.read_text(encoding="utf-8")) if verdicts_path.is_file() else []
    stored = rejected = 0
    with store_lock(paths):
        for item in items if isinstance(items, list) else []:
            req = by_key.get(item.get("key")) if isinstance(item, dict) else None
            if req is None or item.get("fix_id") != req["fix_id"]:
                rejected += 1
                continue
            try:
                verdict = validate_verdict(item.get("verdict"), req["candidates"])
            except VerdictError:
                rejected += 1
                continue
            record = {
                "schema_version": SCHEMA_VERSION,
                "key": req["key"],
                "fix_id": req["fix_id"],
                "model": ADJ_MODEL,
                "effort": ADJ_EFFORT,
                "verdict": verdict,
            }
            append_adjudication(paths, record)
            stored += 1
    return stored, rejected


def cmd_render_request(args: argparse.Namespace) -> int:
    """Run the ``render-request`` subcommand: write a request file from fix specs, without a store or a clone.

    Each spec is ``{fix_id, fix_entry, candidates, evidence}``; the output is the list a run would write for those
    fixes, so calibration fixtures and replays use the production prompt byte for byte.
    """
    try:
        specs = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvolutionError(f"cannot read spec {args.spec}: {exc}") from exc
    if not isinstance(specs, list) or not specs:
        raise EvolutionError("spec must be a non-empty JSON list of {fix_id, fix_entry, candidates, evidence}")
    items = []
    for index, spec in enumerate(specs):
        if (
            not isinstance(spec, dict)
            or not str(spec.get("fix_id") or "").strip()
            or not isinstance(spec.get("candidates"), list)
            or not all(isinstance(cand, dict) for cand in spec["candidates"])
        ):
            raise EvolutionError(
                f"spec[{index}] needs a non-empty fix_id, fix_entry, candidates (list of objects) and evidence"
            )
        try:
            items.append(
                request_item(
                    str(spec["fix_id"]),
                    spec.get("fix_entry") or {},
                    spec["candidates"],
                    spec.get("evidence") or {},
                )
            )
        except ValueError as exc:
            raise EvolutionError(f"spec[{index}]: {exc}") from exc
    Path(args.out).write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    _emit("ITEMS", len(items))
    _emit("ADJ_REQUEST", args.out)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Run the ``ingest`` subcommand: store verdicts, then rerun the pipeline without new blame."""
    paths = paths_from_root(Path(args.root))
    stored, rejected = ingest_verdicts(paths)
    _emit("VERDICTS_STORED", stored)
    _emit("VERDICTS_REJECTED", rejected)
    opts = {"full": False, "adjudicate": False, "bug_labels": args.bug_labels, "today": args.today}
    for key, value in execute_run(paths, Path(args.project_root).resolve(), opts).items():
        _emit(key, value)
    return 0


# --------------------------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(prog="evolution_scan.py", description="Run the /oss:analyse evolution pipeline.")
    sub = parser.add_subparsers(dest="command", required=True)
    scope = sub.add_parser("scope", help="resolve target, sync clone, index releases, decide the scope question")
    scope.add_argument("--args", default="", help="CLEAN_ARGS blob starting with 'evolution'")
    scope.add_argument("--project-root", default=".", help="project directory holding .evolution/ and .reports/")
    scope.add_argument("--break-lock", default="false", help="false | true | confirmed (remove a held lock)")
    for name in ("run", "ingest"):
        cmd = sub.add_parser(name, help=f"{name} the evolution pipeline under the store lock")
        cmd.add_argument("--root", required=True, help="store root printed by the scope step (EVO_ROOT)")
        cmd.add_argument("--project-root", default=".", help="project directory holding .reports/")
        cmd.add_argument("--today", required=True, help="report date YYYY-MM-DD")
        cmd.add_argument("--bug-labels", default="", help="comma-separated bug labels replacing the default set")
        if name == "run":
            cmd.add_argument("--full", default="false", help="true rebuilds store/ (clone, overrides, verdicts kept)")
            cmd.add_argument("--adjudicate", default="false", help="true requests LLM verdicts for ambiguous rows")
    render = sub.add_parser("render-request", help="write an adjudication request file from fix specs (no store)")
    render.add_argument("--spec", required=True, help="JSON list of {fix_id, fix_entry, candidates, evidence}")
    render.add_argument("--out", required=True, help="request file to write")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return 0 on success, 2 when blocked, 1 on an unexpected failure."""
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    handlers = {"scope": cmd_scope, "run": cmd_run, "ingest": cmd_ingest, "render-request": cmd_render_request}
    try:
        return handlers[args.command](args)
    except Blocked as blocked:
        for key, value in blocked.keys.items():
            _emit(key, value)
        print(f"! BLOCKED — {blocked}")
        return EXIT_BLOCKED
    except (StoreVersionError, StoreTrackedError) as exc:
        print(f"! BLOCKED — {exc}")
        return EXIT_BLOCKED
    except EvolutionError as exc:
        print(f"! BLOCKED — {exc}")
        return EXIT_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
