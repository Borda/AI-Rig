#!/usr/bin/env python3
"""Read GitHub data through one gh-first, credential-opaque transport boundary.

## Purpose

Centralize strict GitHub read-only command validation, safe failure classification, and a final unauthenticated public
REST fallback. A single boundary lets collectors share the same no-mutation and credential-opaque guarantees instead of
implementing separate command filters.

## Scope

Permit audited built-in views, REST GET, and GraphQL queries only; never inspect credentials, call ``gh auth``, or
permit remote mutation. The fallback is restricted to public
``https://api.github.com``
endpoints and is considered
only for classified authentication, network, or rate-limit failures.

## Usage

Invoke ``python github_read.py --out <file> -- gh <allowed-read-command>`` or import its validators from a
resource-specific collector. A caller may supply ``--fallback-url`` for a public REST GET, but the primary command
still has to pass read-only validation before execution.

## Used by

PR collection, issue/release/repository research instructions, and GitHub-boundary regression tests use this boundary;
Discussions use an explicit GraphQL query. Resource-specific code receives response bytes and does not need to handle
credentials or transport fallback policy itself.

## Outputs

Write requested response bytes on success or emit only a classified, credential-opaque failure with command label and
exit metadata. The CLI writes the successful bytes exactly to ``--out`` and reports failure codes on stderr without
including tokens or command output that could expose credentials.

## Failure

Unsafe argv, mutation-like input, browser flag, auth/network/rate-limit error, private fallback request, or malformed
public URL fails closed. Read-command failures are surfaced as ``GitHubReadError`` classifications, and the CLI returns
``2`` so collectors can record terminal evidence rather than treating an unsafe request as an empty response.
"""

from __future__ import annotations

import argparse
import os
import re
import ssl
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlparse
from urllib.request import Request, urlopen


MAX_OUTPUT_BYTES = 16 * 1024 * 1024
PUBLIC_GITHUB_API_HOST = "api.github.com"
VIEW_RESOURCE_COMMANDS = frozenset({"gist", "issue", "pr", "project", "release", "repo", "ruleset", "run", "workflow"})
SENSITIVE_QUERY_KEYS = frozenset({"access_token", "auth", "authorization", "client_secret", "password", "token"})
SYSTEM_CA_FILE_CANDIDATES = (
    Path("/etc/ssl/cert.pem"),
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
)
RunCommand = Callable[..., subprocess.CompletedProcess[bytes]]
OpenUrl = Callable[..., Any]
FallbackUrl = str | Callable[[], str | None]
DEFAULT_RUN_COMMAND = subprocess.run


class GitHubReadError(RuntimeError):
    """Carry a stable credential-opaque GitHub read failure."""

    def __init__(self, code: str, *, diagnostics: dict[str, object] | None = None) -> None:
        """Initialize a failure with safe classifier metadata only."""
        super().__init__(code)
        self.diagnostics = diagnostics


def _has_forbidden_view_flag(arguments: list[str]) -> bool:
    """Return whether a nominal view command would open a browser instead of reading data."""
    return any(argument in {"--web", "-w"} or argument.startswith(("--web=", "-w=")) for argument in arguments)


def _is_read_only_graphql_query(arguments: list[str]) -> bool:
    """Return whether GraphQL arguments contain one explicit query and no mutation payload."""
    fields: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        field_flag = arguments[index]
        if field_flag not in {"-f", "-F"} or index + 1 >= len(arguments):
            return False
        key, separator, value = arguments[index + 1].partition("=")
        if not separator or not key or key in fields or (field_flag == "-F" and value.startswith("@")):
            return False
        fields[key] = value
        index += 2
    query = fields.get("query", "").lstrip()
    return query.startswith("query") and "mutation" not in query.casefold()


def _is_read_only_rest_api(arguments: list[str]) -> bool:
    """Return whether REST API argv can only issue an HTTP GET request."""
    if not arguments or not arguments[0].startswith("/"):
        return False
    explicit_get = False
    has_fields = False
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--paginate", "--slurp", "--include"}:
            index += 1
            continue
        if argument in {"--method", "-X"}:
            if index + 1 >= len(arguments) or arguments[index + 1].upper() != "GET":
                return False
            explicit_get = True
            index += 2
            continue
        if argument == "--cache":
            if index + 1 >= len(arguments):
                return False
            index += 2
            continue
        if argument in {"-f", "-F"}:
            if index + 1 >= len(arguments) or "=" not in arguments[index + 1]:
                return False
            _, _, value = arguments[index + 1].partition("=")
            if argument == "-F" and value.startswith("@"):
                return False
            has_fields = True
            index += 2
            continue
        return False
    return explicit_get or not has_fields


