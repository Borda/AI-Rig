#!/usr/bin/env python
"""Scan contributor code before it runs, then run it in a disposable copy with the maintainer's secrets out of reach.

## Purpose

    Give every workflow that executes pull-request or other contributor code one shared tripwire and one shared
    containment wrapper. ``scan`` lists the files a test command loads on its own, reads the files the contribution
    changed, and reports credential reads, secret-environment access, network and exfiltration endpoints, dynamic
    execution, persistence, destructive commands, agent-instruction edits, hidden Unicode, files no rule can read
    (binaries, symlinks, submodules) and dependency provenance. ``bind`` records the user's approval of one exact
    command as a hash. ``run`` executes that command in a disposable snapshot of the working tree, under the strongest
    isolation tier this host proves usable, with an environment allowlist and a fresh home directory.

## Scope

    Trust model: files the base ref already contains are the maintainer's and are listed, not pattern-scanned.
    Packages the base ref already declares and the official index supplies are trusted by declared name; their
    contents are never scanned, and a changed lockfile, ``setup.py`` or requirements include is reported because a
    name does not prove where the installed copy came from. A dependency the contribution adds, a direct URL, VCS or
    path reference, an index override or a lookalike of a known name is reported. The scan is a static heuristic,
    never a safety claim: its verdict is ``needs-attention``, ``review`` or ``no-findings``, and obfuscated or
    dynamically built payloads pass it. The risk level is presentation for the user's decision. It never relaxes
    isolation: every run uses the strongest tier, and a run below the sandbox needs the user's recorded
    ``unsandboxed`` decision whatever the scan said.

    Borderline code (low or medium findings) is not rejected: the recommendation is to run it in the sandbox. A high
    finding is put to the user with its evidence. An attack-shaped combination (private data read beside a way to
    send it out, decoded code beside secret or network access, an instruction file aimed at the reviewing agent) is
    ``likely-malicious``: the approval question leads with "do not run", and the user's answer is the override.

    The approval record is a consent record, not a boundary against the calling agent: keep it outside ``--repo``.
    The command's output is returned to the caller, so a secret the code can read can still leave through it; the
    sandbox limits what it can read, and the printed isolation line states exactly how.

## Usage

    python untrusted_code_guard.py scan --repo DIR --head REF (--base REF | --whole-tree) [--out FILE] [--text]
        [--bind-file FILE ...]
    python untrusted_code_guard.py bind --repo DIR --head REF (--base REF | --whole-tree) --out FILE
        [RUN OPTIONS] -- CMD ...
    python untrusted_code_guard.py run --approval-file FILE [--repo DIR] [--head REF (--base REF | --whole-tree)]
        [RUN OPTIONS] [--timeout SEC] [--report FILE] -- CMD ...
    RUN OPTIONS: [--env NAME ...] [--allow-loopback] [--tier auto|sandbox|env-only] [--decision unsandboxed]
        [--bind-file FILE ...] [--scope command|run]

    ``--bind-file`` names a file outside the repository that the command runs (an issue snippet, a generated script):
    it is scanned as contributed code and its bytes are hashed into the approval.

    ``--scope command`` (default) binds the approval to the exact argv; ``--scope run`` binds it to the scan, tier,
    loopback, environment and decisions only, for a workflow whose agents run many test commands under one user
    answer. ``bind`` writes the options it covers into the approval file, so ``run`` needs only ``--approval-file``
    and the command; an option ``run`` passes that differs from the record fails the hash check.

## Outputs

    ``scan`` prints one JSON report (or, with ``--text``, the summary the approval question shows). ``bind`` writes the
    approval hash, which covers the scan digest, the command, the bound script bytes, the requested tier, loopback,
    environment names and the decisions. ``run`` forwards the command's output and exit status and prints one
    isolation line to stderr.

## Failure

    ``run`` refuses (exit 3) when the approval does not match this exact request, the approved head is not an
    ancestor of the repository's ``HEAD``, or the work done since the approved head adds a high finding or touches a
    dependency manifest: the parent rescans and asks again. A requested tier that is not usable fails (exit 4); it is
    never retried with a weaker tier, and ``auto`` landing below the sandbox fails unless ``unsandboxed`` is bound.

## Used by

    oss:review and oss:resolve PR-code execution gates, the shared untrusted-content rule, and the Codex Rig
    Contributed Code Execution rule, which runs ``scan --host-sandbox`` because Codex supplies the containment itself.

Exit codes:
    0-255 — the command's own status (``run``); 0 for a printed scan or a written approval
    1 — git failure or unreadable repository
    2 — bad arguments, including a secret-looking ``--env`` name
    3 — approval does not match, or the approved head is stale against the working tree
    4 — isolation tier unavailable, or below-sandbox run without the user's recorded decision
    124 — command exceeded ``--timeout``
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import cast

SCHEMA = 2
#: A file larger than this is listed but not read; a changed code file left unread is itself a finding.
MAX_FILE_BYTES = 512 * 1024
#: Findings printed in one report; the digest covers all of them, the rest are counted in ``findings_truncated``.
MAX_FINDINGS = 200
#: Resolved once so every git call names one executable, not a name searched again through a caller-controlled PATH.
_GIT = shutil.which("git") or "git"
_GIT_TIMEOUT = 60
#: Replace objects are ignored so a planted ``refs/replace`` entry cannot change which bytes the scan reads.
_GIT_ENV = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
_PROBE_TIMEOUT = 20
_EXCERPT_CHARS = 120
_DEFAULT_SNAPSHOT_MB = 500

#: Basenames a test or build command loads without being told to.
SURFACE_BASENAMES = frozenset(
    {
        "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py", "setup.py",
        "makefile", "gnumakefile", "sitecustomize.py", "usercustomize.py", ".pre-commit-config.yaml",
        "package.json", ".envrc", "justfile", "taskfile.yml", "uv.toml", "pip.conf", ".npmrc", "pipfile",
    }
)  # fmt: skip
LOCKFILES = frozenset(
    {"uv.lock", "poetry.lock", "pipfile.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "cargo.lock", "pdm.lock"}
)  # fmt: skip
_MEDIA_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".ttf", ".woff", ".woff2", ".bmp"}
)
_PROSE_SUFFIXES = frozenset({".md", ".rst"})
_PROSE_CATEGORIES = frozenset({"prompt_injection_text", "hidden_unicode", "credential_path", "exfil_endpoint"})
_AGENT_PATH = re.compile(
    r"(?:^|/)(?:CLAUDE|AGENTS|GEMINI)\.md$|(?:^|/)\.(?:claude|codex|cursor|windsurf|opencode)/|(?:^|/)SKILL\.md$"
    r"|(?:^|/)\.mcp\.json$|(?:^|/)\.cursorrules$|(?:^|/)copilot-instructions\.md$",
    re.IGNORECASE,
)
#: Paths inside the real repository the sandbox keeps unreadable even though the repository itself is readable.
_REPO_PRIVATE = (".git", ".claude", ".codex", ".temp", ".reports")
_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHASHSEED")
_ENV_WINDOWS_KEYS = ("SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "WINDIR")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|PRIVATE|API_?KEY|AUTH|SESSION|COOKIE", re.IGNORECASE)
_LIFECYCLE_SCRIPTS = ("preinstall", "install", "postinstall", "prepare", "prepack", "pretest", "test", "posttest")
#: Popular package names a new dependency is compared against for lookalikes, beside the base ref's own names.
_KNOWN_NAMES = (
    "numpy", "pandas", "requests", "urllib3", "pytest", "setuptools", "wheel", "pip", "torch", "scipy", "pillow",
    "matplotlib", "flask", "django", "pydantic", "cryptography", "boto3", "pyyaml", "jinja2", "click", "rich",
    "tqdm", "httpx", "attrs", "certifi", "idna", "packaging", "typing-extensions", "six", "colorama",
)  # fmt: skip

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
#: (category, severity, pattern, suffix filter or None for every non-prose file).
_RULES: tuple[tuple[str, str, re.Pattern[str], frozenset[str] | None], ...] = tuple(
    (cat, sev, re.compile(pat, re.IGNORECASE), exts)
    for cat, sev, pat, exts in (
        ("credential_path", "high",
         r"(?<![\w])\.(?:ssh|aws|gnupg|kube|azure|netrc|npmrc|pypirc|git-credentials)\b|\.config[/\\](?:gh|gcloud)\b"
         r"|\bhosts\.yml\b|Library[/\\]Keychains|\bid_(?:rsa|ed25519|ecdsa)\b|\.docker[/\\]config\.json"
         r"|/etc/(?:passwd|shadow)\b|\.claude\.json|\.codex[/\\]auth\.json|\.claude[/\\](?:settings|\.credentials)",
         None),
        ("secret_env", "medium",
         r"\b(?:GH_TOKEN|GITHUB_TOKEN|GITHUB_PAT|ANTHROPIC_API_KEY|OPENAI_API_KEY|AWS_SECRET_ACCESS_KEY"
         r"|AWS_ACCESS_KEY_ID|AWS_SESSION_TOKEN|PYPI_(?:API_)?TOKEN|TWINE_PASSWORD|NPM_TOKEN|HF_TOKEN"
         r"|CLAUDE_CODE_OAUTH_TOKEN|SSH_AUTH_SOCK)\b", None),
        ("env_dump", "medium",
         r"/proc/(?:self|\d+)/environ|\bprintenv\b|(?<![\w.])env\s*\|\s*\w", None),
        ("env_dump", "low", r"dict\(\s*os\.environ\s*\)|os\.environ\.(?:copy|items)\(|\bos\.environb\b", None),
        ("exfil_endpoint", "high",
         r"pastebin\.com|transfer\.sh|webhook\.site|requestbin|ngrok(?:-free)?\.(?:io|app)|discord(?:app)?\.com/api/webhooks"
         r"|hooks\.slack\.com|oast\.(?:fun|pro|live|site|online|me)|interact\.sh|pipedream\.net|trycloudflare\.com"
         r"|burpcollaborator", None),
        ("pipe_to_shell", "high", r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", None),
        ("network_tool", "medium",
         r"\b(?:curl|wget|ncat|netcat|socat|Invoke-WebRequest|Invoke-RestMethod)\b|\bnc\s+-|certutil\s+-urlcache", None),
        ("network_import", "low",
         r"^\s*(?:import|from)\s+(?:socket|requests|urllib\.request|urllib3|http\.client|httpx|aiohttp|ftplib"
         r"|smtplib|paramiko|telnetlib|websockets?)\b", frozenset({".py", ".pth"})),
        ("dynamic_exec", "high",
         r"\b(?:exec|eval)\s*\(\s*(?:base64\.|b64decode|zlib\.|codecs\.|bytes\.fromhex|marshal\.|compile\()"
         r"|powershell(?:\.exe)?\s+-(?:enc|e|encodedcommand)\b", None),
        ("dynamic_exec", "medium", r"__import__\(\s*['\"](?:os|subprocess|socket|ctypes)['\"]\s*\)", None),
        ("dynamic_exec", "low",
         r"\bmarshal\.loads|\bpickle\.loads?\(|\bctypes\.(?:CDLL|cdll|windll)|\bos\.system\("
         r"|subprocess\.\w+\([^)\n]*shell\s*=\s*True", frozenset({".py", ".pth"})),
        ("obfuscation", "medium",
         r"[A-Za-z0-9+/]{200,}={0,2}|(?:\\x[0-9a-f]{2}){16,}|chr\(\d+\)\s*\+\s*chr\(\d+\)\s*\+\s*chr\(", None),
        ("persistence", "high",
         r"\bcrontab\b|\blaunchctl\b|LaunchAgents|LaunchDaemons|authorized_keys|\bschtasks\b"
         r"|systemctl\s+(?:--user\s+)?(?:enable|start)\b", None),
        ("persistence", "medium",
         r"\.(?:bashrc|zshrc|zprofile|bash_profile)\b|(?<![\w])\.profile\b|\.git[/\\]hooks|core\.hooksPath"
         r"|\.config/autostart|\bfsmonitor\b", None),
        ("credential_use", "high",
         r"\bgh\s+auth\s+(?:token|status|login)\b|\bgit\s+credential\b|\bgit\s+config\s+--(?:global|system)\b", None),
        ("remote_write", "medium",
         r"\bgit\s+push\b|\bgh\s+(?:api|release|repo|pr|issue)\b|\bnpm\s+publish\b|\btwine\s+upload\b", None),
        ("package_install", "low", r"\b(?:uv\s+pip\s+install|pip(?:3)?\s+install|uv\s+(?:add|sync))\b", None),
        ("destructive", "high",
         r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(?:~|/|\$HOME|\$\{HOME\})(?:\s|/?$|/\*)"
         r"|shutil\.rmtree\(\s*(?:os\.path\.expanduser\(|Path\.home\(|['\"]~|['\"]/['\"])|\bmkfs\b"
         r"|\bdd\s+if=\S+\s+of=/dev/|:\(\)\s*\{\s*:\|:&\s*\}\s*;", None),
        ("hidden_unicode", "high", "[‪-‮⁦-⁩]", None),
        ("hidden_unicode", "medium", "[​-‏⁠]", None),
        ("prompt_injection_text", "medium",
         r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|rules|prompts?)"
         r"|disregard\s+(?:the\s+)?(?:above|previous|system)\b|you\s+are\s+now\s+(?:in\s+)?(?:developer|dan|root)\b"
         r"|(?:reveal|print|send|post|upload)\s+(?:your\s+)?(?:system\s+prompt|api\s+keys?|credentials|secrets?|tokens?)"
         r"|do\s+not\s+(?:tell|inform|mention)\s+the\s+(?:user|reviewer|maintainer)", None),
    )
)  # fmt: skip

LIMITS = (
    "static pattern scan: obfuscated, encrypted or dynamically built payloads are not detected",
    "only the auto-load surface and files the contribution changed are read; the wider import closure is not",
    "dependencies are trusted by declared name: the scan does not prove an installed package came from the official index",
    "files the base ref already contains are trusted and listed, not pattern-scanned",
    "the command's output is returned to the caller: a secret the code can read can still leave through it",
)


class GuardError(Exception):
    """A scan or run precondition failed; ``code`` is the process exit status."""

    def __init__(self, message: str, code: int = 1) -> None:
        """Keep the message and the exit status the caller maps it to."""
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------------------------------------------------
# git access (frozen reads: nothing here depends on or alters the working tree)
# --------------------------------------------------------------------------------------------------------------------


def _git(repo: str, *args: str) -> bytes:
    """Run one read-only git plumbing call without external diff or textconv programs."""
    try:
        done = subprocess.run(  # noqa: S603
            [_GIT, "-C", repo, "-c", "core.fsmonitor=false", *args],
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            env=_GIT_ENV,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError(f"git {' '.join(args[:2])} failed: {exc}") from exc
    return done.stdout


def _resolve(repo: str, ref: str) -> str:
    """Return the full commit SHA for ``ref``; a ref starting with ``-`` is refused as argv injection."""
    if ref.startswith("-"):
        raise GuardError(f"ref starts with '-': {ref!r}", 2)
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()


def _is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """Return whether ``ancestor`` is reachable from ``descendant``."""
    try:
        done = subprocess.run(  # noqa: S603
            [_GIT, "-C", repo, "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=_GIT_ENV,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError(f"git merge-base failed: {exc}") from exc
    return done.returncode == 0


def _changed_paths(repo: str, base: str, head: str) -> dict[str, str]:
    """Map every path the head adds or modifies relative to ``base`` to its change status (deletions are dropped)."""
    raw = _git(repo, "diff", "--name-status", "-z", "--no-renames", "--no-ext-diff", base, head)
    parts = raw.decode("utf-8", "surrogateescape").split("\0")
    out: dict[str, str] = {}
    for status, path in zip(parts[0::2], parts[1::2]):
        if status and not status.startswith("D"):
            out[path] = status[0]
    return out


def _tree_entries(repo: str, sha: str) -> dict[str, tuple[str, str]]:
    """Map every path in the commit's tree to its ``(mode, blob sha)``."""
    out: dict[str, tuple[str, str]] = {}
    for record in _git(repo, "ls-tree", "-r", "-z", sha).decode("utf-8", "surrogateescape").split("\0"):
        meta, _, path = record.partition("\t")
        if path:
            mode, _kind, oid = meta.split()
            out[path] = (mode, oid)
    return out


