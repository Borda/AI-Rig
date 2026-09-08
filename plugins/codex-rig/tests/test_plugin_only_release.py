"""Acceptance checks for the complete role-card-injected Codex Rig release."""

from __future__ import annotations

from collections import Counter
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_ROOT.parents[1]
EXPECTED_SKILLS = (
    "agent-shims",
    "change-analysis",
    "audit",
    "calibrate",
    "code-remediate",
    "code-review",
    "implement",
    "investigate",
    "kaggle",
    "manage",
    "optimize",
    "release",
    "research",
    "sync",
)
EXPECTED_ROLES = {
    "challenger": ("gpt-5.6-terra", "read-only"),
    "cicd-steward": ("gpt-5.6-luna", "workspace-write"),
    "curator": ("gpt-5.6-terra", "workspace-write"),
    "data-steward": ("gpt-5.6-terra", "workspace-write"),
    "delegation-lead": ("gpt-5.6-luna", "workspace-write"),
    "doc-scribe": ("gpt-5.6-luna", "workspace-write"),
    "linting-expert": ("gpt-5.6-luna", "workspace-write"),
    "oss-shepherd": ("gpt-5.6-luna", "read-only"),
    "qa-specialist": ("gpt-5.6-terra", "workspace-write"),
    "scientist": ("gpt-5.6-terra", "workspace-write"),
    "security-auditor": ("gpt-5.6-sol", "read-only"),
    "solution-architect": ("gpt-5.6-sol", "read-only"),
    "squeezer": ("gpt-5.6-terra", "read-only"),
    "sw-engineer": ("gpt-5.6-terra", "workspace-write"),
    "web-explorer": ("gpt-5.6-luna", "read-only"),
}
EXPECTED_KAGGLE_REFERENCES = {
    "composition.md",
    "eda.md",
    "foundation.md",
    "inference.md",
    "modality-dispatch.md",
    "style-rules.md",
    "submission.md",
    "training.md",
}
RELATIVE_DEPENDENCY = re.compile(r"`((?:\.\./\.\./(?:shared|runtime)/|references/)[A-Za-z0-9_./-]+)")
PRIVATE_WORK_ITEM = re.compile(r"\b(?:W\d+|H\d{2}|M\d{2})\b")
PRIVATE_ROLLOUT_PHASE = re.compile(r"P[345](?:[ab])?", flags=re.IGNORECASE)
PRIVATE_PLAN_REFERENCE = re.compile(r"(?:^|[/\s`'\"])(plan_[A-Za-z0-9_.-]+\.md)")
PERSONAL_ABSOLUTE_PATH = re.compile(
    r"(?:/(?:Users|home)/[^/\\\s'\"]+|(?i:[A-Z]:[\\/]+Users[\\/]+[^/\\\s'\"]+))"
    r"(?:[\\/][^\s'\"]+)?",
)


def _load_json(path: Path) -> dict[str, object]:
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


