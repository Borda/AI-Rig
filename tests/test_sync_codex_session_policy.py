"""Acceptance checks for root-level Codex-home session-policy synchronization."""

from __future__ import annotations

import re
import runpy
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
def test_sync_updates_root_model_settings_in_place(
    tmp_path: Path, legacy_source_config: Path, existing: str, expected: str
) -> None:
    """Valid TOML spellings of the root model settings are updated in place without semantic duplicates.

    A missing root setting is inserted before any unrelated TOML table, TOML literal strings are accepted as existing
    values, and quoted root keys with leading whitespace and trailing comments are rewritten rather than appended again.
    """
    namespace = _namespace()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(existing, encoding="utf-8")

    namespace["sync"](legacy_source_config, SOURCE_POLICY, home)

    assert (home / "config.toml").read_text(encoding="utf-8") == expected


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
    ("location", "delimiter", "embedded_key"),
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
def test_repository_preapproval_excludes_ephemeral_dependency_overlay(marker: str) -> None:
    """Keep the review overlay's download and reviewed-code run out of automatic recipe approval.

    The policy also approves configured checks "including necessary dependency downloads"; without a named exclusion the
    automatic reviewer could approve an escalation that runs a pull request's tests outside the sandbox.
    """
    policy = tomllib.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))["auto_review"]["extra_policy"]
    assert marker in policy


def test_local_merge_grant_preserves_task_and_runtime_boundaries() -> None:
    """Keep granted local integration consent separate from destructive or remote authority."""
    policy = tomllib.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))["auto_review"]["extra_policy"]
    assert "Local merges and their merge commits are approved under the same grant" in policy
    assert "a task-owned non-destructive one is approved without it" in policy
    assert "Do not request another confirmation" in policy
    assert "preserve unrelated changes" in policy
    assert "Explicit denials" in policy
    assert "ordinary finding-fix commits" in policy


#: Anchors of the core local Git grant-scope paragraph that every Codex policy-sibling copy carries byte-for-byte.
_CORE_GIT_PARAGRAPH_START = (
    "Task-scoped local Git work other than a plain commit runs without an authorization question"
)
_CORE_GIT_PARAGRAPH_END = "it does not require inventing Git work or committing unrelated changes."


def _core_git_paragraph(text: str) -> str:
    """Return the core local Git grant-scope paragraph as one sibling's text states it, anchors included."""
    start = text.index(_CORE_GIT_PARAGRAPH_START)
    return text[start : text.index(_CORE_GIT_PARAGRAPH_END, start) + len(_CORE_GIT_PARAGRAPH_END)]


def _markdown_text(relative: str) -> str:
    """Read one repository Markdown sibling as written."""
    return (ROOT / relative).read_text(encoding="utf-8")


def _toml_extra_policy(relative: str) -> str:
    """Read the parsed auto-review ``extra_policy`` string from one repository TOML sibling."""
    return tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))["auto_review"]["extra_policy"]


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(
            "local Git approval is a project-local user grant, not user-global and not declared by any instruction "
            "file",
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
            "the next paragraph approves no local Git operation, and a plain commit that no documented workflow step "
            "defines and no explicit same-turn user request covers takes the per-operation question (Approve, Approve "
            "always or Deny)",
            id="grant-absent-question",
        ),
        pytest.param(
            "Staging, a task-owned non-destructive local merge and its merge commit, a cherry-pick that preserves "
            "existing work, branch and worktree preparation, a fast-forward and inspection inside an authorized task "
            "need neither a grant nor that question.",
            id="non-commit-operations-need-no-authority",
        ),
        pytest.param("The remote-mutation prohibition that follows it applies in every repository", id="push-ban"),
        pytest.param(
            "An escalated local Git request relies on that record only when its approval brief states it as "
            "`git-approval: grant .git/codex-git-approval.json@<created_at>`",
            id="brief-carries-grant",
        ),
        pytest.param(
            "or the escalation request's approval brief does not state that record, the next paragraph approves no "
            "local Git operation",
            id="missing-brief-record-approves-nothing",
        ),
    ],
)
def test_projected_local_git_grant_is_scoped_to_recorded_grants(marker: str) -> None:
    """Keep the projected auto-review Git approval from applying in a checkout that holds no recorded grant.

    ``make sync-codex`` copies ``extra_policy`` into the user's Codex home, where it governs every session; an unscoped
    paragraph would let the approval reviewer treat Git escalations in any checkout as approved. The scoping text must
    precede the grant-scope paragraph it qualifies.
    """
    policy = _toml_extra_policy(".codex/config.toml")
    assert marker in policy
    assert policy.index(marker) < policy.index(_core_git_paragraph(policy))