def _blob(repo: str, sha: str, path: str) -> bytes | None:
    """Return the file bytes at ``sha``, or ``None`` when it does not exist there (or is a submodule)."""
    try:
        return _git(repo, "cat-file", "blob", f"{sha}:{path}")
    except GuardError:
        return None


def _blob_sha(data: bytes) -> str:
    """Return the git blob object id of ``data`` (SHA-1 object format), without needing a repository."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _sha256(data: bytes) -> str:
    """Return the hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------------------------------------------------


def is_surface(path: str) -> bool:
    """Return whether a test or build command loads ``path`` without being asked to."""
    name = PurePosixPath(path).name.lower()
    return (
        name in SURFACE_BASENAMES
        or name.endswith(".pth")
        or (name.startswith("requirements") and name.endswith(".txt"))
    )


def _excerpt(line: str) -> str:
    """Shorten a matched line and drop control characters so a finding is safe to print."""
    text = "".join(ch if ch.isprintable() else " " for ch in line.strip())
    return text[:_EXCERPT_CHARS]


def _finding(category: str, severity: str, path: str, excerpt: str, line: int = 0) -> dict[str, object]:
    """Build one finding record."""
    return {"category": category, "severity": severity, "path": path, "line": line, "excerpt": _excerpt(excerpt)}