def is_read_only_gh_command(argv: list[str]) -> bool:
    """Return whether argv is an allowlisted GitHub read or local PR checkout command."""
    if len(argv) < 3 or argv[0] != "gh":
        return False
    if argv[1] in VIEW_RESOURCE_COMMANDS and argv[2] == "view":
        return not _has_forbidden_view_flag(argv[3:])
    if argv[:3] == ["gh", "pr", "diff"]:
        return len(argv) <= 4 and (len(argv) == 3 or not argv[3].startswith("-"))
    if argv[:3] == ["gh", "pr", "checkout"]:
        return len(argv) == 4 and not argv[3].startswith("-")
    if argv[:3] != ["gh", "api", "graphql"]:
        return _is_read_only_rest_api(argv[2:]) if argv[:2] == ["gh", "api"] else False
    return _is_read_only_graphql_query(argv[3:])


def require_read_only_gh_command(argv: list[str], label: str) -> None:
    """Reject every GitHub CLI command outside the read-only allowlist."""
    if is_read_only_gh_command(argv):
        return
    raise GitHubReadError(
        f"unsafe-gh-command:{label}",
        diagnostics={"failure_class": "unsafe-gh-command", "label": label},
    )


def _diagnostic_message_text(stderr: bytes) -> str:
    """Exclude Git object payloads from diagnostic category matching."""
    text = stderr.decode("utf-8", errors="replace").casefold()
    messages: list[str] = []
    in_file_list = False
    for line in text.splitlines():
        # Complete Git messages delimit payloads even when names contain apostrophes.
        if re.fullmatch(r"fatal: a branch named '[^\n]+' already exists", line) or re.fullmatch(
            r"fatal: '[^\n]+' is already checked out at '[^\n]+'", line
        ):
            in_file_list = False
            continue
        # Git prints overwrite paths as tab-indented data after these exact headers.
        if in_file_list and line.startswith("\t"):
            continue
        in_file_list = bool(
            re.fullmatch(
                r"error: (?:your local changes to the following files|the following untracked working tree files) "
                r"would be overwritten by (?:checkout|merge):",
                line,
            )
        )
        if re.fullmatch(r"\s*! \[rejected\]\s+\S+ -> \S+ \(non-fast-forward\)", line):
            continue
        # A valid ref cannot contain a colon; retain the suffix explaining lock failure.
        line = re.sub(r"^(?:fatal|error): cannot lock ref '[^:\n]+':", "", line, count=1)
        messages.append(re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "", line))
    return "\n".join(messages)


def github_failure_class(stderr: bytes) -> str:
    """Classify a GitHub CLI failure without persisting its output."""
    text = _diagnostic_message_text(stderr)
    if "graphql:" in text and "could not resolve to a " in text:
        return "github-not-found"
    if any(
        token in text
        for token in (
            "could not resolve host",
            "dial tcp",
            "error connecting",
            "name resolution",
            "network is unreachable",
            "no such host",
            "connection refused",
            "connection reset",
            "deadline exceeded",
            "i/o timeout",
            "failed to connect",
            "client.timeout exceeded",
            "tls",
        )
    ):
        return "github-network"
    if "rate limit" in text or "http 429" in text:
        return "github-rate-limit"
    if any(
        token in text
        for token in (
            "bad credentials",
            "invalid token",
            "not logged",
            "authentication failed",
            "authentication required",
            "requires authentication",
            "gh auth login",
            "http 401",
            "token has expired",
            "oauth token has expired",
        )
    ):
        return "github-auth"
    if any(token in text for token in ("not found", "http 404")):
        return "github-not-found"
    if any(token in text for token in ("resource not accessible", "permission", "http 403")):
        return "github-permission"
    return "github-command-failed"


