"""Acceptance checks for the complete role-card-injected Codex Rig release."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
from _modules import load_module, load_module_scoped
from _skill_text import missing_phrases

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
REPOSITORY_ROOT = PLUGIN_ROOT.parents[1]
CALIBRATION_DIR = PLUGIN_ROOT / "runtime" / "calibration"
BEHAVIORAL_CASES_PATH = CALIBRATION_DIR / "behavioral-cases.json"
COMMIT_TEMPLATE_PATH = PLUGIN_ROOT / "shared" / "commit-response-template.md"
EXPECTED_SKILLS = (
    "agent-shims",
    "challenge-resolve",
    "assess",
    "audit",
    "calibrate",
    "code-remediate",
    "code-review",
    "implement",
    "investigate",
    "kaggle",
    "manage",
    "optimize",
    "profile",
    "release",
    "research",
    "sync",
)
EXPECTED_ROLES = {
    "challenger": ("high", "read-only"),
    "cicd-steward": ("high", "workspace-write"),
    "curator": ("high", "workspace-write"),
    "data-steward": ("high", "workspace-write"),
    "delegation-lead": ("high", "workspace-write"),
    "doc-scribe": ("high", "workspace-write"),
    "linting-expert": ("medium", "workspace-write"),
    "oss-shepherd": ("high", "read-only"),
    "qa-specialist": ("medium", "workspace-write"),
    "scientist": ("high", "workspace-write"),
    "security-auditor": ("high", "read-only"),
    "solution-architect": ("high", "read-only"),
    "squeezer": ("medium", "read-only"),
    "sw-engineer": ("medium", "workspace-write"),
    "web-explorer": ("medium", "read-only"),
}
#: Dependencies a skill needs beyond the relative paths its SKILL.md names.
SKILL_EXTRA_DEPENDENCIES = {"code-review": ("validate_artifacts.py",)}
RELATIVE_DEPENDENCY = re.compile(r"`((?:\.\./\.\./(?:shared|runtime)/|references/)[A-Za-z0-9_./-]+)")
#: The documented one-command staging-and-commit form that the file-free commit tests execute.
COMMIT_COMMAND = "rtk git add -- <paths> && rtk git commit --cleanup=verbatim -m <message>"
#: Calibration cases pinned by this release: id -> (target skill or role, expected finding ids).
CALIBRATION_CASES = {
    "adversarial-loop-independent-closure": (
        "challenge-resolve",
        [
            "initial-score-omitted",
            "fixed-pending-verification-counted-closed",
            "same-reviewer-presented-as-independent",
            "current-diff-coverage-missing",
        ],
    ),
    "adversarial-loop-stop-conditions": (
        "challenge-resolve",
        [
            "unauthorized-structural-fix",
            "unresolved-severe-finding-not-escalated",
            "repeated-finding-root-cause-missing",
            "plateau-or-nonconvergence-not-stopped",
            "iteration-cap-exceeded",
        ],
    ),
    "adversarial-loop-owner-and-response-binding": (
        "challenge-resolve",
        ["active-loop-owner-not-bound", "authentic-finding-prose-ignored"],
    ),
    "adversarial-loop-progress-transcript": (
        "challenge-resolve",
        ["cumulative-progress-history-missing", "old-new-split-missing", "progress-weighted-score-missing"],
    ),
    "adversarial-loop-premature-progress-table": ("challenge-resolve", ["premature-convergence-table"]),
    "adversarial-loop-five-step-convergence": ("challenge-resolve", []),
    "adversarial-loop-closure-and-discovery": (
        "challenge-resolve",
        [
            "original-example-does-not-close-invariant",
            "sibling-route-unchecked",
            "new-finding-source-age-unclassified",
            "first-pass-coverage-incomplete",
        ],
    ),
    "challenge-resolve-large-scope-premature-stop": (
        "challenge-resolve",
        ["route-specific-size-limit-treated-as-global", "chunkable-scope-stopped-before-review"],
    ),
    "audit-prompt-efficiency-overcompression": (
        "audit",
        [
            "length-only-optimization",
            "semantic-value-guard-missing",
            "loaded-reference-cost-omitted",
            "adversarial-review-missing",
        ],
    ),
    "manage-implicit-history-rewrite": ("manage", ["history-rewrite-not-explicitly-authorized"]),
    "recurrence-initial-obstacle": ("implement", ["initial-obstacle-not-recorded"]),
    "recurrence-second-occurrence-investigate": (
        "investigate",
        ["recurrence-investigation-required", "root-cause-evidence-required", "recurrence-reset-evidence-missing"],
    ),
    "recurrence-third-occurrence-human-handoff": (
        "delegation-lead",
        [
            "recurrence-human-handoff-required",
            "human-handoff-missing",
            "attempted-actions-missing",
            "shared-obstacle-evidence-missing",
        ],
    ),
    "model-stall-advisory-escalation": (
        "delegation-lead",
        ["reasoning-progress-not-assessed", "model-stall-escalation-required", "stall-ledger-missing"],
    ),
    "model-stall-human-handoff": (
        "delegation-lead",
        [
            "post-escalation-human-handoff-required",
            "model-stall-handoff-evidence-missing",
            "next-step-proposal-missing",
        ],
    ),
    "model-stall-progress-without-closure": (
        "delegation-lead",
        [
            "productive-progress-misclassified",
            "false-advisory-escalation",
            "unfinished-acceptance-must-remain-recorded",
        ],
    ),
    "model-stall-user-directed-progress": (
        "delegation-lead",
        ["user-directed-progress-misclassified", "false-advisory-escalation", "user-decision-evidence-missing"],
    ),
    "model-stall-advisory-route-safety": (
        "delegation-lead",
        [
            "advisory-read-only-sandbox-unverified",
            "advisory-mutation-fence-missing",
            "human-handoff-required-when-route-unavailable",
        ],
    ),
    "delegation-lead-bounded-dispatch-wave": (
        "delegation-lead",
        [
            "specialist-routes-or-packs-not-fixed",
            "specialist-fanout-expanded",
            "specialist-ownership-overlap",
            "dependent-work-dispatched-early",
            "specialist-approval-bypassed",
            "second-specialist-wave-forbidden",
            "specialist-handoff-join-missing",
            "serial-fallback-gates-weakened",
        ],
    ),
}
_CALIBRATION_CASE_PARAMS = [
    pytest.param(case_id, target, findings, id=case_id) for case_id, (target, findings) in CALIBRATION_CASES.items()
]
_SHIPPED_MARKDOWN = [
    pytest.param(path, id=path.relative_to(PLUGIN_ROOT).as_posix())
    for path in [*sorted((PLUGIN_ROOT / "skills").rglob("*.md")), *sorted((PLUGIN_ROOT / "shared").rglob("*.md"))]
]
_SKILL_DEPENDENCY_CASES = [
    pytest.param(skill_id, SKILL_EXTRA_DEPENDENCIES.get(skill_id, ()), id=skill_id)
    for skill_id in EXPECTED_SKILLS
    if skill_id != "agent-shims"
]
_COMPLETED_MERGE = {
    "conflicts_detected": True,
    "status": "completed",
    "post_merge_head": "merge-oid",
    "merge_commit": "merge-oid",
    "resolved_paths": ["src/conflicted.py"],
}
_MERGE_EVIDENCE = ["merge-prestage.md", "pytest.log"]
_BASE_OID, _HEAD_OID, _POST_MERGE_OID = "b" * 40, "a" * 40, "c" * 40
_BOUND_MERGE = {
    **_COMPLETED_MERGE,
    "authorization": "user-confirmed",
    "target_oid": _BASE_OID,
    "pre_merge_head": _HEAD_OID,
    "post_merge_head": _POST_MERGE_OID,
    "merge_commit": _POST_MERGE_OID,
    "evidence": _MERGE_EVIDENCE,
}
_HOSTILE_COMMIT_MESSAGE = (
    "test(cli): preserve literal commit text\n\n"
    "Changes:\n- Keep 'apostrophes', \"quotes\", C:\\work\\path, and Unicode: příliš 日本語.\n"
    "- Preserve $(touch shell-expanded), `touch backtick-expanded`, $HOME, %PATH%, & | ; < >.\n\n"
    "Impact:\n- No evaluation or lossy transport.\n\n"
    "Verification:\n- Exact message and no side-effect files.\n"
    "# Keep this comment-looking line and trailing spaces.  \n\n"
    "Residual limits:\n- None known\n\n---\n\n"
    "Co-authored-by: Codex <codex@openai.com>"
)
_RECOVERY_MESSAGE = "test(cli): preserve author's text\n\nCo-authored-by: Codex <codex@openai.com>"
_ADD_REVIEWED = ["git", "add", "--", "reviewed.txt"]
_skip_posix_shell_unavailable = pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell unavailable")
_skip_rtk_unavailable = pytest.mark.skipif(shutil.which("rtk") is None, reason="RTK executable unavailable")


def _load_json(path: Path) -> dict[str, Any]:
    """Load one UTF-8 JSON object."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _normalized_text(path: Path) -> str:
    """Collapse Markdown wrapping while preserving semantic token order.

    Example:
        >>> _normalized_text(PLUGIN_ROOT / "README.md").startswith("#")
        True
    """
    return " ".join(path.read_text(encoding="utf-8").split())