def _scan_text(path: str, text: str) -> list[dict[str, object]]:
    """Apply every content rule that covers this file and return one finding per hit; prose gets the prose rules."""
    suffix = PurePosixPath(path).suffix.lower()
    prose = suffix in _PROSE_SUFFIXES
    found: list[dict[str, object]] = []
    seen: set[tuple[str, str, int]] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        for category, severity, pattern, exts in _RULES:
            if (exts is not None and suffix not in exts) or (prose and category not in _PROSE_CATEGORIES):
                continue
            if pattern.search(line) and (category, severity, number) not in seen:
                seen.add((category, severity, number))
                found.append(_finding(category, severity, path, line, number))
    return found


def _read_candidate(repo: str, head: str, path: str) -> tuple[bytes | None, str]:
    """Read one file at the head; the second value names why it was left unread (empty when it was read)."""
    data = _blob(repo, head, path)
    if data is None:
        return None, "unreadable or submodule"
    if len(data) > MAX_FILE_BYTES:
        return None, f"larger than {MAX_FILE_BYTES // 1024} KiB"
    if b"\0" in data[:8192]:
        return None, "binary"
    return data, ""


def _structural(path: str, mode: str, why: str) -> dict[str, object] | None:
    """Report a changed file no pattern rule can read: a link, a submodule, an opaque or oversized file, a bare
    script."""
    suffix = PurePosixPath(path).suffix.lower()
    if mode == "120000":
        return _finding("structural", "medium", path, "symlink in the contribution: its target is not read")
    if mode == "160000":
        return _finding("structural", "medium", path, "submodule pointer: the code it points at is not read")
    if why and not (why == "binary" and suffix in _MEDIA_SUFFIXES):
        return _finding("structural", "medium", path, f"not read ({why}); loaded code can hide here")
    if mode == "100755" and not suffix:
        return _finding("structural", "low", path, "executable file without a recognised suffix")
    return None


def _scan_files(
    repo: str, head: str, changed: dict[str, str], entries: dict[str, tuple[str, str]]
) -> tuple[list[dict[str, object]], list[dict[str, str]], int]:
    """Read every changed file once and return its findings, the unread files, and the count scanned."""
    findings: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    scanned = 0
    for path in sorted(changed):
        mode = entries.get(path, ("", ""))[0]
        if _AGENT_PATH.search(path):
            findings.append(
                _finding("agent_instruction_change", "high", path, "changes a file an agent host loads as instructions")
            )
        data, why = (None, "symlink") if mode == "120000" else _read_candidate(repo, head, path)
        extra = _structural(path, mode, why)
        if extra:
            findings.append(extra)
        if data is None:
            skipped.append({"path": path, "reason": why})
            continue
        scanned += 1
        findings.extend(_scan_text(path, data.decode("utf-8", "replace")))
    return findings, skipped, scanned


# --- dependency provenance -------------------------------------------------------------------------------------------

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_DIRECT_REF = re.compile(r"@|(?:git|hg|svn|bzr)\+|://|^\s*(?:\.{1,2}/|/|file:)", re.IGNORECASE)
_INDEX_OPTION = re.compile(
    r"^\s*(?:--index-url|--extra-index-url|--find-links|--trusted-host|-i\b|-f\b)", re.IGNORECASE
)
_INCLUDE_OPTION = re.compile(r"^\s*(?:-r|-c|--requirement|--constraint)\b", re.IGNORECASE)


def _norm(name: str) -> str:
    """Normalize a distribution name the way PEP 503 does, so ``Foo_Bar`` and ``foo-bar`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement(line: str) -> tuple[str, str, bool] | None:
    """Split one requirement string into ``(name, spec, is_direct_reference)``; ``None`` for non-requirements."""
    text = line.split("#", 1)[0].strip()
    if not text or text.startswith("-"):
        return None
    match = _REQ_NAME.match(text)
    if not match:
        return None
    rest = text[match.end() :]
    return _norm(match.group(1)), rest.strip(), bool(_DIRECT_REF.search(rest) or _DIRECT_REF.search(text[:1]))


def _from_requirements_txt(text: str) -> tuple[dict[str, tuple[str, bool]], set[str]]:
    """Read ``requirements*.txt``: requirement lines plus index-override and unresolved-include option lines."""
    deps: dict[str, tuple[str, bool]] = {}
    marks: set[str] = set()
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip()
        if _INDEX_OPTION.match(stripped):
            marks.add(stripped)
        elif _INCLUDE_OPTION.match(stripped):
            marks.add(f"include: {stripped}")
        elif stripped.startswith(("-e", "--editable")):
            marks.add(f"editable: {stripped}")
        else:
            req = _requirement(stripped)
            if req:
                deps[req[0]] = (req[1], req[2])
    return deps, marks


def _toml_loads(text: str) -> dict[str, object] | None:
    """Parse TOML with the stdlib reader when this interpreter has one; ``None`` when it does not."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        return tomllib.loads(text)
    except ValueError:
        return {}


