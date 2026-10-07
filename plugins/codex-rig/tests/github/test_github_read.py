"""Acceptance checks for the shared GitHub read-only transport boundary."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
MODULE = PLUGIN_ROOT / "shared" / "github_read.py"


def _load_reader() -> ModuleType:
    """Load the standalone GitHub reader without package installation."""
    assert MODULE.is_file(), MODULE
    specification = importlib.util.spec_from_file_location("codex_rig_github_read", MODULE)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["gh", "issue", "view", "17", "--json", "title"], id="issue-view"),
        pytest.param(["gh", "release", "view", "v1.2.3", "--json", "name"], id="release-view"),
        pytest.param(["gh", "repo", "view", "Borda/AI-Rig", "--json", "name"], id="repo-view"),
    ],
)
def test_run_gh_read_allows_view_commands(argv: list[str]) -> None:
    """Permit GitHub CLI view commands without widening mutating commands."""
    module = _load_reader()
    calls: list[list[str]] = []

    def _runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Record an allowed command and return an empty successful response."""
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"{}", stderr=b"")

    assert module.run_gh_read(_runner, argv, timeout=5, label="gh-view") == b"{}"
    assert calls == [argv]


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
    ],
)
def test_run_gh_read_rejects_non_read_only_commands(argv: list[str]) -> None:
    """Reject credential inspection, mutations, and browser-opening side effects."""
    module = _load_reader()
    calls: list[list[str]] = []

    def _runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Record any attempted command so rejected input can prove no execution."""
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    with pytest.raises(module.GitHubReadError, match="unsafe-gh-command:unsafe"):
        module.run_gh_read(_runner, argv, timeout=5, label="unsafe")

    assert calls == []


def test_run_gh_read_allows_graphql_query_but_not_mutation() -> None:
    """Permit a GraphQL query even though GitHub transports it with POST."""
    module = _load_reader()

    def _runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return a successful GraphQL response for the read-only query."""
        return subprocess.CompletedProcess(command, 0, stdout=b'{"data": {}}', stderr=b"")

    query = ["gh", "api", "graphql", "-f", "query=query { viewer { login } }"]
    assert module.run_gh_read(_runner, query, timeout=5, label="graphql") == b'{"data": {}}'


def test_default_gh_transport_rejects_oversized_output_without_returning_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep production CLI reads bounded before response bytes enter normal result handling."""
    module = _load_reader()
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 8)

    with pytest.raises(module.GitHubReadError, match="command-output-oversized"):
        module._run_default_gh_command([sys.executable, "-c", "import sys; sys.stdout.write('x' * 9)"], timeout=5)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["gh", "api", "/repos/Borda/AI-Rig/issues/17"], id="implicit-get"),
        pytest.param(["gh", "api", "/repos/Borda/AI-Rig/issues/17", "--method", "GET"], id="explicit-get"),
    ],
)
def test_run_gh_read_allows_rest_get_commands(argv: list[str]) -> None:
    """Allow REST GET requests through the shared read boundary."""
    module = _load_reader()

    def _runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return a successful REST response for the read-only request."""
        return subprocess.CompletedProcess(command, 0, stdout=b"{}", stderr=b"")

    assert module.run_gh_read(_runner, argv, timeout=5, label="rest-get") == b"{}"


