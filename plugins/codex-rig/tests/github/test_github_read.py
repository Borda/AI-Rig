"""Acceptance checks for the shared GitHub read-only transport boundary.

The reader is exercised with fake command runners and URL openers, so no test spawns `gh` or touches the network. The
one real subprocess is the default transport's output cap, which is itself the contract under test.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock
from urllib.error import HTTPError, URLError

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
MODULE = PLUGIN_ROOT / "shared" / "github_read.py"
ISSUE_VIEW = ["gh", "issue", "view", "17", "--json", "title"]
ISSUE_URL = "https://api.github.com/repos/Borda/AI-Rig/issues/17"
PR_VIEW = ["gh", "pr", "view", "7"]
PR_URL = "https://api.github.com/repos/example/project/pulls/7"
JOB_LOG = ["gh", "api", "/repos/example/project/actions/jobs/17/logs"]
CONNECTION_RESET = b"connection reset by peer"


def _load_reader() -> ModuleType:
    """Load the standalone GitHub reader without package installation."""
    assert MODULE.is_file(), MODULE
    return load_module("codex_rig_github_read", MODULE)


@pytest.fixture
def reader() -> ModuleType:
    """Return a freshly loaded GitHub reader module."""
    return _load_reader()


def _fixed_runner(*, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> mock.Mock:
    """Return a recording command runner that always answers with the given process result."""

    def respond(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Build the fixed process result for one command."""
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)

    return mock.Mock(side_effect=respond)


def _public_opener(payload: bytes = b'{"number": 17}') -> tuple[Callable[..., io.BytesIO], list[tuple[str, str]]]:
    """Return a URL opener serving a fixed body, plus the list of (url, method) pairs it was asked for."""
    requested: list[tuple[str, str]] = []

    def open_url(request: Any, *, timeout: int, context: Any) -> io.BytesIO:
        """Record the public request and require an explicit SSL context."""
        assert context is not None
        requested.append((request.full_url, request.get_method()))
        return io.BytesIO(payload)

    return open_url, requested


class _EmptyTrustContext:
    """Model a Python installation whose default trust store holds no certificates."""

    def __init__(self, system_bundle: Path) -> None:
        """Remember the system bundle on offer and start with nothing loaded."""
        self.system_bundle = system_bundle
        self.loaded_bundles: list[str] = []

    def cert_store_stats(self) -> dict[str, int]:
        """Report an empty trust store so fallback CA loading is exercised."""
        return {"x509_ca": 0}

    def load_verify_locations(self, *, cafile: str) -> None:
        """Record the CA bundle chosen by the SSL helper."""
        self.loaded_bundles.append(cafile)