def _strings(value: object) -> Iterable[str]:
    """Yield every string inside a list or dict-of-lists of requirement strings."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _table(value: object) -> dict[str, object]:
    """Return ``value`` when it is a TOML table, else an empty one."""
    return value if isinstance(value, dict) else {}


def _from_pyproject(text: str) -> tuple[dict[str, tuple[str, bool]], set[str]] | None:
    """Read ``pyproject.toml`` dependency tables and index overrides; ``None`` when no TOML reader exists."""
    data = _toml_loads(text)
    if data is None:
        return None
    project, uv = _table(data.get("project")), _table(_table(data.get("tool")).get("uv"))
    sources = [
        project.get("dependencies"), project.get("optional-dependencies"), data.get("dependency-groups"),
        _table(data.get("build-system")).get("requires"), uv.get("dev-dependencies"),
    ]  # fmt: skip
    deps: dict[str, tuple[str, bool]] = {}
    for entry in sources:
        for line in _strings(entry):
            req = _requirement(line)
            if req:
                deps[req[0]] = (req[1], req[2])
    marks = {
        f"[tool.uv].{key}" for key in ("sources", "index", "index-url", "extra-index-url", "find-links") if uv.get(key)
    }
    return deps, marks


def _from_setup_cfg(text: str) -> dict[str, tuple[str, bool]]:
    """Read ``setup.cfg`` install, setup and extras requirements."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return {}
    blobs = [parser.get("options", key, fallback="") for key in ("install_requires", "setup_requires")]
    if parser.has_section("options.extras_require"):
        blobs += [parser.get("options.extras_require", key) for key in parser["options.extras_require"]]
    deps: dict[str, tuple[str, bool]] = {}
    for line in "\n".join(blobs).splitlines():
        req = _requirement(line)
        if req:
            deps[req[0]] = (req[1], req[2])
    return deps


def _package_json(text: str) -> tuple[dict[str, tuple[str, bool]], dict[str, str]]:
    """Read ``package.json`` dependency names with registry-vs-direct classification, and lifecycle scripts."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}, {}
    data = _table(data)
    deps: dict[str, tuple[str, bool]] = {}
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, spec in _table(data.get(key)).items():
            direct = bool(re.match(r"^(?:git|github:|file:|link:|https?:|\.{0,2}/|[\w.-]+/[\w.-]+$)", str(spec)))
            deps[name.lower()] = (str(spec), direct)
    scripts = _table(data.get("scripts"))
    return deps, {k: str(v) for k, v in scripts.items() if k in _LIFECYCLE_SCRIPTS}


def _parse_dependency_file(path: str, text: str) -> tuple[dict[str, tuple[str, bool]], set[str], bool]:
    """Return ``(deps, marks, parsed)`` for one dependency file; ``parsed`` is False when no reader could parse it."""
    name = PurePosixPath(path).name.lower()
    if name == "pyproject.toml":
        got = _from_pyproject(text)
        return (got[0], got[1], True) if got else ({}, set(), False)
    if name == "setup.cfg":
        return _from_setup_cfg(text), set(), True
    if name == "package.json":
        return _package_json(text)[0], set(), True
    if name in {"uv.toml", "pip.conf", ".npmrc"}:
        hit = {
            ln.strip()
            for ln in text.splitlines()
            if re.match(r"\s*(?:index-url|extra-index-url|find-links|registry)", ln, re.I)
        }
        return {}, hit, True
    if name.startswith("requirements") and name.endswith(".txt"):
        deps, marks = _from_requirements_txt(text)
        return deps, marks, True
    return {}, set(), True


def is_dependency_manifest(path: str) -> bool:
    """Return whether ``path`` declares or pins dependencies (manifests, lockfiles, ``setup.py``)."""
    name = PurePosixPath(path).name.lower()
    return (
        name in {"pyproject.toml", "setup.cfg", "setup.py", "package.json", "uv.toml", "pip.conf", ".npmrc", "pipfile"}
        or name in LOCKFILES
        or (name.startswith("requirements") and name.endswith(".txt"))
    )


def _collect(
    repo: str, sha: str | None, paths: Sequence[str]
) -> tuple[dict[str, tuple[str, bool]], set[str], list[str]]:
    """Merge every dependency file at ``sha`` into one name map, mark set and unparsed-file list."""
    deps: dict[str, tuple[str, bool]] = {}
    marks: set[str] = set()
    unparsed: list[str] = []
    for path in paths if sha else []:
        data = _blob(repo, sha, path)
        if data is None:
            continue
        got, over, parsed = _parse_dependency_file(path, data.decode("utf-8", "replace"))
        deps.update(got)
        marks |= over
        if not parsed:
            unparsed.append(path)
    return deps, marks, unparsed


def _edit_distance(a: str, b: str) -> int:
    """Return the optimal-string-alignment distance: insert, delete, substitute or swap two neighbours, each one
    edit."""
    rows = [list(range(len(b) + 1))] + [[i] + [0] * len(b) for i in range(1, len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[len(a)][len(b)]


def _lookalikes(new_names: Iterable[str], known: Iterable[str]) -> list[tuple[str, str]]:
    """Return ``(new, existing)`` pairs one edit apart (two for names of 9+ characters) but not equal."""
    pool = sorted({*known, *_KNOWN_NAMES})
    pairs = []
    for name in sorted(new_names):
        limit = 2 if len(name) >= 9 else 1
        close = [c for c in pool if c != name and len(name) >= 4 and _edit_distance(name, c) <= limit]
        if close:
            pairs.append((name, close[0]))
    return pairs


def _dependency_report(
    repo: str, base: str | None, head: str, tree: Sequence[str], changed: dict[str, str]
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Compare declared dependencies between the base and the head and report what the contribution introduces."""
    files = sorted(p for p in tree if is_dependency_manifest(p) and PurePosixPath(p).name.lower() not in LOCKFILES)
    head_deps, head_marks, unparsed = _collect(repo, head, files)
    base_deps, base_marks, _ = _collect(repo, base, files)
    findings = [
        _finding("dependency_unparsed", "medium", p, "no TOML reader (Python < 3.11): dependency changes not compared")
        for p in unparsed
    ]
    added = sorted(set(head_deps) - set(base_deps))
    direct = sorted(
        n for n, (spec, is_direct) in head_deps.items() if is_direct and base_deps.get(n, ("", False))[0] != spec
    )
    new_marks = sorted(head_marks - base_marks)
    includes = [m for m in new_marks if m.startswith("include: ")]
    overrides = [m for m in new_marks if m not in includes]
    lookalikes = _lookalikes(added, base_deps)
    findings += [
        _finding("dependency_new", "medium", "dependencies", f"new dependency {n} - verify provenance") for n in added
    ]
    findings += [
        _finding("dependency_non_index", "high", "dependencies", f"{n} uses a direct URL, VCS or path reference")
        for n in direct
    ]
    findings += [
        _finding("dependency_index_override", "high", "dependencies", f"index or source override added: {m}")
        for m in overrides
    ]
    findings += [
        _finding("dependency_include", "medium", "dependencies", f"{m} - the included file's contents are not followed")
        for m in includes
    ]
    findings += [_finding("dependency_lookalike", "high", "dependencies", f"{a} looks like {b}") for a, b in lookalikes]
    for path in sorted(
        p for p in changed if PurePosixPath(p).name.lower() in LOCKFILES or PurePosixPath(p).name.lower() == "setup.py"
    ):
        findings.append(
            _finding(
                "dependency_source_unchecked",
                "medium",
                path,
                "changed lockfile or setup.py: resolved sources are not compared",
            )
        )
    report = {
        "added": added,
        "non_index_references": direct,
        "index_overrides": overrides,
        "includes": includes,
        "lookalikes": [f"{a}~{b}" for a, b in lookalikes],
        "trusted_unchanged": len([n for n in head_deps if n in base_deps and n not in direct]),
        "unparsed": unparsed,
    }
    return report, findings


