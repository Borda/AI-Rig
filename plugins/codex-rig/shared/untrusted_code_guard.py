#!/usr/bin/env python
"""Scan contributor code before it runs, then run it in a disposable copy with the maintainer's secrets out of reach.

## Purpose

    Give every workflow that executes pull-request or other contributor code one shared tripwire and one shared
    containment wrapper. ``scan`` lists the files a test command loads on its own, reads the files the contribution
    changed, and reports credential reads, secret-environment access, network and exfiltration endpoints, dynamic
    execution, persistence, destructive commands, agent-instruction edits, hidden Unicode, files no rule can read
    (binaries, symlinks, submodules) and dependency provenance. ``record`` records the user's approval of one exact
    command as a hash. ``run`` executes that command in a disposable snapshot of the working tree, under the strongest
    isolation tier this host proves usable, with an environment allowlist and a fresh home directory.

## Scope

    Trust model: files the base ref already contains are the maintainer's and are listed, not pattern-scanned. A file
    the contribution modifies is scanned whole and every hit counts, since an edit with no hit of its own can redirect
    or unguard the lines it keeps; a hit on a kept line is only labelled as unchanged from the base branch.
    Packages the base ref already declares and the official index supplies are trusted by declared name; their
    contents are never scanned, and a changed lockfile, ``setup.py`` or requirements include is reported because a
    name does not prove where the installed copy came from. A dependency the contribution adds, a direct URL, VCS or
    path reference, an index override or a lookalike of a known name is reported. The scan is a static heuristic,
    never a safety claim: its verdict is ``needs-attention``, ``review`` or ``no-findings``, and obfuscated or
    dynamically built payloads pass it. The risk level is presentation for the user's decision, and this helper never
    lowers isolation because of it: every run uses the strongest tier, and a run below the sandbox needs a recorded
    ``unsandboxed`` decision. That decision is the user's, with one exception a calling workflow owns: after a clean
    scan ``oss:review`` binds ``unsandboxed`` together with ``exact-head`` without asking, so the run is limited to
    the exact bytes the scan read (``oss:resolve`` skips its question after two clean scans but keeps the sandbox).

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
    python untrusted_code_guard.py record --repo DIR --head REF (--base REF | --whole-tree) --out FILE
        [RUN OPTIONS] -- CMD ...
    python untrusted_code_guard.py run --approval-file FILE [--repo DIR] [--head REF (--base REF | --whole-tree)]
        [RUN OPTIONS] [--timeout SEC] [--report FILE] -- CMD ...
    RUN OPTIONS: [--env NAME ...] [--allow-loopback] [--tier auto|sandbox|env-only] [--decision unsandboxed|exact-head]
        [--bind-file FILE ...] [--scope command|run]

    ``--bind-file`` names a file outside the repository that the command runs (an issue snippet, a generated script):
    it is scanned as contributed code and its bytes are hashed into the approval.

    ``--scope command`` (default) binds the approval to the exact argv; ``--scope run`` binds it to the scan, tier,
    loopback, environment and decisions only, for a workflow whose agents run many test commands under one user
    answer. ``record`` writes the options it covers into the approval file, so ``run`` needs only ``--approval-file``
    and the command; an option ``run`` passes that differs from the record fails the hash check.

    ``--decision exact-head`` limits an approval to the exact scanned head: ``run`` refuses (exit 3) when any copied
    file differs from that head, including later commits, deleted files, untracked files and a changed file mode
    (executable bit or symlink, judged as git judges them under ``core.fileMode`` and ``core.symlinks``). Content is
    judged as ``git status`` judges it: line endings and ``ident`` keywords git wrote on checkout (``core.autocrlf``,
    ``core.eol``, the ``text`` and ``eol`` attributes) are the scanned head, while a path under a ``filter`` or
    ``working-tree-encoding`` attribute must match byte for byte, because a smudge filter writes bytes the scan never
    read. ``record`` with this decision applies the same check to the checkout and refuses (exit 3) when it already
    differs, so a caller can ask the user instead of recording an approval no run could use. A workflow that records
    no user decision adds it so a clean scan never authorizes code the scan did not read.

## Outputs

    ``scan`` prints one JSON report (or, with ``--text``, the summary the approval question shows). ``record`` writes
    the approval hash, which covers the scan digest, the command, the bound script bytes, the requested tier, loopback,
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
import ast
import configparser
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tokenize
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
#: One ``git check-attr`` or ``git hash-object`` pass reads every file whose raw bytes differ from the head blob; a CRLF
#: checkout makes that most of the tree, so it gets more time than one plumbing call. Running out refuses the run.
_CONVERSION_TIMEOUT = 300
#: Attributes whose conversion writes working-tree bytes the scan never read: a smudge filter's payload (Git LFS keeps
#: only a pointer in the blob) or a re-encoding of the text. A path carrying one is the head only byte for byte.
_RAW_ONLY_ATTRIBUTES = ("filter", "working-tree-encoding")
_EXCERPT_CHARS = 120
_DEFAULT_SNAPSHOT_MB = 500

#: Basenames a test or build command loads without being told to.
SURFACE_BASENAMES = frozenset(
    {
        "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py", "setup.py",
        "makefile", "gnumakefile", "sitecustomize.py", "usercustomize.py", ".pre-commit-config.yaml",
        "package.json", ".envrc", "justfile", "taskfile.yml", "uv.toml", "pip.conf", "pip.ini", ".npmrc", "pipfile",
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
#: Environment names the scan reports as secret reads; ``--env`` refuses each of them too, so no name the scan
#: flags can reach contributor code through the run's allowlist.
_FLAGGED_ENV_NAMES = (
    r"GH_TOKEN|GITHUB_TOKEN|GITHUB_PAT|ANTHROPIC_API_KEY|OPENAI_API_KEY|AWS_SECRET_ACCESS_KEY"
    r"|AWS_ACCESS_KEY_ID|AWS_SESSION_TOKEN|PYPI_(?:API_)?TOKEN|TWINE_PASSWORD|NPM_TOKEN|HF_TOKEN"
    r"|CLAUDE_CODE_OAUTH_TOKEN|SSH_AUTH_SOCK"
)
_LIFECYCLE_SCRIPTS = ("preinstall", "install", "postinstall", "prepare", "prepack", "pretest", "test", "posttest")
#: Popular package names a new dependency is compared against for lookalikes, beside the base ref's own names.
_KNOWN_NAMES = (
    "numpy", "pandas", "requests", "urllib3", "pytest", "setuptools", "wheel", "pip", "torch", "scipy", "pillow",
    "matplotlib", "flask", "django", "pydantic", "cryptography", "boto3", "pyyaml", "jinja2", "click", "rich",
    "tqdm", "httpx", "attrs", "certifi", "idna", "packaging", "typing-extensions", "six", "colorama",
)  # fmt: skip

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
#: Python install text is data only where it is a comment, a docstring, or a string handed straight to one of these
#: message calls (``raise ImportError("pip install x")``, ``warnings.warn("…")``, ``print("…")``). Any other string
#: counts: a variable that a runner receives later reads the same as the call that runs it. Shell, CI and build files
#: carry the text as code. An exception class or logging handler that itself runs its message is not followed.
_MESSAGE_FUNCTIONS = frozenset({"print", "warn", "exit"})
_MESSAGE_METHODS = frozenset({"warn", "warning", "info", "debug", "error", "critical", "exception", "echo", "exit"})
_COMMENT_MAGIC = re.compile(r"#\s*[!%]")
#: (category, severity, pattern, suffix filter or None for every non-prose file).
_RULES: tuple[tuple[str, str, re.Pattern[str], frozenset[str] | None], ...] = tuple(
    (cat, sev, re.compile(pat, re.IGNORECASE), exts)
    for cat, sev, pat, exts in (
        ("credential_path", "high",
         r"(?<![\w])\.(?:ssh|aws|gnupg|kube|azure|netrc|npmrc|pypirc|git-credentials)\b|\.config[/\\](?:gh|gcloud)\b"
         r"|\bhosts\.yml\b|Library[/\\]Keychains|\bid_(?:rsa|ed25519|ecdsa)\b|\.docker[/\\]config\.json"
         r"|/etc/(?:passwd|shadow)\b|\.claude\.json|\.codex[/\\]auth\.json|\.claude[/\\](?:settings|\.credentials)",
         None),
        ("secret_env", "medium", rf"\b(?:{_FLAGGED_ENV_NAMES})\b", None),
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


#: A unified-diff hunk header; group 1 and 2 are the head-side start line and line count (count 1 when omitted).
_HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def _added_lines(repo: str, base: str, head: str, path: str) -> set[int] | None:
    """Return the head line numbers of ``path`` the contribution added or edited, numbered as git counts (``\\n``).

    The diff is forced to text (``--text``) with textconv and external drivers off, so an attribute or config in the
    checkout cannot hide a hunk. ``None`` means the diff could not be read: the caller then treats every line as
    contributed.
    """
    try:
        raw = _git(
            repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--text",
            base, head, "--", f":(literal){path}",
        )  # fmt: skip
    except GuardError:
        return None
    lines: set[int] = set()
    for match in _HUNK.finditer(raw):
        start, count = int(match.group(1)), 1 if match.group(2) is None else int(match.group(2))
        lines.update(range(start, start + count))
    return lines


def _git_line_numbers(text: str) -> list[int]:
    """Map each ``str.splitlines`` line (1-based index) to the git line holding it, which splits on ``\\n`` only.

    ``splitlines`` also breaks on ``\\r``, form feeds and Unicode separators, so without this map a separator on an
    edited line would shift every later finding onto the wrong git line. Index 0 is unused.
    """
    numbers = [0]
    git_line = 1
    for line in text.splitlines(keepends=True):
        numbers.append(git_line)
        git_line += line.count("\n")
    return numbers


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


def _is_message_call(node: ast.Call) -> bool:
    """Whether a call only reports text: ``print``, ``warn``, ``exit`` or a logging-style method."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _MESSAGE_FUNCTIONS
    return isinstance(func, ast.Attribute) and func.attr in _MESSAGE_METHODS


