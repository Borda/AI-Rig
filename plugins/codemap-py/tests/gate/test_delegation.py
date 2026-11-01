"""Cross-runtime index identity, delegation reuse, and no-rebuild guarantees.

Proves the shared-index delegation contract:

- one canonical resolver maps different working directories in the same repo, and
  either runtime, to the same index path/identity — runtime never in the path;
- after one build, resolving + reading the index from a subdir under runtime
  ``claude`` then ``codex`` triggers zero scanner invocations and preserves the
  index content hash and mtime (scanner-invocation counter oracle, not log text);
- ``CODEMAP_INDEX_DIR`` root-keys equal-basename projects to distinct reusable
  indexes; a symlink/case alias resolves to one identity;
- the legacy flat override is a read-only, root-matched compatibility candidate;
  a mismatch is ignored with an ``index_root_collision`` diagnostic;
- a stale index rebuilt through ``_rwgate`` rebuilds exactly once; the waiter reuses.

``_rwgate`` is a shipped module required by the gate-driven cases; an import
failure is an installed-payload failure, not a skippable optional dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import _index_identity as ii
import _runtime_log as rl
import _rwgate as rw
import pytest

_BIN = Path(__file__).resolve().parents[2] / "bin"
SCAN_INDEX = _BIN / "scan-index"


def _directory_symlink_is_available() -> bool:
    """Return whether this host can create and resolve a directory symlink."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.mkdir()
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            return False
        return link.is_symlink() and link.is_dir()


_skip_directory_symlink_unavailable = pytest.mark.skipif(
    not _directory_symlink_is_available(), reason="directory symlink creation is unavailable on this host"
)

# Scanner-invocation oracle: a thin shim that appends one line per real scan-index
# launch (via env CODEMAP_TEST_SCAN_COUNTER) then execs the real builder. Append is
# atomic for small writes, so a concurrent race cannot silently lose a count.
_SHIM_SOURCE = """\
import os
import subprocess
import sys
import time

counter = os.environ.get("CODEMAP_TEST_SCAN_COUNTER")
if counter:
    # One marker file per scan, never a shared append: this write happens before the engine
    # takes its lease, so racing scans append concurrently, and the Windows CRT emulates
    # O_APPEND by seeking to end first — both writers land on the same offset and one scan
    # vanishes from the tally.
    with open(f"{counter}.{os.getpid()}.{time.time_ns()}", "w", encoding="utf-8") as fh:
        fh.write("scan\\n")
scan_index = os.environ["CODEMAP_TEST_SCAN_INDEX"]
sys.exit(subprocess.run([sys.executable, scan_index, *sys.argv[1:]]).returncode)
"""

# Cross-process writer worker: two of these race through _rwgate.write_index on a
# stale index. The gate's writer-preferred linearisation (writer.json OS lock) must
# let exactly one build; the waiter's build_fn recheck then reuses the published
# index. Same-process threads cannot model this — writer intent is PID-owned.
_WORKER_SOURCE = """\
import os
import subprocess
import sys

# No lease here, deliberately. The exclusive write lease is taken inside the engine
# (codemap_py.graph.main), so every route in is gated by construction. A caller that
# wrapped this spawn in its own rwgate.write_index would deadlock: the child cannot
# acquire (the parent's token is foreign and live to it) and the parent cannot release
# (it is blocked on the child), so the child burns its deadline and fails index_busy.
result = subprocess.run(
    [sys.executable, os.environ["CODEMAP_TEST_SHIM"], "--root", os.environ["CODEMAP_TEST_ROOT"]],
    capture_output=True,
    text=True,
)
sys.stdout.write("built" if result.returncode == 0 else "failed:" + result.stderr[-400:])
sys.exit(result.returncode)
"""


def _git_init(root: Path) -> None:
    """Initialize and commit the minimal repository used by delegation tests."""
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def _count(counter: Path) -> int:
    """Count recorded scans — one marker file per invocation, see ``_SHIM_SOURCE``."""
    return len(list(counter.parent.glob(f"{counter.name}.*")))