def _lifecycle_findings(repo: str, base: str | None, head: str, tree: Sequence[str]) -> list[dict[str, object]]:
    """Flag a ``package.json`` install or test script the contribution adds or rewrites."""
    out: list[dict[str, object]] = []
    for path in (p for p in tree if PurePosixPath(p).name == "package.json"):
        new = _package_json((_blob(repo, head, path) or b"").decode("utf-8", "replace"))[1]
        old = _package_json((_blob(repo, base, path) or b"").decode("utf-8", "replace"))[1] if base else {}
        for key, command in sorted(new.items()):
            if old.get(key) != command:
                bad = bool(re.search(r"curl|wget|node\s+-e|base64|https?://", command))
                out.append(
                    _finding(
                        "lifecycle_script", "high" if bad else "medium", path, f"lifecycle script {key}: {command}"
                    )
                )
    return out


def _rank(findings: list[dict[str, object]]) -> list[dict[str, object]]:
    """Order findings most severe first, then by path and line, so the report is stable."""
    return sorted(findings, key=lambda f: (_SEVERITY_ORDER[str(f["severity"])], str(f["path"]), int(f["line"])))  # type: ignore[call-overload]


def _verdict(findings: Sequence[dict[str, object]]) -> str:
    """Map findings to a verdict that never claims safety."""
    severities = {f["severity"] for f in findings}
    return "needs-attention" if "high" in severities else "review" if severities else "no-findings"


#: Evidence that reads something private, and evidence that can carry it out; both in one file is the theft shape.
_ACCESS = frozenset({"credential_path", "secret_env", "env_dump", "credential_use"})
_SINK = frozenset({"exfil_endpoint", "network_tool", "network_import", "pipe_to_shell"})


def _malicious_shapes(findings: Sequence[dict[str, object]]) -> list[str]:
    """Return the per-file combinations that read as an attack rather than as an unusual test."""
    by_path: dict[str, set[str]] = {}
    high_by_path: dict[str, set[str]] = {}
    for f in findings:
        by_path.setdefault(str(f["path"]), set()).add(str(f["category"]))
        if f["severity"] == "high":
            high_by_path.setdefault(str(f["path"]), set()).add(str(f["category"]))
    shapes = []
    for path, cats in sorted(by_path.items()):
        if cats & _ACCESS and cats & _SINK:
            shapes.append(f"{path}: reads private data and has a way to send it out")
        if "dynamic_exec" in high_by_path.get(path, set()) and cats & (_ACCESS | _SINK):
            shapes.append(f"{path}: decoded code is executed beside a secret or network access")
        if "agent_instruction_change" in cats and "prompt_injection_text" in cats:
            shapes.append(f"{path}: instruction file carries text aimed at the reviewing agent")
        if "hidden_unicode" in high_by_path.get(path, set()) and len(high_by_path[path]) > 1:
            shapes.append(f"{path}: hidden bidi controls beside other high-risk code")
    return shapes


def assess_risk(findings: Sequence[dict[str, object]]) -> dict[str, object]:
    """Turn findings into a risk level and a recommendation for the user's decision; it never changes isolation.

    Levels: ``none`` (no findings), ``borderline`` (low or medium only: unusual but not rejected, run it in the
    sandbox), ``high`` (a high finding: decide on the evidence) and ``likely-malicious`` (an attack-shaped
    combination: the recommendation is not to run it, and choosing to run is the user's explicit override).
    """
    severities = {f["severity"] for f in findings}
    shapes = _malicious_shapes(findings)
    if shapes:
        level, advice = "likely-malicious", "do-not-run"
    elif "high" in severities:
        level, advice = "high", "ask-with-evidence"
    elif severities:
        level, advice = "borderline", "run-in-sandbox"
    else:
        level, advice = "none", "run"
    high = [f"{f['path']}: {f['category']}" for f in findings if f["severity"] == "high"]
    return {"level": level, "recommendation": advice, "reasons": (shapes + high)[:5]}


def probe_isolation(repo: str) -> str:
    """Return the tier this host can start right now (``sandbox`` or ``env-only``) without running anything real."""
    workdir = Path(tempfile.mkdtemp(prefix="untrusted-guard-probe-")).resolve()
    try:
        try:
            tier = choose_isolation("auto", repo, str(workdir), str(workdir), os.path.expanduser("~"), False)[0]
        except GuardError:
            tier = "env-only"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return tier


def _bound_findings(names: Sequence[str]) -> tuple[list[dict[str, object]], list[list[str]]]:
    """Scan files supplied outside the repository (an issue snippet, a generated script) as contributed code."""
    findings: list[dict[str, object]] = []
    hashes: list[list[str]] = []
    for name in sorted(names):
        try:
            data = Path(name).read_bytes()
        except OSError as exc:
            raise GuardError(f"--bind-file {name!r} unreadable: {exc}", 2) from exc
        label = f"bound:{Path(name).name}"
        hashes.append([label, _sha256(data)])
        if len(data) > MAX_FILE_BYTES or b"\0" in data[:8192]:
            findings.append(_finding("structural", "medium", label, "bound file not read (oversized or binary)"))
        else:
            findings.extend(_scan_text(label, data.decode("utf-8", "replace")))
    return findings, hashes


def build_report(
    repo: str,
    head_ref: str,
    base_ref: str | None,
    whole_tree: bool,
    probe: bool = False,
    bound: Sequence[str] = (),
    host_sandbox: bool = False,
) -> dict[str, object]:
    """Scan one head commit against its base and return the full report, digest included.

    ``bound`` names files outside the repository that the command will run; they are scanned as contributed code and
    hashed into the digest. ``probe`` adds the isolation tier available on this host; it is host state, so it stays out
    of the digest. ``host_sandbox`` replaces the probe for a host that confines the run itself: the report then says
    containment is the host's sandbox, not probed, instead of advising on this helper's own run tier.
    """
    head = _resolve(repo, head_ref)
    base = None if whole_tree else _git(repo, "merge-base", _resolve(repo, str(base_ref)), head).decode().strip()
    entries = _tree_entries(repo, head)
    tree = list(entries)
    changed = {p: "A" for p in tree} if base is None else _changed_paths(repo, base, head)
    surface = {p: _sha256(_blob(repo, head, p) or b"") for p in tree if is_surface(p)}
    findings, skipped, scanned = _scan_files(repo, head, changed, entries)
    deps, dep_findings = _dependency_report(repo, base, head, tree, changed)
    bound_findings, bound_hashes = _bound_findings(bound)
    ranked = _rank(findings + dep_findings + _lifecycle_findings(repo, base, head, tree) + bound_findings)
    body: dict[str, object] = {
        "schema": SCHEMA,
        "head": head,
        "base": base,
        "whole_tree": whole_tree,
        "auto_load": [{"path": p, "pr_modified": p in changed, "sha256": surface[p]} for p in sorted(surface)],
        "changed_files": len(changed),
        "scanned_files": scanned,
        "skipped": skipped,
        "bound_files": bound_hashes,
        "findings_hash": _sha256(json.dumps(ranked, sort_keys=True).encode()),
        "findings": ranked[:MAX_FINDINGS],
        "findings_truncated": max(0, len(ranked) - MAX_FINDINGS),
        "dependencies": deps,
        "verdict": _verdict(ranked),
        "risk": assess_risk(ranked),
        "limits": list(LIMITS),
    }
    body["digest"] = _sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    if host_sandbox:
        body["isolation_available"] = "host-sandbox"
    elif probe:
        body["isolation_available"] = probe_isolation(repo)
    body["prompt_summary"] = prompt_summary(body)
    return body