def _write_payload_manifest(package_root: Path, *relative_paths: str) -> None:
    """Declare the exact synthetic publication files inspected by the validator."""
    payload = {"files": [{"path": relative} for relative in relative_paths]}
    (package_root / "package-manifest.json").write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def _write_merge_resolution(path: Path, **overrides: object) -> dict[str, object]:
    """Write one complete merge-resolution fixture with explicit override fields."""
    payload: dict[str, object] = {
        "schema_version": 1,
        "conflicts_detected": False,
        "status": "not-needed",
        "authorization": "not-required",
        "base_remote_ref": "upstream/main",
        "target_oid": "base-oid",
        "pre_merge_head": "head-oid",
        "post_merge_head": "head-oid",
        "merge_commit": None,
        "resolved_paths": [],
        "unmerged_paths": [],
        "evidence": ["merge-tree.txt"],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """Parse the flat scalar fields used by packaged skill and role cards.

    Example:
        >>> _parse_frontmatter(PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md")["name"]
        'code-review'
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines, path
    assert lines[0] == "---", path
    closing_index = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:closing_index]:
        key, separator, value = line.partition(":")
        assert separator, (path, line)
        assert key, (path, line)
        assert key not in fields, (path, line)
        fields[key] = value.strip()
    return fields


def _expected_role_records() -> list[dict[str, object]]:
    """Build the manifest role records implied by the role cards on disk and the expected runtime policy."""
    return [
        {
            "id": role_id,
            "path": f"roles/{role_id}/ROLE.md",
            "sha256": hashlib.sha256((PLUGIN_ROOT / "roles" / role_id / "ROLE.md").read_bytes()).hexdigest(),
            "runtime": {
                "model_reasoning_effort": effort,
                "approval_policy": "on-request",
                "sandbox_mode": sandbox_mode,
            },
        }
        for role_id, (effort, sandbox_mode) in EXPECTED_ROLES.items()
    ]


def _is_retired_payload_path(relative: Path) -> bool:
    """Tell whether a payload path belongs to the retired manager, MCP, or generated-shim layouts."""
    return (
        relative.parts[0] in {"manager", "mcp", "shims"}
        or relative.as_posix() == ".mcp.json"
        or (relative.name.startswith("codex-rig-") and relative.suffix == ".toml")
    )


def _dependency_defects(
    skill_path: Path, dependencies: Iterable[str], recorded_paths: set[str]
) -> list[tuple[str, str]]:
    """Return (defect, dependency) pairs for dependencies that leave the plugin, are missing, or are unmanifested."""
    defects = []
    for relative in sorted(dependencies):
        dependency = (skill_path.parent / relative).resolve()
        if not dependency.is_relative_to(PLUGIN_ROOT):
            defects.append(("outside-plugin", relative))
        elif not dependency.is_file():
            defects.append(("missing-file", relative))
        elif dependency.relative_to(PLUGIN_ROOT).as_posix() not in recorded_paths:
            defects.append(("not-manifested", relative))
    return defects


def _skill_document(skill_id: str) -> tuple[Path, ...]:
    """Return one skill's SKILL.md followed by its trigger-loaded reference files, which publish together."""
    skill_dir = PLUGIN_ROOT / "skills" / skill_id
    return (skill_dir / "SKILL.md", *sorted((skill_dir / "references").glob("*.md")))


def _with_identity(segments: list[list[str]]) -> list[list[str]]:
    """Give each Git segment a throwaway author identity so commits never depend on host configuration."""
    identity = ["-c", "user.name=Contract Test", "-c", "user.email=contract@example.invalid"]
    return [[segment[0], *identity, *segment[1:]] for segment in segments]


def _argv_calls(commands: list[list[str]]) -> list[list[str]]:
    """Run each segment as its own argv call, the form for tools that cannot chain commands."""
    return commands


def _posix_calls(commands: list[list[str]]) -> list[list[str]]:
    """Chain the segments with && inside one POSIX shell call, using the documented apostrophe encoding."""
    quoted = [shlex.join(command) for command in commands]
    return [[shutil.which("sh"), "-c", " && ".join(quoted)]]


def _rtk_calls(commands: list[list[str]]) -> list[list[str]]:
    """Run each segment through the RTK executable."""
    return [[shutil.which("rtk"), *command] for command in commands]


def _git(repo: Path, env: dict[str, str], *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Run one Git command inside the disposable repository and capture its output."""
    return subprocess.run(["git", *args], cwd=repo, env=env, check=check, capture_output=True)


def _inert_state(repo: Path, env: dict[str, str]) -> dict[str, object]:
    """Snapshot the repository facts that a failed preflight must leave untouched."""
    index = repo / ".git" / "index"
    return {
        "index": index.read_bytes() if index.exists() else None,
        "merge_head": (repo / ".git" / "MERGE_HEAD").exists(),
        "head_resolves": _git(repo, env, "rev-parse", "--verify", "HEAD", check=False).returncode == 0,
    }


def _broken_posix_command(message: str) -> str:
    """Build the add-and-commit shell text with the failure-prone encoding that makes the syntax preflight fail."""
    return "git add -- reviewed.txt && git commit -m '" + message.replace("'", "'''") + "'"


def _commit_recovered(
    repo: Path, env: dict[str, str], segments: list[list[str]]
) -> tuple[subprocess.CompletedProcess[bytes], str, bytes]:
    """Syntax-check, then run, the corrected one-shell command; return the check, stored message and commit count."""
    argv = _posix_calls(_with_identity(segments))[0]
    syntax = subprocess.run([argv[0], "-n", "-c", argv[-1]], cwd=repo, env=env, capture_output=True)
    subprocess.run(argv, cwd=repo, env=env, capture_output=True, check=True)
    stored = _git(repo, env, "show", "-s", "--format=format:%B", "HEAD").stdout.decode("utf-8")
    count = _git(repo, env, "rev-list", "--count", "HEAD").stdout.strip()
    return syntax, stored, count


@pytest.fixture(name="manifest", scope="module")
def _manifest() -> dict[str, Any]:
    """Load the package manifest once for the module."""
    return _load_json(PLUGIN_ROOT / "package-manifest.json")


@pytest.fixture(name="recorded_paths", scope="module")
def _recorded_paths(manifest: dict[str, Any]) -> set[str]:
    """Collect every file path the package manifest records."""
    return {record["path"] for record in manifest["files"]}


@pytest.fixture(name="payload_paths", scope="module")
def _payload_paths() -> tuple[Path, ...]:
    """List the release payload files discovered by the package builder, which owns publication file discovery."""
    builder = load_module("codex_rig_release_package_builder", PLUGIN_ROOT / "scripts" / "build_package.py")
    return tuple(builder.iter_payload_files())


@pytest.fixture(name="behavioral_cases", scope="module")
def _behavioral_cases() -> dict[str, dict[str, Any]]:
    """Index the calibration behavioral cases by id."""
    return {case["id"]: case for case in _load_json(BEHAVIORAL_CASES_PATH)["cases"]}


@pytest.fixture(name="commit_template", scope="module")
def _commit_template() -> str:
    """Read the commit response template exactly as shipped."""
    return COMMIT_TEMPLATE_PATH.read_text(encoding="utf-8")


@pytest.fixture(name="commit_contract", scope="module")
def _commit_contract() -> str:
    """Normalize the commit response template to lowercase single-spaced text so wrapping cannot affect matches."""
    return _normalized_text(COMMIT_TEMPLATE_PATH).lower()


@pytest.fixture(name="specialist_policy", scope="module")
def _specialist_policy() -> str:
    """Normalize the specialist orchestration policy to single-spaced text."""
    return _normalized_text(PLUGIN_ROOT / "shared" / "specialist-orchestration.md")


@pytest.fixture(name="parallel_diagram", scope="module")
def _parallel_diagram() -> ElementTree.Element:
    """Parse the architecture diagram that carries the canonical execution flow."""
    diagram = PLUGIN_ROOT / "assets" / "images" / "parallel-execution-architecture.svg"
    return ElementTree.parse(diagram).getroot()  # noqa: S314 - shipped package file, not untrusted input


@pytest.fixture(name="parallel_diagram_lines", scope="module")
def _parallel_diagram_lines(parallel_diagram: ElementTree.Element) -> list[str]:
    """Collect the rendered text of the architecture diagram, one entry per text element."""
    return [
        " ".join("".join(element.itertext()).split())
        for element in parallel_diagram.iter("{http://www.w3.org/2000/svg}text")
    ]


@pytest.fixture(name="package_validator")
def _package_validator() -> Any:
    """Load the package validator script."""
    return load_module("codex_rig_package_validator", PLUGIN_ROOT / "scripts" / "validate_package.py")


@pytest.fixture(name="artifact_validator")
def _artifact_validator() -> Any:
    """Load the shared artifact validator script."""
    return load_module("codex_rig_shared_artifact_validator", PLUGIN_ROOT / "shared" / "validate-artifacts.py")


@pytest.fixture(name="package_root")
def _package_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package_validator: Any) -> Path:
    """Create an empty synthetic package directory and point the validator at it."""
    root = tmp_path / "package"
    root.mkdir()
    monkeypatch.setattr(package_validator, "PACKAGE_ROOT", root)
    return root


@pytest.fixture(name="load_calibration_module")
def _load_calibration_module(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Any]:
    """Provide a loader that imports one calibration script, with its directory importable, until test teardown."""
    monkeypatch.syspath_prepend(str(CALIBRATION_DIR))

    def load(script: str) -> Any:
        return load_module_scoped(f"codex_rig_calibration_{Path(script).stem}", CALIBRATION_DIR / script, monkeypatch)

    return load


@pytest.fixture(name="calibration_runner")
def _calibration_runner(load_calibration_module: Callable[[str], Any]) -> Any:
    """Load the calibration runner module."""
    return load_calibration_module("run.py")


@pytest.fixture(name="stage_merge_resolution")
def _stage_merge_resolution(tmp_path: Path) -> Callable[..., tuple[dict[str, Any], Path]]:
    """Provide a factory that writes one merge-resolution artifact and returns its metadata and directory."""

    def stage(**overrides: object) -> tuple[dict[str, Any], Path]:
        pr_dir = tmp_path / "pr"
        pr_dir.mkdir()
        path = pr_dir / "merge-resolution.json"
        payload = _write_merge_resolution(path, **overrides)
        record = {key: payload[key] for key in ("authorization", "conflicts_detected", "status")}
        return {"merge_resolution": {"artifact_path": str(path), **record}}, pr_dir

    return stage


@pytest.fixture(name="git_env")
def _git_env() -> dict[str, str]:
    """Build an environment that isolates a disposable repository from caller Git routing and global hooks or config."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return env


@pytest.fixture(name="repo")
def _repo(tmp_path: Path, git_env: dict[str, str]) -> Path:
    """Initialize an empty Git repository inside tmp_path."""
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, env=git_env, capture_output=True)
    return tmp_path


class TestPublicationPayloadValidator:
    """The package validator inspects only declared publication bytes and rejects private user-profile paths."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"C:" + b"\\Users\\" + b"Alice\\project", id="backslash"),
            pytest.param(b"d:" + b"/us" + b"ers/alice/project", id="case-insensitive-forward-slash"),
        ],
    )
    def test_rejects_windows_user_profile_paths(
        self, package_validator: Any, package_root: Path, payload: bytes
    ) -> None:
        """Absolute Windows user-profile paths are rejected from public payloads.

        A published file that embeds a simulated profile path leaks the author's account name, in either slash style.
        """
        (package_root / "runtime.txt").write_bytes(payload)
        _write_payload_manifest(package_root, "runtime.txt")

        with pytest.raises(ValueError, match=r"private material in payload: runtime\.txt"):
            package_validator.validate_publication_payload()

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"C:\\ProgramData\\codex-rig", id="system-root"),
            pytest.param(b"%USERPROFILE%\\codex-rig", id="portable-variable"),
            pytest.param(b"docs/windows/users/guide.md", id="relative-documentation"),
        ],
    )
    def test_accepts_non_private_windows_paths(
        self, package_validator: Any, package_root: Path, payload: bytes
    ) -> None:
        """Portable and non-profile Windows paths stay publishable.

        System roots, the portable profile variable, and relative documentation paths look similar to a leak but name no
        account, so rejecting them would block legitimate documentation.
        """
        (package_root / "runtime.txt").write_bytes(payload)
        _write_payload_manifest(package_root, "runtime.txt")

        package_validator.validate_publication_payload()

    def test_ignores_unmanifested_generated_reports(self, package_validator: Any, package_root: Path) -> None:
        """Declared publication bytes are inspected while retained generated reports are not payload.

        A calibration report under the package directory holds a profile path but is absent from the manifest, so the
        validator must pass instead of treating every file on disk as publishable.
        """
        report = package_root / ".reports" / "calibration" / "result.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(b'"' + b"/Us" + b'ers/Alice/project"')
        (package_root / "runtime.txt").write_text("portable runtime\n", encoding="utf-8", newline="\n")
        _write_payload_manifest(package_root, "runtime.txt")

        package_validator.validate_publication_payload()


class TestPackagedRoster:
    """The skill and role rosters on disk, in the manifest, and in the release profile agree exactly."""

    def test_manifest_records_every_skill_in_order(self, manifest: dict[str, Any]) -> None:
        """The manifest skill records list each expected skill with its SKILL.md path."""
        assert manifest["skills"] == [
            {"id": skill_id, "path": f"skills/{skill_id}/SKILL.md"} for skill_id in EXPECTED_SKILLS
        ]

    @pytest.mark.parametrize("skill_id", EXPECTED_SKILLS)
    def test_skill_frontmatter_declares_name_and_description(self, skill_id: str) -> None:
        """Each skill card names itself and carries a non-empty description.

        The frontmatter is the public contract an installed host reads to route to the skill.
        """
        fields = _parse_frontmatter(PLUGIN_ROOT / "skills" / skill_id / "SKILL.md")

        assert fields["name"] == skill_id
        assert fields["description"]

    def test_manifest_role_records_match_cards(self, manifest: dict[str, Any]) -> None:
        """The manifest role aggregate carries each card's hash and runtime policy.

        A role edited without regenerating the manifest, or a policy changed in only one place, makes the aggregate
        drift from the cards that actually ship.
        """
        assert manifest["roles"] == _expected_role_records()

    @pytest.mark.parametrize(
        ("role_id", "effort", "sandbox_mode"),
        [
            pytest.param(role_id, effort, sandbox_mode, id=role_id)
            for role_id, (effort, sandbox_mode) in EXPECTED_ROLES.items()
        ],
    )
    def test_role_frontmatter_declares_runtime(self, role_id: str, effort: str, sandbox_mode: str) -> None:
        """Each role card's frontmatter matches its declared runtime policy exactly."""
        fields = _parse_frontmatter(PLUGIN_ROOT / "roles" / role_id / "ROLE.md")

        assert fields == {
            "role_id": role_id,
            "name": f"codex-rig-{role_id}",
            "model_reasoning_effort": effort,
            "approval_policy": "on-request",
            "sandbox_mode": sandbox_mode,
            "fallback_modes": "[shim, built-in-injected, inline]",
        }

    def test_release_profile_declares_packaged_lifecycle_features(self, manifest: dict[str, Any]) -> None:
        """The plugin metadata and manifest declare the role-card-injected profile and its shipped features.

        Role boundaries must stay aligned with the independently shipped native question provider, and the plugin must
        not register hooks of its own.
        """
        plugin = _load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")

        assert plugin["description"].startswith("Fifteen evidence-first Codex workflows")
        assert plugin["interface"]["capabilities"] == [
            "15 workflow skills and 1 legacy-shim lifecycle manager",
            "15 specialist role cards",
            "Staged execution manifest validation",
            "Gated portable read-only auto execution",
            "Authenticated cleanup for prior agent shims",
            "Optional SessionStart health diagnostic",
            "Built-in and inline role fallback",
            "Quality gates",
            "Native terminal questions",
            "Optional codemap-py structural-context integration",
        ]
        assert manifest["release_profile"] == "role-card-injected"
        assert manifest["features"] == {"manager": True, "hooks": True, "mcp": True, "generated_shims": False}
        assert "hooks" not in plugin
        assert plugin["mcpServers"] == "./.codex-mcp.json"

    def test_payload_excludes_retired_layouts(self, payload_paths: tuple[Path, ...]) -> None:
        """No manager, MCP, or generated-shim file layout ships; the agent-shims skill carries that lifecycle
        instead."""
        retired = [path for path in payload_paths if _is_retired_payload_path(path.relative_to(PLUGIN_ROOT))]

        assert retired == []
        assert (PLUGIN_ROOT / "skills" / "agent-shims" / "SKILL.md").is_file()


class TestShippedMarkdown:
    """Published Markdown works from an installed cache and carries no private planning material."""

    @pytest.mark.parametrize("path", _SHIPPED_MARKDOWN)
    def test_installed_markdown_has_no_source_checkout_only_paths(self, path: Path) -> None:
        """Each shipped Markdown document stays usable from an installed cache.

        A path that only exists in the source checkout dangles for every installed user.
        """
        text = path.read_text(encoding="utf-8")

        assert "plugins/codex-rig/" not in text, path
        assert ".developments/" not in text, path

    def test_agent_shims_skill_points_at_the_manager_script(self) -> None:
        """The agent-shims skill runs its cache-local manager through the portable launcher."""
        lines = (PLUGIN_ROOT / "skills" / "agent-shims" / "SKILL.md").read_text(encoding="utf-8").splitlines()
        invocation = next(line for line in lines if "Run `PLUGIN_ROOT/bin/python" in line)

        assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/scripts/manage_role_agents.py <action>" in invocation
        assert "on native Windows use `PLUGIN_ROOT/bin/python.cmd`" in invocation
        assert "Resolve `PLUGIN_ROOT` two levels above this installed skill directory" in invocation

    @pytest.mark.parametrize(("skill_id", "extra_dependencies"), _SKILL_DEPENDENCY_CASES)
    def test_skill_dependencies_are_cache_local_and_manifested(
        self, recorded_paths: set[str], skill_id: str, extra_dependencies: tuple[str, ...]
    ) -> None:
        """Every file a skill document refers to exists inside the plugin and is recorded in the manifest.

        The skill reaches shared files through the plugin-level shared directory, never a sibling skill-level one, so an
        installed skill cannot point at a missing dependency.
        """
        skill_path = PLUGIN_ROOT / "skills" / skill_id / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        dependencies = {*RELATIVE_DEPENDENCY.findall(text), "result-template.json", *extra_dependencies}

        assert "../_shared/" not in text
        assert "../../shared/" in text
        assert _dependency_defects(skill_path, dependencies, recorded_paths) == []


class TestCalibrationCases:
    """The calibration fixtures pin the findings each escalation and loop scenario must produce."""

    @pytest.mark.parametrize(("case_id", "target", "expected_findings"), _CALIBRATION_CASE_PARAMS)
    def test_case_declares_target_and_findings(
        self, behavioral_cases: dict[str, Any], case_id: str, target: str, expected_findings: list[str]
    ) -> None:
        """Each pinned calibration case targets the right skill or role and expects exactly the documented findings.

        These cases keep the loop, recurrence, model-stall, audit, history-rewrite, and dispatch-wave contracts scored
        against their executable boundaries.
        """
        case = behavioral_cases[case_id]

        assert (case["target"], case["expected_findings"]) == (target, expected_findings)

    def test_adversarial_loop_cases_have_observations(self) -> None:
        """Every pinned adversarial-loop case has a recorded behavioral observation."""
        observations_path = CALIBRATION_DIR / "behavioral-observations.jsonl"
        observed = {
            json.loads(line)["case_id"]
            for line in observations_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        loop_cases = {case_id for case_id, (target, _) in CALIBRATION_CASES.items() if target == "challenge-resolve"}

        assert sorted(loop_cases - observed) == []

    @pytest.mark.installed_plugin
    def test_model_stall_fixture_observations_are_scored(
        self, load_calibration_module: Callable[[str], Any], behavioral_cases: dict[str, Any]
    ) -> None:
        """Every model-stall escalation case has a scored fixture observation.

        The shipped scorer runs against the shipped cases and observations; a case with no observation would pass
        unscored.
        """
        scorer = load_calibration_module("score_behavioral.py")

        result = scorer._score(
            BEHAVIORAL_CASES_PATH,
            CALIBRATION_DIR / "behavioral-observations.jsonl",
            CALIBRATION_DIR / "live-route-policy.json",
            CALIBRATION_DIR / "live-ab-tasks.json",
            PLUGIN_ROOT,
            layout="plugin",
        )

        stall_ids = {case_id for case_id in behavioral_cases if case_id.startswith("model-stall-")}
        assert stall_ids <= {row["case_id"] for row in result["case_results"]}
        assert result["missing_case_ids"] == []


def _as_skill_files(target: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Present a file to the placement check as a packaged skill."""
    return (target,), ()


def _as_role_files(target: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Present a file to the placement check as a packaged role."""
    return (), (target,)


def _without_link(content: str, link: str) -> str:
    """Remove the recurrence policy link from a document."""
    return content.replace(link, "")


def _with_extra_link(content: str, link: str) -> str:
    """Append the recurrence policy link to a document."""
    return content + f"\n{link}\n"


class TestCalibrationRunner:
    """The calibration runner checks route evidence and recurrence-policy placement against packaged files."""

    def test_archived_route_evidence_stays_stale_after_skill_rename(
        self, tmp_path: Path, calibration_runner: Any
    ) -> None:
        """Historical paid route evidence is preserved but flagged as stale, never promoted to current.

        After a skill rename the archived evidence no longer matches the roster; the runner must record that mismatch
        without failing a check, and the recommendations must still point at collecting live observations.
        """
        run = calibration_runner.CalibrationRun(paths=calibration_runner.Paths.create("plugin", tmp_path))

        calibration_runner.check_accepted_route_evidence(run)

        checks = run.paths.checks.read_text(encoding="utf-8")
        assert "accepted-route-evidence=archived-stale:gpt-5.6-baseline" in checks
        assert run.accepted_route_evidence_current is False
        assert run.checks_failed == []
        recommendations, follow_up = calibration_runner.build_recommendations(
            {
                "thresholds": {},
                "gate_metrics_raw": {"observations": 1, "recall": 1.0, "precision": 1.0},
                "observation_freshness": {"live_observations": 0},
                "live_route_acceptance": {"status": "insufficient-evidence"},
                "case_results": [],
            },
            [],
            0,
            run.accepted_route_evidence_current,
        )
        assert recommendations == [
            "No blocking calibration fixes found; maintain the current gates and collect live observations next."
        ]
        assert len(follow_up) == 2

    def test_rejects_active_assignment_drift(self, tmp_path: Path, calibration_runner: Any) -> None:
        """A recorded active role effort that differs from the calibrated role policy fails the evidence check.

        The evidence copy moves qa-specialist from the medium to the high effort tier, which the role policy forbids.
        """
        evidence = json.loads((CALIBRATION_DIR / "accepted-route-evidence.json").read_text(encoding="utf-8"))
        evidence["active_assignments"]["roles"]["gpt-6.1-sol"]["medium"].remove("qa-specialist")
        evidence["active_assignments"]["roles"]["gpt-6.1-sol"]["high"].append("qa-specialist")
        tampered = tmp_path / "tampered-active-assignments.json"
        tampered.write_text(json.dumps(evidence), encoding="utf-8")
        paths = replace(calibration_runner.Paths.create("plugin", tmp_path), accepted_route_evidence=tampered)
        run = calibration_runner.CalibrationRun(paths=paths)

        calibration_runner.check_accepted_route_evidence(run)

        assert "accepted-route-evidence" in run.checks_failed
        assert "active GPT-6 assignments do not match role policy" in run.paths.leaks.read_text(encoding="utf-8")

    def test_recurrence_policy_link_sits_on_retry_owners_only(self, calibration_runner: Any) -> None:
        """Only the designated retry-owner skills and role carry the recurrence policy link in the shipped package."""
        packaged_skills = tuple(sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")))
        packaged_roles = tuple(sorted((PLUGIN_ROOT / "roles").glob("*/ROLE.md")))
        link = calibration_runner.RECURRENCE_POLICY_LINK

        linked_skills = {path.parent.name for path in packaged_skills if link in path.read_text(encoding="utf-8")}
        linked_roles = {path.parent.name for path in packaged_roles if link in path.read_text(encoding="utf-8")}

        assert linked_skills == {"code-remediate", "implement", "investigate"}
        assert linked_roles == {"delegation-lead"}
        assert calibration_runner.find_misplaced_packaged_recurrence_policy_links(packaged_skills, packaged_roles) == []

    @pytest.mark.parametrize(
        ("source", "placement", "edit"),
        [
            pytest.param(
                PLUGIN_ROOT / "skills" / "implement" / "SKILL.md",
                _as_skill_files,
                _without_link,
                id="retry-owner-skill-missing-link",
            ),
            pytest.param(
                PLUGIN_ROOT / "skills" / "manage" / "SKILL.md",
                _as_skill_files,
                _with_extra_link,
                id="linear-skill-extra-link",
            ),
            pytest.param(
                PLUGIN_ROOT / "roles" / "delegation-lead" / "ROLE.md",
                _as_role_files,
                _without_link,
                id="retry-owner-role-missing-link",
            ),
            pytest.param(
                PLUGIN_ROOT / "roles" / "sw-engineer" / "ROLE.md",
                _as_role_files,
                _with_extra_link,
                id="linear-role-extra-link",
            ),
        ],
    )
    def test_flags_misplaced_recurrence_policy_link(
        self,
        tmp_path: Path,
        calibration_runner: Any,
        source: Path,
        placement: Callable[[Path], tuple[tuple[Path, ...], tuple[Path, ...]]],
        edit: Callable[[str, str], str],
    ) -> None:
        """Both a missing owner link and a redundant link on a linear workflow are reported.

        A pristine copy of the packaged file passes, then the link is removed or appended, and the edited copy is the
        one reported as misplaced.
        """
        target = tmp_path / source.relative_to(PLUGIN_ROOT)
        target.parent.mkdir(parents=True)
        shutil.copy2(source, target)
        skill_files, role_files = placement(target)
        assert calibration_runner.find_misplaced_packaged_recurrence_policy_links(skill_files, role_files) == []

        link = calibration_runner.RECURRENCE_POLICY_LINK
        target.write_text(edit(target.read_text(encoding="utf-8"), link), encoding="utf-8")

        assert calibration_runner.find_misplaced_packaged_recurrence_policy_links(skill_files, role_files) == [target]


class TestSkillInstructionContracts:
    """Packaged skill instructions keep the boundaries that protect the installed plugin and audit quality."""

    @pytest.mark.parametrize(
        ("skill_id", "phrases"),
        [
            pytest.param(
                "manage",
                (
                    "installed plugin tree is immutable input",
                    "never edit this skill's plugin cache",
                    "reject any target whose canonical path is inside the same installed plugin root",
                    "bundled `agent-shims` workflow",
                ),
                id="manage",
            ),
            pytest.param(
                "sync",
                (
                    "never copy files into an installed cache",
                    "sync never mutates external agent files",
                    "before plugin removal, run `agent-shims remove`",
                    "after refresh or reinstall, run `agent-shims doctor`",
                    "codex plugin marketplace list --json",
                    "codex plugin list --marketplace borda-ai-rig --json",
                    "codex plugin marketplace upgrade borda-ai-rig",
                    "codex plugin add codex-rig@borda-ai-rig",
                    "preservation of unknown external agent files",
                ),
                id="sync",
            ),
        ],
    )
    def test_management_skill_preserves_installed_plugin_state(self, skill_id: str, phrases: tuple[str, ...]) -> None:
        """Management workflows never treat the installed cache as editable source.

        The text is whitespace-normalized and lowercased so rewrapping the skill cannot break a match.
        """
        skill = _normalized_text(PLUGIN_ROOT / "skills" / skill_id / "SKILL.md").lower()

        assert missing_phrases(skill, phrases) == []

    def test_audit_skill_requires_cost_and_value_evidence(self) -> None:
        """Recurring skill audits cannot reward shorter but weaker instructions."""
        audit = _normalized_text(PLUGIN_ROOT / "skills" / "audit" / "SKILL.md")

        assert (
            missing_phrases(
                audit,
                (
                    "Prompt Efficiency",
                    "prompt-efficiency.md",
                    "provider-native token counts",
                    "o200k_base",
                    "UTF-8 bytes and words",
                    "cost evidence, never quality evidence",
                    "loaded referenced instructions",
                    "behavioral and calibration",
                    "adversarial review",
                    "shorter candidate fails",
                ),
            )
            == []
        )

    def test_audit_artifact_requirements_include_prompt_efficiency(self, artifact_validator: Any) -> None:
        """The audit artifact validator demands the exploration, ledger, and prompt-efficiency files with their
        sections."""
        assert artifact_validator.SKILL_REQUIREMENTS["audit"] == {
            "files": {
                "workflow-exploration.md": ["Transitions", "Counterexamples", "Coverage"],
                "audit-ledger.md": [
                    "Inventory",
                    "Broken References",
                    "Runtime Leaks",
                    "Coverage",
                    "Overlap",
                    "Prompt Efficiency",
                    "Recommendations",
                ],
                "prompt-efficiency.md": [
                    "Measurement",
                    "Cost Baseline",
                    "Loaded Context",
                    "Obligation Map",
                    "Value Guards",
                    "Adversarial Review",
                    "Recommendations",
                ],
            }
        }


class TestCommitContract:
    """The commit response template states the exact authorization, message, and staging rules."""

    @pytest.mark.parametrize(
        "phrases",
        [
            pytest.param(
                (
                    "creating a new commit does not authorize rewriting an existing commit",
                    "unless the user explicitly requests that exact history operation",
                    "never infer rewrite permission from a commit, cleanup, or commit-diet request",
                    "`git commit --amend`",
                    "`git rebase`",
                    "`git reset`",
                ),
                id="history-rewrite-needs-exact-authorization",
            ),
            pytest.param(
                (
                    "required user-facing commit summary",
                    "hash and title",
                    "behavior",
                    "affected surfaces",
                    "verification",
                    "residual limits",
                    "why the boundary exists",
                    "never claim a check passed without concrete execution evidence",
                ),
                id="descriptive-user-facing-handoff",
            ),
            pytest.param(
                (
                    "changes:",
                    "impact:",
                    "verification:",
                    "residual limits:",
                    "every meaningful behavioral, interface, workflow, policy, test, documentation, packaging, or "
                    "operational change",
                    "state the concrete user, developer, runtime, compatibility, or maintenance effect",
                    "generic impact claims",
                ),
                id="detailed-changes-and-impacts",
            ),
            pytest.param(
                (
                    "only final checks that materially validate the committed surfaces",
                    "consolidate closely related checks",
                    "report a required broad gate once using its final outcome",
                    "exploratory probes",
                    "failure-first reproductions",
                    "repeated reruns",
                    "unrelated repository-wide gates",
                    "material change-specific acceptance gate",
                ),
                id="compact-change-specific-verification",
            ),
            pytest.param(
                (
                    "application of the general approval contract",
                    "do not create a temporary or persistent commit-message file",
                    "one message argument",
                    "--cleanup=verbatim",
                    "must not repeat the command, flags, message body, or full approval brief",
                ),
                id="shared-approval-boundary",
            ),
            pytest.param(
                (
                    "show the complete secret-free message in chat",
                    "shell-free argv",
                    "posix",
                    "powershell",
                    "never use double-quoted shell interpolation",
                    "command-size or encoding limits",
                    "do not silently fall back to a file",
                    "normalizing only one terminal lf",
                    "raw git output",
                    "on an actual user or runtime denial, uncertain execution, hook failure, concurrent changes, or "
                    "mismatch",
                    "do not amend",
                    "full message may appear in the runtime command approval",
                    "does not promise a fixed number of host approval prompts",
                ),
                id="literal-message-and-failure-boundaries",
            ),
            pytest.param(
                (
                    "one owning command that stages exactly those paths and commits them",
                    "`rtk git add -- <paths> && rtk git commit --cleanup=verbatim -m <message>`",
                    "show the complete secret-free message in chat with the exact reviewed path list",
                    "`git diff head -- <paths>`",
                    "`git status --porcelain=v1 -- <paths>`",
                    "`git diff --cached --name-only`: any staged entry outside the reviewed paths is user state and "
                    "stops execution",
                    "never `.`, a directory, or a glob",
                    "as two argv calls",
                    "windows powershell 5.1 has no `&&`",
                    "reuse any existing matching command allowance",
                    "`git --no-pager show --no-renames --name-only -z --format= head`",
                    "equal the reviewed path list exactly as a set",
                    "a mismatch includes a committed file set that differs from the reviewed paths",
                    "do not amend, reset, unstage, blindly re-stage, or create a repair commit",
                ),
                id="one-command-staging-with-scope-guards",
            ),
        ],
    )
    def test_template_states_required_rules(self, commit_contract: str, phrases: tuple[str, ...]) -> None:
        """The commit template states each required rule sentence.

        The template is whitespace-normalized and lowercased, so rewrapping cannot break a match; each case pins one
        topic: history-rewrite authorization, handoff detail, compact verification, literal messages, or staging.
        """
        assert missing_phrases(commit_contract, phrases) == []

    def test_template_drops_split_staging_wording(self, commit_contract: str) -> None:
        """The retired instruction to finish staging separately no longer appears.

        Staging and commit now run as one owning command, so a split-staging sentence would contradict it.
        """
        assert "finish authorized staging separately" not in commit_contract

    @pytest.mark.parametrize(
        "contract_path",
        [
            pytest.param(PLUGIN_ROOT / "shared" / "native-skill-contract.md", id="native-skill-contract"),
            pytest.param(PLUGIN_ROOT / "assets" / "AGENTS.md", id="agent-contract"),
        ],
    )
    def test_approval_contract_keeps_runtime_reason_short_and_prefix_safe(self, contract_path: Path) -> None:
        """Neither approval contract duplicates commands or detailed pre-briefs in the approval question."""
        contract = _normalized_text(contract_path).lower()

        assert (
            missing_phrases(
                contract,
                (
                    "all intentional approval requests",
                    "short plain-english question",
                    "outcome or material effect",
                    "must not repeat the command, argv, flags, paths, multiline content, or full approval brief",
                    "short categorical safe prefix",
                    "omit `prefix_rule` for one-time or high-risk commands",
                ),
            )
            == []
        )


class TestFileFreeCommit:
    """The documented one-command commit preserves the reviewed message and scope in a real disposable repository."""

    @pytest.mark.parametrize(
        ("to_argvs", "call_count"),
        [
            pytest.param(_argv_calls, 2, id="argv"),
            pytest.param(_posix_calls, 1, marks=_skip_posix_shell_unavailable, id="posix"),
            pytest.param(_rtk_calls, 2, marks=_skip_rtk_unavailable, id="rtk"),
        ],
    )
    def test_preserves_reviewed_message_and_scope(
        self,
        repo: Path,
        git_env: dict[str, str],
        commit_template: str,
        to_argvs: Callable[[list[list[str]]], list[list[str]]],
        call_count: int,
    ) -> None:
        """Hostile-looking literal text is stored verbatim and only the reviewed path is committed, without draft files.

        The documented command stages explicit reviewed paths and commits them in one shell command; argv-only tools run
        the same two segments as separate calls. An unrelated untracked file must stay out of the commit.
        """
        (repo / "reviewed file.txt").write_bytes(b"reviewed\n")
        (repo / "unrelated.txt").write_bytes(b"user state\n")
        add_text, commit_text = COMMIT_COMMAND.split(" && ")
        add_argv = [part if part != "<paths>" else "reviewed file.txt" for part in shlex.split(add_text)[1:]]
        commit_argv = [*shlex.split(commit_text)[1:-1], _HOSTILE_COMMIT_MESSAGE]

        argvs = to_argvs(_with_identity([add_argv, commit_argv]))
        results = [subprocess.run(argv, cwd=repo, env=git_env, check=True, capture_output=True) for argv in argvs]

        stored = _git(repo, git_env, "--no-pager", "show", "-s", "--format=format:%B", "HEAD").stdout.decode("utf-8")
        committed = _git(
            repo, git_env, "--no-pager", "show", "--no-renames", "--name-only", "-z", "--format=", "HEAD"
        ).stdout.decode("utf-8")
        assert f"`{COMMIT_COMMAND}`" in commit_template
        assert len(results) == call_count
        assert stored.removesuffix("\n") == _HOSTILE_COMMIT_MESSAGE
        assert set(committed.split("\0")) - {""} == {"reviewed file.txt"}
        assert sorted(path.name for path in repo.iterdir()) == [".git", "reviewed file.txt", "unrelated.txt"]

    @_skip_posix_shell_unavailable
    def test_recovers_parse_failure_from_unchanged_state(self, repo: Path, git_env: dict[str, str]) -> None:
        """A failed shell syntax preflight leaves the repository untouched, and the corrected command commits once.

        A broken apostrophe encoding is rejected by the preflight before anything runs; nothing is staged or committed,
        so the corrected command can run without creating a duplicate commit.
        """
        (repo / "reviewed.txt").write_bytes(b"reviewed\n")
        before = _inert_state(repo, git_env)

        failed = subprocess.run(
            [shutil.which("sh"), "-n", "-c", _broken_posix_command(_RECOVERY_MESSAGE)],
            cwd=repo,
            env=git_env,
            capture_output=True,
        )
        after = _inert_state(repo, git_env)
        segments = [_ADD_REVIEWED, ["git", "commit", "--cleanup=verbatim", "-m", _RECOVERY_MESSAGE]]
        syntax, stored, count = _commit_recovered(repo, git_env, segments)

        assert failed.returncode != 0
        assert after == before
        assert (after["merge_head"], after["head_resolves"]) == (False, False)
        assert syntax.returncode == 0, syntax.stderr
        assert stored.removesuffix("\n") == _RECOVERY_MESSAGE
        assert count == b"1"

    @_skip_posix_shell_unavailable
    def test_recovers_parse_failure_from_reviewed_staged_state(self, repo: Path, git_env: dict[str, str]) -> None:
        """After a failed preflight with exactly the reviewed file staged, only the commit segment reruns, once.

        The staged set and staged content are verified to be exactly the reviewed path before the add segment is
        dropped, so recovery neither re-stages blindly nor creates a duplicate commit.
        """
        (repo / "reviewed.txt").write_bytes(b"reviewed\n")
        _git(repo, git_env, *_ADD_REVIEWED[1:])
        before = _inert_state(repo, git_env)

        failed = subprocess.run(
            [shutil.which("sh"), "-n", "-c", _broken_posix_command(_RECOVERY_MESSAGE)],
            cwd=repo,
            env=git_env,
            capture_output=True,
        )
        after = _inert_state(repo, git_env)
        staged = _git(repo, git_env, "diff", "--cached", "--name-only", "-z").stdout
        content = _git(repo, git_env, "show", ":reviewed.txt").stdout
        commit_only = [["git", "commit", "--cleanup=verbatim", "-m", _RECOVERY_MESSAGE]]
        syntax, stored, count = _commit_recovered(repo, git_env, commit_only)

        assert failed.returncode != 0
        assert after == before
        assert (after["merge_head"], after["head_resolves"]) == (False, False)
        assert (staged, content) == (b"reviewed.txt\0", b"reviewed\n")
        assert syntax.returncode == 0, syntax.stderr
        assert stored.removesuffix("\n") == _RECOVERY_MESSAGE
        assert count == b"1"


class TestCodeRemediateMergeResolution:
    """The code-remediate validator admits only completed, authorized, correctly bound target merges."""

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({}, id="conflict-free"),
            pytest.param(
                {**_COMPLETED_MERGE, "authorization": "explicit-input", "evidence": _MERGE_EVIDENCE},
                id="explicit-input",
            ),
            pytest.param(
                {**_COMPLETED_MERGE, "authorization": "user-confirmed", "evidence": _MERGE_EVIDENCE},
                id="user-confirmed",
            ),
        ],
    )
    def test_accepts_completed_intent_first_merge_states(
        self, artifact_validator: Any, stage_merge_resolution: Callable[..., Any], overrides: dict[str, object]
    ) -> None:
        """Conflict-free evidence or one completed conflicted merge under a recorded authorization source validates.

        A task-owned target merge runs with no question or grant check and records its invocation as explicit-input; an
        explicit approval of a withheld merge records user-confirmed. Both must validate.
        """
        metadata, pr_dir = stage_merge_resolution(**overrides)

        result = artifact_validator._validate_code_remediate_merge_resolution(
            metadata, pr_dir, {"local_head": "base-oid"}
        )

        assert result is None

    @pytest.mark.parametrize(
        ("overrides", "error"),
        [
            pytest.param(
                {**_COMPLETED_MERGE, "authorization": "not-required"},
                "target-merge-authorization-required",
                id="conflicts-without-authorization",
            ),
            pytest.param(
                {**_COMPLETED_MERGE, "authorization": "standing-policy", "evidence": _MERGE_EVIDENCE},
                "target-merge-authorization-required",
                id="grant-derived-standing-policy",
            ),
            pytest.param(
                {"conflicts_detected": True, "status": "in-progress", "authorization": "user-confirmed"},
                "code-remediate-target-merge-not-completed",
                id="in-progress-merge-at-result-time",
            ),
        ],
    )
    def test_rejects_unauthorized_or_unfinished_merge(
        self,
        artifact_validator: Any,
        stage_merge_resolution: Callable[..., Any],
        overrides: dict[str, object],
        error: str,
    ) -> None:
        """A conflicted merge without a valid authorization source, or one still in progress, fails closed.

        Target integration never checks a local Git approval grant, so a record citing a grant-derived label carries
        provenance no workflow writes. The record written before the merge only marks it task-owned and must never count
        as a completed merge.
        """
        metadata, pr_dir = stage_merge_resolution(**overrides)

        with pytest.raises(SystemExit, match=error):
            artifact_validator._validate_code_remediate_merge_resolution(metadata, pr_dir, {"local_head": "base-oid"})

    @pytest.mark.parametrize(
        ("overrides", "error"),
        [
            pytest.param(
                {**_BOUND_MERGE, "pre_merge_head": "d" * 40},
                "code-remediate-merge-resolution-pre-merge-head-mismatch",
                id="pre-merge-head-differs-from-collected-pr-head",
            ),
            pytest.param(
                {**_BOUND_MERGE, "post_merge_head": "not-an-oid", "merge_commit": "not-an-oid"},
                "code-remediate-merge-resolution-execution-head-invalid",
                id="execution-heads-are-not-object-ids",
            ),
        ],
    )
    def test_rejects_merge_evidence_bound_to_wrong_heads(
        self,
        artifact_validator: Any,
        stage_merge_resolution: Callable[..., Any],
        overrides: dict[str, object],
        error: str,
    ) -> None:
        """Merge execution evidence must start from the collected PR head and end at valid object ids."""
        metadata, pr_dir = stage_merge_resolution(**overrides)

        with pytest.raises(SystemExit, match=error):
            artifact_validator._validate_code_remediate_merge_resolution(
                metadata, pr_dir, {"local_head": _BASE_OID}, expected_pre_merge_head=_HEAD_OID
            )

    def test_accepts_merge_evidence_bound_to_collected_pr_head(
        self, artifact_validator: Any, stage_merge_resolution: Callable[..., Any]
    ) -> None:
        """Merge execution evidence that starts at the collected PR head and ends at valid object ids validates."""
        metadata, pr_dir = stage_merge_resolution(**_BOUND_MERGE)

        result = artifact_validator._validate_code_remediate_merge_resolution(
            metadata, pr_dir, {"local_head": _BASE_OID}, expected_pre_merge_head=_HEAD_OID
        )

        assert result is None


class TestSpecialistOrchestration:
    """The specialist orchestration policy keeps route fallback ordered and parallel scheduling bounded."""

    def test_fallback_ladder_and_evidence_are_complete(self, specialist_policy: str) -> None:
        """Fallback routing keeps its order, fidelity limits, and audit evidence fields."""
        route_markers = (
            "1. A runtime-provided blank/default subagent",
            "2. An inline pass in the parent context",
            "3. `unavailable` when the runtime cannot provide any safe route",
        )
        positions = [specialist_policy.index(marker) for marker in route_markers]

        assert positions == sorted(positions)
        assert (
            missing_phrases(
                specialist_policy,
                (
                    "`role_id`",
                    "role-card SHA-256",
                    "route",
                    "attempted routes",
                    "fallback reason",
                    "actual model",
                    "reasoning effort",
                    "requested and observed sandbox/approval controls",
                    "independence",
                    "nesting depth",
                    "material fidelity limits",
                    "Fallback only for route absence or rejection",
                    "Never retry another route because the specialist disagreed",
                    "`task_name` is provenance only",
                    "Persistent named shims are platform-blocked for routing",
                ),
            )
            == []
        )

    def test_wave_joins_before_acceptance_without_expanding_fanout(self, specialist_policy: str) -> None:
        """Parallel scheduling stays bounded by fixed packs, ownership, and a final join."""
        assert (
            missing_phrases(
                specialist_policy,
                (
                    "## Bounded Dispatch Wave",
                    "one approved dispatch wave",
                    "immutable packs",
                    "joins all handoffs before acceptance",
                    "Adding a second unplanned wave is forbidden",
                    "Code Review schema-eight native waves follow these rules",
                    "Retain the complete frozen roster and bind actual parent spawn-call order",
                    "descending frozen context bytes with stable role-ID ties",
                    "Keep at most four allocated review slots",
                    "until both genuine task completion and the exact final-answer join",
                    "At an observed parent scheduling opportunity, refill a proven free slot before another wait",
                    "An explicit observed full-pool refusal that proves no child was created retains the same queued role",
                    "including one slot, to finish native schema-eight review as `independent-spawned`",
                    "Historical overlap gates remain strict",
                    "Replenishing the same frozen roster is part of that wave, not new fan-out",
                    "Join every required role before assembly or the next predeclared dependent source, interaction or "
                    "consolidation wave; those waves remain serial",
                    "parent-serially or stop and re-plan with the user",
                    "Never add fan-out, bypass required authorization, or start dependencies",
                    "Code-remediate alone may declare exact shared-file ownership across isolated child worktrees",
                    "equal-gate serial fallback",
                ),
            )
            == []
        )


class TestParallelExecutionDocumentation:
    """Protect the canonical bounded-execution diagram contract."""

    def test_diagram_is_self_contained(self, parallel_diagram: ElementTree.Element) -> None:
        """The diagram renders inside an image tag: no script, no external reference, no imported resource."""
        tags = {element.tag.rsplit("}", 1)[-1] for element in parallel_diagram.iter()}
        attributes = {name.rsplit("}", 1)[-1] for element in parallel_diagram.iter() for name in element.attrib}

        assert "script" not in tags
        assert "foreignObject" not in tags
        assert "href" not in attributes
        assert not [name for name in attributes if name.startswith("on")]

    def test_has_canonical_gate_definitions(self, parallel_diagram_lines: list[str]) -> None:
        """Gate definitions G0 to G8 are in order in the contract table and each has a flow card."""
        table = [int(gate) for line in parallel_diagram_lines for gate in re.findall(r"^G([0-8]) [A-Za-z]", line)]
        cards = {int(gate) for line in parallel_diagram_lines for gate in re.findall(r"^G([0-8])[A-C]? · ", line)}

        assert table == list(range(9))
        assert cards == set(range(9))

    @pytest.mark.parametrize(
        ("gate", "keyword"),
        [
            pytest.param("G5a", "terminal", id="terminal-check"),
            pytest.param("G5b", "join", id="join-check"),
            pytest.param("G5c", "derivation", id="derivation-step"),
        ],
    )
    def test_defines_gate_five_substeps(self, parallel_diagram_lines: list[str], gate: str, keyword: str) -> None:
        """Gate five lists its nested terminal, join, and derivation sub-steps as definitions."""
        text = " ".join(parallel_diagram_lines)

        assert re.search(rf"\b{gate}\b.{{0,80}}{keyword}", text, flags=re.IGNORECASE)

    @pytest.mark.parametrize(
        "documents",
        [
            pytest.param((PLUGIN_ROOT / "README.md",), id="readme"),
            pytest.param(_skill_document("code-review"), id="code-review-skill"),
            pytest.param(_skill_document("implement"), id="implement-skill"),
            pytest.param(_skill_document("manage"), id="manage-skill"),
        ],
    )
    def test_consumer_links_the_canonical_flow(self, documents: tuple[Path, ...]) -> None:
        """Each consumer of the bounded execution flow links the canonical G0 to G8 diagram."""
        text = "".join(path.read_text(encoding="utf-8") for path in documents)

        assert "parallel-execution-architecture.svg" in text, documents[0]

    @pytest.mark.parametrize("endpoint", ["STOP", "re-plan", "FAIL", "ACCEPT"])
    def test_flow_draws_every_endpoint(self, parallel_diagram_lines: list[str], endpoint: str) -> None:
        """Every canonical endpoint appears in the flow, and every decision labels both branches."""
        assert any(endpoint in line for line in parallel_diagram_lines)
        assert "✓ YES" in parallel_diagram_lines
        assert "✗ NO" in parallel_diagram_lines