def _message_spans(text: str) -> dict[int, list[tuple[int, int]]]:
    """Map a line number to the column ranges of a Python file that hold message text, never code.

    Message text is a comment (not a ``# !`` or ``# %`` notebook command), a docstring, and a string written directly as
    an argument of ``raise``'s exception or of a message call. A file Python cannot parse has no message text.
    """
    spans: dict[int, list[tuple[int, int]]] = {}
    lines = text.splitlines()

    def add(first: int, first_col: int, last: int, last_col: int) -> None:
        for number in range(first, last + 1):
            if number > len(lines):
                break
            start = first_col if number == first else 0
            end = last_col if number == last else len(lines[number - 1])
            spans.setdefault(number, []).append((start, end))

    def chars(number: int, offset: int) -> int:
        """Convert a UTF-8 byte offset on one line to a character offset."""
        return len(lines[number - 1].encode("utf-8")[:offset].decode("utf-8", "ignore"))

    def add_string(node: ast.expr) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.end_lineno is not None:
            add(
                node.lineno,
                chars(node.lineno, node.col_offset),
                node.end_lineno,
                chars(node.end_lineno, node.end_col_offset or 0),
            )
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                add_string(part)

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return spans
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr):
            add_string(node.value)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            for arg in node.exc.args:
                add_string(arg)
        elif isinstance(node, ast.Call) and _is_message_call(node):
            for arg in node.args:
                add_string(arg)
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT and not _COMMENT_MAGIC.match(token.string):
                add(token.start[0], token.start[1], token.start[0], token.end[1])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return spans


def _scan_text(path: str, text: str) -> list[dict[str, object]]:
    """Apply every content rule that covers this file and return one finding per hit; prose gets the prose rules."""
    suffix = PurePosixPath(path).suffix.lower()
    prose = suffix in _PROSE_SUFFIXES
    found: list[dict[str, object]] = []
    seen: set[tuple[str, str, int]] = set()
    messages = _message_spans(text) if suffix == ".py" else {}
    for number, line in enumerate(text.splitlines(), start=1):
        for category, severity, pattern, exts in _RULES:
            if (exts is not None and suffix not in exts) or (prose and category not in _PROSE_CATEGORIES):
                continue
            hit = pattern.search(line)
            if hit and category == "package_install" and any(a <= hit.start() < b for a, b in messages.get(number, ())):
                continue
            if hit and (category, severity, number) not in seen:
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


def _file_findings(
    repo: str, base: str | None, head: str, path: str, status: str, text: str
) -> list[dict[str, object]]:
    """Return every finding of one changed file, labelling hits on lines the contribution kept from the base ref.

    The whole file is always scanned and every hit counts toward the verdict: an edit with no hit of its own can
    redirect, unguard or move the lines it keeps. In a modified file whose diff names the contributed lines, a hit on
    any other line gets ``base_line: true`` so the report can say the line is unchanged from the base branch. The label
    never changes the verdict.
    """
    found = _scan_text(path, text)
    if base is None or status != "M" or not found:
        return found
    added = _added_lines(repo, base, head, path)
    if not added:
        return found
    git_line = _git_line_numbers(text)
    for f in found:
        if git_line[int(f["line"])] not in added:  # type: ignore[call-overload]
            f["base_line"] = True
    return found