def prompt_summary(report: dict[str, object]) -> str:
    """Render the plain lines the approval question shows the user before PR code runs."""
    findings = cast("list[dict[str, object]]", report["findings"])
    risk = cast("dict[str, object]", report["risk"])
    deps = cast("dict[str, list[str]]", report["dependencies"])
    counts = {sev: sum(1 for f in findings if f["severity"] == sev) for sev in _SEVERITY_ORDER}
    auto = [a for a in report["auto_load"] if a["pr_modified"]]  # type: ignore[union-attr]
    lines = [
        f"verdict: {report['verdict']} (static heuristic, not a safety claim) · head {str(report['head'])[:12]}",
        f"findings: high {counts['high']} · medium {counts['medium']} · low {counts['low']}"
        f" · files changed {report['changed_files']} · scanned {report['scanned_files']}",
        f"auto-loaded files changed by the contribution: {', '.join(a['path'] for a in auto[:8]) or 'none'}",
        f"dependencies (trusted by declared name only): new {len(deps['added'])} · direct refs {len(deps['non_index_references'])}"
        f" · index overrides {len(deps['index_overrides'])} · includes {len(deps['includes'])} · lookalikes {len(deps['lookalikes'])}",
    ]
    lines += [f"  {f['severity']}: {f['category']} {f['path']}:{f['line']} {f['excerpt']}" for f in findings[:8]]
    lines.append(f"risk: {risk['level']} - recommendation: {risk['recommendation']}")
    lines += [f"  why: {reason}" for reason in risk["reasons"]]
    lines.append(_isolation_advice(risk, str(report.get("isolation_available", "unknown"))))
    return "\n".join(lines)


def _isolation_advice(risk: dict[str, object], available: str) -> str:
    """State what running this code involves and what protection this host gives, for the user's decision."""
    if available == "host-sandbox":
        lead = (
            "recommended: do not run; choosing Run is your explicit override. "
            if risk["level"] == "likely-malicious"
            else ""
        )
        return (
            f"{lead}containment: the host's own sandbox, not probed here; this helper's disposable-copy run is not used."
            " Running outside that sandbox needs your explicit approval. Command output returns to the agent."
        )
    host = {
        "sandbox": "sandbox available here: network denied, writes confined to a disposable copy, home directory unreadable"
        " except the interpreter and tools",
        "env-only": "no sandbox on this host: running would only clear variables and home lookups and would NOT protect"
        " ~/.ssh, the gh token file, the keychain or the network; it needs your explicit unsandboxed choice",
    }.get(available, "isolation on this host not probed")
    lead = (
        "recommended: do not run; choosing Run is your explicit override. "
        if risk["level"] == "likely-malicious"
        else ""
    )
    return f"{lead}to run: a disposable copy of the working tree, strongest tier. {host}. Command output returns to the agent."


# --------------------------------------------------------------------------------------------------------------------
# approval, snapshot, delta
# --------------------------------------------------------------------------------------------------------------------


def approval_hash(report: dict[str, object], args: argparse.Namespace) -> str:
    """Hash everything the user's approval covers: the scan, the exact command, bound scripts, tier, loopback, env,
    decisions."""
    files = []
    for name in sorted(args.bind_file):
        try:
            files.append([name, _sha256(Path(name).read_bytes())])
        except OSError as exc:
            raise GuardError(f"--bind-file {name!r} unreadable: {exc}", 2) from exc
    payload = {
        "scan": report["digest"],
        "scope": args.scope,
        "argv": [] if args.scope == "run" else list(args.cmd),
        "files": files,
        "tier": args.tier,
        "loopback": bool(args.allow_loopback),
        "env": sorted(args.env),
        "decisions": sorted(args.decision),
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


#: Options the approval record carries, so a caller of ``run`` need not repeat them (the hash still covers every one).
_RECORD_KEYS = ("head", "base", "whole_tree", "tier", "allow_loopback", "env", "decision", "bind_file", "scope")
_DEFAULTS: dict[str, object] = {"tier": "auto", "scope": "command", "env": [], "decision": [], "bind_file": []}


def _apply_defaults(args: argparse.Namespace) -> None:
    """Fill options still unset after parsing (and after any approval record) with their defaults."""
    for key, value in _DEFAULTS.items():
        if getattr(args, key) is None:
            setattr(args, key, list(value) if isinstance(value, list) else value)


def _merge_record(args: argparse.Namespace) -> None:
    """Take options the caller left out from the approval record; an explicit differing option fails the hash check."""
    try:
        lines = Path(args.approval_file).read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[1]) if len(lines) > 1 else {}
    except (OSError, ValueError):
        record = {}
    for key in _RECORD_KEYS:
        if key in record and getattr(args, key) in (None, False):
            setattr(args, key, record[key])


def approval_record(digest: str, args: argparse.Namespace) -> str:
    """Return the approval file text: the hash line, then the options it covers as one JSON line."""
    return digest + "\n" + json.dumps({key: getattr(args, key) for key in _RECORD_KEYS}, sort_keys=True) + "\n"


def _check_approval(expected: str, approval_file: str) -> None:
    """Refuse unless the approval file's first line is this exact request's hash."""
    try:
        recorded = Path(approval_file).read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        raise GuardError("approval missing: the parent must show the scan, ask, then run bind", 3) from None
    if recorded != expected:
        raise GuardError("approval does not match this scan, command, tier, loopback, env and decisions", 3)


def _safe_relative(rel: str) -> bool:
    """Return whether a git path is a plain relative path inside the repository."""
    parts = PurePosixPath(rel).parts
    return bool(parts) and not PurePosixPath(rel).is_absolute() and ".." not in parts and ".git" not in parts


def snapshot_repo(repo: str, dest: Path, max_bytes: int) -> list[str]:
    """Copy every tracked and untracked-but-not-ignored file into ``dest`` (no ``.git``) and return the copied paths."""
    listing = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode(
        "utf-8", "surrogateescape"
    )
    copied: list[str] = []
    total = 0
    for rel in (p for p in listing.split("\0") if p and _safe_relative(p)):
        src, dst = Path(repo) / rel, dest / rel
        if src.is_dir() and not src.is_symlink():
            continue
        if not (src.is_symlink() or src.is_file()):
            continue
        total += 0 if src.is_symlink() else src.stat().st_size
        if total > max_bytes:
            raise GuardError(
                f"snapshot exceeds {max_bytes // (1024 * 1024)} MiB; raise --max-snapshot-mb or run a narrower checkout",
                4,
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)
        copied.append(rel)
    return copied


