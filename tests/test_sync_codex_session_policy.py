"""Acceptance checks for root-level Codex-home session-policy synchronization."""

from __future__ import annotations

import runpy
import re
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_codex_session_policy.py"
SOURCE_CONFIG = ROOT / ".codex" / "config.toml"
SOURCE_POLICY = ROOT / ".codex" / "global-session-policy.md"


@pytest.fixture
def legacy_source_config(tmp_path: Path) -> Path:
    """Retain model-only source coverage when repository review defaults evolve."""
    source = tmp_path / "legacy-config.toml"
    source.write_text('model = "gpt-6.1-sol"\nreview_model = "gpt-6.1-sol"\n', encoding="utf-8")
    return source


def _namespace() -> dict[str, object]:
    """Load the sync script's definitions without entering its command-line synchronization path.

    >>> namespace = _namespace()
    >>> namespace["sync"].__name__, namespace["SyncError"].__name__
    ('sync', 'SyncError')
    """
    return runpy.run_path(str(SCRIPT))


def test_sync_projects_actual_repository_defaults_without_replacing_user_configuration(tmp_path: Path) -> None:
    """Prevent root policy sync from replacing unrelated Codex settings."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-5.6-luna"\ncustom = true\n', encoding="utf-8")
    (home / "AGENTS.md").write_text("User instructions.\n", encoding="utf-8")

    namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

    projected = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    repository = tomllib.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    expected = {"model": "gpt-6.1-sol", "review_model": "gpt-6.1-sol", "custom": True}
    if "approvals_reviewer" in repository:
        expected["approvals_reviewer"] = repository["approvals_reviewer"]
    if "extra_policy" in repository.get("auto_review", {}):
        expected["auto_review"] = {"extra_policy": repository["auto_review"]["extra_policy"]}
    assert projected == expected
    instructions = (home / "AGENTS.md").read_text(encoding="utf-8")
    assert instructions.startswith("User instructions.\n")
    assert instructions.count("borda-local:session-model-policy begin") == 1
    assert SOURCE_POLICY.read_text(encoding="utf-8") in instructions


def test_sync_is_idempotent_and_rejects_tampered_policy_block(tmp_path: Path) -> None:
    """Keep user-owned instructions safe while allowing repeat sync runs."""
    namespace = _namespace()
    home = tmp_path / "codex-home"

    namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)
    first = (home / "AGENTS.md").read_text(encoding="utf-8")
    namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == first

    target = home / "AGENTS.md"
    target.write_text(first.replace("Normal parent sessions use", "Edited policy."), encoding="utf-8")
    with pytest.raises(namespace["SyncError"], match="modified"):
        namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)


def test_sync_converts_an_exact_terminal_policy_copy_to_the_managed_block(tmp_path: Path) -> None:
    """Avoid duplicating a policy that predated ownership markers."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    policy = SOURCE_POLICY.read_text(encoding="utf-8")
    (home / "AGENTS.md").write_text(f"User instructions.\n\n{policy}", encoding="utf-8")

    namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

    instructions = (home / "AGENTS.md").read_text(encoding="utf-8")
    assert instructions.count("Normal parent sessions use") == 1
    assert instructions.count("borda-local:session-model-policy begin") == 1


def test_sync_inserts_missing_root_setting_before_toml_tables(tmp_path: Path, legacy_source_config: Path) -> None:
    """Keep a missing root setting out of an unrelated TOML table."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        'model = "gpt-5.6-luna"\n\n[agents.example]\nname = "example"\n', encoding="utf-8"
    )

    namespace["sync"](legacy_source_config, SOURCE_POLICY, home)

    assert (home / "config.toml").read_text(encoding="utf-8") == (
        'model = "gpt-6.1-sol"\n\nreview_model = "gpt-6.1-sol"\n[agents.example]\nname = "example"\n'
    )


def test_sync_updates_single_quoted_root_settings_without_appending_duplicates(
    tmp_path: Path, legacy_source_config: Path
) -> None:
    """Accept valid TOML literal strings in a user-owned target configuration."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        "model = 'gpt-5.6-luna'\nreview_model = 'gpt-5.6-luna'\ncustom = true\n", encoding="utf-8"
    )

    namespace["sync"](legacy_source_config, SOURCE_POLICY, home)

    assert (home / "config.toml").read_text(encoding="utf-8") == (
        'model = "gpt-6.1-sol"\nreview_model = "gpt-6.1-sol"\ncustom = true\n'
    )