@pytest.fixture
def empty_trust_context(reader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _EmptyTrustContext:
    """Install an empty-trust SSL context and a single system CA bundle candidate."""
    system_bundle = tmp_path / "system-ca.pem"
    system_bundle.write_text("test CA bundle", encoding="utf-8")
    context = _EmptyTrustContext(system_bundle)
    monkeypatch.setattr(reader.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(reader, "SYSTEM_CA_FILE_CANDIDATES", (system_bundle,))
    return context


class TestRunGhRead:
    """The allowlist deciding which `gh` commands may reach the runner."""

    @pytest.mark.parametrize(
        ("argv", "response"),
        [
            pytest.param(["gh", "issue", "view", "17", "--json", "title"], b"{}", id="issue-view"),
            pytest.param(["gh", "release", "view", "v1.2.3", "--json", "name"], b"{}", id="release-view"),
            pytest.param(["gh", "repo", "view", "Borda/AI-Rig", "--json", "name"], b"{}", id="repo-view"),
            pytest.param(
                ["gh", "api", "graphql", "-f", "query=query { viewer { login } }"],
                b'{"data": {}}',
                id="graphql-query-posted-but-read-only",
            ),
            pytest.param(["gh", "api", "/repos/Borda/AI-Rig/issues/17"], b"{}", id="rest-implicit-get"),
            pytest.param(
                ["gh", "api", "/repos/Borda/AI-Rig/issues/17", "--method", "GET"], b"{}", id="rest-explicit-get"
            ),
        ],
    )
    def test_allows_read_commands(self, reader: ModuleType, argv: list[str], response: bytes) -> None:
        """View commands, REST GETs and GraphQL queries run without widening to mutating commands.

        GitHub transports a GraphQL query with POST, so it must be recognised as a read by its content rather than by
        its method.
        """
        runner = _fixed_runner(stdout=response)

        payload = reader.run_gh_read(runner, argv, timeout=5, label="gh-read")

        assert payload == response
        assert [call.args[0] for call in runner.call_args_list] == [argv]

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["gh", "auth", "status"], id="auth-status"),
            pytest.param(["gh", "pr", "merge", "17"], id="pr-merge"),
            pytest.param(["gh", "pr", "checkout", "--detach", "17"], id="pr-checkout"),
            pytest.param(["gh", "issue", "view", "17", "--web"], id="web-flag"),
            pytest.param(["gh", "issue", "view", "17", "--web=true"], id="web-assignment"),
            pytest.param(["gh", "issue", "view", "17", "-w=true"], id="short-web-assignment"),
            pytest.param(["gh", "api", "/repos/Borda/AI-Rig/issues", "--method", "POST"], id="rest-post"),
            pytest.param(
                ["gh", "api", "graphql", "-f", "query=mutation { closeIssue(input: {}) { issue { id } } }"],
                id="graphql-mutation",
            ),
            pytest.param(["gh", "evil", "view", "17"], id="unknown-group"),
            pytest.param(["gh", "auth", "view"], id="credential-group"),
            pytest.param(["gh", "api", "graphql", "-F", "query=@/private/secret"], id="graphql-file-field"),
            pytest.param(
                ["gh", "api", "/repos/Borda/AI-Rig/issues", "-F", "body=@/private/secret"], id="rest-file-field"
            ),
        ],
    )
    def test_rejects_non_read_commands_without_running_them(self, reader: ModuleType, argv: list[str]) -> None:
        """Credential inspection, mutations, browser opening, extensions and file-backed fields never execute.

        Each command is refused before the runner is reached, so a rejected input proves it also had no side effect.
        """
        runner = _fixed_runner()

        with pytest.raises(reader.GitHubReadError, match="unsafe-gh-command:unsafe"):
            reader.run_gh_read(runner, argv, timeout=5, label="unsafe")

        runner.assert_not_called()

    @pytest.mark.parametrize(
        ("endpoint", "arguments"),
        [
            pytest.param("/repos/example/project/issues/17", [], id="non-log-resource"),
            pytest.param("/repos/example/project/actions/runs/17/logs", [], id="binary-run-archive"),
            pytest.param("/repos/example/project/actions/jobs/17/logs?query=1", [], id="noncanonical-endpoint"),
            pytest.param("/repos/example/project/actions/jobs/17/logs", ["--method", "DELETE"], id="delete-job-log"),
        ],
    )
    def test_escape_output_flag_stays_restricted_to_job_log_gets(
        self, reader: ModuleType, endpoint: str, arguments: list[str]
    ) -> None:
        """The display compatibility flag cannot widen endpoints or enable mutations."""
        runner = _fixed_runner()

        with pytest.raises(reader.GitHubReadError, match="unsafe-gh-command"):
            reader.run_gh_read(
                runner, ["gh", "api", endpoint, *arguments, "--allow-escape-sequences"], timeout=5, label="unsafe"
            )

        runner.assert_not_called()

    def test_default_transport_rejects_oversized_output_without_returning_it(
        self, reader: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Production CLI reads are bounded before response bytes enter normal result handling.

        This is the one real subprocess in the file: the output cap is enforced on the live pipe, so a fake runner
        could not prove it.
        """
        monkeypatch.setattr(reader, "MAX_OUTPUT_BYTES", 8)

        with pytest.raises(reader.GitHubReadError, match="command-output-oversized"):
            reader._run_default_gh_command([sys.executable, "-c", "import sys; sys.stdout.write('x' * 9)"], timeout=5)


class TestJobLogs:
    """Colored job-log reads: escape-guard recovery, sanitization and the output bound."""

    @pytest.mark.parametrize("escape_guard", [False, True])
    def test_recovers_cli_escape_guard_and_removes_terminal_controls(
        self, reader: ModuleType, escape_guard: bool
    ) -> None:
        """Colored job logs read on old and new gh versions without returning executable terminal controls.

        Newer gh refuses output containing escape sequences until `--allow-escape-sequences` is passed, so the reader
        retries once and then strips the controls itself.
        """
        calls: list[list[str]] = []

        def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            """Model gh's observed output refusal followed by the same successful GET."""
            calls.append(command)
            if escape_guard and "--allow-escape-sequences" not in command:
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout=b"discard partial output",
                    stderr=b"the response contains terminal escape sequences; "
                    b"pass --allow-escape-sequences to output it anyway\n",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    b"\x1b[31mFAILED\x1b[0m\r\n"
                    b"\x1b]8;;https://example.invalid\x1b\\trace\x1b]8;;\x1b\\\n"
                    b"\x1b]52;c;clipboard\x07safe\n\x1bunfinished\x08\r"
                ),
                stderr=b"",
            )

        payload = reader.run_gh_read(runner, JOB_LOG, timeout=5, label="job-log")

        assert payload == b"FAILED\ntrace\nsafe\n\\u001bunfinished\\u0008\\u000d"
        assert calls == ([JOB_LOG, [*JOB_LOG, "--allow-escape-sequences"]] if escape_guard else [JOB_LOG])

    @pytest.mark.parametrize(
        "endpoint", ["/repos/example/project/actions/jobs/17/logs", "/repos/example/project/issues/17"]
    )
    def test_persistent_escape_refusal_is_bounded_and_keeps_output_private(
        self, reader: ModuleType, endpoint: str
    ) -> None:
        """A persistent refusal yields a safe reason, retried once only for job logs and never via public fallback.

        The refused response carries confidential text on both streams; none of it may reach the diagnostics.
        """
        runner = _fixed_runner(
            returncode=1,
            stdout=b"private-output",
            stderr=b"the response contains terminal escape sequences\nsecret-token",
        )
        public_fallback = mock.Mock(side_effect=AssertionError("public fallback called"))

        with pytest.raises(reader.GitHubReadError, match="github-command-failed:log") as error:
            reader.read_with_fallback(
                runner,
                ["gh", "api", endpoint],
                timeout=5,
                label="log",
                fallback_url="https://api.github.com" + endpoint,
                open_url=public_fallback,
            )

        assert runner.call_count == (2 if endpoint.endswith("/logs") else 1)
        assert error.value.diagnostics["failure_reason"] == "terminal-escape-sequences-refused"
        assert "secret-token" not in str(error.value.diagnostics)
        assert "private-output" not in str(error.value.diagnostics)

    def test_sanitization_cannot_expand_output_beyond_limit(
        self, reader: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The output bound is enforced after escaping controls as well as before decoding.

        Two raw NUL bytes fit the limit, but their escaped representation does not.
        """
        monkeypatch.setattr(reader, "MAX_OUTPUT_BYTES", 8)
        runner = _fixed_runner(stdout=b"\x00\x00")

        with pytest.raises(reader.GitHubReadError, match="command-output-oversized:log"):
            reader.run_gh_read(runner, JOB_LOG, timeout=5, label="log")


class TestPublicFallback:
    """The unauthenticated public HTTPS fallback after the CLI cannot reach GitHub."""

    @pytest.mark.parametrize(
        "stderr",
        [
            pytest.param(CONNECTION_RESET, id="connection-reset"),
            pytest.param(b"could not resolve host: api.github.com", id="dns-could-not-resolve"),
            pytest.param(b"dial tcp: lookup api.github.com: no such host", id="dns-no-such-host"),
            pytest.param(b"temporary failure in name resolution", id="dns-name-resolution"),
        ],
    )
    def test_uses_public_https_get_after_network_failure(self, reader: ModuleType, stderr: bytes) -> None:
        """The public API is used, with a plain GET, only after the CLI has a classified network failure.

        Common transport and DNS diagnostics from the GitHub CLI all classify as network, which is what enables the
        fallback.
        """
        open_url, requested = _public_opener()

        payload, transport = reader.read_with_fallback(
            _fixed_runner(returncode=1, stderr=stderr),
            ISSUE_VIEW,
            timeout=5,
            label="gh-issue-view",
            fallback_url=ISSUE_URL,
            open_url=open_url,
        )

        assert reader.github_failure_class(stderr) == "github-network"
        assert payload == b'{"number": 17}'
        assert transport == "public-https-fallback"
        assert requested == [(ISSUE_URL, "GET")]

    def test_keeps_command_unavailable_fail_closed(self, reader: ModuleType) -> None:
        """A missing local GitHub CLI is not proof that public PR data is safe to collect.

        Without `gh` the caller's identity and permissions are unknown, so no public request may be made.
        """
        runner = mock.Mock(side_effect=OSError("gh not installed"))
        open_url = mock.Mock(side_effect=AssertionError("command-unavailable must not activate public HTTPS fallback"))

        with pytest.raises(reader.GitHubReadError, match="command-unavailable:gh-issue-view") as error:
            reader.read_with_fallback(
                runner, ISSUE_VIEW, timeout=5, label="gh-issue-view", fallback_url=ISSUE_URL, open_url=open_url
            )

        assert error.value.diagnostics == {
            "failure_class": "command-unavailable",
            "failure_reason": "unavailable",
            "label": "gh-issue-view",
        }
        open_url.assert_not_called()

    def test_rejects_non_github_url(self, reader: ModuleType) -> None:
        """Fallback requests are limited to the public GitHub API host."""
        runner = _fixed_runner(returncode=1, stderr=CONNECTION_RESET)

        with pytest.raises(reader.GitHubReadError, match="unsafe-github-fallback-url:gh-issue-view"):
            reader.read_with_fallback(
                runner, ISSUE_VIEW, timeout=5, label="gh-issue-view", fallback_url="https://example.invalid/metadata"
            )

    def test_rejects_tokenized_url(self, reader: ModuleType) -> None:
        """A fallback URL carrying a credential query parameter is refused, keeping the fallback unauthenticated."""
        runner = _fixed_runner(returncode=1, stderr=CONNECTION_RESET)

        with pytest.raises(reader.GitHubReadError, match="unsafe-github-fallback-url:gh-issue-view"):
            reader.read_with_fallback(
                runner, ISSUE_VIEW, timeout=5, label="gh-issue-view", fallback_url=f"{ISSUE_URL}?access_token=secret"
            )

    def test_normalizes_transport_error_without_surfacing_detail(self, reader: ModuleType) -> None:
        """A transport error that embeds a token in its message is reported without that detail."""
        runner = _fixed_runner(returncode=1, stderr=CONNECTION_RESET)
        offline = mock.Mock(side_effect=OSError(f"{ISSUE_URL}?token=secret"))

        with pytest.raises(reader.GitHubReadError, match="github-network:gh-issue-view") as error:
            reader.read_with_fallback(
                runner, ISSUE_VIEW, timeout=5, label="gh-issue-view", fallback_url=ISSUE_URL, open_url=offline
            )

        assert "token" not in str(error.value)

    @pytest.mark.parametrize(
        "fallback_error",
        [
            pytest.param(OSError("network unavailable secret-token"), id="socket-error"),
            pytest.param(URLError("DNS failure secret-token"), id="url-error"),
        ],
    )
    def test_failed_fallback_preserves_primary_network_diagnostic(
        self, reader: ModuleType, fallback_error: OSError
    ) -> None:
        """The actionable CLI reason is kept when the fallback fails in the same network category.

        The CLI's DNS failure is more informative than the fallback's generic transport error, and neither may leak the
        token embedded in the fallback message.
        """
        runner = _fixed_runner(returncode=1, stderr=b"could not resolve host api.github.com")
        offline = mock.Mock(side_effect=fallback_error)

        with pytest.raises(reader.GitHubReadError, match="github-network:gh-pr-view") as error:
            reader.read_with_fallback(
                runner, PR_VIEW, timeout=5, label="gh-pr-view", fallback_url=PR_URL, open_url=offline
            )

        assert error.value.diagnostics == {
            "exit_code": 1,
            "failure_class": "github-network",
            "failure_reason": "dns",
            "label": "gh-pr-view",
        }
        assert "secret-token" not in str(error.value)

    def test_distinct_http_failure_stays_authoritative(self, reader: ModuleType) -> None:
        """A definitive fallback access failure replaces the earlier network error.

        A 403 from the public API says more about the PR than the CLI's connection reset, and its body is not exposed.
        """
        runner = _fixed_runner(returncode=1, stderr=CONNECTION_RESET)
        forbidden = mock.Mock(side_effect=HTTPError("https://api.github.com", 403, "secret-token", None, None))

        with pytest.raises(reader.GitHubReadError, match="github-permission:gh-pr-view") as error:
            reader.read_with_fallback(
                runner, PR_VIEW, timeout=5, label="gh-pr-view", fallback_url=PR_URL, open_url=forbidden
            )

        assert error.value.diagnostics is None
        assert "secret-token" not in str(error.value)

    @pytest.mark.parametrize(
        "stderr",
        [
            pytest.param(b"resource not accessible", id="resource-not-accessible"),
            pytest.param(
                b"GraphQL: `example-org` forbids access via a personal access token (classic). (repository)",
                id="classic-token-policy",
            ),
            pytest.param(
                b"GraphQL: `tls-team` forbids access via a personal access token (classic). (repository)",
                id="classic-token-policy-tls-name",
            ),
        ],
    )
    def test_not_used_after_gh_permission_failure(self, reader: ModuleType, stderr: bytes) -> None:
        """An authenticated GitHub permission decision is not bypassed with public HTTPS."""
        open_url = mock.Mock(side_effect=AssertionError("public fallback must not run after a permission failure"))

        with pytest.raises(reader.GitHubReadError, match="github-permission:gh-issue-view"):
            reader.read_with_fallback(
                _fixed_runner(returncode=1, stderr=stderr),
                ISSUE_VIEW,
                timeout=5,
                label="gh-issue-view",
                fallback_url=ISSUE_URL,
                open_url=open_url,
            )

        open_url.assert_not_called()


class TestPublicSslContext:
    """Certificate trust used by the public HTTPS fallback."""

    def test_loads_system_bundle_when_default_store_is_empty(
        self, reader: ModuleType, empty_trust_context: _EmptyTrustContext
    ) -> None:
        """HTTPS verification recovers when Python has no configured default CA file."""
        assert reader._public_github_ssl_context() is empty_trust_context
        assert empty_trust_context.loaded_bundles == [str(empty_trust_context.system_bundle)]

    def test_preserves_explicit_ca_configuration(
        self,
        reader: ModuleType,
        empty_trust_context: _EmptyTrustContext,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit caller-provided trust configuration is not widened with the system bundle.

        With `SSL_CERT_FILE` set the empty store may just be lazy, so the helper must leave it alone.
        """
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "explicit-ca.pem"))

        assert reader._public_github_ssl_context() is empty_trust_context
        assert empty_trust_context.loaded_bundles == []