def _load_shared_artifact_validator() -> Any:
    """Load the packaged artifact validator without relying on package imports."""
    path = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
    spec = importlib.util.spec_from_file_location("codex_rig_shared_artifact_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_package_validator() -> Any:
    """Load the package validator without relying on package imports."""
    path = PLUGIN_ROOT / "scripts" / "validate_package.py"
    spec = importlib.util.spec_from_file_location("codex_rig_package_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_package_builder() -> Any:
    """Load the package builder that owns publication file discovery."""
    path = PLUGIN_ROOT / "scripts" / "build_package.py"
    spec = importlib.util.spec_from_file_location("codex_rig_release_package_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_payload_manifest(package_root: Path, *relative_paths: str) -> None:
    """Declare the exact synthetic publication files inspected by the validator."""
    payload = {"files": [{"path": relative} for relative in relative_paths]}
    (package_root / "package-manifest.json").write_text(json.dumps(payload), encoding="utf-8", newline="\n")


@pytest.mark.parametrize(
    "payload",
    (
        pytest.param(b"C:" + b"\\Users\\" + b"Alice\\project", id="backslash"),
        pytest.param(b"d:" + b"/users/" + b"alice/project", id="case-insensitive-forward-slash"),
    ),
)
def test_package_validator_rejects_simulated_windows_user_profile_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    """Reject absolute Windows user-profile paths from public payloads."""
    validator = _load_package_validator()
    package_root = tmp_path / "package"
    package_root.mkdir()
    (package_root / "runtime.txt").write_bytes(payload)
    _write_payload_manifest(package_root, "runtime.txt")
    monkeypatch.setattr(validator, "PACKAGE_ROOT", package_root)

    with pytest.raises(ValueError, match=r"private material in payload: runtime\.txt"):
        validator.validate_publication_payload()


@pytest.mark.parametrize(
    "payload",
    (
        pytest.param(b"C:\\ProgramData\\codex-rig", id="system-root"),
        pytest.param(b"%USERPROFILE%\\codex-rig", id="portable-variable"),
        pytest.param(b"docs/windows/users/guide.md", id="relative-documentation"),
    ),
)
def test_package_validator_accepts_non_private_simulated_windows_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    """Keep portable and non-profile Windows paths publishable."""
    validator = _load_package_validator()
    package_root = tmp_path / "package"
    package_root.mkdir()
    (package_root / "runtime.txt").write_bytes(payload)
    _write_payload_manifest(package_root, "runtime.txt")
    monkeypatch.setattr(validator, "PACKAGE_ROOT", package_root)

    validator.validate_publication_payload()


def test_package_validator_ignores_unmanifested_generated_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inspect declared publication bytes without treating retained reports as payload."""
    validator = _load_package_validator()
    package_root = tmp_path / "package"
    (package_root / ".reports" / "calibration").mkdir(parents=True)
    (package_root / ".reports" / "calibration" / "result.json").write_bytes(b'"' + b"/Users/" + b'Alice/project"')
    (package_root / "runtime.txt").write_text("portable runtime\n", encoding="utf-8", newline="\n")
    _write_payload_manifest(package_root, "runtime.txt")
    monkeypatch.setattr(validator, "PACKAGE_ROOT", package_root)

    validator.validate_publication_payload()


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
    assert lines and lines[0] == "---", path
    closing_index = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:closing_index]:
        key, separator, value = line.partition(":")
        assert separator and key and key not in fields, (path, line)
        fields[key] = value.strip()
    return fields


def _package_files() -> set[str]:
    """Return all regular release payload paths except the self-hashing manifest."""
    return {path.relative_to(PLUGIN_ROOT).as_posix() for path in _load_package_builder().iter_payload_files()}


def test_skill_roster_names_and_manifest_records_are_exact() -> None:
    """Prevent missing, renamed, retired, or silently added workflow skills."""
    skill_root = PLUGIN_ROOT / "skills"
    discovered = {path.parent.name for path in skill_root.glob("*/SKILL.md")}
    assert discovered == set(EXPECTED_SKILLS)
    assert {"review", "resolve"}.isdisjoint(discovered)

    for skill_id in EXPECTED_SKILLS:
        fields = _parse_frontmatter(skill_root / skill_id / "SKILL.md")
        assert fields["name"] == skill_id
        assert fields["description"]

    manifest = _load_json(PLUGIN_ROOT / "package-manifest.json")
    assert manifest["skills"] == [
        {"id": skill_id, "path": f"skills/{skill_id}/SKILL.md"} for skill_id in EXPECTED_SKILLS
    ]


def test_installed_markdown_has_no_source_checkout_only_paths() -> None:
    """Keep shipped skill and shared documentation usable from an installed cache."""
    markdown_files = [*sorted((PLUGIN_ROOT / "skills").rglob("*.md")), *sorted((PLUGIN_ROOT / "shared").rglob("*.md"))]
    for path in markdown_files:
        text = path.read_text(encoding="utf-8")
        assert "plugins/codex-rig/" not in text, path
        assert ".developments/" not in text, path


def test_skill_dependencies_are_cache_local_and_manifested() -> None:
    """Prevent installed skills from referring to missing or source-tree-only dependencies."""
    manifest = _load_json(PLUGIN_ROOT / "package-manifest.json")
    recorded_paths = {record["path"] for record in manifest["files"]}

    for skill_id in EXPECTED_SKILLS:
        skill_path = PLUGIN_ROOT / "skills" / skill_id / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        if skill_id == "agent-shims":
            assert "../../scripts/manage_role_agents.py" in text
            continue
        assert "../_shared/" not in text
        assert "../../shared/" in text

        dependencies = set(RELATIVE_DEPENDENCY.findall(text)) | {"result-template.json"}
        if skill_id == "code-review":
            dependencies.add("validate_artifacts.py")
        for relative in dependencies:
            dependency = (skill_path.parent / relative).resolve()
            assert dependency.is_relative_to(PLUGIN_ROOT), (skill_id, relative)
            assert dependency.is_file(), (skill_id, relative)
            assert dependency.relative_to(PLUGIN_ROOT).as_posix() in recorded_paths, (skill_id, relative)

    assert recorded_paths == _package_files()


def test_kaggle_reference_set_is_exact_and_manifested() -> None:
    """Prevent notebook composition stages from disappearing or gaining unreviewed inputs."""
    references_root = PLUGIN_ROOT / "skills" / "kaggle" / "references"
    discovered = {path.name for path in references_root.glob("*.md")}
    assert discovered == EXPECTED_KAGGLE_REFERENCES

    manifest = _load_json(PLUGIN_ROOT / "package-manifest.json")
    recorded_paths = {record["path"] for record in manifest["files"]}
    expected_paths = {f"skills/kaggle/references/{name}" for name in EXPECTED_KAGGLE_REFERENCES}
    assert expected_paths <= recorded_paths


def test_role_roster_frontmatter_and_runtime_records_are_exact() -> None:
    """Prevent specialist identity or execution defaults from drifting independently."""
    role_root = PLUGIN_ROOT / "roles"
    discovered = {path.parent.name for path in role_root.glob("*/ROLE.md")}
    assert discovered == set(EXPECTED_ROLES)

    expected_records = []
    for role_id, (model, sandbox_mode) in EXPECTED_ROLES.items():
        role_path = role_root / role_id / "ROLE.md"
        fields = _parse_frontmatter(role_path)
        assert fields == {
            "role_id": role_id,
            "name": f"codex-rig-{role_id}",
            "model": model,
            "model_reasoning_effort": "high",
            "approval_policy": "on-request",
            "sandbox_mode": sandbox_mode,
            "fallback_modes": "[shim, built-in-injected, inline]",
        }
        expected_records.append(
            {
                "id": role_id,
                "path": f"roles/{role_id}/ROLE.md",
                "sha256": hashlib.sha256(role_path.read_bytes()).hexdigest(),
                "runtime": {
                    "model": model,
                    "model_reasoning_effort": "high",
                    "approval_policy": "on-request",
                    "sandbox_mode": sandbox_mode,
                },
            }
        )

    manifest = _load_json(PLUGIN_ROOT / "package-manifest.json")
    assert manifest["roles"] == expected_records


def test_release_profile_declares_only_packaged_lifecycle_features() -> None:
    """Keep shim-manager and hook metadata aligned while MCP remains absent."""
    manifest = _load_json(PLUGIN_ROOT / "package-manifest.json")
    plugin = _load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
    assert plugin["description"].startswith("Thirteen evidence-first Codex workflows")
    assert plugin["interface"]["capabilities"] == [
        "13 workflow skills and 1 legacy-shim lifecycle manager",
        "15 specialist role cards",
        "Staged execution manifest validation",
        "Gated portable read-only auto execution",
        "Authenticated cleanup for prior agent shims",
        "Optional SessionStart health diagnostic",
        "Built-in and inline role fallback",
        "Quality gates",
        "Optional codemap-py structural-context integration",
    ]
    assert manifest["release_profile"] == "role-card-injected"
    assert manifest["features"] == {
        "manager": True,
        "hooks": True,
        "mcp": False,
        "generated_shims": False,
    }
    assert "hooks" not in plugin
    assert "mcpServers" not in plugin

    forbidden_roots = {"manager", "mcp", "shims"}
    for relative in _package_files():
        path = Path(relative)
        assert path.parts[0] not in forbidden_roots
        assert relative != ".mcp.json"
        assert not (path.name.startswith("codex-rig-") and path.suffix == ".toml")
    assert (PLUGIN_ROOT / "skills" / "agent-shims" / "SKILL.md").is_file()


def test_manage_and_sync_preserve_installed_plugin_state() -> None:
    """Prevent management workflows from treating the installed cache as editable source."""
    manage = _normalized_text(PLUGIN_ROOT / "skills" / "manage" / "SKILL.md").lower()
    for required in (
        "installed plugin tree is immutable input",
        "never edit this skill's plugin cache",
        "reject any target whose canonical path is inside the same installed plugin root",
        "bundled `agent-shims` workflow",
    ):
        assert required in manage

    sync = _normalized_text(PLUGIN_ROOT / "skills" / "sync" / "SKILL.md").lower()
    for required in (
        "never copy files into an installed cache",
        "sync never mutates external agent files",
        "before plugin removal, run `agent-shims remove`",
        "after refresh or reinstall, run `agent-shims doctor`",
        "codex plugin marketplace list --json",
        "codex plugin list --marketplace borda-ai-rig --json",
        "codex plugin marketplace upgrade borda-ai-rig",
        "codex plugin add codex-rig@borda-ai-rig",
        "preservation of unknown external agent files",
    ):
        assert required in sync


def test_audit_prompt_efficiency_requires_cost_and_value_evidence() -> None:
    """Prevent recurring skill audits from rewarding shorter but weaker instructions."""
    audit = _normalized_text(PLUGIN_ROOT / "skills" / "audit" / "SKILL.md")
    for required in (
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
    ):
        assert required in audit

    validator = _load_shared_artifact_validator()
    assert validator.SKILL_REQUIREMENTS["audit"] == {
        "files": {
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


def test_audit_calibration_rejects_length_only_prompt_compression() -> None:
    """Keep a scored adversarial case for prompt-cost optimization regressions."""
    cases = _load_json(PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json")["cases"]
    case = next(item for item in cases if item["id"] == "audit-prompt-efficiency-overcompression")
    assert case["target"] == "audit"
    assert case["expected_findings"] == [
        "length-only-optimization",
        "semantic-value-guard-missing",
        "loaded-reference-cost-omitted",
        "adversarial-review-missing",
    ]


def test_commit_contract_requires_exact_history_rewrite_authorization() -> None:
    """Prevent ordinary commit requests from authorizing edits to existing history."""
    contract = _normalized_text(PLUGIN_ROOT / "shared" / "commit-response-template.md").lower()
    for required in (
        "creating a new commit does not authorize rewriting an existing commit",
        "unless the user explicitly requests that exact history operation",
        "never infer rewrite permission from a commit, cleanup, or commit-diet request",
        "`git commit --amend`",
        "`git rebase`",
        "`git reset`",
    ):
        assert required in contract

    cases = _load_json(PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json")["cases"]
    history_case = next(case for case in cases if case["id"] == "manage-implicit-history-rewrite")
    assert history_case["expected_findings"] == ["history-rewrite-not-explicitly-authorized"]


def test_commit_contract_requires_descriptive_user_facing_handoffs() -> None:
    """Keep commit handoffs specific enough to audit without reading the diff."""
    contract = _normalized_text(PLUGIN_ROOT / "shared" / "commit-response-template.md").lower()
    for required in (
        "required user-facing commit summary",
        "hash and title",
        "behavior",
        "affected surfaces",
        "verification",
        "residual limits",
        "why the boundary exists",
        "never claim a check passed without concrete execution evidence",
    ):
        assert required in contract


def test_commit_contract_requires_detailed_changes_and_impacts() -> None:
    """Prevent terse commit bodies that hide behavior or operational impact."""
    contract = _normalized_text(PLUGIN_ROOT / "shared" / "commit-response-template.md").lower()
    for required in (
        "changes:",
        "impact:",
        "verification:",
        "residual limits:",
        "every meaningful behavioral, interface, workflow, policy, test, documentation, packaging, or operational change",
        "state the concrete user, developer, runtime, compatibility, or maintenance effect",
        "generic impact claims",
    ):
        assert required in contract


def test_commit_contract_keeps_verification_change_specific_and_compact() -> None:
    """Prevent verification chronology and unrelated gates from bloating commits."""
    contract = _normalized_text(PLUGIN_ROOT / "shared" / "commit-response-template.md").lower()
    for required in (
        "only final checks that materially validate the committed surfaces",
        "consolidate closely related checks",
        "report a required broad gate once using its final outcome",
        "exploratory probes",
        "failure-first reproductions",
        "repeated reruns",
        "unrelated repository-wide gates",
        "material change-specific acceptance gate",
    ):
        assert required in contract


def test_approval_contract_keeps_runtime_reason_short_and_prefix_safe() -> None:
    """Prevent approval UI prompts from duplicating commands or detailed pre-briefs."""
    native_contract = _normalized_text(PLUGIN_ROOT / "shared" / "native-skill-contract.md").lower()
    agent_contract = _normalized_text(PLUGIN_ROOT / "assets" / "AGENTS.md").lower()
    for contract in (native_contract, agent_contract):
        for required in (
            "all intentional approval requests",
            "short plain-english question",
            "outcome or material effect",
            "must not repeat the command, argv, flags, paths, multiline content, or full approval brief",
            "short categorical safe prefix",
            "omit `prefix_rule` for one-time or high-risk commands",
        ):
            assert required in contract

    commit_contract = _normalized_text(PLUGIN_ROOT / "shared" / "commit-response-template.md").lower()
    assert "application of the general approval contract" in commit_contract
    assert "do not create a temporary or persistent commit-message file" in commit_contract
    assert "one message argument" in commit_contract
    assert "--cleanup=verbatim" in commit_contract
    assert "must not repeat the command, flags, message body, or full approval brief" in commit_contract


def test_commit_contract_preserves_literal_message_and_failure_boundaries() -> None:
    """Reject unsafe interpolation, hidden file fallback, and automatic repair commits."""
    contract = _normalized_text(PLUGIN_ROOT / "shared" / "commit-response-template.md").lower()
    for required in (
        "show the complete secret-free message in chat",
        "shell-free argv",
        "posix",
        "powershell",
        "never use double-quoted shell interpolation",
        "command-size or encoding limits",
        "do not silently fall back to a file",
        "normalizing only one terminal lf",
        "raw git output",
        "on denial, failure, or mismatch, do not retry automatically or change the index",
        "do not amend",
        "full message may appear in the runtime command approval",
        "does not promise a fixed number of host approval prompts",
    ):
        assert required in contract


@pytest.mark.parametrize(
    "transport",
    [
        "argv",
        pytest.param("posix", marks=pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shell unavailable")),
        pytest.param("rtk", marks=pytest.mark.skipif(shutil.which("rtk") is None, reason="RTK executable unavailable")),
    ],
)
def test_file_free_commit_preserves_reviewed_message(tmp_path: Path, transport: str) -> None:
    """Preserve hostile-looking literal text without shell expansion or draft files."""
    contract = (PLUGIN_ROOT / "shared" / "commit-response-template.md").read_text(encoding="utf-8")
    command = re.search(r"`(rtk git commit --cleanup=verbatim -m <message>)`", contract)
    assert command is not None
    message = (
        "test(cli): preserve literal commit text\n\n"
        "Changes:\n- Keep 'apostrophes', \"quotes\", C:\\work\\path, and Unicode: příliš 日本語.\n"
        "- Preserve $(touch shell-expanded), `touch backtick-expanded`, $HOME, %PATH%, & | ; < >.\n\n"
        "Impact:\n- No evaluation or lossy transport.\n\n"
        "Verification:\n- Exact message and no side-effect files.\n"
        "# Keep this comment-looking line and trailing spaces.  \n\n"
        "Residual limits:\n- None known\n\n---\n\n"
        "Co-authored-by: Codex <codex@openai.com>"
    )
    # Isolate the disposable repository from caller Git routing and global hooks/config.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, env=env, capture_output=True)
    argv = shlex.split(command.group(1))
    argv = argv[1:-1] + [message, "--allow-empty"]
    argv[1:1] = ["-c", "user.name=Contract Test", "-c", "user.email=contract@example.invalid"]
    if transport == "rtk":
        argv.insert(0, shutil.which("rtk"))
    if transport == "posix":
        # Match the documented POSIX apostrophe encoding, not shell interpolation.
        quoted = ["'" + part.replace("'", "'\"'\"'") + "'" for part in argv]
        argv = [shutil.which("sh"), "-c", " ".join(quoted)]
    subprocess.run(argv, cwd=tmp_path, env=env, check=True, capture_output=True)
    stored = subprocess.run(
        ["git", "--no-pager", "show", "-s", "--format=format:%B", "HEAD"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    assert stored.removesuffix("\n") == message
    assert sorted(path.name for path in tmp_path.iterdir()) == [".git"]


def test_calibration_recurrence_cases_cover_each_escalation_stage() -> None:
    """Keep calibration fixtures aligned with the recurrence escalation contract."""
    cases = _load_json(PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json")["cases"]
    case_contract = {
        case["id"]: (case["target"], case["expected_findings"])
        for case in cases
        if case["id"].startswith("recurrence-")
    }

    assert case_contract == {
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
    }


def test_calibration_model_stall_cases_cover_advisory_and_human_escalation() -> None:
    """Keep model-stall calibration fixtures aligned with the escalation contract."""
    cases = _load_json(PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json")["cases"]
    case_contract = {
        case["id"]: (case["target"], case["expected_findings"])
        for case in cases
        if case["id"].startswith("model-stall-")
    }

    assert case_contract == {
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
                "closure-condition-not-recorded",
                "evidence-backed-attempt-escalation-required",
                "progress-without-closure-ledger-missing",
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
    }


@pytest.mark.installed_plugin
def test_calibration_model_stall_fixture_observations_are_scored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject an escalation case that has no scored fixture observation."""
    calibration_dir = PLUGIN_ROOT / "runtime" / "calibration"
    monkeypatch.syspath_prepend(str(calibration_dir))
    specification = importlib.util.spec_from_file_location(
        "codex_rig_model_stall_behavioral_score", calibration_dir / "score_behavioral.py"
    )
    assert specification is not None and specification.loader is not None
    scorer = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, scorer)
    specification.loader.exec_module(scorer)

    result = scorer._score(
        calibration_dir / "behavioral-cases.json",
        calibration_dir / "behavioral-observations.jsonl",
        calibration_dir / "live-route-policy.json",
        calibration_dir / "live-ab-tasks.json",
        PLUGIN_ROOT,
        layout="plugin",
    )

    stall_ids = {
        case["id"]
        for case in _load_json(calibration_dir / "behavioral-cases.json")["cases"]
        if case["id"].startswith("model-stall-")
    }
    scored_ids = {row["case_id"] for row in result["case_results"]}
    assert stall_ids <= scored_ids
    assert result["missing_case_ids"] == []


def test_archived_route_evidence_is_not_promoted_after_skill_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve historical paid evidence while exposing its current roster mismatch."""
    calibration_dir = PLUGIN_ROOT / "runtime" / "calibration"
    monkeypatch.syspath_prepend(str(calibration_dir))
    spec = importlib.util.spec_from_file_location("codex_rig_calibration_route_archive", calibration_dir / "run.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, runner)
    spec.loader.exec_module(runner)

    run = runner.CalibrationRun(paths=runner.Paths.create("plugin", tmp_path))
    runner.check_accepted_route_evidence(run)

    checks = run.paths.checks.read_text(encoding="utf-8")
    assert "accepted-route-evidence=archived-stale:skill-roster-mismatch" in checks
    assert run.accepted_route_evidence_current is False
    assert run.checks_failed == []
    recommendations, follow_up = runner.build_recommendations(
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


def test_calibration_recurrence_policy_link_is_limited_to_retry_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject both missing owner links and redundant links on linear workflows."""
    calibration_dir = PLUGIN_ROOT / "runtime" / "calibration"
    monkeypatch.syspath_prepend(str(calibration_dir))
    spec = importlib.util.spec_from_file_location("codex_rig_calibration_recurrence", calibration_dir / "run.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, runner)
    spec.loader.exec_module(runner)

    packaged_skills = tuple(sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")))
    packaged_roles = tuple(sorted((PLUGIN_ROOT / "roles").glob("*/ROLE.md")))
    linked_skills = {
        path.parent.name
        for path in packaged_skills
        if runner.RECURRENCE_POLICY_LINK in path.read_text(encoding="utf-8")
    }
    linked_roles = {
        path.parent.name for path in packaged_roles if runner.RECURRENCE_POLICY_LINK in path.read_text(encoding="utf-8")
    }
    assert linked_skills == {"code-remediate", "implement", "investigate"}
    assert linked_roles == {"delegation-lead"}
    assert runner.find_misplaced_packaged_recurrence_policy_links(packaged_skills, packaged_roles) == []

    for source, relative, is_role, remove_link in (
        (PLUGIN_ROOT / "skills" / "implement" / "SKILL.md", Path("skills/implement/SKILL.md"), False, True),
        (PLUGIN_ROOT / "skills" / "manage" / "SKILL.md", Path("skills/manage/SKILL.md"), False, False),
        (
            PLUGIN_ROOT / "roles" / "delegation-lead" / "ROLE.md",
            Path("roles/delegation-lead/ROLE.md"),
            True,
            True,
        ),
        (PLUGIN_ROOT / "roles" / "sw-engineer" / "ROLE.md", Path("roles/sw-engineer/ROLE.md"), True, False),
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True)
        shutil.copy2(source, target)
        skill_files = () if is_role else (target,)
        role_files = (target,) if is_role else ()
        assert runner.find_misplaced_packaged_recurrence_policy_links(skill_files, role_files) == []
        content = target.read_text(encoding="utf-8")
        if remove_link:
            content = content.replace(runner.RECURRENCE_POLICY_LINK, "")
        else:
            content += f"\n{runner.RECURRENCE_POLICY_LINK}\n"
        target.write_text(content, encoding="utf-8")
        assert runner.find_misplaced_packaged_recurrence_policy_links(skill_files, role_files) == [target]


def test_code_remediate_accepts_only_completed_intent_first_merge_states(tmp_path: Path) -> None:
    """Allow conflict-free evidence or one explicitly authorized completed merge."""
    validator = _load_shared_artifact_validator()
    pr_dir = tmp_path / "pr"
    pr_dir.mkdir()
    path = pr_dir / "merge-resolution.json"
    metadata = {
        "merge_resolution": {
            "artifact_path": str(path),
            "authorization": "not-required",
            "conflicts_detected": False,
            "status": "not-needed",
        }
    }
    target = {"local_head": "base-oid"}

    _write_merge_resolution(path)
    validator._validate_code_remediate_merge_resolution(metadata, pr_dir, target)

    _write_merge_resolution(
        path,
        conflicts_detected=True,
        status="completed",
        authorization="user-confirmed",
        post_merge_head="merge-oid",
        merge_commit="merge-oid",
        resolved_paths=["src/conflicted.py"],
        evidence=["merge-prestage.md", "pytest.log"],
    )
    metadata["merge_resolution"] = {
        "artifact_path": str(path),
        "authorization": "user-confirmed",
        "conflicts_detected": True,
        "status": "completed",
    }
    validator._validate_code_remediate_merge_resolution(metadata, pr_dir, target)


def test_code_remediate_rejects_conflicts_without_merge_authorization(tmp_path: Path) -> None:
    """Prevent review remediation from bypassing target-merge authorization."""
    validator = _load_shared_artifact_validator()
    pr_dir = tmp_path / "pr"
    pr_dir.mkdir()
    path = pr_dir / "merge-resolution.json"
    _write_merge_resolution(
        path,
        conflicts_detected=True,
        status="completed",
        authorization="not-required",
        post_merge_head="merge-oid",
        merge_commit="merge-oid",
        resolved_paths=["src/conflicted.py"],
    )
    metadata = {
        "merge_resolution": {
            "artifact_path": str(path),
            "authorization": "not-required",
            "conflicts_detected": True,
            "status": "completed",
        }
    }

    with pytest.raises(SystemExit, match="target-merge-authorization-required"):
        validator._validate_code_remediate_merge_resolution(metadata, pr_dir, {"local_head": "base-oid"})


def test_specialist_fallback_ladder_and_evidence_are_complete() -> None:
    """Prevent fallback routing from losing order, fidelity limits, or audit evidence."""
    policy = _normalized_text(PLUGIN_ROOT / "shared" / "specialist-orchestration.md")
    route_markers = (
        "1. A runtime-provided blank/default subagent",
        "2. An inline pass in the parent context",
        "3. `unavailable` when the runtime cannot provide any safe route",
    )
    positions = [policy.index(marker) for marker in route_markers]
    assert positions == sorted(positions)
    for field in (
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
    ):
        assert field in policy
    assert "Fallback only for route absence or rejection" in policy
    assert "Never retry another route because the specialist disagreed" in policy
    assert "`task_name` is provenance only" in policy
    assert "Persistent named shims are platform-blocked for routing" in policy


def test_specialist_wave_joins_before_acceptance_without_expanding_fanout() -> None:
    """Keep parallel scheduling bounded by fixed packs, ownership, and final join."""
    policy = _normalized_text(PLUGIN_ROOT / "shared" / "specialist-orchestration.md")

    assert "## Bounded Dispatch Wave" in policy
    assert "one approved dispatch wave" in policy
    assert "immutable packs" in policy
    assert "joins all handoffs before acceptance" in policy
    assert "A second wave is forbidden" in policy
    assert "parent-serially or stop and re-plan with the user" in policy
    assert "Never add fan-out, overlap ownership, bypass approval, or start dependencies" in policy
    assert "equal-gate serial fallback" in policy

    cases = _load_json(PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json")["cases"]
    case = next(item for item in cases if item["id"] == "delegation-lead-bounded-dispatch-wave")
    assert case["target"] == "delegation-lead"
    assert case["expected_findings"] == [
        "specialist-routes-or-packs-not-fixed",
        "specialist-fanout-expanded",
        "specialist-ownership-overlap",
        "dependent-work-dispatched-early",
        "specialist-approval-bypassed",
        "second-specialist-wave-forbidden",
        "specialist-handoff-join-missing",
        "serial-fallback-gates-weakened",
    ]


def test_public_payload_has_no_private_release_references() -> None:
    """Prevent internal planning identifiers and personal paths from entering the release."""
    forbidden_references = (
        "plugin-" + "package.json",
        "(" + "planned" + ")",
        "GA" + " candidate",
        "exact-candidate" + " native",
        "fresh" + " native",
        "repeat independent" + " QA/challenge",
        "currently released" + "/default",
    )
    violations: list[tuple[str, str]] = []
    for path in _load_package_builder().iter_payload_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(PLUGIN_ROOT).as_posix()
        for reference in forbidden_references:
            if reference in text:
                violations.append((relative, reference))
        if match := PRIVATE_PLAN_REFERENCE.search(text):
            violations.append((relative, match.group(1)))
        if match := PRIVATE_WORK_ITEM.search(text):
            violations.append((relative, match.group(0)))
        if match := PRIVATE_ROLLOUT_PHASE.search(text):
            violations.append((relative, match.group(0)))
        if match := PERSONAL_ABSOLUTE_PATH.search(text):
            violations.append((relative, match.group(0)))

    assert violations == []


@pytest.fixture(name="canonical_parallel_flow")
def _canonical_parallel_flow() -> tuple[Path, str, str]:
    """Load the one released text flow that carries every canonical endpoint."""
    architecture = PLUGIN_ROOT / "ARCHITECTURE.md"
    architecture_text = architecture.read_text(encoding="utf-8")
    flow_blocks = re.findall(r"```text\n(.*?)\n```", architecture_text, flags=re.DOTALL)
    matching_flows = [
        flow
        for flow in flow_blocks
        if re.search(r"✓\s+YES\b", flow)
        and re.search(r"✗\s+NO\b", flow)
        and all(endpoint in flow for endpoint in ("STOP", "re-plan", "FAIL", "ACCEPT"))
    ]
    assert len(matching_flows) == 1
    return architecture, architecture_text, matching_flows[0]


class TestParallelExecutionDocumentation:
    """Protect the canonical bounded-execution documentation contract."""

    def test_has_canonical_gate_definitions(self, canonical_parallel_flow: tuple[Path, str, str]) -> None:
        """Keep gate definitions self-contained and linked from every consumer."""
        architecture, architecture_text, _ = canonical_parallel_flow
        canonical_anchor = "#canonical-g0g8-execution-flow"

        gate_definitions = re.findall(r"^- \*\*G([0-8])\b", architecture_text, flags=re.MULTILINE)
        assert gate_definitions == [str(index) for index in range(9)]
        assert re.findall(r"^  - \*\*G5a\b.*terminal", architecture_text, flags=re.IGNORECASE | re.MULTILINE)
        assert re.findall(r"^  - \*\*G5b\b.*join", architecture_text, flags=re.IGNORECASE | re.MULTILINE)
        assert re.findall(r"^  - \*\*G5c\b.*derivation", architecture_text, flags=re.IGNORECASE | re.MULTILINE)

        committed_docs = (
            architecture,
            PLUGIN_ROOT / "README.md",
            PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md",
            PLUGIN_ROOT / "skills" / "implement" / "SKILL.md",
            PLUGIN_ROOT / "skills" / "manage" / "SKILL.md",
        )
        for path in committed_docs:
            text = path.read_text(encoding="utf-8")
            assert ".plans/" not in text, path
            assert "plan_multi-agent-parallelization" not in text, path
            if path != architecture:
                assert canonical_anchor in text, path

    def test_has_centered_two_column_gate_cells(self, canonical_parallel_flow: tuple[Path, str, str]) -> None:
        """Keep every bounded gate and endpoint centered inside its cells."""
        _, _, flow = canonical_parallel_flow
        assert "[" not in flow and "]" not in flow
        assert "{" not in flow and "}" not in flow
        assert "-->" not in flow
        assert "✓ YES" in flow and "✗ NO" in flow
        assert max(len(line) for line in flow.splitlines()) <= 100
        flow_lines = flow.splitlines()
        boxed_lines = [line.strip() for line in flow.splitlines() if line.strip().startswith("│")]
        assert boxed_lines
        assert all(line.endswith("│") for line in boxed_lines)
        expected_gate_cells = Counter(
            [*(f"G{index}" for index in range(9)), "G4", "G4", *(f"G5{suffix}" for suffix in "abc")]
        )
        observed_gate_cells: Counter[str] = Counter()
        for line_index, line in enumerate(flow_lines):
            separators = [index for index, character in enumerate(line) if character == "│"]
            for left, right in zip(separators, separators[1:]):
                cell = line[left + 1 : right]
                content = cell.strip()
                if not content:
                    continue
                left_padding = len(cell) - len(cell.lstrip())
                right_padding = len(cell) - len(cell.rstrip())
                assert abs(left_padding - right_padding) <= 1, (line_index, content, left_padding, right_padding)
                if content not in expected_gate_cells:
                    continue
                observed_gate_cells[content] += 1
                assert flow_lines[line_index - 1][right] == "┬", (line_index, content, "top")
                assert flow_lines[line_index + 1][right] == "┴", (line_index, content, "bottom")
                following_separators = [index for index in separators if index > right]
                assert following_separators
                assert line[right + 1 : following_separators[0]].strip()
        assert observed_gate_cells == expected_gate_cells
        assert all(
            re.search(rf"│[^│]*{re.escape(endpoint)}[^│]*│", flow) for endpoint in ("STOP", "re-plan", "FAIL", "ACCEPT")
        )
        assert all(
            line.strip().startswith("│") and line.strip().endswith("│") for line in flow.splitlines() if "?" in line
        )

    def test_has_horizontal_centered_forks(self, canonical_parallel_flow: tuple[Path, str, str]) -> None:
        """Keep every decision fork horizontal and attached to its source center."""
        _, _, flow = canonical_parallel_flow
        flow_lines = flow.splitlines()

        for question_index, line in enumerate(flow_lines):
            if "?" not in line:
                continue
            bottom_centers = [
                index for index, character in enumerate(flow_lines[question_index + 1]) if character == "┬"
            ]
            fork_centers = [index for index, character in enumerate(flow_lines[question_index + 2]) if character == "┴"]
            assert bottom_centers == fork_centers
            fork_window = flow_lines[question_index + 1 : question_index + 5]
            assert any("✓ YES" in fork_line and "✗ NO" in fork_line for fork_line in fork_window)

    def test_has_continuous_connectors(self, canonical_parallel_flow: tuple[Path, str, str]) -> None:
        """Keep arrows, elbows, and the three-source join connected by column."""
        _, _, flow = canonical_parallel_flow
        flow_lines = flow.splitlines()

        for arrow_index, line in enumerate(flow_lines[:-1]):
            arrow_centers = [index for index, character in enumerate(line) if character == "▼"]
            if not arrow_centers:
                continue
            box_centers = [
                match.start() + (match.end() - match.start() - 1) // 2
                for match in re.finditer(r"┌[─┬]+┐", flow_lines[arrow_index + 1])
            ]
            assert len(arrow_centers) == len(box_centers)
            assert all(abs(arrow - box) <= 1 for arrow, box in zip(arrow_centers, box_centers)), (
                arrow_index,
                arrow_centers,
                box_centers,
            )
            if len(arrow_centers) == 1:
                arrow = arrow_centers[0]
                assert flow_lines[arrow_index - 1][arrow] in {"┬", "┐", "┼"}, (arrow_index, arrow, "source")

        for connector_index, line in enumerate(flow_lines):
            if re.fullmatch(r"\s*└─+┐", line):
                start = line.index("└")
                end = line.index("┐")
                assert flow_lines[connector_index - 1][start] == "┬", (connector_index, start, "elbow-source")
                assert flow_lines[connector_index + 1][end] == "▼", (connector_index, end, "elbow-target")
            if "┼" in line:
                merge_markers = [index for index, character in enumerate(line) if character in {"└", "┼", "┘"}]
                source_connectors = [
                    index for index, character in enumerate(flow_lines[connector_index - 1]) if character == "┬"
                ]
                assert source_connectors == merge_markers
                assert flow_lines[connector_index + 1][line.index("┼")] == "▼"