def _snapshot_bytes(path: Path) -> bytes:
    """Return the bytes git would store for a snapshot entry (a symlink stores its target)."""
    return os.readlink(path).encode() if path.is_symlink() else path.read_bytes()


def check_delta(repo: str, report: dict[str, object], snapshot: Path, copied: Sequence[str]) -> list[str]:
    """Require the approved head to be an ancestor of ``HEAD``, rescan what changed since it, and return those paths.

    Raises exit 3 when the approved head is stale or the delta adds a high finding or touches a dependency manifest.
    """
    head = str(report["head"])
    if not _is_ancestor(repo, head, "HEAD"):
        raise GuardError("approved head is not an ancestor of HEAD: rescan and ask again", 3)
    entries = _tree_entries(repo, head)
    delta = [rel for rel in copied if entries.get(rel, ("", ""))[1] != _blob_sha(_snapshot_bytes(snapshot / rel))]
    manifests = sorted(rel for rel in delta if is_dependency_manifest(rel))
    if manifests:
        raise GuardError(
            f"work since the approved head touches dependency files: {', '.join(manifests[:6])}; rescan and ask again",
            3,
        )
    for rel in delta:
        path = snapshot / rel
        if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            continue
        data = path.read_bytes()
        high = [f for f in _scan_text(rel, data.decode("utf-8", "replace")) if f["severity"] == "high"]
        if b"\0" not in data[:8192] and high:
            raise GuardError(f"work since the approved head adds a high finding in {rel}: rescan and ask again", 3)
    return delta


# --------------------------------------------------------------------------------------------------------------------
# isolation and run
# --------------------------------------------------------------------------------------------------------------------


def _validate_extra_env(names: Sequence[str]) -> None:
    """Reject an extra environment name that is malformed or looks like a credential."""
    for name in names:
        if not _ENV_NAME.match(name):
            raise GuardError(f"invalid --env name {name!r}", 2)
        if _SECRET_NAME.search(name):
            raise GuardError(f"--env {name} looks like a credential and is never passed to contributor code", 2)


def build_env(source: dict[str, str], extra: Sequence[str], home: str, tmp: str) -> dict[str, str]:
    """Build the child environment from an allowlist: nothing else in ``source`` survives."""
    keep = (*_ENV_KEYS, *(_ENV_WINDOWS_KEYS if os.name == "nt" else ()), *extra)
    env = {key: source[key] for key in keep if key in source}
    env.update(
        {
            "HOME": home, "USERPROFILE": home, "APPDATA": home, "LOCALAPPDATA": home, "XDG_CONFIG_HOME": home,
            "XDG_CACHE_HOME": tmp, "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp, "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "PIP_CONFIG_FILE": os.devnull,
        }
    )  # fmt: skip
    return env