class TestFailureClassification:
    """Mapping opaque CLI diagnostics onto safe failure classes and reasons."""

    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            pytest.param(b"run gh auth login to authenticate", "github-auth", id="auth-login-prompt"),
            pytest.param(b"HTTP 401: authentication required", "github-auth", id="http-401"),
            pytest.param(b"request requires authentication", "github-auth", id="requires-authentication"),
            pytest.param(b"oauth token has expired", "github-auth", id="expired-oauth-token"),
            pytest.param(b"context deadline exceeded", "github-network", id="context-deadline"),
            pytest.param(b"read: connection reset by peer", "github-network", id="connection-reset"),
            pytest.param(b"failed to connect to api.github.com port 443", "github-network", id="failed-to-connect"),
            pytest.param(b"Client.Timeout exceeded while awaiting headers", "github-network", id="client-timeout"),
        ],
    )
    def test_routes_auth_and_transport_diagnostics(self, reader: ModuleType, stderr: bytes, expected: str) -> None:
        """Opaque CLI diagnostics route to the safe recovery category without retaining them."""
        assert reader.github_failure_class(stderr) == expected

    @pytest.mark.parametrize("object_type", ["PullRequest", "Repository"])
    def test_missing_graphql_object_is_not_found(self, reader: ModuleType, object_type: str) -> None:
        """GraphQL object resolution failure is not mistaken for DNS resolution failure."""
        stderr = f"GraphQL: Could not resolve to a {object_type} with the supplied identity.".encode()

        assert reader.github_failure_class(stderr) == "github-not-found"
        assert reader.github_failure_reason(stderr, "github-not-found") == "not-found"

    @pytest.mark.parametrize(
        ("stderr", "expected_reason"),
        [
            pytest.param(b"could not resolve host: api.github.com", "dns", id="dns"),
            pytest.param(b"read: connection reset by peer", "connection-reset", id="connection-reset"),
        ],
    )
    def test_run_persists_safe_network_reason_without_stderr(
        self, reader: ModuleType, stderr: bytes, expected_reason: str
    ) -> None:
        """Retry routing stays specific without storing raw network diagnostics or credentials."""
        runner = _fixed_runner(returncode=1, stderr=stderr)

        with pytest.raises(reader.GitHubReadError, match="github-network:gh-issue-view") as error:
            reader.run_gh_read(runner, ISSUE_VIEW, timeout=5, label="gh-issue-view")

        assert error.value.diagnostics == {
            "exit_code": 1,
            "failure_class": "github-network",
            "failure_reason": expected_reason,
            "label": "gh-issue-view",
        }
        assert stderr.decode() not in str(error.value)

    @pytest.mark.parametrize(
        ("stderr", "expected_class", "expected_reason"),
        [
            pytest.param(
                b"fatal: a branch named 'feature' already exists",
                "github-command-failed",
                "local-branch-exists",
                id="branch-exists",
            ),
            pytest.param(
                b"fatal: a branch named 'feature-tls' already exists",
                "github-command-failed",
                "local-branch-exists",
                id="branch-name-with-tls",
            ),
            pytest.param(
                b"fatal: a branch named 'feature'tls' already exists",
                "github-command-failed",
                "local-branch-exists",
                id="branch-name-with-apostrophe",
            ),
            pytest.param(
                b"fatal: a branch named 'permission-fix' already exists",
                "github-command-failed",
                "local-branch-exists",
                id="branch-name-with-permission",
            ),
            pytest.param(
                b"fatal: 'feature' is already checked out at '/private/checkout'",
                "github-command-failed",
                "local-branch-in-other-worktree",
                id="other-worktree",
            ),
            pytest.param(
                b"fatal: 'feature'tls' is already checked out at '/private/permission'checkout'",
                "github-command-failed",
                "local-branch-in-other-worktree",
                id="other-worktree-apostrophes",
            ),
            pytest.param(
                b"fatal: cannot lock ref 'refs/heads/feature': is at abc but expected def",
                "github-command-failed",
                "local-ref-lock-failed",
                id="ref-lock",
            ),
            pytest.param(
                b"fatal: cannot lock ref 'refs/heads/feature'tls': is at abc but expected def",
                "github-command-failed",
                "local-ref-lock-failed",
                id="ref-lock-apostrophe",
            ),
            pytest.param(
                b"error: Your local changes to the following files would be overwritten by checkout:\n\tfile.py",
                "github-command-failed",
                "local-changes-overwritten",
                id="tracked-changes",
            ),
            pytest.param(
                b"error: Your local changes to the following files would be overwritten by checkout:\n\tpermission_notes.txt",
                "github-command-failed",
                "local-changes-overwritten",
                id="tracked-changes-permission-path",
            ),
            pytest.param(
                b"error: The following untracked working tree files would be overwritten by checkout:\n\tfile.py",
                "github-command-failed",
                "local-untracked-files-overwritten",
                id="untracked-files",
            ),
            pytest.param(
                b"error: The following untracked working tree files would be overwritten by checkout:\n\ttls_notes.txt",
                "github-command-failed",
                "local-untracked-files-overwritten",
                id="untracked-files-tls-path",
            ),
            pytest.param(
                b"fatal: Not possible to fast-forward, aborting.",
                "github-command-failed",
                "local-fast-forward-unavailable",
                id="fast-forward",
            ),
            pytest.param(
                b" ! [rejected]        feature -> feature (non-fast-forward)",
                "github-command-failed",
                "local-fetch-non-fast-forward",
                id="fetch-rejected",
            ),
            pytest.param(
                b"   ! [rejected] feature-tls -> origin/feature-tls (non-fast-forward)\n",
                "github-command-failed",
                "local-fetch-non-fast-forward",
                id="fetch-rejected-tls-ref",
            ),
            pytest.param(
                b"checkout failed for an unknown reason", "github-command-failed", "unclassified", id="unknown"
            ),
            pytest.param(
                b"note: a branch already exists somewhere",
                "github-command-failed",
                "unclassified",
                id="unrelated-branch-text",
            ),
            pytest.param(
                b"fatal: unknown diagnostic for 'tls permission HTTP 401 connection refused'",
                "github-command-failed",
                "unclassified",
                id="unknown-quoted-keywords",
            ),
            pytest.param(
                b'error: unknown diagnostic for "tls permission HTTP 401 connection refused"',
                "github-command-failed",
                "unclassified",
                id="unknown-double-quoted-keywords",
            ),
            pytest.param(
                b"fatal: a branch named 'feature' already exists\nHTTP 401: authentication required",
                "github-auth",
                "auth",
                id="auth-beats-branch-exists",
            ),
            pytest.param(
                b"fatal: a branch named 'feature'tls' already exists\nHTTP 401: authentication required",
                "github-auth",
                "auth",
                id="auth-beats-branch-apostrophe",
            ),
            pytest.param(
                b"fatal: a branch named 'feature-tls' already exists\nHTTP 401: authentication required",
                "github-auth",
                "auth",
                id="auth-beats-branch-tls-name",
            ),
            pytest.param(
                b"   ! [rejected] feature-tls -> origin/feature-tls (non-fast-forward)\nHTTP 401: authentication required",
                "github-auth",
                "auth",
                id="auth-beats-fetch-rejected",
            ),
            pytest.param(
                b"fatal: cannot lock ref 'refs/heads/feature'tls': HTTP 401: authentication required",
                "github-auth",
                "auth",
                id="auth-beats-ref-lock-suffix",
            ),
            pytest.param(
                b"fatal: cannot lock ref 'refs/heads/feature'\ncould not resolve host: github.com",
                "github-network",
                "dns",
                id="network-beats-ref-lock",
            ),
            pytest.param(
                b"fatal: 'feature'permission' is already checked out at '/private/checkout'\ncould not resolve host: github.com",
                "github-network",
                "dns",
                id="network-beats-worktree",
            ),
            pytest.param(
                b"error: Your local changes to the following files would be overwritten by checkout:\n\tpermission_notes.txt\ncould not resolve host: github.com",
                "github-network",
                "dns",
                id="network-beats-tracked-changes",
            ),
            pytest.param(
                b"fatal: cannot lock ref 'refs/heads/feature'permission': TLS handshake timeout",
                "github-network",
                "tls",
                id="network-beats-ref-lock-suffix",
            ),
            pytest.param(
                b"fatal: a branch named 'connection refused' already exists\nTLS handshake timeout",
                "github-network",
                "tls",
                id="network-reason-beats-quoted-keywords",
            ),
            pytest.param(
                b"fatal: cannot lock ref 'refs/heads/feature'tls': Permission denied",
                "github-permission",
                "permission",
                id="permission-ref-lock-suffix",
            ),
        ],
    )
    def test_run_preserves_safe_local_checkout_failure(
        self, reader: ModuleType, stderr: bytes, expected_class: str, expected_reason: str
    ) -> None:
        """Actionable checkout causes are exposed while sensitive CLI output is discarded.

        Quoted branch, ref and path names can contain words such as "tls" or "permission"; classification must key on
        the diagnostic's structure, and genuine auth, network or permission text must still win over local causes.
        """
        command = ["gh", "pr", "checkout", "https://github.com/example/project/pull/17"]
        secret = b"ghp_sensitive_sentinel_do_not_persist"
        raw_stderr = stderr + b"\nprivate diagnostic: " + secret
        runner = _fixed_runner(returncode=128, stdout=secret, stderr=raw_stderr)

        with pytest.raises(reader.GitHubReadError) as error:
            reader.run_gh_read(runner, command, timeout=5, label="local-pr-checkout")

        assert [call.args[0] for call in runner.call_args_list] == [command]
        assert str(error.value) == f"{expected_class}:local-pr-checkout"
        assert error.value.diagnostics == {
            "exit_code": 128,
            "failure_class": expected_class,
            "failure_reason": expected_reason,
            "label": "local-pr-checkout",
        }
        retained = str(error.value) + repr(vars(error.value))
        assert secret.decode() not in retained
        assert stderr.decode() not in retained
        assert raw_stderr.decode() not in retained


