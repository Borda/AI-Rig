"""Verify managed reader approvals without changing the real Codex home or using the network."""

from __future__ import annotations

import ast
import hashlib
import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, FILE_SYMLINKS_AVAILABLE


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_github_read_rules.py"
CODEX = shutil.which("codex")


def _installed_plugin(home: Path, version: str) -> Path:
    """Create the minimal installed identity and reader checked by setup."""
    root = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / version
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "codex-rig", "version": version}), encoding="utf-8"
    )
    (root / "shared").mkdir()
    (root / "shared" / "github_read.py").write_text('"""Unused fixture reader."""\n', encoding="utf-8")
    (root / "shared" / "collect_pr.py").write_bytes(b"# Unused fixture collector.\n")
    records = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": f"{path.stat().st_mode & 0o777:04o}",
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    reader = next(record for record in records if record["path"] == "shared/github_read.py")
    (root / "package-manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "plugin": "codex-rig",
                "version": version,
                "files": records,
                "skills": [],
                "roles": [],
                "bootstrap": {"protocol": "fixture", "helper": reader["path"], "sha256": reader["sha256"]},
                "generator": {"version": "fixture", "path": reader["path"], "sha256": reader["sha256"]},
            }
        ),
        encoding="utf-8",
    )
    return root


def _run(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Execute the public installer CLI against a test-owned home."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-home", str(home), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _rule_pattern(path: Path) -> list[object]:
    """Read the generated Starlark rule's literal argument pattern."""
    (statement,) = ast.parse(path.read_text(encoding="utf-8")).body
    assert isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
    call = statement.value
    assert isinstance(call.func, ast.Name) and call.func.id == "prefix_rule"
    values = {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords}
    assert values["decision"] == "allow"
    return values["pattern"]


def test_install_upgrade_and_clear_manage_only_reader_approval(tmp_path: Path) -> None:
    """Refresh the literal installed path while preserving unrelated approval bytes."""
    home = tmp_path / "codex home"
    first = _installed_plugin(home, "1.2.3")
    rules = home / "rules"
    rules.mkdir()
    default = rules / "default.rules"
    original = b'# user content\r\nprefix_rule(pattern=["git", "status"], decision="allow")\r\n'
    default.write_bytes(original)
    managed = rules / "codex-rig-github-read.rules"

    result = _run(home, "--plugin-root", str(first))
    assert result.returncode == 0, result.stderr
    pattern = _rule_pattern(managed)
    assert pattern[0] == ["python", "python3"]
    reader = first / "shared" / "github_read.py"
    expected_paths = list(dict.fromkeys([str(reader), reader.as_posix()]))
    assert pattern[1] == (expected_paths[0] if len(expected_paths) == 1 else expected_paths)
    assert len(pattern) == 2
    assert b"\r\n" not in managed.read_bytes()
    assert default.read_bytes() == original
    initial = managed.read_bytes()
    assert _run(home, "--plugin-root", str(first)).returncode == 0
    assert managed.read_bytes() == initial
    assert not (home / "backups" / "codex-rig").exists()

    second = _installed_plugin(home, "1.2.4")
    assert _run(home, "--plugin-root", str(second)).returncode == 0
    assert "1.2.4" in managed.read_text(encoding="utf-8")
    assert "1.2.3" not in managed.read_text(encoding="utf-8")
    backups = list((home / "backups" / "codex-rig").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == initial

    assert _run(home, "--remove").returncode == 0
    assert not managed.exists()
    assert default.read_bytes() == original
    assert _run(home, "--remove").returncode == 0


def test_install_migrates_exact_legacy_prefix_and_preserves_other_rules(tmp_path: Path) -> None:
    """Remove obsolete UI-saved wrapper rules only after backing up their original bytes."""
    home = tmp_path / "home"
    current = _installed_plugin(home, "2.0.0")
    old_reader = current.parent / "1.2.3" / "shared" / "github_read.py"
    legacy = f'prefix_rule(pattern={json.dumps(["python", str(old_reader)])}, decision="allow")\r\n'.encode()
    other = b'prefix_rule(pattern=["python", "other.py"], decision="allow")\r\n'
    narrow = f'prefix_rule(pattern={json.dumps(["python", str(old_reader), "--out", "one.log"])}, decision="allow")\n'.encode()
    default = home / "rules" / "default.rules"
    default.parent.mkdir()
    original = other + legacy + narrow
    default.write_bytes(original)

    result = _run(home, "--plugin-root", str(current))

    assert result.returncode == 0, result.stderr
    assert default.read_bytes() == other + narrow
    backups = list((home / "backups" / "codex-rig").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert "migrated" in result.stdout


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(b"# user-owned file\n", id="unowned"),
        pytest.param(b"# codex-rig:github-read sha256=invalid\n", id="invalid-marker"),
    ],
)
def test_install_rejects_unowned_or_invalid_managed_file(tmp_path: Path, existing: bytes) -> None:
    """Never overwrite a colliding filename without verified ownership and integrity."""
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    managed = home / "rules" / "codex-rig-github-read.rules"
    managed.parent.mkdir()
    managed.write_bytes(existing)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert managed.read_bytes() == existing
    assert not (home / "backups").exists()


def test_clear_rejects_edited_owned_rule(tmp_path: Path) -> None:
    """Keep edited permissions available for human reconciliation instead of deleting them."""
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    managed = home / "rules" / "codex-rig-github-read.rules"
    edited = managed.read_bytes() + b"# local modification\n"
    managed.write_bytes(edited)

    result = _run(home, "--remove")

    assert result.returncode != 0
    assert managed.read_bytes() == edited


def test_clear_rejects_rehashed_unrecognized_rule_body(tmp_path: Path) -> None:
    """A checksum must not authorize teardown of an arbitrary locally authored rule body."""
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    managed = home / "rules" / "codex-rig-github-read.rules"
    _, _, body = managed.read_bytes().partition(b"\n")
    body += b'prefix_rule(pattern=["python"], decision="allow")\n'
    edited = b"# codex-rig:github-read sha256=" + hashlib.sha256(body).hexdigest().encode() + b"\n" + body
    managed.write_bytes(edited)

    result = _run(home, "--remove")

    assert result.returncode != 0
    assert managed.read_bytes() == edited


def test_clear_rejects_regular_file_home(tmp_path: Path) -> None:
    """Malformed target homes must fail even when no managed rule appears to exist."""
    home = tmp_path / "not-a-directory"
    home.write_bytes(b"user file")

    result = _run(home, "--remove")

    assert result.returncode != 0
    assert home.read_bytes() == b"user file"


def test_install_rejects_plugin_outside_target_home(tmp_path: Path) -> None:
    """Refuse to authorize a script merely because the caller supplies its path."""
    foreign = _installed_plugin(tmp_path / "other", "1.2.3")
    target = tmp_path / "target"

    result = _run(target, "--plugin-root", str(foreign))

    assert result.returncode != 0
    assert not target.exists()


def test_windows_rule_arguments_and_legacy_migration_on_every_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep native Windows and POSIX argument spellings usable without host-dependent conversion."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    functions = runpy.run_path(str(SCRIPT))
    home = PureWindowsPath(r"D:\User Space\codex")
    reader = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "1.2.3" / "shared" / "github_read.py"
    payload = functions["render_rules"](reader)
    rule_file = tmp_path / "windows.rules"
    rule_file.write_bytes(payload)

    assert _rule_pattern(rule_file) == [["python", "python3"], [str(reader), reader.as_posix()]]
    assert b"\r\n" not in payload
    legacy = f'prefix_rule(pattern={json.dumps(["python3", str(reader)])}, decision="allow")\r\n'.encode()
    unchanged = b"# user comment\r\n"
    assert functions["strip_legacy_rules"](unchanged + legacy, home) == unchanged


@pytest.mark.parametrize(
    "component",
    [
        pytest.param(
            "rules",
            id="rules-directory-link",
            marks=pytest.mark.skipif(
                not DIRECTORY_SYMLINKS_AVAILABLE,
                reason="filesystem cannot create directory symlinks",
            ),
        ),
        pytest.param(
            "reader",
            id="reader-file-link",
            marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"),
        ),
        pytest.param(
            "default",
            id="default-file-link",
            marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"),
        ),
    ],
)
def test_install_rejects_linked_rule_or_reader_paths(tmp_path: Path, component: str) -> None:
    """Avoid redirecting either the approved executable or permission writes outside their owned paths."""
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    rules = home / "rules"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = b"# external bytes\n"
    target = outside / "file"
    target.write_bytes(original)
    if component == "rules":
        rules.symlink_to(outside, target_is_directory=True)
    else:
        rules.mkdir()
        link = rules / "default.rules" if component == "default" else root / "shared" / "github_read.py"
        if link.exists():
            link.unlink()
        link.symlink_to(target)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert "linked path" in result.stderr
    assert target.read_bytes() == original
    assert not (rules / "codex-rig-github-read.rules").exists()


def test_setup_rejects_wrong_manifest_identity_before_migration(tmp_path: Path) -> None:
    """Do not remove legacy permissions when the new installation has the wrong identity."""
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "another-plugin", "version": "1.2.3"}), encoding="utf-8"
    )
    default = home / "rules" / "default.rules"
    default.parent.mkdir()
    original = f'prefix_rule(pattern={json.dumps(["python", str(root / "shared" / "github_read.py")])}, decision="allow")\n'.encode()
    default.write_bytes(original)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert "identity" in result.stderr
    assert default.read_bytes() == original
    assert not (default.parent / "codex-rig-github-read.rules").exists()