def test_sync_updates_quoted_root_keys_and_leading_whitespace_without_duplicates(
    tmp_path: Path, legacy_source_config: Path
) -> None:
    """Accept valid TOML root-key spellings without appending semantic duplicates."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        "  \"model\" = 'gpt-5.6-luna' # parent\n'review_model'='gpt-5.6-luna'\ncustom = true\n",
        encoding="utf-8",
    )

    namespace["sync"](legacy_source_config, SOURCE_POLICY, home)

    assert (home / "config.toml").read_text(encoding="utf-8") == (
        '  "model" = "gpt-6.1-sol" # parent\n\'review_model\'="gpt-6.1-sol"\ncustom = true\n'
    )


def test_source_models_accepts_literal_strings(tmp_path: Path) -> None:
    """Allow the repository source to use valid one-line TOML literal strings."""
    namespace = _namespace()
    source = tmp_path / "config.toml"
    source.write_text(
        "  \"model\" = 'gpt-5.6-terra'\n'review_model'='gpt-5.6-terra'\n",
        encoding="utf-8",
    )

    assert namespace["_source_models"](source) == {
        "model": "'gpt-5.6-terra'",
        "review_model": "'gpt-5.6-terra'",
    }


def test_sync_rejects_unsupported_model_assignment_without_writing(tmp_path: Path) -> None:
    """Fail closed instead of appending a duplicate for an unsupported TOML string form."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    config = home / "config.toml"
    original = 'model = """gpt-5.6-luna"""\nreview_model = "gpt-5.6-luna"\n'
    config.write_text(original, encoding="utf-8")

    with pytest.raises(namespace["SyncError"], match="unsupported model string assignment"):
        namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

    assert config.read_text(encoding="utf-8") == original
    assert not (home / "AGENTS.md").exists()


def test_model_only_source_preserves_existing_review_configuration(tmp_path: Path, legacy_source_config: Path) -> None:
    """Omitted optional settings never clear the user's existing review configuration."""
    namespace = _namespace()
    home = tmp_path / "home"
    home.mkdir()
    original = 'approvals_reviewer = "user"\n[auto_review]\npolicy = "primary"\nextra_policy = "local"\n'
    (home / "config.toml").write_text(original, encoding="utf-8")
    namespace["sync"](legacy_source_config, SOURCE_POLICY, home, install_policy=False)
    result = (home / "config.toml").read_text(encoding="utf-8")
    assert result.startswith('approvals_reviewer = "user"\nmodel = "gpt-6.1-sol"\nreview_model = "gpt-6.1-sol"\n')
    assert result.endswith('[auto_review]\npolicy = "primary"\nextra_policy = "local"\n')


@pytest.mark.parametrize("literal", ['"""old\ntext\n"""', "'''old\ntext\n'''", '""""""'])
def test_sync_replaces_multiline_extra_policy_and_preserves_unicode(tmp_path: Path, literal: str) -> None:
    """Replace complete multiline strings and preserve non-BMP Unicode policy content."""
    namespace = _namespace()
    source = tmp_path / "source.toml"
    source.write_text(
        'model = "primary"\nreview_model = "reviewer"\n[auto_review]\nextra_policy = "Policy 🚀"\n', encoding="utf-8"
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        f'["auto_review"]\nextra_policy = {literal} # retained\npolicy = "primary"\n', encoding="utf-8"
    )
    namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert 'extra_policy = "Policy 🚀" # retained\n' in text
    assert tomllib.loads(text)["auto_review"] == {"extra_policy": "Policy 🚀", "policy": "primary"}


