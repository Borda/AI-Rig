"""Exercise setup consent against disposable native Codex configuration."""

from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "bin" / "bridge_consent.py"


class Terminal(io.StringIO):
    """Represent the external terminal boundary while keeping file operations real."""

    def isatty(self) -> bool:
        """Expose the operator-terminal capability to the helper."""
        return True


@pytest.fixture
def consent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every consent operation inside a disposable Codex home."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    (home / "config.toml").write_text(
        'model = "retained"\napprovals_reviewer = "auto_review"\n[auto_review]\n'
        'policy = "User primary policy"\nextra_policy = "Existing user rule"\n',
        encoding="utf-8",
    )
    return home


def test_consent_lifecycle_preserves_user_configuration(consent_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant once, reuse without prompting, then remove only the approved policy block."""
    namespace = runpy.run_path(str(SCRIPT))
    main = namespace["main"]
    config = consent_home / "config.toml"
    original = tomllib.loads(config.read_text(encoding="utf-8"))
    output = Terminal()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", Terminal("Approve\n"))
    assert main(["grant"]) == 0
    policy = tomllib.loads(config.read_text(encoding="utf-8"))
    assert policy["model"] == original["model"]
    assert policy["auto_review"]["policy"] == original["auto_review"]["policy"]
    assert policy["auto_review"]["extra_policy"].startswith("Existing user rule")
    assert "private repository" in policy["auto_review"]["extra_policy"]
    assert "Claude" in output.getvalue()
    assert "not an enforced file allowlist" in output.getvalue()
    granted = config.read_bytes()
    monkeypatch.setattr(sys, "stdin", Terminal(""))
    assert main(["grant"]) == 0
    assert config.read_bytes() == granted
    monkeypatch.setattr(sys, "stdin", Terminal("Approve\n"))
    assert main(["revoke"]) == 0
    assert tomllib.loads(config.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("answer", ["Deny\n", "\n", "approve\n"])
def test_decline_never_writes(consent_home: Path, monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    """Require the exact affirmative response after displaying the consent scope."""
    main = runpy.run_path(str(SCRIPT))["main"]
    original = (consent_home / "config.toml").read_bytes()
    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.setattr(sys, "stdin", Terminal(answer))
    assert main(["grant"]) == 2
    assert (consent_home / "config.toml").read_bytes() == original


@pytest.mark.integration
def test_check_and_noninteractive_grant_never_write(consent_home: Path) -> None:
    """Prove the real command line cannot turn a pipe into setup consent."""
    original = (consent_home / "config.toml").read_bytes()
    result = subprocess.run([sys.executable, str(SCRIPT), "check"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["consent"] == "absent"
    assert report["session_effective"] == "unverified"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "grant"], input="Approve\n", capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "operator terminal" in result.stderr
    assert (consent_home / "config.toml").read_bytes() == original


TOKEN_ENV = "BRIDGE_OPERATOR_TOKEN"  # noqa: S105 - variable name, not a secret


def _mint_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Mint a one-time token the way an operator does, from a terminal, and return what it printed."""
    output = Terminal()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", output)
        patch.setattr(sys, "stdin", Terminal())
        assert runpy.run_path(str(SCRIPT))["main"](["issue-token"]) == 0
    return output.getvalue().splitlines()[1]


def _piped_grant(token: str | None) -> subprocess.CompletedProcess[str]:
    """Run ``grant --approve`` with closed stdin, as a model-run shell would, optionally carrying a token."""
    env = {key: value for key, value in os.environ.items() if key != TOKEN_ENV}
    if token is not None:
        env[TOKEN_ENV] = token
    return subprocess.run(
        [sys.executable, str(SCRIPT), "grant", "--approve"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.mark.integration
def test_operator_token_grants_without_terminal_and_reuses_consent(
    consent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Let unattended setup grant once with an operator-minted token and prove the next dispatch check recognizes it."""
    config = consent_home / "config.toml"
    original = tomllib.loads(config.read_text(encoding="utf-8"))
    block = (PLUGIN / "rules/claude-transfer-policy.txt").read_text(encoding="utf-8").strip()
    result = _piped_grant(_mint_token(monkeypatch))
    assert result.returncode == 0, result.stderr
    assert "Explicit approval supplied by --approve with a one-time operator token" in result.stdout
    assert block in result.stdout
    assert "Type Approve" not in result.stdout
    expected = {
        **original,
        "auto_review": {**original["auto_review"], "extra_policy": "Existing user rule\n\n" + block},
    }
    assert tomllib.loads(config.read_text(encoding="utf-8")) == expected
    granted = config.read_bytes()
    for arguments in (["grant", "--approve"], ["grant"]):
        repeated = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        assert repeated.returncode == 0, repeated.stderr
        assert config.read_bytes() == granted
    checked = subprocess.run([sys.executable, str(SCRIPT), "check"], capture_output=True, text=True, check=False)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout) == {
        "consent": "configured",
        "configured_reviewer": "auto_review",
        "session_effective": "unverified",
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    ("supplied", "refusal"),
    [
        pytest.param(None, "operator terminal", id="no-token"),
        pytest.param("not-the-minted-token", "does not match", id="wrong-token"),
    ],
)
def test_piped_grant_approve_without_valid_operator_proof_never_writes(
    consent_home: Path, monkeypatch: pytest.MonkeyPatch, supplied: str | None, refusal: str
) -> None:
    """A model-run shell has no terminal and cannot mint a token, so --approve alone must not record consent.

    An outstanding token exists in the wrong-token case, proving a guess does not consume or satisfy it.
    """
    _mint_token(monkeypatch)
    original = (consent_home / "config.toml").read_bytes()
    result = _piped_grant(supplied)
    assert result.returncode == 2
    assert refusal in result.stderr
    assert (consent_home / "config.toml").read_bytes() == original


@pytest.mark.integration
def test_operator_token_is_single_use(consent_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token that already authorized one grant cannot authorize a second after revocation."""
    token = _mint_token(monkeypatch)
    assert _piped_grant(token).returncode == 0
    assert not (consent_home / ".bridge-consent-operator-token").exists()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", Terminal())
        patch.setattr(sys, "stdin", Terminal("Approve\n"))
        assert runpy.run_path(str(SCRIPT))["main"](["revoke"]) == 0
    revoked = (consent_home / "config.toml").read_bytes()
    replay = _piped_grant(token)
    assert replay.returncode == 2
    assert "token" in replay.stderr
    assert (consent_home / "config.toml").read_bytes() == revoked


@pytest.mark.integration
def test_expired_operator_token_is_refused(consent_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token older than its lifetime is refused even when it is the minted value."""
    token = _mint_token(monkeypatch)
    record = consent_home / ".bridge-consent-operator-token"
    stored = json.loads(record.read_text(encoding="utf-8"))
    record.write_text(json.dumps({**stored, "expires_at": 1}), encoding="utf-8")
    original = (consent_home / "config.toml").read_bytes()
    result = _piped_grant(token)
    assert result.returncode == 2
    assert "expired" in result.stderr
    assert (consent_home / "config.toml").read_bytes() == original


@pytest.mark.integration
def test_issue_token_refuses_a_piped_caller(consent_home: Path) -> None:
    """Only a terminal may mint a token; a model-run shell with closed stdin cannot approve itself."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "issue-token"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "operator terminal" in result.stderr
    assert not (consent_home / ".bridge-consent-operator-token").exists()


def test_approve_in_the_operator_terminal_skips_the_typed_question(
    consent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag typed by an operator at a terminal is itself the confirmation, so no second question is asked."""
    output = Terminal()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stdin", Terminal(""))
    assert runpy.run_path(str(SCRIPT))["main"](["grant", "--approve"]) == 0
    assert "in the operator terminal" in output.getvalue()
    assert "Type Approve" not in output.getvalue()


@pytest.mark.integration
@pytest.mark.parametrize("action", ["check", "revoke"])
@pytest.mark.parametrize("configured", [False, True])
def test_approval_flag_is_grant_only(consent_home: Path, action: str, configured: bool) -> None:
    """Reject a mutating flag on read-only or revoke actions, even when consent already exists."""
    config = consent_home / "config.toml"
    if configured:
        block = (PLUGIN / "rules/claude-transfer-policy.txt").read_text(encoding="utf-8").strip()
        config.write_text("[auto_review]\nextra_policy = " + json.dumps(block) + "\n", encoding="utf-8")
    original = config.read_bytes()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), action, "--approve"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--approve is supported only with grant" in result.stderr
    assert config.read_bytes() == original


@pytest.mark.parametrize(
    "original",
    [
        pytest.param("", id="empty-config"),
        pytest.param('[auto_review]\npolicy = "primary"\n', id="existing-table"),
        pytest.param('["auto_review"]\nextra_policy = """old\n[other]\nsecret = 1\n"""\n', id="embedded-syntax"),
        pytest.param("['auto_review']\n'extra_policy' = 'existing'\n[next]\nvalue = 7\n", id="quoted-keys"),
    ],
)
def test_real_config_shapes_preserve_other_values(
    consent_home: Path, monkeypatch: pytest.MonkeyPatch, original: str
) -> None:
    """Keep multiline strings and neighbouring tables out of the policy edit."""
    namespace = runpy.run_path(str(SCRIPT))
    config = consent_home / "config.toml"
    config.write_text(original, encoding="utf-8")
    before = tomllib.loads(original)
    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.setattr(sys, "stdin", Terminal("Approve\n"))
    assert namespace["main"](["grant"]) == 0
    after = tomllib.loads(config.read_text(encoding="utf-8"))
    after["auto_review"]["extra_policy"] = before.get("auto_review", {}).get("extra_policy", "")
    assert after == {
        **before,
        "auto_review": {**before.get("auto_review", {}), "extra_policy": after["auto_review"]["extra_policy"]},
    }


def test_statement_split_keeps_brackets_inside_strings_and_comments_out_of_table_detection() -> None:
    """Split tricky multi-line TOML into the same statements the per-line parser boundary produced."""
    namespace = runpy.run_path(str(SCRIPT))
    text = (
        'notes = """\n[auto_review]\nextra_policy = "fake"\n"""" \n'
        'paths = [ # ] [\n  \'a]\', "b\\"]",\n  [1, 2],\n]\n'
        "raw = '''\n]'''\n"
        "[auto_review]\n"
        'extra_policy = "old"\n'
    )

    chunks = namespace["_statements"](text)

    assert chunks == [
        'notes = """\n[auto_review]\nextra_policy = "fake"\n"""" \n',
        'paths = [ # ] [\n  \'a]\', "b\\"]",\n  [1, 2],\n]\n',
        "raw = '''\n]'''\n",
        "[auto_review]\n",
        'extra_policy = "old"\n',
    ]


def test_long_multiline_array_is_parsed_once_per_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a large config from stalling consent with one TOML parse per accumulated line."""
    namespace = runpy.run_path(str(SCRIPT))
    parse = namespace["_parse"]
    parsed: list[int] = []

    def _counting_parse(text: str) -> dict[str, object]:
        """Count each parser invocation while keeping real TOML validation."""
        parsed.append(len(text))
        return parse(text)

    monkeypatch.setitem(namespace["_statements"].__globals__, "_parse", _counting_parse)
    text = "[auto_review]\n[other]\nbig = [\n" + "  1,\n" * 5000 + "]\n"

    chunks = namespace["_statements"](text)

    assert len(chunks) == 3
    assert len(parsed) == 3


@pytest.mark.parametrize("mutation", ["edited", "duplicate", "unknown-version"])
def test_unrecognized_grant_is_never_reused_or_overwritten(
    consent_home: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Reject plausible markers when their grant text is no longer the supported consent."""
    namespace = runpy.run_path(str(SCRIPT))
    block = (PLUGIN / "rules/claude-transfer-policy.txt").read_text(encoding="utf-8").strip()
    changed = (
        block + block
        if mutation == "duplicate"
        else block.replace("v1", "v2")
        if mutation == "unknown-version"
        else block.replace("excludes credentials", "includes credentials")
    )
    config = consent_home / "config.toml"
    config.write_text("[auto_review]\nextra_policy = " + json.dumps(changed) + "\n", encoding="utf-8")
    original = config.read_bytes()
    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.setattr(sys, "stdin", Terminal("Approve\n"))
    for action in ("check", "grant", "revoke"):
        assert namespace["main"]([action]) == 2
        assert config.read_bytes() == original
    assert namespace["main"](["grant", "--approve"]) == 2
    assert config.read_bytes() == original


def test_config_change_during_confirmation_is_preserved(consent_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse overwriting an editor's change made while the operator considers consent."""
    namespace = runpy.run_path(str(SCRIPT))
    config = consent_home / "config.toml"

    class EditingTerminal(Terminal):
        """Change the real config at the terminal input boundary."""

        def readline(self, *args: object, **kwargs: object) -> str:
            """Simulate an unrelated editor while returning an affirmative answer."""
            config.write_text('model = "concurrent"\n', encoding="utf-8")
            return "Approve\n"

    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.setattr(sys, "stdin", EditingTerminal())
    assert namespace["main"](["grant"]) == 2
    assert config.read_text(encoding="utf-8") == 'model = "concurrent"\n'
    assert not list(consent_home.glob(".bridge-consent-*"))


def test_hardlinked_config_is_not_read_or_changed(consent_home: Path, tmp_path: Path) -> None:
    """Refuse a native file that aliases another file rather than treating it as owned config."""
    namespace = runpy.run_path(str(SCRIPT))
    config = consent_home / "config.toml"
    alias = tmp_path / "alias.toml"
    alias.hardlink_to(config)
    original = alias.read_bytes()
    assert namespace["main"](["check"]) == 2
    assert namespace["main"](["grant", "--approve"]) == 2
    assert config.read_bytes() == original == alias.read_bytes()


@pytest.mark.parametrize(
    "literal",
    [pytest.param("'existing'", id="literal"), pytest.param('"""existing\nsecond"""', id="multiline")],
)
def test_grant_and_revoke_preserve_assignment_surroundings(
    consent_home: Path, monkeypatch: pytest.MonkeyPatch, literal: str
) -> None:
    """Retain operator comments, quoted keys, indentation and CRLF on either mutation."""
    main = runpy.run_path(str(SCRIPT))["main"]
    config = consent_home / "config.toml"
    prefix, suffix = "  'extra_policy' = ", " # operator rationale\r\n"
    config.write_bytes(("[auto_review]\r\n" + prefix + literal + suffix).encode())
    original_value = tomllib.loads(config.read_text())["auto_review"]["extra_policy"]
    monkeypatch.setattr(sys, "stdout", Terminal())
    for action in ("grant", "revoke"):
        monkeypatch.setattr(sys, "stdin", Terminal("Approve\n"))
        assert main([action]) == 0
        actual = config.read_bytes().decode()
        assert actual.startswith("[auto_review]\r\n" + prefix)
        assert actual.endswith(suffix)
    assert tomllib.loads(actual)["auto_review"]["extra_policy"] == original_value


@pytest.mark.parametrize("linked", [pytest.param("config", id="linked-config"), pytest.param("home", id="linked-home")])
def test_linked_native_paths_are_refused(
    consent_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: str
) -> None:
    """Reject linked configuration and home without following them for mutations."""
    main = runpy.run_path(str(SCRIPT))["main"]
    config = consent_home / "config.toml"
    original = config.read_bytes()
    target = tmp_path / "original.toml"
    if linked == "config":
        config.rename(target)
        link, destination = config, target
    else:
        link, destination = tmp_path / "linked-home", consent_home
        monkeypatch.setenv("CODEX_HOME", str(link))
    try:
        link.symlink_to(destination, target_is_directory=linked == "home")
    except OSError as error:
        pytest.skip(f"symlink capability unavailable: {error}")
    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.setattr(sys, "stdin", Terminal("Approve\n"))
    for action in ("check", "grant", "revoke"):
        assert main([action]) == 2
    assert (target if linked == "config" else config).read_bytes() == original


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('auto_review.extra_policy = "existing"\n', id="dotted-key"),
        pytest.param('auto_review = {extra_policy = "existing"}\n', id="inline-table"),
    ],
)
def test_unsupported_placement_fails_without_writing(
    consent_home: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """Refuse unsupported native layouts rather than migrating surrounding configuration."""
    main = runpy.run_path(str(SCRIPT))["main"]
    config = consent_home / "config.toml"
    config.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.setattr(sys, "stdin", Terminal("Approve\n"))
    assert main(["grant"]) == 2
    assert config.read_text(encoding="utf-8") == text


def test_retargeted_home_is_not_overwritten(consent_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Detect a replaced parent even when the replacement config has identical bytes."""
    main = runpy.run_path(str(SCRIPT))["main"]
    original = (consent_home / "config.toml").read_bytes()
    old_home = consent_home.with_name("original-home")

    class RetargetingTerminal(Terminal):
        """Replace the real parent at the confirmation boundary."""

        def readline(self, *args: object, **kwargs: object) -> str:
            """Keep both old and replacement config bytes intact for the race check."""
            consent_home.rename(old_home)
            consent_home.mkdir()
            (consent_home / "config.toml").write_bytes(original)
            return "Approve\n"

    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.setattr(sys, "stdin", RetargetingTerminal())
    assert main(["grant"]) == 2
    assert (consent_home / "config.toml").read_bytes() == original
    assert (old_home / "config.toml").read_bytes() == original
    assert not list(consent_home.glob(".bridge-consent-*"))