def test_setup_rejects_invalid_backup_path_before_creating_approval(tmp_path: Path) -> None:
    """Check migration backup prerequisites before granting a new persistent approval."""
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    default = home / "rules" / "default.rules"
    default.parent.mkdir()
    legacy = f'prefix_rule(pattern={json.dumps(["python", str(root / "shared/github_read.py")])}, decision="allow")\n'.encode()
    default.write_bytes(legacy)
    (home / "backups").write_bytes(b"unrelated existing file")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 2
    assert "not a directory" in result.stderr
    assert default.read_bytes() == legacy
    assert not (default.parent / "codex-rig-github-read.rules").exists()
    assert (home / "backups").read_bytes() == b"unrelated existing file"


def test_setup_reports_completed_permission_write_if_later_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep completed rule updates visible when a later filesystem replacement fails."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    module = runpy.run_path(str(SCRIPT))
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    default = home / "rules" / "default.rules"
    default.parent.mkdir()
    legacy = f'prefix_rule(pattern={json.dumps(["python", str(root / "shared/github_read.py")])}, decision="allow")\n'.encode()
    default.write_bytes(legacy)
    replace = module["os"].replace

    def fail_default_replace(source: Path, destination: Path) -> None:
        """Simulate the OS rejecting only the second rules-file replacement."""
        if destination == default:
            raise PermissionError("default replacement denied")
        replace(source, destination)

    monkeypatch.setattr(module["os"], "replace", fail_default_replace)
    result = module["main"](["--codex-home", str(home), "--plugin-root", str(root)])

    output = capsys.readouterr()
    assert result == 2
    assert (default.parent / "codex-rig-github-read.rules").exists()
    assert "rules created:" in output.out
    assert "default replacement denied" in output.err
    assert "partial" in output.err
    assert default.read_bytes() == legacy
    backups = list((home / "backups/codex-rig").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == legacy


@pytest.mark.parametrize("component", ["reader", "manifest"])
def test_setup_rejects_unverified_package_bytes(tmp_path: Path, component: str) -> None:
    """Never approve a cache with a changed reader or missing package integrity evidence."""
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    if component == "reader":
        (root / "shared/github_read.py").write_bytes(b'print("substituted code")\n')
    else:
        (root / "package-manifest.json").unlink()

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 2
    assert not (home / "rules").exists()


def test_explicit_pr_approval_survives_upgrade_and_is_removed_on_clear(tmp_path: Path) -> None:
    """Keep collector grants PR-specific, opt-in, backed up, and refreshable without new consent."""
    home = tmp_path / "home"
    first = _installed_plugin(home, "1.2.3")
    target = "https://github.com/example/project/pull/7"
    managed = home / "rules" / "codex-rig-pr-collection.rules"
    assert _run(home, "--plugin-root", str(first)).returncode == 0
    assert not managed.exists()
    result = _run(home, "--plugin-root", str(first), "--approve-pr", target)
    assert result.returncode == 0, result.stderr
    pattern = _rule_pattern(managed)
    assert pattern[0] == ["python", "python3"]
    collector = first / "shared" / "collect_pr.py"
    paths = list(dict.fromkeys([str(collector), collector.as_posix()]))
    assert pattern[1] == (paths[0] if len(paths) == 1 else paths)
    assert pattern[2:] == ["--target", [target]]
    original = managed.read_bytes()
    assert _run(home, "--plugin-root", str(first), "--approve-pr", target).returncode == 0
    assert managed.read_bytes() == original
    assert not (home / "backups").exists()
    second = _installed_plugin(home, "1.2.4")
    assert _run(home, "--plugin-root", str(second)).returncode == 0
    assert _rule_pattern(managed)[2:] == ["--target", [target]]
    assert "1.2.4" in managed.read_text(encoding="utf-8")
    assert "1.2.3" not in managed.read_text(encoding="utf-8")
    assert original in [path.read_bytes() for path in (home / "backups/codex-rig").iterdir()]
    assert _run(home, "--remove").returncode == 0
    assert not managed.exists()


@pytest.mark.parametrize(
    "target", ["7", "https://github.com/example/project/pull/0", "https://github.com/example/project/pull/7?x=1", "*"]
)
def test_pr_setup_rejects_noncanonical_targets_before_any_permission_write(tmp_path: Path, target: str) -> None:
    """Reject ambiguous identities and URL suffixes before granting any reader or collector access."""
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    result = _run(home, "--plugin-root", str(root), "--approve-pr", target)
    assert result.returncode == 2
    assert not (home / "rules").exists()


def test_pr_setup_refuses_modified_rules_before_updating_reader(tmp_path: Path) -> None:
    """Do not partially expand permissions when the PR allowlist has unrecognized content."""
    home = tmp_path / "home"
    first = _installed_plugin(home, "1.2.3")
    target = "https://github.com/example/project/pull/7"
    assert _run(home, "--plugin-root", str(first), "--approve-pr", target).returncode == 0
    managed = home / "rules" / "codex-rig-pr-collection.rules"
    modified = managed.read_bytes() + b"# user edit\n"
    managed.write_bytes(modified)
    reader = home / "rules" / "codex-rig-github-read.rules"
    original_reader = reader.read_bytes()
    second = _installed_plugin(home, "1.2.4")
    result = _run(home, "--plugin-root", str(second))
    assert result.returncode == 2
    assert managed.read_bytes() == modified
    assert reader.read_bytes() == original_reader


@pytest.mark.skipif(CODEX is None, reason="Codex policy checker is unavailable")
@pytest.mark.integration
def test_real_policy_engine_matches_only_approved_collector_and_pr(tmp_path: Path) -> None:
    """Prove emitted Starlark grants the approved command while preserving unrelated boundaries."""
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    target = "https://github.com/example/project/pull/7"
    assert _run(home, "--plugin-root", str(root), "--approve-pr", target).returncode == 0
    rules = home / "rules" / "codex-rig-pr-collection.rules"
    collector = str(root / "shared" / "collect_pr.py")
    cases = [
        (["python", collector, "--target", target, "--out", "report-one", "--checkout"], True),
        (["python3", collector, "--target", target, "--out", "report-two"], True),
        (["python", collector, "--target", "https://github.com/example/project/pull/8"], False),
        (["python", collector, "--target", "https://github.com/example/other/pull/7"], False),
        (["python", collector, "--target", "7"], False),
        (["python", str(root / "shared" / "github_read.py"), "--target", target], False),
        (["rtk", "python", collector, "--target", target], False),
    ]
    for command, allowed in cases:
        result = subprocess.run(
            [CODEX, "execpolicy", "check", "--rules", str(rules), "--", *command],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert (payload.get("decision") == "allow") is allowed, command

    restriction = home / "rules" / "admin.rules"
    for decision in ("prompt", "forbidden"):
        restriction.write_bytes(f'prefix_rule(pattern=["python"], decision="{decision}")\n'.encode("utf-8"))
        result = subprocess.run(
            [CODEX, "execpolicy", "check", "--rules", str(rules), "--rules", str(restriction), "--", *cases[0][0]],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["decision"] == decision


def test_pr_grants_reject_rehashed_broad_pattern(tmp_path: Path) -> None:
    """A checksum does not turn an arbitrary collector-wide rule into an owned PR grant."""
    home = tmp_path / "home"
    root = _installed_plugin(home, "1.2.3")
    managed = home / "rules" / "codex-rig-pr-collection.rules"
    managed.parent.mkdir()
    body = b'prefix_rule(pattern=["python"], decision="allow")\n'
    original = b"# codex-rig:pr-collection sha256=" + hashlib.sha256(body).hexdigest().encode() + b"\n" + body
    managed.write_bytes(original)
    result = _run(home, "--plugin-root", str(root))
    assert result.returncode == 2
    assert managed.read_bytes() == original
    assert not (managed.parent / "codex-rig-github-read.rules").exists()