def _sha(path: Path) -> str:
    """Return the SHA-256 digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(name="shim")
def _shim(tmp_path: Path) -> Path:
    """Write the scan shim used to record isolated scanner invocations."""
    p = tmp_path / "scan_shim.py"
    p.write_text(_SHIM_SOURCE, encoding="utf-8")
    return p


@pytest.fixture(name="project")
def _project(tmp_path: Path) -> Path:
    """Create a committed one-module repository for delegation tests."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _git_init(root)
    return root


def _scan_env(counter: Path) -> dict:
    """Build the environment for the isolated scanner subprocess."""
    return {
        **os.environ,
        "CODEMAP_TEST_SCAN_COUNTER": str(counter),
        "CODEMAP_TEST_SCAN_INDEX": str(SCAN_INDEX),
    }


@pytest.fixture(name="override_dir")
def _override_dir(tmp_path: Path) -> Path:
    """Create the shared ``CODEMAP_INDEX_DIR`` override directory."""
    override = tmp_path / "shared"
    override.mkdir()
    return override


class TestResolveIndex:
    """The canonical resolver maps working directories and overrides to index identities without any scanner run."""

    def test_same_path_from_repo_subdir(self, project: Path) -> None:
        """Resolving from a nested directory of a repo yields the repo root's index path and identity.

        Both runtimes may start in different subdirectories of one repository; they must still agree on a single index,
        root and coordination directory.
        """
        ident = ii.resolve_index(root=project)
        sub = project / "a" / "b"
        sub.mkdir(parents=True)

        id_sub = ii.resolve_index(cwd=sub)

        assert id_sub.index_path == ident.index_path
        assert id_sub.root == ident.root
        assert id_sub.coordination_dir == ident.coordination_dir
        assert not ident.override

    def test_override_uses_flat_layout(self, tmp_path: Path, root: Path) -> None:
        """Under an override the index is flat ``<override>/<project>.json``.

        The resolver previously root-keyed this path while every writer stayed flat, so the gate coordinated a file
        nobody read. root_key survives as a path-free identity for reporting; it is no longer a directory component.
        """
        override = tmp_path / "shared"

        ident = ii.resolve_index(root=root, index_dir_override=str(override))

        assert ident.override
        assert ident.root_key == ii.root_key(ident.root)
        assert ident.index_path == override.resolve() / "proj.json"
        assert ident.coordination_dir == override.resolve() / ii.COORDINATION_DIRNAME

    def test_equal_basename_projects_share_one_override_file(self, tmp_path: Path, override_dir: Path) -> None:
        """Equal-basename projects sharing one override resolve to ONE file but keep distinct identities.

        Behaviour change from the root-keyed layout: two ``proj`` directories under a single ``CODEMAP_INDEX_DIR`` no
        longer get independent indexes. The flat convention is what every writer already used, so the alternative was a
        resolver pointing somewhere nothing was ever written; the fix is one override dir per project.
        """
        a = tmp_path / "a" / "proj"
        b = tmp_path / "b" / "proj"
        a.mkdir(parents=True)
        b.mkdir(parents=True)

        ia = ii.resolve_index(root=a, index_dir_override=str(override_dir))
        ib = ii.resolve_index(root=b, index_dir_override=str(override_dir))

        assert ia.project == ib.project == "proj"
        assert ia.root_key != ib.root_key  # identity still distinguishes them
        assert ia.index_path == ib.index_path == override_dir.resolve() / "proj.json"

    def test_equal_basename_collision_is_diagnosed_not_silent(self, tmp_path: Path, override_dir: Path) -> None:
        """With project A's index in place, resolving equal-basename project B reports ``index_root_collision``.

        The collision is surfaced instead of silently serving another project's index, and resolution never rewrites
        the occupant: A's own resolution stays diagnostic-free.
        """
        a = tmp_path / "a" / "proj"
        b = tmp_path / "b" / "proj"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        ia = ii.resolve_index(root=a, index_dir_override=str(override_dir))
        ia.index_path.write_text(json.dumps({"scan_root": str(ia.root), "tok": "A"}), encoding="utf-8")

        collided = ii.resolve_index(root=b, index_dir_override=str(override_dir))

        assert ii.resolve_index(root=a, index_dir_override=str(override_dir)).diagnostics == ()
        assert ii.INDEX_ROOT_COLLISION in [d.code for d in collided.diagnostics]
        assert json.loads(ia.index_path.read_text())["tok"] == "A"  # never overwritten by resolution

    @_skip_directory_symlink_unavailable
    def test_symlink_alias_shares_identity(self, tmp_path: Path) -> None:
        """A directory symlink resolves to the same index and root identity as its target.

        A project reached through a link must not get a second index next to the one built through the real path.
        """
        real = tmp_path / "real_proj"
        real.mkdir()
        link = tmp_path / "link_proj"
        link.symlink_to(real, target_is_directory=True)

        id_real = ii.resolve_index(root=real)
        id_link = ii.resolve_index(root=link)

        assert id_real.index_path == id_link.index_path
        assert id_real.root_key == id_link.root_key

    def test_matching_flat_index_resolves_clean(self, root: Path, override_dir: Path) -> None:
        """An existing flat index built for this same root is the target, with no diagnostic.

        Formerly this file was a read-only "legacy candidate" beside a distinct root-keyed target. Flat is now the only
        layout, so a matching occupant is simply the index.
        """
        existing = override_dir / "proj.json"
        existing.write_text(json.dumps({"scan_root": str(root.resolve())}), encoding="utf-8")

        ident = ii.resolve_index(root=root, index_dir_override=str(override_dir))

        assert ident.index_path == existing
        assert ident.diagnostics == ()
        assert json.loads(existing.read_text())["scan_root"] == str(root.resolve())

    def test_mismatched_flat_index_emits_collision_diagnostic(
        self, tmp_path: Path, root: Path, override_dir: Path
    ) -> None:
        """A flat index built for a different root is reported as a collision and left untouched.

        Resolution reports the collision; it never rewrites the path or touches the occupant's file.
        """
        other = tmp_path / "other"
        other.mkdir()
        occupant = override_dir / "proj.json"
        occupant.write_text(json.dumps({"scan_root": str(other.resolve())}), encoding="utf-8")

        ident = ii.resolve_index(root=root, index_dir_override=str(override_dir))

        assert ii.INDEX_ROOT_COLLISION in [d.code for d in ident.diagnostics]
        assert ident.index_path == occupant
        assert json.loads(occupant.read_text())["scan_root"] == str(other.resolve())

    def test_split_index_roots_diagnostic(self, tmp_path: Path) -> None:
        """Two different index paths produce ``split_index_roots``; identical paths produce none.

        The diagnostic tells a consumer its probe and the provider are reading different indexes.
        """
        a = ii.resolve_index(root=tmp_path / "a")
        b = ii.resolve_index(root=tmp_path / "b")

        diag = ii.diagnose_split_index_roots(a.index_path, b.index_path)

        assert diag is not None
        assert diag.code == ii.SPLIT_INDEX_ROOTS
        assert ii.diagnose_split_index_roots(a.index_path, a.index_path) is None