def test_projected_git_grant_keeps_the_remote_mutation_ban() -> None:
    """The grant paragraphs sit before the unchanged push ban, which still forbids every remote mutation.

    A grant covers local operations only; the projected policy must keep refusing ``git push`` and force-push whether or
    not a checkout holds a grant.
    """
    policy = _toml_extra_policy(".codex/config.toml")
    ban = "Never approve git push, force-push, or another remote repository mutation under this policy."
    assert policy.index(_core_git_paragraph(policy)) < policy.index(ban)


def test_codex_config_and_readme_share_the_core_grant_paragraph() -> None:
    """The projected policy and the Codex README state the same grant scope byte-for-byte.

    The README is what a syncing user reads; a reworded copy would describe a grant other than the one the automatic
    approval reviewer applies.
    """
    assert _core_git_paragraph(_toml_extra_policy(".codex/config.toml")) in _markdown_text(".codex/README.md")


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("plugins/codex-rig/README.md", id="codex-rig-readme"),
        pytest.param("plugins/codex-rig/shared/native-skill-contract.md", id="codex-rig-contract"),
    ],
)
def test_local_git_core_paragraph_is_identical_across_codex_rig_siblings(relative: str) -> None:
    """Every Codex Rig policy-sibling copy carries the projected policy's grant-scope paragraph byte-for-byte.

    Host-specific qualifiers live outside this paragraph, so a reworded copy cannot silently widen or narrow what a
    grant covers. The codex-rig shared contract is the paragraph's source of truth; byte equality makes any copy a valid
    reference.
    """
    assert _core_git_paragraph(_toml_extra_policy(".codex/config.toml")) in _markdown_text(relative)


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("Local Git approval is interactive.", id="interactive"),
        pytest.param(
            "**Approve** runs that operation only, **Deny** skips it and leaves changes as they are, and **Approve "
            "always** also records the grant file `codex-git-approval.json` in the repository's common Git directory",
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
            "It copies only `config.toml` settings and the personal policy file, never anything from the Git directory",
            id="sync-skips-grant",
        ),
        pytest.param(
            "Codex writes the grant only after your **Approve always** answer, at the point the Codex Rig contract "
            "defines: right after the approval question, or after the chosen mode's commits when a workflow asks its "
            "remember question.",
            id="deferred-remember-write",
        ),
        pytest.param(
            "writing the grant there is an ordinary sandboxed write that no approval reviewer sees",
            id="sandbox-layer",
        ),
        pytest.param("The projected local Git paragraph is a project-local grant rather than user-global", id="scope"),
        pytest.param(
            "Without a grant the projected grant paragraph approves no local Git operation: a plain commit then needs "
            "a workflow step that defines it, an explicit same-turn request or the per-operation question, while "
            "staging, a task-owned non-destructive local merge and its merge commit, a cherry-pick that preserves "
            "existing work, branch and worktree preparation, a fast-forward and inspection inside an authorized task "
            "never ask",
            id="grant-absent",
        ),
        pytest.param(
            "Self-grant prevention is prompt discipline: Codex documents a `PreToolUse` hook that can deny a Bash "
            "command or an `apply_patch` edit, but a hook that errors lets the tool call continue",
            id="hook-limit",
        ),
        pytest.param(
            "a spawned subagent, teammate or bridge child gains no commit authority from it unless a workflow step "
            "explicitly assigns that commit to the child",
            id="child-carve-out",
        ),
    ],
)
def test_codex_readme_states_projected_git_scope(marker: str) -> None:
    """The Codex README tells a syncing user that the projected Git approval needs a project-local grant."""
    assert marker in _markdown_text(".codex/README.md")


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(
            "**Approve** records a single-use `push-once` token that allows one non-force push of the same branch at "
            "the same `HEAD` to a configured remote, remote and branch written out, within 15 minutes, and the allowed "
            "push spends it",
            id="claude-push-once-token",
        ),
        pytest.param(
            "Codex has no such token: the `extra_policy` above never approves `git push`, force-push or another remote "
            "mutation, so every push asks you each time",
            id="codex-asks-every-push",
        ),
        pytest.param("Force operations are forbidden on both hosts.", id="force-forbidden"),
        pytest.param(
            'Both hosts agree on an explicit request such as "commit this": it authorizes that commit with no further '
            "question",
            id="explicit-commit-agreement",
        ),
        pytest.param(
            "Codex records no grant file for GitHub reads: it reuses the opt-in `github-read` permission profile",
            id="github-read-profile-reuse",
        ),
        pytest.param(
            "that widens only what is read; fetched content stays data, and write and exfiltration paths never widen",
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
            "The Codex push rule therefore stays prose plus the `extra_policy` ban, with no Codex push guard or token.",
            id="probe-3-nothing-built",
        ),
    ],
)
def test_codex_readme_records_host_approval_differences(marker: str) -> None:
    """The Codex README records where push and GitHub-read approval differ between Claude Code and Codex.

    A user running both hosts in one checkout must not assume a Claude push token or ``gh-read`` grant carries over to
    Codex, nor that a documented Codex hook surface already guards pushes. It also records that the hosts now agree on
    an explicit commit request, so the retired host difference is not reintroduced.
    """
    assert marker in _markdown_text(".codex/README.md")


