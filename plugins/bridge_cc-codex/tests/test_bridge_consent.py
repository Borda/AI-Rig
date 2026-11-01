"""Exercise setup consent against disposable native Codex configuration."""

from __future__ import annotations

import functools
import io
import json
import os
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest
from _bridge_link_probes import file_symlinks_supported

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "bin" / "bridge_consent.py"
POLICY_BLOCK = (PLUGIN / "rules/claude-transfer-policy.txt").read_text(encoding="utf-8").strip()
TOKEN_ENV = "BRIDGE_OPERATOR_TOKEN"  # noqa: S105 - variable name, not a secret
DEFAULT_CONFIG = (
    'model = "retained"\napprovals_reviewer = "auto_review"\n[auto_review]\n'
    'policy = "User primary policy"\nextra_policy = "Existing user rule"\n'
)
CONFIGURED_CONFIG = "[auto_review]\nextra_policy = " + json.dumps(POLICY_BLOCK) + "\n"


_skip_no_symlink = pytest.mark.skipif(not file_symlinks_supported(), reason="requires symlink creation capability")


class Terminal(io.StringIO):
    """Represent the external terminal boundary while keeping file operations real."""

    def isatty(self) -> bool:
        """Expose the operator-terminal capability to the helper."""
        return True


class HookedTerminal(Terminal):
    """Terminal that runs a side effect when the operator is asked to confirm, modelling a race."""

    def __init__(self, text: str = "", *, hook: Callable[[], object]) -> None:
        """Remember the side effect to run at the confirmation read."""
        super().__init__(text)
        self.hook = hook

    def readline(self, *args: object, **kwargs: object) -> str:
        """Run the side effect, then return the scripted answer."""
        self.hook()
        return super().readline(*args, **kwargs)


def _cli(*args: str, **options: object) -> subprocess.CompletedProcess[str]:
    """Run the consent helper as a real child process with closed stdin unless told otherwise."""
    settings = {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "check": False, **options}
    return subprocess.run([sys.executable, str(SCRIPT), *args], **settings)