def test_read_with_fallback_uses_public_https_get_after_eligible_network_failure() -> None:
    """Use the public API only after the preferred GitHub CLI has a classified network failure."""
    module = _load_reader()
    requested: list[tuple[str, str]] = []

    def _unavailable(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return a classified network failure that enables the public fallback."""
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"connection reset by peer")

    class Response(io.BytesIO):
        """Provide the context-manager protocol expected from urllib responses."""

        def __enter__(self) -> Response:
            """Return the response as a context-managed stream."""
            return self

        def __exit__(self, *args: object) -> None:
            """Close the response stream when the fallback request completes."""
            self.close()

    def _open_url(request: Any, *, timeout: int, context: Any) -> Response:
        """Record the public GET and return deterministic metadata bytes."""
        requested.append((request.full_url, request.get_method()))
        assert context is not None
        return Response(b'{"number": 17}')

    payload, transport = module.read_with_fallback(
        _unavailable,
        ["gh", "issue", "view", "17", "--json", "title"],
        timeout=5,
        label="gh-issue-view",
        fallback_url="https://api.github.com/repos/Borda/AI-Rig/issues/17",
        open_url=_open_url,
    )

    assert payload == b'{"number": 17}'
    assert transport == "public-https-fallback"
    assert requested == [("https://api.github.com/repos/Borda/AI-Rig/issues/17", "GET")]


def test_public_github_ssl_context_loads_system_bundle_when_default_store_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover HTTPS verification when Python has no configured default CA file."""
    module = _load_reader()
    system_bundle = tmp_path / "system-ca.pem"
    system_bundle.write_text("test CA bundle", encoding="utf-8")
    loaded_bundles: list[str] = []

    class EmptyTrustContext:
        """Model a Python installation whose default trust store is empty."""

        @staticmethod
        def cert_store_stats() -> dict[str, int]:
            """Report an empty trust store so fallback CA loading is exercised."""
            return {"x509_ca": 0}

        @staticmethod
        def load_verify_locations(*, cafile: str) -> None:
            """Record the fallback CA bundle selected by the SSL helper."""
            loaded_bundles.append(cafile)

    context = EmptyTrustContext()
    monkeypatch.setattr(module.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(module, "SYSTEM_CA_FILE_CANDIDATES", (system_bundle,))

    assert module._public_github_ssl_context() is context
    assert loaded_bundles == [str(system_bundle)]


def test_public_github_ssl_context_preserves_explicit_ca_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not widen an explicit caller-provided trust configuration."""
    module = _load_reader()
    system_bundle = tmp_path / "system-ca.pem"
    system_bundle.write_text("system CA bundle", encoding="utf-8")
    loaded_bundles: list[str] = []

    class EmptyTrustContext:
        """Model a trust store whose configured certificates load lazily."""

        @staticmethod
        def cert_store_stats() -> dict[str, int]:
            """Report an empty trust store while explicit configuration is present."""
            return {"x509_ca": 0}

        @staticmethod
        def load_verify_locations(*, cafile: str) -> None:
            """Record unexpected fallback CA loading for the explicit-configuration test."""
            loaded_bundles.append(cafile)

    context = EmptyTrustContext()
    monkeypatch.setattr(module.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(module, "SYSTEM_CA_FILE_CANDIDATES", (system_bundle,))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "explicit-ca.pem"))

    assert module._public_github_ssl_context() is context
    assert loaded_bundles == []


def test_read_with_fallback_keeps_command_unavailable_fail_closed() -> None:
    """Do not treat a missing local GitHub CLI as proof that public PR data is safe to collect."""
    module = _load_reader()
    requested: list[object] = []

    def _unavailable(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Raise a local command-unavailable error without producing a process result."""
        raise OSError("gh not installed")

    def _open_url(*args: Any, **kwargs: Any) -> None:
        """Fail if command-unavailable evidence activates public fallback."""
        requested.append((args, kwargs))
        raise AssertionError("command-unavailable must not activate public HTTPS fallback")

    with pytest.raises(module.GitHubReadError, match="command-unavailable:gh-issue-view") as error:
        module.read_with_fallback(
            _unavailable,
            ["gh", "issue", "view", "17", "--json", "title"],
            timeout=5,
            label="gh-issue-view",
            fallback_url="https://api.github.com/repos/Borda/AI-Rig/issues/17",
            open_url=_open_url,
        )

    assert error.value.diagnostics == {
        "failure_class": "command-unavailable",
        "failure_reason": "unavailable",
        "label": "gh-issue-view",
    }
    assert requested == []


def test_read_with_fallback_rejects_non_github_url() -> None:
    """Keep fallback requests limited to the public GitHub API host."""
    module = _load_reader()

    def _unavailable(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return a network failure so the unsafe fallback URL is checked."""
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"connection reset by peer")

    with pytest.raises(module.GitHubReadError, match="unsafe-github-fallback-url:gh-issue-view"):
        module.read_with_fallback(
            _unavailable,
            ["gh", "issue", "view", "17", "--json", "title"],
            timeout=5,
            label="gh-issue-view",
            fallback_url="https://example.invalid/metadata",
        )


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["gh", "evil", "view", "17"], id="unknown-group"),
        pytest.param(["gh", "auth", "view"], id="credential-group"),
        pytest.param(["gh", "api", "graphql", "-F", "query=@/private/secret"], id="graphql-file-field"),
        pytest.param(["gh", "api", "/repos/Borda/AI-Rig/issues", "-F", "body=@/private/secret"], id="rest-file-field"),
    ],
)
def test_run_gh_read_rejects_extensions_and_file_backed_fields(argv: list[str]) -> None:
    """Prevent extensions or field expansion from escaping the read-only boundary."""
    module = _load_reader()

    def _runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Fail immediately if an unsafe GitHub command reaches the runner."""
        raise AssertionError("unsafe command must not run")

    with pytest.raises(module.GitHubReadError, match="unsafe-gh-command:unsafe"):
        module.run_gh_read(_runner, argv, timeout=5, label="unsafe")


@pytest.mark.parametrize(
    "fallback_error",
    [
        pytest.param(OSError("network unavailable secret-token"), id="socket-error"),
        pytest.param(URLError("DNS failure secret-token"), id="url-error"),
    ],
)
def test_failed_public_fallback_preserves_primary_network_diagnostic(fallback_error: OSError) -> None:
    """Keep the actionable CLI reason when fallback fails in the same network category."""
    module = _load_reader()

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Simulate a DNS failure from the credential broker."""
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"could not resolve host api.github.com")

    def offline(*args: Any, **kwargs: Any) -> None:
        """Fail the independent HTTPS transport without making a network request."""
        raise fallback_error

    with pytest.raises(module.GitHubReadError, match="github-network:gh-pr-view") as error:
        module.read_with_fallback(
            runner,
            ["gh", "pr", "view", "7"],
            timeout=5,
            label="gh-pr-view",
            fallback_url="https://api.github.com/repos/example/project/pulls/7",
            open_url=offline,
        )

    assert error.value.diagnostics == {
        "exit_code": 1,
        "failure_class": "github-network",
        "failure_reason": "dns",
        "label": "gh-pr-view",
    }
    assert "secret-token" not in str(error.value)


def test_public_fallback_keeps_distinct_http_failure_authoritative() -> None:
    """Do not replace a definitive fallback access failure with the earlier network error."""
    module = _load_reader()

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Simulate the primary network failure."""
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"connection reset by peer")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        """Return a forbidden response without exposing its body."""
        raise HTTPError("https://api.github.com", 403, "secret-token", None, None)

    with pytest.raises(module.GitHubReadError, match="github-permission:gh-pr-view") as error:
        module.read_with_fallback(
            runner,
            ["gh", "pr", "view", "7"],
            timeout=5,
            label="gh-pr-view",
            fallback_url="https://api.github.com/repos/example/project/pulls/7",
            open_url=forbidden,
        )
    assert error.value.diagnostics is None
    assert "secret-token" not in str(error.value)


def test_public_fallback_rejects_tokenized_url_and_normalizes_transport_error() -> None:
    """Keep fallback unauthenticated and never surface transport detail in its failure."""
    module = _load_reader()

    def _unavailable(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return a network failure for tokenized-URL fallback tests."""
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"connection reset by peer")

    with pytest.raises(module.GitHubReadError, match="unsafe-github-fallback-url:gh-issue-view"):
        module.read_with_fallback(
            _unavailable,
            ["gh", "issue", "view", "17"],
            timeout=5,
            label="gh-issue-view",
            fallback_url="https://api.github.com/repos/Borda/AI-Rig/issues/17?access_token=secret",
        )

    def _offline(*args: Any, **kwargs: Any) -> None:
        """Raise a token-bearing transport error for sanitization coverage."""
        raise OSError("https://api.github.com/repos/Borda/AI-Rig/issues/17?token=secret")

    with pytest.raises(module.GitHubReadError, match="github-network:gh-issue-view") as error:
        module.read_with_fallback(
            _unavailable,
            ["gh", "issue", "view", "17"],
            timeout=5,
            label="gh-issue-view",
            fallback_url="https://api.github.com/repos/Borda/AI-Rig/issues/17",
            open_url=_offline,
        )

    assert "token" not in str(error.value)


@pytest.mark.parametrize(
    "stderr",
    [
        b"could not resolve host: api.github.com",
        b"dial tcp: lookup api.github.com: no such host",
        b"temporary failure in name resolution",
    ],
)
def test_dns_failures_enable_public_last_resort_transport(stderr: bytes) -> None:
    """Classify common GitHub CLI DNS failures as network and use public fallback."""
    module = _load_reader()

    def _unavailable(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return the parameterized DNS or transport failure classification input."""
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=stderr)

    class Response(io.BytesIO):
        """Provide the context-manager protocol expected from urllib responses."""

        def __enter__(self) -> Response:
            """Return the response as a context-managed stream."""
            return self

        def __exit__(self, *args: object) -> None:
            """Close the response stream after consuming fallback metadata."""
            self.close()

    def _open_url(request: Any, *, timeout: int, context: Any) -> Response:
        """Assert the fallback uses GET with an explicit SSL context."""
        assert request.get_method() == "GET"
        assert context is not None
        return Response(b'{"number": 17}')

    payload, transport = module.read_with_fallback(
        _unavailable,
        ["gh", "issue", "view", "17"],
        timeout=5,
        label="gh-issue-view",
        fallback_url="https://api.github.com/repos/Borda/AI-Rig/issues/17",
        open_url=_open_url,
    )

    assert module.github_failure_class(stderr) == "github-network"
    assert payload == b'{"number": 17}'
    assert transport == "public-https-fallback"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        pytest.param(b"run gh auth login to authenticate", "github-auth", id="b-run-gh-auth-login-to-authenticate"),
        pytest.param(b"HTTP 401: authentication required", "github-auth", id="b-http-401-authentication-required"),
        pytest.param(b"request requires authentication", "github-auth", id="b-request-requires-authentication"),
        pytest.param(b"context deadline exceeded", "github-network", id="b-context-deadline-exceeded"),
        pytest.param(b"read: connection reset by peer", "github-network", id="b-read-connection-reset-by-peer"),
        pytest.param(
            b"failed to connect to api.github.com port 443",
            "github-network",
            id="b-failed-to-connect-to-api.github.com-port-443",
        ),
        pytest.param(
            b"Client.Timeout exceeded while awaiting headers",
            "github-network",
            id="b-client.timeout-exceeded-while-awaiting-headers",
        ),
        pytest.param(b"oauth token has expired", "github-auth", id="b-oauth-token-has-expired"),
    ],
)
def test_gh_failure_classifies_common_auth_and_transport_diagnostics(stderr: bytes, expected: str) -> None:
    """Route opaque CLI diagnostics to the safe recovery category without retaining them."""
    module = _load_reader()

    assert module.github_failure_class(stderr) == expected


