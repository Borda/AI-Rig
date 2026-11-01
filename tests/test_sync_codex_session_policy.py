"""Acceptance checks for root-level Codex-home session-policy synchronization.

Covers the sync script that projects ``.codex/config.toml`` and ``.codex/global-session-policy.md`` into a user's Codex
home, the Bridge consent it must preserve, and the policy text (auto-review ``extra_policy``, ``.codex/README.md``,
Codex Rig siblings) that the projection ships.
"""

from __future__ import annotations

import io
import json
import re
import runpy
import sys
from pathlib import Path

import pytest
from _repo_phrases import missing_phrases

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_codex_session_policy.py"
SOURCE_CONFIG = ROOT / ".codex" / "config.toml"
SOURCE_POLICY = ROOT / ".codex" / "global-session-policy.md"
CONSENT_BLOCK = ROOT / "plugins/bridge_cc-codex/rules/claude-transfer-policy.txt"

#: Anchors of the core local Git grant-scope paragraph that every Codex policy-sibling copy carries byte-for-byte.
_CORE_GIT_PARAGRAPH_START = (
    "Task-scoped local Git work other than a plain commit runs without an authorization question"
)
_CORE_GIT_PARAGRAPH_END = "it does not require inventing Git work or committing unrelated changes."

#: Basic and literal multiline TOML strings holding policy-like text, crossed with the root key they embed.
_NESTED_NOTES = [
    pytest.param('"""', "model", id="basic-model"),
    pytest.param('"""', "approvals_reviewer", id="basic-approvals_reviewer"),
    pytest.param("'''", "model", id="literal-model"),
    pytest.param("'''", "approvals_reviewer", id="literal-approvals_reviewer"),
]
_PRIMARY_DEFAULTS = 'model = "primary"\nreview_model = "reviewer"\napprovals_reviewer = "auto_review"\n'


class _TtyStream(io.StringIO):
    """In-memory text stream that reports itself as an interactive terminal."""

    def isatty(self) -> bool:
        """Report an interactive terminal, as the consent prompt requires."""
        return True


@pytest.fixture(scope="module")
def namespace() -> dict[str, object]:
    """Load the sync script's definitions once without entering its command-line synchronization path.

    The script holds only constants and functions, so sharing the loaded namespace between tests leaks no state.
    """
    return runpy.run_path(str(SCRIPT))


@pytest.fixture(scope="module")
def extra_policy() -> str:
    """Return the repository's auto-review ``extra_policy`` string, parsed from ``.codex/config.toml``."""
    return tomllib.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))["auto_review"]["extra_policy"]


@pytest.fixture(scope="module")
def readme_prose() -> str:
    """Return ``.codex/README.md`` with whitespace runs collapsed, so phrase checks survive a paragraph reflow."""
    return " ".join((ROOT / ".codex/README.md").read_text(encoding="utf-8").split())


@pytest.fixture
def legacy_source_config(tmp_path: Path) -> Path:
    """Retain model-only source coverage when repository review defaults evolve."""
    source = tmp_path / "legacy-config.toml"
    source.write_text('model = "gpt-6.1-sol"\nreview_model = "gpt-6.1-sol"\n', encoding="utf-8")
    return source


@pytest.fixture
def synced_home(tmp_path: Path, namespace: dict[str, object]) -> Path:
    """Return a Codex home that has been synced once from the repository sources."""
    home = tmp_path / "codex-home"
    namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)
    return home


def _markdown_text(relative: str) -> str:
    """Read one repository Markdown sibling as written."""
    return (ROOT / relative).read_text(encoding="utf-8")


def _core_git_paragraph(text: str) -> str:
    """Return the core local Git grant-scope paragraph as one sibling's text states it, anchors included."""
    start = text.index(_CORE_GIT_PARAGRAPH_START)
    return text[start : text.index(_CORE_GIT_PARAGRAPH_END, start) + len(_CORE_GIT_PARAGRAPH_END)]


def _optional_projection(repository: dict[str, object]) -> dict[str, object]:
    """Return the optional settings the repository config defines, as sync projects them into a user's config."""
    projected: dict[str, object] = {}
    if "approvals_reviewer" in repository:
        projected["approvals_reviewer"] = repository["approvals_reviewer"]
    if "extra_policy" in repository.get("auto_review", {}):
        projected["auto_review"] = {"extra_policy": repository["auto_review"]["extra_policy"]}
    return projected


def _nested_notes(delimiter: str, key: str) -> str:
    """Return a TOML ``notes`` array whose multiline string embeds policy-like text for ``key``."""
    return f'notes = [\n{delimiter}\n{key} = "inside"\n[auto_review]\nextra_policy = "inside"\n{delimiter}\n]\n'