@pytest.mark.parametrize(
    "target",
    [
        'approvals_reviewer = "auto_review"\napprovals_reviewer = "user"\n',
        '[auto_review]\nextra_policy = "first"\nextra_policy = "second"\n',
    ],
)
def test_duplicate_review_target_stops_before_writes(tmp_path: Path, legacy_source_config: Path, target: str) -> None:
    """Reject ambiguous user target settings instead of silently overwriting them."""
    namespace = _namespace()
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.toml"
    config.write_text(target, encoding="utf-8")
    with pytest.raises(namespace["SyncError"], match="invalid config TOML"):
        namespace["sync"](legacy_source_config, SOURCE_POLICY, home)
    assert config.read_text(encoding="utf-8") == target
    assert not (home / "AGENTS.md").exists()


def test_sync_can_project_model_defaults_without_changing_agent_instructions(tmp_path: Path) -> None:
    """Preserve the global-agent opt-out while projecting repository defaults."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    original_instructions = "User instructions must remain unchanged.\n"
    (home / "AGENTS.md").write_text(original_instructions, encoding="utf-8")

    namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home, install_policy=False)

    assert 'model = "gpt-6.1-sol"' in (home / "config.toml").read_text(encoding="utf-8")
    assert 'review_model = "gpt-6.1-sol"' in (home / "config.toml").read_text(encoding="utf-8")
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == original_instructions


def test_sync_projects_optional_auto_review_policy_without_replacing_primary_policy(tmp_path: Path) -> None:
    """Copy append-only review policy while retaining user primary policy and table settings."""
    namespace = _namespace()
    source = tmp_path / "source.toml"
    source.write_text(
        'model = "primary"\nreview_model = "reviewer"\napprovals_reviewer = "auto_review"\n'
        '[auto_review]\nextra_policy = """First line.\n[profile] is policy text.\nLast line.\n"""\n',
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    original_policy = 'policy = """User policy.\n[auto_review] is still policy text.\n""" # retain\n'
    (home / "config.toml").write_text(
        "custom = true\n[auto_review]\n" + original_policy + "extra_policy = 'old' # local\nthreshold = 7\n"
        '[profile]\nextra_policy = "profile-specific"\n',
        encoding="utf-8",
    )

    namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)

    result = (home / "config.toml").read_text(encoding="utf-8")
    assert 'approvals_reviewer = "auto_review"\n' in result
    assert original_policy in result
    assert 'extra_policy = "First line.\\n[profile] is policy text.\\nLast line.\\n" # local\n' in result
    assert 'threshold = 7\n[profile]\nextra_policy = "profile-specific"\n' in result
    namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)
    assert (home / "config.toml").read_text(encoding="utf-8") == result


@pytest.mark.parametrize(
    "invalid",
    [
        'approvals_reviewer = "auto_review"\napprovals_reviewer = "user"\n',
        '[auto_review]\nextra_policy = "first"\nextra_policy = "second"\n',
        "[auto_review]\nextra_policy = 42\n",
        '[auto_review]\nextra_policy = "unterminated\n',
    ],
)
def test_invalid_auto_review_source_stops_before_writes(tmp_path: Path, invalid: str) -> None:
    """Reject duplicate, malformed and nonstring owned review settings before touching target state."""
    namespace = _namespace()
    source = tmp_path / "source.toml"
    source.write_text('model = "primary"\nreview_model = "reviewer"\n' + invalid, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    target = home / "config.toml"
    target.write_text("custom = true\n", encoding="utf-8")

    with pytest.raises(namespace["SyncError"]):
        namespace["sync"](source, SOURCE_POLICY, home)

    assert target.read_text(encoding="utf-8") == "custom = true\n"
    assert not (home / "AGENTS.md").exists()


@pytest.mark.parametrize(
    "location, delimiter, embedded_key",
    [
        pytest.param(location, delimiter, key, id=f"{location}-{name}-{key}")
        for location in ("source", "target")
        for delimiter, name in (('"""', "basic"), ("'''", "literal"))
        for key in ("model", "approvals_reviewer")
    ],
)
def test_sync_preserves_nested_multiline_values(
    tmp_path: Path, location: str, delimiter: str, embedded_key: str
) -> None:
    """Keep array-contained policy-like text inert while updating actual root settings."""
    namespace = _namespace()
    notes = (
        f'notes = [\n{delimiter}\n{embedded_key} = "inside"\n[auto_review]\nextra_policy = "inside"\n{delimiter}\n]\n'
    )
    defaults = 'model = "primary"\nreview_model = "reviewer"\napprovals_reviewer = "auto_review"\n'
    source = tmp_path / "source.toml"
    source_text = (notes if location == "source" else "") + defaults + '[auto_review]\nextra_policy = "approved"\n'
    source.write_text(source_text, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    original = (notes if location == "target" else "") + 'model = "old"\nreview_model = "old"\n'
    config = home / "config.toml"
    config.write_text(original, encoding="utf-8")

    namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)

    result = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(result)
    expected = {
        "model": "primary",
        "review_model": "reviewer",
        "approvals_reviewer": "auto_review",
        "auto_review": {"extra_policy": "approved"},
    }
    if location == "target":
        assert result.startswith(notes)
        expected["notes"] = tomllib.loads(original)["notes"]
    assert parsed == expected
    assert source.read_text(encoding="utf-8") == source_text
    namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)
    assert config.read_text(encoding="utf-8") == result


