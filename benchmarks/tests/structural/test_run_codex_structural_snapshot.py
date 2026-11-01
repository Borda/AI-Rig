"""No-model input-snapshot and diff-impact tests for the Codex structural adapter.

Split out of ``test_run_codex_structural.py`` so neither file crosses the 250 KB maintenance limit the suite enforces on
its own runners; the tests are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _codex_stream import completed_stream
from _launcher_capability import requires_private_filesystem
from _runtime_bundle import write_files

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BENCHMARKS_DIR))

# Patch seams live in the package modules the runner shim re-exports from: patching the shim
# would leave each package module's own global binding untouched. Inside the package every seam
# below is reached through its defining module, so this is the single place to patch it.


def _write_name_as_content(*paths: Path) -> None:
    """Write each file with its own basename as content."""
    for path in paths:
        path.write_text(path.name, encoding="utf-8")


def _make_arm_config(root: Path, arm: str) -> Path:
    """Create ``<root>/<arm>/config.toml`` containing the arm name and return its path."""
    config = root / arm / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(arm, encoding="utf-8")
    return config


def _make_arm_package(root: Path, arm: str) -> Path:
    """Create ``<root>/<arm>/package/payload.txt`` containing the arm name and return the package directory."""
    package = root / arm / "package"
    package.mkdir(parents=True, exist_ok=True)
    (package / "payload.txt").write_text(arm, encoding="utf-8")
    return package


def _leaf_symlink_source(tmp_path: Path, source_root: Path, real: Path) -> Path:
    """Return a source whose final component is a symlink to ``real``."""
    source = source_root / "source.json"
    source.symlink_to(real)
    return source


def _hardlinked_source(tmp_path: Path, source_root: Path, real: Path) -> Path:
    """Return a source that is a second hard link to ``real``."""
    source = source_root / "source.json"
    os.link(real, source)
    return source


def _escaping_parent_source(tmp_path: Path, source_root: Path, real: Path) -> Path:
    """Return a source reached through a directory symlink that leaves ``source_root``."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.json").write_text("outside", encoding="utf-8")
    escaping = source_root / "escaping"
    escaping.symlink_to(outside, target_is_directory=True)
    return escaping / "source.json"


def _commit_fixture(repo: Path, message: str) -> None:
    """Commit the staged files of a fixture repository with a throwaway identity."""
    identity = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]
    subprocess.run(["git", "-C", str(repo), *identity, "commit", "-m", message], check=True, capture_output=True)


def _make_locked_diff_impact_repo(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    """Create one clean tracked target plus its resolver-path locked index."""
    repo = tmp_path / "target"
    repo.mkdir()
    target = repo / "module.py"
    target.write_text("BASE\n", encoding="utf-8")
    (repo / ".gitignore").write_text("/.cache/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "module.py", ".gitignore"], check=True, capture_output=True)
    _commit_fixture(repo, "fixture")
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    index = repo / ".cache" / "codemap" / f"{repo.name}.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"git_sha": commit, "scan_version": 11}), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "target_source": {"commit": commit},
                "index": {
                    "raw_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
                    "git_sha": commit,
                    "scan_version": 11,
                },
            }
        ),
        encoding="utf-8",
    )
    task: dict[str, Any] = {
        "id": "DI-fixture",
        "type": "diff_impact",
        "stage": [{"file": "module.py", "append": "STAGED\n"}],
    }
    return repo, index, manifest, task


def _mutate_extra_tracked(repo: Path, extra: Path, tmp_path: Path) -> None:
    """Modify a tracked file that is not part of the staged change."""
    extra.write_text("MUTATED\n", encoding="utf-8")


def _mutate_extra_untracked(repo: Path, extra: Path, tmp_path: Path) -> None:
    """Create an untracked file next to the staged change."""
    (repo / "untracked.py").write_text("UNTRACKED\n", encoding="utf-8")


def _mutate_delete_stage(repo: Path, extra: Path, tmp_path: Path) -> None:
    """Delete the staged file."""
    (repo / "module.py").unlink()