class TestSync:
    """Projecting the repository sources into a Codex home."""

    def test_projects_repository_defaults_and_keeps_user_settings(
        self, tmp_path: Path, namespace: dict[str, object]
    ) -> None:
        """Root policy sync replaces the managed model settings but no unrelated Codex setting.

        The user's home already holds a different model, a custom flag and its own instructions; only the managed keys
        and the delimited policy block may change.
        """
        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text('model = "gpt-5.6-luna"\ncustom = true\n', encoding="utf-8")
        (home / "AGENTS.md").write_text("User instructions.\n", encoding="utf-8")
        repository = tomllib.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))

        namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

        projected = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        instructions = (home / "AGENTS.md").read_text(encoding="utf-8")
        expected = {"model": "gpt-6.1-sol", "review_model": "gpt-6.1-sol", "custom": True}
        assert projected == {**expected, **_optional_projection(repository)}
        assert instructions.startswith("User instructions.\n")
        assert instructions.count("borda-local:session-model-policy begin") == 1
        assert SOURCE_POLICY.read_text(encoding="utf-8") in instructions

    def test_is_idempotent(self, synced_home: Path, namespace: dict[str, object]) -> None:
        """A second sync leaves the instructions file byte-identical.

        Repeat runs (``make sync-codex`` after every pull) must not stack another policy block onto the first.
        """
        first = (synced_home / "AGENTS.md").read_text(encoding="utf-8")

        namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, synced_home)

        assert (synced_home / "AGENTS.md").read_text(encoding="utf-8") == first

    def test_rejects_tampered_policy_block(self, synced_home: Path, namespace: dict[str, object]) -> None:
        """A hand-edited managed policy block stops the sync instead of being overwritten.

        The block is delimited and hashed so user-owned edits inside it are detected and never silently discarded.
        """
        target = synced_home / "AGENTS.md"
        target.write_text(target.read_text(encoding="utf-8").replace("Normal parent sessions use", "Edited."), "utf-8")

        with pytest.raises(namespace["SyncError"], match="modified"):
            namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, synced_home)

    def test_converts_exact_terminal_policy_copy_to_managed_block(
        self, tmp_path: Path, namespace: dict[str, object]
    ) -> None:
        """A policy copy that predates ownership markers is adopted, not duplicated.

        The instructions file ends with the exact policy text but no begin/end markers; sync must wrap that copy rather
        than append a second one.
        """
        home = tmp_path / "codex-home"
        home.mkdir()
        policy = SOURCE_POLICY.read_text(encoding="utf-8")
        (home / "AGENTS.md").write_text(f"User instructions.\n\n{policy}", encoding="utf-8")

        namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

        instructions = (home / "AGENTS.md").read_text(encoding="utf-8")
        assert instructions.count("Normal parent sessions use") == 1
        assert instructions.count("borda-local:session-model-policy begin") == 1

    @pytest.mark.parametrize(
        ("existing", "expected"),
        [
            pytest.param(
                'model = "gpt-5.6-luna"\n\n[agents.example]\nname = "example"\n',
                'model = "gpt-6.1-sol"\n\nreview_model = "gpt-6.1-sol"\n[agents.example]\nname = "example"\n',
                id="missing-root-setting-stays-before-toml-tables",
            ),
            pytest.param(
                "model = 'gpt-5.6-luna'\nreview_model = 'gpt-5.6-luna'\ncustom = true\n",
                'model = "gpt-6.1-sol"\nreview_model = "gpt-6.1-sol"\ncustom = true\n',
                id="single-quoted-root-settings-without-duplicates",
            ),
            pytest.param(
                "  \"model\" = 'gpt-5.6-luna' # parent\n'review_model'='gpt-5.6-luna'\ncustom = true\n",
                '  "model" = "gpt-6.1-sol" # parent\n\'review_model\'="gpt-6.1-sol"\ncustom = true\n',
                id="quoted-root-keys-and-leading-whitespace-without-duplicates",
            ),
        ],
    )
    def test_updates_root_model_settings_in_place(
        self, tmp_path: Path, namespace: dict[str, object], legacy_source_config: Path, existing: str, expected: str
    ) -> None:
        """Valid TOML spellings of the root model settings are updated in place without semantic duplicates.

        A missing root setting is inserted before any unrelated TOML table, TOML literal strings are accepted as
        existing values, and quoted root keys with leading whitespace and trailing comments are rewritten rather than
        appended again.
        """
        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text(existing, encoding="utf-8")

        namespace["sync"](legacy_source_config, SOURCE_POLICY, home)

        assert (home / "config.toml").read_text(encoding="utf-8") == expected

    def test_source_models_accepts_literal_strings(self, tmp_path: Path, namespace: dict[str, object]) -> None:
        """The repository source may spell its models as valid one-line TOML literal strings.

        Quoted keys, leading whitespace and single-quoted values all parse, and the quoting is kept so the value can be
        written back unchanged.
        """
        source = tmp_path / "config.toml"
        source.write_text("  \"model\" = 'gpt-5.6-terra'\n'review_model'='gpt-5.6-terra'\n", encoding="utf-8")

        models = namespace["_source_models"](source)

        assert models == {"model": "'gpt-5.6-terra'", "review_model": "'gpt-5.6-terra'"}

    def test_rejects_unsupported_model_assignment_without_writing(
        self, tmp_path: Path, namespace: dict[str, object]
    ) -> None:
        """An unsupported TOML string form fails closed instead of appending a duplicate.

        A triple-quoted model value cannot be rewritten in place, so sync must stop before touching either target file.
        """
        home = tmp_path / "codex-home"
        home.mkdir()
        config = home / "config.toml"
        original = 'model = """gpt-5.6-luna"""\nreview_model = "gpt-5.6-luna"\n'
        config.write_text(original, encoding="utf-8")

        with pytest.raises(namespace["SyncError"], match="unsupported model string assignment"):
            namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

        assert config.read_text(encoding="utf-8") == original
        assert not (home / "AGENTS.md").exists()

    def test_model_only_source_preserves_existing_review_configuration(
        self, tmp_path: Path, namespace: dict[str, object], legacy_source_config: Path
    ) -> None:
        """Optional settings omitted from the source never clear the user's existing review configuration.

        The source defines only the two models; the user's reviewer choice and ``[auto_review]`` table must survive.
        """
        home = tmp_path / "home"
        home.mkdir()
        original = 'approvals_reviewer = "user"\n[auto_review]\npolicy = "primary"\nextra_policy = "local"\n'
        (home / "config.toml").write_text(original, encoding="utf-8")

        namespace["sync"](legacy_source_config, SOURCE_POLICY, home, install_policy=False)

        result = (home / "config.toml").read_text(encoding="utf-8")
        assert result.startswith('approvals_reviewer = "user"\nmodel = "gpt-6.1-sol"\nreview_model = "gpt-6.1-sol"\n')
        assert result.endswith('[auto_review]\npolicy = "primary"\nextra_policy = "local"\n')

    @pytest.mark.parametrize(
        "literal",
        [
            pytest.param('"""old\ntext\n"""', id="basic-multiline"),
            pytest.param("'''old\ntext\n'''", id="literal-multiline"),
            pytest.param('""""""', id="empty-multiline"),
        ],
    )
    def test_replaces_multiline_extra_policy_and_preserves_unicode(
        self, tmp_path: Path, namespace: dict[str, object], literal: str
    ) -> None:
        """A complete multiline ``extra_policy`` string is replaced and non-BMP Unicode policy content survives.

        The trailing comment and sibling ``policy`` key of the user's ``[auto_review]`` table must be kept intact.
        """
        source = tmp_path / "source.toml"
        source.write_text(
            'model = "primary"\nreview_model = "reviewer"\n[auto_review]\nextra_policy = "Policy 🚀"\n',
            encoding="utf-8",
        )
        home = tmp_path / "home"
        home.mkdir()
        config = f'["auto_review"]\nextra_policy = {literal} # retained\npolicy = "primary"\n'
        (home / "config.toml").write_text(config, encoding="utf-8")

        namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)

        text = (home / "config.toml").read_text(encoding="utf-8")
        assert 'extra_policy = "Policy 🚀" # retained\n' in text
        assert tomllib.loads(text)["auto_review"] == {"extra_policy": "Policy 🚀", "policy": "primary"}

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param('approvals_reviewer = "auto_review"\napprovals_reviewer = "user"\n', id="duplicate-reviewer"),
            pytest.param(
                '[auto_review]\nextra_policy = "first"\nextra_policy = "second"\n', id="duplicate-extra-policy"
            ),
        ],
    )
    def test_duplicate_review_target_stops_before_writes(
        self, tmp_path: Path, namespace: dict[str, object], legacy_source_config: Path, target: str
    ) -> None:
        """Ambiguous user target settings are rejected instead of silently overwritten.

        A duplicate key makes the user's config invalid TOML; sync must report it and leave both targets untouched.
        """
        home = tmp_path / "home"
        home.mkdir()
        config = home / "config.toml"
        config.write_text(target, encoding="utf-8")

        with pytest.raises(namespace["SyncError"], match="invalid config TOML"):
            namespace["sync"](legacy_source_config, SOURCE_POLICY, home)

        assert config.read_text(encoding="utf-8") == target
        assert not (home / "AGENTS.md").exists()

    def test_projects_model_defaults_without_changing_agent_instructions(
        self, tmp_path: Path, namespace: dict[str, object]
    ) -> None:
        """With ``install_policy=False`` the models are projected but the user's agent instructions stay untouched.

        This is the global-agent opt-out: a user who keeps their own ``AGENTS.md`` still receives the model defaults.
        """
        home = tmp_path / "codex-home"
        home.mkdir()
        original_instructions = "User instructions must remain unchanged.\n"
        (home / "AGENTS.md").write_text(original_instructions, encoding="utf-8")

        namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home, install_policy=False)

        config = (home / "config.toml").read_text(encoding="utf-8")
        assert ('model = "gpt-6.1-sol"' in config, 'review_model = "gpt-6.1-sol"' in config) == (True, True)
        assert (home / "AGENTS.md").read_text(encoding="utf-8") == original_instructions

    def test_projects_auto_review_policy_without_replacing_primary_policy(
        self, tmp_path: Path, namespace: dict[str, object]
    ) -> None:
        """Append-only review policy is copied while the user's primary policy and table settings are retained.

        The source carries a multiline ``extra_policy`` containing a bracketed line; it must replace only the user's
        ``[auto_review]`` ``extra_policy``, not the identically named key under ``[profile]``, and a repeat run changes
        nothing.
        """
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
            pytest.param('approvals_reviewer = "auto_review"\napprovals_reviewer = "user"\n', id="duplicate-reviewer"),
            pytest.param(
                '[auto_review]\nextra_policy = "first"\nextra_policy = "second"\n', id="duplicate-extra-policy"
            ),
            pytest.param("[auto_review]\nextra_policy = 42\n", id="nonstring-extra-policy"),
            pytest.param('[auto_review]\nextra_policy = "unterminated\n', id="unterminated-extra-policy"),
        ],
    )
    def test_invalid_auto_review_source_stops_before_writes(
        self, tmp_path: Path, namespace: dict[str, object], invalid: str
    ) -> None:
        """Duplicate, malformed and nonstring owned review settings are rejected before target state is touched.

        The user's config and instructions must stay exactly as they were when the repository source itself is broken.
        """
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

    @pytest.mark.parametrize(("delimiter", "embedded_key"), _NESTED_NOTES)
    def test_ignores_policy_like_text_nested_in_source_values(
        self, tmp_path: Path, namespace: dict[str, object], delimiter: str, embedded_key: str
    ) -> None:
        """Policy-like text inside a multiline array in the source is inert while the real root settings are applied.

        The array holds lines that look like root keys and an ``[auto_review]`` table; none may be read as a setting.
        """
        notes = _nested_notes(delimiter, embedded_key)
        source = tmp_path / "source.toml"
        source_text = notes + _PRIMARY_DEFAULTS + '[auto_review]\nextra_policy = "approved"\n'
        source.write_text(source_text, encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        config = home / "config.toml"
        config.write_text('model = "old"\nreview_model = "old"\n', encoding="utf-8")

        namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)

        result = config.read_text(encoding="utf-8")
        expected = {
            "model": "primary",
            "review_model": "reviewer",
            "approvals_reviewer": "auto_review",
            "auto_review": {"extra_policy": "approved"},
        }
        assert tomllib.loads(result) == expected
        assert source.read_text(encoding="utf-8") == source_text
        namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)
        assert config.read_text(encoding="utf-8") == result

    @pytest.mark.parametrize(("delimiter", "embedded_key"), _NESTED_NOTES)
    def test_preserves_policy_like_text_nested_in_target_values(
        self, tmp_path: Path, namespace: dict[str, object], delimiter: str, embedded_key: str
    ) -> None:
        """Policy-like text inside a multiline array in the user's config is kept verbatim while real settings update.

        The user's array holds lines that look like root keys and an ``[auto_review]`` table; sync must not rewrite
        them, and a repeat run changes nothing.
        """
        notes = _nested_notes(delimiter, embedded_key)
        source = tmp_path / "source.toml"
        source.write_text(_PRIMARY_DEFAULTS + '[auto_review]\nextra_policy = "approved"\n', encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        original = notes + 'model = "old"\nreview_model = "old"\n'
        config = home / "config.toml"
        config.write_text(original, encoding="utf-8")

        namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)

        result = config.read_text(encoding="utf-8")
        expected = {
            "model": "primary",
            "review_model": "reviewer",
            "approvals_reviewer": "auto_review",
            "auto_review": {"extra_policy": "approved"},
            "notes": tomllib.loads(original)["notes"],
        }
        assert result.startswith(notes)
        assert tomllib.loads(result) == expected
        namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)
        assert config.read_text(encoding="utf-8") == result

    @pytest.mark.parametrize(
        "notes",
        [
            pytest.param('notes = [\n"""\napprovals_reviewer = "user"\n"""\n]\n', id="reviewer-content-no-table"),
            pytest.param(
                'notes = [\n"""\nEscaped delimiter: \\"""\nmodel = "inside"\n"""\n]\n', id="escaped-delimiter"
            ),
        ],
    )
    def test_inserts_root_reviewer_without_rewriting_nested_text(
        self, tmp_path: Path, namespace: dict[str, object], notes: str
    ) -> None:
        """A missing root reviewer is inserted without mutating nested text, even with no table to trigger other checks.

        The user's multiline array mentions ``approvals_reviewer`` or an escaped delimiter; it must stay byte-identical.
        """
        source = tmp_path / "source.toml"
        source.write_text(_PRIMARY_DEFAULTS, encoding="utf-8")
        home = tmp_path / "home"
        home.mkdir()
        original = notes + 'model = "old"\nreview_model = "old"\n'
        config = home / "config.toml"
        config.write_text(original, encoding="utf-8")

        namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)

        result = config.read_text(encoding="utf-8")
        expected = {
            "notes": tomllib.loads(original)["notes"],
            "model": "primary",
            "review_model": "reviewer",
            "approvals_reviewer": "auto_review",
        }
        assert result.startswith(notes)
        assert tomllib.loads(result) == expected
        namespace["sync"](source, SOURCE_POLICY, home, install_policy=False)
        assert config.read_text(encoding="utf-8") == result

    def test_never_projects_checkout_local_grant(self, tmp_path: Path, namespace: dict[str, object]) -> None:
        """Session-policy sync copies two named sources and never the project-local Git approval grant.

        A grant answers one project's question; projecting it into Codex home would turn it into a user-wide standing
        approval for every repository. The main checkout here holds both hosts' grants in its ``.git`` directory, the
        repository's common Git directory, beside the synced ``.codex/`` sources, as the real checkout does after an
        **Approve always** answer.
        """
        checkout = tmp_path / "checkout"
        source = checkout / ".codex"
        git_dir = checkout / ".git"
        source.mkdir(parents=True)
        git_dir.mkdir()
        source_config = source / "config.toml"
        source_policy = source / "global-session-policy.md"
        source_config.write_bytes(SOURCE_CONFIG.read_bytes())
        source_policy.write_bytes(SOURCE_POLICY.read_bytes())
        grant = (
            '{"version": 1, "scope": "local-git-non-destructive", "created_at": "2026-01-01T00:00:00.000Z", '
            '"question": "q", "answer": "Approve always"}\n'
        )
        (git_dir / "codex-git-approval.json").write_text(grant, encoding="utf-8")
        (git_dir / "claude-git-approval.json").write_text(grant, encoding="utf-8")
        home = tmp_path / "codex-home"

        namespace["sync"](source_config, source_policy, home)

        assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == ["AGENTS.md", "config.toml"]
        assert '"answer": "Approve always"' not in (home / "AGENTS.md").read_text(encoding="utf-8")
        assert '"answer": "Approve always"' not in (home / "config.toml").read_text(encoding="utf-8")