def _scan_files(
    repo: str, base: str | None, head: str, changed: dict[str, str], entries: dict[str, tuple[str, str]]
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
        findings.extend(_file_findings(repo, base, head, path, changed[path], data.decode("utf-8", "replace")))
    return findings, skipped, scanned


# --- dependency provenance -------------------------------------------------------------------------------------------

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_DIRECT_REF = re.compile(r"@|(?:git|hg|svn|bzr)\+|://|^\s*(?:\.{1,2}/|/|file:)", re.IGNORECASE)
#: Options pip reads from a requirements file, by canonical long spelling: the class this guard gives each and whether
#: it takes a value. ``route`` sends names to another index or source (or trusts another host), ``include`` reads
#: another file, ``editable`` names a local or direct project, ``neutral`` only picks wheels, pre-releases or hashes.
#: The table mirrors pip's own requirements-file option set, so a prefix that is unique here is unique for pip too.
_REQUIREMENTS_OPTIONS: dict[str, tuple[str, bool]] = {
    "--index-url": ("route", True), "--pypi-url": ("route", True), "--extra-index-url": ("route", True),
    "--find-links": ("route", True), "--trusted-host": ("route", True), "--no-index": ("route", False),
    "--requirement": ("include", True), "--constraint": ("include", True), "--editable": ("editable", True),
    "--no-binary": ("neutral", True), "--only-binary": ("neutral", True), "--prefer-binary": ("neutral", False),
    "--require-hashes": ("neutral", False), "--no-require-hashes": ("neutral", False), "--pre": ("neutral", False),
    "--all-releases": ("neutral", True), "--only-final": ("neutral", True), "--use-feature": ("neutral", True),
    "--hash": ("neutral", True), "--config-settings": ("neutral", True), "--global-option": ("neutral", True),
}  # fmt: skip
#: Short spellings pip accepts in a requirements file; every one takes a value, attached (``-iURL``) or separate.
_REQUIREMENTS_SHORT_OPTIONS = {
    "-i": "--index-url", "-f": "--find-links", "-r": "--requirement", "-c": "--constraint", "-e": "--editable",
    "-C": "--config-settings",
}  # fmt: skip
#: A comment as pip strips it: ``#`` at the line start or after whitespace (a URL fragment keeps its ``#``).
_REQUIREMENTS_COMMENT = re.compile(r"(^|\s+)#.*$")
_REQUIREMENTS_ENCODING = re.compile(r"coding[:=][ \t]*([-\w.]+)", re.ASCII)
_REQUIREMENTS_VARIABLE = re.compile(r"\$\{[A-Z0-9_]+\}")
_DependencyDeclaration = tuple[str, str, bool]


def requirements_logical_lines(text: str) -> list[str]:
    r"""Return a requirements file's logical lines as pip reads them: continuations joined, comments and blanks dropped.

    A line ending in a backslash continues on the next physical line unless it is a comment, so the ``--hash`` options
    a pip-compile file puts on continuation lines belong to the requirement above them.

    Examples:
        >>> requirements_logical_lines("requests==2.32.3 \\\n    --hash=sha256:aa\n# note\nidna==3.7  # pinned\n")
        ['requests==2.32.3     --hash=sha256:aa', 'idna==3.7']
    """
    lines: list[str] = []
    pending = ""
    for raw in text.removeprefix("\ufeff").splitlines():
        comment = _REQUIREMENTS_COMMENT.match(raw)
        if raw.endswith("\\") and not comment:
            pending += raw.strip("\\")
            continue
        line = _REQUIREMENTS_COMMENT.sub("", pending + (" " if comment else "") + raw).strip()
        pending = ""
        if line:
            lines.append(line)
    tail = _REQUIREMENTS_COMMENT.sub("", pending).strip()
    return lines + ([tail] if tail else [])


def _canonical_requirements_option(token: str) -> tuple[str, str | None]:
    """Return a token's canonical option and its attached value (``None`` when the value is the next token).

    A long option resolves by exact name or, as Python's option parser does, by a prefix that names one option; an
    unknown or ambiguous one is returned unchanged.
    """
    if token.startswith("--"):
        name, equals, value = token.partition("=")
        if name not in _REQUIREMENTS_OPTIONS:
            matches = [option for option in _REQUIREMENTS_OPTIONS if option.startswith(name)]
            name = matches[0] if len(matches) == 1 and len(name) > 2 else name
        return name, (value if equals else None)
    short = _REQUIREMENTS_SHORT_OPTIONS.get(token[:2])
    return (short, token[2:] or None) if short else (token, None)


def requirements_line_options(line: str) -> list[tuple[str, str, str]]:
    """Return ``(option, kind, value)`` for each option on one requirements-file option line, as pip reads them.

    ``option`` is the canonical long spelling for every form pip's option parser accepts: a short option with an
    attached or separate value (``-iURL``, ``-i URL``), a long option with ``=`` or a separate value, or a long-option
    prefix that names one option (``--extra-index``). ``kind`` is ``route``, ``include``, ``editable`` or
    ``neutral``. An option pip does not define, or an ambiguous prefix, keeps its own spelling and is ``route``, so an
    unmodelled spelling is never read as harmless.

    Examples:
        >>> requirements_line_options("-ihttps://mirror.invalid/simple")
        [('--index-url', 'route', 'https://mirror.invalid/simple')]
        >>> requirements_line_options("--pre --extra-index=https://mirror.invalid/simple")
        [('--pre', 'neutral', ''), ('--extra-index-url', 'route', 'https://mirror.invalid/simple')]
        >>> requirements_line_options("-r base.txt")
        [('--requirement', 'include', 'base.txt')]
        >>> requirements_line_options("--frobnicate")
        [('--frobnicate', 'route', '')]
    """
    try:
        tokens = shlex.split(line)
    except ValueError:
        return [("", "route", "")]
    options: list[tuple[str, str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.startswith("-"):
            continue  # a stray value; its option, if any, was already classified
        option, value = _canonical_requirements_option(token)
        kind, takes_value = _REQUIREMENTS_OPTIONS.get(option, ("route", False))
        if takes_value and value is None and index < len(tokens):
            value, index = tokens[index], index + 1
        options.append((option, kind, value or ""))
    return options


def requirements_line_kinds(line: str) -> set[str]:
    """Return the option classes on one logical requirements-file line; a requirement line has none.

    Examples:
        >>> sorted(requirements_line_kinds("-rbase.txt --no-index"))
        ['include', 'route']
        >>> requirements_line_kinds("numpy>=1 --hash=sha256:aa")
        set()
    """
    if line.startswith("-"):
        return {kind for _, kind, _ in requirements_line_options(line)} or {"route"}
    return set()


def requirement_part(line: str) -> str:
    """Return the requirement of a logical requirement line without its per-requirement options.

    pip splits such a line at the first token that starts with ``-``: what precedes it is the requirement, and the
    ``--hash`` or ``--config-settings`` options that follow apply only to that requirement.

    Examples:
        >>> requirement_part("idna==3.7 --hash=sha256:cc --hash=sha256:dd")
        'idna==3.7'
        >>> requirement_part("numpy>=1.21; python_version >= '3.10'")
        "numpy>=1.21; python_version >= '3.10'"
    """
    return re.split(r"\s+(?=-)", line, maxsplit=1)[0].strip()


def _norm(name: str) -> str:
    """Normalize a distribution name the way PEP 503 does, so ``Foo_Bar`` and ``foo-bar`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement(line: str) -> tuple[str, str, bool] | None:
    """Split one requirement string into ``(name, spec, is_direct_reference)``; ``None`` for non-requirements."""
    text = _REQUIREMENTS_COMMENT.sub("", line).strip()
    if not text or text.startswith("-"):
        return None
    match = _REQ_NAME.match(text)
    if not match:
        return None
    rest = text[match.end() :]
    return _norm(match.group(1)), rest.strip(), bool(_DIRECT_REF.search(rest) or _DIRECT_REF.search(text[:1]))


def _named_requirement(line: str) -> _DependencyDeclaration | None:
    """Read a recognizable named requirement without treating paths or URLs as distribution names."""
    req = _requirement(line)
    if req is None:
        return None
    _, rest, _ = req
    if rest and not rest.startswith(("[", "(", ";", "@", "<", ">", "=", "!", "~")):
        return None
    if line.lower().endswith((".whl", ".zip", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")) and (
        not rest or rest[0] not in "([@<>=!~"
    ):
        return None
    return req


def _preferred_declaration(previous: tuple[str, bool] | None, declared: tuple[str, bool]) -> tuple[str, bool]:
    """Keep the latest declaration while giving direct references priority over plain specs.

    Examples:
        >>> _preferred_declaration(("file:../demo", True), ("^2.0", False))
        ('file:../demo', True)
        >>> _preferred_declaration(("^1.0", False), ("^2.0", False))
        ('^2.0', False)
    """
    return previous if previous and previous[1] and not declared[1] else declared


def _retained_declarations(declarations: Iterable[_DependencyDeclaration]) -> dict[str, tuple[str, bool]]:
    """Reduce declarations to one preferred spec per name.

    Examples:
        >>> _retained_declarations([("demo", "^1.0", False), ("demo", "file:../demo", True)])
        {'demo': ('file:../demo', True)}
    """
    deps: dict[str, tuple[str, bool]] = {}
    for name, spec, is_direct in declarations:
        deps[name] = _preferred_declaration(deps.get(name), (spec, is_direct))
    return deps


def _requirements_file(text: str) -> tuple[list[_DependencyDeclaration], list[str]]:
    """Read all requirement lines and relevant option lines in a requirements file.

    Lines are read as pip reads them (continuations joined, per-requirement options dropped). An option line marks its
    routing, include or editable classes; an option this guard does not model counts as routing.
    """
    declarations: list[_DependencyDeclaration] = []
    marks: list[str] = []
    if not _requirements_utf8(text):
        return [], ["unmodeled requirements input"]
    for line in requirements_logical_lines(text):
        if _REQUIREMENTS_VARIABLE.search(line):
            marks.append(f"unmodeled requirements input: {line}")
            continue
        kinds = requirements_line_kinds(line)
        if "route" in kinds:
            marks.append(line)
        if "include" in kinds:
            marks.append(f"include: {line}")
        if "editable" in kinds:
            marks.append(f"editable: {line}")
        if kinds:
            continue
        req = _named_requirement(requirement_part(line))
        if req:
            declarations.append(req)
        else:
            marks.append(f"unmodeled requirements input: {line}")
    return declarations, marks


def _requirements_utf8(text: str) -> bool:
    """Refuse a file that pip might interpret using a different encoding."""
    headers = text.removeprefix("\ufeff").split("\n", 2)[:2]
    return "\0" not in text and all(
        match[1].lower().replace("_", "-") in {"utf-8", "utf8"}
        for line in headers
        if line.lstrip().startswith("#") and (match := _REQUIREMENTS_ENCODING.search(line))
    )


def _read_requirements_txt(text: str) -> tuple[list[_DependencyDeclaration], set[str]]:
    """Read requirements and distinct routing marks for callers that only need their presence."""
    declarations, marks = _requirements_file(text)
    return declarations, set(marks)


def _from_requirements_txt(text: str) -> tuple[dict[str, tuple[str, bool]], set[str]]:
    """Return retained requirements and option marks from a requirements file."""
    declarations, marks = _read_requirements_txt(text)
    return _retained_declarations(declarations), marks


def _toml_loads(text: str) -> dict[str, object] | None:
    """Parse TOML or return ``None`` when the input or reader is unavailable."""
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
        return None


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


_UV_NON_ROUTING = frozenset(
    {
        "dev-dependencies",
        "package",
        "managed",
        "required-version",
        "environments",
        "cache-keys",
        "workspace",
        "conflicts",
    }
)
UV_SOURCE_MARK = "uv-source: "
# Explicit indexes can be reported without treating unbound packages as routed away from PyPI.
UV_EXPLICIT_INDEX_MARK = f"{UV_SOURCE_MARK}[explicit index] "
_RoutingMarks = set[tuple[str, str]]


def _routing_fingerprint(value: object) -> str:
    """Fingerprint a parsed setting without disclosing its configured value."""
    return _sha256(json.dumps(value, sort_keys=True, default=str).encode())


def _explicit_uv_indexes(value: object) -> bool:
    """Check whether an index array leaves the default index available to unbound names."""
    return isinstance(value, list) and all(
        isinstance(index, dict) and index.get("explicit") is True and index.get("default", False) is False
        for index in value
    )


def _uv_routing(settings: object, scope: str, *, allow_pip: bool = True) -> _RoutingMarks:
    """Mark unknown uv settings globally and well-formed source bindings by distribution name."""
    if not isinstance(settings, dict):
        return {(f"{scope}: invalid settings", _routing_fingerprint(settings))}
    marks = set()
    for key in settings.keys() - _UV_NON_ROUTING:
        value = settings[key]
        if key == "pip" and allow_pip:
            marks.update(_uv_routing(value, f"{scope}.pip", allow_pip=False))
        elif key == "sources" and isinstance(value, dict) and all(_REQ_NAME.fullmatch(name.strip()) for name in value):
            # Keep every binding here. Another manifest can declare the name selected by the gate.
            marks.update(
                (f"{UV_SOURCE_MARK}{_norm(name.strip())}", _routing_fingerprint(binding))
                for name, binding in value.items()
            )
        else:
            prefix = UV_EXPLICIT_INDEX_MARK if key == "index" and _explicit_uv_indexes(value) else ""
            marks.add((f"{prefix}{scope}.{key}", _routing_fingerprint(value)))
    return marks


# Only known non-installer tables can be ignored when selecting the default index.
_NON_INSTALLER_TOOLS = frozenset(
    {
        "bandit", "black", "bumpversion", "check-manifest", "cibuildwheel", "codespell", "commitizen",
        "coverage", "flake8", "interrogate", "isort", "mdformat", "mypy", "nbqa", "poe", "pre-commit",
        "pycln", "pydocstyle", "pylint", "pyproject-fmt", "pyright", "pytest", "pytest-ini", "ruff",
        "semantic_release", "setuptools", "setuptools_scm", "sphinx-pyproject", "sqlfluff", "towncrier",
        "vulture", "yapf",
    }
)  # fmt: skip
_NON_INSTALLER_SECTIONS = {"hatch": {"version", "build", "metadata", "publish"}, "pdm": {"version"}}


def _pyproject_routing(data: dict[str, object]) -> _RoutingMarks:
    """Mark tool settings unless uv models their routing or the tool is known to leave it alone."""
    tools = data.get("tool", {})
    if not isinstance(tools, dict):
        return {("[tool]: invalid settings", _routing_fingerprint(tools))}
    marks = _uv_routing(tools.get("uv", {}), "[tool.uv]")
    for name, settings in tools.items():
        if name == "uv" or (name in _NON_INSTALLER_TOOLS and isinstance(settings, dict)):
            continue
        sections = _NON_INSTALLER_SECTIONS.get(name)
        if (
            sections
            and isinstance(settings, dict)
            and settings
            and settings.keys() <= sections
            and all(isinstance(value, dict) for value in settings.values())
        ):
            continue
        marks.add((f"[tool.{name}]: installer settings are not modeled", _routing_fingerprint(settings)))
    return marks


def _read_pyproject(text: str) -> tuple[list[_DependencyDeclaration], _RoutingMarks] | None:
    """Read the dependency declarations and routing marks of a parsed project."""
    data = _toml_loads(text)
    if data is None:
        return None
    project, uv = _table(data.get("project")), _table(_table(data.get("tool")).get("uv"))
    sources = [
        project.get("dependencies"), project.get("optional-dependencies"), data.get("dependency-groups"),
        _table(data.get("build-system")).get("requires"), uv.get("dev-dependencies"),
    ]  # fmt: skip
    declarations: list[_DependencyDeclaration] = []
    marks = _pyproject_routing(data)
    for entry in sources:
        for line in _strings(entry):
            req = _named_requirement(line)
            if req:
                declarations.append(req)
            elif line.strip():
                marks.add(("pyproject.toml: unmodeled dependency declaration", _routing_fingerprint(line)))
    return declarations, marks


def _from_pyproject(text: str) -> tuple[dict[str, tuple[str, bool]], set[str]] | None:
    """Return retained project dependencies and overrides if a TOML reader is available."""
    result = _read_pyproject(text)
    if result is None:
        return None
    declarations, marks = result
    return _retained_declarations(declarations), {label for label, _ in marks}


def _read_setup_cfg(text: str) -> list[_DependencyDeclaration] | None:
    """Read all install, setup and extras requirements, or return None if setup.cfg is invalid."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    blobs = [parser.get("options", key, fallback="") for key in ("install_requires", "setup_requires")]
    if parser.has_section("options.extras_require"):
        blobs += [parser.get("options.extras_require", key) for key in parser["options.extras_require"]]
    declarations: list[_DependencyDeclaration] = []
    for line in "\n".join(blobs).splitlines():
        text_line = _REQUIREMENTS_COMMENT.sub("", line).strip()
        if not text_line:
            continue
        req = _named_requirement(text_line)
        if req is None:
            return None
        declarations.append(req)
    return declarations


def _from_setup_cfg(text: str) -> dict[str, tuple[str, bool]]:
    """Return retained dependencies from setup.cfg."""
    return _retained_declarations(_read_setup_cfg(text) or [])


def _read_package_json(text: str) -> tuple[list[_DependencyDeclaration], dict[str, str]]:
    """Read all npm dependency declarations and lifecycle scripts."""
    try:
        data = json.loads(text)
    except ValueError:
        return [], {}
    data = _table(data)
    declarations: list[_DependencyDeclaration] = []
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, spec in _table(data.get(key)).items():
            value = str(spec)
            direct = bool(re.match(r"^(?:git|github:|file:|link:|https?:|\.{0,2}/|[\w.-]+/[\w.-]+$)", value))
            declarations.append((name.lower(), value, direct))
    scripts = _table(data.get("scripts"))
    return declarations, {k: str(v) for k, v in scripts.items() if k in _LIFECYCLE_SCRIPTS}


def _package_json(text: str) -> tuple[dict[str, tuple[str, bool]], dict[str, str]]:
    """Return retained npm dependencies and lifecycle scripts from package.json."""
    declarations, scripts = _read_package_json(text)
    return _retained_declarations(declarations), scripts


_PIP_CONFIGS = frozenset({"pip.conf", "pip.ini"})
_NEUTRAL_PIP_OPTIONS = frozenset(
    {"break-system-packages", "cache-dir", "disable-pip-version-check", "log", "no-cache-dir", "no-color",
     "no-input", "progress-bar", "quiet", "require-virtualenv", "retries", "timeout", "verbose"}
)  # fmt: skip
_INSTALLER_CONFIGS = _PIP_CONFIGS | {"uv.toml", "pipfile"}
_UNMODELED_ROOT_CONFIGS = frozenset({"pdm.toml", ".pdm.toml", "pixi.toml", "poetry.toml"})


def _pip_config_routing(name: str, text: str) -> tuple[_RoutingMarks, bool]:
    """Read pip routing options from any section, normalizing option names as pip does."""
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error:
        return {(f"{name}: unparsed", "")}, False
    marks = set()
    for section in [parser.default_section, *parser.sections()]:
        for key, value in parser[section].items():
            option = key.replace("_", "-").removeprefix("--")
            if option not in _NEUTRAL_PIP_OPTIONS:
                marks.add((f"{name} [{section}].{option}", _routing_fingerprint(value)))
    return marks, True


def _pipfile_routing(data: dict[str, object]) -> _RoutingMarks:
    """Fingerprint source declarations and package specs beyond plain version requirements."""
    marks = {
        ("Pipfile: index or source settings", _routing_fingerprint([key, data[key]]))
        for key in data.keys() & {"source", "sources", "index", "indexes"}
    }
    # Pipenv permits custom dependency groups. Inspect package values, never the package names.
    for group, specs in data.items():
        if group in {"requires", "scripts", "pipenv"}:
            continue
        for name, spec in _table(specs).items():
            routing = {key: value for key, value in _table(spec).items() if key not in {"version", "extras", "markers"}}
            if routing:
                marks.add(("Pipfile: index or source settings", _routing_fingerprint([group, name, routing])))
    return marks


def _installer_config_routing(name: str, text: str) -> tuple[_RoutingMarks, bool]:
    """Read routing in a standalone installer config, marking parser failures globally."""
    if name in _UNMODELED_ROOT_CONFIGS:
        return {(f"{name}: installer settings are not modeled", _routing_fingerprint(text))}, True
    if name == ".npmrc":
        return {
            (f".npmrc {match[1]}", _routing_fingerprint(match[2].strip()))
            for line in text.splitlines()
            if (
                match := re.fullmatch(
                    r"\s*((?:@[^\s:=]+:)?(?:index-url|extra-index-url|find-links|registry))\s*=(.*)", line, re.I
                )
            )
        }, True
    if name in _PIP_CONFIGS:
        return _pip_config_routing(name, text)
    data = _toml_loads(text)
    if data is None:
        return {(f"{name}: unparsed", "")}, False
    if name == "uv.toml":
        return _uv_routing(data, name), True
    return _pipfile_routing(data), True


def requirement_routing_label(mark: str) -> str:
    """Describe a requirements option without its potentially credential-bearing value."""
    if mark.startswith("include: "):
        return mark if re.fullmatch(r"include: [-\w.]+[ =]?[\w./-]+", mark, re.ASCII) else "include: requirement file"
    if mark.startswith("editable: "):
        return "editable: requirement"
    if mark.startswith("unmodeled requirements input"):
        return "unmodeled requirements input"
    options = [option for option, _, _ in requirements_line_options(mark) if option in _REQUIREMENTS_OPTIONS]
    return "requirements option" + (" " + ", ".join(options) if options else "")


def is_requirements_file(path: str) -> bool:
    """Recognize conventional requirements files and text inputs in requirements directories."""
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1] if parts else ""
    return name.endswith((".txt", ".in")) and (name.startswith("requirements") or "requirements" in parts[:-1])


def _read_dependency_file(path: str, text: str) -> tuple[list[_DependencyDeclaration], _RoutingMarks, bool]:
    """Read every declaration and option mark before reducing specs to one per name."""
    name = PurePosixPath(path).name.lower()
    if name == "pyproject.toml":
        got = _read_pyproject(text)
        return (got[0], got[1], True) if got else ([], {(f"{name}: unparsed", "")}, False)
    if name == "setup.cfg":
        declarations = _read_setup_cfg(text)
        return ([], {(f"{name}: unparsed", "")}, False) if declarations is None else (declarations, set(), True)
    if name == "package.json":
        return _read_package_json(text)[0], set(), True
    if name in _INSTALLER_CONFIGS or name == ".npmrc" or path.lower() in _UNMODELED_ROOT_CONFIGS:
        marks, parsed = _installer_config_routing(name, text)
        return [], marks, parsed
    if is_requirements_file(path):
        declarations, marks = _requirements_file(text)
        fingerprint = _routing_fingerprint(marks)
        return declarations, {(requirement_routing_label(mark), fingerprint) for mark in marks}, True
    return [], set(), True


def _parse_dependency_file(path: str, text: str) -> tuple[dict[str, tuple[str, bool]], _RoutingMarks, bool]:
    """Return the retained dependencies, marks and parser availability for one file."""
    declarations, marks, parsed = _read_dependency_file(path, text)
    return _retained_declarations(declarations), marks, parsed


def _direct_specs_in_file(path: str, text: str) -> dict[str, set[str]]:
    r"""Return every distinct direct spec declared for each name in one file.

    Examples:
        >>> text = "demo @ https://example.invalid/demo.whl\ndemo @ ./demo\ndemo>=2"
        >>> sorted(_direct_specs_in_file("requirements.txt", text)["demo"])
        ['@ ./demo', '@ https://example.invalid/demo.whl']
    """
    declarations, _, _ = _read_dependency_file(path, text)
    direct: dict[str, set[str]] = {}
    for name, spec, is_direct in declarations:
        if is_direct:
            direct.setdefault(name, set()).add(spec)
    return direct


def is_dependency_manifest(path: str) -> bool:
    """Return whether ``path`` declares or pins dependencies (manifests, lockfiles, ``setup.py``)."""
    name = PurePosixPath(path).name.lower()
    return (
        name in {"pyproject.toml", "setup.cfg", "setup.py", "package.json", ".npmrc"}
        or name in _INSTALLER_CONFIGS
        or path.lower() in _UNMODELED_ROOT_CONFIGS
        or name in LOCKFILES
        or is_requirements_file(path)
    )


_ROUTING_PATTERN = (
    r"index[-_]?url|pypi[-_]url|find[-_]links|no[-_]index|--(default-)?index([[:space:]]|=)"
    r"|--(build-constraint|constraint|requirement)([[:space:]]|=)|PIP_(CONSTRAINT|REQUIREMENT|TRUSTED_HOST)"
    r"|(pip|uv)_config_file|uv_(default_)?index|\[\[(source|tool\.[a-z0-9_.-]+\.(index|source))\]\]"
    r"|^[[:space:]]*-[ifrc]([[:space:]]|=|[a-z0-9./~])"
    r"|(pip|uv)[^#]*[[:space:]]-[ifrc]([[:space:]]|=|[a-z0-9./~])"
)


def project_routing_scan(repo: str, sha: str, modeled: set[str]) -> set[str]:
    """Flag installer directives outside files whose routing has already been checked."""
    try:
        out = _git(
            repo, "grep", "--name-only", "-I", "-i", "-E", "-z", "--no-textconv", "-e", _ROUTING_PATTERN, sha, "--"
        )
    except GuardError as error:
        cause = error.__cause__
        if isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 1:
            return set()
        return {"project routing scan failed"}
    paths = {entry.partition(b":")[2].decode("utf-8", "surrogateescape") for entry in out.split(b"\0") if entry}
    return {f"{path}: installer routing outside modeled declarations" for path in paths - modeled}


def _dependency_ecosystem(path: str) -> str:
    """Identify the ecosystem of the declarations this manifest reader returns."""
    return "npm" if PurePosixPath(path).name.lower() == "package.json" else "python"


def _collect(
    repo: str, sha: str | None, paths: Sequence[str]
) -> tuple[dict[tuple[str, str], tuple[str, bool]], set[tuple[str, str, str]], list[str]]:
    """Merge declarations by ecosystem and name, and scope routing fingerprints to their manifest paths."""
    deps: dict[tuple[str, str], tuple[str, bool]] = {}
    marks: set[tuple[str, str, str]] = set()
    unparsed: list[str] = []
    for path in paths if sha else []:
        data = _blob(repo, sha, path)
        if data is None:
            continue
        try:
            got, over, parsed = _parse_dependency_file(path, data.decode("utf-8-sig"))
        except UnicodeDecodeError:
            got, over, parsed = {}, {("unparsed", "")}, False
        for name, declared in got.items():
            key = (_dependency_ecosystem(path), name)
            deps[key] = _preferred_declaration(deps.get(key), declared)
        marks.update((path, label, fingerprint) for label, fingerprint in over)
        if not parsed:
            unparsed.append(path)
    return deps, marks, unparsed


def _direct_declarations(repo: str, sha: str | None, paths: Sequence[str]) -> dict[tuple[str, str], set[str]]:
    """Return every direct spec declared for each ecosystem and name at ``sha``."""
    direct: dict[tuple[str, str], set[str]] = {}
    for path in paths if sha else []:
        data = _blob(repo, sha, path)
        if data is None:
            continue
        for name, specs in _direct_specs_in_file(path, data.decode("utf-8-sig", "replace")).items():
            direct.setdefault((_dependency_ecosystem(path), name), set()).update(specs)
    return direct


def _local_include(parent: str, value: str) -> str | None:
    """Resolve a literal in-tree include without opening a URL or a host file."""
    value = value.replace("\\", "/")
    if not value or value.startswith(("/", "~")) or ":" in value or _REQUIREMENTS_VARIABLE.search(value):
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(parent), value))
    return None if path in {".", ".."} or path.startswith("../") else path