def github_failure_reason(stderr: bytes, failure_class: str) -> str:
    """Return a safe actionable subtype without retaining GitHub CLI stderr."""
    text = stderr.decode("utf-8", errors="replace").casefold()
    if failure_class == "github-network":
        text = _diagnostic_message_text(stderr)
        if any(token in text for token in ("could not resolve host", "name resolution", "no such host")):
            return "dns"
        if "connection refused" in text:
            return "connection-refused"
        if "connection reset" in text:
            return "connection-reset"
        if any(token in text for token in ("deadline exceeded", "i/o timeout", "client.timeout exceeded")):
            return "timeout"
        if "tls" in text:
            return "tls"
        return "network"
    if failure_class == "github-rate-limit":
        return "rate-limit"
    if failure_class == "github-auth":
        return "auth"
    if failure_class == "github-permission":
        return "permission"
    if failure_class == "github-not-found":
        return "not-found"
    if failure_class == "github-command-failed":
        # Match Git's local diagnostic grammar, never branch names or arbitrary prose alone.
        local_reasons = (
            (r"^fatal: a branch named '[^\n]+' already exists$", "local-branch-exists"),
            (r"^fatal: '[^\n]+' is already checked out at '[^\n]+'$", "local-branch-in-other-worktree"),
            (
                r"^error: your local changes to the following files would be overwritten by (?:checkout|merge):$",
                "local-changes-overwritten",
            ),
            (
                r"^error: the following untracked working tree files would be overwritten by (?:checkout|merge):$",
                "local-untracked-files-overwritten",
            ),
            (r"^fatal: not possible to fast-forward, aborting\.$", "local-fast-forward-unavailable"),
            (r"^\s*! \[rejected\]\s+\S+ -> \S+ \(non-fast-forward\)$", "local-fetch-non-fast-forward"),
            (r"^(?:fatal|error): cannot lock ref '[^\n]+':", "local-ref-lock-failed"),
        )
        for pattern, reason in local_reasons:
            if re.search(pattern, text, flags=re.MULTILINE):
                return reason
    return "unclassified"


def _run_default_gh_command(argv: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    """Run the production GitHub CLI path without buffering arbitrary output in memory."""
    with (
        tempfile.SpooledTemporaryFile(max_size=MAX_OUTPUT_BYTES, mode="w+b") as stdout_buffer,
        tempfile.SpooledTemporaryFile(max_size=MAX_OUTPUT_BYTES, mode="w+b") as stderr_buffer,
    ):
        completed = DEFAULT_RUN_COMMAND(
            argv,
            stdout=stdout_buffer,
            stderr=stderr_buffer,
            check=False,
            shell=False,
            timeout=timeout,
        )
        stdout_size = stdout_buffer.tell()
        stderr_size = stderr_buffer.tell()
        if stdout_size > MAX_OUTPUT_BYTES or stderr_size > MAX_OUTPUT_BYTES:
            raise GitHubReadError("command-output-oversized")
        stdout_buffer.seek(0)
        stderr_buffer.seek(0)
        return subprocess.CompletedProcess(argv, completed.returncode, stdout_buffer.read(), stderr_buffer.read())


def run_gh_read(run: RunCommand, argv: list[str], *, timeout: int, label: str) -> bytes:
    """Run one validated GitHub CLI read without exposing credential diagnostics."""
    require_read_only_gh_command(argv, label)
    try:
        completed = (
            _run_default_gh_command(argv, timeout)
            if run is DEFAULT_RUN_COMMAND
            else run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
                timeout=timeout,
            )
        )
    except subprocess.TimeoutExpired as error:
        raise GitHubReadError(
            f"command-timeout:{label}",
            diagnostics={"failure_class": "command-timeout", "failure_reason": "timeout", "label": label},
        ) from error
    except OSError as error:
        raise GitHubReadError(
            f"command-unavailable:{label}",
            diagnostics={"failure_class": "command-unavailable", "failure_reason": "unavailable", "label": label},
        ) from error
    except GitHubReadError as error:
        if str(error) == "command-output-oversized":
            raise GitHubReadError(f"command-output-oversized:{label}") from error
        raise
    if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
        raise GitHubReadError(f"command-output-oversized:{label}")
    if completed.returncode != 0:
        failure_class = github_failure_class(completed.stderr)
        raise GitHubReadError(
            f"{failure_class}:{label}",
            diagnostics={
                "exit_code": completed.returncode,
                "failure_class": failure_class,
                "failure_reason": github_failure_reason(completed.stderr, failure_class),
                "label": label,
            },
        )
    return completed.stdout