@pytest.mark.parametrize(
    "notes",
    [
        pytest.param('notes = [\n"""\napprovals_reviewer = "user"\n"""\n]\n', id="reviewer-content-no-table"),
        pytest.param('notes = [\n"""\nEscaped delimiter: \\"""\nmodel = "inside"\n"""\n]\n', id="escaped-delimiter"),
    ],
)
def test_sync_inserts_root_reviewer_without_rewriting_nested_text(tmp_path: Path, notes: str) -> None:
    """Prevent silent nested-text mutation even when no table or extra policy triggers another check."""
    namespace = _namespace()
    source = tmp_path / "source.toml"
    source.write_text(
        'model = "primary"\nreview_model = "reviewer"\napprovals_reviewer = "auto_review"\n', encoding="utf-8"
    )
    home = tmp_path / "home"
    home.mkdir()
    original = notes + 'model = "old"\nreview_model = "old"\n'
    config = home / "config.toml"
    config.write_text(original, encoding="utf-8")

    namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)

    result = config.read_text(encoding="utf-8")
    assert result.startswith(notes)
    assert tomllib.loads(result) == {
        "notes": tomllib.loads(original)["notes"],
        "model": "primary",
        "review_model": "reviewer",
        "approvals_reviewer": "auto_review",
    }
    namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)
    assert config.read_text(encoding="utf-8") == result


def test_repository_preapproval_names_only_project_plugin_family() -> None:
    """Keep unrelated installed vendors outside this repository's recipe preapproval."""
    policy = tomllib.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))["auto_review"]["extra_policy"]
    for identity in ("codex-rig@borda-ai-rig", "codemap-py@borda-ai-rig", "bridge@borda-ai-rig"):
        assert identity in policy
    assert "only these project plugins" in policy
    assert "every skill and flow in these plugins" in policy
    assert "Normal substitutions" in policy
    assert "Never approve git push" in policy


def test_repository_bridge_preapproval_preserves_conditional_scope() -> None:
    """Pin the Bridge policy boundary without claiming a live auto-review approval decision."""
    policy = tomllib.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))["auto_review"]["extra_policy"]
    assert set(re.findall(r"\bbridge_[a-z_]+\b", policy)) == {
        "bridge_status",
        "bridge_bind_workspace",
        "bridge_advise",
        "bridge_review",
        "bridge_implement",
    }
    assert "verified installed and loaded" in policy
    assert "ordinary provider inference" in policy
    assert "already authorized task" in policy
    assert "current workspace binding" in policy
    assert "implementation stays within the authorized edit scope" in policy
    assert "Keep native user workspace confirmation" in policy
    assert "setup changes, authentication, and setup verify-live" in policy
    assert "not an arbitrary MCP, shell, or interpreter grant" in policy
    assert "paid calls not preapproved above" in policy