def _included_requirements(repo: str, sha: str, roots: Sequence[str]) -> set[str]:
    """Discover literal local include targets recursively, irrespective of their names."""
    seen: set[str] = set()
    included: set[str] = set()
    pending = [path for path in roots if is_requirements_file(path)]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        data = _blob(repo, sha, path)
        if not data:
            continue
        for target in _requirement_includes(path, data.decode("utf-8-sig", "replace")):
            included.add(target)
            pending.append(target)
    return included


def _requirement_includes(path: str, text: str) -> Iterable[str]:
    """Yield in-tree includes from parsed pip option lines."""
    for line in requirements_logical_lines(text):
        if line.startswith("-"):
            for _, kind, value in requirements_line_options(line):
                if kind == "include" and (target := _local_include(path, value)) is not None:
                    yield target


def _changed_includes(repo: str, base: str, head: str, roots: Sequence[str]) -> set[str]:
    """Identify changed or removed targets even when neither include directive changed."""
    targets = _included_requirements(repo, base, roots) | _included_requirements(repo, head, roots)
    return {path for path in targets if _blob(repo, base, path) != _blob(repo, head, path)}


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


def _ecosystem_lookalikes(added: set[tuple[str, str]], known: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Compare lookalike names within each package ecosystem."""
    return sorted(
        {
            pair
            for ecosystem in ("python", "npm")
            for pair in _lookalikes(
                (name for kind, name in added if kind == ecosystem),
                (name for kind, name in known if kind == ecosystem),
            )
        }
    )


def _dependency_source_findings(
    routes: set[tuple[str, str, str]], overrides: list[str], changed: dict[str, str], changed_includes: set[str]
) -> list[dict[str, object]]:
    """Report changes to modeled sources and flag changed files whose sources are not compared."""
    findings = [
        _finding("dependency_index_override", "high", path, f"index or source override added or changed: {label}")
        for path, label in sorted({(path, label) for path, label, _ in routes if label in overrides})
    ]
    for path in sorted(changed):
        if PurePosixPath(path).name.lower() in LOCKFILES | {"setup.py"}:
            findings.append(
                _finding(
                    "dependency_source_unchecked",
                    "medium",
                    path,
                    "changed lockfile or setup.py: resolved sources are not compared",
                )
            )
    findings.extend(
        _finding(
            "dependency_source_unchecked", "medium", path, "included requirements changed: sources are not compared"
        )
        for path in sorted(changed_includes)
    )
    return findings


def _dependency_report(
    repo: str, base: str | None, head: str, tree: Sequence[str], changed: dict[str, str]
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Compare declared dependencies between the base and the head and report what the contribution introduces."""
    paths = set(tree) | (_tree_entries(repo, base).keys() if base else set())
    files = sorted(p for p in paths if is_dependency_manifest(p) and PurePosixPath(p).name.lower() not in LOCKFILES)
    head_deps, head_marks, unparsed = _collect(repo, head, files)
    base_deps, base_marks, _ = _collect(repo, base, files)
    findings = [
        _finding("dependency_unparsed", "medium", p, "could not parse dependency manifest: changes not compared")
        for p in unparsed
    ]
    added_keys = head_deps.keys() - base_deps.keys()
    added = sorted({name for _, name in added_keys})
    base_direct = _direct_declarations(repo, base, files)
    direct_keys = {
        key for key, specs in _direct_declarations(repo, head, files).items() if specs - base_direct.get(key, set())
    }
    direct = sorted({name for _, name in direct_keys})
    removed_routes = {route for route in base_marks - head_marks if is_requirements_file(route[0])}
    new_routes = (head_marks - base_marks) | removed_routes
    new_marks = sorted({label for _, label, _ in new_routes})
    includes = [m for m in new_marks if m.startswith("include: ")]
    overrides = [m for m in new_marks if m not in includes]
    lookalikes = _ecosystem_lookalikes(added_keys, base_deps)
    findings += [
        _finding("dependency_new", "medium", "dependencies", f"new dependency {n} - verify provenance") for n in added
    ]
    findings += [
        _finding("dependency_non_index", "high", "dependencies", f"{n} uses a direct URL, VCS or path reference")
        for n in direct
    ]
    changed_includes = (
        _changed_includes(repo, base, head, files)
        if base and any(label.startswith("include: ") for _, label, _ in head_marks | base_marks)
        else set()
    )
    findings += _dependency_source_findings(new_routes, overrides, changed, changed_includes)
    findings += [
        _finding("dependency_include", "medium", "dependencies", f"{m} - the included file's contents are not followed")
        for m in includes
    ]
    findings += [_finding("dependency_lookalike", "high", "dependencies", f"{a} looks like {b}") for a, b in lookalikes]
    report = {
        "added": added,
        "non_index_references": direct,
        "index_overrides": overrides,
        "includes": includes,
        "lookalikes": [f"{a}~{b}" for a, b in lookalikes],
        "trusted_unchanged": len((head_deps.keys() & base_deps.keys()) - direct_keys),
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


#: One plain sentence per finding category, so a question can say why code is not treated as safe without jargon.
_PLAIN_REASON = {
    "credential_path": "it reads a place where passwords or keys are kept",
    "secret_env": "it reads a secret environment variable such as a token",
    "env_dump": "it reads or copies the whole environment, which can hold secrets",
    "exfil_endpoint": "it names a site that is used to receive stolen data",
    "pipe_to_shell": "it downloads a script and runs it at once",
    "network_tool": "it calls a network download tool such as curl or wget",
    "dynamic_exec": "it runs shell commands or code that is built or decoded while it runs",
    "obfuscation": "it holds text that looks deliberately scrambled",
    "persistence": "it sets something up that keeps running after the tests end",
    "credential_use": "it uses the saved GitHub or git login",
    "remote_write": "it pushes or changes something on GitHub or a package index",
    "destructive": "it deletes files outside the project",
    "hidden_unicode": "it hides invisible characters in text",
    "prompt_injection_text": "it holds text written to steer an AI reviewer",
    "lifecycle_script": "it changes a script that runs when packages are installed",
    "structural": "it adds a file the scan could not read or an executable of unknown type",
    "agent_instruction_change": "it changes a file that AI agents load as instructions",
    "dependency_new": "it adds a new package that will be installed",
    "dependency_non_index": "it installs a package from a web address or a path, not the package index",
    "dependency_index_override": "it points package installs at a different source",
    "dependency_include": "it includes another requirements file the scan does not follow",
    "dependency_lookalike": "it adds a package whose name imitates a well-known one",
    "dependency_unparsed": "the scan could not compare its package changes",
    "dependency_source_unchecked": "it changes a lock or setup file whose sources were not checked",
    "network_import": "it imports a library that can reach the network",
    "package_install": "it installs packages while it runs",
}


def assess_risk(findings: Sequence[dict[str, object]]) -> dict[str, object]:
    """Turn findings into a risk level and a recommendation for the user's decision; it never changes isolation.

    Levels: ``none`` (no findings), ``borderline`` (low or medium only: unusual but not rejected, run it in the
    sandbox; every low category is a capability such as a network import, a shell call or a package install, and the
    scan is a text heuristic, so ``none`` means no known pattern matched, never proof that the code is safe), ``high``
    (a high finding: decide on the evidence) and ``likely-malicious`` (an
    attack-shaped combination: the recommendation is not to run it, and choosing to run is the user's explicit
    override). ``reasons`` holds one plain sentence per cause, most severe first, shown beside the question.
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
    causes: list[str] = []
    for f in sorted(findings, key=lambda item: _SEVERITY_ORDER[str(item["severity"])]):
        cause = f"{f['path']}: {_PLAIN_REASON.get(str(f['category']), str(f['category']))}"
        if f.get("base_line"):
            cause += " (on a line unchanged from the base branch)"
        if cause not in causes:
            causes.append(cause)
    return {"level": level, "recommendation": advice, "reasons": (shapes + causes)[:5]}


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
    findings, skipped, scanned = _scan_files(repo, base, head, changed, entries)
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
        raise GuardError("approval missing: the parent must show the scan, ask, then run record", 3) from None
    if recorded != expected:
        raise GuardError("approval does not match this scan, command, tier, loopback, env and decisions", 3)


def _safe_relative(rel: str) -> bool:
    """Return whether a git path is a plain relative path inside the repository."""
    parts = PurePosixPath(rel).parts
    return bool(parts) and not PurePosixPath(rel).is_absolute() and ".." not in parts and ".git" not in parts


def listed_files(repo: str) -> list[str]:
    """Return every tracked and untracked-but-not-ignored regular file or symlink of the working tree (no ``.git``).

    These are the paths ``snapshot_repo`` copies; a submodule checkout or other directory is never listed.
    """
    listing = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode(
        "utf-8", "surrogateescape"
    )
    files = []
    for rel in (p for p in listing.split("\0") if p and _safe_relative(p)):
        src = Path(repo) / rel
        if src.is_symlink() or src.is_file():
            files.append(rel)
    return files


def snapshot_repo(repo: str, dest: Path, max_bytes: int) -> list[str]:
    """Copy every tracked and untracked-but-not-ignored file into ``dest`` (no ``.git``) and return the copied paths."""
    copied: list[str] = []
    total = 0
    for rel in listed_files(repo):
        src, dst = Path(repo) / rel, dest / rel
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


def _git_bool(repo: str, key: str, default: bool) -> bool:
    """Read a boolean git setting for ``repo``; an unset or unreadable value gives ``default``, as git's own does."""
    try:
        value = _git(repo, "config", "--bool", "--get", key).decode().strip()
    except GuardError:
        return default
    return value == "true" if value in ("true", "false") else default


def _snapshot_mode(path: Path, *, exec_bit: bool) -> str:
    """Return the git tree mode of a snapshot entry: ``120000`` for a symlink, else ``100755`` or ``100644``."""
    if path.is_symlink():
        return "120000"
    return "100755" if exec_bit and path.stat().st_mode & 0o100 else "100644"


def _tree_git(repo: str, tree: Path, args: Sequence[str], stdin: bytes) -> bytes:
    """Run one git command with the repository's own configuration and ``tree`` (a snapshot or the checkout) as its work
    tree, so attributes come from the ``.gitattributes`` files in ``tree``, as ``git status`` reads them.

    Raises exit 3 on any failure or timeout: a file git could not judge is never taken for the scanned head.
    """
    git_dir = _git(repo, "rev-parse", "--absolute-git-dir").decode("utf-8", "surrogateescape").strip()
    try:
        done = subprocess.run(  # noqa: S603
            [_GIT, f"--git-dir={git_dir}", f"--work-tree={tree}", "-C", str(tree), "-c", "core.fsmonitor=false", *args],
            input=stdin,
            check=True,
            capture_output=True,
            timeout=_CONVERSION_TIMEOUT,
            env=_GIT_ENV,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuardError(f"git {args[0]} could not compare the working tree with the approved head: {exc}", 3) from exc
    return done.stdout


def _raw_only(repo: str, tree: Path, paths: Sequence[str]) -> set[str]:
    """Return the paths a ``filter`` or ``working-tree-encoding`` attribute applies to (``_RAW_ONLY_ATTRIBUTES``)."""
    if not paths:
        return set()
    stdin = b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in paths)
    out = _tree_git(repo, tree, ["check-attr", "-z", "--stdin", *_RAW_ONLY_ATTRIBUTES], stdin).split(b"\0")
    return {
        path.decode("utf-8", "surrogateescape")
        for path, _name, value in zip(out[0::3], out[1::3], out[2::3])
        if value not in (b"unspecified", b"unset")
    }


def _c_quoted(path: str) -> bytes:
    """Quote a repository path the way git's ``--stdin-paths`` unquotes a line that starts with a double quote."""
    out = bytearray(b'"')
    for byte in path.encode("utf-8", "surrogateescape"):
        if byte in b'"\\':
            out += b"\\" + bytes([byte])
        elif byte < 0x20 or byte == 0x7F:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    return bytes(out + b'"')


def _cleaned_blob_ids(repo: str, tree: Path, paths: Sequence[str]) -> list[str]:
    """Return the blob id git stores for each file of ``tree`` after its clean conversion (end of line and ``ident``).

    One ``git hash-object --stdin-paths`` call, without ``-w``: it writes no object and is what ``git add`` would hash.
    """
    if not paths:
        return []
    stdin = b"".join(_c_quoted(p) + b"\n" for p in paths)
    ids = _tree_git(repo, tree, ["hash-object", "--stdin-paths"], stdin).decode("ascii", "replace").split()
    if len(ids) != len(paths):
        raise GuardError(f"git hash-object returned {len(ids)} ids for {len(paths)} files: the run is refused", 3)
    return ids


def changed_files(repo: str, entries: dict[str, tuple[str, str]], tree: Path, files: Sequence[str]) -> list[str]:
    """Return the listed files whose content is not the head's, judged the way ``git status`` judges a checkout.

    A file is the head's when its raw bytes hash to the head blob, or when the head holds it as a regular file and git's
    own clean conversion of its bytes hashes to that blob: ``core.autocrlf``, ``core.eol``, the ``text`` and ``eol``
    attributes and ``ident`` only rewrite line endings and keyword expansions of the scanned text, so a CRLF checkout of
    LF blobs is the scanned head. A path a ``filter`` or ``working-tree-encoding`` attribute applies to counts only byte
    for byte, since those write bytes the scan never read; such a path never reaches ``git hash-object``, so no filter
    program runs here. Files the head lacks, links, gitlinks and paths the head holds as something else differ unless
    their raw bytes match. Modes are ``mode_delta``'s concern.
    """
    pending: list[str] = []
    changed: set[str] = set()
    for rel in files:
        mode, oid = entries.get(rel, ("", ""))
        path = tree / rel
        if oid == _blob_sha(_snapshot_bytes(path)):
            continue
        if mode in ("100644", "100755") and not path.is_symlink():
            pending.append(rel)
        else:
            changed.add(rel)
    raw_only = _raw_only(repo, tree, pending)
    changed |= raw_only
    convertible = [rel for rel in pending if rel not in raw_only]
    changed |= {
        rel for rel, oid in zip(convertible, _cleaned_blob_ids(repo, tree, convertible)) if oid != entries[rel][1]
    }
    return [rel for rel in files if rel in changed]


def mode_delta(repo: str, entries: dict[str, tuple[str, str]], snapshot: Path, copied: Sequence[str]) -> list[str]:
    """Return copied files whose mode differs from the head tree, judged the way git judges a working tree.

    A blob id covers bytes only, so a new executable bit, or a symlink swapped for a regular file holding the link
    target, would pass the content comparison. Git records the executable bit only while ``core.fileMode`` is on (it is
    off on native Windows and some mounted filesystems), and with ``core.symlinks`` off it checks a link out as a
    regular file holding the target; those differences are ignored here as git ignores them. Paths the head does not
    hold as a file or link are left to the content comparison (``changed_files``).
    """
    exec_bit = _git_bool(repo, "core.fileMode", default=True)
    symlinks = _git_bool(repo, "core.symlinks", default=True)
    changed = []
    for rel in copied:
        expected = entries.get(rel, ("", ""))[0]
        if expected not in ("100644", "100755", "120000"):
            continue
        actual = _snapshot_mode(snapshot / rel, exec_bit=exec_bit)
        if not exec_bit and expected == "100755":
            expected = "100644"
        if not symlinks and expected == "120000" and actual == "100644":
            continue
        if actual != expected:
            changed.append(rel)
    return changed


def check_delta(repo: str, report: dict[str, object], snapshot: Path, copied: Sequence[str]) -> list[str]:
    """Require the approved head to be an ancestor of ``HEAD``, rescan what changed since it, and return those paths.

    What changed is judged as ``git status`` judges it (``changed_files``), so line endings git wrote on checkout are
    not work since the head. Raises exit 3 when the approved head is stale, the delta adds a high finding or touches a
    dependency manifest, or git cannot judge a converted file.
    """
    head = str(report["head"])
    if not _is_ancestor(repo, head, "HEAD"):
        raise GuardError("approved head is not an ancestor of HEAD: rescan and ask again", 3)
    delta = changed_files(repo, _tree_entries(repo, head), snapshot, copied)
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


def require_exact_head(repo: str, report: dict[str, object], tree: Path, files: Sequence[str]) -> list[str]:
    """Refuse (exit 3) unless ``tree`` is exactly the approved head: no changed, added or deleted file, no new mode.

    ``files`` are the paths ``listed_files`` gives for ``tree``: ``run`` passes its snapshot, ``record`` the checkout
    itself, so an approval no run could use is refused before it is recorded. Returns the (empty) delta.
    """
    delta = check_delta(repo, report, tree, files)
    kept, entries = set(files), _tree_entries(repo, str(report["head"]))
    delta += sorted(
        rel
        for rel, (mode, _oid) in entries.items()
        if mode in ("100644", "100755", "120000") and _safe_relative(rel) and rel not in kept
    )  # a file deleted since the scanned head changes what the command sees too
    delta += sorted(set(mode_delta(repo, entries, tree, files)) - set(delta))
    if delta:
        raise GuardError(
            f"the approval covers only the exact scanned head, but the tree differs in {', '.join(delta[:6])}: "
            "rescan and ask again",
            3,
        )
    return delta


# --------------------------------------------------------------------------------------------------------------------
# isolation and run
# --------------------------------------------------------------------------------------------------------------------


def _validate_extra_env(names: Sequence[str]) -> None:
    """Reject an extra environment name that is malformed or looks like a credential."""
    for name in names:
        if not _ENV_NAME.match(name):
            raise GuardError(f"invalid --env name {name!r}", 2)
        if _SECRET_NAME.search(name) or re.fullmatch(_FLAGGED_ENV_NAMES, name, re.IGNORECASE):
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
        if "exact-head" in args.decision:
            delta = require_exact_head(repo, report, snapshot, copied)
        else:
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
    """Add the options ``record`` and ``run`` must receive identically: the approval hash covers every one of them."""
    p.add_argument("--env", action="append")
    p.add_argument("--allow-loopback", action="store_true")
    p.add_argument("--tier", choices=("auto", "sandbox", "env-only"))
    p.add_argument("--decision", action="append", choices=("unsandboxed", "exact-head"))
    p.add_argument("--bind-file", action="append")
    p.add_argument("--scope", choices=("command", "run"))
    p.add_argument("cmd", nargs=argparse.REMAINDER)


def _parser() -> argparse.ArgumentParser:
    """Build the three-subcommand CLI (``scan``, ``record``, ``run``; ``bind`` aliases ``record``)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("scan", "record", "run"):
        # `bind` is the former name of `record`; the Claude Code worktree-isolation check refuses a command carrying
        # that bare word (read as the shell builtin), so callers use `record` and the alias keeps older approvals' calls
        p = sub.add_parser(name, aliases=["bind"] if name == "record" else [])
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
        if name == "record":
            p.add_argument("--out", required=True)
        else:
            p.add_argument("--approval-file", required=True)
            p.add_argument("--timeout", type=int, default=900)
            p.add_argument("--max-snapshot-mb", type=int, default=_DEFAULT_SNAPSHOT_MB)
            p.add_argument("--report")
        _add_run_options(p)
    return parser


def _require_exact_checkout(repo: str, report: dict[str, object]) -> None:
    """Refuse an exact-head ``record`` (exit 3) when the checkout already differs from the scanned head.

    Every run of such an approval would be refused, and the caller can still ask the user instead. ``run`` repeats the
    check on its own snapshot, since the checkout can change after binding; a file that vanishes mid-check refuses.
    """
    try:
        require_exact_head(repo, report, Path(repo), listed_files(repo))
    except OSError as exc:
        raise GuardError(f"the working tree changed while it was compared with the scanned head: {exc}", 3) from exc


def _strip_separator(cmd: list[str]) -> list[str]:
    """Drop the ``--`` that separates options from the command."""
    return cmd[1:] if cmd[:1] == ["--"] else cmd


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch ``scan``, ``record`` (alias ``bind``) or ``run`` and map a guard failure to its exit status."""
    args = _parser().parse_args(argv)
    if args.mode == "bind":
        args.mode = "record"
    try:
        if args.mode == "run":
            return run_command(args)
        if args.mode == "record":
            _apply_defaults(args)
            args.cmd = _strip_separator(args.cmd)
            if not args.cmd and args.scope != "run":
                raise GuardError("record needs a command after -- unless --scope run", 2)
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
        if args.mode == "record":
            if "exact-head" in args.decision:
                _require_exact_checkout(repo, report)
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