@pytest.mark.parametrize("object_type", ["PullRequest", "Repository"])
def test_gh_failure_classifies_missing_graphql_object_as_not_found(object_type: str) -> None:
    """Do not mistake GitHub GraphQL object resolution for DNS resolution."""
    module = _load_reader()
    stderr = f"GraphQL: Could not resolve to a {object_type} with the supplied identity.".encode()

    assert module.github_failure_class(stderr) == "github-not-found"
    assert module.github_failure_reason(stderr, "github-not-found") == "not-found"


@pytest.mark.parametrize(
    ("stderr", "expected_reason"),
    [
        pytest.param(b"could not resolve host: api.github.com", "dns", id="dns"),
        pytest.param(b"read: connection reset by peer", "connection-reset", id="connection-reset"),
    ],
)
def test_run_gh_read_persists_safe_network_reason_without_stderr(stderr: bytes, expected_reason: str) -> None:
    """Keep retry routing specific without storing raw network diagnostics or credentials."""
    module = _load_reader()

    def _runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return the parameterized network failure without exposing stderr elsewhere."""
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=stderr)

    with pytest.raises(module.GitHubReadError, match="github-network:gh-issue-view") as error:
        module.run_gh_read(
            _runner,
            ["gh", "issue", "view", "17", "--json", "title"],
            timeout=5,
            label="gh-issue-view",
        )

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
            id="branch-tls-data",
        ),
        pytest.param(
            b"fatal: a branch named 'feature'tls' already exists",
            "github-command-failed",
            "local-branch-exists",
            id="branch-apostrophe-data",
        ),
        pytest.param(
            b"fatal: 'feature'tls' is already checked out at '/private/permission'checkout'",
            "github-command-failed",
            "local-branch-in-other-worktree",
            id="worktree-apostrophe-data",
        ),
        pytest.param(
            b"fatal: cannot lock ref 'refs/heads/feature'tls': is at abc but expected def",
            "github-command-failed",
            "local-ref-lock-failed",
            id="ref-apostrophe-data",
        ),
        pytest.param(
            b"fatal: a branch named 'feature'tls' already exists\nHTTP 401: authentication required",
            "github-auth",
            "auth",
            id="branch-apostrophe-auth-precedence",
        ),
        pytest.param(
            b"fatal: 'feature'permission' is already checked out at '/private/checkout'\ncould not resolve host: github.com",
            "github-network",
            "dns",
            id="worktree-apostrophe-network-precedence",
        ),
        pytest.param(
            b"fatal: cannot lock ref 'refs/heads/feature'tls': Permission denied",
            "github-permission",
            "permission",
            id="ref-apostrophe-permission-suffix",
        ),
        pytest.param(
            b"fatal: cannot lock ref 'refs/heads/feature'tls': HTTP 401: authentication required",
            "github-auth",
            "auth",
            id="ref-apostrophe-auth-suffix",
        ),
        pytest.param(
            b"fatal: cannot lock ref 'refs/heads/feature'permission': TLS handshake timeout",
            "github-network",
            "tls",
            id="ref-apostrophe-network-suffix",
        ),
        pytest.param(
            b"fatal: a branch named 'permission-fix' already exists",
            "github-command-failed",
            "local-branch-exists",
            id="branch-permission-data",
        ),
        pytest.param(
            b"fatal: unknown diagnostic for 'tls permission HTTP 401 connection refused'",
            "github-command-failed",
            "unclassified",
            id="unknown-quoted-data",
        ),
        pytest.param(
            b'error: unknown diagnostic for "tls permission HTTP 401 connection refused"',
            "github-command-failed",
            "unclassified",
            id="unknown-double-quoted-data",
        ),
        pytest.param(
            b"fatal: a branch named 'feature-tls' already exists\nHTTP 401: authentication required",
            "github-auth",
            "auth",
            id="quoted-data-auth-precedence",
        ),
        pytest.param(
            b"fatal: a branch named 'connection refused' already exists\nTLS handshake timeout",
            "github-network",
            "tls",
            id="quoted-data-network-reason",
        ),
        pytest.param(
            b"fatal: 'feature' is already checked out at '/private/checkout'",
            "github-command-failed",
            "local-branch-in-other-worktree",
            id="other-worktree",
        ),
        pytest.param(
            b"error: Your local changes to the following files would be overwritten by checkout:\n\tfile.py",
            "github-command-failed",
            "local-changes-overwritten",
            id="tracked-changes",
        ),
        pytest.param(
            b"error: The following untracked working tree files would be overwritten by checkout:\n\tfile.py",
            "github-command-failed",
            "local-untracked-files-overwritten",
            id="untracked-files",
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
            id="fetch-ref-tls-data",
        ),
        pytest.param(
            b"error: Your local changes to the following files would be overwritten by checkout:\n\tpermission_notes.txt",
            "github-command-failed",
            "local-changes-overwritten",
            id="tracked-path-permission-data",
        ),
        pytest.param(
            b"error: The following untracked working tree files would be overwritten by checkout:\n\ttls_notes.txt",
            "github-command-failed",
            "local-untracked-files-overwritten",
            id="untracked-path-tls-data",
        ),
        pytest.param(
            b"   ! [rejected] feature-tls -> origin/feature-tls (non-fast-forward)\nHTTP 401: authentication required",
            "github-auth",
            "auth",
            id="fetch-ref-data-auth-precedence",
        ),
        pytest.param(
            b"error: Your local changes to the following files would be overwritten by checkout:\n\tpermission_notes.txt\ncould not resolve host: github.com",
            "github-network",
            "dns",
            id="tracked-path-data-network-precedence",
        ),
        pytest.param(
            b"fatal: cannot lock ref 'refs/heads/feature': is at abc but expected def",
            "github-command-failed",
            "local-ref-lock-failed",
            id="ref-lock",
        ),
        pytest.param(b"checkout failed for an unknown reason", "github-command-failed", "unclassified", id="unknown"),
        pytest.param(
            b"note: a branch already exists somewhere",
            "github-command-failed",
            "unclassified",
            id="unrelated-branch-text",
        ),
        pytest.param(
            b"fatal: a branch named 'feature' already exists\nHTTP 401: authentication required",
            "github-auth",
            "auth",
            id="auth-precedence",
        ),
        pytest.param(
            b"fatal: cannot lock ref 'refs/heads/feature'\ncould not resolve host: github.com",
            "github-network",
            "dns",
            id="network-precedence",
        ),
    ],
)
def test_run_gh_read_preserves_safe_local_checkout_failure(
    stderr: bytes, expected_class: str, expected_reason: str
) -> None:
    """Expose actionable checkout causes while discarding sensitive CLI output."""
    module = _load_reader()
    command = ["gh", "pr", "checkout", "https://github.com/example/project/pull/17"]
    calls: list[list[str]] = []
    secret = b"ghp_sensitive_sentinel_do_not_persist"
    raw_stderr = stderr + b"\nprivate diagnostic: " + secret

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return a synthetic failure at the external CLI boundary."""
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 128, stdout=secret, stderr=raw_stderr)

    with pytest.raises(module.GitHubReadError) as error:
        module.run_gh_read(_runner, command, timeout=5, label="local-pr-checkout")

    assert calls == [command]
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


def test_read_with_fallback_preserves_gh_permission_failure() -> None:
    """Do not bypass an authenticated GitHub permission decision with public HTTPS."""
    module = _load_reader()
    calls: list[object] = []

    def _denied(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return an authenticated permission failure that must not be retried publicly."""
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"resource not accessible")

    def _open_url(*args: Any, **kwargs: Any) -> None:
        """Fail if a permission failure activates public fallback."""
        calls.append((args, kwargs))
        raise AssertionError("public fallback must not run after a permission failure")

    with pytest.raises(module.GitHubReadError, match="github-permission:gh-issue-view"):
        module.read_with_fallback(
            _denied,
            ["gh", "issue", "view", "17", "--json", "title"],
            timeout=5,
            label="gh-issue-view",
            fallback_url="https://api.github.com/repos/Borda/AI-Rig/issues/17",
            open_url=_open_url,
        )

    assert calls == []