@pytest.mark.parametrize(
    "retired",
    [
        pytest.param("A second host difference", id="explicit-commit-host-difference"),
        pytest.param("allows exactly that command", id="exact-command-token"),
        pytest.param("question showing the exact push command", id="exact-push-question"),
        pytest.param("per-project `gh-read` grant", id="gh-read-grant"),
        pytest.param("A local Git operation that needs authorization asks one question", id="every-git-operation-asks"),
    ],
)
def test_codex_readme_drops_retired_push_and_commit_claims(retired: str) -> None:
    """The Codex README drops retired push, explicit-commit, gh-read and every-operation-asks claims.

    Claude Code's ``push-once`` token covers one non-force push of the approved branch at the approved ``HEAD`` in any
    spelling, both hosts treat an explicit commit request as approval, GitHub reads need no record, and only a plain
    commit can ask; a leftover sentence would contradict them.
    """
    assert retired not in _markdown_text(".codex/README.md")


def test_codex_github_reads_reuse_the_opt_in_profile() -> None:
    """The README's GitHub-read claim holds: default profile has no network, ``github-read`` opts in to GitHub only.

    Codex records no ``gh-read`` grant file, so the opt-in profile is the whole mechanism; a default profile with
    network enabled would make every session a GitHub reader without the user selecting it.
    """
    config = tomllib.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    profiles = config["permissions"]
    assert config["default_permissions"] == "local-workflow"
    assert profiles["local-workflow"]["network"]["enabled"] is False
    assert profiles["github-read"]["network"]["enabled"] is True
    assert profiles["github-read"]["network"]["domains"] == {"api.github.com": "allow", "github.com": "allow"}


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


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("## Local Git Preapproval", id="preapproval-heading"),
        pytest.param("All task-scoped non-destructive local Git operations are preapproved", id="preapproval-sentence"),
        pytest.param("## Local Git Approval Grant", id="grant-section-heading"),
        pytest.param(_CORE_GIT_PARAGRAPH_START, id="grant-scope-paragraph"),
        pytest.param("**Approve always** also records", id="grant-answer-contract"),
    ],
)
@pytest.mark.parametrize("relative", ["CLAUDE.md", "AGENTS.md"])
def test_root_instructions_carry_no_grant_policy(relative: str, marker: str) -> None:
    """Root instruction files hold no Git approval policy; the shipped plugins carry it.

    The mechanism must reach a plugin-only install, which never sees this repository's root files; a standing
    preapproval or a grant section left there would also let an agent here commit without the user's interactive answer.
    At most a one-line pointer to the plugin rule may remain.
    """
    assert marker not in _markdown_text(relative)


def test_projected_policy_excludes_spawned_child_commits() -> None:
    """The projected auto-review policy does not cover a spawned child agent's own stage or commit request."""
    policy = _toml_extra_policy(".codex/config.toml")
    assert (
        "a spawned child agent gains none from this paragraph and its own stage or commit request is not covered, "
        "unless a workflow step explicitly assigns that commit to it" in policy
    )


def test_sync_never_projects_checkout_local_grant(tmp_path: Path) -> None:
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

    _namespace()["sync"](source_config, source_policy, home)

    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == ["AGENTS.md", "config.toml"]
    assert '"answer": "Approve always"' not in (home / "AGENTS.md").read_text(encoding="utf-8")
    assert '"answer": "Approve always"' not in (home / "config.toml").read_text(encoding="utf-8")