def _mutate_symlink_stage(repo: Path, extra: Path, tmp_path: Path) -> None:
    """Replace the staged file with a symlink to another tracked file."""
    (repo / "module.py").unlink()
    (repo / "module.py").symlink_to(extra)


def _mutate_hardlink_stage(repo: Path, extra: Path, tmp_path: Path) -> None:
    """Replace the staged file with a hard link whose bytes equal the staged content."""
    (repo / "module.py").unlink()
    staged_copy = tmp_path / "staged-copy.py"
    staged_copy.write_text("BASE\nSTAGED\n", encoding="utf-8")
    os.link(staged_copy, repo / "module.py")


class TestInputSnapshot:
    """Archiving launch inputs into an immutable per-run snapshot."""

    def test_archives_hashes_but_never_credential_bytes(self, script_run_codex: Any, tmp_path: Path) -> None:
        """Paid provenance stores exact launch inputs while excluding auth contents.

        Launch inputs including a credential file are archived. The ledger records the credential as supplied but never
        archived, entries are sorted, and shared files are hashed under shared/.
        """
        source = tmp_path / "source"
        source.mkdir()
        manifest = source / "manifest.json"
        tasks = source / "tasks.json"
        runner = source / "runner.py"
        contract = source / "contract.py"
        launcher = source / "run-all.sh"
        index = source / "index.json"
        write_files(
            source,
            {
                "manifest.json": "manifest",
                "tasks.json": "tasks",
                "runner.py": "runner",
                "run-all.sh": "launcher",
                "index.json": "index",
                "contract.py": "contract",
            },
        )
        package = source / "package"
        package.mkdir()
        (package / "module.py").write_text("module", encoding="utf-8")
        auth = source / "auth.json"
        auth.write_text("secret-token", encoding="utf-8")
        auth.chmod(0o600)
        snapshot = script_run_codex._write_input_snapshot(
            tmp_path / "inputs",
            manifest_path=manifest,
            tasks_path=tasks,
            runner_path=runner,
            invocation_launcher_path=launcher,
            index_path=index,
            auth_source=auth,
            arm_archives={"B_auto": {"direct-cli": package}},
            additional_shared_files={"readcrop-contracts.py": contract},
        )
        payload = json.loads((tmp_path / "inputs" / "input-snapshot.json").read_text(encoding="utf-8"))
        assert payload["auth_source"] == {"archived": False, "supplied": True}
        assert not any("auth.json" in entry["archived_path"] for entry in payload["files"])
        assert "secret-token" not in json.dumps(payload)
        assert str(auth) not in json.dumps(payload)
        assert payload["files"] == sorted(payload["files"], key=lambda item: (item["role"], item["archived_path"]))
        launcher_entry = next(entry for entry in payload["files"] if entry["role"] == "invocation_launcher")
        assert launcher_entry["archived_path"] == "shared/run-all.sh"
        assert launcher_entry["sha256"] == hashlib.sha256(launcher.read_bytes()).hexdigest()
        contract_entry = next(entry for entry in payload["files"] if entry["role"] == "shared:readcrop-contracts.py")
        assert contract_entry["archived_path"] == "shared/readcrop-contracts.py"
        assert contract_entry["sha256"] == hashlib.sha256(contract.read_bytes()).hexdigest()
        assert (
            snapshot["sha256"] == hashlib.sha256((tmp_path / "inputs" / "input-snapshot.json").read_bytes()).hexdigest()
        )

    def test_uses_source_names_for_nonstructural_launch_inputs(self, script_run_codex: Any, tmp_path: Path) -> None:
        """A stage-specific adapter keeps its task and runner provenance legible.

        A readcrop tasks file and the structural runner are archived. They must appear under their own source names in
        shared/.
        """
        source = tmp_path / "source"
        source.mkdir()
        manifest = source / "manifest.json"
        tasks = source / "tasks-readcrop.json"
        runner = source / "run-codex-structural.py"
        _write_name_as_content(manifest, tasks, runner)

        script_run_codex._write_input_snapshot(
            tmp_path / "inputs",
            manifest_path=manifest,
            tasks_path=tasks,
            runner_path=runner,
            invocation_launcher_path=runner,
            index_path=None,
            auth_source=None,
            arm_archives={},
        )
        payload = json.loads((tmp_path / "inputs" / "input-snapshot.json").read_text(encoding="utf-8"))
        archived = {entry["archived_path"] for entry in payload["files"]}

        assert {"shared/tasks-readcrop.json", "shared/run-codex-structural.py"}.issubset(archived)

    @requires_private_filesystem
    def test_keeps_private_executable_launcher_for_later_b_home(
        self,
        script_run_codex: Any,
        tmp_path: Path,
    ) -> None:
        """B must remain runnable from its immutable snapshot after the admission home is removed.

        A B home's direct launcher is archived as a private 0700 single-link file. After the original runtime is
        deleted, a new home built from the frozen launcher still verifies with identical bytes.
        """
        shared = tmp_path / "shared"
        shared.mkdir()
        manifest = shared / "manifest.json"
        tasks = shared / "tasks.json"
        runner_path = shared / "runner.py"
        _write_name_as_content(manifest, tasks, runner_path)

        runtime = tmp_path / "admission-home" / "direct-cli"
        launcher = runtime / "bin" / "codemap-py"
        write_files(
            runtime,
            {
                "bin/codemap-py": "fixture:codemap-py\n",
                "bin/_exclusions.py": "fixture:_exclusions.py\n",
                "scripts/codemap_py_entry.py": "fixture:codemap_py_entry.py\n",
                "src/codemap_py/__init__.py": "fixture:__init__.py\n",
            },
        )
        launcher.chmod(0o755)
        launcher_bytes = launcher.read_bytes()

        snapshot_root = tmp_path / "inputs"
        script_run_codex._write_input_snapshot(
            snapshot_root,
            manifest_path=manifest,
            tasks_path=tasks,
            runner_path=runner_path,
            index_path=None,
            auth_source=None,
            arm_archives={"B_auto": {"direct-cli": runtime}},
        )
        archived_root = snapshot_root / "B_auto" / "direct-cli"
        archived_launcher = archived_root / "bin" / "codemap-py"
        archived_metadata = archived_launcher.lstat()
        assert stat.S_ISREG(archived_metadata.st_mode)
        assert archived_metadata.st_nlink == 1
        assert stat.S_IMODE(archived_metadata.st_mode) == 0o700
        assert archived_launcher.read_bytes() == launcher_bytes

        shutil.rmtree(runtime.parent)
        adapter = script_run_codex.CodexRunner("gpt-6.1-sol", tmp_path)
        adapter._bind_runtime_snapshot(
            snapshot_root,
            {"B_auto": {"direct-cli": archived_root}},
        )
        frozen_launcher = adapter._runtime_direct_launcher("B_auto")
        with script_run_codex.prepare_arm_home("B_auto", root=tmp_path, codemap_bin=frozen_launcher) as home:
            assert home.codemap_verified is True
            assert home.codemap_launcher_path is not None
            assert home.codemap_launcher_path.read_bytes() == launcher_bytes

    def test_launcher_validation_rejects_drift(self, script_run_codex: Any, tmp_path: Path) -> None:
        """A paid run cannot continue after its executing shell snapshot changes.

        A launcher script is validated against its recorded hash and then edited. The second validation must fail.
        """
        launcher = tmp_path / "run-all.sh"
        launcher.write_text("original", encoding="utf-8")
        expected = hashlib.sha256(launcher.read_bytes()).hexdigest()

        script_run_codex._validate_invocation_launcher(launcher, expected)
        launcher.write_text("mutated", encoding="utf-8")

        with pytest.raises(ValueError, match="invocation launcher changed"):
            script_run_codex._validate_invocation_launcher(launcher, expected)

    def test_includes_configuration_for_every_arm(self, script_run_codex: Any, tmp_path: Path) -> None:
        """Plain-arm provenance must not disappear when only treatments have package trees.

        Config files exist for three arms but package trees for only two. The ledger must list a config for every arm,
        including plain.
        """
        shared = tmp_path / "shared"
        shared.mkdir()
        manifest = shared / "manifest.json"
        tasks = shared / "tasks.json"
        runner = shared / "runner.py"
        _write_name_as_content(manifest, tasks, runner)
        arm_files = {arm: {"config.toml": _make_arm_config(tmp_path, arm)} for arm in ("A_plain", "B_auto", "C_strict")}
        arm_archives = {arm: {"package": _make_arm_package(tmp_path, arm)} for arm in ("B_auto", "C_strict")}

        script_run_codex._write_input_snapshot(
            tmp_path / "inputs",
            manifest_path=manifest,
            tasks_path=tasks,
            runner_path=runner,
            index_path=None,
            auth_source=None,
            arm_archives=arm_archives,
            arm_files=arm_files,
        )

        payload = json.loads((tmp_path / "inputs" / "input-snapshot.json").read_text(encoding="utf-8"))
        archived = {entry["archived_path"] for entry in payload["files"]}
        assert {f"{arm}/config.toml" for arm in arm_files}.issubset(archived)

    def test_copy_rejects_source_replacement_between_validation_and_open(
        self,
        script_run_codex: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Snapshot bytes must come from the exact inode that passed validation.

        The source is swapped for a symlink to different bytes just before it is opened. The copy must fail and leave
        neither a destination nor a ledger entry.
        """
        source = tmp_path / "source.json"
        source.write_text("validated", encoding="utf-8")
        replacement = tmp_path / "replacement.json"
        replacement.write_text("substituted", encoding="utf-8")
        destination = tmp_path / "snapshot" / "source.json"
        entries: list[dict[str, Any]] = []
        original_open = os.open
        replaced = False

        def _replace_before_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, *args: Any
        ) -> int:
            """Swap the authentication source for a symlink before its first matching open."""
            nonlocal replaced
            if Path(path) == source and not replaced:
                replaced = True
                source.unlink()
                source.symlink_to(replacement)
            return original_open(path, flags, *args)

        monkeypatch.setattr(script_run_codex.os, "open", _replace_before_open)

        with pytest.raises(ValueError, match=r"changed|copied securely|symlink"):
            script_run_codex._archive_snapshot_file(
                source,
                destination,
                role="fixture",
                archive_root=destination.parent,
                entries=entries,
            )

        assert not destination.exists()
        assert entries == []

    @pytest.mark.parametrize(
        "make_source",
        [
            pytest.param(_leaf_symlink_source, id="leaf-symlink"),
            pytest.param(_hardlinked_source, id="hardlink"),
            pytest.param(_escaping_parent_source, id="escaping-parent"),
        ],
    )
    def test_source_indirection_fails_closed(self, script_run_codex: Any, tmp_path: Path, make_source: Any) -> None:
        """Snapshot admission rejects direct, linked, and escaping source aliases.

        A leaf symlink, a hard link and a parent-directory symlink escaping the source root are each offered to the
        archiver. All fail closed with no output.
        """
        source_root = tmp_path / "source-root"
        source_root.mkdir()
        real = source_root / "real.json"
        real.write_text("fixture", encoding="utf-8")
        source = make_source(tmp_path, source_root, real)
        destination = tmp_path / "snapshot" / "source.json"
        entries: list[dict[str, Any]] = []

        with pytest.raises(ValueError, match=r"symlink|single-link|escaped"):
            script_run_codex._archive_snapshot_file(
                source,
                destination,
                role="fixture",
                archive_root=destination.parent,
                source_root=source_root,
                entries=entries,
            )

        assert not destination.exists()
        assert entries == []


class TestDiffImpact:
    """Diff-impact staging, admission and revert against real throwaway git repositories."""

    def test_stager_wraps_all_arms_and_restores_on_success_or_failure(
        self, script_run_codex: Any, tmp_path: Path
    ) -> None:
        """The shared Claude stager exposes identical staged bytes to every arm and always reverts.

        A real throwaway git repository is staged for three arms. Each arm sees the staged bytes, and afterwards the
        file is restored, including when the arm body raises.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        target = repo / "module.py"
        target.write_text("BASE\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "add", "module.py"], check=True, capture_output=True)
        _commit_fixture(repo, "fixture")
        task = {"id": "DI-01", "type": "diff_impact", "stage": [{"file": "module.py", "append": "STAGED\n"}]}
        stager = script_run_codex._diff_impact_stager(repo, task)
        assert stager is not None
        with stager:
            seen = [target.read_text(encoding="utf-8") for _arm in script_run_codex.CODEX_STRUCTURAL_ARMS]
        assert seen == ["BASE\nSTAGED\n"] * 3
        assert target.read_text(encoding="utf-8") == "BASE\n"

        failing = script_run_codex._diff_impact_stager(repo, task)
        assert failing is not None

        def _run_expected_failure() -> None:
            """Run the statements expected to fail as one callable."""
            with failing:
                assert target.read_text(encoding="utf-8") == "BASE\nSTAGED\n"
                raise RuntimeError("arm failure")

        with pytest.raises(RuntimeError):
            _run_expected_failure()
        assert target.read_text(encoding="utf-8") == "BASE\n"

    def test_admission_requires_exact_status_and_post_stage_bytes(self, script_run_codex: Any, tmp_path: Path) -> None:
        """DI may admit only its declared modified tracked file at captured bytes.

        In a real repository the admitted stage passes and bytes changed after admission fail. Once reverted, the clean
        repository is admitted again.
        """
        repo, index, manifest, task = _make_locked_diff_impact_repo(tmp_path)
        script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest)
        stager = script_run_codex._diff_impact_stager(repo, task)
        assert stager is not None
        with stager:
            admission = script_run_codex._capture_diff_impact_stage(repo, task)
            script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest, admission)

            (repo / "module.py").write_text("BASE\nTAMPERED\n", encoding="utf-8")
            with pytest.raises(ValueError, match="bytes changed after admission"):
                script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest, admission)

        script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest)

    def test_contamination_persists_stage_and_worktree_evidence(self, script_run_codex: Any, tmp_path: Path) -> None:
        """A DI rejection retains enough evidence to diagnose the mutated stage.

        The transport edits the staged file during the run. The result is marked contaminated and carries the stage,
        changed paths and status evidence.
        """
        repo, index, manifest, task = _make_locked_diff_impact_repo(tmp_path)
        task["prompt"] = "Inspect the staged change."
        stager = script_run_codex._diff_impact_stager(repo, task)
        assert stager is not None

        def _transport(_command: list[str], **_kwargs: Any) -> str:
            """Return fixture transport evidence; no provider call."""
            (repo / "module.py").write_text("BASE\nSTAGED\nTAMPERED\n", encoding="utf-8")
            return completed_stream()

        with stager:
            admission = script_run_codex._capture_diff_impact_stage(repo, task)
            runner = script_run_codex.CodexRunner(
                "gpt-6.1-sol",
                repo,
                index_path=index,
                manifest_path=manifest,
                transport=_transport,
            )
            result = runner.run(task, "A_plain", diff_impact_stage=admission)

        assert result.contaminated is True
        assert result.stage_evidence == {
            "stage": task["stage"],
            "changed_paths": ["module.py"],
            "expected_status": {"module.py": " M"},
            "observed_status": {"module.py": " M"},
        }

    @pytest.mark.parametrize(
        ("mutate", "error"),
        [
            pytest.param(_mutate_extra_tracked, "unexpected worktree status", id="extra-tracked"),
            pytest.param(_mutate_extra_untracked, "unexpected worktree status", id="extra-untracked"),
            pytest.param(_mutate_delete_stage, "unexpected worktree status", id="delete-stage"),
            pytest.param(_mutate_symlink_stage, "unexpected worktree status", id="symlink-stage"),
            pytest.param(_mutate_hardlink_stage, "unlinked regular tracked files", id="hardlink-stage"),
        ],
    )
    def test_admission_rejects_worktree_mutations(
        self, script_run_codex: Any, tmp_path: Path, mutate: Any, error: str
    ) -> None:
        """Extra paths and destructive type changes cannot hide behind DI admission.

        A real repository with a staged change is mutated five ways: extra tracked change, untracked file, deletion,
        symlink and hard link. Each fails admission with its message, and a revert makes admission pass again.
        """
        repo, index, manifest, task = _make_locked_diff_impact_repo(tmp_path)
        extra = repo / "extra.py"
        extra.write_text("BASE\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "extra.py"], check=True, capture_output=True)
        _commit_fixture(repo, "extra")
        commit = script_run_codex._repo_sha(repo)
        index.write_text(json.dumps({"git_sha": commit, "scan_version": 11}), encoding="utf-8")
        manifest.write_text(
            json.dumps(
                {
                    "target_source": {"commit": commit},
                    "index": {
                        "raw_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
                        "git_sha": commit,
                        "scan_version": 11,
                    },
                }
            ),
            encoding="utf-8",
        )
        stager = script_run_codex._diff_impact_stager(repo, task)
        assert stager is not None
        with stager:
            admission = script_run_codex._capture_diff_impact_stage(repo, task)
            mutate(repo, extra, tmp_path)
            with pytest.raises(ValueError, match=error):
                script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest, admission)

        subprocess.run(
            ["git", "-C", str(repo), "checkout", "--", "module.py", "extra.py"], check=True, capture_output=True
        )
        (repo / "untracked.py").unlink(missing_ok=True)
        script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest)

    def test_git_status_always_includes_untracked_files(
        self, script_run_codex: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A user Git configuration cannot hide an untracked contaminant from admission.

        The subprocess runner is faked. Git status must be invoked with the porcelain, NUL and all-untracked flags so
        user configuration cannot hide files.
        """
        commands: list[list[str]] = []

        def _run(command: list[str], **_kwargs: Any) -> Any:
            """Record argv and return an empty successful subprocess result."""
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(script_run_codex.subprocess, "run", _run)
        assert script_run_codex._git_porcelain_status(tmp_path) == {}
        assert commands == [["git", "-C", str(tmp_path), "status", "--porcelain=v1", "-z", "--untracked-files=all"]]

    def test_git_status_failure_rejects_admission(
        self, script_run_codex: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Git-status failures cannot be treated as a clean worktree.

        A faked git exits non-zero. The failure must raise instead of reading as a clean tree.
        """
        monkeypatch.setattr(
            script_run_codex.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="fixture failure"),
        )

        with pytest.raises(ValueError, match="could not verify worktree cleanliness"):
            script_run_codex._git_porcelain_status(tmp_path)

    @requires_private_filesystem
    def test_preflight_exercises_stage_admission_and_strict_revert(
        self, script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-model DI preflight proves enter, exact admission, and clean restoration together.

        Preflight runs stage, admission and revert for every arm on a real repository with a fake home. Each arm admits
        once and the repository is clean afterwards.
        """
        repo, index, manifest, task = _make_locked_diff_impact_repo(tmp_path)
        runner = script_run_codex.CodexRunner("gpt-6.1-sol", repo, index_path=index, manifest_path=manifest)
        admitted: list[str] = []

        def _prepare(arm: str, *, diff_impact_stage: Any = None) -> Any:
            """Prepare and record an isolated arm home."""
            assert diff_impact_stage is not None
            script_run_codex._validate_locked_runtime(repo, index, arm, manifest, diff_impact_stage)
            admitted.append(arm)
            home_path = tmp_path / f"home-{arm}"
            home_path.mkdir()
            home_path.chmod(0o700)
            return script_run_codex.ArmHome(arm, home_path, {}, False)

        monkeypatch.setattr(runner, "_prepare_verified_home", _prepare)
        runner.preflight_diff_impact_stages([task], script_run_codex.CODEX_STRUCTURAL_ARMS)

        assert admitted == list(script_run_codex.CODEX_STRUCTURAL_ARMS)
        script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest)