class TestCommandLine:
    """The `main` entry point: files written, streams used and exit codes."""

    PR_VIEW_URL = "https://github.com/example/project/pull/17"

    def test_job_log_writes_safe_text_without_printing_response(
        self, reader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Sanitized Unicode log text goes only to the requested file, never to the terminal.

        The subprocess is launched with piped streams and no shell, so the colored response cannot leak to stdout.
        """

        def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            """Supply a colored response through the captured subprocess boundary."""
            assert kwargs["stdout"] == subprocess.PIPE
            assert kwargs["stderr"] == subprocess.PIPE
            assert kwargs["shell"] is False
            return subprocess.CompletedProcess(
                command, 0, stdout="\x1b[31méchec\x1b[0m\n\u009b31m".encode(), stderr=b""
            )

        monkeypatch.setattr(reader.subprocess, "run", runner)
        output = tmp_path / "job.log"

        assert reader.main(["--out", str(output), "--", *JOB_LOG]) == 0

        assert output.read_bytes() == "échec\n\\u009b31m".encode()
        assert capsys.readouterr() == ("", "")

    def test_non_log_response_keeps_exact_bytes(self, reader: ModuleType) -> None:
        """Sanitization applies only to job logs: other API responses keep their control bytes."""
        payload = "\x1b[31méchec\x1b[0m\n\u009b31m".encode()

        result = reader.run_gh_read(
            _fixed_runner(stdout=payload), ["gh", "api", "/repos/example/project"], timeout=5, label="repo"
        )

        assert result == payload

    @pytest.mark.parametrize(
        ("stderr", "failure_class", "failure_reason"),
        [
            pytest.param(b"could not resolve host api.github.com", "github-network", "dns", id="dns"),
            pytest.param(b"HTTP 403: Resource not accessible", "github-permission", "permission", id="api-refusal"),
            pytest.param(
                b"GraphQL: `example-org` forbids access via a personal access token (classic). "
                b"Please use a GitHub App, OAuth App, or a personal access token with fine-grained permissions. "
                b"(repository)",
                "github-permission",
                "classic-token-forbidden",
                id="classic-token-policy",
            ),
        ],
    )
    def test_preserves_safe_failure_diagnostics(
        self,
        reader: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        stderr: bytes,
        failure_class: str,
        failure_reason: str,
    ) -> None:
        """The classified cause is exposed at the CLI without leaking output or writing a success file.

        The failing CLI emitted private text on both streams; the user sees only a class line plus a JSON diagnostic.
        """
        destination = tmp_path / "response.json"
        secret = b"private-diagnostic-token-do-not-retain"
        argv = ["gh", "pr", "view", self.PR_VIEW_URL, "--json", "url,state"]
        runner = _fixed_runner(returncode=1, stdout=secret, stderr=stderr + b"\n" + secret)
        monkeypatch.setattr(reader.subprocess, "run", runner)

        assert reader.main(["--out", str(destination), "--", *argv]) == 2

        captured = capsys.readouterr()
        lines = captured.err.splitlines()
        assert lines[0] == f"{failure_class}:github-read"
        assert len(lines) == 2
        assert json.loads(lines[1]) == {
            "exit_code": 1,
            "failure_class": failure_class,
            "failure_reason": failure_reason,
            "label": "github-read",
        }
        assert captured.out == ""
        assert secret.decode() not in captured.err
        assert stderr.decode() not in captured.err
        assert not destination.exists()
        assert [call.args[0] for call in runner.call_args_list] == [argv]

    def test_preserves_successful_response_bytes(
        self, reader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Successful metadata is written byte-exact, creating parent directories, with silent streams.

        Downstream collectors parse this file, so any re-encoding or trailing-newline change would corrupt it.
        """
        destination = tmp_path / "nested" / "response.json"
        payload = b'{"url":"https://github.com/example/project/pull/17","state":"OPEN"}\n'
        argv = ["gh", "pr", "view", self.PR_VIEW_URL, "--json", "url,state"]
        runner = _fixed_runner(stdout=payload)
        monkeypatch.setattr(reader.subprocess, "run", runner)

        assert reader.main(["--out", str(destination), "--", *argv]) == 0

        assert destination.read_bytes() == payload
        assert [call.args[0] for call in runner.call_args_list] == [argv]
        captured = capsys.readouterr()
        assert captured.out == captured.err == ""