def _is_public_github_api_url(url: str) -> bool:
    """Return whether url is a safe unauthenticated GitHub REST endpoint."""
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == PUBLIC_GITHUB_API_HOST
        and port is None
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and parsed.path.startswith("/repos/")
        and not any(
            key.casefold() in SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
    )


def _public_github_ssl_context() -> ssl.SSLContext:
    """Build an HTTPS context that recovers an omitted system CA bundle."""
    context = ssl.create_default_context()
    if (
        context.cert_store_stats().get("x509_ca", 0)
        or os.environ.get("SSL_CERT_FILE")
        or os.environ.get("SSL_CERT_DIR")
    ):
        return context
    # Python framework installs may omit their OpenSSL CA symlink even when the OS bundle exists.
    for ca_file in SYSTEM_CA_FILE_CANDIDATES:
        if ca_file.is_file():
            context.load_verify_locations(cafile=str(ca_file))
            break
    return context


def public_github_get(url: str, *, timeout: int, label: str, open_url: OpenUrl = urlopen) -> bytes:
    """Read one public GitHub REST resource through unauthenticated HTTPS GET."""
    if not _is_public_github_api_url(url):
        raise GitHubReadError(f"unsafe-github-fallback-url:{label}")
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "codex-rig-read"})
    try:
        with open_url(request, timeout=timeout, context=_public_github_ssl_context()) as response:
            payload = response.read(MAX_OUTPUT_BYTES + 1)
    except HTTPError as error:
        if error.code == 404:
            failure_class = "github-not-found"
        elif error.code == 403:
            failure_class = "github-permission"
        elif error.code == 429:
            failure_class = "github-rate-limit"
        else:
            failure_class = "github-http-failed"
        raise GitHubReadError(f"{failure_class}:{label}") from error
    except URLError as error:
        raise GitHubReadError(f"github-network:{label}") from error
    except OSError as error:
        raise GitHubReadError(f"github-network:{label}") from error
    if len(payload) > MAX_OUTPUT_BYTES:
        raise GitHubReadError(f"command-output-oversized:{label}")
    return payload


def _may_use_public_fallback(error: GitHubReadError) -> bool:
    """Return whether gh is unavailable to obtain public data through its primary route."""
    return str(error).split(":", maxsplit=1)[0] in {
        "command-timeout",
        "github-auth",
        "github-network",
        "github-rate-limit",
    }


def read_with_fallback(
    run: RunCommand,
    argv: list[str],
    *,
    timeout: int,
    label: str,
    fallback_url: FallbackUrl | None = None,
    open_url: OpenUrl = urlopen,
) -> tuple[bytes, str]:
    """Prefer authenticated gh, retaining safe diagnostics when public fallback fails identically."""
    try:
        return run_gh_read(run, argv, timeout=timeout, label=label), "gh"
    except GitHubReadError as error:
        if not _may_use_public_fallback(error):
            raise
        resolved_fallback_url = fallback_url() if callable(fallback_url) else fallback_url
        if resolved_fallback_url is None:
            raise
        try:
            return (
                public_github_get(resolved_fallback_url, timeout=timeout, label=label, open_url=open_url),
                "public-https-fallback",
            )
        except GitHubReadError as fallback_error:
            # Keep the classified CLI reason without masking a different fallback outcome.
            if str(fallback_error) == str(error) and error.diagnostics:
                raise error from fallback_error
            raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse one generic GitHub read command and optional public fallback URL."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="File receiving successful response bytes")
    parser.add_argument("--fallback-url", help="Optional public https://api.github.com/repos/... GET endpoint")
    parser.add_argument("--timeout-seconds", default=60, type=int, help="Per-transport timeout")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Read-only gh argv, after --")
    arguments = parser.parse_args(argv)
    if arguments.timeout_seconds < 1:
        parser.error("--timeout-seconds must be positive")
    if arguments.command[:1] == ["--"]:
        arguments.command = arguments.command[1:]
    if not arguments.command:
        parser.error("a gh command after -- is required")
    return arguments


def main(argv: list[str] | None = None) -> int:
    """Write one safe GitHub read response and report only stable failure codes."""
    arguments = parse_args(argv)
    try:
        payload, _transport = read_with_fallback(
            subprocess.run,
            arguments.command,
            timeout=arguments.timeout_seconds,
            label="github-read",
            fallback_url=arguments.fallback_url,
        )
    except GitHubReadError as error:
        print(error, file=sys.stderr)
        return 2
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