def _piped_grant(token_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``grant --approve`` with closed stdin, as a model-run shell would, carrying the given token variables."""
    env = {key: value for key, value in os.environ.items() if key != TOKEN_ENV}
    return _cli("grant", "--approve", env={**env, **token_env})


def _without_extra_policy(config: dict[str, object]) -> dict[str, object]:
    """Return a parsed config with the consent-bearing ``extra_policy`` key removed."""
    table = {key: value for key, value in config.get("auto_review", {}).items() if key != "extra_policy"}
    return {**config, "auto_review": table}


@pytest.fixture
def consent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every consent operation inside a disposable Codex home."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    (home / "config.toml").write_text(DEFAULT_CONFIG, encoding="utf-8")
    return home


@pytest.fixture
def consent_namespace() -> dict[str, object]:
    """Load the consent helper script once per test so private helpers stay reachable."""
    return runpy.run_path(str(SCRIPT))


@pytest.fixture
def run_consent(
    consent_namespace: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[int, str]]:
    """Return a runner invoking ``main`` in-process behind a scripted operator terminal."""

    def run(args: list[str], answer: str = "Approve\n", terminal: Callable[[str], Terminal] = Terminal):
        """Run one helper action and return its exit code and everything it printed."""
        output = Terminal()
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdout", output)
            patch.setattr(sys, "stdin", terminal(answer))
            code = consent_namespace["main"](args)
        return code, output.getvalue()

    return run


@pytest.fixture
def operator_token(consent_home: Path, run_consent: Callable[..., tuple[int, str]]) -> str:
    """Mint a one-time token the way an operator does, from a terminal, and return what it printed."""
    code, output = run_consent(["issue-token"], answer="")
    assert code == 0, output
    return output.splitlines()[1]


@pytest.fixture
def token_granted(consent_home: Path, operator_token: str) -> bytes:
    """Record consent through the unattended token path and return the resulting config bytes."""
    result = _piped_grant({TOKEN_ENV: operator_token})
    assert result.returncode == 0, result.stderr
    return (consent_home / "config.toml").read_bytes()


class TestConsentLifecycle:
    """Grant, reuse, revoke and decline through the operator terminal."""

    def test_grant_reuse_revoke_preserves_user_configuration(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]]
    ) -> None:
        """Grant once, reuse without prompting, then remove only the approved policy block.

        Starts from a config holding user values beside an existing ``extra_policy``. The user's own keys and text must
        survive both mutations, and the second grant must not ask or rewrite anything.
        """
        config = consent_home / "config.toml"
        original = tomllib.loads(config.read_text(encoding="utf-8"))

        code, output = run_consent(["grant"])
        policy = tomllib.loads(config.read_text(encoding="utf-8"))
        granted = config.read_bytes()
        reuse_code, _ = run_consent(["grant"], answer="")
        reused = config.read_bytes()
        revoke_code, _ = run_consent(["revoke"])

        assert (code, reuse_code, revoke_code) == (0, 0, 0)
        assert policy["model"] == original["model"]
        assert policy["auto_review"]["policy"] == original["auto_review"]["policy"]
        assert policy["auto_review"]["extra_policy"].startswith("Existing user rule")
        assert "private repository" in policy["auto_review"]["extra_policy"]
        assert "Claude" in output
        assert "not an enforced file allowlist" in output
        assert reused == granted
        assert tomllib.loads(config.read_text(encoding="utf-8")) == original

    @pytest.mark.parametrize("answer", ["Deny\n", "\n", "approve\n"])
    def test_non_affirmative_answer_never_writes(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]], answer: str
    ) -> None:
        """Require the exact affirmative response after displaying the consent scope.

        Anything other than the literal ``Approve`` (a denial, an empty line, a lowercase variant) must leave the native
        config byte-identical and exit with the refusal status.
        """
        original = (consent_home / "config.toml").read_bytes()

        code, _ = run_consent(["grant"], answer=answer)

        assert code == 2
        assert (consent_home / "config.toml").read_bytes() == original

    def test_approve_flag_in_operator_terminal_skips_typed_question(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]]
    ) -> None:
        """The flag typed by an operator at a terminal is itself the confirmation, so no second question is asked.

        Stdin is empty, so a leftover prompt would read nothing and refuse; success proves no question was posed.
        """
        code, output = run_consent(["grant", "--approve"], answer="")

        assert code == 0
        assert "in the operator terminal" in output
        assert "Type Approve" not in output

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
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]], original: str
    ) -> None:
        """Keep multiline strings and neighbouring tables out of the policy edit.

        Each shape (empty file, existing table, TOML-looking text inside a string, quoted keys) must gain the policy
        block while every other parsed value stays identical.
        """
        config = consent_home / "config.toml"
        config.write_text(original, encoding="utf-8")
        before = tomllib.loads(original)

        code, _ = run_consent(["grant"])

        after = tomllib.loads(config.read_text(encoding="utf-8"))
        assert code == 0
        assert POLICY_BLOCK in after["auto_review"]["extra_policy"]
        assert _without_extra_policy(after) == _without_extra_policy(before)

    @pytest.mark.parametrize(
        "literal",
        [pytest.param("'existing'", id="literal"), pytest.param('"""existing\nsecond"""', id="multiline")],
    )
    def test_grant_preserves_assignment_surroundings(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]], literal: str
    ) -> None:
        """Retain operator comments, quoted keys, indentation and CRLF when granting.

        The existing ``extra_policy`` assignment uses a quoted key, indentation, a trailing comment and CRLF endings;
        the edit may only extend the value, never reformat the line around it.
        """
        prefix, suffix = "  'extra_policy' = ", " # operator rationale\r\n"
        config = consent_home / "config.toml"
        config.write_bytes(("[auto_review]\r\n" + prefix + literal + suffix).encode())

        code, _ = run_consent(["grant"])

        actual = config.read_bytes().decode()
        assert code == 0
        assert actual.startswith("[auto_review]\r\n" + prefix)
        assert actual.endswith(suffix)

    @pytest.mark.parametrize(
        "literal",
        [pytest.param("'existing'", id="literal"), pytest.param('"""existing\nsecond"""', id="multiline")],
    )
    def test_revoke_restores_original_assignment(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]], literal: str
    ) -> None:
        """Retain operator comments, quoted keys, indentation and CRLF and the original value when revoking.

        A granted config is revoked; the surrounding line text must match the pre-grant layout and the parsed value must
        equal what the operator originally wrote.
        """
        prefix, suffix = "  'extra_policy' = ", " # operator rationale\r\n"
        config = consent_home / "config.toml"
        config.write_bytes(("[auto_review]\r\n" + prefix + literal + suffix).encode())
        original_value = tomllib.loads(config.read_text())["auto_review"]["extra_policy"]
        run_consent(["grant"])

        code, _ = run_consent(["revoke"])

        actual = config.read_bytes().decode()
        assert code == 0
        assert actual.startswith("[auto_review]\r\n" + prefix)
        assert actual.endswith(suffix)
        assert tomllib.loads(actual)["auto_review"]["extra_policy"] == original_value


class TestNonInteractiveCli:
    """Drive the helper as a real child process, where a pipe must never become setup consent."""

    @pytest.mark.integration
    def test_check_reports_absent_consent_without_writing(self, consent_home: Path) -> None:
        """Report absent consent and an unverified session through the real command line.

        A fresh config has no policy block, so the read-only check must say so and leave the file untouched.
        """
        original = (consent_home / "config.toml").read_bytes()

        result = _cli("check")

        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["consent"] == "absent"
        assert report["session_effective"] == "unverified"
        assert (consent_home / "config.toml").read_bytes() == original

    @pytest.mark.integration
    def test_piped_grant_is_refused_without_writing(self, consent_home: Path) -> None:
        """Prove the real command line cannot turn a pipe into setup consent.

        An ``Approve`` line arriving over a pipe is not an operator terminal, so the grant must refuse.
        """
        original = (consent_home / "config.toml").read_bytes()

        result = _cli("grant", input="Approve\n", stdin=None)

        assert result.returncode == 2
        assert "operator terminal" in result.stderr
        assert (consent_home / "config.toml").read_bytes() == original

    @pytest.mark.integration
    @pytest.mark.parametrize("action", ["check", "revoke"])
    @pytest.mark.parametrize(
        "config_text",
        [pytest.param(DEFAULT_CONFIG, id="unconfigured"), pytest.param(CONFIGURED_CONFIG, id="configured")],
    )
    def test_approval_flag_is_grant_only(self, consent_home: Path, action: str, config_text: str) -> None:
        """Reject a mutating flag on read-only or revoke actions, even when consent already exists.

        ``--approve`` only has meaning for ``grant``; on other actions it must fail loudly rather than be ignored.
        """
        config = consent_home / "config.toml"
        config.write_text(config_text, encoding="utf-8")
        original = config.read_bytes()

        result = _cli(action, "--approve")

        assert result.returncode == 2
        assert "--approve is supported only with grant" in result.stderr
        assert config.read_bytes() == original


class TestOperatorToken:
    """Unattended setup grants consent only with a one-time token minted from an operator terminal."""

    @pytest.mark.integration
    def test_grants_without_terminal(self, consent_home: Path, operator_token: str) -> None:
        """Let unattended setup grant once with an operator-minted token, extending only the policy text.

        The child has closed stdin and the token in its environment; the helper must print the policy it recorded and
        change nothing in the config except appending the block to ``extra_policy``.
        """
        config = consent_home / "config.toml"
        original = tomllib.loads(config.read_text(encoding="utf-8"))

        result = _piped_grant({TOKEN_ENV: operator_token})

        expected_policy = "Existing user rule\n\n" + POLICY_BLOCK
        expected = {**original, "auto_review": {**original["auto_review"], "extra_policy": expected_policy}}
        assert result.returncode == 0, result.stderr
        assert "Explicit approval supplied by --approve with a one-time operator token" in result.stdout
        assert POLICY_BLOCK in result.stdout
        assert "Type Approve" not in result.stdout
        assert tomllib.loads(config.read_text(encoding="utf-8")) == expected

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "arguments",
        [pytest.param(["grant", "--approve"], id="approve-flag"), pytest.param(["grant"], id="plain-grant")],
    )
    def test_granted_consent_is_reused_without_token(
        self, consent_home: Path, token_granted: bytes, arguments: list[str]
    ) -> None:
        """Recognise existing consent on a later grant that carries no token and has no terminal.

        After a token grant the same consent must be accepted idempotently, with the config left byte-identical.
        """
        result = _cli(*arguments)

        assert result.returncode == 0, result.stderr
        assert (consent_home / "config.toml").read_bytes() == token_granted

    @pytest.mark.integration
    def test_check_reports_configured_after_grant(self, token_granted: bytes) -> None:
        """The next dispatch check recognises consent recorded by the token grant.

        The report must name the configured reviewer and keep the session effect unverified, since the helper cannot see
        inside a running session.
        """
        checked = _cli("check")

        assert checked.returncode == 0, checked.stderr
        assert json.loads(checked.stdout) == {
            "consent": "configured",
            "configured_reviewer": "auto_review",
            "session_effective": "unverified",
        }

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("token_env", "refusal"),
        [
            pytest.param({}, "operator terminal", id="no-token"),
            pytest.param({TOKEN_ENV: "not-the-minted-token"}, "does not match", id="wrong-token"),
        ],
    )
    def test_piped_grant_without_valid_operator_proof_never_writes(
        self, consent_home: Path, operator_token: str, token_env: dict[str, str], refusal: str
    ) -> None:
        """A model-run shell has no terminal and cannot mint a token, so --approve alone must not record consent.

        An outstanding token exists in the wrong-token case, proving a guess does not consume or satisfy it.
        """
        original = (consent_home / "config.toml").read_bytes()

        result = _piped_grant(token_env)

        assert result.returncode == 2
        assert refusal in result.stderr
        assert (consent_home / "config.toml").read_bytes() == original

    @pytest.mark.integration
    def test_is_single_use(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]], operator_token: str, token_granted: bytes
    ) -> None:
        """A token that already authorized one grant cannot authorize a second after revocation.

        The first grant consumes the token; once consent is revoked, replaying the same token must be refused.
        """
        run_consent(["revoke"])
        revoked = (consent_home / "config.toml").read_bytes()

        replay = _piped_grant({TOKEN_ENV: operator_token})

        assert not (consent_home / ".bridge-consent-operator-token").exists()
        assert replay.returncode == 2
        assert "token" in replay.stderr
        assert (consent_home / "config.toml").read_bytes() == revoked

    @pytest.mark.integration
    def test_expired_token_is_refused(self, consent_home: Path, operator_token: str) -> None:
        """A token older than its lifetime is refused even when it is the minted value.

        The stored record is rewritten with an expiry in the distant past, simulating a token left unused too long.
        """
        record = consent_home / ".bridge-consent-operator-token"
        stored = json.loads(record.read_text(encoding="utf-8"))
        record.write_text(json.dumps({**stored, "expires_at": 1}), encoding="utf-8")
        original = (consent_home / "config.toml").read_bytes()

        result = _piped_grant({TOKEN_ENV: operator_token})

        assert result.returncode == 2
        assert "expired" in result.stderr
        assert (consent_home / "config.toml").read_bytes() == original

    @pytest.mark.integration
    def test_issue_refuses_piped_caller(self, consent_home: Path) -> None:
        """Only a terminal may mint a token; a model-run shell with closed stdin cannot approve itself.

        With stdin closed the helper must refuse and leave no token record behind.
        """
        result = _cli("issue-token")

        assert result.returncode == 2
        assert "operator terminal" in result.stderr
        assert not (consent_home / ".bridge-consent-operator-token").exists()


class TestUnsafeConfiguration:
    """Refuse to mutate or trust configuration that is edited, aliased, linked or laid out unexpectedly."""

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(["check"], id="check"),
            pytest.param(["grant"], id="grant"),
            pytest.param(["revoke"], id="revoke"),
            pytest.param(["grant", "--approve"], id="grant-approve"),
        ],
    )
    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda block: block.replace("excludes credentials", "includes credentials"), id="edited"),
            pytest.param(lambda block: block + block, id="duplicate"),
            pytest.param(lambda block: block.replace("v1", "v2"), id="unknown-version"),
        ],
    )
    def test_unrecognized_grant_is_never_reused_or_overwritten(
        self,
        consent_home: Path,
        run_consent: Callable[..., tuple[int, str]],
        mutate: Callable[[str], str],
        args: list[str],
    ) -> None:
        """Reject plausible markers when their grant text is no longer the supported consent.

        A config carrying an edited, duplicated or future-version block looks granted but is not the approved text, so
        every action must refuse and leave the bytes alone.
        """
        config = consent_home / "config.toml"
        config.write_text("[auto_review]\nextra_policy = " + json.dumps(mutate(POLICY_BLOCK)) + "\n", encoding="utf-8")
        original = config.read_bytes()

        code, _ = run_consent(args)

        assert code == 2
        assert config.read_bytes() == original

    def test_config_change_during_confirmation_is_preserved(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]]
    ) -> None:
        """Refuse overwriting an editor's change made while the operator considers consent.

        The config is rewritten at the moment the operator answers; the helper must notice, refuse, and leave neither
        the editor's text damaged nor a temporary file behind.
        """
        config = consent_home / "config.toml"
        editing = functools.partial(HookedTerminal, hook=lambda: config.write_text('model = "concurrent"\n'))

        code, _ = run_consent(["grant"], terminal=editing)

        assert code == 2
        assert config.read_text(encoding="utf-8") == 'model = "concurrent"\n'
        assert not list(consent_home.glob(".bridge-consent-*"))

    @pytest.mark.parametrize(
        "args", [pytest.param(["check"], id="check"), pytest.param(["grant", "--approve"], id="grant-approve")]
    )
    def test_hardlinked_config_is_not_read_or_changed(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]], tmp_path: Path, args: list[str]
    ) -> None:
        """Refuse a native file that aliases another file rather than treating it as owned config.

        The config has a second hard link elsewhere; editing it would silently change that other path too.
        """
        config = consent_home / "config.toml"
        alias = tmp_path / "alias.toml"
        alias.hardlink_to(config)
        original = alias.read_bytes()

        code, _ = run_consent(args)

        assert code == 2
        assert config.read_bytes() == original == alias.read_bytes()

    @_skip_no_symlink
    @pytest.mark.parametrize("action", ["check", "grant", "revoke"])
    def test_symlinked_config_is_refused(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]], tmp_path: Path, action: str
    ) -> None:
        """Reject a linked configuration file without following it for mutations.

        ``config.toml`` is replaced by a symlink to the real file; every action must refuse and leave the target as is.
        """
        config = consent_home / "config.toml"
        original = config.read_bytes()
        target = tmp_path / "original.toml"
        config.rename(target)
        config.symlink_to(target)

        code, _ = run_consent([action])

        assert code == 2
        assert target.read_bytes() == original

    @_skip_no_symlink
    @pytest.mark.parametrize("action", ["check", "grant", "revoke"])
    def test_symlinked_home_is_refused(
        self,
        consent_home: Path,
        run_consent: Callable[..., tuple[int, str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        action: str,
    ) -> None:
        """Reject a linked Codex home without following it for mutations.

        ``CODEX_HOME`` points at a symlink to the real home; every action must refuse and leave the config as is.
        """
        original = (consent_home / "config.toml").read_bytes()
        link = tmp_path / "linked-home"
        link.symlink_to(consent_home, target_is_directory=True)
        monkeypatch.setenv("CODEX_HOME", str(link))

        code, _ = run_consent([action])

        assert code == 2
        assert (consent_home / "config.toml").read_bytes() == original

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param('auto_review.extra_policy = "existing"\n', id="dotted-key"),
            pytest.param('auto_review = {extra_policy = "existing"}\n', id="inline-table"),
        ],
    )
    def test_unsupported_placement_fails_without_writing(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]], text: str
    ) -> None:
        """Refuse unsupported native layouts rather than migrating surrounding configuration.

        Dotted keys and inline tables are valid TOML the line editor cannot extend safely, so grant must stop.
        """
        config = consent_home / "config.toml"
        config.write_text(text, encoding="utf-8")

        code, _ = run_consent(["grant"])

        assert code == 2
        assert config.read_text(encoding="utf-8") == text

    def test_retargeted_home_is_not_overwritten(
        self, consent_home: Path, run_consent: Callable[..., tuple[int, str]]
    ) -> None:
        """Detect a replaced parent even when the replacement config has identical bytes.

        The Codex home directory is swapped for a new one holding the same config bytes while the operator confirms; the
        grant must refuse and touch neither copy.
        """
        original = (consent_home / "config.toml").read_bytes()
        old_home = consent_home.with_name("original-home")

        def retarget() -> None:
            """Keep both old and replacement config bytes intact for the race check."""
            consent_home.rename(old_home)
            consent_home.mkdir()
            (consent_home / "config.toml").write_bytes(original)

        code, _ = run_consent(["grant"], terminal=functools.partial(HookedTerminal, hook=retarget))

        assert code == 2
        assert (consent_home / "config.toml").read_bytes() == original
        assert (old_home / "config.toml").read_bytes() == original
        assert not list(consent_home.glob(".bridge-consent-*"))


class TestTomlStatements:
    """Split native TOML into whole statements without being fooled by brackets in strings and comments."""

    def test_split_keeps_brackets_in_strings_and_comments_out_of_table_detection(
        self, consent_namespace: dict[str, object]
    ) -> None:
        """Split tricky multi-line TOML into the same statements the per-line parser boundary produced.

        Bracketed text inside multi-line strings, arrays and comments must not be mistaken for table headers.
        """
        text = (
            'notes = """\n[auto_review]\nextra_policy = "fake"\n"""" \n'
            'paths = [ # ] [\n  \'a]\', "b\\"]",\n  [1, 2],\n]\n'
            "raw = '''\n]'''\n"
            "[auto_review]\n"
            'extra_policy = "old"\n'
        )

        chunks = consent_namespace["_statements"](text)

        assert chunks == [
            'notes = """\n[auto_review]\nextra_policy = "fake"\n"""" \n',
            'paths = [ # ] [\n  \'a]\', "b\\"]",\n  [1, 2],\n]\n',
            "raw = '''\n]'''\n",
            "[auto_review]\n",
            'extra_policy = "old"\n',
        ]

    def test_long_multiline_array_is_parsed_once_per_statement(
        self, consent_namespace: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep a large config from stalling consent with one TOML parse per accumulated line.

        A 5000-line array sits between two tables; the parser must run once per statement, not once per line.
        """
        parse = mock.create_autospec(consent_namespace["_parse"], side_effect=consent_namespace["_parse"])
        monkeypatch.setitem(consent_namespace["_statements"].__globals__, "_parse", parse)
        text = "[auto_review]\n[other]\nbig = [\n" + "  1,\n" * 5000 + "]\n"

        chunks = consent_namespace["_statements"](text)

        assert len(chunks) == 3
        assert parse.call_count == 3
