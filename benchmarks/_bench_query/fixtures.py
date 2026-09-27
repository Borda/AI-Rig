"""Self-contained fixture repositories for the deterministic correctness suites (D/B/R/K/U)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _bench_query.models import ScenarioResult, THRESHOLDS
from _bench_query.output import log


# ---- DETERMINISTIC CORRECTNESS SUITES (D/B/R/K/U) ----
#
# Unlike S/H/X (which score scan-query against its OWN frozen output, so a pass proves only
# determinism), these suites build a self-contained fixture repo in a tmp dir whose ground truth
# is KNOWN by construction — N importers, an exactly-corrupted index, a single broken sphinx xref.
# scan-query never authors that truth, so a pass is genuine independent-oracle correctness and the
# suite joins the primary verdict (suite name "correctness", in _PRIMARY_SUITES). They assert the
# user-visible CLI contract against an arbitrary target repo (a product acceptance check), not the
# per-edge-case matrix already unit-tested in plugins/codemap-py/tests/ — kept thin, no duplication.
#
# Each suite runs OFFLINE (no external PL clone / index) but needs scan-index to build its fixture;
# when scan-index is unavailable the suite skips (logs + returns []), mirroring the S/H/X skip path.


class _Checklist:
    """Accumulates named boolean sub-checks into one pass/fail correctness scenario.

    A single fixture-based suite makes several assertions against one CLI contract
    (e.g. diff-impact must surface the changed module AND its risk tier AND the test
    union). Rather than emit one :class:`ScenarioResult` per assertion — which would
    inflate the scenario count and obscure that they share a fixture — each suite
    records its assertions here and folds them into one aggregate result whose
    ``passed`` is the conjunction of every recorded check.

    Examples:
        >>> cl = _Checklist()
        >>> cl.record("importers_high", True)
        >>> cl.record("risk_tier_high", True)
        >>> cl.passed, [name for name, ok in cl.checks]
        (True, ['importers_high', 'risk_tier_high'])
    """

    def __init__(self) -> None:
        self.checks: list[tuple[str, bool]] = []

    def record(self, name: str, ok: bool) -> None:
        """Record one named sub-check outcome (coerced to ``bool``)."""
        self.checks.append((name, bool(ok)))

    @property
    def passed(self) -> bool:
        """Return ``True`` only when at least one check ran and all recorded checks passed."""
        return bool(self.checks) and all(ok for _name, ok in self.checks)

    @property
    def failures(self) -> list[str]:
        """Return the names of every sub-check that failed, in record order."""
        return [name for name, ok in self.checks if not ok]


def _correctness_scenario(scenario: str, name: str, checklist: _Checklist, error: str | None = None) -> ScenarioResult:
    """Fold a :class:`_Checklist` (or a fixture-setup error) into one correctness ScenarioResult.

    Args:
        scenario: scenario code (e.g. ``"D_diff_impact"``) — also the THRESHOLDS key.
        name: human label (e.g. ``"diff-impact"``).
        checklist: the accumulated sub-checks for this suite.
        error: fixture-setup failure reason; when set the scenario fails outright and
            the reason is surfaced instead of a per-check breakdown.

    Returns:
        A ScenarioResult with ``suite="correctness"`` whose ``passed`` is the checklist
        conjunction (or ``False`` on a setup error).
    """
    if error is not None:
        return ScenarioResult(
            scenario=scenario,
            name=name,
            suite="correctness",
            passed=False,
            result={"contract_holds": False, "error": error, "checks": {}},
            threshold=THRESHOLDS[scenario],
            notes=f"fixture setup failed: {error}",
        )
    checks = {n: ok for n, ok in checklist.checks}
    failures = checklist.failures
    return ScenarioResult(
        scenario=scenario,
        name=name,
        suite="correctness",
        passed=checklist.passed,
        result={"contract_holds": checklist.passed, "checks": checks, "failed_checks": failures},
        threshold=THRESHOLDS[scenario],
        notes=("all checks passed" if checklist.passed else f"failed: {', '.join(failures)}"),
    )


def _fixture_git(root: Path, *args: str) -> None:
    """Run fixture Git commands with fixed identity and no automatic maintenance.

    Automatic maintenance can outlive its Git parent and race removal of the
    disposable repository. Per-command settings keep that work disabled without
    changing host configuration; other Git configuration remains inherited.

    Args:
        root: repository working directory.
        *args: git subcommand and its arguments.

    Raises:
        RuntimeError: when the git command exits non-zero.
    """
    settings = [
        "-c",
        "user.email=bench@codemap",
        "-c",
        "user.name=bench",
        "-c",
        "maintenance.auto=false",
        "-c",
        "gc.auto=0",
    ]
    result = subprocess.run(["git", *settings, *args], cwd=str(root), capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")


def _fixture_scan(scan_index_bin: Path, root: Path, *extra: str) -> Path:
    """Build a codemap index over *root* and return the index path, raising on failure.

    Args:
        scan_index_bin: path to the scan-index executable.
        root: fixture repository root to index.
        *extra: additional scan-index flags (e.g. ``"--incremental"``).

    Returns:
        Path to the JSON index scan-index wrote (``<root>/.cache/codemap/<root.name>.json``).

    Raises:
        RuntimeError: when scan-index exits non-zero or produces no index file.
    """
    result = subprocess.run(
        [sys.executable, str(scan_index_bin.resolve()), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"scan-index failed: {result.stderr.strip()}")
    index_path = root / ".cache" / "codemap" / f"{root.name}.json"
    if not index_path.exists():
        raise RuntimeError(f"scan-index produced no index at {index_path}")
    return index_path


def _fixture_query_raw(
    scan_query_bin: Path, root: Path, index_path: Path, args: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run scan-query against a fixture index WITHOUT asserting success (caller inspects exit + output).

    Args:
        scan_query_bin: path to the scan-query executable.
        root: fixture repository root (used as cwd).
        index_path: path to the fixture index.
        args: subcommand and arguments (e.g. ``["diff-impact", "--no-heal"]``).

    Returns:
        The completed process; the caller reads ``returncode`` / ``stdout`` / ``stderr``.
    """
    cmd = [sys.executable, str(scan_query_bin.resolve()), "--index", str(index_path.resolve()), *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(root), timeout=30)


