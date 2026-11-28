#!/usr/bin/env python3
"""Select the local Git remote matching an authoritative repository URL.

## Purpose

Prevent PR workflows from fetching or checking out against a similarly named fork instead of the PR's actual base
repository. Selection is based on normalized host and owner/repository identity, not on the conventional name ``origin``
alone.

## Scope

It parses local remote URLs and returns a deterministic match; it does not alter remotes, fetch refs, or call GitHub.
SSH/scp-style, HTTPS, repository, and pull-request URLs are reduced to the same two-part identity, while malformed URLs
are retained in the rejected-candidate details.

## Usage

Run ``python select-git-remote.py --expected-url <url> --cwd <repository>`` through ``collect_pr.py`` with the canonical
PR URL and local repository context. Use ``--identity-only`` when only normalized base-repository identity is needed and
no local Git remote lookup should occur. Before the first PR collector call for a numeric user target, run ``python
select-git-remote.py --canonical-pr-url <positive-number> --cwd <repository>`` to bind a URL approval rule.

## Used by

Pull-request evidence collection and remote-selection acceptance tests use this selector. ``collect_pr.py`` consumes the
chosen remote before recording target and head checkout evidence, so the selection is part of the PR identity chain.

## Outputs

It emits a JSON-compatible selected remote record including the expected identity and all local candidates considered.
When multiple exact matches exist, ``origin`` wins the deterministic ordering; the payload still lists every matching
name and URL for auditability. Numeric PR resolution prefers a valid ``origin`` repository over other configured forks,
then uses a sole configured GitHub repository when ``origin`` is absent. It emits only a canonical URL on stdout, so the
caller can use identical URL arguments in the actual collector command and its proposed host approval prefix.

## Failure

Malformed authoritative URL or no exact match exits non-zero so PR collection does not use a guessed fork. Multiple
exact matches are not treated as an error: the deterministic selector prefers ``origin`` and reports every matching
candidate for auditability. Numeric PR resolution exits non-zero when ``origin`` has no unique valid GitHub identity.
Without ``origin``, conflicting configured GitHub repositories also fail. It never contacts GitHub or mutates remotes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

# Load the packaged reader when the selector is invoked directly or imported by path.
sys.pycache_prefix = str(Path(__file__).resolve())
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect_pr import _github_remote_identity, _tracking_remote_urls  # noqa: E402

#: Local selection supports literal GitHub names with leading punctuation, including organization .github repositories.
LITERAL_NAME = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """Represent a normalized Git host and owner/repository path."""

    host: str
    repository: str


def parse_repository_url(raw_url: str, *, configured_remote: bool = False) -> RepositoryIdentity:
    """Parse a target URL or identify a configured fetch URL by its complete repository path."""
    if not raw_url.isprintable() or any(char.isspace() for char in raw_url):
        raise ValueError("unrecognized-repository-url")
    scp_match = re.fullmatch(r"(?:[^@]+@)?([^:]+):(.+)", raw_url)
    if scp_match and "://" not in raw_url:
        host, path = scp_match.groups()
    else:
        parsed = urlparse(raw_url)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        path = parsed.path
    if configured_remote and host.lower() == "github.com":
        # Explicit review targets may use credentialed HTTPS remotes. Their transport is sanitized after selection.
        candidate = re.sub(r"^(https://)[^/@?#]+@", r"\1", raw_url, count=1, flags=re.IGNORECASE | re.ASCII)
        identity = _github_remote_identity(candidate, component_pattern=LITERAL_NAME)
        if identity is None:
            raise ValueError("unrecognized-repository-url")
        return RepositoryIdentity(host="github.com", repository="/".join(identity).lower())
    parts = [part for part in path.strip("/").split("/") if part]
    if "pull" in parts:
        parts = parts[: parts.index("pull")]
    if len(parts) < 2 or not host:
        raise ValueError(f"unrecognized-repository-url:{raw_url}")
    repository = "/".join(parts[:2])
    if repository.endswith(".git"):
        repository = repository[:-4]
    return RepositoryIdentity(host=host.lower(), repository=repository.lower())


def read_remotes(cwd: Path) -> dict[str, list[str]]:
    """Read every configured fetch URL without contacting a remote service."""

    def git(*args: str, optional: bool = False) -> str:
        """Preserve Git's record delimiters and distinguish missing configuration from command failure."""
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False, timeout=30)  # noqa: S603, S607 - local Git queries, argv only
        if optional and result.returncode == 1:
            return ""
        result.check_returncode()
        return result.stdout.decode("utf-8")

    names = git("remote").removesuffix("\n").split("\n")
    return {name: _tracking_remote_urls(git, name) for name in names if name}


def select_remote(expected_url: str, remotes: dict[str, list[str]]) -> dict[str, object]:
    """Select a deterministic remote whose normalized identity matches expected."""
    expected = parse_repository_url(expected_url)
    matches: list[tuple[str, str]] = []
    rejected: dict[str, list[str]] = {}
    for name, urls in remotes.items():
        for url in urls:
            try:
                identity = parse_repository_url(url, configured_remote=True)
            except ValueError:
                rejected.setdefault(name, []).append(url)
                continue
            if identity == expected:
                matches.append((name, url))
    matches.sort(key=lambda item: (item[0] != "origin", item[0], item[1]))
    if not matches:
        raise ValueError(f"no-matching-remote:{expected.host}/{expected.repository}")
    selected_name, selected_url = matches[0]
    return {
        "expected": asdict(expected),
        "remote": selected_name,
        "remote_url": selected_url,
        "matching_remotes": [{"name": name, "url": url} for name, url in matches],
        "unparseable_urls": rejected,
    }


def canonical_pr_url(number: str, remotes: dict[str, list[str]]) -> str:
    """Bind a positive PR number to origin or the sole configured GitHub repository."""
    if not re.fullmatch(r"[1-9][0-9]*", number):
        raise ValueError("invalid-pr-number")
    repositories: set[str] = set()
    origin_repositories: set[str] = set()
    for name, urls in remotes.items():
        for url in urls:
            identity = _github_remote_identity(url, component_pattern=LITERAL_NAME)
            if identity is None:
                continue
            repository = "/".join(identity).lower()
            repositories.add(repository)
            if name == "origin":
                origin_repositories.add(repository)
    if "origin" in remotes:
        if not origin_repositories:
            raise ValueError("no-github-origin")
        if len(origin_repositories) != 1:
            raise ValueError("ambiguous-github-origin")
        return f"https://github.com/{next(iter(origin_repositories))}/pull/{number}"
    if not repositories:
        raise ValueError("no-github-repository")
    if len(repositories) != 1:
        raise ValueError("ambiguous-github-repositories")
    return f"https://github.com/{next(iter(repositories))}/pull/{number}"


def main() -> int:
    """Emit a selected remote record or locally resolved canonical PR URL."""
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--expected-url", help="Authoritative GitHub PR or repository URL.")
    target.add_argument("--canonical-pr-url", help="Positive PR number to bind to one configured GitHub repository.")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Local repository used for remote matching.")
    parser.add_argument(
        "--identity-only", action="store_true", help="Return normalized host/repository identity without Git lookup."
    )
    args = parser.parse_args()
    try:
        if args.canonical_pr_url is not None:
            if args.identity_only:
                parser.error("--identity-only requires --expected-url")
            print(canonical_pr_url(args.canonical_pr_url, read_remotes(args.cwd)))
            return 0
        if args.identity_only:
            payload = asdict(parse_repository_url(args.expected_url))
        else:
            payload = select_remote(args.expected_url, read_remotes(args.cwd))
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
