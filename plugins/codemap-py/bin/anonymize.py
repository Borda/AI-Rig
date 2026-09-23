#!/usr/bin/env python
"""anonymize.py — replace project-identifying names in codemap JSONL logs with salted pseudonyms.

Scrubbed: qualified names in the ``args`` payload and every element of ``argv``; every
identifying token in the ``intent``/``target``/``search_path``/``project`` command fields
and in the identity-bearing keys ``module``, ``qname``, ``qualified_name``,
``index_path``, ``path``, ``caller``, ``callee``, and the ``imported_by``/
``changed_modules`` lists wherever they appear, including nested inside ``result``; once
inside the ``result`` subtree, also ``source``, ``imports``, ``name``, ``resolved_qname``,
``file``, ``pattern``, ``hint``, and the ``unique_qualified_names``/``untracked_py``/
``suggestions`` lists (result-scoped because these bare key names collide with unrelated
top-level record fields, e.g. ``record["source"] == "bench"``); qualified names embedded
in ``error``/``stderr`` prose; the ``session``/``hook_session`` join keys; and the session
id embedded in the output filename. Kept: timestamps, counts, flags, tool names, and the
command shape itself.

This is a field denylist, not a schema-complete scrub: a new query-result shape that
introduces an identifying field name not listed above passes through unscrubbed until
that key is added here (see :data:`_COMMAND_FIELDS` and :data:`_RESULT_FIELDS`).

Pseudonyms are stable within a project (same salt + same name → same pseudonym)
but opaque to anyone without the salt file. Never share the salt alongside the
anonymized log — the salt lives only at ``--salt`` path (default local to project).

Anonymized ``-anon.jsonl`` files are written to a dedicated export directory
(``--out-dir``, default ``.cache/codemap/export/``) that is deliberately separate
from the salt directory. Writing anonymized output into any directory that holds a
``.salt`` file is refused outright: a recipient handed both the output and the salt
could reverse every pseudonym.

Usage:
    python anonymize.py --input cli.jsonl
    python anonymize.py --input skills.jsonl --out-dir .cache/codemap/export [--salt PATH]
    python anonymize.py --input cli.jsonl --output /explicit/path/cli-anon.jsonl
    python anonymize.py --input .cache/codemap/logs --out-dir .cache/codemap/export

Exit codes:
    0 — success (directory mode counts oversized files as skipped instead of failing)
    1 — input not found, or a file input larger than MAX_LOG_SIZE
    2 — refused: output directory contains a salt file, or ``--output`` with a directory input
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path

#: Default directory for anonymized ``-anon.jsonl`` output. Deliberately distinct
#: from the log/salt directory so anonymized shards are never written next to the
#: salt that would make their pseudonyms reversible.
DEFAULT_OUT_DIR = ".cache/codemap/export"

#: Filename that, when present in a target directory, marks it as salt-bearing.
#: Writing anonymized output beside this file would let a recipient of both the
#: output and the salt reverse every pseudonym — so it is refused.
SALT_FILENAME = ".salt"

#: Maximum accepted input log size — the 50 MB value ``scan-stats.py`` and
#: ``smoke_test_index.py`` already use for ``MAX_INDEX_SIZE`` (CWE-400: DoS guard).
#: Caps the whole file only: a single pathologically long line inside an
#: under-cap file is still read in full.
MAX_LOG_SIZE = 50_000_000

#: Exit code returned when the resolved output directory holds a salt file.
_EXIT_UNSAFE_OUT_DIR = 2
_EXIT_DIRECTORY_OUTPUT = 2

#: Matches a qualified-name token embedded in free text: an identifier followed by
#: at least one ``.`` or ``::`` separator plus a further identifier (e.g. ``pkg.auth``,
#: ``pkg.auth::login``, ``mod::Class.method``). Lets error/stderr prose be scrubbed
#: token-by-token without swallowing the surrounding words or punctuation.
_QUALIFIED_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::)[A-Za-z_][A-Za-z0-9_]*)+")

#: Record fields whose string values are free-text that may embed qualified names
#: inline (error messages, captured ``stderr``/tracebacks). Scrubbed per-token,
#: preserving surrounding prose, rather than replaced wholesale.
_FREE_TEXT_FIELDS = ("error", "stderr")

#: Fields holding a command line, a file path, a project root, or a qualified name:
#: ``target`` (Read path / Grep pattern / search command), ``search_path`` (Grep/Glob
#: scope), ``intent`` (skill arguments), ``project`` (the CLI's own working-tree root,
#: 0.39.4+), and the identity-bearing ``result``-payload keys ``module``, ``qname``,
#: ``qualified_name``, ``index_path``, ``path``, ``caller``, ``callee`` — wherever any of
#: these keys appear, including nested inside ``result``, since :func:`_scrub_special_fields`
#: already walks every nesting depth. Every identifier in them is project data, not only the
#: dotted ones — for example, ``grep -rn internal_secret_name src/`` carries no dot at all —
#: so they are scrubbed with :func:`_anonymize_command`, which pseudonymizes bare identifiers too.
#:
#: This is a field denylist: a new query-result shape that introduces a differently-named
#: identifying field passes through unscrubbed until its key is added to this tuple (or to
#: :data:`_COMMAND_LIST_FIELDS` for a list of such names) — an honest, bounded fix, not a
#: claim of exhaustive coverage.
_COMMAND_FIELDS = (
    "intent", "target", "search_path", "project",
    "module", "qname", "qualified_name", "index_path", "path", "caller", "callee",
)  # fmt: skip

#: List-valued identifying fields: each element is a qualified module name (e.g.
#: ``imported_by``/``changed_modules`` hold lists of dotted module paths). Scrubbed
#: element-by-element via :func:`_anonymize_command` in :func:`_scrub_field`, mirroring
#: the existing ``not_covered`` per-element rule.
_COMMAND_LIST_FIELDS = ("imported_by", "changed_modules")

#: String-valued keys that only carry identity when nested inside the ``result``
#: payload — gated on ``in_result`` rather than added to :data:`_COMMAND_FIELDS`
#: because the bare key name collides with an unrelated top-level record field:
#: ``record["source"] == "bench"`` (``join_avoidance.py``) marks a synthetic
#: benchmark record, so scrubbing ``source`` unconditionally would silently break
#: that filter. Found leaking verbatim in a scan of real telemetry
#: (``.cache/codemap/logs``, 847 shards, 16566 records): function source bodies
#: and import blocks in ``symbols[]`` (``source``/``imports``), bare identifier
#: names in ``central``/``coupled``/``matches``/``symbols``/``undocumented``/
#: ``uncovered`` entries (``name``), a resolved qualified name missed by
#: :data:`_COMMAND_FIELDS`'s ``qname``/``qualified_name`` (``resolved_qname``), a
#: broken-file path (``file``), and a raw search pattern (``pattern``); a
#: command-shaped hint string with an embedded bare symbol, e.g.
#: ``grep -rn "atomic_publish"`` (``hint``).
#:
#: Key-based matching means the same key can carry two different meanings under
#: ``result`` and both get this treatment: ``symbols[].source`` is a function body
#: (the leak this set exists to close) but ``xrefs``' ``broken[].source`` is a fixed
#: diagnostic label (e.g. ``"sphinx"``) that gets pseudonymized too even though it
#: was never identifying. Per the module's own design (see :data:`_GENERIC_TOKENS`):
#: a safe field losing readability is the accepted failure mode, never a leak.
_RESULT_FIELDS = ("source", "imports", "name", "resolved_qname", "file", "pattern", "hint")

#: List-valued counterpart to :data:`_RESULT_FIELDS`, same result-only gating and
#: same telemetry scan: lists of untracked-file paths and qualified/suggested names.
_RESULT_LIST_FIELDS = ("unique_qualified_names", "untracked_py", "suggestions")

#: Join keys that identify one local Claude Code session. Replaced by a stable
#: pseudonym: cross-layer joins survive (same salt → same pseudonym) while the raw
#: id, which correlates an export back to the machine that produced it, does not leave.
_SESSION_FIELDS = ("session", "hook_session")

#: Any identifier-shaped token, qualified or bare — the unit :func:`_anonymize_command`
#: decides about. Punctuation, digits, flags and separators fall between matches and
#: therefore survive, which is what keeps a scrubbed command still readable as a command.
_FREE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::)[A-Za-z_][A-Za-z0-9_]*)*")

#: Tokens kept verbatim inside a command field: search tools the telemetry exists to
#: measure, conventional directory names, and Python vocabulary that appears in grep
#: patterns. Deliberately short — a token missing from this set is pseudonymized, so
#: the failure mode is a less readable export, never a leak.
_GENERIC_TOKENS = frozenset(
    {
        "ack", "ag", "awk", "cat", "egrep", "fgrep", "find", "git", "grep", "head",
        "python", "python3", "rg", "sed", "sort", "tail", "uniq", "wc", "xargs",
        "bin", "docs", "lib", "src", "test", "tests",
        "class", "def", "from", "import",
    }
)  # fmt: skip

#: File extensions kept verbatim while the stem they belong to is pseudonymized, so an
#: export still shows *what kind* of file was touched without naming it.
_KNOWN_SUFFIXES = frozenset(
    {"c", "cfg", "cpp", "h", "ini", "js", "json", "jsonl", "md", "py", "pyi", "rst", "toml", "ts", "txt", "yaml", "yml"}
)

#: Shortest token that can identify anything. Below it a token is a flag cluster or an
#: abbreviation (``-rn`` → ``rn``); keeping those is what preserves command shape.
_MIN_IDENTIFYING_LEN = 3

#: Telemetry shard stems, whose ``<layer>_<session-id>`` form would otherwise carry the
#: raw session id into the anonymized filename.
_SHARD_STEM_RE = re.compile(r"^(cli|tools|skills)_(.+)$")


def _load_salt(salt_file: Path) -> bytes:
    """Load salt from file, creating it with a fresh random value if absent.

    Args:
        salt_file: Path to the salt file (hex-encoded 32 bytes).

    The salt file is created 0o600 (owner read/write only): the module guarantees
    pseudonyms are "opaque to anyone without the salt", so a world/group-readable
    salt on a multi-user host or shared CI runner would let any local user reverse
    every pseudonym. ``O_CREAT | O_EXCL`` also closes the exists→create race — a
    concurrent writer that wins the create is deferred to by re-reading the file.

    Returns:
        32-byte salt as raw bytes.

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.TemporaryDirectory() as d:
        ...     s = _load_salt(pathlib.Path(d) / ".salt")
        ...     len(s) == 32 and s == _load_salt(pathlib.Path(d) / ".salt")
        True
    """
    if salt_file.exists():
        return bytes.fromhex(salt_file.read_text().strip())
    salt = secrets.token_bytes(32)
    salt_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(salt_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # A concurrent writer created the salt between our exists-check and open;
        # defer to it rather than overwrite so both processes share one salt.
        return bytes.fromhex(salt_file.read_text().strip())
    with os.fdopen(fd, "w") as f:
        if hasattr(os, "fchmod"):
            os.fchmod(f.fileno(), 0o600)  # exact 0o600 regardless of the process umask
        f.write(salt.hex())
    return salt


def _pseudo(value: str, salt: bytes) -> str:
    """Return a stable salted pseudonym for a qualified name.

    Args:
        value: The original symbol or module name.
        salt: Per-project random salt.

    Returns:
        Short pseudonym string starting with ``sym_``.

    Examples:
        >>> s = b'x' * 32
        >>> p = _pseudo("pkg.auth::login", s)
        >>> p.startswith("sym_") and len(p) == 16
        True
        >>> _pseudo("pkg.auth::login", s) == _pseudo("pkg.auth::login", s)
        True
    """
    digest = hashlib.sha256(salt + value.encode()).hexdigest()[:12]
    return f"sym_{digest}"


def _is_qualified(v: str) -> bool:
    """Return whether ``v`` contains ``.`` or ``::`` as a qualified-name separator.

    Args:
        v: String to check.

    Returns:
        Whether ``v`` appears to be a qualified name.

    Examples:
        >>> _is_qualified("pkg.auth")
        True
        >>> _is_qualified("pkg::login")
        True
        >>> _is_qualified("short")
        False
    """
    return "." in v or "::" in v


def _anonymize_text(text: str, salt: bytes) -> str:
    """Replace every qualified-name token embedded in free text with its pseudonym.

    Unlike :func:`_anonymize_value`, which pseudonymizes a string only when the
    *whole* value is a qualified name, this scans inside prose (error messages,
    captured stderr / tracebacks) and rewrites each qualified token found while
    leaving the surrounding words and punctuation intact.

    Args:
        text: Free-text string that may contain zero or more qualified names.
        salt: Per-project salt bytes.

    Returns:
        The text with each qualified-name token replaced by its ``sym_`` pseudonym.

    Examples:
        >>> s = b'x' * 32
        >>> out = _anonymize_text("module pkg.auth::login not indexed", s)
        >>> "pkg.auth::login" in out
        False
        >>> out.startswith("module sym_") and out.endswith(" not indexed")
        True
        >>> _anonymize_text("no qualified names here", s)
        'no qualified names here'
    """
    return _QUALIFIED_TOKEN_RE.sub(lambda m: _pseudo(m.group(0), salt), text)


def _pseudo_token(token: str, salt: bytes) -> str:
    """Return the pseudonym for one command token, or the token when it identifies nothing.

    Three tokens survive verbatim: a known file extension (its stem is still hashed, so
    ``auth.py`` becomes ``sym_….py`` — the file type is diagnostic, the name is not), a
    generic tool/structure word, and anything shorter than
    :data:`_MIN_IDENTIFYING_LEN`.

    Args:
        token: One identifier-shaped token from a command or path.
        salt: Per-project salt bytes.

    Returns:
        The token, pseudonymized unless it is provably non-identifying.

    Examples:
        >>> s = b'x' * 32
        >>> _pseudo_token("grep", s)
        'grep'
        >>> _pseudo_token("rn", s)
        'rn'
        >>> _pseudo_token("auth.py", s).startswith("sym_") and _pseudo_token("auth.py", s).endswith(".py")
        True
        >>> _pseudo_token("internal_secret_name", s).startswith("sym_")
        True
    """
    stem, dot, suffix = token.rpartition(".")
    if dot and stem and suffix.lower() in _KNOWN_SUFFIXES:
        return f"{_pseudo_token(stem, salt)}.{suffix}"
    if len(token) < _MIN_IDENTIFYING_LEN or token.lower() in _GENERIC_TOKENS:
        return token
    return _pseudo(token, salt)


def _anonymize_command(text: str, salt: bytes) -> str:
    """Pseudonymize every identifying token in a command line, path, or intent string.

    Stricter than :func:`_anonymize_text`, which only rewrites *qualified* names: a
    command field regularly carries project data with no dot in it at all, so a
    whole-value "is this qualified?" gate exported it verbatim. Working token-by-token
    keeps flags, separators and punctuation, so the scrubbed value still reads as the
    command it was.

    Args:
        text: The raw command, path, or intent string.
        salt: Per-project salt bytes.

    Returns:
        The string with every identifying token replaced by its ``sym_`` pseudonym.

    Examples:
        >>> s = b'x' * 32
        >>> out = _anonymize_command("grep -rn internal_secret_name src/", s)
        >>> "internal_secret_name" in out
        False
        >>> out.startswith("grep -rn sym_") and out.endswith(" src/")
        True
        >>> _anonymize_command("/repo/src/auth.py", s).endswith(".py")
        True
    """
    return _FREE_TOKEN_RE.sub(lambda m: _pseudo_token(m.group(0), salt), text)


def _anonymize_stem(stem: str, salt: bytes) -> str:
    """Pseudonymize the session id embedded in a telemetry shard filename stem.

    Args:
        stem: Input filename stem (e.g. ``tools_abc-123`` or ``cli``).
        salt: Per-project salt bytes.

    Returns:
        The stem with any ``<layer>_<session-id>`` suffix pseudonymized; other stems
        are returned unchanged.

    Examples:
        >>> s = b'x' * 32
        >>> _anonymize_stem("cli", s)
        'cli'
        >>> _anonymize_stem("tools_abc-123", s).startswith("tools_sym_")
        True
        >>> "abc-123" in _anonymize_stem("tools_abc-123", s)
        False
    """
    match = _SHARD_STEM_RE.match(stem)
    return f"{match.group(1)}_{_pseudo(match.group(2), salt)}" if match else stem


def _anonymize_value(v: object, salt: bytes) -> object:
    """Recursively replace qualified names with pseudonyms.

    Args:
        v: Any JSON-compatible value.
        salt: Per-project salt.

    Returns:
        Value with qualified strings replaced.
    """
    if isinstance(v, str) and _is_qualified(v):
        return _pseudo(v, salt)
    if isinstance(v, dict):
        return {k: _anonymize_value(val, salt) for k, val in v.items()}
    if isinstance(v, list):
        return [_anonymize_value(item, salt) for item in v]
    return v


def _scrub_field(key: str, val: object, salt: bytes, *, in_result: bool = False) -> object:
    """Return *val* scrubbed by the rule its *key* selects, or ``None`` when no rule applies.

    Splitting the per-key decision out of :func:`_scrub_special_fields` keeps that
    function's recursion readable as one thing. ``None`` means "not a special field" —
    special fields never legitimately hold ``None``, since every rule below demands a
    ``str`` or ``list``.

    Args:
        key: The dict key being inspected.
        val: Its value.
        salt: Per-project salt bytes.
        in_result: True while the caller is recursing inside the ``result`` payload.
            Gates :data:`_RESULT_FIELDS`/:data:`_RESULT_LIST_FIELDS`, whose key names
            (e.g. ``source``) collide with unrelated top-level record fields — see
            their module-level docstring.

    Returns:
        The scrubbed value, or ``None`` to let the caller recurse instead.

    Examples:
        >>> s = b'x' * 32
        >>> _scrub_field("error", "boom in pkg.auth", s).startswith("boom in sym_")
        True
        >>> _scrub_field("session", "abc-123", s).startswith("sym_")
        True
        >>> _scrub_field("project", "/Users/jsmith/proj", s).count("/")
        3
        >>> out = _scrub_field("imported_by", ["pkg.auth", "pkg.other"], s)
        >>> all(v.startswith("sym_") for v in out)
        True
        >>> "atomic_publish" in _scrub_field("name", "atomic_publish", s, in_result=True)
        False
        >>> _scrub_field("name", "atomic_publish", s, in_result=False) is None
        True
        >>> _scrub_field("timing_ms", 12, s) is None
        True
    """
    # Merged conditions keep return points <= the PLR0911 cap: two field families share
    # one transform each (_anonymize_command for scalars, the same list comprehension
    # for lists), so the in_result-gated result-only rule folds into its sibling instead
    # of adding its own return.
    if key in _FREE_TEXT_FIELDS and isinstance(val, str):
        return _anonymize_text(val, salt)
    if isinstance(val, str) and (key in _COMMAND_FIELDS or (in_result and key in _RESULT_FIELDS)):
        return _anonymize_command(val, salt)
    if isinstance(val, list) and (key in _COMMAND_LIST_FIELDS or (in_result and key in _RESULT_LIST_FIELDS)):
        return [_anonymize_command(e, salt) if isinstance(e, str) else e for e in val]
    if key in _SESSION_FIELDS and isinstance(val, str) and val:
        return _pseudo(val, salt)
    if key == "not_covered" and isinstance(val, list):
        return [_anonymize_text(e, salt) if isinstance(e, str) else e for e in val]
    return None


def _scrub_special_fields(v: object, salt: bytes, *, in_result: bool = False) -> object:
    """Recursively scrub the free-text, command, session, result, and ``not_covered`` fields.

    Walks any nested dict/list structure and applies :func:`_scrub_field` to every key.
    Qualified names embedded in ``error``/``stderr`` prose are pseudonymized while the
    surrounding words survive; ``intent``/``target``/``search_path``/``project`` and the
    identity-bearing keys in :data:`_COMMAND_FIELDS` additionally lose their bare
    identifiers, at any nesting depth; once recursion enters the ``result`` subtree the
    :data:`_RESULT_FIELDS`/:data:`_RESULT_LIST_FIELDS` rules also apply, at any depth
    below that point; ``session``/``hook_session`` become stable pseudonyms; each element
    of a :data:`_COMMAND_LIST_FIELDS`/:data:`_RESULT_LIST_FIELDS` list (e.g.
    ``imported_by``) is pseudonymized; and each ``not_covered`` element is scrubbed
    individually, so qualified-name elements become pseudonyms while plain diagnostic
    labels (e.g. ``lazy-loading``) pass through.

    This complements — and is applied alongside — :func:`_anonymize_value`, which
    handles whole-value qualified names in the ``args`` payload.

    Args:
        v: Any JSON-compatible value (record, nested dict, list, or scalar).
        salt: Per-project salt bytes.
        in_result: True once recursion has entered the ``result`` subtree. Set
            automatically the moment a ``"result"`` key is visited; callers pass the
            default and never set it explicitly.

    Returns:
        A new value with the special fields scrubbed; non-special data unchanged.

    Examples:
        >>> s = b'x' * 32
        >>> out = _scrub_special_fields({"error": "boom in pkg.auth"}, s)
        >>> out["error"].startswith("boom in sym_")
        True
        >>> nc = _scrub_special_fields({"not_covered": ["a.b", "lazy-loading"]}, s)
        >>> nc["not_covered"][0].startswith("sym_"), nc["not_covered"][1]
        (True, 'lazy-loading')
        >>> r = _scrub_special_fields({"result": {"symbols": [{"name": "atomic_publish"}]}}, s)
        >>> "atomic_publish" in r["result"]["symbols"][0]["name"]
        False
        >>> u = _scrub_special_fields({"source": "bench"}, s)
        >>> u["source"]
        'bench'
    """
    if isinstance(v, dict):
        scrubbed: dict = {}
        for key, val in v.items():
            nested = in_result or key == "result"
            replacement = _scrub_field(key, val, salt, in_result=nested)
            scrubbed[key] = _scrub_special_fields(val, salt, in_result=nested) if replacement is None else replacement
        return scrubbed
    if isinstance(v, list):
        return [_scrub_special_fields(item, salt, in_result=in_result) for item in v]
    return v


def anonymize_record(record: dict, salt: bytes) -> dict:
    """Anonymize one JSONL log record in-place (returns new dict).

    Replaces qualified names in the ``args`` payload and pseudonymizes every string
    element of ``argv`` (path or not — a dot-free absolute path such as
    ``/Users/x/Workspace/myproject`` carries no ``.``/``::`` and would survive a
    whole-value "is this qualified?" gate). In addition, and wherever those fields
    appear (including nested inside ``result``), scrubs qualified names out of the
    free-text ``error`` / ``stderr`` fields, every identifying token out of the
    ``intent`` / ``target`` / ``search_path`` / ``project`` command fields and the
    identity-bearing keys in :data:`_COMMAND_FIELDS` / :data:`_COMMAND_LIST_FIELDS`,
    every identifying token found *anywhere inside* ``result`` under the
    :data:`_RESULT_FIELDS` / :data:`_RESULT_LIST_FIELDS` keys (e.g. verbatim source
    bodies in ``symbols[].source``, bare names in ``central[].name``), the
    ``session`` / ``hook_session`` join keys, and each element of any ``not_covered``
    list. Leaves all other fields (timestamps, counts, flags, and top-level fields
    that share a name with a result-only key, such as the ``source`` marking a
    synthetic benchmark record) unchanged.

    Args:
        record: Parsed log record.
        salt: Per-project salt bytes.

    Returns:
        New dict with qualified names replaced by pseudonyms.

    Examples:
        >>> s = b'x' * 32
        >>> r = anonymize_record({"cmd": "rdeps", "args": {"module": "pkg.auth"}}, s)
        >>> r["args"]["module"].startswith("sym_")
        True
        >>> r["cmd"]
        'rdeps'
        >>> e = anonymize_record({"result": {"error": "module pkg.auth not indexed"}}, s)
        >>> "pkg.auth" in e["result"]["error"]
        False
        >>> t = anonymize_record({"tool": "Bash", "target": "grep -rn secret_name src/"}, s)
        >>> "secret_name" in t["target"], t["tool"]
        (False, 'Bash')
        >>> p = anonymize_record({"project": "/Users/jsmith/Workspace/myproject"}, s)
        >>> "jsmith" in p["project"], p["project"].count("/")
        (False, 4)
        >>> av = anonymize_record({"argv": ["--root", "/Users/jsmith/Workspace/myproject"]}, s)
        >>> "jsmith" in av["argv"][1], av["argv"][1].startswith("/")
        (False, True)
        >>> m = anonymize_record({"result": {"module": "pkg.auth.core"}}, s)
        >>> "pkg.auth.core" in m["result"]["module"]
        False
        >>> one_sym = {"name": "atomic_publish", "source": "def atomic_publish(): ..."}
        >>> src = anonymize_record({"result": {"symbols": [one_sym]}}, s)
        >>> sym = src["result"]["symbols"][0]
        >>> "atomic_publish" in sym["name"], "atomic_publish" in sym["source"]
        (False, False)
        >>> bench = anonymize_record({"source": "bench", "cmd": "rdeps"}, s)
        >>> bench["source"]
        'bench'
    """
    out = _scrub_special_fields(record, salt)
    assert isinstance(out, dict)  # a dict in always yields a dict out
    if "args" in out and isinstance(out["args"], dict):
        out["args"] = _anonymize_value(out["args"], salt)
    if "argv" in out and isinstance(out["argv"], list):
        out["argv"] = [_anonymize_command(a, salt) if isinstance(a, str) else a for a in out["argv"]]
    return out


def _dir_has_salt(directory: Path) -> bool:
    """Return True if ``directory`` contains a salt file.

    Args:
        directory: Directory to inspect (may not yet exist).

    Returns:
        Whether a :data:`SALT_FILENAME` file lives directly in the directory.

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.TemporaryDirectory() as d:
        ...     _dir_has_salt(pathlib.Path(d))
        False
        >>> with tempfile.TemporaryDirectory() as d:
        ...     _ = (pathlib.Path(d) / ".salt").write_text("00")
        ...     _dir_has_salt(pathlib.Path(d))
        True
    """
    return (directory / SALT_FILENAME).exists()


def _resolve_output(
    input_path: Path, out_dir: str | None, explicit_output: str | None, salt: bytes | None = None
) -> Path:
    """Resolve the anonymized output path from CLI flags.

    An explicit ``--output`` wins when given. Otherwise the file is named
    ``<input-stem>-anon.jsonl`` inside ``out_dir`` (default :data:`DEFAULT_OUT_DIR`).
    With *salt*, a ``<layer>_<session-id>`` stem is pseudonymized first, so the raw
    session id does not survive in the exported filename either. Without it the
    directory is still resolved correctly, which is all the salt-safety check needs.

    Args:
        input_path: The source log file.
        out_dir: ``--out-dir`` value, or None to use the default export directory.
        explicit_output: ``--output`` value, or None to derive from ``out_dir``.
        salt: Per-project salt bytes, or None to keep the input stem verbatim.

    Returns:
        The resolved destination path (not yet created).

    Examples:
        >>> import pathlib
        >>> _resolve_output(pathlib.Path("logs/cli.jsonl"), None, None).as_posix()
        '.cache/codemap/export/cli-anon.jsonl'
        >>> _resolve_output(pathlib.Path("logs/cli.jsonl"), "exp", None).as_posix()
        'exp/cli-anon.jsonl'
        >>> _resolve_output(pathlib.Path("logs/cli.jsonl"), None, "out/x.jsonl").as_posix()
        'out/x.jsonl'
        >>> _resolve_output(pathlib.Path("logs/tools_abc.jsonl"), "exp", None, b'x' * 32).name
        'tools_sym_ae56085268ec-anon.jsonl'
    """
    if explicit_output is not None:
        return Path(explicit_output)
    base = out_dir if out_dir is not None else DEFAULT_OUT_DIR
    stem = _anonymize_stem(input_path.stem, salt) if salt is not None else input_path.stem
    return Path(base) / f"{stem}-anon.jsonl"


def _directory_output(input_dir: Path, input_path: Path, out_dir: str | None, salt: bytes | None = None) -> Path:
    """Resolve one directory-input export path while retaining its relative topology.

    Args:
        input_dir: Root directory supplied through ``--input``.
        input_path: JSONL file found below ``input_dir``.
        out_dir: Export root, or ``None`` for :data:`DEFAULT_OUT_DIR`.
        salt: Per-project salt bytes, or ``None`` before salt-safety validation.

    Returns:
        The derived export path below the selected export root.
    """
    relative = input_path.relative_to(input_dir)
    stem = _anonymize_stem(relative.stem, salt) if salt is not None else relative.stem
    base = Path(out_dir) if out_dir is not None else Path(DEFAULT_OUT_DIR)
    return base / relative.parent / f"{stem}-anon.jsonl"


def _directory_inputs(input_dir: Path, exclude_root: Path | None = None) -> list[Path]:
    """Return every JSONL file below a directory input in deterministic order.

    Files under *exclude_root* (the export destination) are skipped so a re-run whose export root sits inside the input
    tree never re-ingests its own prior anonymized exports.
    """
    paths = (path for path in input_dir.rglob("*.jsonl") if path.is_file())
    if exclude_root is not None:
        resolved = exclude_root.resolve()
        paths = (path for path in paths if not path.resolve().is_relative_to(resolved))
    return sorted(paths)


def _output_paths(
    input_path: Path, inputs: list[Path], out_dir: str | None, explicit_output: str | None, salt: bytes | None = None
) -> list[Path]:
    """Resolve derived exports for a file input or every file below a directory input."""
    if input_path.is_dir():
        return [_directory_output(input_path, path, out_dir, salt) for path in inputs]
    return [_resolve_output(input_path, out_dir, explicit_output, salt)]


def _unsafe_output_path(output_paths: list[Path], salt_path: Path) -> Path | None:
    """Return the first target that would place an export beside a salt file."""
    return next(
        (
            path
            for path in output_paths
            if _dir_has_salt(path.parent) or path.parent.resolve() == salt_path.parent.resolve()
        ),
        None,
    )


def _process_inputs(inputs: list[Path], output_paths: list[Path], salt: bytes) -> tuple[int, int, int]:
    """Write bounded anonymized exports and return processed, skipped, and oversized counts."""
    processed = skipped = oversized = 0
    for source_path, output_path in zip(inputs, output_paths, strict=True):
        try:
            size = source_path.stat().st_size
        except OSError:
            skipped += 1
            continue
        if size > MAX_LOG_SIZE:
            oversized += 1
            print(
                f"anonymize: skipping oversized input ({size} bytes; max {MAX_LOG_SIZE}): {source_path}",
                file=sys.stderr,
            )
            continue
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current_processed, current_skipped = process(source_path, output_path, salt)
        except OSError as exc:
            skipped += 1
            print(f"anonymize: skipping unreadable input: {source_path}: {exc}", file=sys.stderr)
            continue
        processed += current_processed
        skipped += current_skipped
    return processed, skipped, oversized


def process(input_path: Path, output_path: Path, salt: bytes) -> tuple[int, int]:
    """Anonymize all records in input_path and write to output_path.

    Args:
        input_path: Source JSONL file.
        output_path: Destination JSONL file (overwritten if exists).
        salt: Per-project salt bytes.

    Returns:
        ``(records_processed, records_skipped)`` tuple.

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        ...     _ = f.write('{"cmd":"rdeps","args":{"module":"pkg.auth"}}\\n')
        ...     tmp = pathlib.Path(f.name)
        >>> out = tmp.with_suffix('.out.jsonl')
        >>> process(tmp, out, b'x' * 32)
        (1, 0)
        >>> out.unlink(); tmp.unlink()
    """
    processed = skipped = 0
    with input_path.open() as fin, output_path.open("w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                anon = anonymize_record(record, salt)
                fout.write(json.dumps(anon, separators=(",", ":")) + "\n")
                processed += 1
            except Exception:  # noqa: BLE001
                skipped += 1
    return processed, skipped


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the anonymize CLI."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="Source JSONL log file or directory tree")
    parser.add_argument(
        "--output",
        default=None,
        help="Explicit destination file (file input only; overrides --out-dir and is refused beside a salt file)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help=f"Export root for derived outputs (default {DEFAULT_OUT_DIR}; directory input preserves its relative topology)",
    )
    parser.add_argument(
        "--salt",
        default=".cache/codemap/logs/.salt",
        help="Salt file path (created with random value if absent; keep local — never share)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the anonymize CLI.

    Args:
        argv: Override ``sys.argv[1:]`` (mainly for testing).

    Returns:
        0 on success; 1 if the input is missing, or a file input is oversized (directory
        mode skips and counts oversized files instead); 2 if the output directory holds a
        salt file, or ``--output`` is combined with a directory input.
    """
    args = _build_parser().parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"anonymize: input not found: {input_path}", file=sys.stderr)
        return 1

    is_directory = input_path.is_dir()
    if is_directory and args.output is not None:
        print(
            "anonymize: --output is only valid for a file --input; use --out-dir for directory input", file=sys.stderr
        )
        return _EXIT_DIRECTORY_OUTPUT

    export_root = Path(args.out_dir) if args.out_dir is not None else Path(DEFAULT_OUT_DIR)
    inputs = _directory_inputs(input_path, export_root) if is_directory else [input_path]
    if not is_directory and input_path.stat().st_size > MAX_LOG_SIZE:
        print(
            f"anonymize: input too large ({input_path.stat().st_size} bytes; max {MAX_LOG_SIZE}): {input_path}",
            file=sys.stderr,
        )
        return 1

    salt_path = Path(args.salt)
    output_paths = _output_paths(input_path, inputs, args.out_dir, args.output)
    unsafe_path = _unsafe_output_path(output_paths, salt_path)
    if unsafe_path is not None:
        print(
            f"anonymize: refusing to write into {unsafe_path.parent} — it contains a '{SALT_FILENAME}' file; "
            f"anonymized output beside the salt is reversible. Use --out-dir (default {DEFAULT_OUT_DIR}).",
            file=sys.stderr,
        )
        return _EXIT_UNSAFE_OUT_DIR

    salt = _load_salt(salt_path)
    output_paths = _output_paths(input_path, inputs, args.out_dir, args.output, salt)
    unsafe_path = _unsafe_output_path(output_paths, salt_path)
    if unsafe_path is not None:
        print(
            f"anonymize: refusing to write into {unsafe_path.parent} — it contains a '{SALT_FILENAME}' file; "
            f"anonymized output beside the salt is reversible. Use --out-dir (default {DEFAULT_OUT_DIR}).",
            file=sys.stderr,
        )
        return _EXIT_UNSAFE_OUT_DIR
    processed, skipped, oversized = _process_inputs(inputs, output_paths, salt)

    if is_directory:
        output_root = export_root
        details = [f"{skipped} skipped" for _ in [None] if skipped]
        details.extend(f"{oversized} oversized" for _ in [None] if oversized)
        detail = f" ({', '.join(details)})" if details else ""
        print(f"anonymize: {processed} records from {len(inputs)} files → {output_root}{detail}")
    else:
        print(f"anonymize: {processed} records → {output_paths[0]}" + (f" ({skipped} skipped)" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