def _fixture_query(scan_query_bin: Path, root: Path, index_path: Path, args: list[str]) -> dict:
    """Run scan-query against a fixture index and return parsed JSON, raising on any failure.

    Args:
        scan_query_bin: path to the scan-query executable.
        root: fixture repository root (used as cwd).
        index_path: path to the fixture index.
        args: subcommand and arguments (e.g. ``["diff-impact", "--no-heal"]``).

    Returns:
        The parsed JSON payload from scan-query stdout.

    Raises:
        RuntimeError: when scan-query exits non-zero or emits invalid JSON.
    """
    proc = _fixture_query_raw(scan_query_bin, root, index_path, args)
    if proc.returncode != 0:
        raise RuntimeError(f"scan-query {args} exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"scan-query {args} emitted invalid JSON: {exc}") from exc


def _build_diff_impact_repo(root: Path, scan_index_bin: Path) -> Path:
    """Materialise + commit + index a fixture where ``lib`` has 5 importers, then edit ``lib``.

    Five importer modules put ``lib`` at the HIGH risk tier (>=5 reverse-deps); a test
    module exercises it (test-impact union); one edited-but-unindexed staged file
    surfaces via ``unmapped_files``. The edit to ``lib.py`` is left uncommitted so the
    working-tree diff-impact has a change to report.

    Args:
        root: fixture repository root (created by the caller).
        scan_index_bin: path to scan-index.

    Returns:
        Path to the built index.
    """
    (root / "lib.py").write_text("def helper(x):\n    return x + 1\n\n\ndef untouched(y):\n    return y\n")
    for n in range(1, 6):  # 5 importers → HIGH tier
        (root / f"consumer{n}.py").write_text(f"import lib\n\n\ndef run{n}(x):\n    return lib.helper(x)\n")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_lib.py").write_text("import lib\n\n\ndef test_helper():\n    assert lib.helper(1) == 2\n")
    _fixture_git(root, "init", "-q")
    _fixture_git(root, "add", ".")
    _fixture_git(root, "commit", "-q", "-m", "base")
    index_path = _fixture_scan(scan_index_bin, root)
    # Working-tree edit (uncommitted): the change diff-impact must report.
    (root / "lib.py").write_text("def helper(x):\n    return x + 2\n\n\ndef untouched(y):\n    return y\n")
    # Staged but unindexed new file → must surface as unmapped, never hidden.
    (root / "extra.py").write_text("def brand_new():\n    return 0\n")
    _fixture_git(root, "add", "extra.py")
    return index_path


def _check_diff_impact(scan_query_bin: Path, root: Path, index_path: Path, cl: _Checklist) -> None:
    """Record diff-impact contract checks against the 5-importer fixture into *cl*.

    Asserts the user-visible CLI contract: HIGH tier for >=5 importers, changed module
    and symbol detection, test-impact union naming the test file, a single coverage
    block, ``--base`` range scoping, and unmapped-file surfacing.

    Args:
        scan_query_bin: path to scan-query.
        root: fixture repo root.
        index_path: fixture index path.
        cl: checklist to record outcomes into.
    """
    data = _fixture_query(scan_query_bin, root, index_path, ["--no-heal", "diff-impact"])
    modules = data.get("changed_modules", [])
    lib_entry = next((m for m in modules if m.get("module") == "lib"), None)
    cl.record("changed_module_detected", lib_entry is not None)
    cl.record("changed_symbol_detected", bool(lib_entry) and lib_entry.get("changed_symbols") == ["lib::helper"])
    cl.record("risk_tier_high_5plus_importers", bool(lib_entry) and lib_entry.get("risk") == "HIGH")
    # Known ground truth: 5 consumer modules + the test module all import lib → 6 importers,
    # comfortably past the HIGH threshold (>=5). Assert the exact deterministic count.
    importers = lib_entry.get("importers", []) if lib_entry else []
    cl.record("importer_count_is_6", len(importers) == 6)
    cl.record("importers_include_5_consumers", all(f"consumer{n}" in importers for n in range(1, 6)))
    cl.record("highest_risk_high", data.get("highest_risk") == "HIGH")
    test_files = data.get("test_impact", {}).get("test_files", [])
    cl.record("test_impact_union_names_test_file", any("test_lib.py" in f for f in test_files))
    cl.record("single_coverage_block", "index" in data and all("index" not in m for m in modules))
    cl.record("unmapped_file_surfaced", "extra.py" in data.get("unmapped_files", []))
    # ``--base`` scopes the diff: an empty base (HEAD) still reports the working-tree edit here,
    # so verify a committed range instead by committing the edit and diffing HEAD~1.
    _fixture_git(root, "add", "-A")
    _fixture_git(root, "commit", "-q", "-m", "edit lib")
    clean = _fixture_query(scan_query_bin, root, index_path, ["--no-heal", "diff-impact"])
    ranged = _fixture_query(scan_query_bin, root, index_path, ["--no-heal", "diff-impact", "--base", "HEAD~1"])
    cl.record("base_ref_clean_after_commit", clean.get("changed_files") == 0)
    cl.record("base_ref_scopes_committed_change", "lib" in [m.get("module") for m in ranged.get("changed_modules", [])])


def run_correctness_diff_impact(scan_query_bin: Path, scan_index_bin: Path | None) -> list[ScenarioResult]:
    """Suite D: diff-impact blast-radius correctness on a fixture with a known 5-importer module.

    Builds a self-contained git repo (``lib`` imported by 5 modules + 1 test), edits
    ``lib``, and asserts the full ``diff-impact`` CLI contract — changed module/symbol,
    HIGH risk tier, test-impact union, single coverage block, ``--base`` scoping, and
    unmapped-file surfacing — against KNOWN ground truth. Skips when scan-index is absent.

    Args:
        scan_query_bin: path to the scan-query executable.
        scan_index_bin: path to scan-index, or None (suite then skips).

    Returns:
        A single-element list with the aggregate D scenario, or ``[]`` when skipped.
    """
    if scan_index_bin is None:
        log("[suite-D] scan-index not found — diff-impact correctness skipped")
        return []
    cl = _Checklist()
    with tempfile.TemporaryDirectory(prefix="codemap-bench-diff-") as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        try:
            index_path = _build_diff_impact_repo(root, scan_index_bin)
            _check_diff_impact(scan_query_bin, root, index_path, cl)
        except (RuntimeError, OSError) as exc:
            return [_correctness_scenario("D_diff_impact", "diff-impact", cl, error=str(exc))]
    return [_correctness_scenario("D_diff_impact", "diff-impact", cl)]


def _build_batch_repo(root: Path, scan_index_bin: Path) -> Path:
    """Materialise + index a small import-chain fixture (gamma←beta←alpha) for batch checks.

    Args:
        root: fixture repository root (created by the caller).
        scan_index_bin: path to scan-index.

    Returns:
        Path to the built index.
    """
    (root / "gamma.py").write_text("def func_gamma(x):\n    return x + 1\n")
    (root / "beta.py").write_text("import gamma\n\n\ndef func_beta(x):\n    return gamma.func_gamma(x) * 2\n")
    (root / "alpha.py").write_text(
        "import beta\nimport gamma\n\n\ndef func_alpha(x):\n    return beta.func_beta(x) + gamma.func_gamma(x)\n"
    )
    return _fixture_scan(scan_index_bin, root)


def _run_batch(scan_query_bin: Path, root: Path, index_path: Path, items: list[dict]) -> dict:
    """Run ``batch`` feeding *items* via stdin and return the decoded batch payload.

    Args:
        scan_query_bin: path to scan-query.
        root: fixture repo root.
        index_path: fixture index path.
        items: batch request array.

    Returns:
        The parsed batch result dict.

    Raises:
        RuntimeError: when scan-query exits non-zero or emits invalid JSON.
    """
    cmd = [sys.executable, str(scan_query_bin.resolve()), "--index", str(index_path.resolve()), "batch", "-"]
    proc = subprocess.run(
        cmd,
        input=json.dumps(items),
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=30,
        env={**os.environ, "CODEMAP_LOGGING": "false"},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"batch exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}")
    return json.loads(proc.stdout)


def _check_batch(scan_query_bin: Path, root: Path, index_path: Path, cl: _Checklist) -> None:
    """Record batch contract checks (N valid + 1 invalid) into *cl*.

    Asserts the user-visible CLI contract: exit 0 with a per-item error for the invalid
    item, input-order preservation, a shared coverage summary, top-level ``error``
    plus ``ok:false`` on the bad item, and byte-equivalence of a batched result to its
    full standalone form, including per-item coverage metadata.

    Args:
        scan_query_bin: path to scan-query.
        root: fixture repo root.
        index_path: fixture index path.
        cl: checklist to record outcomes into.
    """
    items = [
        {"cmd": "deps", "args": ["alpha"]},
        {"cmd": "rdeps", "args": ["gamma"]},
        {"cmd": "not-a-command"},  # invalid item
    ]
    batch = _run_batch(scan_query_bin, root, index_path, items)
    entries = batch.get("batch", [])
    cl.record("count_matches_input", batch.get("count") == 3)
    cl.record("per_item_order_preserved", [e.get("index") for e in entries] == [0, 1, 2])
    valid_ok = len(entries) == 3 and entries[0].get("ok") is True and entries[1].get("ok") is True
    cl.record("valid_items_ok", valid_ok)
    bad = entries[2] if len(entries) == 3 else {}
    cl.record("invalid_item_ok_false", bad.get("ok") is False)
    cl.record("invalid_item_has_top_level_error", "error" in bad)
    cl.record("single_shared_coverage_block", "index" in batch)
    # Byte-equivalence: batch now retains the same per-item coverage as standalone.
    standalone = _fixture_query(scan_query_bin, root, index_path, ["deps", "alpha"])
    batched_result = entries[0].get("result") if entries else None
    cl.record("byte_equivalent_to_standalone", batched_result == standalone)


def run_correctness_batch(scan_query_bin: Path, scan_index_bin: Path | None) -> list[ScenarioResult]:
    """Suite B: batch-mode correctness (ordering, invalid-item isolation, coverage dedup, equivalence).

    Builds a small import-chain fixture, runs a batch of N valid + 1 invalid item, and
    asserts the full ``batch`` CLI contract against KNOWN ground truth. Skips when
    scan-index is absent.

    Args:
        scan_query_bin: path to the scan-query executable.
        scan_index_bin: path to scan-index, or None (suite then skips).

    Returns:
        A single-element list with the aggregate B scenario, or ``[]`` when skipped.
    """
    if scan_index_bin is None:
        log("[suite-B] scan-index not found — batch correctness skipped")
        return []
    cl = _Checklist()
    with tempfile.TemporaryDirectory(prefix="codemap-bench-batch-") as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        try:
            index_path = _build_batch_repo(root, scan_index_bin)
            _check_batch(scan_query_bin, root, index_path, cl)
        except (RuntimeError, OSError) as exc:
            return [_correctness_scenario("B_batch", "batch", cl, error=str(exc))]
    return [_correctness_scenario("B_batch", "batch", cl)]


_MONOREPO_PYPROJECT = '[tool.codemap]\nsrc_roots = ["libs/core/src", "services/api/src"]\n'


def _build_src_roots_repo(root: Path, scan_index_bin: Path) -> Path:
    """Materialise + index a two-source-root monorepo with a stray colliding copy.

    ``pkg_a`` lives under the first configured root, ``pkg_b`` under the second, and a
    stray ``pkg_b`` copy sits at the project root (under no configured root) so the
    configured-root path must win the collision deterministically.

    Args:
        root: fixture repository root (created by the caller).
        scan_index_bin: path to scan-index.

    Returns:
        Path to the built index.
    """
    (root / "pyproject.toml").write_text(_MONOREPO_PYPROJECT)
    pkg_a = root / "libs" / "core" / "src" / "pkg_a"
    pkg_a.mkdir(parents=True)
    (pkg_a / "__init__.py").write_text("")
    (pkg_a / "mod_a.py").write_text("def a():\n    return 1\n")
    pkg_b = root / "services" / "api" / "src" / "pkg_b"
    pkg_b.mkdir(parents=True)
    (pkg_b / "__init__.py").write_text("")
    (pkg_b / "mod_b.py").write_text("def b():\n    return 2\n")
    stray = root / "pkg_b"  # collides with the real pkg_b under services/api/src
    stray.mkdir()
    (stray / "__init__.py").write_text("")
    (stray / "mod_b.py").write_text("def b():\n    return 99\n")
    return _fixture_scan(scan_index_bin, root)


def _check_src_roots(index_path: Path, cl: _Checklist) -> None:
    """Record src_roots monorepo contract checks by reading the built index into *cl*.

    Asserts the user-visible index contract: module naming from each configured root,
    the recorded ``src_roots`` meta, and the collision winner residing under the
    configured root (the stray root-level copy loses deterministically).

    Args:
        index_path: fixture index path.
        cl: checklist to record outcomes into.
    """
    index = json.loads(index_path.read_text())
    names = {m.get("name") for m in index.get("modules", [])}
    cl.record("naming_from_first_root", {"pkg_a", "pkg_a.mod_a"}.issubset(names))
    cl.record("naming_from_second_root", {"pkg_b", "pkg_b.mod_b"}.issubset(names))
    cl.record("src_roots_meta_recorded", index.get("src_roots") == ["libs/core/src", "services/api/src"])
    collision = next((c for c in index.get("collisions", []) if c.get("name") == "pkg_b.mod_b"), None)
    winner_under_root = bool(collision) and collision.get("kept") == "services/api/src/pkg_b/mod_b.py"
    cl.record("collision_winner_under_configured_root", winner_under_root)
    kept_path = next((m.get("path") for m in index.get("modules", []) if m.get("name") == "pkg_b.mod_b"), None)
    cl.record("kept_module_path_under_root", kept_path == "services/api/src/pkg_b/mod_b.py")


def run_correctness_src_roots(scan_query_bin: Path, scan_index_bin: Path | None) -> list[ScenarioResult]:
    """Suite R: monorepo multi-source-root naming + collision correctness on a fixture.

    Builds a two-root monorepo with a stray colliding copy and asserts naming from each
    configured root, the ``src_roots`` meta, and the collision winner under the
    configured root, against KNOWN ground truth. Skips when scan-index is absent.

    Args:
        scan_query_bin: path to the scan-query executable (unused here; kept for a
            uniform suite signature so registration is a plain tuple of callables).
        scan_index_bin: path to scan-index, or None (suite then skips).

    Returns:
        A single-element list with the aggregate R scenario, or ``[]`` when skipped.
    """
    _ = scan_query_bin  # index-only suite; signature kept uniform for registration
    if scan_index_bin is None:
        log("[suite-R] scan-index not found — src_roots correctness skipped")
        return []
    cl = _Checklist()
    with tempfile.TemporaryDirectory(prefix="codemap-bench-roots-") as tmp:
        root = Path(tmp) / "monorepo"
        root.mkdir()
        try:
            index_path = _build_src_roots_repo(root, scan_index_bin)
            _check_src_roots(index_path, cl)
        except (RuntimeError, OSError, ValueError) as exc:
            return [_correctness_scenario("R_src_roots", "src_roots", cl, error=str(exc))]
    return [_correctness_scenario("R_src_roots", "src_roots", cl)]


# Each entry corrupts a healthy index in place, keyed by the self-check ``reason`` slug the CLI
# must surface. Kept as data (not inline branches) so a new corruption variant is one tuple.
_SELF_CHECK_CORRUPTIONS: tuple[tuple[str, str], ...] = (
    ("missing_keys", "drop the modules key"),
    ("bad_version", "scan_version not an int"),
    ("modules_not_list", "modules is an object, not a list"),
)


def _apply_self_check_corruption(index: dict, reason: str) -> dict:
    """Return a copy of *index* corrupted to trigger the named self-check ``reason``.

    Args:
        index: a healthy decoded index dict.
        reason: the self-check reason slug to induce.

    Returns:
        The corrupted index dict.
    """
    if reason == "missing_keys":
        return {k: v for k, v in index.items() if k != "modules"}
    if reason == "bad_version":
        return {**index, "scan_version": "eleven"}
    return {**index, "modules": {}}  # modules_not_list


def _build_self_check_repo(root: Path, scan_index_bin: Path) -> Path:
    """Materialise + index a minimal healthy project whose index the checks then corrupt.

    Args:
        root: fixture repository root (created by the caller).
        scan_index_bin: path to scan-index.

    Returns:
        Path to the built (healthy) index.
    """
    (root / "leaf.py").write_text("def leaf_fn(x):\n    return x\n")
    (root / "consumer.py").write_text("import leaf\n\n\ndef use(x):\n    return leaf.leaf_fn(x)\n")
    return _fixture_scan(scan_index_bin, root)


def _check_self_check(scan_query_bin: Path, root: Path, index_path: Path, cl: _Checklist) -> None:
    """Record index self-check contract checks (corrupt variants + truncation) into *cl*.

    Asserts the user-visible CLI contract: a healthy index serves; each structural
    corruption yields exit 3 with a parseable JSON error naming the reason and rebuild
    fix; a truncated write is refused; and no query output escapes before the verdict
    (never a partial serve).

    Args:
        scan_query_bin: path to scan-query.
        root: fixture repo root.
        index_path: healthy fixture index path (mutated in place per check).
        cl: checklist to record outcomes into.
    """
    healthy = index_path.read_text()
    ok = _fixture_query_raw(scan_query_bin, root, index_path, ["deps", "consumer"])
    cl.record("healthy_index_serves", ok.returncode == 0 and "error" not in json.loads(ok.stdout))

    for reason, _label in _SELF_CHECK_CORRUPTIONS:
        index_path.write_text(json.dumps(_apply_self_check_corruption(json.loads(healthy), reason)))
        proc = _fixture_query_raw(scan_query_bin, root, index_path, ["deps", "consumer"])
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        cl.record(f"corrupt_{reason}_exit_3", proc.returncode == 3)
        cl.record(f"corrupt_{reason}_reason_surfaced", payload.get("reason") == reason)

    # Truncated JSON (half-written file) → refused with the same rebuild path.
    index_path.write_text(healthy[: len(healthy) // 2])
    trunc = _fixture_query_raw(scan_query_bin, root, index_path, ["deps", "consumer"])
    trunc_payload = json.loads(trunc.stdout) if trunc.stdout.strip() else {}
    cl.record("truncated_json_exit_3", trunc.returncode == 3)
    cl.record("truncated_json_error", trunc_payload.get("error") == "index is not valid JSON")

    # Never partial-serve: the whole stdout is one JSON error object, not a query result.
    index_path.write_text(json.dumps(_apply_self_check_corruption(json.loads(healthy), "missing_keys")))
    partial = _fixture_query_raw(scan_query_bin, root, index_path, ["central", "--top", "5"])
    partial_payload = json.loads(partial.stdout) if partial.stdout.strip() else {}
    cl.record("never_partial_serves", set(partial_payload) == {"error", "reason", "detail", "path", "fix"})


def run_correctness_self_check(scan_query_bin: Path, scan_index_bin: Path | None) -> list[ScenarioResult]:
    """Suite K: index self-check correctness — corrupt indexes are refused, never partly served.

    Builds a healthy index, corrupts it several ways in place (missing key, bad version,
    wrong type, truncated JSON), and asserts each yields exit 3 with a parseable JSON
    error and no partial serve, against KNOWN ground truth. Skips when scan-index is absent.

    Args:
        scan_query_bin: path to the scan-query executable.
        scan_index_bin: path to scan-index, or None (suite then skips).

    Returns:
        A single-element list with the aggregate K scenario, or ``[]`` when skipped.
    """
    if scan_index_bin is None:
        log("[suite-K] scan-index not found — self-check correctness skipped")
        return []
    cl = _Checklist()
    with tempfile.TemporaryDirectory(prefix="codemap-bench-selfcheck-") as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        try:
            index_path = _build_self_check_repo(root, scan_index_bin)
            _check_self_check(scan_query_bin, root, index_path, cl)
        except (RuntimeError, OSError, ValueError) as exc:
            return [_correctness_scenario("K_self_check", "self-check", cl, error=str(exc))]
    return [_correctness_scenario("K_self_check", "self-check", cl)]


def _build_uncovered_xrefs_repo(root: Path, scan_index_bin: Path) -> Path:
    """Materialise + index a fixture with exactly 2 undocumented public fns and 1 broken xref.

    ``mymod`` exposes two public, docstring-less functions (undocumented count == 2) and
    ``real_fn`` (documented) — a leaf so the counts are stable. ``user`` cites a
    non-existent symbol via a Sphinx role (one broken xref under ``mymod``).

    Args:
        root: fixture repository root (created by the caller).
        scan_index_bin: path to scan-index.

    Returns:
        Path to the built index.
    """
    (root / "mymod.py").write_text(
        "def real_fn():\n"
        '    """Documented public function."""\n'
        "    return 1\n\n\n"
        "def undoc_one(x):\n"  # public, no docstring → undocumented
        "    return x\n\n\n"
        "def undoc_two(y):\n"  # public, no docstring → undocumented
        "    return y\n"
    )
    (root / "user.py").write_text(
        'def user_fn():\n    """Wrongly cites :func:`mymod.does_not_exist`."""\n    return 1\n'
    )
    return _fixture_scan(scan_index_bin, root)


def _check_uncovered_xrefs(scan_query_bin: Path, root: Path, index_path: Path, cl: _Checklist) -> None:
    """Record undocumented-count + broken-xref contract checks into *cl*.

    Asserts exact deterministic counts against KNOWN ground truth: ``undocumented``
    finds exactly the 2 docstring-less public functions (and never the documented one),
    and ``xrefs --broken`` finds exactly the one dangling Sphinx target.

    Args:
        scan_query_bin: path to scan-query.
        root: fixture repo root.
        index_path: fixture index path.
        cl: checklist to record outcomes into.
    """
    undoc = _fixture_query(scan_query_bin, root, index_path, ["undocumented", "mymod"])
    # Module-level functions use the bare name as qualified_name (``::`` only nests methods).
    qnames = {f.get("qualified_name") for f in undoc.get("undocumented", [])}
    cl.record("undocumented_total_is_2", undoc.get("total") == 2)
    cl.record("undocumented_names_exact", qnames == {"undoc_one", "undoc_two"})
    cl.record("documented_fn_not_flagged", "real_fn" not in qnames)

    broken = _fixture_query(scan_query_bin, root, index_path, ["xrefs", "mymod", "--broken"])
    targets = {b.get("target") for b in broken.get("broken", [])}
    cl.record("broken_xref_count_is_1", broken.get("count") == 1)
    cl.record("broken_xref_target_exact", targets == {"mymod::does_not_exist"})


def run_correctness_uncovered_xrefs(scan_query_bin: Path, scan_index_bin: Path | None) -> list[ScenarioResult]:
    """Suite U: undocumented-count + broken-sphinx-xref correctness on a fixture with known counts.

    Builds a fixture with exactly 2 undocumented public functions and 1 broken Sphinx
    xref, then asserts ``undocumented`` and ``xrefs --broken`` return those exact counts
    — replacing the LLM bench's circular scan-query-derived ground truth with independent,
    construction-known truth. Skips when scan-index is absent.

    Args:
        scan_query_bin: path to the scan-query executable.
        scan_index_bin: path to scan-index, or None (suite then skips).

    Returns:
        A single-element list with the aggregate U scenario, or ``[]`` when skipped.
    """
    if scan_index_bin is None:
        log("[suite-U] scan-index not found — uncovered/xrefs correctness skipped")
        return []
    cl = _Checklist()
    with tempfile.TemporaryDirectory(prefix="codemap-bench-uncov-") as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        try:
            index_path = _build_uncovered_xrefs_repo(root, scan_index_bin)
            _check_uncovered_xrefs(scan_query_bin, root, index_path, cl)
        except (RuntimeError, OSError) as exc:
            return [_correctness_scenario("U_uncovered_xrefs", "uncovered-xrefs", cl, error=str(exc))]
    return [_correctness_scenario("U_uncovered_xrefs", "uncovered-xrefs", cl)]