@dataclass(frozen=True, slots=True)
class _BuiltIndex:
    """Identity, scan counter, and content baseline for one already-built index."""

    ident: ii.IndexIdentity
    counter: Path
    h0: str
    m0: int


@pytest.fixture(name="built_index")
def _built_index(project: Path, shim: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _BuiltIndex:
    """Resolve one project's index and build it once via the scan shim, ready for reuse scenarios."""
    monkeypatch.setenv("CODEMAP_LOGGING", "true")
    counter = tmp_path / "counter.txt"
    ident = ii.resolve_index(root=project)
    subprocess.run(
        [sys.executable, str(shim), "--root", str(ident.root)],
        cwd=str(project),
        env=_scan_env(counter),
        check=True,
        capture_output=True,
    )
    return _BuiltIndex(ident=ident, counter=counter, h0=_sha(ident.index_path), m0=ident.index_path.stat().st_mtime_ns)


class TestBuiltIndexReuse:
    """After one build, resolving and reading the index never triggers another scan."""

    def test_scan_once_builds_index(self, built_index: _BuiltIndex) -> None:
        """One scan-index invocation records exactly one scan and leaves a readable index file.

        This pins the scanner-invocation counter oracle the reuse tests below rely on: the baseline is exactly one.
        """
        assert _count(built_index.counter) == 1
        assert built_index.ident.index_path.is_file()

    def test_subdir_resolves_to_built_index(self, built_index: _BuiltIndex, project: Path) -> None:
        """Resolving from a nested working directory returns the same index path as the repo root.

        Cross-runtime delegation depends on both runtimes agreeing on the index path even when invoked from different
        subdirectories of the same repo, not only from the repo root.
        """
        sub = project / "pkg" / "deep"
        sub.mkdir(parents=True)

        id_sub = ii.resolve_index(cwd=sub)

        assert id_sub.index_path == built_index.ident.index_path

    def test_two_runtime_reads_do_not_trigger_rebuild(self, built_index: _BuiltIndex) -> None:
        """Two successive gated reads (the Claude then the Codex runtime) never rebuild the index.

        Runtime identity only routes the log, never the index path: the scanner-invocation counter must stay at one, and
        the index bytes/mtime must be untouched after both reads.
        """
        ident = built_index.ident

        with rw.read_index(ident.index_path, timeout=5) as claude_view:
            pass
        with rw.read_index(ident.index_path, timeout=5) as codex_view:
            pass

        assert [claude_view["scan_root"], codex_view["scan_root"]] == [str(ident.root)] * 2
        assert [claude_view["project"], codex_view["project"]] == [ident.project] * 2
        assert _count(built_index.counter) == 1  # no rebuild on reuse
        assert _sha(ident.index_path) == built_index.h0
        assert ident.index_path.stat().st_mtime_ns == built_index.m0

    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_delegated_read_writes_per_runtime_log(
        self, built_index: _BuiltIndex, project: Path, tmp_path: Path, runtime: str
    ) -> None:
        """A runtime's delegated read writes its own runtime-scoped delegation log file.

        The gated read and the ``reused_index`` log record are the pair a delegating runtime performs; the record must
        land under that runtime's own subtree of the log root.
        """
        log_root = tmp_path / "logs"

        with rw.read_index(built_index.ident.index_path, timeout=5):
            pass
        rl.write_log(runtime, {"event": "reused_index"}, session="deleg", root=project, override=str(log_root))

        assert (log_root / runtime / "cli_deleg.jsonl").is_file()


# ── stale index: concurrent writers are serialized by the engine's own lease ──
def test_concurrent_scans_are_serialized_and_publish_a_valid_index(project: Path, shim: Path, tmp_path: Path) -> None:
    """Two processes racing a stale index both succeed and leave one valid index.

    This is the contention test the gate exists for. Before engine-level leasing, both
    scanners walked the AST simultaneously and the loser's ``os.replace`` won by
    accident (last-writer-wins). Now each ``scan-index`` takes the exclusive write lease
    inside ``graph.main``, so the walks are serialized whatever route invoked them.

    Serialized is not deduplicated: two explicit full-scan requests both run, and the
    counter shows 2. Skipping the second scan is the job of ``--incremental``, whose
    recheck runs inside the exclusive phase and degrades a waiter to a near-noop pass.
    """
    counter = tmp_path / "counter.txt"
    worker = tmp_path / "gate_worker.py"
    worker.write_text(_WORKER_SOURCE, encoding="utf-8")
    ident = ii.resolve_index(root=project)
    ident.index_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **_scan_env(counter),
        "CODEMAP_BIN": str(_BIN),
        "CODEMAP_TEST_SHIM": str(shim),
        "CODEMAP_TEST_ROOT": str(ident.root),
    }

    # Two separate processes race the stale index — the real delegation scenario
    # (Claude process vs Codex process), not two same-PID threads.
    procs = [
        subprocess.Popen(
            [sys.executable, str(worker), str(ident.index_path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=90) for p in procs]

    assert all(p.returncode == 0 for p in procs), outs
    labels = sorted(out.strip() for out, _err in outs)
    assert labels == ["built", "built"]
    assert _count(counter) == 2  # both ran; the lease ordered them, it did not drop one
    # The point of the lease: whichever finished last, the published file is complete.
    published = json.loads(ident.index_path.read_bytes())
    assert published.get("scan_version")
    assert published.get("file_shas")
    # No temp leaked — a crashed or superseded writer must not litter the index dir.
    assert not list(ident.index_dir.glob(".*.tmp"))