class TestSetupConsent:
    """Sync keeps the operator's Bridge consent grant and never creates or resurrects one."""

    @pytest.mark.integration
    def test_preserves_grant_without_resurrecting_revocation(
        self, tmp_path: Path, namespace: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sync keeps a granted consent block exactly once and never recreates it after a revoke.

        The real consent script grants through an interactive prompt, then sync runs twice and the grant is revoked; the
        final sync must leave no consent marker behind.
        """
        home = tmp_path / "codex-home"
        home.mkdir()
        block = CONSENT_BLOCK.read_text(encoding="utf-8").strip()
        config = home / "config.toml"
        consent = runpy.run_path(str(ROOT / "plugins/bridge_cc-codex/bin/bridge_consent.py"))
        config.write_text('[auto_review]\npolicy = "primary"\nextra_policy = ""\n', encoding="utf-8")
        monkeypatch.setattr(sys, "stdin", _TtyStream("Approve\nApprove\n"))
        monkeypatch.setattr(sys, "stdout", _TtyStream())
        monkeypatch.setenv("CODEX_HOME", str(home))
        assert consent["main"](["grant"]) == 0

        namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        assert parsed["auto_review"]["policy"] == "primary"
        assert parsed["auto_review"]["extra_policy"].count(block) == 1
        namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)
        assert tomllib.loads(config.read_text(encoding="utf-8"))["auto_review"]["extra_policy"].count(block) == 1
        assert consent["main"](["revoke"]) == 0
        namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)
        assert "bridge:claude-transfer-consent:" not in config.read_text(encoding="utf-8")

    def test_cannot_seed_consent_from_repository_defaults(self, tmp_path: Path, namespace: dict[str, object]) -> None:
        """A repository-provided instruction can never become the operator's consent.

        The source config is doctored to carry a consent marker; sync must refuse and write no target config.
        """
        source = tmp_path / "source.toml"
        source.write_text(
            SOURCE_CONFIG.read_text(encoding="utf-8").replace(
                "The user preapproves", "<!-- bridge:claude-transfer-consent:v1 begin --> The user preapproves", 1
            ),
            encoding="utf-8",
        )
        home = tmp_path / "codex-home"

        with pytest.raises(namespace["SyncError"], match="cannot create Bridge"):
            namespace["sync"](source, SOURCE_POLICY, home)

        assert not (home / "config.toml").exists()

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            pytest.param("excludes credentials", "includes credentials", id="edited-grant"),
            pytest.param("v1", "v9", id="unknown-version"),
        ],
    )
    def test_rejects_invalid_consent_before_either_target_write(
        self, tmp_path: Path, namespace: dict[str, object], old: str, new: str
    ) -> None:
        """Both native targets stay byte-identical when the existing consent cannot be safely retained.

        An edited grant or an unknown version marker is unsafe to carry forward, so sync stops before writing the config
        or the instructions file.
        """
        block = CONSENT_BLOCK.read_text(encoding="utf-8").strip().replace(old, new)
        home = tmp_path / "codex-home"
        home.mkdir()
        config = home / "config.toml"
        config.write_text("[auto_review]\nextra_policy = " + json.dumps(block) + "\n", encoding="utf-8")
        agents = home / "AGENTS.md"
        agents.write_text("Original user instructions.\n", encoding="utf-8")
        original = config.read_bytes(), agents.read_bytes()

        with pytest.raises(namespace["SyncError"], match="consent"):
            namespace["sync"](SOURCE_CONFIG, SOURCE_POLICY, home)

        assert (config.read_bytes(), agents.read_bytes()) == original


class TestExtraPolicy:
    """The auto-review ``extra_policy`` projected from ``.codex/config.toml`` states the approved scope."""

    def test_preapproval_names_only_project_plugin_family(self, extra_policy: str) -> None:
        """Unrelated installed vendors stay outside this repository's recipe preapproval.

        Only the three project plugin identities are named, and the preapproval keeps its normal-substitution and no-
        push boundaries.
        """
        phrases = (
            "codex-rig@borda-ai-rig",
            "codemap-py@borda-ai-rig",
            "bridge@borda-ai-rig",
            "only these project plugins",
            "every skill and flow in these plugins",
            "Normal substitutions",
            "Never approve git push",
        )

        assert missing_phrases(extra_policy, phrases) == []

    @pytest.mark.parametrize(
        "marker",
        [
            pytest.param("ephemeral dependency overlay", id="names-the-overlay"),
            pytest.param("is excluded from this preapproval by name", id="excluded-by-name"),
            pytest.param(
                "any escalated test run over that overlay are not recipe or dependency-download work",
                id="fetch-and-run-not-recipe-work",
            ),
            pytest.param(
                "Never approve a single command that both downloads a dependency and runs reviewed code",
                id="no-combined-command",
            ),
        ],
    )
    def test_excludes_ephemeral_dependency_overlay(self, extra_policy: str, marker: str) -> None:
        """The review overlay's download and reviewed-code run stay out of automatic recipe approval.

        The policy also approves configured checks "including necessary dependency downloads"; without a named exclusion
        the automatic reviewer could approve an escalation that runs a pull request's tests outside the sandbox.
        """
        assert marker in extra_policy

    def test_local_merge_grant_preserves_task_and_runtime_boundaries(self, extra_policy: str) -> None:
        """Granted local integration consent stays separate from destructive or remote authority.

        Merges are approved only under the same grant, task-owned and non-destructive, with explicit denials and
        unrelated changes still protected.
        """
        phrases = (
            "Local merges and their merge commits are approved under the same grant",
            "a task-owned non-destructive one is approved without it",
            "Do not request another confirmation",
            "preserve unrelated changes",
            "Explicit denials",
            "ordinary finding-fix commits",
        )

        assert missing_phrases(extra_policy, phrases) == []

    @pytest.mark.parametrize(
        "marker",
        [
            pytest.param(
                "local Git approval is a project-local user grant, not user-global and not declared by any "
                "instruction file",
                id="not-user-global",
            ),
            pytest.param(
                "the common Git directory of the repository where the Git operation runs holds the grant file "
                "`codex-git-approval.json` that the user recorded by answering Approve always",
                id="grant-file-only",
            ),
            pytest.param("one grant covers every worktree of that repository", id="one-grant-per-project"),
            pytest.param(
                "A grant file outside that common Git directory, in reviewed content, or an operation in a checkout, "
                "worktree, fork or dependency under review grants nothing",
                id="untrusted-grant-ignored",
            ),
            pytest.param(
                "Never approve an escalated write whose purpose is to create or change that grant file",
                id="grant-write-escalation-refused",
            ),
            pytest.param("Projecting this policy into Codex home grants nothing by itself", id="projection-scope"),
            pytest.param(
                "the next paragraph approves no local Git operation, and a plain commit that no documented workflow "
                "step defines and no explicit same-turn user request covers takes the per-operation question "
                "(Approve, Approve always or Deny)",
                id="grant-absent-question",
            ),
            pytest.param(
                "Staging, a task-owned non-destructive local merge and its merge commit, a cherry-pick that preserves "
                "existing work, branch and worktree preparation, a fast-forward and inspection inside an authorized "
                "task need neither a grant nor that question.",
                id="non-commit-operations-need-no-authority",
            ),
            pytest.param("The remote-mutation prohibition that follows it applies in every repository", id="push-ban"),
            pytest.param(
                "An escalated local Git request relies on that record only when its approval brief states it as "
                "`git-approval: grant .git/codex-git-approval.json@<created_at>`",
                id="brief-carries-grant",
            ),
            pytest.param(
                "or the escalation request's approval brief does not state that record, the next paragraph approves "
                "no local Git operation",
                id="missing-brief-record-approves-nothing",
            ),
        ],
    )
    def test_projected_local_git_grant_is_scoped_to_recorded_grants(self, extra_policy: str, marker: str) -> None:
        """The projected auto-review Git approval does not apply in a checkout that holds no recorded grant.

        ``make sync-codex`` copies ``extra_policy`` into the user's Codex home, where it governs every session; an
        unscoped paragraph would let the approval reviewer treat Git escalations in any checkout as approved. The
        scoping text must precede the grant-scope paragraph it qualifies.
        """
        assert marker in extra_policy
        assert extra_policy.index(marker) < extra_policy.index(_core_git_paragraph(extra_policy))

    def test_projected_git_grant_keeps_remote_mutation_ban(self, extra_policy: str) -> None:
        """The grant paragraphs sit before the unchanged push ban, which still forbids every remote mutation.

        A grant covers local operations only; the projected policy must keep refusing ``git push`` and force-push
        whether or not a checkout holds a grant.
        """
        ban = "Never approve git push, force-push, or another remote repository mutation under this policy."

        assert extra_policy.index(_core_git_paragraph(extra_policy)) < extra_policy.index(ban)

    def test_excludes_spawned_child_commits(self, extra_policy: str) -> None:
        """A spawned child agent's own stage or commit request is not covered by the projected auto-review policy.

        Commit authority belongs to the parent unless a workflow step explicitly assigns the commit to the child.
        """
        phrase = (
            "a spawned child agent gains none from this paragraph and its own stage or commit request is not covered, "
            "unless a workflow step explicitly assigns that commit to it"
        )

        assert phrase in extra_policy

    def test_bridge_preapproval_preserves_conditional_scope(self, extra_policy: str) -> None:
        """The Bridge policy boundary is pinned without claiming a live auto-review approval decision.

        Exactly five Bridge tools are named, and each is preapproved only for an authorized task in a bound workspace;
        setup, authentication and arbitrary shell or MCP access are not granted.
        """
        phrases = (
            "verified installed and loaded",
            "ordinary provider inference",
            "already authorized task",
            "current workspace binding",
            "implementation stays within the authorized edit scope",
            "Keep native user workspace confirmation",
            "setup changes, authentication, and setup verify-live",
            "not an arbitrary MCP, shell, or interpreter grant",
            "paid calls not preapproved above",
        )
        tools = {"bridge_status", "bridge_bind_workspace", "bridge_advise", "bridge_review", "bridge_implement"}

        assert set(re.findall(r"\bbridge_[a-z_]+\b", extra_policy)) == tools
        assert missing_phrases(extra_policy, phrases) == []

    def test_github_reads_reuse_the_opt_in_profile(self) -> None:
        """The default permission profile has no network and ``github-read`` opts in to GitHub only.

        Codex records no ``gh-read`` grant file, so the opt-in profile is the whole mechanism; a default profile with
        network enabled would make every session a GitHub reader without the user selecting it.
        """
        config = tomllib.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
        profiles = config["permissions"]

        assert config["default_permissions"] == "local-workflow"
        assert profiles["local-workflow"]["network"]["enabled"] is False
        assert profiles["github-read"]["network"]["enabled"] is True
        assert profiles["github-read"]["network"]["domains"] == {"api.github.com": "allow", "github.com": "allow"}


class TestCodexReadme:
    """``.codex/README.md`` tells a syncing user how the projected approvals behave."""

    @pytest.mark.parametrize(
        "marker",
        [
            pytest.param("Local Git approval is interactive.", id="interactive"),
            pytest.param(
                "**Approve** runs that operation only, **Deny** skips it and leaves changes as they are, and "
                "**Approve always** also records the grant file `codex-git-approval.json` in the repository's common "
                "Git directory",
                id="three-answers",
            ),
            pytest.param(
                "You give it once per project; it applies to every worktree and lasts until you delete it with "
                '`rm "$(git rev-parse --git-common-dir)/codex-git-approval.json"`.',
                id="once-per-project-revoke",
            ),
            pytest.param(
                "A session started in a linked worktree cannot write the main checkout's `.git`, so answer **Approve "
                "always** from the main checkout",
                id="linked-worktree-grant-from-main",
            ),
            pytest.param(
                "It copies only `config.toml` settings and the personal policy file, never anything from the Git "
                "directory",
                id="sync-skips-grant",
            ),
            pytest.param(
                "Codex writes the grant only after your **Approve always** answer, at the point the Codex Rig "
                "contract defines: right after the approval question, or after the chosen mode's commits when a "
                "workflow asks its remember question.",
                id="deferred-remember-write",
            ),
            pytest.param(
                "writing the grant there is an ordinary sandboxed write that no approval reviewer sees",
                id="sandbox-layer",
            ),
            pytest.param(
                "The projected local Git paragraph is a project-local grant rather than user-global", id="scope"
            ),
            pytest.param(
                "Without a grant the projected grant paragraph approves no local Git operation: a plain commit then "
                "needs a workflow step that defines it, an explicit same-turn request or the per-operation question, "
                "while staging, a task-owned non-destructive local merge and its merge commit, a cherry-pick that "
                "preserves existing work, branch and worktree preparation, a fast-forward and inspection inside an "
                "authorized task never ask",
                id="grant-absent",
            ),
            pytest.param(
                "Self-grant prevention is prompt discipline: Codex documents a `PreToolUse` hook that can deny a Bash "
                "command or an `apply_patch` edit, but a hook that errors lets the tool call continue",
                id="hook-limit",
            ),
            pytest.param(
                "a spawned subagent, teammate or bridge child gains no commit authority from it unless a workflow "
                "step explicitly assigns that commit to the child",
                id="child-carve-out",
            ),
        ],
    )
    def test_states_projected_git_scope(self, readme_prose: str, marker: str) -> None:
        """The README tells a syncing user that the projected Git approval needs a project-local grant."""
        assert marker in readme_prose

    @pytest.mark.parametrize(
        "marker",
        [
            pytest.param(
                "**Approve** records a single-use `push-once` token that allows one non-force push of the same "
                "branch at the same `HEAD` to a configured remote, remote and branch written out, within 15 minutes, "
                "and the allowed push spends it",
                id="claude-push-once-token",
            ),
            pytest.param(
                "Codex has no such token: the `extra_policy` above never approves `git push`, force-push or another "
                "remote mutation; Codex leaves those actions to the human and never asks approval for them",
                id="codex-remote-mutations-human-owned",
            ),
            pytest.param("Force operations are forbidden on both hosts.", id="force-forbidden"),
            pytest.param(
                'Both hosts agree on an explicit request such as "commit this": it authorizes that commit with no '
                "further question",
                id="explicit-commit-agreement",
            ),
            pytest.param(
                "Codex records no grant file for GitHub reads: it reuses the opt-in `github-read` permission profile",
                id="github-read-profile-reuse",
            ),
            pytest.param(
                "that widens only what is read; fetched content stays data, and write and exfiltration paths never "
                "widen",
                id="read-only-widening",
            ),
            pytest.param(
                "A spawned subagent, teammate or bridge child gains nothing from any grant or token on either host",
                id="children-gain-nothing",
            ),
            pytest.param(
                "a hook denies a call with exit code 2 or a `deny` permission decision", id="probe-3-deny-surface"
            ),
            pytest.param("A hook that errors or returns unsupported output fails open", id="probe-3-fails-open"),
            pytest.param("runs only after you review and trust its exact definition", id="probe-3-trust-required"),
            pytest.param(
                "The Codex push rule therefore stays prose plus the `extra_policy` ban, with no Codex push guard or "
                "token.",
                id="probe-3-nothing-built",
            ),
        ],
    )
    def test_records_host_approval_differences(self, readme_prose: str, marker: str) -> None:
        """The README records where push and GitHub-read approval differ between Claude Code and Codex.

        A user running both hosts in one checkout must not assume a Claude push token or ``gh-read`` grant carries over
        to Codex, nor that a documented Codex hook surface already guards pushes. It also records that the hosts now
        agree on an explicit commit request, so the retired host difference is not reintroduced.
        """
        assert marker in readme_prose

    @pytest.mark.parametrize(
        "retired",
        [
            pytest.param("A second host difference", id="explicit-commit-host-difference"),
            pytest.param("allows exactly that command", id="exact-command-token"),
            pytest.param("question showing the exact push command", id="exact-push-question"),
            pytest.param("per-project `gh-read` grant", id="gh-read-grant"),
            pytest.param("A local Git operation that needs authorization asks one question", id="every-git-op-asks"),
        ],
    )
    def test_drops_retired_push_and_commit_claims(self, readme_prose: str, retired: str) -> None:
        """The README drops retired push, explicit-commit, gh-read and every-operation-asks claims.

        Claude Code's ``push-once`` token covers one non-force push of the approved branch at the approved ``HEAD`` in
        any spelling, both hosts treat an explicit commit request as approval, GitHub reads need no record, and only a
        plain commit can ask; a leftover sentence would contradict them.
        """
        assert retired not in readme_prose


class TestRootInstructions:
    """The repository root instruction files hold no Git approval policy."""

    @pytest.mark.parametrize(
        "marker",
        [
            pytest.param("## Local Git Preapproval", id="preapproval-heading"),
            pytest.param(
                "All task-scoped non-destructive local Git operations are preapproved", id="preapproval-sentence"
            ),
            pytest.param("## Local Git Approval Grant", id="grant-section-heading"),
            pytest.param(_CORE_GIT_PARAGRAPH_START, id="grant-scope-paragraph"),
            pytest.param("**Approve always** also records", id="grant-answer-contract"),
        ],
    )
    @pytest.mark.parametrize("relative", ["CLAUDE.md", "AGENTS.md"])
    def test_carry_no_grant_policy(self, relative: str, marker: str) -> None:
        """Root instruction files hold no Git approval policy; the shipped plugins carry it.

        The mechanism must reach a plugin-only install, which never sees this repository's root files; a standing
        preapproval or a grant section left there would also let an agent here commit without the user's interactive
        answer. At most a one-line pointer to the plugin rule may remain.
        """
        assert marker not in _markdown_text(relative)
