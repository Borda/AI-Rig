"""Acceptance checks for root-level Codex-home session-policy synchronization."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_codex_session_policy.py"
SOURCE_CONFIG = ROOT / ".codex" / "config.toml"
SOURCE_POLICY = ROOT / ".codex" / "global-session-policy.md"


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

    assert (home / "config.toml").read_text(encoding="utf-8") == (
        'model = "gpt-6-sol"\ncustom = true\nreview_model = "gpt-6-sol"\n'
    )
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


def test_sync_inserts_missing_root_setting_before_toml_tables(tmp_path: Path) -> None:
    """Keep a missing root setting out of an unrelated TOML table."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        'model = "gpt-5.6-luna"\n\n[agents.example]\nname = "example"\n', encoding="utf-8"
    )

    namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

    assert (home / "config.toml").read_text(encoding="utf-8") == (
        'model = "gpt-6-sol"\n\nreview_model = "gpt-6-sol"\n[agents.example]\nname = "example"\n'
    )


def test_sync_updates_single_quoted_root_settings_without_appending_duplicates(tmp_path: Path) -> None:
    """Accept valid TOML literal strings in a user-owned target configuration."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        "model = 'gpt-5.6-luna'\nreview_model = 'gpt-5.6-luna'\ncustom = true\n", encoding="utf-8"
    )

    namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

    assert (home / "config.toml").read_text(encoding="utf-8") == (
        'model = "gpt-6-sol"\nreview_model = "gpt-6-sol"\ncustom = true\n'
    )


def test_sync_updates_quoted_root_keys_and_leading_whitespace_without_duplicates(tmp_path: Path) -> None:
    """Accept valid TOML root-key spellings without appending semantic duplicates."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        "  \"model\" = 'gpt-5.6-luna' # parent\n'review_model'='gpt-5.6-luna'\ncustom = true\n",
        encoding="utf-8",
    )

    namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

    assert (home / "config.toml").read_text(encoding="utf-8") == (
        '  "model" = "gpt-6-sol" # parent\n\'review_model\'="gpt-6-sol"\ncustom = true\n'
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


def test_sync_can_project_model_defaults_without_changing_agent_instructions(tmp_path: Path) -> None:
    """Preserve the global-agent opt-out while projecting repository defaults."""
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    original_instructions = "User instructions must remain unchanged.\n"
    (home / "AGENTS.md").write_text(original_instructions, encoding="utf-8")

    namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home, install_policy=False)

    assert 'model = "gpt-6-sol"' in (home / "config.toml").read_text(encoding="utf-8")
    assert 'review_model = "gpt-6-sol"' in (home / "config.toml").read_text(encoding="utf-8")
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == original_instructions