def _readable_roots(repo: str) -> list[str]:
    """List what the run must read from the real filesystem: interpreter prefixes, the active venv, PATH tools, the
    repo."""
    raw = [sys.base_prefix, sys.prefix, sys.exec_prefix, os.environ.get("VIRTUAL_ENV", ""), repo]
    raw += [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    roots = {os.path.realpath(p) for p in raw if p and Path(p).exists()}
    return sorted(roots)


def _sbpl_quote(path: str) -> str:
    """Quote a path for a sandbox profile; a quote, backslash or newline in a path is refused rather than escaped."""
    if any(ch in path for ch in '"\n\r\\'):
        raise GuardError(f"path not representable in a sandbox profile: {path!r}", 4)
    return f'"{path}"'


def _macos_profile(snapshot: str, tmp: str, repo: str, real_home: str, roots: Sequence[str], loopback: bool) -> str:
    """Build the macOS profile: network denied, writes confined, home unreadable except what the run needs."""
    lines = ["(version 1)", "(allow default)", "(deny network*)"]
    if loopback:
        lines += [
            '(allow network-bind (local ip "localhost:*"))',
            '(allow network-inbound (local ip "localhost:*"))',
            '(allow network-outbound (remote ip "localhost:*"))',
        ]
    writable = " ".join(f"(subpath {_sbpl_quote(p)})" for p in (snapshot, tmp, "/dev"))
    lines += ["(deny file-write*)", f"(allow file-write* {writable})"]
    home = os.path.realpath(real_home)
    lines.append(f"(deny file-read-data (subpath {_sbpl_quote(home)}))")
    lines += [
        f"(allow file-read-data (subpath {_sbpl_quote(r)}))" for r in roots if r == home or r.startswith(home + os.sep)
    ]
    private = [os.path.join(os.path.realpath(repo), name) for name in _REPO_PRIVATE]
    lines += [f"(deny file-read-data (subpath {_sbpl_quote(p)}))" for p in private]
    lines.append(f'(deny file-read-data (regex #"^{re.escape(os.path.realpath(repo))}/(.*/)?\\.env($|[./])"))')
    lines.append('(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.securityd"))')
    return "\n".join(lines) + "\n"


def _bwrap_prefix(snapshot: str, tmp: str, repo: str, roots: Sequence[str]) -> list[str]:
    """Build the Linux prefix: every namespace private, selective read-only binds, writes confined to the copy and
    temp."""
    args = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run"]  # fmt: skip  # noqa: S108  (paths inside the sandbox)
    for system in ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc"):
        if Path(system).exists():
            args += ["--ro-bind", system, system]
    # The real checkout is never bound: the run works in the copy, and binding it would expose its .env and make a
    # tmpfs over .git writable. Roots inside it (a venv) are bound on their own.
    real_repo = os.path.realpath(repo)
    for root in roots:
        if root != real_repo and not root.startswith(("/usr", "/bin", "/sbin", "/lib", "/etc")):
            args += ["--ro-bind", root, root]
    args += ["--bind", snapshot, snapshot, "--bind", tmp, tmp]
    return args


def _probe(prefix: Sequence[str], workdir: str) -> bool:
    """Prove a containment prefix starts a trivial process; the probe runs isolated, with an empty environment."""
    try:
        done = subprocess.run(  # noqa: S603
            [*prefix, sys.executable, "-I", "-S", "-c", "pass"],
            capture_output=True, timeout=_PROBE_TIMEOUT, check=False, env={}, cwd=workdir,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def choose_isolation(
    requested: str, repo: str, snapshot: str, tmp: str, real_home: str, loopback: bool
) -> tuple[str, list[str], str]:
    """Return ``(tier, command prefix, mechanism)``; ``env-only`` has an empty prefix.

    A requested tier never downgrades.
    """
    candidate: list[str] = []
    mechanism = ""
    roots = _readable_roots(repo)
    if requested != "env-only":
        sandbox_exec, bwrap = shutil.which("sandbox-exec"), shutil.which("bwrap")
        if sys.platform == "darwin" and sandbox_exec:
            profile = Path(tmp) / "guard.sb"
            profile.write_text(_macos_profile(snapshot, tmp, repo, real_home, roots, loopback), encoding="utf-8")
            candidate, mechanism = [sandbox_exec, "-f", str(profile)], "sandbox-exec"
        elif sys.platform.startswith("linux") and bwrap:
            if loopback:
                raise GuardError("--allow-loopback is not supported on the Linux sandbox tier", 4)
            candidate, mechanism = [bwrap, *_bwrap_prefix(snapshot, tmp, repo, roots)[1:]], "bwrap"
    if candidate and _probe(candidate, tmp):
        return "sandbox", candidate, mechanism
    if requested == "sandbox":
        raise GuardError("sandbox tier requested but not usable on this host (needs sandbox-exec or bwrap)", 4)
    return "env-only", [], ""


def isolation_line(tier: str, mechanism: str, env_keys: int, loopback: bool) -> str:
    """State what protection the run has, in words the approval record and the user can rely on."""
    if tier == "sandbox":
        net = "loopback only" if loopback else "denied"
        note = " (Linux tier not exercised by the plugin's tests on this host)" if mechanism == "bwrap" else ""
        return (
            f"untrusted-guard: isolation=sandbox({mechanism}){note} network={net} fs-write=disposable-copy+temp "
            f"home-read=interpreter+tools-only repo-private=hidden env=allowlist({env_keys} keys) home=temp"
        )
    return (
        f"untrusted-guard: isolation=env-only env=allowlist({env_keys} keys) home=temp - this does NOT protect ~/.ssh, "
        "the gh hosts.yml token file, the keychain or the network; only inherited variables and home-relative lookups are cleared"
    )


def _resolve_program(cmd: Sequence[str], env: dict[str, str], windows: bool = os.name == "nt") -> list[str]:
    """Return the command with a bare program name resolved through the child's own ``PATH`` on Windows.

    Windows ``CreateProcess`` searches the system directory before ``PATH``, so a bare ``bash`` starts the WSL launcher
    (exit 1 without a distribution) instead of the Bash the caller's ``PATH`` names; POSIX already follows ``env``.
    """
    argv = list(cmd)
    if windows and argv and not os.path.dirname(argv[0]):
        argv[0] = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
    return argv


def execute(cmd: Sequence[str], env: dict[str, str], cwd: str, timeout: int) -> int:
    """Run the command in its own process group and kill the whole group on timeout."""
    kwargs: dict[str, object] = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x200}
    try:
        proc = subprocess.Popen(_resolve_program(cmd, env), env=env, cwd=cwd, **kwargs)  # noqa: S603
    except OSError as exc:
        print(f"untrusted-guard: cannot start command: {exc}", file=sys.stderr)
        return 127
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(proc.pid, 9)
        else:
            proc.kill()
        proc.wait()
        print(f"untrusted-guard: command exceeded {timeout}s and was killed", file=sys.stderr)
        return 124


def run_command(args: argparse.Namespace) -> int:
    """Verify the approval, snapshot the working tree, check what changed since the approved head, and run contained."""
    repo = str(Path(args.repo).resolve())
    _merge_record(args)
    _apply_defaults(args)
    args.cmd = _strip_separator(args.cmd)
    if not args.cmd:
        raise GuardError("run needs a command after --", 2)
    if not args.head or not (args.base or args.whole_tree):
        raise GuardError("run needs --head and --base (or --whole-tree), or an approval record that names them", 2)
    _validate_extra_env(args.env)
    report = build_report(repo, args.head, args.base, args.whole_tree, bound=args.bind_file)
    _check_approval(approval_hash(report, args), args.approval_file)
    workdir = Path(tempfile.mkdtemp(prefix="untrusted-guard-")).resolve()
    try:
        home, tmp, snapshot = workdir / "home", workdir / "tmp", workdir / "tree"
        for directory in (home, tmp, snapshot):
            directory.mkdir()
        copied = snapshot_repo(repo, snapshot, args.max_snapshot_mb * 1024 * 1024)
        delta = check_delta(repo, report, snapshot, copied)
        tier, prefix, mechanism = choose_isolation(
            args.tier, repo, str(snapshot), str(workdir), os.path.expanduser("~"), args.allow_loopback
        )
        if tier != "sandbox" and "unsandboxed" not in args.decision:
            raise GuardError("no usable sandbox and the user has not recorded an unsandboxed decision for this run", 4)
        env = build_env(dict(os.environ), args.env, str(home), str(tmp))
        line = isolation_line(tier, mechanism, len(env), args.allow_loopback)
        level = report["risk"]["level"]  # type: ignore[index]
        print(
            f"{line} risk={level} delta-files={len(delta)} decisions={','.join(sorted(args.decision)) or 'none'}",
            file=sys.stderr,
        )
        if args.report:
            record = {"tier": tier, "mechanism": mechanism, "risk": level, "decisions": sorted(args.decision),
                      "delta_files": len(delta), "scan_digest": report["digest"], "head": report["head"], "line": line}  # fmt: skip
            Path(args.report).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return execute([*prefix, *args.cmd], env, str(snapshot), args.timeout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------------------------


def _add_run_options(p: argparse.ArgumentParser) -> None:
    """Add the options ``bind`` and ``run`` must receive identically: the approval hash covers every one of them."""
    p.add_argument("--env", action="append")
    p.add_argument("--allow-loopback", action="store_true")
    p.add_argument("--tier", choices=("auto", "sandbox", "env-only"))
    p.add_argument("--decision", action="append", choices=("unsandboxed",))
    p.add_argument("--bind-file", action="append")
    p.add_argument("--scope", choices=("command", "run"))
    p.add_argument("cmd", nargs=argparse.REMAINDER)


def _parser() -> argparse.ArgumentParser:
    """Build the three-subcommand CLI."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("scan", "bind", "run"):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        p.add_argument("--head", required=name != "run")
        group = p.add_mutually_exclusive_group(required=name != "run")
        group.add_argument("--base")
        group.add_argument("--whole-tree", action="store_true")
        if name == "scan":
            p.add_argument("--out")
            p.add_argument("--text", action="store_true")
            p.add_argument("--bind-file", action="append")
            p.add_argument("--host-sandbox", action="store_true")
            continue
        if name == "bind":
            p.add_argument("--out", required=True)
        else:
            p.add_argument("--approval-file", required=True)
            p.add_argument("--timeout", type=int, default=900)
            p.add_argument("--max-snapshot-mb", type=int, default=_DEFAULT_SNAPSHOT_MB)
            p.add_argument("--report")
        _add_run_options(p)
    return parser


def _strip_separator(cmd: list[str]) -> list[str]:
    """Drop the ``--`` that separates options from the command."""
    return cmd[1:] if cmd[:1] == ["--"] else cmd


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: dispatch ``scan``, ``bind`` or ``run`` and map a guard failure to its exit status."""
    args = _parser().parse_args(argv)
    try:
        if args.mode == "run":
            return run_command(args)
        if args.mode == "bind":
            _apply_defaults(args)
            args.cmd = _strip_separator(args.cmd)
            if not args.cmd and args.scope != "run":
                raise GuardError("bind needs a command after -- unless --scope run", 2)
            _validate_extra_env(args.env)
        repo = str(Path(args.repo).resolve())
        bound = getattr(args, "bind_file", None) or []
        host_sandbox = bool(getattr(args, "host_sandbox", False))
        report = build_report(
            repo,
            args.head,
            args.base,
            args.whole_tree,
            probe=args.mode == "scan" and not host_sandbox,
            bound=bound,
            host_sandbox=host_sandbox,
        )
        if args.mode == "bind":
            digest = approval_hash(report, args)
            Path(args.out).write_text(approval_record(digest, args), encoding="utf-8")
            print(digest)
            return 0
        if args.out:
            Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(report["prompt_summary"] if args.text else json.dumps(report, indent=2))
        return 0
    except GuardError as exc:
        print(f"untrusted-guard: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
